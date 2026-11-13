import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta
from urllib.parse import urlparse

from dotenv import dotenv_values
from werkzeug.security import check_password_hash, generate_password_hash

import env_schema
import forecast

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "app.db")

# 查询间隔的合法范围（秒）。下限防止把日本邮政官网刷成被封，上限一周；
# 前端输入框的 min / max 与这里同步。
MIN_CHECK_INTERVAL = 30
MAX_CHECK_INTERVAL = 7 * 24 * 3600

# 任务事件（时间线）的类型穷举：写入时校验，前端文案/配色/圆点映射与之一一对应（测试会比对两边）。
EVENT_KIND_META = {
    "status_change": {"label": "状态变化", "tone": "violet", "dot": "info"},
    "push_ok": {"label": "推送成功", "tone": "ok", "dot": "ok"},
    "push_fail": {"label": "推送失败", "tone": "err", "dot": "err"},
    "query_error": {"label": "查询出错", "tone": "warn", "dot": "warn"},
    "query_recovered": {"label": "查询恢复", "tone": "cyan", "dot": "info"},
    "auto_archived": {"label": "签收自动归档", "tone": "ok", "dot": "ok"},
}
EVENT_KINDS = frozenset(EVENT_KIND_META)

# 事件保留策略：每个任务最多留这么多条，且整体只留最近这么多天；写入时顺手清理。
EVENT_MAX_PER_TASK = 500
EVENT_RETENTION_DAYS = 180
# 事件分页每页上限
EVENT_PAGE_MAX = 100

PROFILE_DEFAULTS = {
    "check_interval": 300,
    "bark_keys": "",
    "bark_query_params": "?sound=minuet&level=timeSensitive",
    "bark_url_enabled": 1,
}


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _connect():
    """打开连接。调用方必须用 closing(...) 包起来——
    sqlite3 的 __exit__ 只提交/回滚事务，不会关闭连接。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _has_table(conn, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _has_column(conn, table_name: str, column: str) -> bool:
    if not _has_table(conn, table_name):
        return False
    return any(row["name"] == column for row in conn.execute(f"PRAGMA table_info({table_name})"))


def _load_env_value(env_values, key: str, default: str = "") -> str:
    """.env 文件里出现过的键以文件为准（包括显式留空），没出现才回退到进程环境变量。
    否则后台把某项清空后，追踪脚本仍会读到启动时继承的旧环境变量，清空等于没生效。"""
    if key in env_values:
        value = env_values.get(key)
        return str(value or "").strip().strip("'").strip('"') or default
    return os.getenv(key, default).strip()


def _normalize_check_interval(value) -> int:
    """读库与迁移用的宽松规整：历史数据里的非法值退回默认，不能因为一条旧数据让启动失败。"""
    try:
        parsed = int(value)
        return parsed if parsed > 0 else 300
    except Exception:
        return 300


def _validate_check_interval(value, default: int | None = None) -> int:
    """用户写入用的严格校验：超出范围直接报错，不静默改成别的值。
    None / 空串按 default 处理（新建时是默认间隔，修改时是原值）。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        if default is None:
            raise ValueError("请填写查询间隔。")
        return int(default)
    try:
        parsed = int(str(value).strip()) if not isinstance(value, bool) else None
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        raise ValueError("查询间隔必须是整数秒。")
    if parsed < MIN_CHECK_INTERVAL or parsed > MAX_CHECK_INTERVAL:
        raise ValueError(f"查询间隔需在 {MIN_CHECK_INTERVAL}–{MAX_CHECK_INTERVAL} 秒之间。")
    return parsed


def _normalize_bool(value, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return 1 if value else 0
    return 1 if str(value).strip().lower() in {"1", "true", "yes", "on"} else 0


def _normalize_role(value: str) -> str:
    return "admin" if str(value).strip().lower() == "admin" else "user"


def _normalize_username(value: str) -> str:
    username = str(value or "").strip().lower()
    if not username:
        raise ValueError("用户名不能为空。")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,31}", username):
        raise ValueError("用户名只能包含小写字母、数字、点、下划线和短横线，长度 3-32。")
    return username


def _normalize_display_name(value: str, fallback: str = "") -> str:
    display_name = str(value or "").strip()
    return display_name or fallback or "未命名用户"


def _normalize_tracking_number(value: str) -> str:
    tracking_number = re.sub(r"\s+", "", str(value or "").strip())
    return tracking_number.upper()


def _mask_secret(value: str) -> str:
    value = (value or "").strip()
    if len(value) <= 8:
        return value
    return f"{value[:4]}...{value[-4:]}"


def _extract_bark_key(raw_value: str) -> str:
    token = str(raw_value or "").strip()
    if not token:
        return ""
    if "://" not in token:
        return token

    parsed = urlparse(token)
    parts = [part for part in parsed.path.split("/") if part]
    return parts[0].strip() if parts else ""


def normalize_bark_keys(value) -> str:
    if isinstance(value, (list, tuple, set)):
        raw_items = [str(item or "").strip() for item in value]
    else:
        raw_items = re.split(r"[\n,;]+", str(value or ""))

    keys = []
    seen = set()
    for item in raw_items:
        key = _extract_bark_key(item)
        if not key or key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return "\n".join(keys)


def parse_bark_keys(value) -> list[str]:
    normalized = normalize_bark_keys(value)
    return [item for item in normalized.splitlines() if item.strip()]


def _mask_keys(value: str) -> str:
    keys = parse_bark_keys(value)
    if not keys:
        return ""
    return ", ".join(_mask_secret(key) for key in keys)


def build_tracking_url(tracking_number: str) -> str:
    return (
        "https://trackings.post.japanpost.jp/services/srv/search/direct"
        f"?reqCodeNo1={tracking_number}&searchKind=S002&locale=ja"
    )


# --- 行 → dict ---

def _row_to_account(row, *, include_secret: bool = False):
    if row is None:
        return None
    account = dict(row)
    account["name"] = account.get("display_name", "")
    account["bark_url_enabled"] = bool(account.get("bark_url_enabled"))
    account["login_enabled"] = bool(account.get("login_enabled"))
    account["is_admin"] = account.get("role") == "admin"
    account["bark_keys"] = normalize_bark_keys(account.get("bark_keys", ""))
    account["bark_keys_masked"] = _mask_keys(account["bark_keys"])
    account["bark_device_count"] = len(parse_bark_keys(account["bark_keys"]))
    account["has_password"] = bool(account.get("password_hash"))
    if not include_secret:
        account.pop("password_hash", None)
    return account


def _row_to_task(row):
    if row is None:
        return None
    task = dict(row)
    task["check_interval"] = _normalize_check_interval(task.get("check_interval"))
    task["enabled"] = bool(task.get("enabled"))
    task["archived"] = bool(task.get("archived"))
    task["atypical"] = bool(task.get("atypical"))
    task["seen_count"] = int(task.get("seen_count") or 0)
    task["tracking_url"] = build_tracking_url(task.get("tracking_number", ""))
    return task


def load_system_env(dotenv_path: str, keys) -> dict:
    env_values = dotenv_values(dotenv_path)
    return {key: _load_env_value(env_values, key, "") for key in keys}


# --- 建表 ---

def _ensure_accounts_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL DEFAULT '',
            password_hash TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT 'user',
            note TEXT NOT NULL DEFAULT '',
            bark_keys TEXT NOT NULL DEFAULT '',
            bark_query_params TEXT NOT NULL DEFAULT '?sound=minuet&level=timeSensitive',
            bark_url_enabled INTEGER NOT NULL DEFAULT 1,
            login_enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def _ensure_tasks_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tracking_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            tracking_number TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            check_interval INTEGER NOT NULL DEFAULT 300,
            enabled INTEGER NOT NULL DEFAULT 1,
            archived INTEGER NOT NULL DEFAULT 0,
            last_tracking_info TEXT NOT NULL DEFAULT '',
            last_checked_at TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            last_push_at TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL,
            seen_count INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(account_id, tracking_number),
            FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_active ON tracking_tasks(enabled, archived)"
    )


def _migrate_portal_sub_column(conn):
    """portal SSO 映射列：accounts.portal_sub 记录该账号对应的 portal 账号固定 ID（X-Portal-Sub）。
    portal 用户名（X-User）用户自己可以改，所以不按用户名映射。
    可空 + 唯一索引（SQLite 的 UNIQUE 允许多个 NULL）；ADD COLUMN 不能直接带 UNIQUE，
    所以唯一性由单独的索引保证。幂等，只加列不动已有数据，账号 id 保持不变。"""
    if not _has_column(conn, "accounts", "portal_sub"):
        conn.execute("ALTER TABLE accounts ADD COLUMN portal_sub TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_portal_sub ON accounts(portal_sub)"
    )


def _ensure_events_schema(conn):
    """任务时间线：状态变化、推送结果、查询出错/恢复。随任务删除级联删除。
    tracking_number 记下事件发生时的单号——任务改过单号后，旧单号的事件仍留在这条任务下，
    也用它防止「抓旧单号的结果写到新单号下」。

    ⚠️ 本表的外键指向 tracking_tasks：以后若要重建 tracking_tasks（RENAME 再 DROP），
    同样会踩 data-model.md 里那个外键重定向 + 级联删光的坑，必须先把事件读进内存再重建。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            account_id INTEGER NOT NULL,
            tracking_number TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tracking_tasks(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_events_task_time ON task_events(task_id, created_at)"
    )
    # 按时间整体清理用
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_events_time ON task_events(created_at)"
    )


# --- 到达预测：渠道、承运方履历、发货前节点 ---

# carrier_records.source 与 pre_milestones 的 stage / source 都是穷举，CHECK 约束由它们派生
_CARRIER_SOURCE_SQL = ", ".join(f"'{source}'" for source in forecast.CARRIER_SOURCES)
_PRE_STAGE_SQL = ", ".join(f"'{stage}'" for stage in forecast.PRE_STAGES)
# 发货前节点的来源：caller 外部调用方写入 / manual 用户手填（manual 优先，见 _write_milestones）
MILESTONE_SOURCES = ("caller", "manual")
_MILESTONE_SOURCE_SQL = ", ".join(f"'{source}'" for source in MILESTONE_SOURCES)


_CARRIER_RECORDS_COLUMNS = "id, task_id, tracking_number, source, record_at, status, detail, created_at"
_CARRIER_RECORDS_TABLE_SQL = f"""
    CREATE TABLE {{name}} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL,
        tracking_number TEXT NOT NULL,
        source TEXT NOT NULL CHECK(source IN ({_CARRIER_SOURCE_SQL})),
        record_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        UNIQUE(task_id, tracking_number, source, record_at, status, detail),
        FOREIGN KEY(task_id) REFERENCES tracking_tasks(id) ON DELETE CASCADE
    )
"""


def _migrate_carrier_sources(conn):
    """已有库的 carrier_records 建表语句里 source 的 CHECK 是旧的来源集合（SQLite 不能 ALTER 约束），
    新增来源（如 cn_ems）后按「新表 → 复制 → 删旧表 → 改名」只重建 carrier_records 本身，幂等（集合一致就跳过）。

    为什么不会踩 data-model.md 的外键坑：那个坑是「改名被引用的父表」时子表外键被重定向，随后 DROP 级联删光子表。
    carrier_records 是子表（外键指向 tracking_tasks），没有任何表引用它：DROP 旧表只删它自己的行，
    新表改名时也没有别的外键需要重定向；新表自带的外键原样指向 tracking_tasks。tracking_tasks 不动。
    调用方（ensure_storage）在 PRAGMA foreign_keys=OFF 下执行，复制时也不会逐行做外键检查。
    索引随旧表一起删除，随后由 _ensure_forecast_schema 的 CREATE INDEX IF NOT EXISTS 补回。"""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'carrier_records'").fetchone()
    if row is None or f"IN ({_CARRIER_SOURCE_SQL})" in _squash_sql(row[0]):
        return
    conn.execute("DROP TABLE IF EXISTS carrier_records_new")
    conn.execute(_CARRIER_RECORDS_TABLE_SQL.format(name="carrier_records_new"))
    conn.execute(
        f"INSERT INTO carrier_records_new ({_CARRIER_RECORDS_COLUMNS}) "
        f"SELECT {_CARRIER_RECORDS_COLUMNS} FROM carrier_records"
    )
    conn.execute("DROP TABLE carrier_records")
    conn.execute("ALTER TABLE carrier_records_new RENAME TO carrier_records")


def _ensure_track17_schema(conn):
    """track17_numbers：每个单号在 17TRACK 的登记状态（按单号，不按任务：同单号多个任务只登记一次）。
    17TRACK 按登记的单号扣额度，有这一行（registered_at 非空）就不再登记。
    - registered_at：登记成功（或 17TRACK 回「已登记」）的时间；登记失败时为 NULL、last_error 记原因，下轮再试；
    - last_synced_at：最近一次查到中国邮政事件的时间；
    - last_attempt_at：最近一次请求（登记或查询）的时间，补抓归档单号时按它轮转、限频；
    - last_error：最近一次失败原因，成功后清空。
    不设外键：单号与任务的增删无关，删了任务也不该再为同一单号扣一次额度。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS track17_numbers (
            tracking_number TEXT PRIMARY KEY,
            registered_at TEXT,
            last_synced_at TEXT,
            last_attempt_at TEXT,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )


def _ensure_forecast_schema(conn):
    """到达预测用的一列三表：只加列、只新建；唯一的重建是 carrier_records 来源集合变了时只重建它自己
    （_migrate_carrier_sources，它是子表，不踩 data-model.md 的外键坑），tracking_tasks 永不重建。

    - tracking_tasks.channel：渠道名，空串 = 未标注（ADD COLUMN，幂等）。
    - tracking_tasks.atypical：非常规件（0/1），作为预测样本时降权（ADD COLUMN，幂等）。
    - carrier_records：承运方履历，长期保留、不清理（task_events 有 180 天 / 500 条的清理，不能当经验库）。
      存原始状态，阶段在读取时由 forecast.record_stage 计算，以后改映射规则对历史数据自动生效。
    - pre_milestones：发货前节点（下单 / 打包 / 出库），每个任务每个阶段一行。
    - track17_numbers：17TRACK 登记状态（见 _ensure_track17_schema）。
    前两张表都随任务删除级联删除；以后若要重建 tracking_tasks，同 task_events 一样要先把数据读进内存。

    carrier_records 为空而 task_events 里有 status_change 时，把它们解析回填一次（INSERT OR IGNORE，重复执行无害）。"""
    if not _has_column(conn, "tracking_tasks", "channel"):
        conn.execute("ALTER TABLE tracking_tasks ADD COLUMN channel TEXT NOT NULL DEFAULT ''")
    # 非常规件：作为预测样本时降权（forecast.ATYPICAL_WEIGHT）。ADD COLUMN 幂等，不重建 tracking_tasks
    if not _has_column(conn, "tracking_tasks", "atypical"):
        conn.execute("ALTER TABLE tracking_tasks ADD COLUMN atypical INTEGER NOT NULL DEFAULT 0")
    _migrate_carrier_sources(conn)
    if not _has_table(conn, "carrier_records"):
        conn.execute(_CARRIER_RECORDS_TABLE_SQL.format(name="carrier_records"))
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_carrier_records_number ON carrier_records(tracking_number)"
    )
    _ensure_track17_schema(conn)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS pre_milestones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            stage TEXT NOT NULL CHECK(stage IN ({_PRE_STAGE_SQL})),
            at TEXT NOT NULL,
            source TEXT NOT NULL CHECK(source IN ({_MILESTONE_SOURCE_SQL})),
            updated_at TEXT NOT NULL,
            UNIQUE(task_id, stage),
            FOREIGN KEY(task_id) REFERENCES tracking_tasks(id) ON DELETE CASCADE
        )
        """
    )
    if conn.execute("SELECT 1 FROM carrier_records LIMIT 1").fetchone() is None:
        rows = conn.execute(
            """
            SELECT e.task_id, e.tracking_number, e.status, e.detail, e.created_at
            FROM task_events e JOIN tracking_tasks t ON t.id = e.task_id
            WHERE e.kind = 'status_change'
            ORDER BY e.id
            """
        ).fetchall()
        for row in rows:
            _insert_carrier_records(
                conn, row["task_id"], row["tracking_number"], [{"info": row["status"], "detail": row["detail"]}],
                created_at=row["created_at"],
            )


def _insert_carrier_records(conn, task_id: int, tracking_number: str, records, *, source: str = "jp_post",
                            created_at: str | None = None) -> int:
    """把抓到的履历 [{"info", "detail"}] 写进 carrier_records（INSERT OR IGNORE），返回解析成功的条数。
    info 拆成 record_at + status（forecast.parse_record_info）；没有日期前缀的记录放不进时间轴，跳过。"""
    if source not in forecast.CARRIER_SOURCES:
        raise ValueError(f"未定义的承运方来源：{source}")
    stamp = created_at or _ts()
    count = 0
    for record in records or []:
        parsed = forecast.parse_record_info(record.get("info"))
        if parsed is None:
            continue
        record_at, status = parsed
        conn.execute(
            """
            INSERT OR IGNORE INTO carrier_records (task_id, tracking_number, source, record_at, status, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (int(task_id), str(tracking_number or ""), source, record_at, status, str(record.get("detail") or ""), stamp),
        )
        count += 1
    return count


def _insert_parsed_records(conn, task_id: int, tracking_number: str, source: str, rows) -> int:
    """写已拆好的记录 [{"record_at", "status", "detail"}]（cn_ems 用，record_at 已是日本时间），INSERT OR IGNORE。"""
    if source not in forecast.CARRIER_SOURCES:
        raise ValueError(f"未定义的承运方来源：{source}")
    stamp = _ts()
    for row in rows:
        conn.execute(
            """
            INSERT OR IGNORE INTO carrier_records (task_id, tracking_number, source, record_at, status, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (int(task_id), str(tracking_number or ""), source, row["record_at"], str(row["status"] or ""),
             str(row.get("detail") or ""), stamp),
        )
    return len(rows)


def record_cn_records(task_id: int, tracking_number: str, rows) -> int | None:
    """写中国邮政始发国段记录（source='cn_ems'），与 jp_post 同样带抓取时的单号校验：
    单号已被改走（或任务已删除）返回 None，不写。否则返回条数。"""
    with closing(_connect()) as conn:
        with conn:
            row = conn.execute(
                "SELECT tracking_number FROM tracking_tasks WHERE id = ?", (int(task_id),)
            ).fetchone()
            if row is None or row["tracking_number"] != tracking_number:
                return None
            return _insert_parsed_records(conn, task_id, tracking_number, "cn_ems", rows)


# --- 17TRACK 登记状态 ---

# 已归档单号的始发国段补抓：登记后最多补到这么久，每批最多这么多单号（17TRACK 每次查询上限 40）
TRACK17_CATCHUP_DAYS = 3
TRACK17_BATCH = 40
# 认为始发国段已经完整（不再补抓）的事件：中国邮政记到「到达寄达地」
TRACK17_DONE_STATUS = "到达寄达地"


# 单号在 17TRACK 的登记状态穷举（网页显示用，前端 forecast.js 有一份一致的映射）：
# unregistered 未登记（含登记失败前从没成功过、且没有错误记录）/ pending 已登记、还没查到中国邮政事件 /
# synced 已查到并写入事件 / error 最近一次请求出错（登记或查询，原因在 last_error）
TRACK17_STATUS_META = {
    "unregistered": {"label": "未登记", "tone": "neutral"},
    "pending": {"label": "已登记待数据", "tone": "info"},
    "synced": {"label": "已同步", "tone": "ok"},
    "error": {"label": "出错", "tone": "err"},
}


def track17_status(row) -> str:
    """track17_numbers 的一行（或 None）→ TRACK17_STATUS_META 的 key。出错优先于其他状态。"""
    if row is None:
        return "unregistered"
    if str(row["last_error"] or "").strip():
        return "error"
    if not row["registered_at"]:
        return "unregistered"
    return "synced" if row["last_synced_at"] else "pending"


def track17_number_view(tracking_number: str) -> dict:
    """网页显示用：{"status", "registered_at", "last_synced_at", "error"}。"""
    row = track17_state(tracking_number)
    return {
        "status": track17_status(row),
        "registered_at": row["registered_at"] if row else None,
        "last_synced_at": row["last_synced_at"] if row else None,
        "error": (str(row["last_error"] or "").strip() or None) if row else None,
    }


def track17_summary() -> dict:
    """管理员 17TRACK 卡片的统计：已登记单号数及其中各状态个数、最近一次同步时间、最近一条错误（没有为 None）。"""
    with closing(_connect()) as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM track17_numbers").fetchall()]
    registered = [row for row in rows if row["registered_at"]]
    counts = {"pending": 0, "synced": 0, "error": 0}
    for row in registered:
        counts[track17_status(row)] += 1
    synced_times = [row["last_synced_at"] for row in rows if row["last_synced_at"]]
    errors = [row for row in rows if str(row["last_error"] or "").strip()]
    latest_error = max(errors, key=lambda row: row["last_attempt_at"] or "") if errors else None
    return {
        "registered": len(registered),
        **counts,
        "last_synced_at": max(synced_times) if synced_times else None,
        "last_error": {
            "tracking_number": latest_error["tracking_number"],
            "error": latest_error["last_error"],
            "at": latest_error["last_attempt_at"],
        } if latest_error else None,
    }


def track17_state(tracking_number: str):
    """单号的 17TRACK 登记状态 dict；没有记录返回 None。"""
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM track17_numbers WHERE tracking_number = ?", (str(tracking_number),)
        ).fetchone()
    return dict(row) if row else None


def track17_update(tracking_number: str, *, registered: bool = False, synced: bool = False, error: str | None = None):
    """记一次 17TRACK 请求的结果：registered 记登记成功（已登记过的不改时间），synced 记查到了事件，
    error 非 None 时记失败原因（空串表示成功、清空原因）。每次都刷新 last_attempt_at。"""
    now = _ts()
    with closing(_connect()) as conn:
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO track17_numbers (tracking_number, created_at) VALUES (?, ?)",
                (str(tracking_number), now),
            )
            conn.execute(
                """
                UPDATE track17_numbers
                SET registered_at = CASE WHEN ? AND registered_at IS NULL THEN ? ELSE registered_at END,
                    last_synced_at = CASE WHEN ? THEN ? ELSE last_synced_at END,
                    last_error = COALESCE(?, last_error),
                    last_attempt_at = ?
                WHERE tracking_number = ?
                """,
                (int(registered), now, int(synced), now, error, now, str(tracking_number)),
            )


def track17_catchup_candidates(now=None, *, min_gap_seconds: int = 1800, limit: int = TRACK17_BATCH) -> list:
    """需要补抓始发国段的已归档单号：已登记、登记不满 TRACK17_CATCHUP_DAYS 天、该单号的任务全部已归档
    （还在追踪的由 tracker 每轮自己查）、从没查到过事件或还没有「到达寄达地」、距上次请求超过 min_gap_seconds。
    返回 [(单号, 任务 id)]，任务取该单号下最新的一条；按上次请求时间轮转，最多 limit 个。"""
    moment = now or datetime.now()
    since = (moment - timedelta(days=TRACK17_CATCHUP_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    gap = (moment - timedelta(seconds=min_gap_seconds)).strftime("%Y-%m-%d %H:%M:%S")
    with closing(_connect()) as conn:
        rows = conn.execute(
            """
            SELECT n.tracking_number, MAX(t.id) AS task_id
            FROM track17_numbers n
            JOIN tracking_tasks t ON t.tracking_number = n.tracking_number
            WHERE n.registered_at IS NOT NULL AND n.registered_at >= ?
              AND (n.last_attempt_at IS NULL OR n.last_attempt_at <= ?)
              AND (n.last_synced_at IS NULL OR NOT EXISTS (
                    SELECT 1 FROM carrier_records c
                    WHERE c.tracking_number = n.tracking_number AND c.source = 'cn_ems' AND c.status = ?))
            GROUP BY n.tracking_number
            HAVING MIN(t.archived) = 1
            ORDER BY COALESCE(n.last_attempt_at, ''), n.tracking_number
            LIMIT ?
            """,
            (since, gap, TRACK17_DONE_STATUS, int(limit)),
        ).fetchall()
    return [(row["tracking_number"], int(row["task_id"])) for row in rows]


def record_carrier_records(task_id: int, tracking_number: str, records) -> int | None:
    """只写履历、不动任务状态（补录用）。与 update_task_state 一样带抓取时的单号校验：
    单号已被改走（或任务已删除）返回 None，不写。否则返回解析成功的条数。"""
    with closing(_connect()) as conn:
        with conn:
            row = conn.execute(
                "SELECT tracking_number FROM tracking_tasks WHERE id = ?", (int(task_id),)
            ).fetchone()
            if row is None or row["tracking_number"] != tracking_number:
                return None
            return _insert_carrier_records(conn, task_id, tracking_number, records)


def _read_milestones(conn, task_ids) -> dict:
    """{task_id: {stage: {"at", "source"}}}"""
    ids = [int(task_id) for task_id in task_ids]
    if not ids:
        return {}
    placeholders = ", ".join("?" for _ in ids)
    result: dict = {task_id: {} for task_id in ids}
    for row in conn.execute(
        f"SELECT task_id, stage, at, source FROM pre_milestones WHERE task_id IN ({placeholders})", ids
    ).fetchall():
        result[int(row["task_id"])][row["stage"]] = {"at": row["at"], "source": row["source"]}
    return result


def normalize_milestones(value, *, allow_tz: bool = False, allow_empty: bool = True) -> dict:
    """发货前节点输入 {stage: 时间字符串} 校验与规整：key 只能是 ordered / packed / handed_over，
    值规整成 'YYYY-MM-DD HH:MM' 或 'YYYY-MM-DD'。allow_empty 时空串 / None 规整为 ''（表示清空）。不合法抛 ValueError。"""
    if not isinstance(value, dict):
        raise ValueError("发货前节点必须是对象。")
    result = {}
    for stage, raw in value.items():
        if stage not in forecast.PRE_STAGES:
            raise ValueError(f"不支持的发货前节点：{stage}（只能是 {', '.join(forecast.PRE_STAGES)}）")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if not allow_empty:
                raise ValueError(f"{stage} 的时间不能为空。")
            result[stage] = ""
            continue
        if not isinstance(raw, str):
            raise ValueError(f"{stage} 的时间必须是字符串。")
        try:
            result[stage] = forecast.normalize_milestone_time(raw, allow_tz=allow_tz)
        except ValueError as exc:
            raise ValueError(f"{forecast.STAGE_META[stage]['label']}：{exc}") from None
    return result


def _write_milestones(conn, task_id: int, milestones: dict, source: str):
    """写发货前节点（milestones 已经过 normalize_milestones）。manual 优先：
    - manual：有值 → 写入（覆盖 caller 行）；值与现有相同则不动（表单原样保存不会把 caller 行变成 manual）；空串 → 删除该行。
    - caller：只写有值的项；已有 manual 行的不覆盖；不能删除。"""
    if source not in MILESTONE_SOURCES:
        raise ValueError(f"未定义的节点来源：{source}")
    now = _ts()
    for stage, at in milestones.items():
        existing = conn.execute(
            "SELECT at, source FROM pre_milestones WHERE task_id = ? AND stage = ?", (int(task_id), stage)
        ).fetchone()
        if not at:
            if source == "manual" and existing is not None:
                conn.execute("DELETE FROM pre_milestones WHERE task_id = ? AND stage = ?", (int(task_id), stage))
            continue
        if existing is not None:
            if existing["at"] == at:
                continue
            if source == "caller" and existing["source"] == "manual":
                continue
        conn.execute(
            """
            INSERT INTO pre_milestones (task_id, stage, at, source, updated_at) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(task_id, stage) DO UPDATE SET at = excluded.at, source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (int(task_id), stage, at, source, now),
        )


def _number_records(conn, tracking_number: str) -> list:
    return [
        dict(row) for row in conn.execute(
            "SELECT source, record_at, status, detail FROM carrier_records WHERE tracking_number = ?",
            (tracking_number,),
        ).fetchall()
    ]


def _forecast_number_stages(conn, tracking_number: str, *, milestone_task_ids, records=None) -> dict:
    """一个单号的阶段时间：该单号下所有任务的承运方履历，加上 milestone_task_ids 这些任务的发货前节点。
    始发国段的时区换算按单号国家码（forecast.record_time）。records 已取过时可直接传入。"""
    if records is None:
        records = _number_records(conn, tracking_number)
    milestone_maps = _read_milestones(conn, milestone_task_ids)
    stage_maps = [forecast.stages_from_records(records, tracking_number=tracking_number)]
    for task_id in milestone_task_ids:
        stage_maps.append(forecast.stages_from_records([], {
            stage: item["at"] for stage, item in milestone_maps.get(int(task_id), {}).items()
        }))
    return forecast.merge_stages(*stage_maps)


def _forecast_samples(conn, exclude_number: str) -> list:
    """历史样本：所有账号所有任务（含归档）按单号去重。同单号多个任务：阶段时间取最早，
    渠道取最近更新的那条非空渠道，任一任务标了非常规件就算非常规。排除当前单号。
    只返回阶段、渠道、收寄网点与非常规标记，不带单号与账号。"""
    tasks_by_number: dict = {}
    for row in conn.execute(
        "SELECT id, tracking_number, channel, atypical FROM tracking_tasks ORDER BY updated_at DESC, id DESC"
    ).fetchall():
        tasks_by_number.setdefault(row["tracking_number"], []).append(row)
    numbers = set(tasks_by_number)
    numbers.update(
        row[0] for row in conn.execute("SELECT DISTINCT tracking_number FROM carrier_records").fetchall()
    )
    numbers.discard(exclude_number)
    samples = []
    for number in sorted(numbers):
        rows = tasks_by_number.get(number, [])
        records = _number_records(conn, number)
        stages = _forecast_number_stages(
            conn, number, milestone_task_ids=[row["id"] for row in rows], records=records
        )
        if not stages:
            continue
        channel = next((row["channel"] for row in rows if str(row["channel"] or "").strip()), "")
        samples.append({
            "stages": stages,
            "channel": channel,
            "office": forecast.collection_office(records),
            # 同单号多个任务：任一任务标了非常规就算
            "atypical": any(int(row["atypical"] or 0) for row in rows),
        })
    return samples


def _office_channels(samples) -> dict:
    """{收寄网点: {渠道…}}：只看标了渠道、提取得出收寄网点的样本。"""
    result: dict = {}
    for sample in samples:
        channel = str(sample.get("channel") or "").strip()
        if channel and sample.get("office"):
            result.setdefault(sample["office"], set()).add(channel)
    return result


def task_forecast(task_id: int, now=None, transit_region=None):
    """任务的到达预测（结构见 forecast.build_forecast，另加 suggested_channel / office_multi_channel）；
    transit_region 不传时读系统设置 TRANSIT_REGION_LABEL（env_schema.transit_region_label）；
    任务不存在返回 None。
    当前单的阶段：本单号在所有任务下的承运方履历（日本邮政与中国邮政的公开数据）+ 本任务自己的发货前节点
    （别的账号同单号任务的节点不并进来，免得把别人的下单时间显示给这个人）。
    渠道推断只在本任务渠道为空时给出，不写库：收寄网点在其他已标渠道的单号里只对应过一个渠道才建议。"""
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM tracking_tasks WHERE id = ?", (int(task_id),)).fetchone()
        if row is None:
            return None
        number = row["tracking_number"]
        records = _number_records(conn, number)
        current = _forecast_number_stages(conn, number, milestone_task_ids=[int(task_id)], records=records)
        samples = _forecast_samples(conn, number)
    office = forecast.collection_office(records)
    if transit_region is None:
        transit_region = env_schema.transit_region_label()
    result = forecast.build_forecast(
        current, row["channel"], samples, now or datetime.now(),
        current_office=office, transit_region=transit_region,
    )
    if str(row["channel"] or "").strip():
        result.update({"collection_office": office, "suggested_channel": None, "office_multi_channel": False})
    else:
        result.update(forecast.suggest_channel(office, _office_channels(samples)))
    return result


def task_cn_records(task_id: int) -> list:
    """任务当前单号的中国邮政始发国段记录（所有任务下去重），按时间倒序：[{"record_at", "status", "detail", "segment"}]。
    record_at 已是日本时间；segment 按本单号的阶段边界切分（forecast.segment_at）。任务不存在返回空列表。"""
    with closing(_connect()) as conn:
        row = conn.execute("SELECT tracking_number FROM tracking_tasks WHERE id = ?", (int(task_id),)).fetchone()
        if row is None:
            return []
        number = row["tracking_number"]
        rows = conn.execute(
            """
            SELECT record_at, status, detail FROM carrier_records
            WHERE tracking_number = ? AND source = 'cn_ems'
            GROUP BY record_at, status, detail
            ORDER BY record_at DESC, MAX(id) DESC
            """,
            (number,),
        ).fetchall()
        stages = forecast.stages_from_records(_number_records(conn, number), tracking_number=number)
    result = []
    for item in rows:
        record = dict(item)
        record["segment"] = forecast.segment_at(stages, forecast.parse_stored_time(record["record_at"]))
        result.append(record)
    return result


# --- v1 → v2 迁移 ---

def _migrate_v1_to_v2(conn):
    """把 accounts 里的单任务字段和 tracking_history 拆进 tracking_tasks，
    再重建 accounts 去掉那些列。幂等：以 accounts 是否还有 tracking_number 列为判据。

    ⚠️ 步骤顺序不能随意调换，也必须在 foreign_keys=OFF 下执行：
    `ALTER TABLE accounts RENAME` 会把子表已有的 FK 引用一起重定向到改名后的表，
    之后 DROP 旧表就会顺着 ON DELETE CASCADE 把子表数据全部删掉。
    所以这里先搬完身份数据、重建好 accounts，最后才创建 tracking_tasks 并写入。"""
    if not _has_column(conn, "accounts", "tracking_number"):
        return False

    now = _ts()

    # 1. 先把要搬的数据读进内存，后面表结构会变
    account_rows = [dict(row) for row in conn.execute("SELECT * FROM accounts").fetchall()]
    history_rows = []
    if _has_table(conn, "tracking_history"):
        history_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT account_id, tracking_number, first_seen_at, last_seen_at, seen_count FROM tracking_history"
            ).fetchall()
        ]

    # 2. 重建 accounts（此时若已存在空的 tracking_tasks，一并丢弃后面重建，
    #    避免 RENAME 把它的 FK 指到旧表上）
    conn.execute("DROP TABLE IF EXISTS tracking_tasks")
    conn.execute("ALTER TABLE accounts RENAME TO accounts_v1")
    _ensure_accounts_schema(conn)
    conn.execute(
        """
        INSERT INTO accounts (
            id, username, display_name, password_hash, role, note,
            bark_keys, bark_query_params, bark_url_enabled, login_enabled,
            created_at, updated_at
        )
        SELECT id, username, display_name, password_hash, role, note,
               bark_keys, bark_query_params, bark_url_enabled, login_enabled,
               created_at, updated_at
        FROM accounts_v1
        """
    )
    conn.execute("DROP TABLE accounts_v1")
    if _has_table(conn, "tracking_history"):
        conn.execute("DROP TABLE tracking_history")

    # 3. accounts 定型后再建任务表，FK 才指向新表
    _ensure_tasks_schema(conn)

    # 4. 每个账号的当前单号 → 一条活跃任务（继承原有轮询状态）
    for row in account_rows:
        number = _normalize_tracking_number(row.get("tracking_number"))
        if not number:
            continue
        created = str(row.get("created_at") or now)
        conn.execute(
            """
            INSERT OR IGNORE INTO tracking_tasks (
                account_id, tracking_number, label, check_interval, enabled, archived,
                last_tracking_info, last_checked_at, last_error, last_push_at,
                first_seen_at, seen_count, created_at, updated_at
            ) VALUES (?, ?, '', ?, ?, 0, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                int(row["id"]),
                number,
                _normalize_check_interval(row.get("check_interval")),
                _normalize_bool(row.get("tracking_enabled"), default=1),
                str(row.get("last_tracking_info") or ""),
                str(row.get("last_checked_at") or ""),
                str(row.get("last_error") or ""),
                str(row.get("last_push_at") or ""),
                created,
                created,
                now,
            ),
        )

    # 5. tracking_history 里的其余单号 → 归档任务（保留档案时间与次数）
    for row in history_rows:
        number = _normalize_tracking_number(row.get("tracking_number"))
        if not number:
            continue
        first_seen = str(row.get("first_seen_at") or now)
        seen_count = int(row.get("seen_count") or 1)
        existing = conn.execute(
            "SELECT id FROM tracking_tasks WHERE account_id = ? AND tracking_number = ?",
            (int(row["account_id"]), number),
        ).fetchone()
        if existing:
            # 就是当前单号：把档案里的首次时间与计数补回去
            conn.execute(
                "UPDATE tracking_tasks SET first_seen_at = ?, seen_count = ? WHERE id = ?",
                (first_seen, seen_count, existing["id"]),
            )
            continue
        conn.execute(
            """
            INSERT INTO tracking_tasks (
                account_id, tracking_number, label, check_interval, enabled, archived,
                first_seen_at, seen_count, created_at, updated_at
            ) VALUES (?, ?, '', 300, 0, 1, ?, ?, ?, ?)
            """,
            (
                int(row["account_id"]),
                number,
                first_seen,
                seen_count,
                first_seen,
                str(row.get("last_seen_at") or now),
            ),
        )
    return True


def _migrate_legacy_profiles(conn, dotenv_path: str):
    """全新库：从更早的 user_profiles 表或 .env 引导出第一个账号（含首任务）。"""
    now = _ts()
    if _has_table(conn, "user_profiles"):
        for row in conn.execute("SELECT * FROM user_profiles ORDER BY id").fetchall():
            note = str(row["note"] or "").strip()
            legacy_note = "从旧版 user_profiles 迁移"
            account_id = _insert_account(
                conn,
                {
                    "username": f"legacy-user-{row['id']}",
                    "display_name": str(row["name"] or f"迁移用户 {row['id']}"),
                    "password_hash": "",
                    "role": "user",
                    "note": f"{legacy_note}；{note}" if note else legacy_note,
                    "bark_keys": str(row["bark_key"] or ""),
                    "bark_query_params": str(row["bark_query_params"] or PROFILE_DEFAULTS["bark_query_params"]),
                    "bark_url_enabled": row["bark_url_enabled"],
                    "login_enabled": 0,
                    "created_at": str(row["created_at"] or now),
                    "updated_at": str(row["updated_at"] or now),
                },
            )
            number = _normalize_tracking_number(row["tracking_number"])
            if account_id and number:
                _insert_task(conn, int(account_id), {
                    "tracking_number": number,
                    "check_interval": row["check_interval"],
                    "enabled": 1,
                })
        return

    env_values = dotenv_values(dotenv_path)
    account_id = _insert_account(
        conn,
        {
            "username": "legacy-default",
            "display_name": "默认用户",
            "password_hash": "",
            "role": "user",
            "note": "从 .env 初始化",
            "bark_keys": _load_env_value(env_values, "BARK_KEY", ""),
            "bark_query_params": _load_env_value(
                env_values, "BARK_QUERY_PARAMS", PROFILE_DEFAULTS["bark_query_params"]
            ),
            "bark_url_enabled": _normalize_bool(_load_env_value(env_values, "BARK_URL_ENABLED", "1"), default=1),
            "login_enabled": 0,
            "created_at": now,
            "updated_at": now,
        },
    )
    number = _normalize_tracking_number(_load_env_value(env_values, "TRACKING_NUMBER", ""))
    if account_id and number:
        _insert_task(conn, int(account_id), {
            "tracking_number": number,
            "check_interval": _normalize_check_interval(_load_env_value(env_values, "CHECK_INTERVAL", "300")),
            "enabled": 1,
        })


def _sync_admin_account(conn, admin_username: str, admin_password_hash: str):
    if not admin_password_hash.strip():
        return

    username = _normalize_username(admin_username or "admin")
    row = conn.execute("SELECT id FROM accounts WHERE username = ?", (username,)).fetchone()
    now = _ts()
    if row is None:
        _insert_account(
            conn,
            {
                "username": username,
                "display_name": "管理员",
                "password_hash": admin_password_hash.strip(),
                "role": "admin",
                "note": "从 .env 管理员配置引导",
                "bark_keys": "",
                "bark_query_params": PROFILE_DEFAULTS["bark_query_params"],
                "bark_url_enabled": 1,
                "login_enabled": 1,
                "created_at": now,
                "updated_at": now,
            },
        )
        return

    conn.execute(
        """
        UPDATE accounts
        SET password_hash = ?, role = 'admin', login_enabled = 1, updated_at = ?
        WHERE username = ?
        """,
        (admin_password_hash.strip(), now, username),
    )


def ensure_storage(dotenv_path: str):
    admin_username = os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"
    admin_password_hash = os.getenv("ADMIN_PASSWORD_HASH", "").strip()

    with closing(_connect()) as conn:
        # 表结构变更期间关掉外键约束：见 _migrate_v1_to_v2 的说明。
        # PRAGMA 必须在事务外执行才生效，所以放在 with conn 之前。
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            with conn:
                _ensure_accounts_schema(conn)
                _migrate_v1_to_v2(conn)
                _ensure_tasks_schema(conn)
                # 事件表必须排在 v1→v2 迁移之后：迁移会重建任务表，事件表的外键要指向定型后的表
                _ensure_events_schema(conn)
                _migrate_portal_sub_column(conn)
                # 机器接口的两张表与墓碑触发器：只新建、不改动已有表
                _ensure_api_schema(conn)
                # 到达预测：渠道列、承运方履历、发货前节点（只加列、只新建）
                _ensure_forecast_schema(conn)
                if conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0:
                    _migrate_legacy_profiles(conn, dotenv_path)
                _sync_admin_account(conn, admin_username, admin_password_hash)
        finally:
            conn.execute("PRAGMA foreign_keys=ON")


# --- 插入 ---

def _insert_account(conn, data: dict):
    now = _ts()
    cursor = conn.execute(
        """
        INSERT INTO accounts (
            username, display_name, password_hash, role, note,
            bark_keys, bark_query_params, bark_url_enabled, login_enabled,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data["username"],
            data["display_name"],
            data.get("password_hash", ""),
            data.get("role", "user"),
            str(data.get("note", "") or ""),
            normalize_bark_keys(data.get("bark_keys", "")),
            str(data.get("bark_query_params", PROFILE_DEFAULTS["bark_query_params"])).strip()
            or PROFILE_DEFAULTS["bark_query_params"],
            _normalize_bool(data.get("bark_url_enabled", 1), default=1),
            _normalize_bool(data.get("login_enabled", 1), default=1),
            data.get("created_at", now),
            data.get("updated_at", now),
        ),
    )
    return cursor.lastrowid


def _insert_task(conn, account_id: int, data: dict):
    """插入任务；同账号同单号已归档则复活它（archived=0，seen_count+1）。
    同单号正在追踪（未归档）时拒绝：以前会静默覆盖备注和间隔，用户却看到"已添加"。"""
    number = _normalize_tracking_number(data.get("tracking_number", ""))
    if not number:
        raise ValueError("请填写日本邮政单号。")

    now = _ts()
    existing = conn.execute(
        "SELECT id, seen_count, archived FROM tracking_tasks WHERE account_id = ? AND tracking_number = ?",
        (account_id, number),
    ).fetchone()
    if existing and not existing["archived"]:
        raise ValueError("该单号已在追踪中，如需修改备注或间隔请直接编辑那条任务。")
    if existing:
        conn.execute(
            """
            UPDATE tracking_tasks
            SET archived = 0, enabled = ?, label = ?, check_interval = ?,
                seen_count = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                _normalize_bool(data.get("enabled", 1), default=1),
                str(data.get("label", "") or ""),
                _normalize_check_interval(data.get("check_interval", PROFILE_DEFAULTS["check_interval"])),
                int(existing["seen_count"] or 0) + 1,
                now,
                existing["id"],
            ),
        )
        return existing["id"]

    cursor = conn.execute(
        """
        INSERT INTO tracking_tasks (
            account_id, tracking_number, label, check_interval, enabled, archived,
            first_seen_at, seen_count, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 0, ?, 1, ?, ?)
        """,
        (
            account_id,
            number,
            str(data.get("label", "") or ""),
            _normalize_check_interval(data.get("check_interval", PROFILE_DEFAULTS["check_interval"])),
            _normalize_bool(data.get("enabled", 1), default=1),
            now,
            now,
            now,
        ),
    )
    return cursor.lastrowid


# --- 查询：账号 ---

def _attach_tasks(conn, accounts: list[dict]):
    if not accounts:
        return accounts

    account_ids = [int(account["id"]) for account in accounts]
    placeholders = ", ".join("?" for _ in account_ids)
    rows = conn.execute(
        f"""
        SELECT * FROM tracking_tasks
        WHERE account_id IN ({placeholders})
        ORDER BY archived, enabled DESC, updated_at DESC, id DESC
        """,
        account_ids,
    ).fetchall()

    milestones = _read_milestones(conn, [row["id"] for row in rows])
    grouped: dict[int, list] = {account_id: [] for account_id in account_ids}
    for row in rows:
        task = _row_to_task(row)
        task["milestones"] = milestones.get(int(row["id"]), {})
        grouped.setdefault(int(row["account_id"]), []).append(task)

    for account in accounts:
        tasks = grouped.get(int(account["id"]), [])
        account["tasks"] = [task for task in tasks if not task["archived"]]
        account["archived_tasks"] = [task for task in tasks if task["archived"]]
        account["task_count"] = len(tasks)
        account["active_task_count"] = len([task for task in account["tasks"] if task["enabled"]])
        # 渠道输入框的 <datalist>：这个账号用过的渠道
        account["channels"] = sorted({task["channel"] for task in tasks if task.get("channel")})
    return accounts


def list_accounts(include_disabled: bool = True):
    query = "SELECT * FROM accounts"
    params: list = []
    if not include_disabled:
        query += " WHERE login_enabled = 1"
    query += " ORDER BY CASE role WHEN 'admin' THEN 0 ELSE 1 END, id"
    with closing(_connect()) as conn:
        rows = conn.execute(query, params).fetchall()
        accounts = [_row_to_account(row) for row in rows]
        return _attach_tasks(conn, accounts)


def get_account(account_id: int):
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        account = _row_to_account(row)
        if not account:
            return None
        return _attach_tasks(conn, [account])[0]


def get_account_by_username(username: str, *, include_secret: bool = False):
    if not str(username or "").strip():
        return None
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM accounts WHERE username = ?",
            (str(username).strip().lower(),),
        ).fetchone()
        account = _row_to_account(row, include_secret=include_secret)
        if not account:
            return None
        return _attach_tasks(conn, [account])[0]


def verify_account_password(account, password: str) -> bool:
    if not account or not account.get("password_hash") or not account.get("login_enabled"):
        return False
    try:
        return check_password_hash(account["password_hash"], password)
    except Exception:
        return False


# --- 查询：任务 ---

def list_tasks(account_id: int | None = None, *, include_archived: bool = False):
    query = "SELECT * FROM tracking_tasks"
    conditions = []
    params: list = []
    if account_id is not None:
        conditions.append("account_id = ?")
        params.append(int(account_id))
    if not include_archived:
        conditions.append("archived = 0")
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY archived, enabled DESC, updated_at DESC, id DESC"
    with closing(_connect()) as conn:
        return [_row_to_task(row) for row in conn.execute(query, params).fetchall()]


def list_due_tasks():
    """tracker 用：所有该轮询的任务，每条带上归属账号的 Bark 配置。"""
    with closing(_connect()) as conn:
        rows = conn.execute(
            """
            SELECT t.*,
                   a.username AS account_username,
                   a.display_name AS account_display_name,
                   a.bark_keys AS account_bark_keys,
                   a.bark_query_params AS account_bark_query_params,
                   a.bark_url_enabled AS account_bark_url_enabled
            FROM tracking_tasks t
            JOIN accounts a ON a.id = t.account_id
            WHERE t.enabled = 1 AND t.archived = 0
            ORDER BY t.id
            """
        ).fetchall()

    tasks = []
    for row in rows:
        task = _row_to_task(row)
        task["account"] = {
            "id": task["account_id"],
            "username": row["account_username"],
            "display_name": row["account_display_name"],
            "bark_keys": normalize_bark_keys(row["account_bark_keys"]),
            "bark_query_params": row["account_bark_query_params"],
            "bark_url_enabled": bool(row["account_bark_url_enabled"]),
        }
        for key in (
            "account_username",
            "account_display_name",
            "account_bark_keys",
            "account_bark_query_params",
            "account_bark_url_enabled",
        ):
            task.pop(key, None)
        tasks.append(task)
    return tasks


def get_task(task_id: int):
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM tracking_tasks WHERE id = ?", (int(task_id),)).fetchone()
        return _row_to_task(row)


# --- 写入：账号 ---

def _create_account(conn, data: dict, *, self_register: bool = False):
    username = _normalize_username(data.get("username", ""))
    display_name = _normalize_display_name(data.get("display_name") or data.get("name"), fallback=username)
    role = "user" if self_register else _normalize_role(data.get("role", "user"))
    password = str(data.get("password", "") or data.get("new_password", "")).strip()
    login_enabled = 1 if self_register else _normalize_bool(data.get("login_enabled", 1), default=1)

    if login_enabled and not password:
        raise ValueError("请提供登录密码。")
    password_hash = generate_password_hash(password) if password else ""

    try:
        account_id = _insert_account(
            conn,
            {
                "username": username,
                "display_name": display_name,
                "password_hash": password_hash,
                "role": role,
                "note": str(data.get("note", "")).strip(),
                "bark_keys": data.get("bark_keys", ""),
                "bark_query_params": data.get("bark_query_params", PROFILE_DEFAULTS["bark_query_params"]),
                "bark_url_enabled": data.get("bark_url_enabled", 1),
                "login_enabled": login_enabled,
            },
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError("用户名已存在。") from exc

    # 建号时顺带给了单号，就一并建首个任务
    number = _normalize_tracking_number(data.get("tracking_number", ""))
    if account_id and number:
        _insert_task(conn, int(account_id), {
            "tracking_number": number,
            "check_interval": _validate_check_interval(
                data.get("check_interval"), default=PROFILE_DEFAULTS["check_interval"]
            ),
            "label": data.get("label", ""),
            "enabled": data.get("tracking_enabled", 1),
        })
    return account_id


def create_account(data: dict):
    with closing(_connect()) as conn:
        with conn:
            account_id = _create_account(conn, data, self_register=False)
    return get_account(account_id)


def register_user(data: dict):
    with closing(_connect()) as conn:
        with conn:
            account_id = _create_account(conn, data, self_register=True)
    return get_account(account_id)


def update_account(account_id: int, data: dict, *, actor_role: str = "admin", actor_id: int | None = None):
    """只更新身份与 Bark 字段；任务字段走 create_task / update_task。"""
    with closing(_connect()) as conn:
        with conn:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
            if row is None:
                raise ValueError("用户不存在。")

            current = dict(row)
            is_self = actor_id == account_id
            if actor_role != "admin" and not is_self:
                raise ValueError("无权修改其他用户。")
            if current["role"] == "admin" and actor_role != "admin":
                raise ValueError("普通用户不能修改管理员账号。")

            next_username = current["username"]
            next_role = current["role"]
            next_login_enabled = current["login_enabled"]
            if actor_role == "admin":
                if "username" in data:
                    next_username = _normalize_username(data["username"])
                next_role = _normalize_role(data.get("role", current["role"]))
                next_login_enabled = _normalize_bool(
                    data.get("login_enabled", current["login_enabled"]), default=current["login_enabled"]
                )

            display_name = _normalize_display_name(
                data.get("display_name") or data.get("name"),
                fallback=current["display_name"],
            )
            password = str(data.get("password", "") or data.get("new_password", "")).strip()
            password_hash = generate_password_hash(password) if password else current["password_hash"]

            # 按键是否存在判断，不能用 or 回落：提交空串表示清空，回落旧值会让 Bark Keys 永远清不掉
            if "bark_keys" in data:
                bark_keys = data["bark_keys"]
            else:
                bark_keys = current["bark_keys"]

            if next_login_enabled and not password_hash:
                raise ValueError("已启用登录的用户必须设置密码。")

            try:
                conn.execute(
                    """
                    UPDATE accounts
                    SET username = ?, display_name = ?, password_hash = ?, role = ?, note = ?,
                        bark_keys = ?, bark_query_params = ?, bark_url_enabled = ?,
                        login_enabled = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        next_username,
                        display_name,
                        password_hash,
                        next_role,
                        str(data.get("note", current["note"]) or "").strip(),
                        normalize_bark_keys(bark_keys),
                        str(data.get("bark_query_params", current["bark_query_params"])).strip()
                        or PROFILE_DEFAULTS["bark_query_params"],
                        _normalize_bool(
                            data.get("bark_url_enabled", current["bark_url_enabled"]),
                            default=current["bark_url_enabled"],
                        ),
                        next_login_enabled,
                        _ts(),
                        account_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("用户名已存在。") from exc
    return get_account(account_id)


# --- portal SSO 映射 ---

def _normalize_portal_sub(value) -> str | None:
    """portal 账号 ID（X-Portal-Sub，uuid 字符串）原样保存，只去首尾空白；空串视为清除映射。"""
    portal_sub = str(value or "").strip()
    if not portal_sub:
        return None
    if len(portal_sub) > 64 or any(ch.isspace() for ch in portal_sub):
        raise ValueError("portal ID 格式不正确。")
    return portal_sub


def set_account_portal_sub(account_id: int, portal_sub):
    """设置/清除某账号的 portal 映射（portal 账号 ID）。portal_sub 为空则清除。
    同一个 portal ID 已映射到别的账号时拒绝（不偷偷挪映射）。"""
    portal_sub = _normalize_portal_sub(portal_sub)
    with closing(_connect()) as conn:
        with conn:
            row = conn.execute("SELECT id FROM accounts WHERE id = ?", (int(account_id),)).fetchone()
            if row is None:
                raise ValueError("用户不存在。")
            if portal_sub is not None:
                other = conn.execute(
                    "SELECT id, username FROM accounts WHERE portal_sub = ? AND id != ?",
                    (portal_sub, int(account_id)),
                ).fetchone()
                if other is not None:
                    raise ValueError(f"portal ID {portal_sub} 已映射到账号 {other['username']}（id {other['id']}）。")
            conn.execute(
                "UPDATE accounts SET portal_sub = ?, updated_at = ? WHERE id = ?",
                (portal_sub, _ts(), int(account_id)),
            )
    return get_account(int(account_id))


def _derive_local_username(conn, portal_user: str) -> str:
    """自动建号时的本地用户名：优先就用 portal 用户名；portal 允许的格式
    （大写、2 位长度、开头下划线）不一定满足本地规则，或者同名已被别的账号占着，
    就规整成小写合法形式并追加数字后缀。映射存的是 portal_sub，与这里的用户名无关。"""
    base = re.sub(r"[^a-z0-9_.-]", "-", portal_user.lower()).strip("-_.") or "portal"
    if not re.match(r"[a-z0-9]", base):
        base = "p" + base
    if len(base) < 3:
        base = f"{base}-portal"
    base = base[:28]
    candidate = base
    suffix = 2
    while conn.execute("SELECT 1 FROM accounts WHERE username = ?", (candidate,)).fetchone():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return _normalize_username(candidate)


def resolve_portal_account(
    portal_sub: str,
    portal_user: str,
    portal_role: str = "user",
    *,
    autocreate: bool = False,
    adopt_by_name: bool = False,
):
    """按 portal 身份找本地账号，返回 (account, action)。portal_sub = X-Portal-Sub（固定 ID），
    portal_user = X-User（当前用户名，可改）。
    action: "mapped"（portal_sub 命中）/ "adopted"（同名且未映射，已写入 portal_sub）/
            "created"（自动建号）/ None（未开通，account 也为 None）。
    没有 sub 一律返回 (None, None)，绝不只按用户名匹配。本地 username 不随 portal 改名。
    规则顺序：先按 portal_sub 命中；adopt_by_name 开启时再认领同名未映射账号；最后才自动建号。
    同名认领默认关闭：portal 用户名用户自己能改，改成某个未映射本地账号的名字就能接管它。
    自动建号遇到同名未映射账号时另起不冲突的用户名，绝不并入旧账号。
    整个过程在一个事务里，避免并发请求重复建号。"""
    try:
        portal_sub = _normalize_portal_sub(portal_sub)
    except ValueError:
        return None, None
    portal_user = str(portal_user or "").strip()
    if portal_sub is None:
        return None, None
    action = None
    account_id = None
    with closing(_connect()) as conn:
        with conn:
            row = conn.execute("SELECT id FROM accounts WHERE portal_sub = ?", (portal_sub,)).fetchone()
            if row is not None:
                account_id, action = int(row["id"]), "mapped"
            elif portal_user:
                row = None
                if adopt_by_name:
                    # 本地用户名一律小写，这里按原样精确比较：portal 里的 "Alice" 不会认领本地 "alice"
                    row = conn.execute(
                        "SELECT id, portal_sub FROM accounts WHERE username = ?", (portal_user,)
                    ).fetchone()
                if row is not None and row["portal_sub"] is None:
                    conn.execute(
                        "UPDATE accounts SET portal_sub = ?, updated_at = ? WHERE id = ?",
                        (portal_sub, _ts(), int(row["id"])),
                    )
                    account_id, action = int(row["id"]), "adopted"
                elif autocreate:
                    username = _derive_local_username(conn, portal_user)
                    account_id = _insert_account(
                        conn,
                        {
                            "username": username,
                            "display_name": portal_user,
                            # 随机密码哈希：没人知道明文，等于禁用本地密码登录；
                            # 非空是为了满足"已启用登录必须有密码"的既有校验
                            "password_hash": generate_password_hash(secrets.token_urlsafe(32)),
                            "role": _normalize_role(portal_role),
                            "note": "portal SSO 自动开通",
                            "bark_keys": "",
                            "bark_query_params": PROFILE_DEFAULTS["bark_query_params"],
                            "bark_url_enabled": 1,
                            "login_enabled": 1,
                        },
                    )
                    conn.execute(
                        "UPDATE accounts SET portal_sub = ? WHERE id = ?", (portal_sub, int(account_id))
                    )
                    action = "created"
    if account_id is None:
        return None, None
    return get_account(account_id), action


# --- 写入：任务 ---

def _assert_task_access(conn, task_id: int, actor_role: str, actor_id: int | None):
    row = conn.execute("SELECT * FROM tracking_tasks WHERE id = ?", (int(task_id),)).fetchone()
    if row is None:
        raise ValueError("追踪任务不存在。")
    if actor_role != "admin" and int(row["account_id"]) != (actor_id if actor_id is not None else -1):
        raise ValueError("无权修改其他用户的追踪任务。")
    return row


def create_task(account_id: int, data: dict):
    with closing(_connect()) as conn:
        with conn:
            if conn.execute("SELECT 1 FROM accounts WHERE id = ?", (int(account_id),)).fetchone() is None:
                raise ValueError("用户不存在。")
            payload = dict(data)
            payload["check_interval"] = _validate_check_interval(
                data.get("check_interval"), default=PROFILE_DEFAULTS["check_interval"]
            )
            revived = conn.execute(
                "SELECT 1 FROM tracking_tasks WHERE account_id = ? AND tracking_number = ? AND archived = 1",
                (int(account_id), _normalize_tracking_number(data.get("tracking_number", ""))),
            ).fetchone() is not None
            channel = forecast.normalize_channel(data.get("channel"))
            milestones = normalize_milestones(data.get("milestones") or {})
            task_id = _insert_task(conn, int(account_id), payload)
            # 新建 / 复活时只写填了的项：复活时留空不等于要清掉原来的渠道和节点
            if channel:
                conn.execute("UPDATE tracking_tasks SET channel = ? WHERE id = ?", (channel, int(task_id)))
            _write_milestones(conn, task_id, {stage: at for stage, at in milestones.items() if at}, "manual")
    task = get_task(task_id)
    # 调用方据此给出"已从归档恢复"之类的准确提示
    task["revived"] = revived
    return task


def update_task(task_id: int, data: dict, *, actor_role: str = "admin", actor_id: int | None = None):
    with closing(_connect()) as conn:
        with conn:
            row = _assert_task_access(conn, task_id, actor_role, actor_id)
            current = dict(row)
            number = _normalize_tracking_number(data.get("tracking_number", current["tracking_number"]))
            if not number:
                raise ValueError("请填写日本邮政单号。")

            # 换了单号就清空轮询状态，否则旧包裹的记录会挂在新单号下
            number_changed = number != current["tracking_number"]
            try:
                conn.execute(
                    """
                    UPDATE tracking_tasks
                    SET tracking_number = ?, label = ?, check_interval = ?,
                        enabled = ?, archived = ?,
                        last_tracking_info = ?, last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        number,
                        str(data.get("label", current["label"]) or ""),
                        _validate_check_interval(data.get("check_interval"), default=current["check_interval"]),
                        _normalize_bool(data.get("enabled", current["enabled"]), default=current["enabled"]),
                        _normalize_bool(data.get("archived", current["archived"]), default=current["archived"]),
                        "" if number_changed else current["last_tracking_info"],
                        "" if number_changed else current["last_error"],
                        _ts(),
                        int(task_id),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("这个单号在该账号下已存在。") from exc
            # 渠道与发货前节点只在请求里带了才改（停用/启用之类的局部更新不带它们）；节点留空 = 清除
            if "channel" in data:
                conn.execute(
                    "UPDATE tracking_tasks SET channel = ? WHERE id = ?",
                    (forecast.normalize_channel(data.get("channel")), int(task_id)),
                )
            if "atypical" in data:
                if not isinstance(data["atypical"], bool):
                    raise ValueError("非常规件标记必须是布尔值。")
                conn.execute(
                    "UPDATE tracking_tasks SET atypical = ? WHERE id = ?", (int(data["atypical"]), int(task_id))
                )
            if data.get("milestones") is not None:
                _write_milestones(conn, task_id, normalize_milestones(data["milestones"]), "manual")
    return get_task(task_id)


def archive_task(task_id: int, *, actor_role: str = "admin", actor_id: int | None = None):
    with closing(_connect()) as conn:
        with conn:
            _assert_task_access(conn, task_id, actor_role, actor_id)
            conn.execute(
                "UPDATE tracking_tasks SET archived = 1, enabled = 0, updated_at = ? WHERE id = ?",
                (_ts(), int(task_id)),
            )
    return get_task(task_id)


def delete_task(task_id: int, *, actor_role: str = "admin", actor_id: int | None = None):
    with closing(_connect()) as conn:
        with conn:
            _assert_task_access(conn, task_id, actor_role, actor_id)
            conn.execute("DELETE FROM tracking_tasks WHERE id = ?", (int(task_id),))


# 补录历史单号：每行结果穷举，前端 src/static/forecast.js 有同样一份（测试比对）
BACKFILL_RESULT_META = {
    "added": {"label": "已补录", "tone": "ok"},
    "updated": {"label": "已补充", "tone": "info"},
    "no_records": {"label": "查不到履历", "tone": "warn"},
    "invalid": {"label": "格式错误", "tone": "err"},
}
BACKFILL_MAX_LINES = 30


def backfill_task(account_id: int, number: str, channel: str, ordered_at: str = ""):
    """补录一行（单号已规整校验、渠道已规整且非空、下单时间已规整或为空）。返回 (result, task_id)：
    - updated：该账号已有这个单号的任务（不论是否归档）——不新建，渠道为空时补上渠道，给了下单时间就以手填写入；
    - created：新建一条已归档、已停用的任务（label 空）。不经 _insert_task，不复活、不触发轮询与推送；
      api_refs 的四个触发器只管 UPDATE / DELETE，INSERT 一条已归档任务不会动来源记录。
      抓履历由调用方随后做（record_carrier_records）。"""
    now = _ts()
    with closing(_connect()) as conn:
        with conn:
            if conn.execute("SELECT 1 FROM accounts WHERE id = ?", (int(account_id),)).fetchone() is None:
                raise ValueError("用户不存在。")
            row = conn.execute(
                "SELECT id FROM tracking_tasks WHERE account_id = ? AND tracking_number = ?",
                (int(account_id), number),
            ).fetchone()
            if row is not None:
                task_id = int(row["id"])
                conn.execute(
                    "UPDATE tracking_tasks SET channel = ? WHERE id = ? AND channel = ''", (channel, task_id)
                )
                result = "updated"
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO tracking_tasks (
                        account_id, tracking_number, label, check_interval, enabled, archived, channel,
                        first_seen_at, seen_count, created_at, updated_at
                    ) VALUES (?, ?, '', ?, 0, 1, ?, ?, 1, ?, ?)
                    """,
                    (int(account_id), number, PROFILE_DEFAULTS["check_interval"], channel, now, now, now),
                )
                task_id = int(cursor.lastrowid)
                result = "created"
            if ordered_at:
                _write_milestones(conn, task_id, {"ordered": ordered_at}, "manual")
    return result, task_id


def _latest_event(conn, task_id: int, tracking_number: str, kinds):
    placeholders = ", ".join("?" for _ in kinds)
    return conn.execute(
        f"""
        SELECT * FROM task_events
        WHERE task_id = ? AND tracking_number = ? AND kind IN ({placeholders})
        ORDER BY id DESC LIMIT 1
        """,
        (int(task_id), tracking_number, *kinds),
    ).fetchone()


def _insert_event(conn, task_row, kind: str, status: str = "", detail: str = "", created_at: str | None = None):
    if kind not in EVENT_KINDS:
        raise ValueError(f"未定义的事件类型：{kind}")
    conn.execute(
        """
        INSERT INTO task_events (task_id, account_id, tracking_number, kind, status, detail, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(task_row["id"]),
            int(task_row["account_id"]),
            str(task_row["tracking_number"] or ""),
            kind,
            str(status or ""),
            str(detail or ""),
            created_at or _ts(),
        ),
    )


def _prune_events(conn, task_id: int):
    """保留策略：本任务只留最新 EVENT_MAX_PER_TASK 条；全表删掉 EVENT_RETENTION_DAYS 天前的。"""
    conn.execute(
        """
        DELETE FROM task_events
        WHERE task_id = ? AND id <= (
            SELECT id FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 1 OFFSET ?
        )
        """,
        (int(task_id), int(task_id), EVENT_MAX_PER_TASK),
    )
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - EVENT_RETENTION_DAYS * 86400))
    conn.execute("DELETE FROM task_events WHERE created_at < ?", (cutoff,))


def _status_records_to_add(conn, task_row, records) -> list:
    """去重：推送失败时 latest_info 不落库，下一轮会再次判定为「有变化」并带着同一批记录回来。
    以本单号最近一条 status_change 为界，只追加它之后的记录。"""
    if not records:
        return []
    last = _latest_event(conn, task_row["id"], task_row["tracking_number"], ("status_change",))
    if last is None:
        return list(records)
    last_status = last["status"]
    infos = [record["info"] for record in records]
    if last_status in infos:
        # 同一状态文本可能出现多次，以最后一次为准
        index = len(infos) - 1 - infos[::-1].index(last_status)
        return list(records[index + 1:])
    return list(records)


def update_task_state(
    task_id: int,
    *,
    latest_info: str | None = None,
    error: str | None = None,
    pushed: bool = False,
    tracking_number: str | None = None,
    query_ok: bool | None = None,
    new_records=None,
    push_status: str = "",
    push_failed: str | None = None,
    push_detail: str = "",
    auto_archive: bool = False,
    carrier_records=None,
):
    """tracker 回写轮询结果，并在同一事务里写时间线事件。

    - tracking_number：tracker 抓取时的单号。给了就校验——用户可能在抓取期间改了单号，
      这时旧单号的结果不能写到新单号头上，整次回写作废，返回 None。
    - query_ok：True/False 表示这轮查询成功/失败（None 表示不涉及）。只在
      「正常→出错」「出错→恢复」切换时各写一条事件，持续出错不重复写。
    - new_records：上次已知之后新增的物流记录 [{"info", "detail"}]，按时间正序。
    - pushed / push_failed：推送成功 / 失败原因；push_status 是这次推送的物流状态。
      同一状态连续推送失败只记第一次（失败会按退避反复重试）。
    - auto_archive：签收后自动归档。与上面的事件同一事务：先把引用这条任务、仍为 active 的
      来源记录标成 delivered_archived，再归档（archived=1、enabled=0），最后记 auto_archived 事件
      （排在本轮的 status_change / push_ok 之后）。任务已归档则跳过。
    - carrier_records：这次抓到的全部履历，同一事务里 INSERT OR IGNORE 进 carrier_records（到达预测的经验库）。
      与事件一样受单号校验保护，单号被改了就不写。"""
    with closing(_connect()) as conn:
        with conn:
            row = conn.execute("SELECT * FROM tracking_tasks WHERE id = ?", (int(task_id),)).fetchone()
            if row is None:
                return None
            current = dict(row)
            if tracking_number is not None and current["tracking_number"] != tracking_number:
                return None
            checked_at = _ts()
            # WHERE 再带一次单号：上面的 SELECT 与这里之间单号仍可能被改，以实际更新行数为准
            cursor = conn.execute(
                """
                UPDATE tracking_tasks
                SET last_tracking_info = ?, last_checked_at = ?, last_error = ?,
                    last_push_at = ?, updated_at = ?
                WHERE id = ? AND tracking_number = ?
                """,
                (
                    current["last_tracking_info"] if latest_info is None else str(latest_info),
                    checked_at,
                    current["last_error"] if error is None else str(error),
                    checked_at if pushed else current["last_push_at"],
                    checked_at,
                    int(task_id),
                    current["tracking_number"],
                ),
            )
            if cursor.rowcount != 1:
                return None

            if carrier_records:
                _insert_carrier_records(conn, task_id, current["tracking_number"], carrier_records)

            wrote = False
            if query_ok is not None:
                last_query = _latest_event(
                    conn, task_id, current["tracking_number"], ("query_error", "query_recovered")
                )
                in_error = last_query is not None and last_query["kind"] == "query_error"
                if not query_ok and not in_error:
                    _insert_event(conn, current, "query_error", current["last_tracking_info"], error or "", checked_at)
                    wrote = True
                elif query_ok and in_error:
                    _insert_event(conn, current, "query_recovered", latest_info or current["last_tracking_info"], "", checked_at)
                    wrote = True

            for record in _status_records_to_add(conn, current, new_records or []):
                _insert_event(conn, current, "status_change", record["info"], record.get("detail", ""), checked_at)
                wrote = True

            if pushed:
                _insert_event(conn, current, "push_ok", push_status, push_detail, checked_at)
                wrote = True
            elif push_failed is not None:
                attempted = str(push_status or "")
                last_push = _latest_event(conn, task_id, current["tracking_number"], ("push_ok", "push_fail"))
                repeated = (
                    last_push is not None
                    and last_push["kind"] == "push_fail"
                    and last_push["status"] == attempted
                )
                if not repeated:
                    _insert_event(conn, current, "push_fail", attempted, push_failed, checked_at)
                    wrote = True

            if auto_archive and not current["archived"]:
                # 先标来源状态再归档：trg_api_refs_task_archive 只改仍为 active 的记录，
                # 这样外部调用方看到的是 delivered_archived，而不是 user_archived
                conn.execute(
                    "UPDATE api_refs SET state = 'delivered_archived', updated_at = ? WHERE task_id = ? AND state = 'active'",
                    (checked_at, int(task_id)),
                )
                conn.execute(
                    "UPDATE tracking_tasks SET archived = 1, enabled = 0, updated_at = ? WHERE id = ?",
                    (checked_at, int(task_id)),
                )
                status = current["last_tracking_info"] if latest_info is None else str(latest_info)
                _insert_event(conn, current, "auto_archived", status, "", checked_at)
                wrote = True

            if wrote:
                _prune_events(conn, task_id)
    return get_task(task_id)


def task_has_event(task_id: int, tracking_number: str, kind: str) -> bool:
    """本单号下是否记过某类事件。tracker 用它判断「已签收但从未自动归档过」的任务。"""
    if kind not in EVENT_KINDS:
        raise ValueError(f"未定义的事件类型：{kind}")
    with closing(_connect()) as conn:
        return _latest_event(conn, task_id, str(tracking_number or ""), (kind,)) is not None


def _row_to_event(row):
    event = dict(row)
    event["kind_label"] = EVENT_KIND_META[event["kind"]]["label"]
    event["kind_tone"] = EVENT_KIND_META[event["kind"]]["tone"]
    event["kind_dot"] = EVENT_KIND_META[event["kind"]]["dot"]
    return event


def list_task_events(task_id: int, *, limit: int = 20, before: int | None = None):
    """按时间倒序分页。游标用事件 id：id 随写入单调递增，与 created_at 同序，
    同一秒写入的多条也能稳定翻页。返回 (events, has_more)。"""
    limit = max(1, min(EVENT_PAGE_MAX, int(limit)))
    params: list = [int(task_id)]
    query = "SELECT * FROM task_events WHERE task_id = ?"
    if before is not None:
        query += " AND id < ?"
        params.append(int(before))
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit + 1)
    with closing(_connect()) as conn:
        rows = conn.execute(query, params).fetchall()
    events = [_row_to_event(row) for row in rows[:limit]]
    return events, len(rows) > limit



# --- 机器接口（/api/v1）：调用方令牌与任务来源记录 ---

# 调用方令牌：固定前缀便于在日志、密钥扫描里认出来；库里只存 SHA-256 十六进制。
API_TOKEN_PREFIX = "jpt_"
API_TOKEN_MAX_LENGTH = 128
# last_used_at 节流：距上次写入超过这么多秒才再写，避免每个请求都写库
API_LAST_USED_THROTTLE_SECONDS = 60

# api_refs.state 穷举。user_archived / user_deleted / 回到 active 由触发器随任务变化自动维护，
# archived_by_api 与 delivered_archived 由归档方在同一事务里先标记，见 _ensure_api_schema。
# 新增状态后已有库的 CHECK 约束会由 _migrate_api_refs_states 自动放宽。
API_REF_STATES = {
    "active": "调用方登记过、任务在追踪（含暂停）",
    "archived_by_api": "调用方自己通过 archive 接口归档；再 PUT 视为新一轮，自动复活",
    "user_archived": "用户在网页里归档；再 PUT 需带 revive",
    "delivered_archived": "追踪脚本检测到签收后自动归档；再 PUT 返回 delivered 不复活，需带 revive",
    "user_deleted": "用户在网页里删除了任务或改走了单号（墓碑，task_id 置空）；再 PUT 需带 revive",
}

# 机器接口可接受的单号：先按 _normalize_tracking_number 规整（去空白、转大写），再限制字符集与长度
API_TRACKING_NUMBER_RE = re.compile(r"[A-Z0-9-]{6,40}")

_API_REF_STATE_SQL = ", ".join(f"'{state}'" for state in API_REF_STATES)
# 触发器里的时间与 _ts() 同格式（本地时间）
_SQL_NOW = "strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')"


_API_REFS_TABLE_SQL = f"""
    CREATE TABLE {{name}} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        caller_id INTEGER NOT NULL,
        account_id INTEGER NOT NULL,
        tracking_number TEXT NOT NULL,
        task_id INTEGER,
        external_ref TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ({_API_REF_STATE_SQL})),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(caller_id, tracking_number),
        FOREIGN KEY(caller_id) REFERENCES api_callers(id) ON DELETE CASCADE,
        FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
    )
"""
_API_REFS_COLUMNS = "id, caller_id, account_id, tracking_number, task_id, external_ref, state, created_at, updated_at"

# 建在 tracking_tasks 上、维护 api_refs 的四个触发器。定义变了的会在 ensure_storage 里 DROP 后重建
# （sqlite_master 里存的就是这段文本，按空白规整后比对），不动任何表。
_API_REF_TRIGGERS = {
    "trg_api_refs_task_delete": f"""
        CREATE TRIGGER trg_api_refs_task_delete
        BEFORE DELETE ON tracking_tasks
        BEGIN
            UPDATE api_refs SET task_id = NULL, state = 'user_deleted', updated_at = {_SQL_NOW}
            WHERE task_id = OLD.id;
        END
    """,
    "trg_api_refs_task_renumber": f"""
        CREATE TRIGGER trg_api_refs_task_renumber
        AFTER UPDATE OF tracking_number ON tracking_tasks
        WHEN OLD.tracking_number <> NEW.tracking_number
        BEGIN
            UPDATE api_refs SET task_id = NULL, state = 'user_deleted', updated_at = {_SQL_NOW}
            WHERE task_id = NEW.id;
        END
    """,
    "trg_api_refs_task_archive": f"""
        CREATE TRIGGER trg_api_refs_task_archive
        AFTER UPDATE OF archived ON tracking_tasks
        WHEN OLD.archived = 0 AND NEW.archived = 1
        BEGIN
            UPDATE api_refs SET state = 'user_archived', updated_at = {_SQL_NOW}
            WHERE task_id = NEW.id AND state = 'active';
        END
    """,
    "trg_api_refs_task_unarchive": f"""
        CREATE TRIGGER trg_api_refs_task_unarchive
        AFTER UPDATE OF archived ON tracking_tasks
        WHEN OLD.archived = 1 AND NEW.archived = 0
        BEGIN
            UPDATE api_refs SET state = 'active', updated_at = {_SQL_NOW}
            WHERE task_id = NEW.id AND state IN ('user_archived', 'archived_by_api', 'delivered_archived');
        END
    """,
}


def _squash_sql(sql) -> str:
    return " ".join(str(sql or "").split())


def _migrate_api_refs_states(conn):
    """已有库的 api_refs 建表语句里 CHECK 约束是旧的状态集合（SQLite 不能 ALTER 约束），
    新增状态后按官方的「新表 → 复制 → 删旧表 → 改名」流程重建 api_refs 本身。
    api_refs 没有被任何表外键引用，重建不会触发 data-model.md 里那个级联坑；
    建在 tracking_tasks 上的触发器引用了 api_refs，先删掉（随后由 _ensure_api_schema 按新定义重建），
    否则改名时 SQLite 会因触发器引用了不存在的表而报错。tracking_tasks 不动。"""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'api_refs'").fetchone()
    if row is None or _API_REF_STATE_SQL in row[0]:
        return
    for name in _API_REF_TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    conn.execute("DROP TABLE IF EXISTS api_refs_new")
    conn.execute(_API_REFS_TABLE_SQL.format(name="api_refs_new"))
    conn.execute(f"INSERT INTO api_refs_new ({_API_REFS_COLUMNS}) SELECT {_API_REFS_COLUMNS} FROM api_refs")
    conn.execute("DROP TABLE api_refs")
    conn.execute("ALTER TABLE api_refs_new RENAME TO api_refs")


def _ensure_api_schema(conn):
    """api_callers：调用方令牌；api_refs：调用方登记过的单号（来源记录 + 墓碑）。

    api_refs.task_id 刻意不设外键：data-model.md 里那个「RENAME 重定向外键 + 级联」的坑，
    一旦以后重建 tracking_tasks 会把来源记录全部弄丢。墓碑由 _API_REF_TRIGGERS 的四个触发器维护，
    对任何删除/归档路径（网页、管理员、账号级联、以后新增的代码）都生效：
    - 删除任务前：引用它的记录置 task_id = NULL、state = user_deleted（BEFORE，赶在删除之前）
    - 改单号：同上（原单号已不再由这条任务追踪）
    - 归档（0→1）：仍为 active 的记录 → user_archived（API 自己归档、签收自动归档时会先把状态
      改成 archived_by_api / delivered_archived，触发器就不会误记成用户归档）
    - 取消归档（1→0）：user_archived / archived_by_api / delivered_archived → active"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS api_callers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            account_id INTEGER NOT NULL,
            token_sha256 TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            last_used_at TEXT,
            revoked INTEGER NOT NULL DEFAULT 0,
            revoked_at TEXT,
            FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
        )
        """
    )
    _migrate_api_refs_states(conn)
    if not _has_table(conn, "api_refs"):
        conn.execute(_API_REFS_TABLE_SQL.format(name="api_refs"))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_api_refs_task ON api_refs(task_id)")
    for name, sql in _API_REF_TRIGGERS.items():
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?", (name,)).fetchone()
        if row is not None and _squash_sql(row[0]) == _squash_sql(sql):
            continue
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        conn.execute(sql.strip())


def hash_api_token(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _new_api_token() -> str:
    return API_TOKEN_PREFIX + secrets.token_urlsafe(32)


def _normalize_caller_name(value) -> str:
    name = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{1,63}", name):
        raise ValueError("调用方名称只能包含小写字母、数字、点、下划线和短横线，长度 2-64。")
    return name


def _row_to_caller(row):
    if row is None:
        return None
    caller = dict(row)
    caller.pop("token_sha256", None)
    caller["revoked"] = bool(caller.get("revoked"))
    return caller


def _select_caller(conn, name: str):
    return conn.execute(
        """
        SELECT c.*, a.username AS account_username
        FROM api_callers c JOIN accounts a ON a.id = c.account_id
        WHERE c.name = ?
        """,
        (name,),
    ).fetchone()


def issue_api_caller(name: str, username: str):
    """签发调用方令牌，返回 (caller, 明文令牌)。明文只在这里出现一次，库里只存哈希。
    同名调用方未吊销时拒绝；已吊销的同名调用方可重新签发（沿用原记录与来源记录），
    但必须绑定同一个账号——换账号请换名字，免得旧账号下的来源记录挂到新账号上。"""
    name = _normalize_caller_name(name)
    token = _new_api_token()
    now = _ts()
    with closing(_connect()) as conn:
        with conn:
            account = conn.execute(
                "SELECT id FROM accounts WHERE username = ?", (str(username or "").strip().lower(),)
            ).fetchone()
            if account is None:
                raise ValueError(f"账号不存在：{username}")
            existing = conn.execute("SELECT * FROM api_callers WHERE name = ?", (name,)).fetchone()
            if existing is not None:
                if not existing["revoked"]:
                    raise ValueError(f"调用方 {name} 已存在且有效；要换令牌请用 rotate-caller。")
                if int(existing["account_id"]) != int(account["id"]):
                    raise ValueError(f"已吊销的调用方 {name} 绑定的是另一个账号，请换一个名称。")
                conn.execute(
                    """
                    UPDATE api_callers
                    SET token_sha256 = ?, revoked = 0, revoked_at = NULL, created_at = ?, last_used_at = NULL
                    WHERE id = ?
                    """,
                    (hash_api_token(token), now, existing["id"]),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO api_callers (name, account_id, token_sha256, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (name, int(account["id"]), hash_api_token(token), now),
                )
            caller = _row_to_caller(_select_caller(conn, name))
    return caller, token


def rotate_api_caller(name: str):
    """换新令牌，旧令牌立即失效。返回 (caller, 新明文令牌)。已吊销的调用方不能轮换。"""
    name = _normalize_caller_name(name)
    token = _new_api_token()
    with closing(_connect()) as conn:
        with conn:
            existing = conn.execute("SELECT * FROM api_callers WHERE name = ?", (name,)).fetchone()
            if existing is None:
                raise ValueError(f"调用方不存在：{name}")
            if existing["revoked"]:
                raise ValueError(f"调用方 {name} 已吊销；要恢复请用 issue-caller 重新签发。")
            conn.execute(
                "UPDATE api_callers SET token_sha256 = ?, last_used_at = NULL WHERE id = ?",
                (hash_api_token(token), existing["id"]),
            )
            caller = _row_to_caller(_select_caller(conn, name))
    return caller, token


def revoke_api_caller(name: str):
    """吊销调用方，令牌立即失效。返回 (caller, 本次是否真的改了状态)。"""
    name = _normalize_caller_name(name)
    with closing(_connect()) as conn:
        with conn:
            existing = conn.execute("SELECT * FROM api_callers WHERE name = ?", (name,)).fetchone()
            if existing is None:
                raise ValueError(f"调用方不存在：{name}")
            changed = not existing["revoked"]
            if changed:
                conn.execute(
                    "UPDATE api_callers SET revoked = 1, revoked_at = ? WHERE id = ?",
                    (_ts(), existing["id"]),
                )
            caller = _row_to_caller(_select_caller(conn, name))
    return caller, changed


def list_api_callers():
    with closing(_connect()) as conn:
        rows = conn.execute(
            """
            SELECT c.*, a.username AS account_username
            FROM api_callers c JOIN accounts a ON a.id = c.account_id
            ORDER BY c.id
            """
        ).fetchall()
    return [_row_to_caller(row) for row in rows]


def authenticate_api_caller(token):
    """按令牌找有效调用方：算 SHA-256 后按哈希查行，再用常量时间比较确认；吊销的返回 None。
    命中时按节流更新 last_used_at。"""
    if not isinstance(token, str) or not token.startswith(API_TOKEN_PREFIX):
        return None
    if len(token) > API_TOKEN_MAX_LENGTH:
        return None
    digest = hash_api_token(token)
    with closing(_connect()) as conn:
        row = conn.execute(
            """
            SELECT c.*, a.username AS account_username
            FROM api_callers c JOIN accounts a ON a.id = c.account_id
            WHERE c.token_sha256 = ?
            """,
            (digest,),
        ).fetchone()
        if row is None or not hmac.compare_digest(str(row["token_sha256"]), digest):
            return None
        if row["revoked"]:
            return None
        now = _ts()
        cutoff = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - API_LAST_USED_THROTTLE_SECONDS)
        )
        with conn:
            conn.execute(
                """
                UPDATE api_callers SET last_used_at = ?
                WHERE id = ? AND revoked = 0 AND (last_used_at IS NULL OR last_used_at < ?)
                """,
                (now, row["id"], cutoff),
            )
    return _row_to_caller(row)


def normalize_api_tracking_number(value) -> str:
    """机器接口用：规整后不合法抛 ValueError。"""
    number = _normalize_tracking_number(value)
    if not API_TRACKING_NUMBER_RE.fullmatch(number):
        raise ValueError("单号格式不正确：只允许字母、数字和短横线，长度 6-40。")
    return number


class ApiConflict(Exception):
    """PUT 被用户的网页操作挡住：code 为 user_deleted / user_archived（签收自动归档不算，返回 delivered）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _api_view(conn, task_row, ref_row):
    task = _row_to_task(task_row)
    linked = ref_row is not None and ref_row["task_id"] is not None and int(ref_row["task_id"]) == int(task["id"])
    task["api_ref"] = dict(ref_row) if linked else None
    return task


def _api_select_ref(conn, caller_id: int, account_id: int, number: str):
    return conn.execute(
        "SELECT * FROM api_refs WHERE caller_id = ? AND account_id = ? AND tracking_number = ?",
        (int(caller_id), int(account_id), number),
    ).fetchone()


def _api_select_task(conn, account_id: int, number: str):
    return conn.execute(
        "SELECT * FROM tracking_tasks WHERE account_id = ? AND tracking_number = ?",
        (int(account_id), number),
    ).fetchone()


def _api_upsert_ref(conn, caller_id: int, account_id: int, number: str, task_id: int, state: str,
                    external_ref: str | None):
    if state not in API_REF_STATES:
        raise ValueError(f"未定义的来源状态：{state}")
    now = _ts()
    existing = _api_select_ref(conn, caller_id, account_id, number)
    if existing is None:
        conn.execute(
            """
            INSERT INTO api_refs (caller_id, account_id, tracking_number, task_id, external_ref, state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (int(caller_id), int(account_id), number, int(task_id), external_ref or "", state, now, now),
        )
    else:
        conn.execute(
            """
            UPDATE api_refs SET task_id = ?, state = ?, external_ref = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                int(task_id),
                state,
                existing["external_ref"] if external_ref is None else external_ref,
                now,
                existing["id"],
            ),
        )
    return _api_select_ref(conn, caller_id, account_id, number)


def _api_task_delivered(conn, task_row, linked_ref_state) -> bool:
    """已归档的任务是不是因签收被自动归档的。
    本调用方有关联的来源记录时以它的状态为准（用户取消归档后再归档会变成 user_archived）；
    没有关联记录（例如网页建的任务）时看这条任务本单号下最近一条事件是不是 auto_archived——
    自动归档后任务不再轮询，不会再写别的事件。"""
    if not task_row["archived"]:
        return False
    if linked_ref_state is not None:
        return linked_ref_state == "delivered_archived"
    last = conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? AND tracking_number = ? ORDER BY id DESC LIMIT 1",
        (int(task_row["id"]), task_row["tracking_number"]),
    ).fetchone()
    return last is not None and last["kind"] == "auto_archived"


def _api_apply_forecast_fields(conn, task_id: int, channel: str | None, milestones: dict | None):
    """调用方带来的渠道与发货前节点：渠道只在任务渠道为空时写入（不覆盖用户填的）；
    节点以 caller 身份写入，不覆盖用户手填的 manual 行。两者都已在接口层校验规整过。"""
    if channel:
        conn.execute(
            "UPDATE tracking_tasks SET channel = ? WHERE id = ? AND channel = ''", (channel, int(task_id))
        )
    if milestones:
        _write_milestones(conn, task_id, milestones, "caller")


def api_put_tracking(caller_id: int, account_id: int, number: str, *, label: str | None = None,
                     external_ref: str | None = None, revive: bool = False,
                     channel: str | None = None, milestones: dict | None = None):
    """PUT /api/v1/tracking/<number> 的存储侧。返回 (result, task)，result 穷举：
    created（新建任务）/ revived（从归档恢复）/ linked（关联到账号下已有的任务，或墓碑后用户自己又加回）/
    existing（本调用方早已登记、任务在追踪）/ delivered（签收后已自动归档，未带 revive 时原样返回、不复活）。
    被用户删除/归档且未带 revive 时抛 ApiConflict。

    channel / milestones 在任务存在（含 delivered 不复活）的各分支都生效，见 _api_apply_forecast_fields；
    被 409 挡住时整个请求作废，不写。

    规则：label 只在新建任务时使用；关联或复活已有任务时不动它的 label / 间隔 / 暂停状态
    （复活会把 enabled 置 1，否则复活了也不轮询）。external_ref 给了就更新，不给保留原值。
    不走 _insert_task：网页侧那条「已归档就复活并覆盖备注和间隔」的语义不适合这里。"""
    with closing(_connect()) as conn:
        # IMMEDIATE：同一单号的并发 PUT 串行执行，不会因为唯一约束互相撞
        conn.execute("BEGIN IMMEDIATE")
        with conn:
            ref = _api_select_ref(conn, caller_id, account_id, number)
            task = _api_select_task(conn, account_id, number)
            ref_state = ref["state"] if ref is not None else None
            if ref is not None and ref["task_id"] is not None and (task is None or int(task["id"]) != int(ref["task_id"])):
                # 防御：触发器之外的方式改动过任务表，按墓碑处理
                ref_state = "user_deleted"
            ref_on_task = ref is not None and task is not None and ref["task_id"] is not None \
                and int(ref["task_id"]) == int(task["id"])
            now = _ts()

            if task is None:
                if ref_state == "user_deleted" and not revive:
                    raise ApiConflict("user_deleted", "该单号的任务已被用户在网页里删除；如需重新追踪请带 revive: true。")
                cursor = conn.execute(
                    """
                    INSERT INTO tracking_tasks (
                        account_id, tracking_number, label, check_interval, enabled, archived,
                        first_seen_at, seen_count, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 1, 0, ?, 1, ?, ?)
                    """,
                    (int(account_id), number, str(label or ""), PROFILE_DEFAULTS["check_interval"], now, now, now),
                )
                task_id = int(cursor.lastrowid)
                result = "created"
            elif not task["archived"]:
                task_id = int(task["id"])
                result = "existing" if ref_on_task and ref_state == "active" else "linked"
            elif not revive and _api_task_delivered(conn, task, ref_state if ref_on_task else None):
                # 签收后自动归档：不复活，原样返回；本调用方的来源记录（没有就新建）记为 delivered_archived
                task_id = int(task["id"])
                ref = _api_upsert_ref(conn, caller_id, account_id, number, task_id, "delivered_archived", external_ref)
                _api_apply_forecast_fields(conn, task_id, channel, milestones)
                task_row = conn.execute("SELECT * FROM tracking_tasks WHERE id = ?", (task_id,)).fetchone()
                return "delivered", _api_view(conn, task_row, ref)
            else:
                task_id = int(task["id"])
                api_archived = ref_on_task and ref_state == "archived_by_api"
                if not api_archived and not revive:
                    raise ApiConflict("user_archived", "该单号的任务已被用户在网页里归档；如需恢复追踪请带 revive: true。")
                conn.execute(
                    """
                    UPDATE tracking_tasks
                    SET archived = 0, enabled = 1, seen_count = seen_count + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, task_id),
                )
                result = "revived"

            ref = _api_upsert_ref(conn, caller_id, account_id, number, task_id, "active", external_ref)
            _api_apply_forecast_fields(conn, task_id, channel, milestones)
            task_row = conn.execute("SELECT * FROM tracking_tasks WHERE id = ?", (task_id,)).fetchone()
            view = _api_view(conn, task_row, ref)
    return result, view


def api_get_tracking(caller_id: int, account_id: int, number: str):
    """返回 (task 或 None, 墓碑状态)。task 为 None 时第二项是 user_deleted（本调用方登记过、已被删）或 None。"""
    with closing(_connect()) as conn:
        task = _api_select_task(conn, account_id, number)
        ref = _api_select_ref(conn, caller_id, account_id, number)
        if task is None:
            return None, ("user_deleted" if ref is not None else None)
        return _api_view(conn, task, ref), None


def api_archive_tracking(caller_id: int, account_id: int, number: str):
    """幂等归档。返回 (result, task)，result：archived / already_archived；任务不存在返回 (None, 墓碑状态)。
    已归档（不论谁归档的，含签收自动归档）直接返回，不改来源状态（delivered_archived 仍是 delivered_archived）。
    归档前先把引用这条任务、仍为 active 的来源记录标成 archived_by_api，触发器就不会把它们记成用户归档。"""
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        with conn:
            task = _api_select_task(conn, account_id, number)
            ref = _api_select_ref(conn, caller_id, account_id, number)
            if task is None:
                return None, ("user_deleted" if ref is not None else None)
            if task["archived"]:
                return "already_archived", _api_view(conn, task, ref)
            task_id = int(task["id"])
            now = _ts()
            conn.execute(
                "UPDATE api_refs SET state = 'archived_by_api', updated_at = ? WHERE task_id = ? AND state = 'active'",
                (now, task_id),
            )
            ref = _api_upsert_ref(conn, caller_id, account_id, number, task_id, "archived_by_api", None)
            conn.execute(
                "UPDATE tracking_tasks SET archived = 1, enabled = 0, updated_at = ? WHERE id = ?",
                (now, task_id),
            )
            task_row = conn.execute("SELECT * FROM tracking_tasks WHERE id = ?", (task_id,)).fetchone()
            view = _api_view(conn, task_row, ref)
    return "archived", view

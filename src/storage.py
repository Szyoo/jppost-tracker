import os
import re
import sqlite3
import time
from contextlib import closing
from urllib.parse import urlparse

from dotenv import dotenv_values
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "app.db")

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
    value = env_values.get(key)
    if value is not None and str(value).strip() != "":
        return str(value).strip().strip("'").strip('"')
    return os.getenv(key, default).strip()


def _normalize_check_interval(value) -> int:
    try:
        parsed = int(value)
        return parsed if parsed > 0 else 300
    except Exception:
        return 300


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
    account["bark_key"] = account["bark_keys"]
    account["bark_keys_masked"] = _mask_keys(account["bark_keys"])
    account["bark_key_masked"] = account["bark_keys_masked"]
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
    """插入任务；同账号同单号已存在则复活它（archived=0，seen_count+1）。"""
    number = _normalize_tracking_number(data.get("tracking_number", ""))
    if not number:
        raise ValueError("请填写日本邮政单号。")

    now = _ts()
    existing = conn.execute(
        "SELECT id, seen_count FROM tracking_tasks WHERE account_id = ? AND tracking_number = ?",
        (account_id, number),
    ).fetchone()
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

    grouped: dict[int, list] = {account_id: [] for account_id in account_ids}
    for row in rows:
        grouped.setdefault(int(row["account_id"]), []).append(_row_to_task(row))

    for account in accounts:
        tasks = grouped.get(int(account["id"]), [])
        account["tasks"] = [task for task in tasks if not task["archived"]]
        account["archived_tasks"] = [task for task in tasks if task["archived"]]
        account["task_count"] = len(tasks)
        account["active_task_count"] = len([task for task in account["tasks"] if task["enabled"]])
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
                "bark_keys": data.get("bark_keys") or data.get("bark_key", ""),
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
            "check_interval": data.get("check_interval", PROFILE_DEFAULTS["check_interval"]),
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
                        str(data.get("note", current["note"])).strip(),
                        normalize_bark_keys(data.get("bark_keys") or data.get("bark_key", current["bark_keys"])),
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
            task_id = _insert_task(conn, int(account_id), data)
    return get_task(task_id)


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
                        _normalize_check_interval(data.get("check_interval", current["check_interval"])),
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


def update_task_state(task_id: int, *, latest_info: str | None = None, error: str | None = None, pushed: bool = False):
    """tracker 回写轮询结果。"""
    with closing(_connect()) as conn:
        with conn:
            row = conn.execute("SELECT * FROM tracking_tasks WHERE id = ?", (int(task_id),)).fetchone()
            if row is None:
                return None
            current = dict(row)
            checked_at = _ts()
            conn.execute(
                """
                UPDATE tracking_tasks
                SET last_tracking_info = ?, last_checked_at = ?, last_error = ?,
                    last_push_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    current["last_tracking_info"] if latest_info is None else str(latest_info),
                    checked_at,
                    current["last_error"] if error is None else str(error),
                    checked_at if pushed else current["last_push_at"],
                    checked_at,
                    int(task_id),
                ),
            )
    return get_task(task_id)


def account_to_profile_env(account) -> dict:
    """兼容旧的 .env 视图：任务字段取该账号首个启用任务。"""
    if not account:
        return {
            "TRACKING_NUMBER": "",
            "CHECK_INTERVAL": "300",
            "BARK_KEY": "",
            "BARK_QUERY_PARAMS": PROFILE_DEFAULTS["bark_query_params"],
            "BARK_URL_ENABLED": "1",
        }
    tasks = account.get("tasks") or []
    active = next((task for task in tasks if task.get("enabled")), None) or (tasks[0] if tasks else {})
    return {
        "TRACKING_NUMBER": str(active.get("tracking_number", "") or ""),
        "CHECK_INTERVAL": str(_normalize_check_interval(active.get("check_interval"))),
        "BARK_KEY": normalize_bark_keys(account.get("bark_keys", "")),
        "BARK_QUERY_PARAMS": str(account.get("bark_query_params", "") or PROFILE_DEFAULTS["bark_query_params"]),
        "BARK_URL_ENABLED": "1" if account.get("bark_url_enabled") else "0",
    }

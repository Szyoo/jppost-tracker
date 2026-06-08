import os
import re
import sqlite3
import time
from urllib.parse import urlparse

from dotenv import dotenv_values
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "app.db")

PROFILE_DEFAULTS = {
    "tracking_number": "",
    "check_interval": 300,
    "bark_keys": "",
    "bark_query_params": "?sound=minuet&level=timeSensitive",
    "bark_url_enabled": 1,
}


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _connect():
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


def _row_to_account(row, *, include_secret: bool = False):
    if row is None:
        return None
    account = dict(row)
    account["name"] = account.get("display_name", "")
    account["check_interval"] = _normalize_check_interval(account.get("check_interval"))
    account["bark_url_enabled"] = bool(account.get("bark_url_enabled"))
    account["login_enabled"] = bool(account.get("login_enabled"))
    account["tracking_enabled"] = bool(account.get("tracking_enabled"))
    account["is_admin"] = account.get("role") == "admin"
    account["bark_keys"] = normalize_bark_keys(account.get("bark_keys", ""))
    account["bark_key"] = account["bark_keys"]
    account["bark_keys_masked"] = _mask_keys(account["bark_keys"])
    account["bark_key_masked"] = account["bark_keys_masked"]
    account["has_password"] = bool(account.get("password_hash"))
    if not include_secret:
        account.pop("password_hash", None)
    return account


def load_system_env(dotenv_path: str, keys) -> dict:
    env_values = dotenv_values(dotenv_path)
    return {key: _load_env_value(env_values, key, "") for key in keys}


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
            tracking_number TEXT NOT NULL DEFAULT '',
            check_interval INTEGER NOT NULL DEFAULT 300,
            bark_keys TEXT NOT NULL DEFAULT '',
            bark_query_params TEXT NOT NULL DEFAULT '?sound=minuet&level=timeSensitive',
            bark_url_enabled INTEGER NOT NULL DEFAULT 1,
            login_enabled INTEGER NOT NULL DEFAULT 1,
            tracking_enabled INTEGER NOT NULL DEFAULT 1,
            last_tracking_info TEXT NOT NULL DEFAULT '',
            last_checked_at TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            last_push_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def _ensure_tracking_history_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tracking_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            tracking_number TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            seen_count INTEGER NOT NULL DEFAULT 1,
            UNIQUE(account_id, tracking_number),
            FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
        )
        """
    )


def _record_tracking_number(conn, account_id: int, tracking_number: str):
    normalized = _normalize_tracking_number(tracking_number)
    if not normalized:
        return

    now = _ts()
    existing = conn.execute(
        """
        SELECT id, seen_count FROM tracking_history
        WHERE account_id = ? AND tracking_number = ?
        """,
        (account_id, normalized),
    ).fetchone()
    if existing:
        conn.execute(
            """
            UPDATE tracking_history
            SET last_seen_at = ?, seen_count = ?
            WHERE id = ?
            """,
            (now, int(existing["seen_count"] or 0) + 1, existing["id"]),
        )
        return

    conn.execute(
        """
        INSERT INTO tracking_history (
            account_id, tracking_number, first_seen_at, last_seen_at, seen_count
        ) VALUES (?, ?, ?, ?, 1)
        """,
        (account_id, normalized, now, now),
    )


def _backfill_tracking_history(conn):
    rows = conn.execute(
        """
        SELECT id, tracking_number
        FROM accounts
        WHERE TRIM(tracking_number) != ''
        """
    ).fetchall()
    for row in rows:
        normalized = _normalize_tracking_number(row["tracking_number"])
        if not normalized:
            continue
        exists = conn.execute(
            """
            SELECT 1
            FROM tracking_history
            WHERE account_id = ? AND tracking_number = ?
            """,
            (int(row["id"]), normalized),
        ).fetchone()
        if exists is None:
            _record_tracking_number(conn, int(row["id"]), normalized)


def _history_row_to_item(row):
    return {
        "tracking_number": row["tracking_number"],
        "first_seen_at": row["first_seen_at"],
        "last_seen_at": row["last_seen_at"],
        "seen_count": int(row["seen_count"] or 0),
    }


def _attach_tracking_history(conn, accounts: list[dict]):
    if not accounts:
        return accounts

    account_ids = [int(account["id"]) for account in accounts]
    placeholders = ", ".join("?" for _ in account_ids)
    rows = conn.execute(
        f"""
        SELECT account_id, tracking_number, first_seen_at, last_seen_at, seen_count
        FROM tracking_history
        WHERE account_id IN ({placeholders})
        ORDER BY last_seen_at DESC, id DESC
        """,
        account_ids,
    ).fetchall()

    history_map = {account_id: [] for account_id in account_ids}
    for row in rows:
        history_map.setdefault(int(row["account_id"]), []).append(_history_row_to_item(row))

    for account in accounts:
        history = history_map.get(int(account["id"]), [])
        account["tracking_history"] = history
        account["tracking_history_count"] = len(history)
        account["tracking_history_preview"] = history[:8]
    return accounts


def _insert_account(conn, data: dict):
    tracking_number = _normalize_tracking_number(data.get("tracking_number", ""))
    cursor = conn.execute(
        """
        INSERT INTO accounts (
            username, display_name, password_hash, role, note,
            tracking_number, check_interval, bark_keys, bark_query_params,
            bark_url_enabled, login_enabled, tracking_enabled,
            last_tracking_info, last_checked_at, last_error, last_push_at,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data["username"],
            data["display_name"],
            data.get("password_hash", ""),
            data.get("role", "user"),
            data.get("note", ""),
            tracking_number,
            _normalize_check_interval(data.get("check_interval", 300)),
            normalize_bark_keys(data.get("bark_keys", "")),
            str(data.get("bark_query_params", PROFILE_DEFAULTS["bark_query_params"])).strip()
            or PROFILE_DEFAULTS["bark_query_params"],
            _normalize_bool(data.get("bark_url_enabled", 1), default=1),
            _normalize_bool(data.get("login_enabled", 1), default=1),
            _normalize_bool(data.get("tracking_enabled", 1), default=1),
            str(data.get("last_tracking_info", "") or ""),
            str(data.get("last_checked_at", "") or ""),
            str(data.get("last_error", "") or ""),
            str(data.get("last_push_at", "") or ""),
            data.get("created_at", _ts()),
            data.get("updated_at", _ts()),
        ),
    )
    account_id = cursor.lastrowid
    if account_id:
        _record_tracking_number(conn, int(account_id), tracking_number)
    return account_id


def _migrate_legacy_profiles(conn, dotenv_path: str):
    now = _ts()
    if _has_table(conn, "user_profiles"):
        rows = conn.execute("SELECT * FROM user_profiles ORDER BY id").fetchall()
        for row in rows:
            note = str(row["note"] or "").strip()
            legacy_note = "从旧版 user_profiles 迁移"
            merged_note = f"{legacy_note}；{note}" if note else legacy_note
            _insert_account(
                conn,
                {
                    "username": f"legacy-user-{row['id']}",
                    "display_name": str(row["name"] or f"迁移用户 {row['id']}"),
                    "password_hash": "",
                    "role": "user",
                    "note": merged_note,
                    "tracking_number": str(row["tracking_number"] or ""),
                    "check_interval": row["check_interval"],
                    "bark_keys": str(row["bark_key"] or ""),
                    "bark_query_params": str(row["bark_query_params"] or PROFILE_DEFAULTS["bark_query_params"]),
                    "bark_url_enabled": row["bark_url_enabled"],
                    "login_enabled": 0,
                    "tracking_enabled": 1,
                    "created_at": str(row["created_at"] or now),
                    "updated_at": str(row["updated_at"] or now),
                },
            )
        return

    env_values = dotenv_values(dotenv_path)
    _insert_account(
        conn,
        {
            "username": "legacy-default",
            "display_name": "默认用户",
            "password_hash": "",
            "role": "user",
            "note": "从 .env 初始化",
            "tracking_number": _load_env_value(env_values, "TRACKING_NUMBER", ""),
            "check_interval": _normalize_check_interval(_load_env_value(env_values, "CHECK_INTERVAL", "300")),
            "bark_keys": _load_env_value(env_values, "BARK_KEY", ""),
            "bark_query_params": _load_env_value(
                env_values,
                "BARK_QUERY_PARAMS",
                PROFILE_DEFAULTS["bark_query_params"],
            ),
            "bark_url_enabled": _normalize_bool(_load_env_value(env_values, "BARK_URL_ENABLED", "1"), default=1),
            "login_enabled": 0,
            "tracking_enabled": 1,
            "created_at": now,
            "updated_at": now,
        },
    )


def _sync_admin_account(conn, admin_username: str, admin_password_hash: str):
    if not admin_password_hash.strip():
        return

    username = _normalize_username(admin_username or "admin")
    row = conn.execute("SELECT * FROM accounts WHERE username = ?", (username,)).fetchone()
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
                "tracking_number": "",
                "check_interval": 300,
                "bark_keys": "",
                "bark_query_params": PROFILE_DEFAULTS["bark_query_params"],
                "bark_url_enabled": 1,
                "login_enabled": 1,
                "tracking_enabled": 0,
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

    with _connect() as conn:
        _ensure_accounts_schema(conn)
        _ensure_tracking_history_schema(conn)
        count = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        if count == 0:
            _migrate_legacy_profiles(conn, dotenv_path)
        _sync_admin_account(conn, admin_username, admin_password_hash)
        _backfill_tracking_history(conn)
        conn.commit()


def account_to_profile_env(account) -> dict:
    if not account:
        return {
            "TRACKING_NUMBER": "",
            "CHECK_INTERVAL": "300",
            "BARK_KEY": "",
            "BARK_QUERY_PARAMS": PROFILE_DEFAULTS["bark_query_params"],
            "BARK_URL_ENABLED": "1",
        }
    return {
        "TRACKING_NUMBER": str(account.get("tracking_number", "") or ""),
        "CHECK_INTERVAL": str(_normalize_check_interval(account.get("check_interval"))),
        "BARK_KEY": normalize_bark_keys(account.get("bark_keys", "")),
        "BARK_QUERY_PARAMS": str(account.get("bark_query_params", "") or PROFILE_DEFAULTS["bark_query_params"]),
        "BARK_URL_ENABLED": "1" if account.get("bark_url_enabled") else "0",
    }


def list_accounts(include_disabled: bool = True):
    query = "SELECT * FROM accounts"
    params = []
    if not include_disabled:
        query += " WHERE login_enabled = 1 OR tracking_enabled = 1"
    query += " ORDER BY CASE role WHEN 'admin' THEN 0 ELSE 1 END, id"
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
        accounts = [_row_to_account(row) for row in rows]
        return _attach_tracking_history(conn, accounts)


def list_tracked_accounts():
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM accounts
            WHERE tracking_enabled = 1
            ORDER BY id
            """
        ).fetchall()
        accounts = [_row_to_account(row) for row in rows]
        return _attach_tracking_history(conn, accounts)


def get_account(account_id: int):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        account = _row_to_account(row)
        if not account:
            return None
        return _attach_tracking_history(conn, [account])[0]


def get_account_by_username(username: str, *, include_secret: bool = False):
    if not str(username or "").strip():
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM accounts WHERE username = ?",
            (str(username).strip().lower(),),
        ).fetchone()
        account = _row_to_account(row, include_secret=include_secret)
        if not account:
            return None
        return _attach_tracking_history(conn, [account])[0]


def verify_account_password(account, password: str) -> bool:
    if not account or not account.get("password_hash") or not account.get("login_enabled"):
        return False
    try:
        return check_password_hash(account["password_hash"], password)
    except Exception:
        return False


def _create_account(conn, data: dict, *, self_register: bool = False):
    username = _normalize_username(data.get("username", ""))
    display_name = _normalize_display_name(data.get("display_name") or data.get("name"), fallback=username)
    role = "user" if self_register else _normalize_role(data.get("role", "user"))
    password = str(data.get("password", "") or data.get("new_password", "")).strip()
    login_enabled = 1 if self_register else _normalize_bool(data.get("login_enabled", 1), default=1)
    tracking_enabled = 1 if self_register else _normalize_bool(data.get("tracking_enabled", 1), default=1)

    if login_enabled and not password:
        raise ValueError("请提供登录密码。")
    password_hash = generate_password_hash(password) if password else ""
    now = _ts()

    try:
        account_id = _insert_account(
            conn,
            {
                "username": username,
                "display_name": display_name,
                "password_hash": password_hash,
                "role": role,
                "note": str(data.get("note", "")).strip(),
                "tracking_number": str(data.get("tracking_number", "")).strip(),
                "check_interval": data.get("check_interval", PROFILE_DEFAULTS["check_interval"]),
                "bark_keys": data.get("bark_keys") or data.get("bark_key", ""),
                "bark_query_params": data.get("bark_query_params", PROFILE_DEFAULTS["bark_query_params"]),
                "bark_url_enabled": data.get("bark_url_enabled", 1),
                "login_enabled": login_enabled,
                "tracking_enabled": tracking_enabled,
                "created_at": now,
                "updated_at": now,
            },
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError("用户名已存在。") from exc
    return {"id": account_id}


def create_account(data: dict):
    with _connect() as conn:
        account = _create_account(conn, data, self_register=False)
        conn.commit()
        return get_account(account["id"])


def register_user(data: dict):
    with _connect() as conn:
        account = _create_account(conn, data, self_register=True)
        conn.commit()
        return get_account(account["id"])


def update_account(account_id: int, data: dict, *, actor_role: str = "admin", actor_id: int | None = None):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            raise ValueError("用户不存在。")

        current = dict(row)
        is_self = actor_id == account_id
        if actor_role != "admin" and not is_self:
            raise ValueError("无权修改其他用户。")

        next_username = current["username"]
        next_role = current["role"]
        next_login_enabled = current["login_enabled"]
        next_tracking_enabled = current["tracking_enabled"]
        if actor_role == "admin":
            if "username" in data:
                next_username = _normalize_username(data["username"])
            next_role = _normalize_role(data.get("role", current["role"]))
            next_login_enabled = _normalize_bool(data.get("login_enabled", current["login_enabled"]), default=current["login_enabled"])
            next_tracking_enabled = _normalize_bool(data.get("tracking_enabled", current["tracking_enabled"]), default=current["tracking_enabled"])

        display_name = _normalize_display_name(
            data.get("display_name") or data.get("name"),
            fallback=current["display_name"],
        )
        password = str(data.get("password", "") or data.get("new_password", "")).strip()
        password_hash = current["password_hash"]
        if password:
            password_hash = generate_password_hash(password)

        if next_login_enabled and not password_hash:
            raise ValueError("已启用登录的用户必须设置密码。")

        if current["role"] == "admin" and actor_role != "admin":
            raise ValueError("普通用户不能修改管理员账号。")

        try:
            tracking_number = _normalize_tracking_number(data.get("tracking_number", current["tracking_number"]))
            previous_tracking_number = _normalize_tracking_number(current["tracking_number"])
            conn.execute(
                """
                UPDATE accounts
                SET username = ?, display_name = ?, password_hash = ?, role = ?, note = ?,
                    tracking_number = ?, check_interval = ?, bark_keys = ?, bark_query_params = ?,
                    bark_url_enabled = ?, login_enabled = ?, tracking_enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    next_username,
                    display_name,
                    password_hash,
                    next_role,
                    str(data.get("note", current["note"])).strip(),
                    tracking_number,
                    _normalize_check_interval(data.get("check_interval", current["check_interval"])),
                    normalize_bark_keys(data.get("bark_keys") or data.get("bark_key", current["bark_keys"])),
                    str(data.get("bark_query_params", current["bark_query_params"])).strip()
                    or PROFILE_DEFAULTS["bark_query_params"],
                    _normalize_bool(data.get("bark_url_enabled", current["bark_url_enabled"]), default=current["bark_url_enabled"]),
                    next_login_enabled,
                    next_tracking_enabled,
                    _ts(),
                    account_id,
                ),
            )
            if tracking_number and tracking_number != previous_tracking_number:
                _record_tracking_number(conn, account_id, tracking_number)
        except sqlite3.IntegrityError as exc:
            raise ValueError("用户名已存在。") from exc

        conn.commit()
    return get_account(account_id)


def update_tracking_state(account_id: int, *, latest_info: str | None = None, error: str | None = None, pushed: bool = False):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            return None

        current = dict(row)
        checked_at = _ts()
        next_info = current["last_tracking_info"] if latest_info is None else str(latest_info)
        next_error = current["last_error"] if error is None else str(error)
        next_push_at = checked_at if pushed else current["last_push_at"]
        conn.execute(
            """
            UPDATE accounts
            SET last_tracking_info = ?, last_checked_at = ?, last_error = ?, last_push_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (next_info, checked_at, next_error, next_push_at, checked_at, account_id),
        )
        conn.commit()
    return get_account(account_id)

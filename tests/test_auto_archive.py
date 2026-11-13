"""签收后自动归档：签收判定、tracker 流程（推送成功 / 失败 / 无 Keys / 开关）、机器接口的 delivered 语义、迁移。"""
import sqlite3

import pytest

import storage
from test_task_events import BARK_ENV, R1, R2, R3, R4, Net, info, page_html

NUM = "AB123456789JP"
DELIVERED = R4  # お届け先にお届け済み


@pytest.fixture
def tracker(db):
    import tracker as tracker_module

    return tracker_module


@pytest.fixture
def owner(db):
    return db.create_account({"username": "ella", "password": "pw-ella", "role": "user", "bark_keys": "key-1"})


@pytest.fixture
def task(db, owner):
    return db.create_task(owner["id"], {"tracking_number": NUM})


@pytest.fixture
def net(tracker, monkeypatch):
    return Net(tracker, monkeypatch)


def due_ids():
    return {t["id"] for t in storage.list_due_tasks()}


def run_once(tracker, task_id, env=None):
    due = next(t for t in storage.list_due_tasks() if t["id"] == task_id)
    return tracker.process_task(due, env if env is not None else BARK_ENV)


def kinds(task_id):
    events, _ = storage.list_task_events(task_id, limit=100)
    return [(e["kind"], e["status"]) for e in reversed(events)]


def ref_state(task_id):
    with sqlite3.connect(storage.DB_PATH) as conn:
        return [r[0] for r in conn.execute("SELECT state FROM api_refs WHERE task_id = ?", (task_id,))]


# --- 签收判定 ---

@pytest.mark.parametrize("value", [
    "2026/10/27 13:50 お届け先にお届け済み",
    "2026/10/27 13:50 お届け済み",
    "2026/1/7 9:05 窓口でお渡し",
    "お届け先にお届け済み",
    "  2026/10/27 13:50   お届け済み  ",
])
def test_is_delivered_positive(tracker, value):
    assert tracker.is_delivered(value) is True


@pytest.mark.parametrize("value", [
    None,
    "",
    "   ",
    "2026/10/27 13:50",
    "2026/10/27 13:50 お届け先にお伺いしましたがご不在でした",
    "2026/10/27 13:50 ご不在のため持ち戻り",
    "2026/10/27 13:50 お届け予定",
    "2026/10/27 13:50 お届け済みではありません",
    "2026/10/27 13:50 引受",
    "2026/10/27 13:50 持ち出し中",
    "2026/10/27 13:50 窓口でお渡し予定",
    "2026/10/27 13:50 差出人に返送済み",
])
def test_is_delivered_negative(tracker, value):
    assert tracker.is_delivered(value) is False


def test_tracking_status_strips_datetime(tracker):
    assert tracker.tracking_status("2026/10/27 13:50 お届け済み") == "お届け済み"
    assert tracker.tracking_status(None) == ""


# --- tracker 流程 ---

def test_delivered_push_ok_archives_after_push(tracker, task, net):
    net.page = page_html(R1)
    run_once(tracker, task["id"])
    net.page = page_html(R1, R2, DELIVERED)
    assert run_once(tracker, task["id"]) is False

    current = storage.get_task(task["id"])
    assert current["archived"] and not current["enabled"]
    assert current["last_tracking_info"] == info(DELIVERED)
    assert kinds(task["id"])[2:] == [
        ("status_change", info(R2)),
        ("status_change", info(DELIVERED)),
        ("push_ok", "已妥投（東京）"),
        ("auto_archived", info(DELIVERED)),
    ]
    assert len(net.pushes) == 2
    events, _ = storage.list_task_events(task["id"])
    assert events[0]["kind_label"] == "签收自动归档"
    # 已归档的任务不再出现在待轮询列表里，不会被再次处理
    assert task["id"] not in due_ids()


def test_delivered_in_new_records_counts(tracker, task, net):
    """最新一条不是签收、但本轮新增记录里出现过签收，也归档。"""
    net.page = page_html(R1, DELIVERED, ("2026/10/02 15:00", "お問い合わせ番号が見つかりません", ""))
    run_once(tracker, task["id"])
    assert storage.get_task(task["id"])["archived"]


def test_delivered_push_fail_keeps_task_until_push_succeeds(tracker, task, net):
    net.page = page_html(R1, DELIVERED)
    net.push_ok = False
    assert run_once(tracker, task["id"]) is True
    assert run_once(tracker, task["id"]) is True
    current = storage.get_task(task["id"])
    assert not current["archived"] and current["enabled"]
    assert task["id"] in due_ids()

    net.push_ok = True
    assert run_once(tracker, task["id"]) is False
    assert storage.get_task(task["id"])["archived"]
    assert kinds(task["id"]) == [
        ("status_change", info(R1)),
        ("status_change", info(DELIVERED)),
        ("push_fail", "已妥投（東京）"),
        ("push_ok", "已妥投（東京）"),
        ("auto_archived", info(DELIVERED)),
    ]


def test_delivered_without_bark_keys_archives_directly(tracker, db, owner, task, net):
    db.update_account(owner["id"], {"bark_keys": ""})
    net.page = page_html(R1, DELIVERED)
    assert run_once(tracker, task["id"]) is False
    assert net.pushes == []
    assert storage.get_task(task["id"])["archived"]
    assert kinds(task["id"]) == [
        ("status_change", info(R1)),
        ("status_change", info(DELIVERED)),
        ("auto_archived", info(DELIVERED)),
    ]


def test_not_delivered_status_is_not_archived(tracker, task, net):
    net.page = page_html(R1, ("2026/10/02 14:00", "ご不在のため持ち戻り", ""))
    run_once(tracker, task["id"])
    net.page = page_html(R1, ("2026/10/02 14:00", "ご不在のため持ち戻り", ""), ("2026/10/03 09:00", "お届け予定", ""))
    run_once(tracker, task["id"])
    assert not storage.get_task(task["id"])["archived"]
    assert "auto_archived" not in [k for k, _ in kinds(task["id"])]


def test_switch_off_keeps_polling_and_switch_on_catches_up(tracker, task, net):
    off = {**BARK_ENV, "AUTO_ARCHIVE_DELIVERED": "0"}
    net.page = page_html(R1, DELIVERED)
    run_once(tracker, task["id"], off)
    run_once(tracker, task["id"], off)
    current = storage.get_task(task["id"])
    assert not current["archived"] and current["enabled"]
    assert task["id"] in due_ids()

    # 开关打开后：无新记录也补归档一次，不重复推送
    run_once(tracker, task["id"], {**BARK_ENV, "AUTO_ARCHIVE_DELIVERED": "1"})
    assert storage.get_task(task["id"])["archived"]
    assert len(net.pushes) == 1
    assert kinds(task["id"])[-1] == ("auto_archived", info(DELIVERED))


def test_switch_default_is_on(tracker):
    assert tracker.auto_archive_enabled({}) is True
    assert tracker.auto_archive_enabled({"AUTO_ARCHIVE_DELIVERED": ""}) is True
    assert tracker.auto_archive_enabled({"AUTO_ARCHIVE_DELIVERED": "off"}) is False
    assert "AUTO_ARCHIVE_DELIVERED" in tracker.TRACKER_ENV_KEYS


def test_user_unarchive_after_auto_archive_is_respected(tracker, db, task, net):
    net.page = page_html(R1, DELIVERED)
    run_once(tracker, task["id"])
    db.update_task(task["id"], {"archived": 0, "enabled": 1})
    run_once(tracker, task["id"])
    assert not storage.get_task(task["id"])["archived"]
    assert [k for k, _ in kinds(task["id"])].count("auto_archived") == 1


def test_auto_archive_skips_already_archived(db, task):
    db.archive_task(task["id"])
    db.update_task_state(task["id"], latest_info="x", tracking_number=NUM, auto_archive=True)
    assert kinds(task["id"]) == []


# --- 机器接口 ---

@pytest.fixture
def token(db, owner):
    _, value = db.issue_api_caller("caller-a", "ella")
    return value


def bearer(value):
    return {"Authorization": f"Bearer {value}"}


def test_api_task_delivered_semantics(client, tracker, db, owner, token, net):
    h = bearer(token)
    resp = client.put(f"/api/v1/tracking/{NUM}", json={"external_ref": "r1"}, headers=h)
    assert resp.status_code == 201
    task_id = next(t["id"] for t in db.list_tasks(owner["id"]) if t["tracking_number"] == NUM)

    net.page = page_html(R1, R2, R3, DELIVERED)
    run_once(tracker, task_id)
    assert storage.get_task(task_id)["archived"]
    assert ref_state(task_id) == ["delivered_archived"]

    # PUT 不复活，200 delivered
    resp = client.put(f"/api/v1/tracking/{NUM}", headers=h)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["result"] == "delivered"
    assert body["tracking"]["archived"] is True and body["tracking"]["enabled"] is False
    assert body["tracking"]["source_state"] == "delivered_archived"
    assert body["tracking"]["external_ref"] == "r1"
    assert storage.get_task(task_id)["archived"]

    # GET 正常返回，时间线里能看到 auto_archived
    body = client.get(f"/api/v1/tracking/{NUM}", headers=h).get_json()
    assert body["tracking"]["archived"] is True and body["tracking"]["source_state"] == "delivered_archived"
    assert body["events"][0]["kind"] == "auto_archived"

    # archive 幂等：already_archived，来源状态保持 delivered_archived
    resp = client.post(f"/api/v1/tracking/{NUM}/archive", headers=h)
    assert resp.status_code == 200
    assert resp.get_json()["result"] == "already_archived"
    assert resp.get_json()["tracking"]["source_state"] == "delivered_archived"
    assert ref_state(task_id) == ["delivered_archived"]

    # 带 revive 才复活；复活后不会因为最新记录仍是签收而被立刻再次归档
    resp = client.put(f"/api/v1/tracking/{NUM}", json={"revive": True}, headers=h)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["result"] == "revived" and body["tracking"]["archived"] is False
    assert body["tracking"]["source_state"] == "active"
    run_once(tracker, task_id)
    assert not storage.get_task(task_id)["archived"]


def test_web_unarchive_returns_ref_to_active(client, tracker, db, owner, token, net):
    client.put(f"/api/v1/tracking/{NUM}", headers=bearer(token))
    task_id = next(t["id"] for t in db.list_tasks(owner["id"]) if t["tracking_number"] == NUM)
    net.page = page_html(R1, DELIVERED)
    run_once(tracker, task_id)
    assert ref_state(task_id) == ["delivered_archived"]

    client.post("/login", data={"username": "ella", "password": "pw-ella"})
    resp = client.put(f"/api/tasks/{task_id}", json={"archived": False, "enabled": True})
    assert resp.status_code == 200
    assert ref_state(task_id) == ["active"]
    # 用户之后手动归档：照旧是 user_archived，PUT 409
    client.post(f"/api/tasks/{task_id}/archive")
    assert ref_state(task_id) == ["user_archived"]
    resp = client.put(f"/api/v1/tracking/{NUM}", headers=bearer(token))
    assert resp.status_code == 409 and resp.get_json()["error"]["code"] == "user_archived"


def test_web_created_task_auto_archived_then_api_put(client, tracker, db, owner, token, task, net):
    net.page = page_html(R1, DELIVERED)
    run_once(tracker, task["id"])
    assert storage.get_task(task["id"])["archived"]

    resp = client.put(f"/api/v1/tracking/{NUM}", json={"external_ref": "late"}, headers=bearer(token))
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["result"] == "delivered" and body["tracking"]["archived"] is True
    assert body["tracking"]["source_state"] == "delivered_archived"
    assert body["tracking"]["external_ref"] == "late"


def test_user_archive_of_web_task_still_conflicts(client, db, owner, token, task):
    db.archive_task(task["id"])
    resp = client.put(f"/api/v1/tracking/{NUM}", headers=bearer(token))
    assert resp.status_code == 409 and resp.get_json()["error"]["code"] == "user_archived"


# --- 迁移 ---

OLD_STATES = "'active', 'archived_by_api', 'user_archived', 'user_deleted'"
OLD_UNARCHIVE = """
        CREATE TRIGGER IF NOT EXISTS trg_api_refs_task_unarchive
        AFTER UPDATE OF archived ON tracking_tasks
        WHEN OLD.archived = 1 AND NEW.archived = 0
        BEGIN
            UPDATE api_refs SET state = 'active', updated_at = strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')
            WHERE task_id = NEW.id AND state IN ('user_archived', 'archived_by_api');
        END
"""


def _downgrade_to_old_schema(db_path):
    """把 api_refs 与取消归档触发器改回上线前的定义（CHECK 只有四种状态）。"""
    with sqlite3.connect(db_path) as conn:
        for name in storage._API_REF_TRIGGERS:
            conn.execute(f"DROP TRIGGER {name}")
        conn.execute(storage._API_REFS_TABLE_SQL.format(name="api_refs_old").replace(storage._API_REF_STATE_SQL, OLD_STATES))
        conn.execute(f"INSERT INTO api_refs_old SELECT {storage._API_REFS_COLUMNS} FROM api_refs")
        conn.execute("DROP TABLE api_refs")
        conn.execute("ALTER TABLE api_refs_old RENAME TO api_refs")
        conn.execute("CREATE INDEX idx_api_refs_task ON api_refs(task_id)")
        for name, sql in storage._API_REF_TRIGGERS.items():
            conn.execute(OLD_UNARCHIVE if name == "trg_api_refs_task_unarchive" else sql)


def test_existing_db_upgrade_updates_constraint_and_trigger(client, db, owner, token, task):
    client.put(f"/api/v1/tracking/{NUM}", json={"external_ref": "keep"}, headers=bearer(token))
    db.update_task_state(task["id"], latest_info="s1", pushed=True, push_status="s1")
    _downgrade_to_old_schema(storage.DB_PATH)
    with sqlite3.connect(storage.DB_PATH) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE api_refs SET state = 'delivered_archived'")
        before_tasks = conn.execute("SELECT * FROM tracking_tasks ORDER BY id").fetchall()
        before_events = conn.execute("SELECT * FROM task_events ORDER BY id").fetchall()
        before_refs = conn.execute("SELECT * FROM api_refs ORDER BY id").fetchall()
    assert before_refs

    storage.ensure_storage("/nonexistent/.env")
    storage.ensure_storage("/nonexistent/.env")  # 幂等

    with sqlite3.connect(storage.DB_PATH) as conn:
        assert conn.execute("SELECT * FROM tracking_tasks ORDER BY id").fetchall() == before_tasks
        assert conn.execute("SELECT * FROM task_events ORDER BY id").fetchall() == before_events
        assert conn.execute("SELECT * FROM api_refs ORDER BY id").fetchall() == before_refs
        table_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'api_refs'").fetchone()[0]
        trigger_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'trg_api_refs_task_unarchive'"
        ).fetchone()[0]
        triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(api_refs)")}
        assert {r[2] for r in conn.execute("PRAGMA foreign_key_list(api_refs)")} == {"api_callers", "accounts"}
    assert "'delivered_archived'" in table_sql
    assert "delivered_archived" in trigger_sql
    assert set(storage._API_REF_TRIGGERS) <= triggers
    assert "idx_api_refs_task" in indexes

    # 升级后的库上：自动归档写 delivered_archived，取消归档回到 active
    db.update_task_state(task["id"], latest_info=info(DELIVERED), tracking_number=NUM, auto_archive=True)
    assert ref_state(task["id"]) == ["delivered_archived"]
    db.update_task(task["id"], {"archived": 0})
    assert ref_state(task["id"]) == ["active"]

"""任务时间线：事件写入（状态变化 / 推送 / 查询出错切换）、防串号、保留上限、接口分页与权限、迁移。"""
import os
import re
import sqlite3

import pytest

import storage
from conftest import ADMIN_PASSWORD, gate

BARK_ENV = {"BARK_SERVER": "http://bark.example.com"}


# --- 夹具与工具 ---

def page_html(*records):
    """仿日本邮政履历表：每条记录两行，第一行带日期、状态、详情、取扱局，第二行是邮编。"""
    rows = "".join(
        f'<tr><td class="w_120" rowspan="2">{date}</td><td class="w_150" rowspan="2">{status}</td>'
        f'<td class="w_180">{detail}</td><td class="w_105">東京</td></tr><tr><td>1000001</td></tr>'
        for date, status, detail in records
    )
    return f'<html><body><table class="tableType01 txt_c m_b5" summary="履歴情報">{rows}</table></body></html>'


R1 = ("2026/10/01 10:00", "引受", "")
R2 = ("2026/10/01 18:00", "通過", "")
R3 = ("2026/10/02 09:00", "到着", "")
R4 = ("2026/10/02 14:00", "お届け先にお届け済み", "")


def info(record):
    return f"{record[0]} {record[1]}"


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")


class Net:
    """可控的假网络：page 为 None 时抓取失败；push_ok 控制 Bark 推送结果。"""

    def __init__(self, tracker, monkeypatch):
        self.page = None
        self.push_ok = True
        self.pushes = []
        self.on_fetch = None
        monkeypatch.setattr(tracker.requests, "get", self._get)
        monkeypatch.setattr(tracker, "send_bark_notification", self._push)

    def _get(self, url, **kwargs):
        if self.on_fetch:
            self.on_fetch()
        if self.page is None:
            import requests

            raise requests.exceptions.ConnectionError("连接被拒绝")
        return FakeResponse(self.page)

    def _push(self, config, title, body):
        self.pushes.append(body)
        return (True, "") if self.push_ok else (False, "HTTP 500")


@pytest.fixture
def tracker(db):
    import tracker as tracker_module

    return tracker_module


@pytest.fixture
def owner(db):
    return db.create_account({"username": "ella", "password": "pw-ella", "role": "user", "bark_keys": "key-1"})


@pytest.fixture
def task(db, owner):
    return db.create_task(owner["id"], {"tracking_number": "AB123456789JP"})


def run_once(tracker, task_id):
    due = next(t for t in storage.list_due_tasks() if t["id"] == task_id)
    return tracker.process_task(due, BARK_ENV)


def kinds(task_id):
    events, _ = storage.list_task_events(task_id, limit=100)
    return [(e["kind"], e["status"]) for e in reversed(events)]


# --- 解析 ---

def test_parse_records_keeps_every_row_in_order(tracker):
    records = tracker.parse_tracking_records(page_html(R1, R2, ("2026/10/02 09:00", "到着", "窓口")))
    assert [r["info"] for r in records] == [info(R1), info(R2), "2026/10/02 09:00 到着"]
    assert records[2]["detail"] == "窓口 · 東京"
    assert tracker.parse_tracking_records("<html></html>") == []


def test_records_since(tracker):
    records = tracker.parse_tracking_records(page_html(R1, R2, R3))
    assert [r["info"] for r in tracker.records_since(records, info(R1))] == [info(R2), info(R3)]
    assert len(tracker.records_since(records, "")) == 3  # 首次抓取：全部历史
    assert [r["info"] for r in tracker.records_since(records, "不在页面上")] == [info(R3)]
    assert tracker.records_since(records, info(R3)) == []


# --- tracker 写事件 ---

def test_status_change_records_intermediate_states_and_push_ok(tracker, task, monkeypatch):
    net = Net(tracker, monkeypatch)
    net.page = page_html(R1)
    assert run_once(tracker, task["id"]) is False
    assert kinds(task["id"]) == [("status_change", info(R1)), ("push_ok", info(R1))]

    # 两次轮询之间走过两个状态：中间那条也要记下来，推送只推最新一条
    net.page = page_html(R1, R2, R3)
    run_once(tracker, task["id"])
    assert kinds(task["id"])[2:] == [
        ("status_change", info(R2)),
        ("status_change", info(R3)),
        ("push_ok", info(R3)),
    ]
    assert len(net.pushes) == 2

    # 无变化：不写事件
    run_once(tracker, task["id"])
    assert len(kinds(task["id"])) == 5
    assert storage.get_task(task["id"])["last_tracking_info"] == info(R3)


def test_push_failure_logged_once_and_status_not_duplicated_on_retry(tracker, task, monkeypatch):
    net = Net(tracker, monkeypatch)
    net.page = page_html(R1)
    net.push_ok = False
    assert run_once(tracker, task["id"]) is True
    assert run_once(tracker, task["id"]) is True
    assert run_once(tracker, task["id"]) is True
    assert kinds(task["id"]) == [("status_change", info(R1)), ("push_fail", info(R1))]
    events, _ = storage.list_task_events(task["id"])
    assert events[0]["detail"] == "HTTP 500"
    assert storage.get_task(task["id"])["last_tracking_info"] == ""

    net.push_ok = True
    assert run_once(tracker, task["id"]) is False
    assert kinds(task["id"]) == [
        ("status_change", info(R1)),
        ("push_fail", info(R1)),
        ("push_ok", info(R1)),
    ]

    # 新状态再次推送失败：要重新记一条失败
    net.page = page_html(R1, R2)
    net.push_ok = False
    run_once(tracker, task["id"])
    assert kinds(task["id"])[-2:] == [("status_change", info(R2)), ("push_fail", info(R2))]


def test_query_error_only_logged_on_transitions(tracker, task, monkeypatch):
    net = Net(tracker, monkeypatch)
    net.page = page_html(R1)
    run_once(tracker, task["id"])
    base = len(kinds(task["id"]))

    net.page = None
    for _ in range(3):
        run_once(tracker, task["id"])
    assert [k for k, _ in kinds(task["id"])[base:]] == ["query_error"]
    assert storage.get_task(task["id"])["last_error"] == "无法获取最新快递信息。"

    net.page = page_html(R1)
    run_once(tracker, task["id"])
    run_once(tracker, task["id"])
    assert [k for k, _ in kinds(task["id"])[base:]] == ["query_error", "query_recovered"]
    assert storage.get_task(task["id"])["last_error"] == ""


def test_missing_config_counts_as_query_error(tracker, db, owner, task, monkeypatch):
    Net(tracker, monkeypatch)
    db.update_account(owner["id"], {"bark_keys": ""})
    run_once(tracker, task["id"])
    run_once(tracker, task["id"])
    assert [k for k, _ in kinds(task["id"])] == ["query_error"]
    assert "Bark Keys" in storage.list_task_events(task["id"])[0][0]["detail"]


def test_number_changed_during_fetch_is_discarded(tracker, db, task, monkeypatch):
    """抓旧单号期间用户把单号改了：旧结果不能写到新单号头上，也不记事件。"""
    net = Net(tracker, monkeypatch)
    net.page = page_html(R1)
    net.on_fetch = lambda: db.update_task(task["id"], {"tracking_number": "CD987654321JP"})
    run_once(tracker, task["id"])
    fresh = storage.get_task(task["id"])
    assert fresh["tracking_number"] == "CD987654321JP"
    assert fresh["last_tracking_info"] == "" and fresh["last_checked_at"] == ""
    assert kinds(task["id"]) == []


def test_update_task_state_guard_and_event_number(db, task):
    assert storage.update_task_state(task["id"], latest_info="x", tracking_number="OTHER") is None
    assert storage.get_task(task["id"])["last_tracking_info"] == ""
    storage.update_task_state(
        task["id"], latest_info=info(R1), tracking_number=task["tracking_number"],
        new_records=[{"info": info(R1), "detail": ""}],
    )
    events, _ = storage.list_task_events(task["id"])
    assert events[0]["tracking_number"] == task["tracking_number"]
    assert events[0]["account_id"] == task["account_id"]


def test_unknown_kind_is_rejected(db, task):
    from contextlib import closing

    with closing(storage._connect()) as conn:
        row = conn.execute("SELECT * FROM tracking_tasks WHERE id = ?", (task["id"],)).fetchone()
        with pytest.raises(ValueError):
            storage._insert_event(conn, row, "something_else")


# --- 保留策略 ---

def _push_many(task, count):
    for i in range(count):
        storage.update_task_state(
            task["id"], tracking_number=task["tracking_number"],
            new_records=[{"info": f"状态 {i}", "detail": ""}],
        )


def test_retention_cap_per_task(db, task, monkeypatch):
    monkeypatch.setattr(storage, "EVENT_MAX_PER_TASK", 5)
    _push_many(task, 8)
    events, has_more = storage.list_task_events(task["id"], limit=100)
    assert [e["status"] for e in events] == [f"状态 {i}" for i in range(7, 2, -1)]
    assert not has_more


def test_retention_drops_old_events(db, task):
    with sqlite3.connect(storage.DB_PATH) as conn:
        conn.execute(
            "INSERT INTO task_events (task_id, account_id, tracking_number, kind, created_at) VALUES (?, ?, ?, 'push_ok', ?)",
            (task["id"], task["account_id"], task["tracking_number"], "2020-01-01 00:00:00"),
        )
    _push_many(task, 1)
    assert [e["status"] for e in storage.list_task_events(task["id"])[0]] == ["状态 0"]


def test_events_cascade_with_task_delete(db, task):
    _push_many(task, 2)
    storage.delete_task(task["id"])
    with sqlite3.connect(storage.DB_PATH) as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 0


# --- 接口：分页与权限 ---

def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password})


def test_events_api_pagination(client, db, task):
    _push_many(task, 5)
    login(client, "ella", "pw-ella")
    first = client.get(f"/api/tasks/{task['id']}/events?limit=2").get_json()
    assert [e["status"] for e in first["events"]] == ["状态 4", "状态 3"]
    assert first["has_more"] and first["next_before"] == first["events"][-1]["id"]
    assert first["events"][0]["kind"] == "status_change"

    second = client.get(f"/api/tasks/{task['id']}/events?limit=2&before={first['next_before']}").get_json()
    assert [e["status"] for e in second["events"]] == ["状态 2", "状态 1"]
    third = client.get(f"/api/tasks/{task['id']}/events?limit=2&before={second['next_before']}").get_json()
    assert [e["status"] for e in third["events"]] == ["状态 0"]
    assert not third["has_more"] and third["next_before"] is None

    assert client.get(f"/api/tasks/{task['id']}/events?limit=abc").status_code == 400
    assert client.get(f"/api/tasks/{task['id']}/events?before=-1").status_code == 400
    capped = client.get(f"/api/tasks/{task['id']}/events?limit=100000").get_json()
    assert len(capped["events"]) == 5


def test_events_api_permissions(client, db, task):
    _push_many(task, 1)
    url = f"/api/tasks/{task['id']}/events"
    assert client.get(url).status_code == 401

    db.create_account({"username": "frank", "password": "pw-frank", "role": "user"})
    login(client, "frank", "pw-frank")
    assert client.get(url).status_code == 403
    assert client.get("/api/tasks/99999/events").status_code == 404
    client.post("/logout")

    login(client, "admin", ADMIN_PASSWORD)
    assert len(client.get(url).get_json()["events"]) == 1


def test_events_api_under_sso(client, db, sso, task, owner):
    _push_many(task, 1)
    db.set_account_portal_sub(owner["id"], "sub-ella")
    url = f"/api/tasks/{task['id']}/events"
    assert client.get(url).status_code == 401
    assert client.get(url, headers=gate("ella")).status_code == 200
    other = db.create_account({"username": "gina", "password": "pw", "role": "user"})
    db.set_account_portal_sub(other["id"], "sub-gina")
    assert client.get(url, headers=gate("gina")).status_code == 403
    db.set_account_portal_sub(2, "sub-carol")
    assert client.get(url, headers=gate("carol", "admin")).status_code == 200


# --- 页面 ---

def test_portal_renders_recent_events_with_load_more(client, db, task):
    _push_many(task, 7)
    storage.update_task_state(
        task["id"], tracking_number=task["tracking_number"], push_status="状态 6", push_failed="HTTP 500"
    )
    login(client, "ella", "pw-ella")
    html = client.get("/").data.decode()
    assert f'data-task-timeline="{task["id"]}"' in html
    assert html.count('class="timeline-row') == 5  # 只渲染最近几条
    assert "timeline-row is-fail" in html and "推送失败" in html
    assert "data-timeline-more" in html and "/static/timeline.js" in html


def test_portal_empty_timeline(client, db, task):
    login(client, "ella", "pw-ella")
    html = client.get("/").data.decode()
    assert "还没有记录" in html and "data-timeline-more" not in html


def test_admin_page_has_timeline_component(client, db):
    login(client, "admin", ADMIN_PASSWORD)
    html = client.get("/").data.decode()
    assert "<task-timeline" in html


def test_js_kind_map_matches_python():
    """前端映射必须穷举后端的每一种事件类型，文案与配色一致，不多不少。"""
    path = os.path.join(os.path.dirname(storage.__file__), "static", "timeline.js")
    source = open(path, encoding="utf-8").read()
    block = re.search(r"EVENT_KIND_META = Object\.freeze\(\{(.*?)\}\);", source, re.S).group(1)
    js_map = {
        kind: {"label": label, "tone": tone}
        for kind, label, tone in re.findall(r"(\w+): \{ label: '([^']+)', tone: '(\w+)' \}", block)
    }
    assert js_map == storage.EVENT_KIND_META
    assert storage.EVENT_KINDS == frozenset(js_map)


# --- 迁移 ---

def _foreign_targets(db_path, table):
    with sqlite3.connect(db_path) as conn:
        return {row[2] for row in conn.execute(f"PRAGMA foreign_key_list({table})")}


def test_migration_adds_events_table_to_existing_v2_db(tmp_path, monkeypatch):
    db_path = tmp_path / "v2.db"
    conn = sqlite3.connect(db_path)
    storage._ensure_accounts_schema(conn)
    storage._ensure_tasks_schema(conn)
    conn.execute(
        "INSERT INTO accounts (id, username, role, created_at, updated_at) VALUES (7, 'old', 'user', 'x', 'x')"
    )
    conn.execute(
        "INSERT INTO tracking_tasks (id, account_id, tracking_number, last_tracking_info, first_seen_at, created_at, updated_at)"
        " VALUES (3, 7, 'N1', '旧状态', 'x', 'x', 'x')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(storage, "DB_PATH", str(db_path))
    storage.ensure_storage(str(tmp_path / "missing.env"))
    storage.ensure_storage(str(tmp_path / "missing.env"))  # 幂等

    assert storage.get_task(3)["last_tracking_info"] == "旧状态"
    assert _foreign_targets(db_path, "task_events") == {"tracking_tasks"}
    with sqlite3.connect(db_path) as c:
        indexes = {r[1] for r in c.execute("PRAGMA index_list(task_events)")}
    assert "idx_task_events_task_time" in indexes
    storage.update_task_state(3, tracking_number="N1", new_records=[{"info": "新状态", "detail": ""}])
    assert storage.list_task_events(3)[0][0]["status"] == "新状态"


def test_migration_from_v1_keeps_tasks_and_events_fk(tmp_path, monkeypatch):
    db_path = tmp_path / "v1.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL DEFAULT '', password_hash TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT 'user', note TEXT NOT NULL DEFAULT '',
            bark_keys TEXT NOT NULL DEFAULT '', bark_query_params TEXT NOT NULL DEFAULT '',
            bark_url_enabled INTEGER NOT NULL DEFAULT 1, login_enabled INTEGER NOT NULL DEFAULT 1,
            tracking_number TEXT NOT NULL DEFAULT '', check_interval INTEGER NOT NULL DEFAULT 300,
            tracking_enabled INTEGER NOT NULL DEFAULT 1, last_tracking_info TEXT NOT NULL DEFAULT '',
            last_checked_at TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '',
            last_push_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO accounts (id, username, tracking_number, last_tracking_info, created_at, updated_at)"
        " VALUES (5, 'veteran', 'v1number', '老状态', 'x', 'x')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(storage, "DB_PATH", str(db_path))
    storage.ensure_storage(str(tmp_path / "missing.env"))

    tasks = storage.list_tasks(5)
    assert [(t["tracking_number"], t["last_tracking_info"]) for t in tasks] == [("V1NUMBER", "老状态")]
    assert _foreign_targets(db_path, "task_events") == {"tracking_tasks"}
    assert _foreign_targets(db_path, "tracking_tasks") == {"accounts"}
    storage.update_task_state(tasks[0]["id"], tracking_number="V1NUMBER", new_records=[{"info": "新", "detail": ""}])
    storage.delete_task(tasks[0]["id"])
    with sqlite3.connect(db_path) as c:
        assert c.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 0

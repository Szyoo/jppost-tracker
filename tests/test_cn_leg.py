"""始发国段（中国邮政，经 17TRACK）：客户端、事件时区、日本邮政始发国行的时区修正、carrier_records CHECK 迁移、
cn_ems 阶段映射、收寄网点与渠道推断、tracker 同步与补抓、补录、系统设置里的密钥。
网络请求一律替换成假的 17TRACK，绝不真的调用（按登记单号扣额度）。"""
import os
import sqlite3
from datetime import datetime, timedelta

import pytest

import forecast
import storage
import track17
from conftest import ADMIN_PASSWORD

NUM = "EB100000001CN"
NUM2 = "EB100000002CN"
NUM3 = "EB100000003CN"
US_NUM = "EB100000004US"
KEY = "test-track17-key"
ENV = {"TRACK17_API_KEY": KEY}

# 中国邮政 provider 的事件（17TRACK 原样倒序给出，单号虚构）
CN_EVENTS = [
    ("2026-11-13T07:17:00+08:00", "到达寄达地处理中心", ""),
    ("2026-11-12T18:27:00+08:00", "到达寄达地", ""),
    ("2026-11-12T15:44:00+08:00", "飞机进港", ""),
    ("2026-11-12T13:41:00+08:00", "航空公司启运", ""),
    ("2026-11-11T10:37:00+08:00", "飞机进港", ""),
    ("2026-11-10T21:45:06+08:00", "航空公司启运", ""),
    ("2026-11-10T15:55:20+08:00", "航空公司接收", ""),
    ("2026-11-10T13:46:24+08:00", "已交承运商运输", "威海市"),
    ("2026-11-10T13:41:32+08:00", "邮件离开【山东省威海市国际互换局】，正在发往下一站", "威海市"),
    ("2026-11-10T12:37:44+08:00", "送交出口海关", ""),
    ("2026-11-10T12:32:44+08:00", "【山东省威海市国际互换局】已出口直封", ""),
    ("2026-11-10T12:32:14+08:00", "出口海关/放行", ""),
    ("2026-11-10T07:45:48+08:00", "送交出口海关", ""),
    ("2026-11-09T18:54:19+08:00", "邮件离开【威海市丹东路第二揽投部】，正在发往【威海国际】", ""),
    ("2026-11-09T18:54:03+08:00", "邮件已在【威海市丹东路第二揽投部】完成分拣，准备发出", ""),
    ("2026-11-09T18:31:38+08:00", "中国邮政已收取邮件", ""),
]
JAPAN_SIDE_EVENTS = [
    ("2026-11-14T10:05:00+08:00", "【日本】已妥投", ""),
    ("2026-11-13T09:00:00+08:00", "境外进口海关留存待验", ""),
]


def dt(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M")


def track_info(events, country="CN"):
    return {"tracking": {"providers": [{
        "provider": {"name": "China Post", "country": country},
        "events": [{"time_iso": t, "description": d, "location": loc} for t, d, loc in events],
    }]}}


class Fake17:
    """假的 17TRACK：registered 已登记的单号；ready 有轨迹的单号 → track_info；fail 为真时所有请求抛连接错误。"""

    def __init__(self, monkeypatch):
        self.registered = set()
        self.ready = {}
        self.fail = False
        self.calls = []
        monkeypatch.setattr(track17.requests, "post", self.post)

    def post(self, url, headers=None, json=None, timeout=None):
        import requests

        path = url.rsplit("/", 1)[-1]
        self.calls.append((path, [item["number"] for item in (json or [])]))
        assert headers["17token"] == KEY and timeout
        if self.fail:
            raise requests.exceptions.ConnectionError("连接超时")
        if path == "register":
            accepted, rejected = [], []
            for item in json:
                if item["number"] in self.registered:
                    rejected.append({"number": item["number"], "error": {"code": -18019901, "message": "The tracking number is already registered."}})
                elif item["number"].startswith("BAD"):
                    rejected.append({"number": item["number"], "error": {"code": -18010012, "message": "Invalid number."}})
                else:
                    self.registered.add(item["number"])
                    accepted.append({"number": item["number"], "carrier": 3011})
            return Resp({"code": 0, "data": {"accepted": accepted, "rejected": rejected}})
        if path == "gettrackinfo":
            accepted, rejected = [], []
            for item in json:
                if item["number"] in self.ready:
                    accepted.append({"number": item["number"], "track_info": self.ready[item["number"]]})
                else:
                    rejected.append({"number": item["number"], "error": {"code": -18019909, "message": "No tracking information at this time."}})
            return Resp({"code": 0, "data": {"accepted": accepted, "rejected": rejected}})
        if path == "getquota":
            return Resp({"code": 0, "data": {"quota_total": 100, "quota_used": 3, "quota_remain": 97, "note": "x"}})
        raise AssertionError(path)

    def count(self, path):
        return sum(1 for name, _ in self.calls if name == path)


class Resp:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def json(self):
        return self.payload


@pytest.fixture
def fake17(monkeypatch):
    return Fake17(monkeypatch)


@pytest.fixture
def owner(db):
    return db.create_account({"username": "ella", "password": "pw-ella", "role": "user"})


def carrier_rows(source=None):
    with sqlite3.connect(storage.DB_PATH) as conn:
        query = "SELECT task_id, tracking_number, source, record_at, status, detail FROM carrier_records"
        if source:
            return conn.execute(query + " WHERE source = ? ORDER BY id", (source,)).fetchall()
        return conn.execute(query + " ORDER BY id").fetchall()


def cn_rows(events=CN_EVENTS):
    return track17._cn_events(track_info(events))


# --- 17TRACK 客户端 ---

def test_register_already_registered_counts_as_success(fake17):
    fake17.registered.add(NUM2)
    assert track17.register([NUM, NUM2, "BAD1"], KEY, 5) == {
        NUM: "", NUM2: "", "BAD1": "登记被拒（-18010012）：Invalid number.",
    }
    with pytest.raises(ValueError):
        track17.register([], KEY, 5)
    with pytest.raises(ValueError):
        track17.register([f"N{i}" for i in range(41)], KEY, 5)


def test_gettrackinfo_rejected_is_not_ready_and_only_cn_provider(fake17):
    fake17.ready[NUM] = {"tracking": {"providers": [
        {"provider": {"name": "Japan Post", "country": "JP"}, "events": [{"time_iso": "2026-11-14T09:00:00+09:00", "description": "x"}]},
        *track_info(CN_EVENTS[-2:])["tracking"]["providers"],
    ]}}
    fake17.ready[NUM3] = {"tracking": {"providers": []}}
    result = track17.get_cn_events([NUM, NUM2, NUM3], KEY, 5)
    assert result[NUM2] is None  # 进了 rejected：未就绪
    assert result[NUM3] == []
    assert [row["status"] for row in result[NUM]] == ["中国邮政已收取邮件", "邮件已在【威海市丹东路第二揽投部】完成分拣，准备发出"]


def test_client_errors_raise_track17_error(fake17, monkeypatch):
    fake17.fail = True
    with pytest.raises(track17.Track17Error):
        track17.register([NUM], KEY, 5)
    fake17.fail = False
    monkeypatch.setattr(track17.requests, "post", lambda *a, **k: Resp({"code": -1, "data": None}))
    with pytest.raises(track17.Track17Error):
        track17.get_cn_events([NUM], KEY, 5)
    monkeypatch.setattr(track17.requests, "post", lambda *a, **k: Resp({}, status=401))
    with pytest.raises(track17.Track17Error):
        track17.get_quota(KEY, 5)


# --- 时区 ---

def test_track17_event_time_two_kinds():
    t = forecast.track17_event_time
    # 境内与航班事件：+08:00 是对的，换算成日本时间
    assert t("2026-11-09T18:31:38+08:00", "中国邮政已收取邮件") == "2026-11-09 19:31"
    assert t("2026-11-10T21:45:06+08:00", "航空公司启运") == "2026-11-10 22:45"
    assert t("2026-11-12T15:44:00+08:00", "飞机进港") == "2026-11-12 16:44"
    # 到达寄达地及之后：其实是日本时间却标成 +08:00，取字面时刻
    assert t("2026-11-12T18:27:00+08:00", "到达寄达地") == "2026-11-12 18:27"
    assert t("2026-10-25T22:53:00+08:00", "到达寄达地处理中心") == "2026-10-25 22:53"
    assert t("2026-11-13T09:00:00+08:00", "境外进口海关留存待验") == "2026-11-13 09:00"
    assert t("2026-11-14T10:05:00+08:00", "【日本】已妥投") == "2026-11-14 10:05"
    assert t("2026-11-13T09:00:00+08:00", "寄达地海关允许进口") == "2026-11-13 09:00"
    # 没有时区按中国时间；不合法返回 None
    assert t("2026-11-09T18:31:38", "中国邮政已收取邮件") == "2026-11-09 19:31"
    assert t("", "x") is None and t("昨天", "x") is None


def test_jp_post_origin_rows_converted_by_country_code():
    records = [
        {"source": "jp_post", "record_at": "2026-10-18 11:11", "status": "引受", "detail": "CHINA"},
        {"source": "jp_post", "record_at": "2026-10-19 08:22", "status": "国際交換局に到着", "detail": "WEIHAI D EMS · CHINA"},
        {"source": "jp_post", "record_at": "2026-10-21", "status": "国際交換局から発送", "detail": "CHINA"},  # 只有日期：不换算
        {"source": "jp_post", "record_at": "2026-10-25 22:53", "status": "国際交換局に到着", "detail": "東京国際郵便局 · 東京都"},
    ]
    stages = forecast.stages_from_records(records, tracking_number=NUM)
    assert stages["accepted"] == dt("2026-10-18 12:11")  # 中国时间 11:11 = 日本 12:11
    assert stages["origin_exchange"] == dt("2026-10-19 09:22")
    assert stages["departed_origin"] == dt("2026-10-21 12:00")
    assert stages["arrived_japan"] == dt("2026-10-25 22:53")  # 日本境内不动
    # 没有时区映射的国家码按日本时间
    assert forecast.stages_from_records(records, tracking_number=US_NUM)["accepted"] == dt("2026-10-18 11:11")
    assert forecast.origin_country(NUM) == "CN" and forecast.origin_country("12345") == ""


def test_task_forecast_applies_origin_timezone_without_touching_db(db, owner):
    task = db.create_task(owner["id"], {"tracking_number": NUM})
    db.record_carrier_records(task["id"], NUM, [{"info": "2026/10/18 11:11 引受", "detail": "CHINA"}])
    result = db.task_forecast(task["id"], now=dt("2026-10-18 13:00"))
    assert result["arrived"] == [{"stage": "accepted", "at": "2026-10-18 12:11"}]
    assert carrier_rows()[0][3] == "2026-10-18 11:11"  # 库里保留原值


# --- 阶段映射 ---

def test_cn_ems_stage_mapping():
    rows = [{"source": "cn_ems", **row} for row in cn_rows(CN_EVENTS + JAPAN_SIDE_EVENTS)]
    stages = forecast.stages_from_records(rows, tracking_number=NUM)
    assert stages == {
        "accepted": dt("2026-11-09 19:31"),
        "left_collection": dt("2026-11-09 19:54"),
        "origin_customs": dt("2026-11-10 08:45"),
        "departed_origin": dt("2026-11-10 13:32"),
        "handed_to_airline": dt("2026-11-10 14:46"),  # 已交承运商运输早于航空公司接收
        "flight_departed": dt("2026-11-10 22:45"),  # 第一条航空公司启运
        "transit_arrived": dt("2026-11-11 11:37"),  # 第一条飞机进港（中转地）
        "transit_departed": dt("2026-11-12 14:41"),  # 第二条航空公司启运
        "landed_destination": dt("2026-11-12 18:27"),
        "arrived_japan": dt("2026-11-13 07:17"),
    }
    order = [forecast.STAGE_ORDER[stage] for stage in sorted(stages, key=stages.get)]
    assert order == sorted(order)  # 新阶段插在正确的时间顺序上
    stage = forecast.record_stage
    for unmapped in ("飞机进港", "境外进口海关留存待验", "【日本】已妥投", "离开【日本】处理中心", "寄达地海关允许进口"):
        assert stage("cn_ems", unmapped) is None
    assert stage("cn_ems", "邮件已在【山东省威海市国际互换局】完成分拣，准备发出") is None


def test_cn_and_jp_same_stage_takes_earliest():
    rows = [{"source": "cn_ems", **row} for row in cn_rows()]
    rows.append({"source": "jp_post", "record_at": "2026-11-09 18:31", "status": "引受", "detail": "CHINA"})
    rows.append({"source": "jp_post", "record_at": "2026-11-13 07:20", "status": "国際交換局に到着", "detail": "東京国際郵便局 · 東京都"})
    stages = forecast.stages_from_records(rows, tracking_number=NUM)
    assert stages["accepted"] == dt("2026-11-09 19:31")  # 两边换算后同一时刻
    assert stages["arrived_japan"] == dt("2026-11-13 07:17")


def test_collection_office():
    rows = [{"source": "cn_ems", **row} for row in cn_rows()]
    assert forecast.collection_office(rows) == "威海市丹东路第二揽投部"
    only_exchange = [{"source": "cn_ems", **row} for row in cn_rows(CN_EVENTS[8:9])]
    assert forecast.collection_office(only_exchange) is None
    assert forecast.collection_office([]) is None


# --- carrier_records CHECK 迁移 ---

OLD_CARRIER_SQL = """
    CREATE TABLE carrier_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL,
        tracking_number TEXT NOT NULL,
        source TEXT NOT NULL CHECK(source IN ('jp_post')),
        record_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        UNIQUE(task_id, tracking_number, source, record_at, status, detail),
        FOREIGN KEY(task_id) REFERENCES tracking_tasks(id) ON DELETE CASCADE
    )
"""


def test_carrier_check_migration_keeps_data_and_is_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    storage._ensure_accounts_schema(conn)
    storage._ensure_tasks_schema(conn)
    storage._ensure_events_schema(conn)
    conn.execute("ALTER TABLE tracking_tasks ADD COLUMN channel TEXT NOT NULL DEFAULT ''")
    conn.execute(OLD_CARRIER_SQL)
    conn.execute("CREATE INDEX idx_carrier_records_number ON carrier_records(tracking_number)")
    conn.execute("INSERT INTO accounts (id, username, role, created_at, updated_at) VALUES (7, 'old', 'user', 'x', 'x')")
    conn.execute(
        "INSERT INTO tracking_tasks (id, account_id, tracking_number, first_seen_at, created_at, updated_at)"
        f" VALUES (3, 7, '{NUM}', 'x', 'x', 'x')"
    )
    conn.execute(
        "INSERT INTO carrier_records (id, task_id, tracking_number, source, record_at, status, detail, created_at)"
        f" VALUES (11, 3, '{NUM}', 'jp_post', '2026-10-18 11:11', '引受', 'CHINA', 'c1')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO carrier_records (task_id, tracking_number, source, record_at, created_at)"
            f" VALUES (3, '{NUM}', 'cn_ems', '2026-10-01', 'x')"
        )
    conn.close()
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(storage, "DB_PATH", str(db_path))

    storage.ensure_storage(str(tmp_path / "missing.env"))
    assert carrier_rows() == [(3, NUM, "jp_post", "2026-10-18 11:11", "引受", "CHINA")]
    with sqlite3.connect(db_path) as c:
        sql = c.execute("SELECT sql FROM sqlite_master WHERE name = 'carrier_records'").fetchone()[0]
        assert "'cn_ems'" in sql
        assert c.execute("SELECT id, created_at FROM carrier_records").fetchall() == [(11, "c1")]
        assert {row[2] for row in c.execute("PRAGMA foreign_key_list(carrier_records)")} == {"tracking_tasks"}
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name = 'idx_carrier_records_number'").fetchone()
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name = 'carrier_records_new'").fetchone() is None
        assert c.execute("SELECT COUNT(*) FROM tracking_tasks").fetchone()[0] == 1
    assert storage.record_cn_records(3, NUM, [{"record_at": "2026-11-09 19:31", "status": "中国邮政已收取邮件", "detail": ""}]) == 1

    storage.ensure_storage(str(tmp_path / "missing.env"))  # 幂等
    with sqlite3.connect(db_path) as c:
        assert c.execute("SELECT sql FROM sqlite_master WHERE name = 'carrier_records'").fetchone()[0] == sql
    assert len(carrier_rows()) == 2
    storage.delete_task(3)  # 外键级联仍然有效
    assert carrier_rows() == []


# --- 渠道推断 ---

def _seed_sample(db, owner, number, channel, events=CN_EVENTS):
    task = db.create_task(owner["id"], {"tracking_number": number, "channel": channel})
    db.record_cn_records(task["id"], number, cn_rows(events))
    return task


def test_channel_suggestion_single_multiple_and_none(db, owner):
    current = db.create_task(owner["id"], {"tracking_number": NUM})
    db.record_cn_records(current["id"], NUM, cn_rows(CN_EVENTS[-3:]))
    now = dt("2026-11-10 00:00")

    # 没有其他样本：有网点、无建议
    result = db.task_forecast(current["id"], now=now)
    assert result["collection_office"] == "威海市丹东路第二揽投部"
    assert result["suggested_channel"] is None and result["office_multi_channel"] is False

    # 同网点的历史只有一个渠道 → 建议
    _seed_sample(db, owner, NUM2, "渠道A")
    result = db.task_forecast(current["id"], now=now)
    assert result["suggested_channel"] == "渠道A" and result["office_multi_channel"] is False
    assert db.get_task(current["id"])["channel"] == ""  # 不自动写库

    # 同网点出现第二个渠道 → 不建议
    _seed_sample(db, owner, NUM3, "渠道B")
    result = db.task_forecast(current["id"], now=now)
    assert result["suggested_channel"] is None and result["office_multi_channel"] is True

    # 任务已有渠道：不再推断
    db.update_task(current["id"], {"channel": "渠道B"})
    result = db.task_forecast(current["id"], now=now)
    assert result["suggested_channel"] is None and result["office_multi_channel"] is False
    assert result["collection_office"] == "威海市丹东路第二揽投部"


def test_forecast_api_returns_cn_records_and_set_channel(client, db, owner):
    task = db.create_task(owner["id"], {"tracking_number": NUM})
    db.record_cn_records(task["id"], NUM, cn_rows())
    _seed_sample(db, owner, NUM2, "渠道A")
    client.post("/login", data={"username": "ella", "password": "pw-ella"})
    body = client.get(f"/api/tasks/{task['id']}/forecast").get_json()
    assert body["forecast"]["suggested_channel"] == "渠道A"
    records = body["cn_records"]
    assert records[0] == {"record_at": "2026-11-13 07:17", "status": "到达寄达地处理中心", "detail": "", "segment": "japan"}
    assert records[-1]["status"] == "中国邮政已收取邮件" and len(records) == len(CN_EVENTS)
    # 「设为该渠道」按钮走现有编辑接口，只带 channel
    assert client.put(f"/api/tasks/{task['id']}", json={"channel": "渠道A"}).status_code == 200
    assert db.get_task(task["id"])["channel"] == "渠道A"


# --- tracker ---

def _due(task_id):
    return next(t for t in storage.list_due_tasks() if t["id"] == task_id)


def test_tracker_registers_once_then_records_cn_events(db, owner, fake17, monkeypatch):
    import tracker
    from test_task_events import Net, page_html

    net = Net(tracker, monkeypatch)
    net.page = page_html(("2026/11/09 18:31", "引受", "CHINA"))
    task = db.create_task(owner["id"], {"tracking_number": NUM})
    other = db.create_account({"username": "fred", "password": "pw-fred", "role": "user"})
    twin = db.create_task(other["id"], {"tracking_number": NUM})

    tracker.process_task(_due(task["id"]), ENV)
    assert fake17.calls == [("register", [NUM])]
    assert storage.track17_state(NUM)["registered_at"]
    assert len(carrier_rows("jp_post")) == 1  # 日本邮政照常

    fake17.ready[NUM] = track_info(CN_EVENTS)
    tracker.process_task(_due(twin["id"]), ENV)  # 同单号另一个任务：不再登记
    tracker.process_task(_due(task["id"]), ENV)
    assert fake17.count("register") == 1 and fake17.count("gettrackinfo") == 2
    assert len(carrier_rows("cn_ems")) == 2 * len(CN_EVENTS)  # 各任务各一份，读取时按单号合并
    state = storage.track17_state(NUM)
    assert state["last_synced_at"] and state["last_error"] == ""
    assert not net.pushes  # cn 段不推送（账号没配 Bark）


def test_tracker_non_cn_and_disabled_skip_17track(db, owner, fake17, monkeypatch):
    import tracker
    from test_task_events import Net, page_html

    Net(tracker, monkeypatch).page = page_html(("2026/11/09 18:31", "引受", "USA"))
    us = db.create_task(owner["id"], {"tracking_number": US_NUM})
    cn = db.create_task(owner["id"], {"tracking_number": NUM})
    tracker.process_task(_due(us["id"]), ENV)
    tracker.process_task(_due(cn["id"]), {})
    assert fake17.calls == []
    assert storage.track17_state(NUM) is None


def test_17track_errors_never_affect_jp_tracking_and_push(db, owner, fake17, monkeypatch):
    import tracker
    from test_task_events import Net, page_html

    db.update_account(owner["id"], {"bark_keys": "key-1"})
    net = Net(tracker, monkeypatch)
    net.page = page_html(("2026/11/09 18:31", "引受", "CHINA"))
    task = db.create_task(owner["id"], {"tracking_number": NUM})
    fake17.fail = True
    env = {**ENV, "BARK_SERVER": "http://bark.example.com"}
    assert tracker.process_task(_due(task["id"]), env) is False
    assert len(net.pushes) == 1 and len(carrier_rows("jp_post")) == 1
    state = storage.track17_state(NUM)
    assert state["registered_at"] is None and "连接超时" in state["last_error"]
    assert db.get_task(task["id"])["last_error"] == ""

    # 登记成功后查询出错：同样只记 last_error
    fake17.fail = False
    net.page = page_html(("2026/11/09 18:31", "引受", "CHINA"), ("2026/11/10 08:00", "国際交換局に到着", "CHINA"))
    tracker.process_task(_due(task["id"]), env)
    assert storage.track17_state(NUM)["registered_at"]
    monkeypatch.setattr(track17, "get_cn_events", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("坏响应")))
    net.page = page_html(("2026/11/09 18:31", "引受", "CHINA"), ("2026/11/10 08:00", "国際交換局に到着", "CHINA"),
                         ("2026/11/10 09:00", "国際交換局から発送", "CHINA"))
    tracker.process_task(_due(task["id"]), env)
    assert len(net.pushes) == 3 and len(carrier_rows("jp_post")) == 3
    assert storage.track17_state(NUM)["last_error"] == "坏响应"


def test_cn_records_skipped_when_number_changed(db, owner, fake17):
    import tracker

    task = db.create_task(owner["id"], {"tracking_number": NUM})
    fake17.registered.add(NUM)
    storage.track17_update(NUM, registered=True, error="")
    fake17.ready[NUM] = track_info(CN_EVENTS)
    db.update_task(task["id"], {"tracking_number": NUM2})
    assert tracker.sync_cn_leg(task["id"], NUM, ENV, timeout=5) == ("pending", 0)
    assert carrier_rows("cn_ems") == []
    assert storage.track17_state(NUM)["last_synced_at"] is None


def test_catch_up_archived_numbers(db, owner, fake17):
    import tracker

    archived = db.create_task(owner["id"], {"tracking_number": NUM})
    db.archive_task(archived["id"])
    active = db.create_task(owner["id"], {"tracking_number": NUM2})
    for number in (NUM, NUM2):
        storage.track17_update(number, registered=True, error="")
    # 刚请求过：30 分钟内不补抓
    assert storage.track17_catchup_candidates(datetime.now()) == []
    soon = datetime.now() + timedelta(minutes=31)
    assert storage.track17_catchup_candidates(soon) == [(NUM, archived["id"])]  # 还在追踪的不补抓
    assert tracker.catch_up_cn_legs(ENV, timeout=5, now=soon) == 0  # 还没数据
    fake17.ready[NUM] = track_info(CN_EVENTS)
    assert tracker.catch_up_cn_legs(ENV, timeout=5, now=soon + timedelta(minutes=31)) == 1
    assert fake17.count("register") == 0
    assert len(carrier_rows("cn_ems")) == len(CN_EVENTS)
    # 已有「到达寄达地」：不再补抓；登记超过 3 天也不再补
    assert storage.track17_catchup_candidates(soon + timedelta(hours=2)) == []
    assert storage.track17_catchup_candidates(soon + timedelta(days=4)) == []
    assert active["id"]


def test_catch_up_keeps_trying_without_destination_event(db, owner, fake17):
    import tracker

    task = db.create_task(owner["id"], {"tracking_number": NUM})
    db.archive_task(task["id"])
    storage.track17_update(NUM, registered=True, error="")
    fake17.ready[NUM] = track_info(CN_EVENTS[-3:])  # 只到揽收
    soon = datetime.now() + timedelta(minutes=31)
    assert tracker.catch_up_cn_legs(ENV, timeout=5, now=soon) == 1
    assert storage.track17_catchup_candidates(soon + timedelta(minutes=31)) == [(NUM, task["id"])]


# --- 补录 ---

def test_backfill_cn_number_registers_and_notes(client, db, owner, fake17, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "fetch_carrier_records", lambda number: [])
    monkeypatch.setattr(app_module, "load_system_env", lambda path, keys: {key: ENV.get(key, "") for key in keys})
    client.post("/login", data={"username": "ella", "password": "pw-ella"})
    fake17.ready[NUM2] = track_info(CN_EVENTS)
    fake17.registered.add(NUM2)  # 17TRACK 那边早已登记过：视为成功、照常抓
    body = client.post("/api/tasks/backfill", json={"lines": f"{NUM} 渠道A\n{NUM2} 渠道A\n{US_NUM} 渠道B"}).get_json()
    notes = [item.get("cn_note") for item in body["results"]]
    assert notes == ["国内段稍后补上", f"国内段 {len(CN_EVENTS)} 条", None]
    assert [path for path, _ in fake17.calls] == ["register", "gettrackinfo", "register", "gettrackinfo"]
    assert storage.track17_state(NUM)["registered_at"] and storage.track17_state(NUM2)["last_synced_at"]
    assert storage.track17_state(US_NUM) is None
    # 再补录同一单号不会再登记
    client.post("/api/tasks/backfill", json={"lines": f"{NUM} 渠道A"})
    assert fake17.count("register") == 2


def test_backfill_without_key_never_calls_17track(client, db, owner, fake17, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "fetch_carrier_records", lambda number: [])
    monkeypatch.setattr(app_module, "load_system_env", lambda path, keys: {key: "" for key in keys})
    client.post("/login", data={"username": "ella", "password": "pw-ella"})
    body = client.post("/api/tasks/backfill", json={"lines": f"{NUM} 渠道A"}).get_json()
    assert "cn_note" not in body["results"][0] and fake17.calls == []


def test_backfill_cn_notes_cover_sync_results():
    import app as app_module
    import tracker

    assert set(app_module.BACKFILL_CN_NOTES) == set(tracker.CN_SYNC_RESULTS)


# --- 系统设置里的密钥 ---

def test_secret_masked_and_mask_means_unchanged(client, db, tmp_path, monkeypatch):
    import env_schema
    import app as app_module

    path = tmp_path / ".env"
    path.write_text(f"TRACK17_API_KEY={KEY}\n")
    monkeypatch.setattr(app_module, "DOTENV_PATH", str(path))
    for key in env_schema.ENV_KEYS:  # update_env 会写进程环境变量，测试后还原
        monkeypatch.setenv(key, os.environ.get(key, ""))
    monkeypatch.setenv("TRACK17_API_KEY", KEY)
    client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD})
    page = client.get("/").get_data(as_text=True)
    assert KEY not in page and env_schema.SECRET_MASK in page
    body = client.post("/update_env", json={"TRACK17_API_KEY": env_schema.SECRET_MASK}).get_json()
    assert body["status"] == "success" and body["env_vars"]["TRACK17_API_KEY"] == env_schema.SECRET_MASK
    assert KEY in path.read_text()
    body = client.post("/update_env", json={"TRACK17_API_KEY": "new-key-123", "REQUEST_TIMEOUT": "20"}).get_json()
    assert body["env_vars"]["TRACK17_API_KEY"] == env_schema.SECRET_MASK and "new-key-123" not in str(body)
    assert "TRACK17_API_KEY=new-key-123" in path.read_text()
    body = client.post("/update_env", json={"TRACK17_API_KEY": ""}).get_json()
    assert body["env_vars"]["TRACK17_API_KEY"] == ""

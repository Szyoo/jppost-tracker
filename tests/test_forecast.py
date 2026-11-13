"""到达预测：履历解析与阶段映射、样本合并、加权统计、条件化与 overdue、可信度、起点；
carrier_records 回填与 tracker 写入、pre_milestones 优先级、网页与 /api/v1 接口、补录、前后端映射一致。"""
import os
import re
import sqlite3
from datetime import datetime, timedelta

import pytest

import forecast
import storage
from conftest import ADMIN_PASSWORD, gate

# 一单从中国寄日本的 EMS 全程（真实样本的结构，单号虚构）
SAMPLE = [
    ("2026/10/18 11:11 引受", "CHINA"),
    ("2026/10/19 08:22 国際交換局に到着", "WEIHAI D EMS · CHINA"),
    ("2026/10/19 08:22 税関検査のため税関へ提示", "CHINA"),
    ("2026/10/19 11:23 税関保留", "CHINA"),
    ("2026/10/21 13:30 国際交換局から発送", "WEIHAI D EMS · CHINA"),
    ("2026/10/21 13:30 税関から受領", "CHINA"),
    ("2026/10/21 13:35 税関検査のため税関へ提示", "CHINA"),
    ("2026/10/25 22:53 国際交換局に到着", "東京国際郵便局 · 東京都"),
    ("2026/10/26 04:30 通関手続中", "東京国際郵便局 · 東京都"),
    ("2026/10/26 10:02 国際交換局から発送", "東京国際郵便局 · 東京都"),
    ("2026/10/26 18:05 到着", "配達予定日：10月27日 · 板橋北郵便局 · 東京都"),
    ("2026/10/27 持ち出し中", "板橋北郵便局 · 東京都"),
    ("2026/10/27 13:50 お届け済み", "板橋北郵便局 · 東京都"),
]
NUM = "EB000000001CN"
NUM2 = "EB000000002CN"
NUM3 = "EB000000003CN"


def as_records(sample):
    rows = []
    for info, detail in sample:
        record_at, status = forecast.parse_record_info(info)
        rows.append({"source": "jp_post", "record_at": record_at, "status": status, "detail": detail})
    return rows


def dt(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M")


# --- 解析与阶段映射 ---

def test_parse_record_info():
    assert forecast.parse_record_info("2026/10/21 13:30 国際交換局から発送") == ("2026-10-21 13:30", "国際交換局から発送")
    assert forecast.parse_record_info("2026/10/27 持ち出し中") == ("2026-10-27", "持ち出し中")
    assert forecast.parse_record_info("2026/1/5 9:05 引受") == ("2026-01-05 09:05", "引受")
    assert forecast.parse_record_info("引受") is None
    assert forecast.parse_record_info("2026/13/40 引受") is None


def test_stage_mapping_full_sample():
    stages = forecast.stages_from_records(as_records(SAMPLE))
    assert stages == {
        "accepted": dt("2026-10-18 11:11"),
        "origin_exchange": dt("2026-10-19 08:22"),
        "origin_customs": dt("2026-10-19 08:22"),  # 取最早，后面两次始发国海关不算
        "departed_origin": dt("2026-10-21 13:30"),
        "arrived_japan": dt("2026-10-25 22:53"),  # 同名状态，靠 detail 的都道府县判在日本
        "japan_customs": dt("2026-10-26 04:30"),
        "departed_exchange": dt("2026-10-26 10:02"),
        "local_arrived": dt("2026-10-26 18:05"),
        "out_for_delivery": dt("2026-10-27 12:00"),  # 只有日期按 12:00
        "delivered": dt("2026-10-27 13:50"),
    }


def test_stage_mapping_edge_cases():
    stage = forecast.record_stage
    assert stage("jp_post", "国際交換局に到着", "") == "origin_exchange"
    assert stage("jp_post", "国際交換局に到着", "札幌 · 北海道") == "arrived_japan"
    assert stage("jp_post", "税関検査のため税関へ提示", "大阪国際郵便局 · 大阪府") == "japan_customs"
    assert stage("jp_post", "お届け先にお届け済み", "") == "delivered"
    assert stage("jp_post", "窓口でお渡し", "") == "delivered"
    for unknown in ("ご不在のため持ち戻り", "お届け先にお伺いしましたがご不在でした", "通過", ""):
        assert stage("jp_post", unknown, "東京都") is None
    with pytest.raises(ValueError):
        stage("unknown_source", "引受", "")


def test_stages_cover_all_carrier_sources_and_tracker_reuses_delivered():
    import tracker

    assert set(forecast.CARRIER_SOURCES) == {"jp_post", "cn_ems"}
    assert tracker.DELIVERED_STATUSES is forecast.DELIVERED_STATUSES
    assert forecast.STAGES[0] == "ordered" and forecast.STAGES[-1] == "delivered"
    assert set(forecast.PRE_STAGES) <= set(forecast.STAGES)


def test_merge_stages_takes_earliest():
    a = {"accepted": dt("2026-10-02 10:00"), "delivered": dt("2026-10-10 10:00")}
    b = {"accepted": dt("2026-10-01 10:00"), "ordered": dt("2026-09-28 10:00")}
    assert forecast.merge_stages(a, b) == {
        "accepted": dt("2026-10-01 10:00"), "delivered": dt("2026-10-10 10:00"), "ordered": dt("2026-09-28 10:00"),
    }


# --- 统计 ---

def test_weighted_quantile():
    assert forecast.weighted_quantile([3, 1, 2], [1, 1, 1], 0.5) == 2
    assert forecast.weighted_quantile([1, 2], [1, 1], 0.5) == 1  # 偶数取下侧
    assert forecast.weighted_quantile([1, 10], [0.3, 1.0], 0.5) == 10  # 同渠道权重大
    values = list(range(1, 11))
    assert forecast.weighted_quantile(values, [1] * 10, 0.2) == 2
    assert forecast.weighted_quantile(values, [1] * 10, 0.8) == 8
    with pytest.raises(ValueError):
        forecast.weighted_quantile([], [], 0.5)


def sample_of(hours_to_customs, hours_to_delivered, channel="A", base="2026-10-01 00:00", extra=None):
    """以 arrived_japan 为基准的简化样本：到达日本后 N 小时通关、M 小时签收。"""
    t0 = dt(base)
    stages = {
        "accepted": t0 - timedelta(days=5),
        "arrived_japan": t0,
        "japan_customs": t0 + timedelta(hours=hours_to_customs),
        "delivered": t0 + timedelta(hours=hours_to_delivered),
    }
    stages.update(extra or {})
    return {"stages": stages, "channel": channel}


CURRENT = {"accepted": dt("2026-11-05 10:00"), "arrived_japan": dt("2026-11-10 10:00")}


def test_forecast_basic_prediction_and_weights():
    now = dt("2026-11-10 12:00")  # 已等 2 小时
    samples = [sample_of(10, 40), sample_of(20, 50), sample_of(30, 60, channel="B")]
    result = forecast.build_forecast(CURRENT, "A", samples, now)
    assert result["current"] == {"stage": "arrived_japan", "at": "2026-11-10 10:00", "waited_seconds": 7200}
    assert result["origin"] == {"stage": "accepted", "at": "2026-11-05 10:00"}
    assert result["day"] == 6  # 11/5 是第 1 天
    assert result["samples"] == {"total": 3, "same_channel": 2, "same_office": 0, "similar": 2, "atypical": 0, "similar_atypical": 0}
    assert result["basis"] == "channel"  # 当前单没有收寄网点：邮政段退回按渠道
    assert result["confidence"] == "rough"
    assert result["overdue"] is False
    customs, delivered = result["remaining"]
    assert customs["stage"] == "japan_customs"
    # 剩余 = d - w：8h、18h、28h；权重 1、1、0.3（总 2.3）→ 中位数 18h；3 单用 20%–80% 分位：
    # 20% 处 0.46 落在 8h，80% 处 1.84 落在 18h——其他渠道那单权重小，拉不动上沿
    assert customs["median_seconds"] == 18 * 3600
    assert customs["eta"] == "2026-11-11 06:00"
    assert (customs["low_seconds"], customs["high_seconds"]) == (8 * 3600, 18 * 3600)
    assert customs["occurrence"] == 1.0 and customs["maybe"] is False
    assert customs["samples"] == {"total": 3, "similar": 2, "atypical": 0}
    assert customs["segment"] == "japan" and customs["basis"] == "channel"
    assert delivered["stage"] == "delivered" and delivered["median_seconds"] == 48 * 3600


def test_forecast_min_max_interval_below_three_samples():
    now = dt("2026-11-10 10:00")
    result = forecast.build_forecast(CURRENT, "A", [sample_of(10, 40), sample_of(30, 60)], now)
    customs = result["remaining"][0]
    assert (customs["low_seconds"], customs["median_seconds"], customs["high_seconds"]) == (36000, 36000, 108000)


def test_forecast_conditioning_drops_fast_samples():
    """已等 15 小时：10 小时就通关的样本不合格，只剩更慢的。"""
    now = dt("2026-11-11 01:00")
    result = forecast.build_forecast(CURRENT, "A", [sample_of(10, 40), sample_of(20, 50), sample_of(30, 60)], now)
    assert (result["samples"]["total"], result["samples"]["same_channel"]) == (2, 2)
    assert result["remaining"][0]["median_seconds"] == 5 * 3600  # (20-15)h
    assert result["overdue"] is False


def test_forecast_overdue_falls_back_to_all_samples():
    now = dt("2026-11-13 10:00")  # 已等 72 小时，比所有样本离开「到达日本」都久
    result = forecast.build_forecast(CURRENT, "A", [sample_of(10, 40), sample_of(20, 50)], now)
    assert result["overdue"] is True
    assert (result["samples"]["total"], result["samples"]["same_channel"]) == (2, 2)
    assert all(item["median_seconds"] == 0 for item in result["remaining"])


def test_forecast_occurrence_and_maybe():
    now = dt("2026-11-10 10:00")
    out = {"out_for_delivery": dt("2026-10-02 12:00")}
    samples = [sample_of(10, 40, extra=out), sample_of(20, 50), sample_of(30, 60)]
    result = forecast.build_forecast(CURRENT, "A", samples, now)
    by_stage = {item["stage"]: item for item in result["remaining"]}
    assert by_stage["out_for_delivery"]["occurrence"] == pytest.approx(1 / 3, abs=1e-3)
    assert by_stage["out_for_delivery"]["maybe"] is True
    assert "local_arrived" not in by_stage  # 出现率 0 不列


def test_confidence_levels():
    now = dt("2026-11-10 10:00")
    assert forecast.build_forecast(CURRENT, "A", [], now)["confidence"] == "none"
    assert forecast.build_forecast(CURRENT, "A", [sample_of(1, 2, "B")], now)["confidence"] == "borrowed"
    assert forecast.build_forecast(CURRENT, "", [sample_of(1, 2, "")], now)["confidence"] == "borrowed"  # 未标注不算同渠道
    assert forecast.build_forecast(CURRENT, "A", [sample_of(1, 2)] * 4, now)["confidence"] == "rough"
    assert forecast.build_forecast(CURRENT, "A", [sample_of(1, 2)] * 5, now)["confidence"] == "solid"
    none = forecast.build_forecast(CURRENT, "A", [], now)
    assert none["remaining"] == [] and none["current"]["stage"] == "arrived_japan"
    # 样本没有当前阶段之后的任何记录 → 不可用
    stuck = {"stages": {"arrived_japan": dt("2026-10-01 00:00")}, "channel": "A"}
    assert forecast.build_forecast(CURRENT, "A", [stuck], now)["confidence"] == "none"


def test_origin_selection_and_delivered():
    now = dt("2026-11-10 10:00")
    stages = {
        "handed_over": dt("2026-11-02 09:00"),
        "accepted": dt("2026-11-03 10:00"),
    }
    assert forecast.build_forecast(stages, "", [], now)["origin"]["stage"] == "handed_over"
    stages["ordered"] = dt("2026-11-01 23:00")
    result = forecast.build_forecast(stages, "", [], now)
    assert result["origin"]["stage"] == "ordered" and result["day"] == 10
    only_accept = {"accepted": dt("2026-11-10 08:00")}
    assert forecast.build_forecast(only_accept, "", [], now)["day"] == 1

    done = forecast.stages_from_records(as_records(SAMPLE))
    result = forecast.build_forecast(done, "", [sample_of(1, 2)], now)
    assert result["delivered"] is True and result["remaining"] == []
    assert result["total_seconds"] == int((dt("2026-10-27 13:50") - dt("2026-10-18 11:11")).total_seconds())
    assert forecast.build_forecast({}, "", [], now)["current"] is None


def test_current_stage_tie_takes_later_order():
    stages = {"origin_exchange": dt("2026-11-01 08:00"), "origin_customs": dt("2026-11-01 08:00")}
    result = forecast.build_forecast(stages, "", [], dt("2026-11-01 09:00"))
    assert result["current"]["stage"] == "origin_customs"


def test_normalize_milestone_time():
    norm = forecast.normalize_milestone_time
    assert norm("2026-10-01") == "2026-10-01"
    assert norm("2026-10-01 08:30") == "2026-10-01 08:30"
    assert norm("2026-10-01 08:30:59") == "2026-10-01 08:30"
    assert norm("2026-10-01T08:30") == "2026-10-01 08:30"
    assert norm("2026-10-01T00:30:00Z", allow_tz=True) == "2026-10-01 09:30"
    assert norm("2026-10-01T08:30:00+08:00", allow_tz=True) == "2026-10-01 09:30"
    for bad in ("2026/10/01", "yesterday", "2026-13-01"):
        with pytest.raises(ValueError):
            norm(bad, allow_tz=True)
    with pytest.raises(ValueError):
        norm("2026-10-01T00:30:00Z")  # 网页表单不收时区


# --- 存储：carrier_records 与 pre_milestones ---

@pytest.fixture
def owner(db):
    return db.create_account({"username": "ella", "password": "pw-ella", "role": "user"})


@pytest.fixture
def task(db, owner):
    return db.create_task(owner["id"], {"tracking_number": NUM})


def raw_records(sample):
    return [{"info": info, "detail": detail} for info, detail in sample]


def carrier_rows(task_id=None):
    with sqlite3.connect(storage.DB_PATH) as conn:
        query = "SELECT task_id, tracking_number, source, record_at, status, detail FROM carrier_records"
        params = ()
        if task_id is not None:
            query += " WHERE task_id = ?"
            params = (task_id,)
        return conn.execute(query + " ORDER BY id", params).fetchall()


def test_update_task_state_writes_carrier_records_idempotently(db, task):
    db.update_task_state(task["id"], tracking_number=NUM, carrier_records=raw_records(SAMPLE[:3]))
    db.update_task_state(task["id"], tracking_number=NUM, carrier_records=raw_records(SAMPLE[:5]))
    rows = carrier_rows(task["id"])
    assert len(rows) == 5
    assert rows[0] == (task["id"], NUM, "jp_post", "2026-10-18 11:11", "引受", "CHINA")
    # 单号对不上：整次回写作废，履历也不写
    assert db.update_task_state(task["id"], tracking_number="OTHER", carrier_records=raw_records(SAMPLE)) is None
    assert len(carrier_rows(task["id"])) == 5
    assert db.record_carrier_records(task["id"], "OTHER", raw_records(SAMPLE)) is None
    assert db.record_carrier_records(task["id"], NUM, raw_records(SAMPLE)) == len(SAMPLE)
    assert len(carrier_rows(task["id"])) == len(SAMPLE)


def test_carrier_source_check_constraint(db, task):
    with sqlite3.connect(storage.DB_PATH) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO carrier_records (task_id, tracking_number, source, record_at, created_at)"
                " VALUES (?, ?, 'other_post', '2026-10-01', 'x')",
                (task["id"], NUM),
            )


def test_tracker_records_all_rows_even_when_push_fails(db, owner, task, monkeypatch):
    import tracker
    from test_task_events import Net, page_html

    db.update_account(owner["id"], {"bark_keys": "key-1"})
    net = Net(tracker, monkeypatch)
    net.push_ok = False
    net.page = page_html(*[(info[:16], info[17:], detail) for info, detail in SAMPLE[:4]])
    due = next(t for t in storage.list_due_tasks() if t["id"] == task["id"])
    assert tracker.process_task(due, {"BARK_SERVER": "http://bark.example.com"}) is True
    assert len(carrier_rows(task["id"])) == 4


def test_tracker_skips_records_when_number_changed(db, task, monkeypatch):
    import tracker
    from test_task_events import Net, page_html

    net = Net(tracker, monkeypatch)
    net.page = page_html(("2026/10/18 11:11", "引受", "CHINA"))
    net.on_fetch = lambda: db.update_task(task["id"], {"tracking_number": NUM2})
    due = next(t for t in storage.list_due_tasks() if t["id"] == task["id"])
    tracker.process_task(due, {"BARK_SERVER": "http://bark.example.com"})
    assert carrier_rows() == []


def test_backfill_from_task_events_on_startup(db, task):
    storage.update_task_state(task["id"], tracking_number=NUM, new_records=raw_records(SAMPLE[:4]))
    with sqlite3.connect(storage.DB_PATH) as conn:
        conn.execute("DELETE FROM carrier_records")
    storage.ensure_storage("missing.env")
    assert [row[4] for row in carrier_rows(task["id"])] == ["引受", "国際交換局に到着", "税関検査のため税関へ提示", "税関保留"]
    storage.ensure_storage("missing.env")  # 幂等
    assert len(carrier_rows(task["id"])) == 4


def test_existing_db_gets_channel_column_and_tables(tmp_path, monkeypatch):
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    storage._ensure_accounts_schema(conn)
    storage._ensure_tasks_schema(conn)
    storage._ensure_events_schema(conn)
    conn.execute("INSERT INTO accounts (id, username, role, created_at, updated_at) VALUES (7, 'old', 'user', 'x', 'x')")
    conn.execute(
        "INSERT INTO tracking_tasks (id, account_id, tracking_number, first_seen_at, created_at, updated_at)"
        " VALUES (3, 7, 'N1', 'x', 'x', 'x')"
    )
    conn.execute(
        "INSERT INTO task_events (task_id, account_id, tracking_number, kind, status, detail, created_at)"
        " VALUES (3, 7, 'N1', 'status_change', '2026/10/27 持ち出し中', '板橋北郵便局 · 東京都', 'x')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(storage, "DB_PATH", str(db_path))
    storage.ensure_storage(str(tmp_path / "missing.env"))
    assert storage.get_task(3)["channel"] == ""
    assert carrier_rows(3) == [(3, "N1", "jp_post", "2026-10-27", "持ち出し中", "板橋北郵便局 · 東京都")]
    with sqlite3.connect(db_path) as c:
        fks = {row[2] for row in c.execute("PRAGMA foreign_key_list(pre_milestones)")}
    assert fks == {"tracking_tasks"}
    storage.delete_task(3)
    assert carrier_rows() == []


def milestones_of(task_id):
    with sqlite3.connect(storage.DB_PATH) as conn:
        return {row[0]: (row[1], row[2]) for row in conn.execute(
            "SELECT stage, at, source FROM pre_milestones WHERE task_id = ?", (task_id,)
        )}


def test_milestone_priority_manual_over_caller(db, task):
    from contextlib import closing

    def write(milestones, source):
        with closing(storage._connect()) as conn:
            with conn:
                storage._write_milestones(conn, task["id"], milestones, source)

    write({"ordered": "2026-10-01", "packed": "2026-10-02 10:00"}, "caller")
    assert milestones_of(task["id"]) == {"ordered": ("2026-10-01", "caller"), "packed": ("2026-10-02 10:00", "caller")}
    # 手填覆盖调用方；原样保存不改来源
    db.update_task(task["id"], {"milestones": {"ordered": "2026-09-30", "packed": "2026-10-02 10:00"}})
    assert milestones_of(task["id"]) == {"ordered": ("2026-09-30", "manual"), "packed": ("2026-10-02 10:00", "caller")}
    # 调用方不覆盖手填，但能更新自己的
    write({"ordered": "2026-10-05", "packed": "2026-10-03"}, "caller")
    assert milestones_of(task["id"]) == {"ordered": ("2026-09-30", "manual"), "packed": ("2026-10-03", "caller")}
    # 手填清空即删除
    db.update_task(task["id"], {"milestones": {"ordered": "", "packed": ""}})
    assert milestones_of(task["id"]) == {}
    with pytest.raises(ValueError):
        db.update_task(task["id"], {"milestones": {"shipped": "2026-10-01"}})
    with pytest.raises(ValueError):
        db.update_task(task["id"], {"milestones": {"ordered": "去年"}})


def test_channel_normalized_and_listed(db, owner):
    created = db.create_task(owner["id"], {"tracking_number": NUM, "channel": "  渠道A  ",
                                           "milestones": {"ordered": "2026-10-01", "packed": ""}})
    assert created["channel"] == "渠道A"
    assert milestones_of(created["id"]) == {"ordered": ("2026-10-01", "manual")}
    with pytest.raises(ValueError):
        db.create_task(owner["id"], {"tracking_number": NUM2, "channel": "x" * 51})
    account = db.get_account(owner["id"])
    assert account["channels"] == ["渠道A"]
    assert account["tasks"][0]["milestones"]["ordered"] == {"at": "2026-10-01", "source": "manual"}
    # 只改启用状态不动渠道；显式给空串才清掉
    db.update_task(created["id"], {"enabled": False})
    assert db.get_task(created["id"])["channel"] == "渠道A"
    db.update_task(created["id"], {"channel": ""})
    assert db.get_task(created["id"])["channel"] == ""


def test_task_forecast_samples_dedup_and_exclusion(db, owner):
    """同单号挂在两个账号下合并成一个样本；当前单号自己不算样本；其他账号的节点不并进当前单。"""
    other = db.create_account({"username": "fay", "password": "pw", "role": "user"})
    past_a = db.create_task(owner["id"], {"tracking_number": NUM2, "channel": "A"})
    past_b = db.create_task(other["id"], {"tracking_number": NUM2})
    db.update_task_state(past_a["id"], tracking_number=NUM2, carrier_records=raw_records(SAMPLE[:8]))
    db.update_task_state(past_b["id"], tracking_number=NUM2, carrier_records=raw_records(SAMPLE))

    current = db.create_task(owner["id"], {"tracking_number": NUM, "channel": "A",
                                           "milestones": {"ordered": "2026-11-01"}})
    db.update_task_state(current["id"], tracking_number=NUM,
                         carrier_records=[{"info": "2026/11/03 10:00 引受", "detail": "CHINA"}])
    shadow = db.create_task(other["id"], {"tracking_number": NUM, "milestones": {"ordered": "2026-10-01"}})

    result = db.task_forecast(current["id"], now=dt("2026-11-03 12:00"))
    assert result["origin"] == {"stage": "ordered", "at": "2026-11-01 12:00"}
    assert result["day"] == 3
    assert (result["samples"]["total"], result["samples"]["same_channel"]) == (1, 1)
    stages = [item["stage"] for item in result["remaining"]]
    assert stages[0] == "origin_exchange" and stages[-1] == "delivered"
    assert "EB" not in repr(result)  # 不暴露任何单号
    assert db.task_forecast(99999) is None
    assert shadow  # 另一个账号的同单号任务存在，但它的下单时间没有并进来


# --- 网页接口 ---

def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password})


def test_forecast_api_permissions(client, db, owner, task):
    url = f"/api/tasks/{task['id']}/forecast"
    assert client.get(url).status_code == 401
    db.create_account({"username": "frank", "password": "pw-frank", "role": "user"})
    login(client, "frank", "pw-frank")
    assert client.get(url).status_code == 403
    assert client.get("/api/tasks/99999/forecast").status_code == 404
    client.post("/logout")
    login(client, "ella", "pw-ella")
    body = client.get(url).get_json()
    assert body["status"] == "success" and body["forecast"]["confidence"] == "none"
    client.post("/logout")
    login(client, "admin", ADMIN_PASSWORD)
    assert client.get(url).status_code == 200


def test_web_task_create_update_with_channel_and_milestones(client, db, owner):
    login(client, "admin", ADMIN_PASSWORD)
    resp = client.post(f"/api/users/{owner['id']}/tasks", json={
        "tracking_number": NUM, "channel": "渠道A", "milestones": {"ordered": "2026-10-01 09:00"},
    })
    task = resp.get_json()["task"]
    assert task["channel"] == "渠道A"
    assert milestones_of(task["id"]) == {"ordered": ("2026-10-01 09:00", "manual")}
    bad = client.put(f"/api/tasks/{task['id']}", json={"milestones": {"ordered": "bad"}})
    assert bad.status_code == 400
    client.put(f"/api/tasks/{task['id']}", json={"channel": "渠道B", "milestones": {"ordered": "", "packed": "2026-10-02"}})
    assert db.get_task(task["id"])["channel"] == "渠道B"
    assert milestones_of(task["id"]) == {"packed": ("2026-10-02", "manual")}


def test_portal_form_channel_and_milestones(client, db, owner, task):
    login(client, "ella", "pw-ella")
    html = client.get("/").data.decode()
    assert f'data-task-forecast="{task["id"]}"' in html and "/static/forecast.js" in html
    assert "data-backfill-open" in html and 'name="ms_ordered"' in html
    client.post(f"/me/tasks/{task['id']}/update", data={
        "tracking_number": NUM, "label": "", "check_interval": "300", "enabled": "on",
        "channel": "渠道A", "ms_ordered": "2026-10-01", "ms_packed": "", "ms_handed_over": "",
    })
    assert db.get_task(task["id"])["channel"] == "渠道A"
    assert milestones_of(task["id"]) == {"ordered": ("2026-10-01", "manual")}
    html = client.get("/").data.decode()
    assert '<option value="渠道A">' in html and "task-channel" in html
    # 归档区的「恢复」表单没有这些字段，不能把渠道和节点清掉
    db.archive_task(task["id"])
    client.post("/me/tasks", data={"tracking_number": NUM, "label": "", "check_interval": "300", "enabled": "1"})
    assert db.get_task(task["id"])["channel"] == "渠道A"
    assert milestones_of(task["id"]) == {"ordered": ("2026-10-01", "manual")}


def test_admin_page_has_forecast_component(client, db):
    login(client, "admin", ADMIN_PASSWORD)
    html = client.get("/").data.decode()
    assert "<task-forecast" in html and "openBackfill" in html and "channel-options-me" in html


# --- /api/v1 ---

@pytest.fixture
def token(db, owner):
    _, value = db.issue_api_caller("caller-a", "ella")
    return value


def v1_put(client, token, number=NUM, body=None):
    return client.put(f"/api/v1/tracking/{number}", json=body, headers={"Authorization": f"Bearer {token}"})


def test_v1_put_channel_and_milestones(client, db, owner, token):
    resp = v1_put(client, token, body={"channel": "渠道X", "milestones": {
        "ordered": "2026-10-01T00:30:00Z", "handed_over": "2026-10-03 08:00:00",
    }})
    assert resp.status_code == 201 and resp.get_json()["tracking"]["channel"] == "渠道X"
    task = db.list_tasks(owner["id"])[0]
    assert milestones_of(task["id"]) == {
        "ordered": ("2026-10-01 09:30", "caller"), "handed_over": ("2026-10-03 08:00", "caller"),
    }
    # 用户改了渠道和下单时间：调用方再 PUT 不覆盖
    db.update_task(task["id"], {"channel": "用户填的", "milestones": {"ordered": "2026-09-30"}})
    resp = v1_put(client, token, body={"channel": "渠道Y", "milestones": {"ordered": "2026-10-05", "packed": "2026-10-02"}})
    assert resp.get_json()["result"] == "existing"
    assert db.get_task(task["id"])["channel"] == "用户填的"
    assert milestones_of(task["id"])["ordered"] == ("2026-09-30", "manual")
    assert milestones_of(task["id"])["packed"] == ("2026-10-02", "caller")


def test_v1_put_fields_apply_on_delivered_branch(client, db, owner, token):
    v1_put(client, token)
    task = db.list_tasks(owner["id"])[0]
    db.update_task_state(task["id"], tracking_number=NUM, latest_info="2026/10/27 13:50 お届け済み", auto_archive=True)
    resp = v1_put(client, token, body={"channel": "渠道Z", "milestones": {"ordered": "2026-10-01"}})
    assert resp.get_json()["result"] == "delivered"
    assert db.get_task(task["id"])["channel"] == "渠道Z"
    assert milestones_of(task["id"]) == {"ordered": ("2026-10-01", "caller")}


@pytest.mark.parametrize("body", [
    {"channel": "x" * 51},
    {"channel": 5},
    {"milestones": "2026-10-01"},
    {"milestones": {"shipped": "2026-10-01"}},
    {"milestones": {"ordered": "not a date"}},
    {"milestones": {"ordered": ""}},
    {"milestones": {"ordered": 20261001}},
])
def test_v1_put_invalid_fields(client, db, owner, token, body):
    resp = v1_put(client, token, body=body)
    assert resp.status_code == 400 and resp.get_json()["error"]["code"] == "invalid_body"
    assert db.list_tasks(owner["id"]) == []


def test_v1_forecast(client, db, owner, token):
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get(f"/api/v1/tracking/{NUM}/forecast", headers=headers).status_code == 404
    assert client.get(f"/api/v1/tracking/{NUM}/forecast").status_code == 401
    other = db.create_account({"username": "gina", "password": "pw", "role": "user"})
    db.create_task(other["id"], {"tracking_number": NUM})
    assert client.get(f"/api/v1/tracking/{NUM}/forecast", headers=headers).status_code == 404  # 别的账号的单号
    v1_put(client, token)
    body = client.get(f"/api/v1/tracking/{NUM}/forecast", headers=headers).get_json()
    assert body["tracking_number"] == NUM and body["forecast"]["confidence"] == "none"
    assert client.get("/api/v1/tracking/!!/forecast", headers=headers).status_code == 400


# --- 补录 ---

@pytest.fixture
def fake_fetch(monkeypatch):
    import app as app_module

    pages = {}
    calls = []

    def fetch(number):
        calls.append(number)
        return pages.get(number, [])

    monkeypatch.setattr(app_module, "fetch_carrier_records", fetch)
    return pages, calls


def test_backfill_results(client, db, owner, task, fake_fetch):
    pages, calls = fake_fetch
    pages[NUM2] = raw_records(SAMPLE)
    login(client, "ella", "pw-ella")
    lines = "\n".join([
        f"{NUM2} 渠道 A 2026-10-10",   # 新建，渠道里有空格
        f"{NUM3} 渠道B",               # 新建但查不到
        f"{NUM} 渠道C 2026/09/01",     # 已有任务：补渠道与下单
        "bad!! 渠道",                  # 单号不合法
        f"{NUM} ",                     # 缺渠道
        "",
    ])
    body = client.post("/api/tasks/backfill", json={"lines": lines}).get_json()
    statuses = [(item["status"], item.get("records")) for item in body["results"]]
    assert statuses == [("added", len(SAMPLE)), ("no_records", 0), ("updated", None), ("invalid", None), ("invalid", None)]
    assert calls == [NUM2, NUM3]

    added = next(t for t in db.list_tasks(owner["id"], include_archived=True) if t["tracking_number"] == NUM2)
    assert added["archived"] and not added["enabled"] and added["label"] == "" and added["channel"] == "渠道 A"
    assert milestones_of(added["id"]) == {"ordered": ("2026-10-10", "manual")}
    assert len(carrier_rows(added["id"])) == len(SAMPLE)
    assert db.get_task(task["id"])["channel"] == "渠道C"
    assert milestones_of(task["id"]) == {"ordered": ("2026-09-01", "manual")}
    # 已有渠道不覆盖
    client.post("/api/tasks/backfill", json={"lines": f"{NUM} 渠道D"})
    assert db.get_task(task["id"])["channel"] == "渠道C"
    # 补录的已归档任务不会被轮询，也不产生来源记录
    assert NUM2 not in [t["tracking_number"] for t in storage.list_due_tasks()]
    with sqlite3.connect(storage.DB_PATH) as conn:
        assert conn.execute("SELECT COUNT(*) FROM api_refs").fetchone()[0] == 0

    # 补录过的单号成了当前任务的样本
    db.update_task_state(task["id"], tracking_number=NUM, carrier_records=[{"info": "2026/11/03 10:00 引受", "detail": "CHINA"}])
    db.update_task(task["id"], {"channel": "渠道 A"})
    result = db.task_forecast(task["id"], now=dt("2026-11-03 12:00"))
    assert (result["samples"]["total"], result["samples"]["same_channel"]) == (1, 1) and result["confidence"] == "rough"


def test_backfill_limits_and_permissions(client, db, owner, fake_fetch):
    assert client.post("/api/tasks/backfill", json={"lines": f"{NUM} A"}).status_code == 401
    login(client, "ella", "pw-ella")
    assert client.post("/api/tasks/backfill", json={"lines": ""}).status_code == 400
    many = "\n".join(f"EB{index:09d}CN A" for index in range(31))
    assert client.post("/api/tasks/backfill", json={"lines": many}).status_code == 400
    assert client.post("/api/tasks/backfill", json={"lines": f"{NUM} A", "account_id": 2}).status_code == 403
    client.post("/logout")

    login(client, "admin", ADMIN_PASSWORD)
    body = client.post("/api/tasks/backfill", json={"lines": f"{NUM} A", "account_id": owner["id"]}).get_json()
    assert body["results"][0]["status"] == "no_records" and "user_state" in body
    assert [t["tracking_number"] for t in db.list_tasks(owner["id"], include_archived=True)] == [NUM]
    assert client.post("/api/tasks/backfill", json={"lines": f"{NUM} A", "account_id": 99999}).status_code == 404


def test_backfill_line_parser():
    import app as app_module

    parse = app_module.parse_backfill_line
    assert parse("eb000000001cn 渠道A") == (NUM, "渠道A", "")
    assert parse(f"{NUM} 渠道A 2026-10-01") == (NUM, "渠道A", "2026-10-01")
    assert parse(f"{NUM} 2026-10-01") == (NUM, "2026-10-01", "")  # 只有两段时第二段就是渠道
    for bad in (NUM, "!! 渠道", f"{NUM} {'x' * 51}"):
        with pytest.raises(ValueError):
            parse(bad)


# --- 前后端映射一致 ---

def _js_source():
    path = os.path.join(os.path.dirname(storage.__file__), "static", "forecast.js")
    return open(path, encoding="utf-8").read()


def _js_block(name):
    return re.search(rf"export const {name} = Object\.freeze\(\{{(.*?)\}}\);", _js_source(), re.S).group(1)


def test_js_stage_map_matches_python():
    block = _js_block("STAGE_META")
    found = re.findall(r"(\w+): \{ label: '([^']+)', segment: '(\w+)' \}", block)
    assert [key for key, _, _ in found] == list(forecast.STAGE_META)  # 顺序也一致
    assert {key: {"label": label, "segment": segment} for key, label, segment in found} == forecast.STAGE_META


def test_js_segment_basis_and_transit_maps_match_python():
    pairs = re.findall(r"(\w+): \{ label: '([^']+)' \}", _js_block("SEGMENT_META"))
    assert [key for key, _ in pairs] == list(forecast.SEGMENT_META)
    assert {key: {"label": label} for key, label in pairs} == forecast.SEGMENT_META
    pairs = re.findall(r"(\w+): \{ label: '([^']+)' \}", _js_block("BASIS_META"))
    assert {key: {"label": label} for key, label in pairs} == forecast.BASIS_META
    pairs = re.findall(r"(\w+): '([^']+)'", _js_block("TRANSIT_STAGE_TEMPLATES"))
    assert dict(pairs) == forecast.TRANSIT_STAGE_TEMPLATES


def test_js_confidence_map_matches_python():
    pairs = re.findall(r"(\w+): \{ label: '([^']+)' \}", _js_block("CONFIDENCE_META"))
    assert {key: {"label": label} for key, label in pairs} == forecast.CONFIDENCE_META


def test_js_backfill_result_map_matches_python():
    found = re.findall(r"(\w+): \{ label: '([^']+)', tone: '(\w+)' \}", _js_block("BACKFILL_RESULT_META"))
    assert {key: {"label": label, "tone": tone} for key, label, tone in found} == storage.BACKFILL_RESULT_META


def test_js_origin_text_covers_origin_stages():
    block = re.search(r"ORIGIN_TEXT = Object\.freeze\(\{(.*?)\}\);", _js_source(), re.S).group(1)
    assert set(re.findall(r"(\w+): '", block)) == set(forecast.ORIGIN_STAGES)

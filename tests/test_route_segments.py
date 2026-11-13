"""线路分段：中转段两个阶段、段归属穷举、中转地显示名、按段加权与可信度、非常规件降权，
以及 17TRACK 管理卡片接口、任务详情的外链与登记状态。17TRACK 一律用假接口，不真的调用。"""
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta

import pytest

import env_schema
import forecast
import storage
from conftest import ADMIN_PASSWORD, gate
from test_cn_leg import CN_EVENTS, ENV, KEY, NUM, NUM2, NUM3, US_NUM, Fake17, cn_rows


def dt(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M")


def cn_records(events=CN_EVENTS):
    return [{"source": "cn_ems", **row} for row in cn_rows(events)]


# 只有一段航班：去掉第二条启运与之后的飞机进港
ONE_FLIGHT = [event for event in CN_EVENTS if event[0] < "2026-11-11"] + [CN_EVENTS[0], CN_EVENTS[1]]


# --- 中转段阶段 ---

def test_two_flights_have_transit_stages():
    stages = forecast.stages_from_records(cn_records(), tracking_number=NUM)
    assert stages["flight_departed"] == dt("2026-11-10 22:45")
    assert stages["transit_arrived"] == dt("2026-11-11 11:37")  # 第一条飞机进港
    assert stages["transit_departed"] == dt("2026-11-12 14:41")  # 第二条启运
    assert stages["landed_destination"] == dt("2026-11-12 18:27")


def test_one_flight_has_no_transit_stages():
    stages = forecast.stages_from_records(cn_records(ONE_FLIGHT), tracking_number=NUM)
    assert "flight_departed" in stages
    assert "transit_arrived" not in stages and "transit_departed" not in stages


def test_transit_stages_dedupe_repeated_events_across_tasks():
    doubled = cn_records() + cn_records()  # 同单号两个任务各一份
    assert forecast.transit_stages(doubled) == forecast.transit_stages(cn_records())
    assert forecast.transit_stages(cn_records(ONE_FLIGHT) * 2) == {}


# --- 段归属 ---

def test_every_stage_belongs_to_a_defined_segment_in_order():
    assert list(forecast.SEGMENT_META) == ["warehouse", "domestic", "transit", "japan"]
    indexes = [forecast.SEGMENTS.index(meta["segment"]) for meta in forecast.STAGE_META.values()]
    assert indexes == sorted(indexes)
    assert [s for s in forecast.STAGES if forecast.stage_segment(s) == "warehouse"] == list(forecast.PRE_STAGES)
    assert [s for s in forecast.STAGES if forecast.stage_segment(s) == "transit"] == ["transit_arrived", "transit_departed"]
    assert forecast.stage_segment("flight_departed") == "domestic"
    assert forecast.stage_segment("landed_destination") == "japan"
    assert forecast.STAGE_ORDER["flight_departed"] < forecast.STAGE_ORDER["transit_arrived"] < \
        forecast.STAGE_ORDER["transit_departed"] < forecast.STAGE_ORDER["landed_destination"]


def test_segment_at_splits_by_stage_boundaries():
    stages = forecast.stages_from_records(cn_records(), tracking_number=NUM)
    seg = lambda text: forecast.segment_at(stages, dt(text))  # noqa: E731
    assert seg("2026-11-09 19:31") == "domestic"
    assert seg("2026-11-11 02:00") == "domestic"  # 航班在飞，还没到中转地
    assert seg("2026-11-11 11:37") == "transit"
    assert seg("2026-11-12 16:44") == "transit"  # 第二段航班落地前
    assert seg("2026-11-12 18:27") == "japan"
    assert seg("2026-11-01 00:00") == "domestic"  # 早于所有阶段：取最早阶段的段
    assert forecast.segment_at({}, dt("2026-11-01 00:00")) is None


def test_cn_records_carry_segment(db):
    owner = db.create_account({"username": "ella", "password": "pw-ella", "role": "user"})
    task = db.create_task(owner["id"], {"tracking_number": NUM})
    db.record_cn_records(task["id"], NUM, cn_rows())
    by_status = {}
    for record in db.task_cn_records(task["id"]):
        by_status.setdefault(record["status"], set()).add(record["segment"])
    assert by_status["中国邮政已收取邮件"] == {"domestic"}
    assert by_status["航空公司启运"] == {"domestic", "transit"}  # 第一条在国内段，第二条在中转段
    assert by_status["到达寄达地处理中心"] == {"japan"}


# --- 中转地显示名 ---

def test_transit_region_label_default_and_blank():
    assert env_schema.transit_region_label({}) == "韩国"
    assert env_schema.transit_region_label({"TRANSIT_REGION_LABEL": ""}) == ""
    assert env_schema.transit_region_label({"TRANSIT_REGION_LABEL": " 台湾 "}) == "台湾"
    assert forecast.stage_label("transit_arrived", "韩国") == "到达韩国"
    assert forecast.stage_label("transit_departed", "韩国") == "离开韩国"
    assert forecast.segment_label("transit", "韩国") == "韩国段"
    assert forecast.stage_label("transit_arrived", "") == "到达中转地"
    assert forecast.segment_label("transit", "") == "中转段"
    assert forecast.stage_label("accepted", "韩国") == "引受" and forecast.segment_label("japan", "韩国") == "日本段"


def test_task_forecast_carries_transit_region(db, monkeypatch):
    owner = db.create_account({"username": "ella", "password": "pw-ella", "role": "user"})
    task = db.create_task(owner["id"], {"tracking_number": NUM})
    monkeypatch.delenv("TRANSIT_REGION_LABEL", raising=False)
    assert db.task_forecast(task["id"])["transit_region"] == "韩国"
    monkeypatch.setenv("TRANSIT_REGION_LABEL", "")
    assert db.task_forecast(task["id"])["transit_region"] == ""
    assert db.task_forecast(task["id"], transit_region="台湾")["transit_region"] == "台湾"


def test_settings_page_shows_default_until_cleared(client, db, tmp_path, monkeypatch):
    import app as app_module

    path = tmp_path / ".env"
    path.write_text("")
    monkeypatch.setattr(app_module, "DOTENV_PATH", str(path))
    for key in env_schema.ENV_KEYS:
        monkeypatch.setenv(key, os.environ.get(key, ""))
    monkeypatch.delenv("TRANSIT_REGION_LABEL")
    assert app_module.build_system_env()["TRANSIT_REGION_LABEL"] == "韩国"
    client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD})
    body = client.post("/update_env", json={"TRANSIT_REGION_LABEL": ""}).get_json()
    assert body["env_vars"]["TRANSIT_REGION_LABEL"] == ""
    assert env_schema.transit_region_label() == ""


# --- 按段加权与可信度 ---

T0 = dt("2026-11-01 00:00")


def route_sample(packed_h, customs_h, channel, office, atypical=False):
    return {
        "stages": {
            "ordered": T0,
            "packed": T0 + timedelta(hours=packed_h),
            "accepted": T0 + timedelta(hours=packed_h + 10),
            "japan_customs": T0 + timedelta(hours=customs_h),
        },
        "channel": channel,
        "office": office,
        "atypical": atypical,
    }


def test_weights_by_segment_office_vs_channel():
    """网点相同、渠道不同的样本：邮政段满权重，仓库段只算 0.3。"""
    same_office = route_sample(30, 300, channel="B", office="网点X")
    same_channel = route_sample(20, 200, channel="A", office="网点Y")
    now = dt("2026-11-01 00:00")
    result = forecast.build_forecast({"ordered": T0}, "A", [same_office, same_channel], now, current_office="网点X")
    by_stage = {item["stage"]: item for item in result["remaining"]}
    # 仓库段比渠道：同渠道那单 1.0、同网点那单 0.3 → 中位数取同渠道的 20h
    assert by_stage["packed"]["basis"] == "channel" and by_stage["packed"]["segment"] == "warehouse"
    assert by_stage["packed"]["median_seconds"] == 20 * 3600
    assert by_stage["packed"]["samples"] == {"total": 2, "similar": 1, "atypical": 0}
    # 日本段比网点：同网点那单 1.0 → 中位数取它的 300h
    assert by_stage["japan_customs"]["basis"] == "office" and by_stage["japan_customs"]["segment"] == "japan"
    assert by_stage["japan_customs"]["median_seconds"] == 300 * 3600
    assert by_stage["accepted"]["basis"] == "office"
    assert result["samples"]["same_channel"] == 1 and result["samples"]["same_office"] == 1


def test_office_basis_falls_back_to_channel_when_missing():
    no_office = route_sample(30, 300, channel="A", office=None)  # 样本缺网点：按渠道比，同渠道 → 满权重
    other = route_sample(20, 200, channel="B", office="网点Y")
    result = forecast.build_forecast({"ordered": T0}, "A", [no_office, other], T0, current_office="网点X")
    customs = next(item for item in result["remaining"] if item["stage"] == "japan_customs")
    assert customs["median_seconds"] == 300 * 3600 and customs["samples"]["similar"] == 1
    # 当前单缺网点：所有段都按渠道比
    result = forecast.build_forecast({"ordered": T0}, "A", [no_office, other], T0)
    assert {item["basis"] for item in result["remaining"]} == {"channel"}


def test_confidence_uses_first_remaining_segment():
    now = T0
    # 当前在仓库段（ordered）：第一个剩余阶段 packed 在仓库段 → 按渠道数同类
    samples = [route_sample(20, 200, channel="B", office="网点X") for _ in range(5)]
    result = forecast.build_forecast({"ordered": T0}, "A", samples, now, current_office="网点X")
    assert result["basis"] == "channel" and result["samples"]["similar"] == 0 and result["confidence"] == "borrowed"
    # 当前在国内段（accepted）：第一个剩余阶段 japan_customs 在日本段 → 按网点数同类，渠道不同也算
    current = {"ordered": T0, "packed": T0 + timedelta(hours=1), "accepted": T0 + timedelta(hours=2)}
    result = forecast.build_forecast(current, "A", samples, T0 + timedelta(hours=2), current_office="网点X")
    assert result["basis"] == "office" and result["samples"]["similar"] == 5 and result["confidence"] == "solid"
    result = forecast.build_forecast(current, "A", samples[:2], T0 + timedelta(hours=2), current_office="网点X")
    assert result["confidence"] == "rough"
    assert forecast.build_forecast(current, "A", [], T0, current_office="网点X")["confidence"] == "none"


# --- 非常规件 ---

def test_atypical_sample_downweighted():
    current = {"ordered": T0, "packed": T0 + timedelta(hours=1), "accepted": T0 + timedelta(hours=2)}
    now = T0 + timedelta(hours=2)
    big = route_sample(1, 200, channel="A", office="网点X", atypical=True)
    normal = route_sample(1, 300, channel="A", office="网点X")
    result = forecast.build_forecast(current, "A", [big, normal], now, current_office="网点X")
    customs = result["remaining"][0]
    # 离开引受后的耗时 189h（非常规）与 289h（常规），权重 1.0×0.3 与 1.0：中位数落在常规那单
    assert customs["median_seconds"] == 289 * 3600
    assert customs["samples"] == {"total": 2, "similar": 2, "atypical": 1}
    assert result["samples"]["atypical"] == 1 and result["samples"]["similar_atypical"] == 1
    big["atypical"] = False
    assert forecast.build_forecast(current, "A", [big, normal], now, current_office="网点X")["remaining"][0]["median_seconds"] == 189 * 3600
    assert forecast.ATYPICAL_WEIGHT == 0.3


def test_current_atypical_does_not_change_other_weights(db):
    owner = db.create_account({"username": "ella", "password": "pw-ella", "role": "user"})
    current = db.create_task(owner["id"], {"tracking_number": NUM})
    db.update_task(current["id"], {"atypical": True})
    other = db.create_task(owner["id"], {"tracking_number": NUM2})
    db.record_carrier_records(other["id"], NUM2, [{"info": "2026/10/18 11:11 引受", "detail": "CHINA"}])
    with closing(storage._connect()) as conn:
        samples = storage._forecast_samples(conn, NUM)
    assert [sample["atypical"] for sample in samples] == [False]


def test_atypical_merge_any_task_marks_sample(db):
    owner = db.create_account({"username": "ella", "password": "pw-ella", "role": "user"})
    fred = db.create_account({"username": "fred", "password": "pw-fred", "role": "user"})
    a = db.create_task(owner["id"], {"tracking_number": NUM2})
    b = db.create_task(fred["id"], {"tracking_number": NUM2})
    db.record_carrier_records(a["id"], NUM2, [{"info": "2026/10/18 11:11 引受", "detail": "CHINA"}])
    db.update_task(b["id"], {"atypical": True})
    with closing(storage._connect()) as conn:
        samples = storage._forecast_samples(conn, NUM)
    assert len(samples) == 1 and samples[0]["atypical"] is True
    assert db.get_task(b["id"])["atypical"] is True and db.get_task(a["id"])["atypical"] is False


def test_atypical_column_migration_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    storage._ensure_accounts_schema(conn)
    storage._ensure_tasks_schema(conn)
    conn.execute("INSERT INTO accounts (id, username, role, created_at, updated_at) VALUES (7, 'old', 'user', 'x', 'x')")
    conn.execute(
        "INSERT INTO tracking_tasks (id, account_id, tracking_number, first_seen_at, created_at, updated_at)"
        f" VALUES (3, 7, '{NUM}', 'x', 'x', 'x')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(storage, "DB_PATH", str(db_path))
    storage.ensure_storage(str(tmp_path / "missing.env"))
    storage.ensure_storage(str(tmp_path / "missing.env"))
    assert storage.get_task(3)["atypical"] is False
    with sqlite3.connect(db_path) as c:
        cols = [row[1] for row in c.execute("PRAGMA table_info(tracking_tasks)")]
    assert cols.count("atypical") == 1


def test_atypical_api_and_forms(client, db, monkeypatch):
    owner = db.create_account({"username": "ella", "password": "pw-ella", "role": "user"})
    task = db.create_task(owner["id"], {"tracking_number": NUM})
    client.post("/login", data={"username": "ella", "password": "pw-ella"})
    assert client.put(f"/api/tasks/{task['id']}", json={"atypical": True}).status_code == 200
    assert db.get_task(task["id"])["atypical"] is True
    for bad in ("yes", 1, None):
        assert client.put(f"/api/tasks/{task['id']}", json={"atypical": bad}).status_code == 400
    assert db.get_task(task["id"])["atypical"] is True
    # 门户编辑表单：带隐藏标记、未勾选 = 取消；不带标记（恢复表单等）不动
    form = {"tracking_number": NUM, "label": "", "check_interval": "300", "enabled": "on"}
    client.post(f"/me/tasks/{task['id']}/update", data={**form, "atypical_field": "1"})
    assert db.get_task(task["id"])["atypical"] is False
    client.post(f"/me/tasks/{task['id']}/update", data={**form, "atypical_field": "1", "atypical": "on"})
    assert db.get_task(task["id"])["atypical"] is True
    client.post(f"/me/tasks/{task['id']}/update", data=form)
    assert db.get_task(task["id"])["atypical"] is True
    body = client.get(f"/api/tasks/{task['id']}/forecast").get_json()
    assert body["task"] == {"archived": False, "atypical": True}


def test_v1_does_not_expose_atypical(client, db):
    import api_v1

    owner = db.create_account({"username": "ella", "password": "pw-ella", "role": "user"})
    task = db.create_task(owner["id"], {"tracking_number": NUM})
    db.update_task(task["id"], {"atypical": True})
    assert "atypical" not in api_v1.tracking_view(db.get_task(task["id"]))


# --- 17TRACK 管理卡片 ---

@pytest.fixture
def fake17(monkeypatch):
    import app as app_module

    app_module._track17_quota_cache.clear()
    return Fake17(monkeypatch)


@pytest.fixture
def key_env(monkeypatch):
    import app as app_module

    env = {}
    monkeypatch.setattr(app_module, "load_system_env", lambda path, keys: {key: env.get(key, "") for key in keys})
    return env


def admin_login(client):
    client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD})


def test_track17_card_permissions(client, db, fake17, key_env):
    key_env.update(ENV)
    json_headers = {"Accept": "application/json"}
    # 未登录、伪造身份头（SSO 未开时不信任）都拿不到
    assert client.get("/api/admin/track17", headers=json_headers).status_code != 200
    forged = {**gate("admin", role="admin", sub="sub-admin"), **json_headers}
    assert client.get("/api/admin/track17", headers=forged).status_code != 200
    assert client.get("/api/admin/track17", headers={**forged, "Authorization": "Bearer anything"}).status_code != 200
    db.create_account({"username": "ella", "password": "pw-ella", "role": "user"})
    client.post("/login", data={"username": "ella", "password": "pw-ella"})
    assert client.get("/api/admin/track17", headers=json_headers).status_code == 403
    assert fake17.calls == []


def test_track17_card_unconfigured_failed_and_ok(client, db, fake17, key_env):
    admin_login(client)
    body = client.get("/api/admin/track17").get_json()
    assert body["configured"] is False and body["quota"] is None and fake17.calls == []
    assert body["console_url"] == "https://api.17track.net"

    key_env.update(ENV)
    fake17.fail = True
    response = client.get("/api/admin/track17")
    body = response.get_json()
    assert response.status_code == 200 and body["configured"] is True and body["quota_ok"] is False
    assert KEY not in response.get_data(as_text=True)

    fake17.fail = False
    storage.track17_update(NUM, registered=True, error="")
    storage.track17_update(NUM2, registered=True, synced=True, error="")
    storage.track17_update(NUM3, registered=True, error="坏响应")
    storage.track17_update("EB100000009CN", error="登记被拒")  # 没登记成功：不计入已登记
    body = client.get("/api/admin/track17").get_json()
    assert body["quota_ok"] is True and body["quota"]["quota_remain"] == 97
    numbers = body["numbers"]
    assert (numbers["registered"], numbers["pending"], numbers["synced"], numbers["error"]) == (3, 1, 1, 1)
    assert numbers["last_synced_at"] and numbers["last_error"]["error"] in ("坏响应", "登记被拒")


def test_track17_quota_cached_for_five_minutes(client, db, fake17, key_env, monkeypatch):
    import app as app_module

    key_env.update(ENV)
    admin_login(client)
    clock = {"now": 1000.0}
    # 只让缓存看到假时钟：登录会话等其他地方照用真时间
    monkeypatch.setattr(app_module, "_cache_clock", lambda: clock["now"])
    client.get("/api/admin/track17")
    client.get("/api/admin/track17")
    assert fake17.count("getquota") == 1
    clock["now"] += app_module.TRACK17_QUOTA_TTL - 1
    client.get("/api/admin/track17")
    assert fake17.count("getquota") == 1
    clock["now"] += 2
    client.get("/api/admin/track17")
    assert fake17.count("getquota") == 2
    # 失败不缓存：下次照常再查
    fake17.fail = True
    clock["now"] += app_module.TRACK17_QUOTA_TTL + 1
    assert client.get("/api/admin/track17").get_json()["quota_ok"] is False
    fake17.fail = False
    assert client.get("/api/admin/track17").get_json()["quota_ok"] is True


# --- 任务详情：外链与登记状态 ---

def test_forecast_links_and_track17_status(client, db, fake17, key_env):
    owner = db.create_account({"username": "ella", "password": "pw-ella", "role": "user"})
    cn = db.create_task(owner["id"], {"tracking_number": NUM})
    us = db.create_task(owner["id"], {"tracking_number": US_NUM})
    client.post("/login", data={"username": "ella", "password": "pw-ella"})

    body = client.get(f"/api/tasks/{cn['id']}/forecast").get_json()  # 未配密钥：只有跳转链接
    assert body["links"]["track17"] == f"https://t.17track.net/zh-cn#nums={NUM}"
    assert body["links"]["jp_post"] == db.get_task(cn["id"])["tracking_url"]
    assert body["track17"] is None

    key_env.update(ENV)
    body = client.get(f"/api/tasks/{cn['id']}/forecast").get_json()
    assert body["track17"] == {"status": "unregistered", "registered_at": None, "last_synced_at": None, "error": None}
    storage.track17_update(NUM, registered=True, error="")
    assert client.get(f"/api/tasks/{cn['id']}/forecast").get_json()["track17"]["status"] == "pending"
    storage.track17_update(NUM, synced=True, error="")
    assert client.get(f"/api/tasks/{cn['id']}/forecast").get_json()["track17"]["status"] == "synced"
    storage.track17_update(NUM, error="连接超时")
    view = client.get(f"/api/tasks/{cn['id']}/forecast").get_json()["track17"]
    assert view["status"] == "error" and view["error"] == "连接超时"

    body = client.get(f"/api/tasks/{us['id']}/forecast").get_json()  # 非 CN：没有 17TRACK
    assert body["links"]["track17"] is None and body["track17"] is None
    assert fake17.calls == []


# --- 前后端映射一致 ---

def _js_source():
    path = os.path.join(os.path.dirname(storage.__file__), "static", "forecast.js")
    return open(path, encoding="utf-8").read()


def test_js_track17_status_map_matches_python():
    block = re.search(r"export const TRACK17_STATUS_META = Object\.freeze\(\{(.*?)\}\);", _js_source(), re.S).group(1)
    found = re.findall(r"(\w+): \{ label: '([^']+)', tone: '(\w+)' \}", block)
    assert {key: {"label": label, "tone": tone} for key, label, tone in found} == storage.TRACK17_STATUS_META

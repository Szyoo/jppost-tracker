"""合并履历与推送：日本邮政状态汉化、带方向与地点的节点名、跨来源归并（±2 分钟、时区换算后）、段主来源、
推送白名单与去重（哪边先报推哪边、另一边晚到不再推）、同轮只推最新、失败重推、上线迁移不补发、推送附带的到达预测。

样本按真实的一单（中国寄日本的大件，经韩国中转两段航班）两边履历的结构造，单号虚构；17TRACK 与日本邮政都用假网络。"""
import sqlite3
from datetime import datetime

import pytest

import forecast
import jp_status
import route_nodes
import storage
from test_cn_leg import CN_EVENTS, ENV, NUM, Fake17, cn_rows
from test_task_events import Net


def page_html(*rows):
    """仿日本邮政履历表，detail 原样放进一个单元格（「東京国際郵便局 · 東京都」解析后就是它本身）。"""
    cells = "".join(
        f'<tr><td class="w_120">{date}</td><td class="w_150">{status}</td><td>{detail}</td></tr>'
        for date, status, detail in rows
    )
    return f'<html><body><table class="tableType01 txt_c m_b5" summary="履歴情報">{cells}</table></body></html>'

BARK = {"BARK_SERVER": "http://bark.example.com"}

# 日本邮政那一侧（页面原样：始发国几行是中国当地时间）
JP_ROWS = [
    ("2026/11/09 18:31", "引受", "CHINA"),
    ("2026/11/10 07:45", "国際交換局に到着", "WEIHAI D EMS · CHINA"),
    ("2026/11/10 07:45", "税関検査のため税関へ提示", "CHINA"),
    ("2026/11/10 12:32", "税関から受領", "CHINA"),
    ("2026/11/10 12:32", "国際交換局から発送", "WEIHAI D EMS · CHINA"),
    ("2026/11/13 07:18", "国際交換局に到着", "東京国際郵便局 · 東京都"),
    ("2026/11/13 09:00", "通関手続中", "東京国際郵便局 · 東京都"),
    ("2026/11/13 15:10", "国際交換局から発送", "東京国際郵便局 · 東京都"),
    ("2026/11/14 06:05", "到着", "配達予定日：11月14日 · 板橋北郵便局 · 東京都"),
    ("2026/11/14", "持ち出し中", "板橋北郵便局 · 東京都"),
    ("2026/11/14 10:06", "お届け済み", "板橋北郵便局 · 東京都"),
]
# 中国邮政那一侧到日本以后的事件（17TRACK 标成 +08:00，其实是日本时间）
CN_JAPAN = [
    ("2026-11-14T10:05:00+08:00", "【日本】已妥投", ""),
    ("2026-11-14T08:30:00+08:00", "到达境外自提点", ""),
    ("2026-11-14T06:05:00+08:00", "已到达【日本】投递局", ""),
    ("2026-11-13T15:10:00+08:00", "离开【日本】处理中心", ""),
    ("2026-11-13T12:00:00+08:00", "寄达地海关允许进口", ""),
    ("2026-11-13T09:00:00+08:00", "境外进口海关留存待验", ""),
    ("2026-11-13T08:59:00+08:00", "送交境外进口海关", ""),
]


def jp_records(rows=JP_ROWS):
    result = []
    for date, status, detail in rows:
        record_at, parsed = forecast.parse_record_info(f"{date} {status}")
        result.append({"source": "jp_post", "record_at": record_at, "status": parsed, "detail": detail})
    return result


def cn_records(events=CN_EVENTS):
    return [{"source": "cn_ems", **row} for row in cn_rows(events)]


def nodes_of(records, number=NUM, region="韩国"):
    return route_nodes.build_nodes(records, tracking_number=number, transit_region=region)


def by_name(nodes):
    return {node["name"]: node for node in nodes}


# --- 汉化 ---

def test_status_translation_and_detail():
    zh = jp_status.status_zh
    assert zh("引受") == "收寄" and zh("通関手続中") == "通关中" and zh("お届け済み") == "已妥投"
    assert zh("ご不在のため持ち戻り") == "无人在家已带回" and zh("配達希望受付") == "已受理指定投递"
    assert jp_status.detail_zh("配達予定日：10月27日 · 板橋北郵便局 · 東京都") == "预计投递日：10月27日 · 板橋北郵便局 · 東京都"
    assert jp_status.info_zh("2026/10/27 13:50 お届け済み") == "2026/10/27 13:50 已妥投"
    assert jp_status.info_push("2026/10/26 04:30 通関手続中") == "2026/10/26 04:30 通关中（通関手続中）"


def test_unknown_status_shown_as_is_and_logged_once(capsys):
    jp_status._logged_unknown.discard("未知の状態")
    assert jp_status.status_zh("未知の状態") == "未知の状態"
    assert jp_status.status_zh("未知の状態") == "未知の状態"
    assert capsys.readouterr().out.count("未收录的日本邮政状态：未知の状態") == 1
    assert jp_status.info_push("2026/10/26 04:30 未知の状態") == "2026/10/26 04:30 未知の状態"


# --- 节点名：方向与地点 ---

def test_node_names_carry_direction_and_place():
    names = by_name([n for n in nodes_of(cn_records(CN_EVENTS + CN_JAPAN) + jp_records()) if n["visible"]])
    for expected in (
        "中国收寄",
        "提交中国出口海关查验（威海）",
        "中国出口海关放行（威海）",
        "离开威海国际互换局（出境）",
        "离开揽收网点 威海市丹东路第二揽投部 → 威海国际互换局",
        "威海国际互换局 发往机场",
        "交航（威海）",
        "航空公司接收（威海）",
        "中国→韩国 航班起飞",
        "到达韩国中转",
        "韩国→日本 航班起飞",
        "韩国→日本 航班落地",
        "日本邮政接收（寄达地）",
        "提交日本进口海关（東京国際郵便局）",
        "日本进口海关放行（東京国際郵便局）",
        "到达東京国際郵便局（入境）",
        "日本进口通关中（東京国際郵便局）",
        "离开東京国際郵便局",
        "到达板橋北郵便局",
        "板橋北郵便局 派送中",
        "已妥投（板橋北郵便局）",
    ):
        assert expected in names, expected
    assert not any(name.startswith("提交海关查验") for name in names)
    # 显示给人的名字不是两边原文
    raw = {status for _, status, _ in CN_EVENTS + CN_JAPAN} | {status for _, status, _ in JP_ROWS}
    assert not raw & set(names)
    assert names["韩国→日本 航班落地"]["segment"] == "japan"


def test_names_without_transit_region_and_unknown_country():
    names = by_name(nodes_of(cn_records(), region=""))
    assert "中国→中转地 航班起飞" in names and "中转地→日本 航班起飞" in names
    other = route_nodes.build_nodes(jp_records(JP_ROWS[:1]), tracking_number="EB000000001ZZ")
    assert other[0]["name"] == "ZZ收寄"  # 没有中文名的国家码用代码本身
    assert route_nodes.exchange_name_from_detail("SHANGHAI EMS · CHINA") == "SHANGHAI EMS"  # 未收录的互换局原样显示


# --- 归并与显示 ---

def test_shared_nodes_merge_and_segment_primary():
    nodes = nodes_of(cn_records(CN_EVENTS + CN_JAPAN) + jp_records())
    visible = by_name([n for n in nodes if n["visible"]])
    # 共有节点：两边一致（时区换算后：引受 18:31 中国时间 = 中国邮政 18:31:38 +08:00）
    assert visible["中国收寄"]["sources"] == ["cn_ems", "jp_post"] and visible["中国收寄"]["at"] == "2026-11-09 19:31"
    assert visible["到达東京国際郵便局（入境）"]["sources"] == ["cn_ems", "jp_post"]  # 07:17 与 07:18
    assert visible["已妥投（板橋北郵便局）"]["sources"] == ["cn_ems", "jp_post"]
    assert visible["已妥投（板橋北郵便局）"]["original"] == "お届け済み"
    # 段主来源：国内段的日本邮政独有事件（到达威海互换局）不显示；日本段的中国邮政独有事件（自提点）不显示
    hidden = {n["name"] for n in nodes if not n["visible"]}
    assert "到达威海国际互换局" in hidden and "到达自提点" in hidden
    # 先按语义再按时间：通关中（09:00）两边合并，提交进口海关（08:59，只有中国邮政）独立成节点；放行虽只有中国邮政也显示
    assert visible["日本进口通关中（東京国際郵便局）"]["sources"] == ["cn_ems", "jp_post"]
    assert visible["提交日本进口海关（東京国際郵便局）"]["sources"] == ["cn_ems"]
    assert visible["日本进口海关放行（東京国際郵便局）"]["sources"] == ["cn_ems"]
    collect = visible["离开揽收网点 威海市丹东路第二揽投部 → 威海国际互换局"]
    assert collect["at"] == "2026-11-09 19:54" and collect["sources"] == ["cn_ems"]
    # 日本段的日期只有一天的「持ち出し中」与中国邮政的事件不强行合并，按日本邮政显示
    assert visible["板橋北郵便局 派送中"]["sources"] == ["jp_post"]
    assert visible["到达板橋北郵便局"]["detail"] == "预计投递日：11月14日 · 板橋北郵便局 · 東京都"


def test_secondary_shared_node_shown_before_primary_reports():
    """日本段以日本邮政为主，但中国邮政先报了「到达寄达地处理中心」：先显示（标中邮），日本邮政到了再合并。"""
    early = nodes_of(cn_records() + jp_records(JP_ROWS[:5]))
    node = next(n for n in early if n["kind"] == "arrive_japan_exchange")
    assert node["visible"] and node["sources"] == ["cn_ems"]
    later = nodes_of(cn_records() + jp_records(JP_ROWS[:6]))
    node = next(n for n in later if n["kind"] == "arrive_japan_exchange")
    assert node["sources"] == ["cn_ems", "jp_post"]


def test_without_cn_data_everything_is_jp():
    nodes = nodes_of(jp_records())
    assert all(n["visible"] and n["sources"] == ["jp_post"] for n in nodes)
    assert by_name(nodes)["到达威海国际互换局"]["segment"] == "domestic"
    assert by_name(nodes)["到达東京国際郵便局（入境）"]["segment"] == "japan"


@pytest.mark.parametrize("minute, merged", [("07:19", True), ("07:20", False)])
def test_two_minute_boundary(minute, merged):
    rows = [("2026/11/13 " + minute, "国際交換局に到着", "東京国際郵便局 · 東京都")]
    nodes = [n for n in nodes_of(cn_records() + jp_records(rows)) if n["kind"] == "arrive_japan_exchange"]
    assert (len(nodes) == 1) is merged


def test_node_key_stable_and_notified_matching():
    jp_first = next(n for n in nodes_of(cn_records(CN_EVENTS[1:]) + jp_records(JP_ROWS[5:6])) if n["kind"] == "arrive_japan_exchange")
    both = next(n for n in nodes_of(cn_records() + jp_records(JP_ROWS[5:6])) if n["kind"] == "arrive_japan_exchange")
    assert jp_first["key"] == "arrive_japan_exchange@2026-11-13 07:18"
    assert both["key"] == "arrive_japan_exchange@2026-11-13 07:17"
    assert route_nodes.node_notified(both, {jp_first["key"]})  # 另一边晚到、早一分钟：仍是同一节点
    assert not route_nodes.node_notified(both, {"arrive_japan_exchange@2026-11-13 09:00"})


# --- 推送 ---

@pytest.fixture
def owner(db):
    return db.create_account({"username": "ella", "password": "pw-ella", "role": "user", "bark_keys": "key-1"})


@pytest.fixture
def cn_task(db, owner, monkeypatch):
    task = db.create_task(owner["id"], {"tracking_number": NUM})
    fake = Fake17(monkeypatch)
    fake.registered.add(NUM)
    storage.track17_update(NUM, registered=True, error="")
    return task, fake


def run(tracker, task_id, env=None):
    due = next(t for t in storage.list_due_tasks() if t["id"] == task_id)
    return tracker.process_task(due, {**ENV, **BARK, **(env or {})})


def ready(fake, events):
    from test_cn_leg import track_info

    fake.ready[NUM] = track_info(events)


def test_arrived_japan_pushed_once_cn_first(cn_task, monkeypatch):
    """「日本邮政接收（寄达地）」先到：推它；之后两边报的「到达東京（入境）」都只记录。"""
    import tracker

    task, fake = cn_task
    net = Net(tracker, monkeypatch)
    ready(fake, CN_EVENTS[1:])  # 中国邮政到「到达寄达地」为止
    net.page = page_html(*JP_ROWS[:5])
    run(tracker, task["id"])
    assert len(net.pushes) == 1 and net.titles[-1] == "日本邮政接收（寄达地） · 01CN"
    net.page = page_html(*JP_ROWS[:6])  # 日本邮政报到达东京
    run(tracker, task["id"])
    ready(fake, CN_EVENTS)  # 中国邮政晚到（07:17）
    run(tracker, task["id"])
    assert len(net.pushes) == 1
    assert any(key.startswith("arrive_japan_exchange@") for key in storage.notified_node_keys(task["id"], NUM))


def test_arrived_japan_pushed_once_jp_first(cn_task, monkeypatch):
    """日本邮政先报「到达東京（入境）」：推它；中国邮政晚到的「到达寄达地」更早，只记录不补发。"""
    import tracker

    task, fake = cn_task
    net = Net(tracker, monkeypatch)
    ready(fake, CN_EVENTS[2:])  # 中国邮政到航班落地为止
    net.page = page_html(*JP_ROWS[:5])
    run(tracker, task["id"])
    assert net.titles[-1] == "离开威海国际互换局（出境） · 01CN"  # 航班事件不在白名单里
    net.page = page_html(*JP_ROWS[:6])
    run(tracker, task["id"])
    assert len(net.pushes) == 2 and net.titles[-1] == "到达東京国際郵便局（入境） · 01CN"
    ready(fake, CN_EVENTS)
    run(tracker, task["id"])
    assert len(net.pushes) == 2


def test_whitelist_only_latest_pushed_rest_marked(cn_task, monkeypatch):
    import tracker

    task, fake = cn_task
    net = Net(tracker, monkeypatch)
    ready(fake, CN_EVENTS[-1:])
    net.page = page_html(*JP_ROWS[:1])
    run(tracker, task["id"])
    assert net.titles == ["中国收寄 · 01CN"]
    ready(fake, CN_EVENTS[-3:])  # 揽收网点分拣、离开：不在白名单里，只记录
    run(tracker, task["id"])
    assert len(net.pushes) == 1
    keys = storage.notified_node_keys(task["id"], NUM)
    assert any(key.startswith("left_collection@") for key in keys)
    ready(fake, CN_EVENTS[-8:])  # 再来一批：出口海关、出口直封（出境）、发往机场
    run(tracker, task["id"])
    assert len(net.pushes) == 2 and net.titles[-1] == "离开威海国际互换局（出境） · 01CN"
    events, _ = storage.list_task_events(task["id"], limit=100)
    assert [e["status"] for e in events if e["kind"] == "push_ok"][0] == "离开威海国际互换局（出境）"
    # 同轮其余节点（海关、发往机场）也记为已处理，之后不再推
    run(tracker, task["id"])
    assert len(net.pushes) == 2
    assert len(storage.notified_node_keys(task["id"], NUM)) == len(
        route_nodes.pushable_nodes(storage.task_nodes(task["id"], "韩国")))


def test_push_failure_retries_same_nodes(cn_task, monkeypatch):
    import tracker

    task, fake = cn_task
    net = Net(tracker, monkeypatch)
    ready(fake, CN_EVENTS[-1:])
    net.page = page_html(*JP_ROWS[:1])
    run(tracker, task["id"])
    net.push_ok = False
    ready(fake, CN_EVENTS[-8:])
    assert run(tracker, task["id"]) is True
    assert len(storage.notified_node_keys(task["id"], NUM)) == 1  # 失败：本轮节点都不记
    net.push_ok = True
    assert run(tracker, task["id"]) is False
    assert net.titles[-1] == net.titles[-2] == "离开威海国际互换局（出境） · 01CN"  # 重推的是同一个节点
    run(tracker, task["id"])
    assert len(net.pushes) == 3  # 1 次成功 + 1 次失败 + 1 次重推成功，之后不再推


def test_late_cn_backlog_not_pushed(cn_task, monkeypatch):
    """17TRACK 数据晚到：早于已推送节点的国内段事件静默记下，不补发。"""
    import tracker

    task, fake = cn_task
    net = Net(tracker, monkeypatch)
    net.page = page_html(*JP_ROWS[:6])
    run(tracker, task["id"])  # 首次：只推最新（到达东京）
    ready(fake, CN_EVENTS)
    run(tracker, task["id"])
    assert len(net.pushes) == 1


def test_migration_marks_existing_nodes_without_pushing(tmp_path, monkeypatch):
    import tracker

    db_path = tmp_path / "old.db"
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(storage, "DB_PATH", str(db_path))
    storage.ensure_storage(str(tmp_path / "missing.env"))
    owner = storage.create_account({"username": "ella", "password": "pw-ella", "role": "user", "bark_keys": "key-1"})
    task = storage.create_task(owner["id"], {"tracking_number": NUM})
    storage.update_task_state(task["id"], latest_info=f"{JP_ROWS[4][0]} {JP_ROWS[4][1]}", tracking_number=NUM,
                              carrier_records=[{"info": f"{d} {s}", "detail": x} for d, s, x in JP_ROWS[:5]])
    storage.record_cn_records(task["id"], NUM, cn_rows(CN_EVENTS[1:]))
    with sqlite3.connect(db_path) as conn:  # 模拟上线前的库：还没有 notified_nodes
        conn.execute("DROP TABLE notified_nodes")
    storage.ensure_storage(str(tmp_path / "missing.env"))
    assert len(storage.notified_node_keys(task["id"], NUM)) > 5
    net = Net(tracker, monkeypatch)
    net.page = page_html(*JP_ROWS[:5])
    run(tracker, task["id"], {"TRACK17_API_KEY": ""})
    assert net.pushes == []
    storage.ensure_storage(str(tmp_path / "missing.env"))  # 再次启动不重复标记、不报错


def test_no_cn_number_push(db, owner, monkeypatch):
    import tracker

    task = db.create_task(owner["id"], {"tracking_number": "AB123456789JP", "label": "电饭煲"})
    net = Net(tracker, monkeypatch)
    net.page = page_html(("2026/10/01 10:00", "引受", ""), ("2026/10/01 18:00", "通過", ""))
    run(tracker, task["id"], {"TRACK17_API_KEY": ""})
    assert net.titles == ["日本收寄 · 电饭煲"]  # 「经过」不在白名单里，首次抓取推最新的白名单节点
    assert "\n" not in net.pushes[0] and net.pushes[0].startswith("第 ")


def test_unknown_and_anomaly_jp_status_roles():
    nodes = nodes_of(jp_records(JP_ROWS[:9] + [("2026/11/14 12:00", "ご不在のため持ち戻り", "板橋北郵便局 · 東京都"),
                                               ("2026/11/14 13:00", "配達希望受付", "板橋北郵便局 · 東京都")]))
    roles = {n["name"]: n["role"] for n in nodes}
    assert roles["无人在家已带回（板橋北郵便局）"] == "anomaly"
    assert roles["已受理指定投递（板橋北郵便局）"] is None
    assert roles["离开東京国際郵便局"] == "departed_exchange" and roles["到达板橋北郵便局"] == "local_arrived"
    assert roles["日本进口通关中（東京国際郵便局）"] is None and roles["到达威海国际互换局"] is None


# --- 推送正文（一行） ---

NOW = datetime(2026, 11, 13, 9, 30)


def _result(**overrides):
    base = {
        "current": {"stage": "arrived_japan", "at": "2026-11-13 07:17", "waited_seconds": 3600},
        "origin": {"stage": "accepted", "at": "2026-11-09 19:31"},
        "day": 5, "delivered": False, "total_seconds": None, "confidence": "rough", "overdue": False,
        "remaining": [{"stage": "delivered", "eta": "2026-11-14 18:30", "eta_low": "2026-11-14 15:40", "eta_high": "2026-11-14 18:40"}],
    }
    base.update(overrides)
    return base


def _eta(at):
    return [{"stage": "delivered", "eta": at, "eta_low": at, "eta_high": at}]


def test_push_body_formats():
    body = forecast.push_body
    assert body(_result(), NOW) == "第 5 天 · 预计明天晚上签收"
    assert body(_result(remaining=_eta("2026-11-13 15:00")), NOW) == "第 5 天 · 预计今天下午签收"
    assert body(_result(remaining=_eta("2026-11-14 10:00")), NOW) == "第 5 天 · 预计明天上午签收"
    assert body(_result(remaining=_eta("2026-11-15 03:00")), NOW) == "第 5 天 · 预计 11/15（周日）凌晨签收"
    assert body(_result(remaining=_eta("2026-11-16 12:00")), NOW) == "第 5 天 · 预计 11/16（周一）下午签收"
    assert body(_result(confidence="none"), NOW) == "第 5 天"
    assert body(_result(remaining=[]), NOW) == "第 5 天"
    assert body(_result(overdue=True), NOW) == "第 5 天 · 比以往都慢"
    assert body(_result(), NOW, with_forecast=False) == "第 5 天"
    assert body(_result(origin=None), NOW) == "预计明天晚上签收"
    done = dict(current={"stage": "delivered", "at": "2026-11-14 10:06", "waited_seconds": 0}, delivered=True, day=None)
    assert body(_result(**done, total_seconds=4 * 86400 + 15 * 3600), NOW) == "全程 4.5 天"
    assert body(_result(**done, total_seconds=4 * 86400 + 18 * 3600), NOW) == "全程 5 天"
    assert body(_result(**done, total_seconds=4 * 86400 + 5 * 3600), NOW) == "全程 4 天"
    assert body(_result(current=None), NOW) == ""
    assert [forecast.day_part(h) for h in (0, 5, 6, 11, 12, 17, 18, 23)] == ["凌晨", "凌晨", "上午", "上午", "下午", "下午", "晚上", "晚上"]


def test_push_body_switch_and_failure(db, owner, monkeypatch):
    import tracker

    task = db.create_task(owner["id"], {"tracking_number": "AB123456789JP"})
    net = Net(tracker, monkeypatch)
    monkeypatch.setattr(tracker, "task_forecast", lambda *a, **k: _result(remaining=_eta("2099-01-01 10:00")))
    net.page = page_html(("2026/10/01 10:00", "引受", ""))
    run(tracker, task["id"], {"TRACK17_API_KEY": ""})
    assert net.titles[-1] == "日本收寄 · 89JP" and "预计 1/1（周四）上午签收" in net.pushes[-1]  # 默认开

    net.page = page_html(("2026/10/01 10:00", "引受", ""), ("2026/10/02 08:00", "到着", ""))
    run(tracker, task["id"], {"TRACK17_API_KEY": "", "PUSH_FORECAST": "0"})
    assert net.pushes[-1].startswith("第 ") and "预计" not in net.pushes[-1]

    def boom(*args, **kwargs):
        raise RuntimeError("预测坏了")

    monkeypatch.setattr(tracker, "task_forecast", boom)
    net.page = page_html(("2026/10/01 10:00", "引受", ""), ("2026/10/02 08:00", "到着", ""), ("2026/10/02 09:00", "持ち出し中", ""))
    assert run(tracker, task["id"], {"TRACK17_API_KEY": ""}) is False
    assert len(net.pushes) == 3 and net.pushes[-1] == "10/2 09:00"  # 预测失败照常推送，正文用节点时间


def test_history_endpoint_and_display_fields(client, db, owner):
    task = db.create_task(owner["id"], {"tracking_number": NUM})
    storage.update_task_state(task["id"], latest_info=f"{JP_ROWS[2][0]} {JP_ROWS[2][1]}", tracking_number=NUM,
                              carrier_records=[{"info": f"{d} {s}", "detail": x} for d, s, x in JP_ROWS[:3]],
                              new_records=[{"info": f"{d} {s}", "detail": x} for d, s, x in JP_ROWS[:3]])
    assert storage.get_task(task["id"])["last_status_display"] == "2026/11/10 08:45 提交中国出口海关查验（威海）"
    events, _ = storage.list_task_events(task["id"])
    assert events[0]["status_display"] == "2026/11/10 08:45 提交中国出口海关查验（威海）"
    client.post("/login", data={"username": "ella", "password": "pw-ella"})
    history = client.get(f"/api/tasks/{task['id']}/forecast").get_json()["history"]
    assert [node["name"] for node in history] == ["提交中国出口海关查验（威海）", "到达威海国际互换局", "中国收寄"]
    assert set(history[0]) == {"key", "at", "segment", "stage", "name", "sources", "original", "detail"}
    assert datetime.strptime(history[0]["at"], "%Y-%m-%d %H:%M")


# --- 生产库副本发现的问题 ---

def test_pairing_semantic_first_then_nearest():
    """同一语义多个候选取时间最近的；语义不同即使时间更近也不配。"""
    cn = cn_records([
        ("2026-11-13T04:29:00+08:00", "送交境外进口海关", ""),
        ("2026-11-13T04:30:00+08:00", "境外进口海关留存待验", ""),
        ("2026-11-13T04:31:00+08:00", "境外进口海关留存待验", ""),
    ])
    jp = jp_records([("2026/11/13 04:30", "通関手続中", "東京国際郵便局 · 東京都")])
    nodes = nodes_of(cn + jp)
    merged = [n for n in nodes if n["sources"] == ["cn_ems", "jp_post"]]
    assert len(merged) == 1 and merged[0]["at"] == "2026-11-13 04:30" and merged[0]["kind"] == "import_customs"
    submit = next(n for n in nodes if n["kind"] == "import_customs_submit")
    assert submit["sources"] == ["cn_ems"] and submit["name"] == "提交日本进口海关（東京国際郵便局）"


def test_duplicate_jp_rows_across_tasks_keep_richer_detail():
    rich = ("2026/11/14 06:05", "到着", "配達予定日：11月14日 · 板橋北郵便局 · 東京都")
    plain = ("2026/11/14 06:05", "到着", "板橋北郵便局 · 東京都")
    nodes = nodes_of(jp_records([plain, rich]))
    assert [n["name"] for n in nodes] == ["到达板橋北郵便局"]
    assert nodes[0]["detail"].startswith("预计投递日：11月14日")


def test_unknown_cn_event_shown_raw_and_logged_once(capsys):
    route_nodes._logged_unknown_cn.discard("某个新事件")
    nodes = nodes_of(cn_records([("2026-11-09T18:31:38+08:00", "中国邮政已收取邮件", ""),
                                 ("2026-11-09T20:00:00+08:00", "某个新事件", "")]))
    nodes_of(cn_records([("2026-11-09T20:00:00+08:00", "某个新事件", "")]))
    assert "某个新事件" in [n["name"] for n in nodes]
    assert capsys.readouterr().out.count("未收录的中国邮政事件：某个新事件") == 1


def test_one_flight_landing_named_and_in_japan():
    one = [e for e in CN_EVENTS if e[0] < "2026-11-11"] + [("2026-11-11T02:00:00+08:00", "飞机进港", ""), CN_EVENTS[1]]
    names = by_name(nodes_of(cn_records(one)))
    assert names["中国→日本 航班起飞"]["segment"] == "domestic"
    assert names["中国→日本 航班落地"]["segment"] == "japan" and names["中国→日本 航班落地"]["visible"]


def test_date_only_node_sorted_before_later_stage_same_day():
    for records in (jp_records(), cn_records(CN_EVENTS + CN_JAPAN) + jp_records()):
        names = [n["name"] for n in nodes_of(records) if n["visible"]]
        assert names.index("板橋北郵便局 派送中") < names.index("已妥投（板橋北郵便局）")

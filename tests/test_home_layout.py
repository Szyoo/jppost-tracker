"""以单号为中心的页面结构：管理后台首页（自己的单号 + 其他账号分组 + 已归档）、设置 / 用户 / 日志标签页，
普通用户门户页（添加单号 + 在途卡片 + 已归档 + 设置区块），以及任务卡片的摘要行（与推送正文同一写法）。"""
import json
import os
import re

import pytest

import app as app_module
import storage
from conftest import ADMIN_PASSWORD
from test_forecast import milestones_of
from test_merged_nodes import JP_ROWS, NOW, _result

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "static")


def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password})


def initial_user_state(html):
    match = re.search(r'<script id="initial-user-state" type="application/json">(.*?)</script>', html, re.S)
    return json.loads(match.group(1))


def read_static(name):
    with open(os.path.join(STATIC, name), encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture
def ella(db):
    return db.create_account({"username": "ella", "password": "pw-ella", "role": "user"})


@pytest.fixture
def frank(db):
    return db.create_account({"username": "frank", "password": "pw-frank", "role": "user"})


# --- 管理后台 ---

def test_admin_home_is_tracking_numbers(client, db, ella):
    mine = db.create_task(2, {"tracking_number": "AA111111111JP", "label": "键盘"})
    theirs = db.create_task(ella["id"], {"tracking_number": "BB222222222JP"})
    old = db.create_task(ella["id"], {"tracking_number": "CC333333333JP"})
    db.archive_task(old["id"])
    login(client, "admin", ADMIN_PASSWORD)
    html = client.get("/").data.decode()

    # 标签页：首页 / 设置 / 用户 / 日志，旧的「系统设置」「我的设置」「用户管理」标签与首页仪表盘都没了
    tabs = re.findall(r"@click=\"page = '(\w+)'\">([^<]+)</button>", html)
    assert tabs == [("home", "首页"), ("settings", "设置"), ("users", "用户"), ("logs", "日志")]
    for gone in ("我的设置", "用户管理", "全局概览", "任务总览", "overviewTasks", "openTaskEditor", "page === 'env'", "page === 'me'"):
        assert gone not in html, gone

    # 首页：添加单号在最上，然后我的单号、其他账号分组、已归档；卡片都是同一个组件
    home = html[html.index("v-if=\"page === 'home'\""):html.index("v-if=\"page === 'settings'\"")]
    assert home.index("添加单号") < home.index('id="account-me"') < home.index("v-for=\"group in otherGroups\"") < home.index("已归档")
    assert home.count("<task-card") == 3 and home.count("<task-add-form") == 2
    assert 'v-for="task in sortActive(meTasks)"' in home and 'v-for="task in group.tasks"' in home
    assert 'v-for="task in archivedTasks"' in home

    # 卡片数据：自己的、其他账号的、已归档的都在下发的账号列表里
    users = {user["id"]: user for user in initial_user_state(html)["users"]}
    assert [task["id"] for task in users[2]["tasks"]] == [mine["id"]]
    assert [task["id"] for task in users[ella["id"]]["tasks"]] == [theirs["id"]]
    assert [task["id"] for task in users[ella["id"]]["archived_tasks"]] == [old["id"]]

    # 设置页：资料与 Bark、服务控制、17TRACK、状态规则、系统设置都在一页
    settings = html[html.index("v-if=\"page === 'settings'\""):html.index("v-if=\"page === 'users'\"")]
    for anchor in ("settings-me", "settings-service", "settings-track17", "settings-status-rules", "settings-env"):
        assert f'id="{anchor}"' in settings
    assert "meBarkKeys" in settings and "startScript" in settings and "saveEnv" in settings

    # 用户页不再放任务卡片，只给跳到首页分组的入口
    users_page = html[html.index("v-if=\"page === 'users'\""):html.index("v-if=\"page === 'logs'\"")]
    assert "<task-card" not in users_page and "goToAccountTasks(selectedUser.id)" in users_page


def test_admin_task_card_component_mounts_forecast_and_records():
    js = read_static("app.js")
    card = js[js.index("app.component('task-card'"):]
    assert "<task-forecast" in card and "<task-timeline" in card
    assert ':open="!task.archived"' in card and "@loaded=\"onLoaded\"" in card
    # 编辑表单字段一个不少：单号、备注、间隔、渠道、非常规件、发货前节点
    for field in ("draft.tracking_number", "draft.label", "draft.check_interval", "draft.channel", "draft.atypical", "draft.milestones"):
        assert field in card, field
    for action in ("actions.toggle(task)", "actions.archive(task)", "actions.restore(task)", "actions.remove(task)", "@click=\"save\""):
        assert action in card, action
    assert "<summary>推送与查询记录</summary>" in js and "<summary>时间线</summary>" not in js


# --- 普通用户门户页 ---

def test_portal_is_tracking_numbers_first(client, db, ella):
    active = db.create_task(ella["id"], {"tracking_number": "AA111111111JP", "label": "键盘"})
    broken = db.create_task(ella["id"], {"tracking_number": "BB222222222JP"})
    storage.update_task_state(broken["id"], error="查询超时", tracking_number="BB222222222JP")
    old = db.create_task(ella["id"], {"tracking_number": "CC333333333JP"})
    db.archive_task(old["id"])
    login(client, "ella", "pw-ella")
    html = client.get("/").data.decode()

    assert "我的概览" not in html and "stat-grid" not in html
    assert html.index("添加单号") < html.index("我的单号") < html.index("已归档") < html.index('id="settings"')
    # 每张卡片（含已归档）都有预测挂载点、摘要行与推送记录；异常的排最前
    for task in (active, broken, old):
        assert f'data-task-forecast="{task["id"]}"' in html
        assert f'data-task-summary="{task["id"]}"' in html
        assert f'data-task-timeline="{task["id"]}"' in html
    assert html.index(f'id="task-{broken["id"]}"') < html.index(f'id="task-{active["id"]}"') < html.index(f'id="task-{old["id"]}"')
    # 在途的预测默认展开，已归档的收着（展开时才拉）
    card = html[html.index(f'id="task-{active["id"]}"'):html.index(f'id="task-{old["id"]}"')]
    assert '<details class="disclosure flush task-forecast" open>' in card
    archived = html[html.index(f'id="task-{old["id"]}"'):]
    assert '<details class="disclosure flush task-forecast">' in archived
    assert "<summary>推送与查询记录</summary>" in html and "<summary>时间线</summary>" not in html
    # 设置区块：资料、Bark 设备、Bark 设置手顺
    settings = html[html.index('id="settings"'):]
    assert 'name="bark_keys"' in settings and "Bark 设置手顺" in settings


def test_portal_toggle_form_keeps_channel_and_milestones(client, db, ella):
    task = db.create_task(ella["id"], {"tracking_number": "AA111111111JP", "channel": "渠道A",
                                       "milestones": {"ordered": "2026-10-01"}})
    db.update_task(task["id"], {"atypical": True}, actor_role="admin")
    assert db.get_task(task["id"])["atypical"]
    login(client, "ella", "pw-ella")
    html = client.get("/").data.decode()
    assert ">停用</button>" in html
    # 「停用」按钮的表单只带单号、备注、间隔
    client.post(f"/me/tasks/{task['id']}/update", data={"tracking_number": "AA111111111JP", "label": "", "check_interval": "300"})
    after = db.get_task(task["id"])
    assert not after["enabled"] and after["channel"] == "渠道A" and after["atypical"]
    assert milestones_of(task["id"]) == {"ordered": ("2026-10-01", "manual")}
    assert ">启用</button>" in client.get("/").data.decode()


def test_unauthenticated_still_redirects(client, db):
    resp = client.get("/")
    assert resp.status_code == 302 and resp.headers["Location"].startswith("/login")


# --- 摘要行 ---

def _seed_jp(task, rows):
    records = [{"info": f"{date} {status}", "detail": detail} for date, status, detail in rows]
    storage.update_task_state(task["id"], latest_info=records[-1]["info"], tracking_number=task["tracking_number"],
                              carrier_records=records, new_records=records)


def test_summary_line_matches_push_body(db, ella):
    task = db.create_task(ella["id"], {"tracking_number": "EB000000001CN"})
    _seed_jp(task, JP_ROWS[:3])
    summary = app_module.task_summary
    step = "提交中国出口海关查验（威海）"
    assert summary(task["id"], _result(), NOW) == f"{step} · 第 5 天 · 预计明天晚上签收"
    assert summary(task["id"], _result(confidence="none"), NOW) == f"{step} · 第 5 天"
    assert summary(task["id"], _result(overdue=True), NOW) == f"{step} · 第 5 天 · 比以往都慢"
    assert summary(task["id"], _result(origin=None, confidence="none"), NOW) == step


def test_summary_line_delivered_unrecognized_and_empty(db, ella, monkeypatch):
    task = db.create_task(ella["id"], {"tracking_number": "EB000000001CN"})
    assert app_module.task_summary(task["id"], _result(current=None), NOW) == "还没有物流记录"
    _seed_jp(task, JP_ROWS)
    done = _result(current={"stage": "delivered", "at": "2026-11-14 10:06", "waited_seconds": 0},
                   delivered=True, day=None, total_seconds=4 * 86400 + 15 * 3600)
    assert app_module.task_summary(task["id"], done, NOW).endswith(" · 全程 4.5 天")
    # 还没归类的日本邮政新状态：与推送标题同样写成「原文（未识别）」
    monkeypatch.setattr(app_module, "task_nodes", lambda task_id: [
        {"visible": True, "role": "unrecognized", "original": "新しい状態", "name": "新しい状態"},
    ])
    assert app_module.task_summary(task["id"], _result(), NOW) == "新しい状態（未识别） · 第 5 天 · 预计明天晚上签收"


def test_forecast_endpoint_carries_summary_and_keeps_auth(client, db, ella, frank):
    task = db.create_task(ella["id"], {"tracking_number": "EB000000001CN"})
    _seed_jp(task, JP_ROWS[:3])
    url = f"/api/tasks/{task['id']}/forecast"
    assert client.get(url).status_code == 401
    login(client, "frank", "pw-frank")
    assert client.get(url).status_code == 403
    client.post("/logout")
    login(client, "ella", "pw-ella")
    body = client.get(url).get_json()
    assert body["status"] == "success" and body["summary"].startswith("提交中国出口海关查验（威海）")
    client.post("/logout")
    login(client, "admin", ADMIN_PASSWORD)
    assert client.get(url).get_json()["summary"] == body["summary"]

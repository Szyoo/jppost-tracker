"""系统设置写 .env、任务校验、日志轮转、子进程读线程、管理员自我保护等加固项。"""
import io
import os

import pytest
from dotenv import dotenv_values

import app as app_module
import storage
from conftest import ADMIN_PASSWORD, gate


def login_admin(client):
    resp = client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD})
    assert resp.status_code == 302


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """把系统设置写到临时 .env，并让本测试改到的环境变量在结束后复原。"""
    path = tmp_path / ".env"
    path.write_text("")
    monkeypatch.setattr(app_module, "DOTENV_PATH", str(path))
    for key in app_module.SYSTEM_ENV_KEYS:
        monkeypatch.setenv(key, os.environ.get(key, ""))
    return path


# --- 系统设置写 .env ---

def test_update_env_writes_without_quotes(client, env_file):
    login_admin(client)
    resp = client.post("/update_env", json={"LOCAL_BARK_ENABLED": "1", "BARK_SERVER_INTERNAL": "http://bark:8080"})
    assert resp.status_code == 200, resp.get_json()
    text = env_file.read_text()
    assert "LOCAL_BARK_ENABLED=1\n" in text
    assert "BARK_SERVER_INTERNAL=http://bark:8080\n" in text
    assert "'" not in text and '"' not in text
    assert os.environ["BARK_SERVER_INTERNAL"] == "http://bark:8080"


@pytest.mark.parametrize("value", ["a b", "x#y", "it's", 'q"q', "back\\slash", "$HOME", "line\nbreak"])
def test_update_env_rejects_unsafe_values_and_writes_nothing(client, env_file, value):
    login_admin(client)
    resp = client.post("/update_env", json={"BARK_HEALTH_PATH": "/ok", "BARK_SERVER_PUBLIC": value})
    assert resp.status_code == 400
    assert env_file.read_text() == ""


def test_update_env_clear_takes_effect_for_web_and_tracker(client, env_file, monkeypatch):
    # 模拟容器启动时从同一文件加载进来的旧值
    monkeypatch.setenv("BARK_SERVER_INTERNAL", "http://old:8080")
    monkeypatch.setenv("BARK_SERVER", "")
    monkeypatch.setenv("BARK_SERVER_PUBLIC", "")
    env_file.write_text("BARK_SERVER_INTERNAL=http://old:8080\n")
    login_admin(client)
    resp = client.post("/update_env", json={"BARK_SERVER_INTERNAL": ""})
    assert resp.status_code == 200
    assert dotenv_values(env_file)["BARK_SERVER_INTERNAL"] == ""
    # Web 进程：不再回落到旧环境变量
    assert app_module.get_bark_internal_url() == ""
    # 追踪脚本：文件里出现过的键以文件为准，即使进程环境里还是旧值
    monkeypatch.setenv("BARK_SERVER_INTERNAL", "http://old:8080")
    assert storage.load_system_env(str(env_file), ["BARK_SERVER_INTERNAL"]) == {"BARK_SERVER_INTERNAL": ""}


def test_load_system_env_falls_back_to_environ_only_when_key_absent(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("A=from-file\nB=\n")
    monkeypatch.setenv("A", "env-a")
    monkeypatch.setenv("B", "env-b")
    monkeypatch.setenv("C", "env-c")
    assert storage.load_system_env(str(path), ["A", "B", "C"]) == {"A": "from-file", "B": "", "C": "env-c"}


def test_env_enabled_treats_empty_as_default(monkeypatch):
    monkeypatch.setenv("AUTO_START_TRACKER", "")
    assert app_module._env_enabled("AUTO_START_TRACKER", "1") is True
    monkeypatch.setenv("AUTO_START_TRACKER", "0")
    assert app_module._env_enabled("AUTO_START_TRACKER", "1") is False


def test_apply_note_distinguishes_tracker_fields():
    note = app_module.build_env_apply_note(["BARK_SERVER_INTERNAL", "BARK_HEALTH_PATH", "APP_PORT"])
    assert "追踪脚本在下次查询时读取新值：脚本推送地址" in note
    assert "已即时生效：健康检查路径" in note
    assert "需重启 Web 服务后生效：Web 控制台端口" in note


# --- Bark Keys 等字段清空 ---

def test_update_account_can_clear_bark_keys_and_note(db):
    user = db.create_account({"username": "alice", "password": "pw", "bark_keys": "k1\nk2", "note": "n"})
    assert user["bark_device_count"] == 2
    cleared = db.update_account(user["id"], {"bark_keys": "", "note": ""})
    assert cleared["bark_keys"] == "" and cleared["note"] == ""
    # 没提交的字段保持原值
    db.update_account(user["id"], {"bark_keys": "k3"})
    kept = db.update_account(user["id"], {"display_name": "Alice"})
    assert kept["bark_keys"] == "k3"


def test_me_form_clears_bark_keys(client, db):
    db.create_account({"username": "bob", "password": "pw", "bark_keys": "k1"})
    client.post("/login", data={"username": "bob", "password": "pw"})
    client.post("/me/update", data={"display_name": "Bob", "bark_keys": ""})
    assert db.get_account_by_username("bob")["bark_keys"] == ""


def test_test_push_with_cleared_keys_does_not_use_saved(db):
    account = {"display_name": "x", "bark_keys": "saved"}
    assert app_module.build_bark_test_account(account, {"bark_keys": ""})["bark_keys"] == ""
    assert app_module.build_bark_test_account(account, {})["bark_keys"] == "saved"


# --- 查询间隔范围 ---

def test_check_interval_bounds(db):
    user = db.create_account({"username": "carol", "password": "pw"})
    with pytest.raises(ValueError, match="30"):
        db.create_task(user["id"], {"tracking_number": "AA1", "check_interval": 10})
    with pytest.raises(ValueError):
        db.create_task(user["id"], {"tracking_number": "AA1", "check_interval": storage.MAX_CHECK_INTERVAL + 1})
    with pytest.raises(ValueError, match="整数"):
        db.create_task(user["id"], {"tracking_number": "AA1", "check_interval": "abc"})
    task = db.create_task(user["id"], {"tracking_number": "AA1", "check_interval": 30})
    assert task["check_interval"] == 30
    assert db.create_task(user["id"], {"tracking_number": "AA2"})["check_interval"] == 300
    with pytest.raises(ValueError):
        db.update_task(task["id"], {"check_interval": 5})
    assert db.get_task(task["id"])["check_interval"] == 30
    # 不提交间隔（或提交空串）时保持原值
    assert db.update_task(task["id"], {"label": "x", "check_interval": ""})["check_interval"] == 30


def test_admin_api_rejects_small_interval(client, db):
    login_admin(client)
    resp = client.post("/api/users/2/tasks", json={"tracking_number": "AB1", "check_interval": 1})
    assert resp.status_code == 400 and "30" in resp.get_json()["message"]


# --- 重复添加单号 ---

def test_duplicate_active_task_is_rejected_without_overwrite(client, db):
    db.create_account({"username": "dan", "password": "pw"})
    client.post("/login", data={"username": "dan", "password": "pw"})
    client.post("/me/tasks", data={"tracking_number": "AB123", "label": "键盘", "check_interval": "600", "enabled": "on"})
    resp = client.post("/me/tasks", data={"tracking_number": "ab 123", "label": "改掉", "check_interval": "60", "enabled": "on"})
    assert resp.status_code == 302 and "status=error" in resp.headers["Location"]
    tasks = db.list_tasks(db.get_account_by_username("dan")["id"])
    assert len(tasks) == 1 and tasks[0]["label"] == "键盘" and tasks[0]["check_interval"] == 600


def test_archived_task_is_revived_with_clear_message(client, db):
    dan = db.create_account({"username": "dan", "password": "pw"})
    task = db.create_task(dan["id"], {"tracking_number": "AB123"})
    db.archive_task(task["id"])
    client.post("/login", data={"username": "dan", "password": "pw"})
    resp = client.post("/me/tasks", data={"tracking_number": "AB123", "check_interval": "300", "enabled": "on"})
    location = resp.headers["Location"]
    assert "status=success" in location
    from urllib.parse import parse_qs, urlparse
    assert "已恢复追踪" in parse_qs(urlparse(location).query)["message"][0]
    revived = db.get_task(task["id"])
    assert not revived["archived"] and revived["seen_count"] == 2


def test_admin_api_duplicate_task_is_400(client, db):
    login_admin(client)
    assert client.post("/api/users/2/tasks", json={"tracking_number": "AB9"}).status_code == 200
    resp = client.post("/api/users/2/tasks", json={"tracking_number": "AB9", "label": "x"})
    assert resp.status_code == 400 and "已在追踪中" in resp.get_json()["message"]


# --- 管理员自我保护（本地登录） ---

def test_admin_cannot_disable_or_demote_self(client, db):
    login_admin(client)
    resp = client.put("/api/users/2", json={"login_enabled": False})
    assert resp.status_code == 400
    resp = client.put("/api/users/2", json={"role": "user", "login_enabled": True})
    assert resp.status_code == 400
    me = db.get_account(2)
    assert me["role"] == "admin" and me["login_enabled"]
    # 改别人不受限
    other = db.create_account({"username": "erin", "password": "pw", "role": "admin"})
    assert client.put(f"/api/users/{other['id']}", json={"role": "user", "login_enabled": False}).status_code == 200
    assert db.get_account(other["id"])["role"] == "user"
    # 保存自己的其他字段照常
    assert client.put("/api/users/2", json={"role": "admin", "login_enabled": True, "note": "ok"}).status_code == 200


# --- 日志轮转与尾部 ---

def test_rotate_log_file_keeps_backups(tmp_path):
    path = str(tmp_path / "t.log")
    for round_ in range(5):
        with open(path, "w") as f:
            f.write(f"r{round_}" * 10)
        app_module._rotate_log_file(path, max_bytes=10, backup_count=3)
    assert not os.path.exists(path)
    assert sorted(os.listdir(tmp_path)) == ["t.log.1", "t.log.2", "t.log.3"]
    assert open(path + ".1").read().startswith("r4")
    assert open(path + ".3").read().startswith("r2")


def test_log_channel_rotates_and_keeps_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "LOG_MAX_BYTES", 200)
    path = str(tmp_path / "x.log")
    channel = app_module.LogChannel(path, "x_log", tail_lines=5)
    for i in range(50):
        channel.append(f"line {i:03d}\n")
    assert channel.tail().splitlines() == [f"line {i:03d}" for i in range(45, 50)]
    assert os.path.getsize(path) <= 200
    assert os.path.exists(path + ".1")
    # 重启后只恢复尾部
    reloaded = app_module.LogChannel(path, "x_log", tail_lines=3)
    assert reloaded.tail().splitlines() == ["line 047", "line 048", "line 049"]


def test_read_tail_lines_skips_partial_first_line(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "LOG_TAIL_READ_BYTES", 20)
    path = tmp_path / "big.log"
    path.write_text("".join(f"row-{i}\n" for i in range(100)))
    lines = app_module._read_tail_lines(str(path), limit=10)
    assert lines and all(line.startswith("row-") for line in lines)
    assert lines[-1] == "row-99\n"


# --- 读输出线程只管自己的进程 ---

class FakeProc:
    def __init__(self, output=""):
        self.stdout = io.StringIO(output)

    def wait(self):
        return 0

    def poll(self):
        return None


def test_old_reader_does_not_touch_new_process(monkeypatch):
    old, new = FakeProc("bye\n"), FakeProc()
    monkeypatch.setattr(app_module, "script_process", new)
    monkeypatch.setattr(app_module, "script_stop_requested", True)
    app_module.read_script_output(old)
    assert app_module.script_process is new
    assert old.stdout.closed and not new.stdout.closed


def test_reader_clears_own_process(monkeypatch):
    proc = FakeProc("hi\n")
    monkeypatch.setattr(app_module, "script_process", proc)
    monkeypatch.setattr(app_module, "script_stop_requested", True)
    app_module.read_script_output(proc)
    assert app_module.script_process is None


def test_old_bark_reader_does_not_touch_new_process(monkeypatch):
    old, new = FakeProc(), FakeProc()
    monkeypatch.setattr(app_module, "bark_server_process", new)
    app_module.read_bark_output(old)
    assert app_module.bark_server_process is new and not new.stdout.closed


# --- tracker 可测部分 ---

def test_tracker_interval_floor_and_runtime_config():
    import tracker

    task = {"id": 1, "account_id": 2, "tracking_number": "AB1", "check_interval": 5, "account": {"bark_keys": "k"}}
    config = tracker.build_runtime_config(task, {"BARK_SERVER": "http://b/"})
    assert config["check_interval"] == storage.MIN_CHECK_INTERVAL
    assert config["bark_server"] == "http://b"
    assert tracker._task_interval({"check_interval": 600}) == 600
    assert tracker.validate_config(config) == []

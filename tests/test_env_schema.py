"""系统设置的字段定义：生效时机与类型穷举、按类型与范围校验、追踪脚本重读的键、Render 遗留已清理。"""
import os

import pytest

import app as app_module
import env_schema
import tracker
from conftest import ADMIN_PASSWORD


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("")
    monkeypatch.setattr(app_module, "DOTENV_PATH", str(path))
    for key in env_schema.ENV_KEYS:
        monkeypatch.setenv(key, os.environ.get(key, ""))
    return path


def post_env(client, payload):
    client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD})
    return client.post("/update_env", json=payload)


def test_every_field_has_known_apply_and_type():
    for key, field in env_schema.ENV_FIELDS.items():
        assert field["apply"] in env_schema.APPLY_META, key
        assert field["type"] in env_schema.FIELD_TYPES, key
        assert field["label"] and field["desc"], key


def test_serialized_groups_carry_apply_label_and_tone():
    for group in env_schema.serialize_groups():
        for field in group["fields"]:
            meta = env_schema.APPLY_META[field["apply"]]
            assert field["apply_label"] == meta["label"] and field["apply_tone"] == meta["tone"]


def test_tracker_rereads_exactly_the_tracker_fields():
    assert set(tracker.TRACKER_ENV_KEYS) == set(env_schema.TRACKER_ENV_KEYS)
    assert {"BARK_SERVER_INTERNAL", "BARK_SERVER_PUBLIC", "BARK_SERVER", "REQUEST_TIMEOUT"} == set(env_schema.TRACKER_ENV_KEYS)
    assert "REQUEST_TIMEOUT" in app_module.SYSTEM_ENV_KEYS


def test_request_timeout_default_matches_tracker():
    assert env_schema.ENV_FIELDS["REQUEST_TIMEOUT"]["default"] == "15"
    assert tracker.build_runtime_config({"id": 1, "account": {}}, {})["request_timeout"] == 15
    assert tracker.build_runtime_config({"id": 1, "account": {}}, {"REQUEST_TIMEOUT": "30"})["request_timeout"] == 30


def test_apply_note_covers_every_timing():
    note = env_schema.apply_note(["BARK_HEALTH_PATH", "BARK_SERVER_INTERNAL", "BARK_BIND_ADDRESS", "APP_PORT"])
    assert "已即时生效：健康检查路径" in note
    assert "追踪脚本在下次查询时读取新值：脚本推送地址" in note
    assert "下次启动本地 Bark 服务时生效" in note and "本地 Bark 监听地址" in note
    assert "需重启 Web 服务后生效：Web 控制台端口" in note


@pytest.mark.parametrize("key,value,expected", [
    ("APP_PORT", "8080", "8080"),
    ("APP_PORT", "", ""),
    ("REQUEST_TIMEOUT", "030", "30"),
    ("LOCAL_BARK_ENABLED", "off", "0"),
    ("LOCAL_BARK_ENABLED", "TRUE", "1"),
    ("BARK_SERVER_PUBLIC", "https://bark.example.com", "https://bark.example.com"),
    ("BARK_HEALTH_PATH", "/ping", "/ping"),
])
def test_validate_accepts(key, value, expected):
    assert env_schema.validate_value(key, value) == expected


@pytest.mark.parametrize("key,value", [
    ("APP_PORT", "abc"),
    ("APP_PORT", "0"),
    ("APP_PORT", "70000"),
    ("REQUEST_TIMEOUT", "-3"),
    ("REQUEST_TIMEOUT", "1.5"),
    ("BARK_HEALTH_TIMEOUT", "121"),
    ("LOCAL_BARK_ENABLED", "maybe"),
    ("BARK_SERVER_INTERNAL", "bark:8080"),
    ("BARK_SERVER_INTERNAL", "ftp://bark"),
    ("BARK_SERVER_PUBLIC", "http://"),
])
def test_validate_rejects(key, value):
    with pytest.raises(ValueError):
        env_schema.validate_value(key, value)


def test_update_env_rejects_invalid_type_and_writes_nothing(client, env_file):
    resp = post_env(client, {"BARK_HEALTH_PATH": "/ok", "APP_PORT": "99999"})
    assert resp.status_code == 400
    assert "APP_PORT" in resp.get_json()["message"]
    assert env_file.read_text() == ""


def test_update_env_normalizes_bool_and_accepts_request_timeout(client, env_file):
    resp = post_env(client, {"REQUEST_TIMEOUT": "25", "AUTO_START_TRACKER": "off"})
    assert resp.status_code == 200, resp.get_json()
    text = env_file.read_text()
    assert "REQUEST_TIMEOUT=25\n" in text and "AUTO_START_TRACKER=0\n" in text


def test_render_keepalive_is_gone(client, env_file):
    assert "PUBLIC_URL" not in env_schema.ENV_KEYS and "KEEPALIVE_INTERVAL" not in env_schema.ENV_KEYS
    assert not hasattr(app_module, "start_keepalive")
    resp = post_env(client, {"PUBLIC_URL": "https://x.example.com"})
    assert resp.status_code == 400
    page = client.get("/").get_data(as_text=True)
    assert "keepalive" not in page and "冷启动" not in page

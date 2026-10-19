"""Bark 共享模块：地址选择、参数解析、发送；以及 Web 与追踪脚本都走同一份实现。"""
import bark
import tracker
import app as app_module


class FakeResponse:
    status_code = 200
    text = "ok"


def test_internal_and_public_url_fallback_order():
    env = {"BARK_SERVER_INTERNAL": "http://in:8080/", "BARK_SERVER": " http://old ", "BARK_SERVER_PUBLIC": "https://pub"}
    assert bark.internal_url(env) == "http://in:8080"
    assert bark.public_url(env) == "https://pub"
    # 内网留空：先回退兼容地址，再回退公网
    assert bark.internal_url({"BARK_SERVER": "http://old", "BARK_SERVER_PUBLIC": "https://pub"}) == "http://old"
    assert bark.internal_url({"BARK_SERVER_PUBLIC": "https://pub/"}) == "https://pub"
    # 公网留空：先兼容地址，再内网
    assert bark.public_url({"BARK_SERVER_INTERNAL": "http://in"}) == "http://in"
    assert bark.internal_url({}) == "" and bark.public_url({"BARK_SERVER": "  "}) == ""


def test_parse_query_params():
    assert bark.parse_query_params("") == {}
    assert bark.parse_query_params(None) == {}
    assert bark.parse_query_params("?sound=minuet&level=timeSensitive") == {"sound": "minuet", "level": "timeSensitive"}
    assert bark.parse_query_params("group=%E5%BF%AB%E9%80%92&&flag") == {"group": "快递", "flag": ""}


def test_build_params_adds_url_only_when_enabled_and_present():
    assert bark.build_params("?sound=a", True, "https://t/1") == {"sound": "a", "url": "https://t/1"}
    assert bark.build_params("?sound=a", False, "https://t/1") == {"sound": "a"}
    assert bark.build_params("?sound=a", True, "") == {"sound": "a"}


def test_send_push_single_key_uses_get_short_link(monkeypatch):
    calls = []
    monkeypatch.setattr(bark.requests, "get", lambda url, timeout: calls.append((url, timeout)) or FakeResponse())
    resp = bark.send_push("http://b", ["k1"], "标题 1", "正文/2", {"sound": "a b"}, 7)
    assert resp.status_code == 200
    assert calls == [("http://b/k1/%E6%A0%87%E9%A2%98%201/%E6%AD%A3%E6%96%87%2F2?sound=a+b", 7)]


def test_send_push_multiple_keys_uses_batch_endpoint(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bark.requests, "post", lambda url, json, timeout: calls.append((url, json, timeout)) or FakeResponse()
    )
    bark.send_push("http://b", ["k1", "k2"], "t", "b", {"level": "active"}, 5)
    assert calls == [("http://b/push", {"title": "t", "body": "b", "device_keys": ["k1", "k2"], "level": "active"}, 5)]


def test_web_and_tracker_use_shared_implementation(monkeypatch):
    monkeypatch.setenv("BARK_SERVER_INTERNAL", "http://in:1")
    monkeypatch.setenv("BARK_SERVER_PUBLIC", "https://pub")
    assert app_module.get_bark_internal_url() == "http://in:1"
    assert app_module.get_bark_public_url() == "https://pub"
    config = tracker.build_runtime_config(
        {"id": 1, "tracking_number": "AA1", "account": {"bark_keys": "k1"}},
        {"BARK_SERVER_INTERNAL": "http://in:1"},
    )
    assert config["bark_server"] == "http://in:1" and config["bark_keys"] == ["k1"]

    seen = []
    monkeypatch.setattr(bark.requests, "get", lambda url, timeout: seen.append(url) or FakeResponse())
    config.update(bark_query_params="?sound=a", bark_url_enabled=True, tracking_url="https://t/AA1")
    ok, reason = tracker.send_bark_notification(config, "t", "b")
    assert ok and reason == ""
    assert seen and "sound=a" in seen[0] and "url=https%3A%2F%2Ft%2FAA1" in seen[0]

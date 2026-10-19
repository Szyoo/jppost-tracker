"""Bark 推送的共享逻辑：地址选择、查询参数解析、发送。
Web 控制台（测试推送）与追踪脚本（物流变化推送）共用，避免两边各写一份后慢慢走样。"""
import urllib.parse

import requests


def first_non_empty(*values) -> str:
    for value in values:
        if value and str(value).strip():
            return str(value).strip()
    return ""


def internal_url(env) -> str:
    """推送实际访问的地址：内网优先，依次回退到兼容地址与公网地址。env 为任何带 get 的映射。"""
    return first_non_empty(
        env.get("BARK_SERVER_INTERNAL"),
        env.get("BARK_SERVER"),
        env.get("BARK_SERVER_PUBLIC"),
    ).rstrip("/")


def public_url(env) -> str:
    """手机接入与健康检查用的地址：公网优先，依次回退到兼容地址与内网地址。"""
    return first_non_empty(
        env.get("BARK_SERVER_PUBLIC"),
        env.get("BARK_SERVER"),
        env.get("BARK_SERVER_INTERNAL"),
    ).rstrip("/")


def parse_query_params(query_string) -> dict:
    """把 "?sound=minuet&level=active" 之类的串解析成字典，保留 URL 解码后的键值。"""
    query_string = str(query_string or "").strip()
    if not query_string:
        return {}
    result = {}
    for part in query_string.lstrip("?").split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        result[urllib.parse.unquote(key)] = urllib.parse.unquote(value)
    return result


def build_params(query_string, url_enabled: bool, tracking_url: str) -> dict:
    """账号配置的附加参数；开启了点击跳转且有链接时再叠加 url。"""
    params = parse_query_params(query_string)
    tracking_url = str(tracking_url or "").strip()
    if url_enabled and tracking_url:
        params["url"] = tracking_url
    return params


def send_push(server: str, keys: list[str], title: str, body: str, params: dict, timeout: int) -> requests.Response:
    """向所有 keys 推送一次，不重试；网络异常原样抛出，由调用方决定怎么记录。
    单设备走 GET 短链，多设备只能用 /push 批量接口。"""
    if len(keys) == 1:
        title_enc = urllib.parse.quote(title, safe="")
        body_enc = urllib.parse.quote(body, safe="")
        query = urllib.parse.urlencode(params, doseq=True)
        suffix = f"?{query}" if query else ""
        return requests.get(f"{server}/{keys[0]}/{title_enc}/{body_enc}{suffix}", timeout=timeout)
    payload = {"title": title, "body": body, "device_keys": keys, **params}
    return requests.post(f"{server}/push", json=payload, timeout=timeout)

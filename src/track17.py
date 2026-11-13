"""17TRACK API v2.2 客户端：给中国寄出的单号补上始发国段（中国邮政 EMS）的轨迹。

只管 HTTP 请求与响应解析，不碰数据库（登记状态由 storage.track17_* 记录，编排在 tracker.sync_cn_leg）。
- register：登记单号。**每登记一个新单号扣 1 个额度**；已登记过的单号会进 rejected，按成功处理。
- gettrackinfo：查轨迹，不扣额度。刚登记后要一两分钟才有数据（未就绪时进 rejected 或 events 为空）。
- getquota：剩余额度，管理员页面显示用。
所有失败都抛 Track17Error，调用方负责兜住——17TRACK 出错绝不能影响日本邮政追踪与推送。
"""
import urllib.parse

import requests

import forecast

API_BASE = "https://api.17track.net/track/v2.2"
# 17TRACK 公开查询页（网页上的「17TRACK」跳转胶囊）
TRACKING_PAGE = "https://t.17track.net/zh-cn#nums={number}"
# 每次请求最多这么多单号（register / gettrackinfo 的上限）
MAX_NUMBERS = 40
# 17TRACK 的「单号已登记」错误码；有的响应只在 message 里写 registered，两者都认
ALREADY_REGISTERED_CODE = -18019901
# 中国邮政在 providers 里的国家码
CN_PROVIDER_COUNTRY = "CN"


def tracking_page_url(number: str) -> str:
    return TRACKING_PAGE.format(number=urllib.parse.quote(str(number), safe=""))


class Track17Error(Exception):
    """请求失败、HTTP 非 2xx、响应 code 非 0 或结构不对。"""


def _post(path: str, api_key: str, body, timeout: int) -> dict:
    headers = {"17token": api_key, "Content-Type": "application/json"}
    try:
        if body is None:
            response = requests.post(f"{API_BASE}/{path}", headers=headers, timeout=timeout)
        else:
            response = requests.post(f"{API_BASE}/{path}", headers=headers, json=body, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        raise Track17Error(f"请求 17TRACK 失败：{exc}") from None
    if not 200 <= response.status_code < 300:
        raise Track17Error(f"17TRACK 返回 HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        raise Track17Error("17TRACK 返回的不是 JSON") from None
    if not isinstance(payload, dict) or payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
        code = payload.get("code") if isinstance(payload, dict) else None
        raise Track17Error(f"17TRACK 返回错误（code {code}）")
    return payload["data"]


def _check_numbers(numbers) -> list:
    numbers = [str(number) for number in numbers]
    if not numbers or len(numbers) > MAX_NUMBERS:
        raise ValueError(f"单号数量必须在 1～{MAX_NUMBERS} 之间。")
    return numbers


def _is_already_registered(error) -> bool:
    if not isinstance(error, dict):
        return False
    return error.get("code") == ALREADY_REGISTERED_CODE or "registered" in str(error.get("message") or "").lower()


def register(numbers, api_key: str, timeout: int) -> dict:
    """登记单号。返回 {单号: 失败原因}，空串表示成功（含「已登记」）。响应里没提到的单号按失败处理。"""
    numbers = _check_numbers(numbers)
    data = _post("register", api_key, [{"number": number} for number in numbers], timeout)
    result = {number: "17TRACK 没有返回该单号的登记结果" for number in numbers}
    for item in data.get("accepted") or []:
        if isinstance(item, dict) and item.get("number") in result:
            result[item["number"]] = ""
    for item in data.get("rejected") or []:
        if not isinstance(item, dict) or item.get("number") not in result:
            continue
        error = item.get("error")
        if _is_already_registered(error):
            result[item["number"]] = ""
        else:
            message = error.get("message") if isinstance(error, dict) else ""
            code = error.get("code") if isinstance(error, dict) else None
            result[item["number"]] = f"登记被拒（{code}）：{message}".strip("：")
    return result


def _cn_events(track_info) -> list:
    """track_info 里国家为 CN 的 provider 的全部事件 → [{"record_at", "status", "detail"}]（日本时间，按时间正序）。"""
    tracking = (track_info or {}).get("tracking") or {}
    rows = []
    for provider in tracking.get("providers") or []:
        meta = (provider or {}).get("provider") or {}
        if str(meta.get("country") or "").upper() != CN_PROVIDER_COUNTRY:
            continue
        for event in provider.get("events") or []:
            description = str((event or {}).get("description") or "").strip()
            record_at = forecast.track17_event_time(event.get("time_iso"), description)
            if not description or record_at is None:
                continue
            rows.append({
                "record_at": record_at,
                "status": description,
                "detail": str(event.get("location") or "").strip(),
            })
    rows.sort(key=lambda row: row["record_at"])
    return rows


def get_cn_events(numbers, api_key: str, timeout: int) -> dict:
    """查轨迹。返回 {单号: 事件列表或 None}：None 表示还没就绪（进了 rejected、或响应里没有它）；
    列表可能为空（有轨迹但没有中国邮政的事件）。"""
    numbers = _check_numbers(numbers)
    data = _post("gettrackinfo", api_key, [{"number": number} for number in numbers], timeout)
    result = {number: None for number in numbers}
    for item in data.get("accepted") or []:
        if isinstance(item, dict) and item.get("number") in result:
            result[item["number"]] = _cn_events(item.get("track_info"))
    return result


def get_quota(api_key: str, timeout: int) -> dict:
    """剩余额度等：{"quota_total", "quota_used", "quota_remain", ...}（17TRACK 原样返回的整数字段）。"""
    data = _post("getquota", api_key, None, timeout)
    return {key: value for key, value in data.items() if isinstance(value, int)}

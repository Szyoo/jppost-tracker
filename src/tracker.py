import os
import time
import urllib.parse
import time as _time

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from storage import ensure_storage, list_tracked_accounts, load_system_env, parse_bark_keys, update_tracking_state

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
DOTENV_PATH = os.path.join(BASE_DIR, ".env")
SYSTEM_ENV_KEYS = ["BARK_SERVER_INTERNAL", "BARK_SERVER", "BARK_SERVER_PUBLIC", "REQUEST_TIMEOUT"]

load_dotenv(DOTENV_PATH)
ensure_storage(DOTENV_PATH)

os.environ.setdefault("TZ", "Asia/Tokyo")
if hasattr(_time, "tzset"):
    try:
        _time.tzset()
    except Exception:
        pass


def _first_non_empty(*values: str) -> str:
    for value in values:
        if value and str(value).strip():
            return str(value).strip()
    return ""


def _normalize_int(value, default: int) -> int:
    try:
        parsed = int(value)
        return parsed if parsed > 0 else default
    except Exception:
        return default


def _parse_query_params(query_string: str) -> dict:
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


def build_tracking_url(tracking_number: str) -> str:
    return (
        "https://trackings.post.japanpost.jp/services/srv/search/direct"
        f"?reqCodeNo1={tracking_number}&searchKind=S002&locale=ja"
    )


def build_runtime_config(account: dict, system_env: dict) -> dict:
    tracking_number = str(account.get("tracking_number", "") or "").strip()
    bark_server = _first_non_empty(
        system_env.get("BARK_SERVER_INTERNAL"),
        system_env.get("BARK_SERVER"),
        system_env.get("BARK_SERVER_PUBLIC"),
    ).rstrip("/")
    return {
        "account_id": account["id"],
        "username": account.get("username", ""),
        "display_name": account.get("display_name") or account.get("name") or account.get("username") or "用户",
        "tracking_number": tracking_number,
        "tracking_url": build_tracking_url(tracking_number),
        "check_interval": _normalize_int(account.get("check_interval", 300), 300),
        "request_timeout": _normalize_int(system_env.get("REQUEST_TIMEOUT", "15"), 15),
        "bark_server": bark_server,
        "bark_keys": parse_bark_keys(account.get("bark_keys") or account.get("bark_key")),
        "bark_query_params": str(account.get("bark_query_params", "") or ""),
        "bark_url_enabled": bool(account.get("bark_url_enabled")),
        "last_tracking_info": str(account.get("last_tracking_info", "") or ""),
    }


def validate_config(config: dict) -> list[str]:
    missing = []
    if not config["tracking_number"]:
        missing.append("tracking_number")
    if not config["bark_server"]:
        missing.append("bark_server")
    if not config["bark_keys"]:
        missing.append("bark_keys")
    return missing


def send_bark_notification(config: dict, title: str, message: str, retries: int = 1) -> bool:
    if not config["bark_server"] or not config["bark_keys"]:
        print(f"[{config['display_name']}] 未配置 Bark 地址或 Bark Keys，跳过推送。")
        return False

    query_params = _parse_query_params(config["bark_query_params"])
    if config["bark_url_enabled"]:
        query_params["url"] = config["tracking_url"]

    last_error = ""
    keys = config["bark_keys"]
    for attempt in range(retries + 1):
        try:
            if len(keys) == 1:
                title_enc = urllib.parse.quote(title, safe="")
                body_enc = urllib.parse.quote(message, safe="")
                extra = urllib.parse.urlencode(query_params, doseq=True)
                suffix = f"?{extra}" if extra else ""
                url = f"{config['bark_server']}/{keys[0]}/{title_enc}/{body_enc}{suffix}"
                resp = requests.get(url, timeout=config["request_timeout"])
            else:
                payload = {"title": title, "body": message, "device_keys": keys, **query_params}
                resp = requests.post(
                    f"{config['bark_server']}/push",
                    json=payload,
                    timeout=config["request_timeout"],
                )

            if 200 <= resp.status_code < 300:
                print(f"[{config['display_name']}] Bark 通知已发送到 {len(keys)} 个设备。")
                return True
            last_error = f"HTTP {resp.status_code}"
        except Exception as exc:
            last_error = str(exc)

        if attempt < retries:
            print(f"[{config['display_name']}] Bark 发送失败（{last_error}），30 秒后重试。")
            time.sleep(30)

    print(f"[{config['display_name']}] Bark 通知发送失败：{last_error}")
    return False


def get_latest_tracking_info(config: dict):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "sec-ch-ua": '"Google Chrome";v="137", "Chromium";v="137", "Not/A)Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
        }
        response = requests.get(config["tracking_url"], headers=headers, timeout=config["request_timeout"])
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        table = soup.find("table", {"class": "tableType01 txt_c m_b5", "summary": "履歴情報"})
        if not table:
            return None

        date_cells = table.find_all("td", class_="w_120")
        if not date_cells:
            return None

        latest_date = date_cells[-1].get_text(strip=True)
        latest_row = date_cells[-1].parent
        if latest_row is None:
            return None
        status_cell = latest_row.find("td", class_="w_150")
        latest_status = status_cell.get_text(strip=True) if status_cell else ""
        return f"{latest_date} {latest_status}".strip()
    except requests.exceptions.RequestException as exc:
        print(f"[{config['display_name']}] 请求快递信息失败: {exc}")
        return None
    except Exception as exc:
        print(f"[{config['display_name']}] 解析快递信息时出错: {exc}")
        return None


def process_account(account: dict, system_env: dict):
    config = build_runtime_config(account, system_env)
    missing = validate_config(config)
    if missing:
        error = f"缺少必要配置: {', '.join(missing)}"
        print(f"[{config['display_name']}] {error}")
        update_tracking_state(config["account_id"], error=error)
        return

    current_info = get_latest_tracking_info(config)
    if not current_info:
        update_tracking_state(config["account_id"], error="无法获取最新快递信息。")
        return

    print(f"[{config['display_name']}] 最新物流记录: {current_info}")
    pushed = False
    if current_info != config["last_tracking_info"]:
        pushed = send_bark_notification(config, "快递更新通知", current_info)
    else:
        print(f"[{config['display_name']}] 暂无更新。")

    update_tracking_state(
        config["account_id"],
        latest_info=current_info,
        error="",
        pushed=pushed,
    )


def main():
    print("多用户快递监控程序启动...")
    next_runs = {}
    last_empty_log_at = 0.0

    try:
        while True:
            accounts = list_tracked_accounts()
            now = time.time()

            if not accounts:
                if now - last_empty_log_at >= 60:
                    print("当前没有启用追踪的用户。")
                    last_empty_log_at = now
                time.sleep(5)
                continue

            account_ids = {account["id"] for account in accounts}
            for stale_id in list(next_runs.keys()):
                if stale_id not in account_ids:
                    next_runs.pop(stale_id, None)

            due_accounts = []
            for account in accounts:
                next_run = next_runs.get(account["id"], 0)
                if now >= next_run:
                    due_accounts.append(account)

            if not due_accounts:
                time.sleep(1)
                continue

            system_env = load_system_env(DOTENV_PATH, SYSTEM_ENV_KEYS)
            for account in due_accounts:
                process_account(account, system_env)
                next_runs[account["id"]] = time.time() + _normalize_int(account.get("check_interval", 300), 300)

            time.sleep(1)
    except KeyboardInterrupt:
        print("程序终止。")


if __name__ == "__main__":
    main()

import os
import time
import urllib.parse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from storage import (
    build_tracking_url,
    ensure_storage,
    list_due_tasks,
    load_system_env,
    parse_bark_keys,
    update_task_state,
)

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
DOTENV_PATH = os.path.join(BASE_DIR, ".env")
SYSTEM_ENV_KEYS = ["BARK_SERVER_INTERNAL", "BARK_SERVER", "BARK_SERVER_PUBLIC", "REQUEST_TIMEOUT"]

# 主循环单次休眠上限：即使所有任务都还没到期，也最多 30 秒后重查一次数据库，
# 这样后台新建或改过间隔的任务不必等满一个 check_interval 才被感知。
MAX_LOOP_SLEEP = 30
IDLE_LOOP_SLEEP = 5

load_dotenv(DOTENV_PATH)
ensure_storage(DOTENV_PATH)

# 日本邮政官网只给日本时间，进程时区固定成东京，日志与解析结果才对得上。
os.environ.setdefault("TZ", "Asia/Tokyo")
if hasattr(time, "tzset"):
    try:
        time.tzset()
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


def _build_log_prefix(display_name: str, label: str, tracking_number: str) -> str:
    """一个账号可能同时追多个包裹，日志前缀必须带上任务身份，否则看不出是谁的哪一件。"""
    number = tracking_number or "未填单号"
    if label:
        return f"[{display_name} · {label}({number})]"
    return f"[{display_name} · {number}]"


def build_push_message(config: dict, latest_info: str) -> tuple[str, str]:
    """推送的标题与正文也必须带任务标识：同一账号的多个包裹会推到同一台设备上，
    标题若都是"快递更新通知"，在手机通知栏里根本分不清是哪一件。"""
    label = config["label"]
    number = config["tracking_number"]
    if label:
        return f"快递更新 · {label}", f"{number} {latest_info}"
    return f"快递更新 · {number}", latest_info


def build_runtime_config(task: dict, system_env: dict) -> dict:
    # 单号与轮询间隔属于任务，Bark 配置属于账号，两者来源不同不能混。
    account = task.get("account") or {}
    tracking_number = str(task.get("tracking_number", "") or "").strip()
    label = str(task.get("label", "") or "").strip()
    display_name = (
        account.get("display_name") or account.get("name") or account.get("username") or "用户"
    )
    bark_server = _first_non_empty(
        system_env.get("BARK_SERVER_INTERNAL"),
        system_env.get("BARK_SERVER"),
        system_env.get("BARK_SERVER_PUBLIC"),
    ).rstrip("/")
    return {
        "task_id": task["id"],
        "account_id": task.get("account_id") or account.get("id"),
        "username": account.get("username", ""),
        "display_name": display_name,
        "label": label,
        "log_prefix": _build_log_prefix(display_name, label, tracking_number),
        "tracking_number": tracking_number,
        # storage 已经拼好 URL；这里兜底也复用它的实现，避免单号 URL 格式在两处各写一遍。
        "tracking_url": str(task.get("tracking_url") or "").strip() or build_tracking_url(tracking_number),
        "check_interval": _normalize_int(task.get("check_interval", 300), 300),
        "request_timeout": _normalize_int(system_env.get("REQUEST_TIMEOUT", "15"), 15),
        "bark_server": bark_server,
        "bark_keys": parse_bark_keys(account.get("bark_keys") or account.get("bark_key")),
        "bark_query_params": str(account.get("bark_query_params", "") or ""),
        "bark_url_enabled": bool(account.get("bark_url_enabled")),
        "last_tracking_info": str(task.get("last_tracking_info", "") or ""),
    }


def validate_config(config: dict) -> list[str]:
    # 这些文案会写进 last_error 直接显示在网页上，所以用中文而不是字段名。
    missing = []
    if not config["tracking_number"]:
        missing.append("单号")
    if not config["bark_server"]:
        missing.append("Bark 服务地址（系统设置）")
    if not config["bark_keys"]:
        missing.append("Bark Keys（账号设置）")
    return missing


def send_bark_notification(config: dict, title: str, message: str, retries: int = 1) -> bool:
    if not config["bark_server"] or not config["bark_keys"]:
        print(f"{config['log_prefix']} 未配置 Bark 地址或 Bark Keys，跳过推送。")
        return False

    query_params = _parse_query_params(config["bark_query_params"])
    if config["bark_url_enabled"]:
        query_params["url"] = config["tracking_url"]

    last_error = ""
    keys = config["bark_keys"]
    for attempt in range(retries + 1):
        try:
            # 单设备走 GET 短链，多设备只能用 /push 批量接口。
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
                print(f"{config['log_prefix']} Bark 通知已发送到 {len(keys)} 个设备。")
                return True
            last_error = f"HTTP {resp.status_code}"
        except Exception as exc:
            last_error = str(exc)

        if attempt < retries:
            print(f"{config['log_prefix']} Bark 发送失败（{last_error}），30 秒后重试。")
            time.sleep(30)

    print(f"{config['log_prefix']} Bark 通知发送失败：{last_error}")
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

        # 履历表按时间正序排列，最后一行才是最新状态。
        latest_date = date_cells[-1].get_text(strip=True)
        latest_row = date_cells[-1].parent
        if latest_row is None:
            return None
        status_cell = latest_row.find("td", class_="w_150")
        latest_status = status_cell.get_text(strip=True) if status_cell else ""
        return f"{latest_date} {latest_status}".strip()
    except requests.exceptions.RequestException as exc:
        print(f"{config['log_prefix']} 请求快递信息失败: {exc}")
        return None
    except Exception as exc:
        print(f"{config['log_prefix']} 解析快递信息时出错: {exc}")
        return None


def process_task(task: dict, system_env: dict):
    config = build_runtime_config(task, system_env)
    prefix = config["log_prefix"]

    missing = validate_config(config)
    if missing:
        error = f"缺少必要配置: {', '.join(missing)}"
        print(f"{prefix} {error}")
        update_task_state(config["task_id"], error=error)
        return

    current_info = get_latest_tracking_info(config)
    if not current_info:
        update_task_state(config["task_id"], error="无法获取最新快递信息。")
        return

    print(f"{prefix} 最新物流记录: {current_info}")
    pushed = False
    if current_info != config["last_tracking_info"]:
        title, body = build_push_message(config, current_info)
        pushed = send_bark_notification(config, title, body)
    else:
        print(f"{prefix} 暂无更新。")

    update_task_state(
        config["task_id"],
        latest_info=current_info,
        error="",
        pushed=pushed,
    )


def main():
    print("多任务快递监控程序启动...")
    next_runs: dict[int, float] = {}
    last_empty_log_at = 0.0

    try:
        while True:
            tasks = list_due_tasks()
            now = time.time()

            if not tasks:
                # 空库时也别刷屏，一分钟提示一次就够。
                if now - last_empty_log_at >= 60:
                    print("当前没有启用的追踪任务。")
                    last_empty_log_at = now
                time.sleep(IDLE_LOOP_SLEEP)
                continue

            # 任务被停用、归档或删除后就不会再出现在结果里，顺手清掉它的计时。
            live_ids = {int(task["id"]) for task in tasks}
            for stale_id in list(next_runs.keys()):
                if stale_id not in live_ids:
                    next_runs.pop(stale_id, None)

            # 没有记录过的任务默认到期时刻为 0，也就是新任务立刻抓一次。
            due_tasks = [task for task in tasks if now >= next_runs.get(int(task["id"]), 0.0)]

            if not due_tasks:
                # 睡到最近一个任务到期，避免每秒空转查库；上限见 MAX_LOOP_SLEEP。
                next_due = min(next_runs.get(int(task["id"]), now) for task in tasks)
                time.sleep(max(0.5, min(MAX_LOOP_SLEEP, next_due - now)))
                continue

            system_env = load_system_env(DOTENV_PATH, SYSTEM_ENV_KEYS)
            for task in due_tasks:
                process_task(task, system_env)
                # 每个任务用自己的间隔独立计时，且从本次抓取结束算起，
                # 免得抓取耗时或 Bark 重试把下一轮挤到马上又触发。
                next_runs[int(task["id"])] = time.time() + _normalize_int(
                    task.get("check_interval", 300), 300
                )
    except KeyboardInterrupt:
        print("程序终止。")


if __name__ == "__main__":
    main()

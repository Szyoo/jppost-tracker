import os
import time

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

import bark
from env_schema import TRACKER_ENV_KEYS
from storage import (
    MIN_CHECK_INTERVAL,
    build_tracking_url,
    ensure_storage,
    list_due_tasks,
    load_system_env,
    parse_bark_keys,
    update_task_state,
)

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
DOTENV_PATH = os.path.join(BASE_DIR, ".env")

# 主循环单次休眠上限：即使所有任务都还没到期，也最多 30 秒后重查一次数据库，
# 这样后台新建或改过间隔的任务不必等满一个 check_interval 才被感知。
MAX_LOOP_SLEEP = 30
IDLE_LOOP_SLEEP = 5
# 推送失败后的首次退避秒数，之后按 2 倍递增，上限为任务自己的 check_interval
PUSH_RETRY_BASE = 30

load_dotenv(DOTENV_PATH)
ensure_storage(DOTENV_PATH)

# 日本邮政官网只给日本时间，进程时区固定成东京，日志与解析结果才对得上。
os.environ.setdefault("TZ", "Asia/Tokyo")
if hasattr(time, "tzset"):
    try:
        time.tzset()
    except Exception:
        pass


def _normalize_int(value, default: int) -> int:
    try:
        parsed = int(value)
        return parsed if parsed > 0 else default
    except Exception:
        return default


def _task_interval(task: dict) -> int:
    """任务的轮询间隔。写入口已强制下限，这里再兜一次底：库里可能还有下限出台前写入的小间隔。"""
    return max(MIN_CHECK_INTERVAL, _normalize_int(task.get("check_interval", 300), 300))


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
    bark_server = bark.internal_url(system_env)
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
        "check_interval": _task_interval(task),
        "request_timeout": _normalize_int(system_env.get("REQUEST_TIMEOUT", "15"), 15),
        "bark_server": bark_server,
        "bark_keys": parse_bark_keys(account.get("bark_keys")),
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


def send_bark_notification(config: dict, title: str, message: str) -> tuple[bool, str]:
    """只尝试一次，失败立即返回。重试交给主循环按退避安排——
    在这里 sleep 会让一个任务的重试拖住其他所有任务的轮询。
    返回 (是否成功, 失败原因)。"""
    if not config["bark_server"] or not config["bark_keys"]:
        print(f"{config['log_prefix']} 未配置 Bark 地址或 Bark Keys，跳过推送。")
        return False, "未配置 Bark 地址或 Bark Keys"

    query_params = bark.build_params(
        config["bark_query_params"], config["bark_url_enabled"], config["tracking_url"]
    )

    keys = config["bark_keys"]
    try:
        resp = bark.send_push(
            config["bark_server"], keys, title, message, query_params, config["request_timeout"]
        )

        if 200 <= resp.status_code < 300:
            print(f"{config['log_prefix']} Bark 通知已发送到 {len(keys)} 个设备。")
            return True, ""
        reason = f"HTTP {resp.status_code}"
    except Exception as exc:
        reason = str(exc)

    print(f"{config['log_prefix']} Bark 通知发送失败：{reason}")
    return False, reason


REQUEST_HEADERS = {
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


def parse_tracking_records(html: str) -> list[dict]:
    """解析履历表的全部记录，按时间正序返回 [{"info", "detail"}]。
    info 是「日期 状态」，与 last_tracking_info 同一格式（用它判断有无变化）；
    detail 是同一行其余单元格（详情、取扱局、所在地等），只用于时间线展示。
    选择器与只取最后一行时完全相同，只是把每一行都读出来。"""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", {"class": "tableType01 txt_c m_b5", "summary": "履歴情報"})
    if not table:
        return []

    records = []
    for date_cell in table.find_all("td", class_="w_120"):
        row = date_cell.parent
        if row is None:
            continue
        date_text = date_cell.get_text(strip=True)
        status_cell = row.find("td", class_="w_150")
        status_text = status_cell.get_text(strip=True) if status_cell else ""
        extras = []
        for cell in row.find_all("td"):
            if cell is date_cell or cell is status_cell:
                continue
            text = cell.get_text(" ", strip=True)
            if text:
                extras.append(text)
        records.append({"info": f"{date_text} {status_text}".strip(), "detail": " · ".join(extras)})
    return records


def get_tracking_records(config: dict) -> list[dict]:
    """抓取并解析全部物流记录；请求或解析失败返回空列表。"""
    try:
        response = requests.get(config["tracking_url"], headers=REQUEST_HEADERS, timeout=config["request_timeout"])
        response.raise_for_status()
        return parse_tracking_records(response.text)
    except requests.exceptions.RequestException as exc:
        print(f"{config['log_prefix']} 请求快递信息失败: {exc}")
        return []
    except Exception as exc:
        print(f"{config['log_prefix']} 解析快递信息时出错: {exc}")
        return []


def get_latest_tracking_info(config: dict):
    # 履历表按时间正序排列，最后一行才是最新状态。
    records = get_tracking_records(config)
    return records[-1]["info"] if records else None


def records_since(records: list[dict], last_info: str) -> list[dict]:
    """上次已知记录之后新增的记录（两次轮询之间可能走过好几个状态）。
    首次抓取（last_info 为空）把全部历史记下来；上次的记录在页面上找不到时
    （官网改了格式等）只算最新一条，免得把整段历史当成新增。"""
    if not records:
        return []
    if not last_info:
        return list(records)
    infos = [record["info"] for record in records]
    if last_info in infos:
        index = len(infos) - 1 - infos[::-1].index(last_info)
        return list(records[index + 1:])
    return [records[-1]]


def process_task(task: dict, system_env: dict) -> bool:
    """处理一个任务。返回 True 表示这轮需要尽快重试（推送失败），
    由主循环按退避安排下一次，本函数绝不阻塞等待。
    所有回写都带上抓取时的单号：抓取期间单号被改了，这轮结果直接作废。"""
    config = build_runtime_config(task, system_env)
    prefix = config["log_prefix"]
    task_id = config["task_id"]
    number = config["tracking_number"]

    missing = validate_config(config)
    if missing:
        error = f"缺少必要配置: {', '.join(missing)}"
        print(f"{prefix} {error}")
        update_task_state(task_id, error=error, tracking_number=number, query_ok=False)
        return False

    records = get_tracking_records(config)
    if not records:
        update_task_state(task_id, error="无法获取最新快递信息。", tracking_number=number, query_ok=False)
        return False

    current_info = records[-1]["info"]
    print(f"{prefix} 最新物流记录: {current_info}")
    if current_info == config["last_tracking_info"]:
        print(f"{prefix} 暂无更新。")
        update_task_state(task_id, latest_info=current_info, error="", tracking_number=number, query_ok=True)
        return False

    new_records = records_since(records, config["last_tracking_info"])
    title, body = build_push_message(config, current_info)
    pushed, reason = send_bark_notification(config, title, body)
    if pushed:
        update_task_state(
            task_id,
            latest_info=current_info,
            error="",
            pushed=True,
            tracking_number=number,
            query_ok=True,
            new_records=new_records,
            push_status=current_info,
            push_detail=f"已推送到 {len(config['bark_keys'])} 个设备",
        )
        return False

    # 推送失败时刻意不写入 latest_info：写了下一轮就会判定"无更新"而不再推送，
    # 这条物流变化的通知就永久丢了。保持旧值，等重试成功再落库。
    # 状态变化事件照常记（storage 侧去重，重试时不会重复记）。
    update_task_state(
        task_id,
        error=f"推送失败（{reason}），稍后重试。",
        tracking_number=number,
        query_ok=True,
        new_records=new_records,
        push_status=current_info,
        push_failed=reason,
    )
    return True


def main():
    print("多任务快递监控程序启动...")
    next_runs: dict[int, float] = {}
    push_failures: dict[int, int] = {}
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
                    push_failures.pop(stale_id, None)

            # 没有记录过的任务默认到期时刻为 0，也就是新任务立刻抓一次。
            due_tasks = [task for task in tasks if now >= next_runs.get(int(task["id"]), 0.0)]

            if not due_tasks:
                # 睡到最近一个任务到期，避免每秒空转查库；上限见 MAX_LOOP_SLEEP。
                next_due = min(next_runs.get(int(task["id"]), now) for task in tasks)
                time.sleep(max(0.5, min(MAX_LOOP_SLEEP, next_due - now)))
                continue

            # 每轮重读 .env：后台「系统设置」的修改与清空在下一轮即生效，
            # 文件里出现过的键以文件为准，不会回落到启动时继承的旧环境变量
            system_env = load_system_env(DOTENV_PATH, TRACKER_ENV_KEYS)
            for task in due_tasks:
                task_id = int(task["id"])
                interval = _task_interval(task)
                needs_retry = process_task(task, system_env)

                if needs_retry:
                    # 推送失败：指数退避重试（30s、60s、120s…），上限不超过任务自己的间隔。
                    # 退避由调度器安排，所以别的任务照常轮询，不会被这个任务拖住。
                    fails = push_failures.get(task_id, 0) + 1
                    push_failures[task_id] = fails
                    delay = min(PUSH_RETRY_BASE * (2 ** (fails - 1)), interval)
                    print(f"[调度] 任务 {task_id} 推送失败第 {fails} 次，{int(delay)} 秒后重试。")
                else:
                    push_failures.pop(task_id, None)
                    delay = interval

                # 从本次处理结束算起，免得抓取耗时把下一轮挤到马上又触发。
                next_runs[task_id] = time.time() + delay
    except KeyboardInterrupt:
        print("程序终止。")


if __name__ == "__main__":
    main()

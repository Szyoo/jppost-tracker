import os
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from dotenv import dotenv_values, load_dotenv

import ai_classify
import bark
import env_schema
import forecast
import track17
from route_nodes import ONCE_ROLES, node_notified, node_title, push_eligible, pushable_nodes
# 认定为「已签收」的状态（只做整串精确匹配）定义在 forecast，签收自动归档与到达预测的阶段映射共用同一份
from forecast import DELIVERED_STATUSES
from forecast import origin_country as forecast_origin_country
from env_schema import BOOL_FALSE, BOOL_TRUE, ENV_FIELDS, TRACKER_ENV_KEYS
from storage import (
    MIN_CHECK_INTERVAL,
    build_tracking_url,
    ensure_storage,
    list_due_tasks,
    load_system_env,
    mark_nodes_notified,
    notified_node_keys,
    parse_bark_keys,
    record_carrier_records,
    register_unrecognized_statuses,
    record_cn_records,
    task_forecast,
    task_nodes,
    task_has_event,
    track17_catchup_candidates,
    track17_state,
    track17_update,
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

# 记录的 info 是「日期 时间 状态」（见 parse_tracking_records），状态前的日期时间要先剥掉
_INFO_DATETIME_PREFIX = re.compile(r"^\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}\s*")
# 已归档单号补抓始发国段的间隔（秒）：每隔这么久最多补一批（track17.MAX_NUMBERS 个）
CN_CATCHUP_INTERVAL = 30 * 60
# 账号没配 Bark Keys 时照常追踪、只是不推送；这句提示留在 last_error 上，免得用户以为会收到通知
NO_PUSH_HINT = "未配置 Bark Keys（账号设置），只记录物流状态、不推送。"

load_dotenv(DOTENV_PATH)
ensure_storage(DOTENV_PATH)

# 日本邮政官网只给日本时间，进程时区固定成东京，日志与解析结果才对得上。
os.environ.setdefault("TZ", "Asia/Tokyo")
if hasattr(time, "tzset"):
    try:
        time.tzset()
    except Exception:
        pass


def load_tracker_env() -> dict:
    """每轮重读的系统设置。带 unset_default 的键（TRANSIT_REGION_LABEL）在 .env 与环境变量里都没出现时从结果里去掉，
    这样读取方能区分「没设置（用默认）」与「显式留空（用通用名）」。"""
    values = load_system_env(DOTENV_PATH, TRACKER_ENV_KEYS)
    present = set(dotenv_values(DOTENV_PATH)) if os.path.exists(DOTENV_PATH) else set()
    for key in TRACKER_ENV_KEYS:
        if ENV_FIELDS[key].get("unset_default") and key not in present and key not in os.environ:
            values.pop(key, None)
    return values


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


def tracking_status(info) -> str:
    """从「2026/10/27 13:50 お届け先にお届け済み」这样的记录里取出状态部分；没有日期前缀时整串即状态。"""
    text = str(info or "").strip()
    return _INFO_DATETIME_PREFIX.sub("", text, count=1).strip()


def is_delivered(info) -> bool:
    """这条物流记录是否表示已签收。纯函数，只认 DELIVERED_STATUSES 里的状态（整串精确匹配），
    认定为签收的字样：お届け先にお届け済み、お届け済み、窓口でお渡し。
    空串、None、不在、持ち戻り、お届け予定等一律不算。"""
    status = tracking_status(info)
    return bool(status) and status in DELIVERED_STATUSES


def auto_archive_enabled(system_env: dict) -> bool:
    """系统设置 AUTO_ARCHIVE_DELIVERED：留空或不认识的值按默认（开）。"""
    value = str(system_env.get("AUTO_ARCHIVE_DELIVERED", "") or "").strip().lower()
    if value in BOOL_TRUE:
        return True
    if value in BOOL_FALSE:
        return False
    return ENV_FIELDS["AUTO_ARCHIVE_DELIVERED"]["default"] == "1"


def push_title(config: dict, node: dict) -> str:
    """推送标题：「节点名 · 备注」，没有备注时用单号后 4 位（同一账号多个包裹推到同一台设备，要分得清是哪一件）。
    还没归类的日本邮政新状态用「原文（未识别）」。"""
    tag = config["label"] or str(config["tracking_number"] or "")[-4:]
    return f"{node_title(node)} · {tag}"


def _node_when(node) -> str:
    """正文算不出来时的兜底：节点时间「11/13 07:18」（只有日期时「11/14」）。"""
    moment = node["moment"]
    return f"{moment.month}/{moment.day}" + ("" if len(node["at"]) <= 10 else f" {moment:%H:%M}")


def build_push_message(config: dict, node: dict, body: str = "") -> tuple[str, str]:
    """一轮只推一个节点（本轮最新的白名单节点）。正文是 forecast.push_body 的一行
    （「第 N 天 · 预计 11/14（周六）上午签收」/「全程 X 天」）；算不出来时用节点时间。"""
    return push_title(config, node), (body or _node_when(node))


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
    # 没配 Bark Keys 不算缺配置：照常追踪、记时间线（签收后也照常自动归档），只是不推送。
    missing = []
    if not config["tracking_number"]:
        missing.append("单号")
    if config["bark_keys"] and not config["bark_server"]:
        missing.append("Bark 服务地址（系统设置）")
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


# --- 始发国段（中国邮政，经 17TRACK） ---

# sync_cn_leg 的结果穷举：disabled 没配密钥 / not_cn 不是中国寄出的单号 / registered 这次刚登记（数据要一两分钟后才有）/
# pending 已登记但还没有数据 / synced 查到并写入了事件 / error 17TRACK 出错（原因记在 track17_numbers.last_error）
CN_SYNC_RESULTS = ("disabled", "not_cn", "registered", "pending", "synced", "error")


def track17_api_key(system_env: dict) -> str:
    return str(system_env.get("TRACK17_API_KEY", "") or "").strip()


def sync_cn_leg(task_id: int, number: str, system_env: dict, *, timeout: int, log_prefix: str = "",
                fetch_after_register: bool = False):
    """CN 单号的始发国段同步一次：没登记过 → 登记（成功或「已登记」都记为已登记，每个单号只登记一次）；
    已登记 → 查轨迹，把中国邮政的全部事件写进 carrier_records（source='cn_ems'，带单号校验）。
    fetch_after_register：刚登记完也立刻查一次（补录用，多半还没数据）。
    返回 (结果, 写入条数)，结果见 CN_SYNC_RESULTS。所有异常都在这里兜住，只记日志与 last_error。"""
    api_key = track17_api_key(system_env)
    if not api_key:
        return "disabled", 0
    if forecast_origin_country(number) != "CN":
        return "not_cn", 0
    prefix = log_prefix or f"[{number}]"
    try:
        state = track17_state(number)
        just_registered = False
        if state is None or not state.get("registered_at"):
            reason = track17.register([number], api_key, timeout).get(number, "")
            if reason:
                track17_update(number, error=reason)
                print(f"{prefix} 17TRACK 登记失败：{reason}")
                return "error", 0
            track17_update(number, registered=True, error="")
            print(f"{prefix} 已在 17TRACK 登记，始发国段稍后补上。")
            if not fetch_after_register:
                return "registered", 0
            just_registered = True
        events = track17.get_cn_events([number], api_key, timeout).get(number)
        if not events:
            track17_update(number, error="")
            return ("registered" if just_registered else "pending"), 0
        count = record_cn_records(task_id, number, events)
        if count is None:
            # 抓取期间单号被改走：这次结果作废，不记 synced
            track17_update(number, error="")
            return "pending", 0
        track17_update(number, synced=True, error="")
        return "synced", count
    except Exception as exc:  # 17TRACK 的任何问题都不能影响日本邮政追踪
        try:
            track17_update(number, error=str(exc)[:300])
        except Exception:
            pass
        print(f"{prefix} 17TRACK 查询出错：{exc}")
        return "error", 0


def catch_up_cn_legs(system_env: dict, *, timeout: int, now=None) -> int:
    """已归档（不再轮询）但始发国段还没补全的单号，批量查一次（最多 track17.MAX_NUMBERS 个，只查不登记）。
    返回写入了事件的单号数。异常只记日志。"""
    api_key = track17_api_key(system_env)
    if not api_key:
        return 0
    try:
        candidates = track17_catchup_candidates(now, limit=track17.MAX_NUMBERS)
        if not candidates:
            return 0
        numbers = [number for number, _ in candidates]
        try:
            found = track17.get_cn_events(numbers, api_key, timeout)
        except Exception as exc:
            for number in numbers:
                track17_update(number, error=str(exc)[:300])
            print(f"[始发国段] 补抓 {len(numbers)} 个归档单号失败：{exc}")
            return 0
        synced = 0
        for number, task_id in candidates:
            events = found.get(number)
            if events and record_cn_records(task_id, number, events) is not None:
                track17_update(number, synced=True, error="")
                synced += 1
            else:
                track17_update(number, error="")
        print(f"[始发国段] 补抓 {len(numbers)} 个归档单号，{synced} 个拿到了中国邮政轨迹。")
        return synced
    except Exception as exc:
        print(f"[始发国段] 补抓出错：{exc}")
        return 0


def transit_region(system_env: dict) -> str:
    """中转地名：system_env 里没有这个键（.env 与环境变量都没设）时按默认，显式留空用通用名。"""
    return env_schema.transit_region_label(system_env)


def push_forecast_enabled(system_env: dict) -> bool:
    """系统设置 PUSH_FORECAST：留空或不认识的值按默认（开）。"""
    value = str(system_env.get("PUSH_FORECAST", "") or "").strip().lower()
    if value in BOOL_TRUE:
        return True
    if value in BOOL_FALSE:
        return False
    return ENV_FIELDS["PUSH_FORECAST"]["default"] == "1"


def select_new_nodes(task_id: int, number: str, region: str):
    """这轮要推送的节点与要静默记下的节点：(push, silent)，push 最多一个。
    - 可推送节点：合并履历里可见的承运方节点（route_nodes.pushable_nodes）；
    - 已推送过的（route_nodes.node_notified：同一节点另一边晚到也算推送过）跳过；
    - 只推白名单（route_nodes.push_eligible：有推送角色；「航班到达日本」两个说法只认最早那个，
      已记下过其中一个就不再推另一个），其余新节点静默记下；
    - 早于已处理节点里最晚那个的新节点（例如 17TRACK 数据晚到、补上了更早的国内段事件）静默记下，不补发；
    - 同一轮有多个可推节点时只推最新的一个，其余静默记下（新任务首次抓取也一样只推一条）。"""
    nodes = pushable_nodes(task_nodes(task_id, region))
    notified = notified_node_keys(task_id, number)
    fresh = [node for node in nodes if not node_notified(node, notified)]
    if not fresh:
        return [], []
    done = [node for node in nodes if node_notified(node, notified)]
    floor = max(node["moment"] for node in done) if done else None
    done_once = {node["role"] for node in done} & ONCE_ROLES
    eligible = push_eligible(nodes)
    candidates = [node for node in fresh if node["key"] in eligible and node["role"] not in done_once
                  and (floor is None or node["moment"] >= floor)]
    push = candidates[-1:]
    return push, [node for node in fresh if node not in push]


def process_task(task: dict, system_env: dict) -> bool:
    """处理一个任务。返回 True 表示这轮需要尽快重试（推送失败），
    由主循环按退避安排下一次，本函数绝不阻塞等待。
    所有回写都带上抓取时的单号：抓取期间单号被改了，这轮结果直接作废。

    每轮先同步两边（17TRACK 的中国邮政、日本邮政官网），把日本邮政履历写进 carrier_records，
    再算合并履历（route_nodes），对「还没推送过、且在合并履历里可见」的新节点只推白名单里本轮最新的一个，
    其余新节点记为已处理不推送；哪边先报就由那一轮推送，另一边晚到的同一节点不再推送（notified_nodes）。
    推送失败时本轮节点都不记，下轮按退避重推。
    task_events 的 status_change 照旧按日本邮政履历写；签收自动归档照旧按日本邮政的签收状态判断。"""
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

    # 始发国段与日本邮政互不影响：sync_cn_leg 自己兜住全部异常
    sync_cn_leg(task_id, number, system_env, timeout=config["request_timeout"], log_prefix=prefix)

    records = get_tracking_records(config)
    if not records:
        update_task_state(task_id, error="无法获取最新快递信息。", tracking_number=number, query_ok=False)
        return False

    current_info = records[-1]["info"]
    print(f"{prefix} 最新物流记录: {current_info}")
    push_enabled = bool(config["bark_keys"])
    ok_error = "" if push_enabled else NO_PUSH_HINT
    auto_archive_on = auto_archive_enabled(system_env)

    jp_changed = current_info != config["last_tracking_info"]
    new_records = records_since(records, config["last_tracking_info"]) if jp_changed else []
    if jp_changed:
        delivered = auto_archive_on and (
            is_delivered(current_info) or any(is_delivered(record["info"]) for record in new_records)
        )
    else:
        # 已签收、上轮也已处理过，但从没自动归档过（开关后来才打开、或功能上线前就签收了的任务）：
        # 这轮补归档。自动归档过又被用户取消归档的不再归档，尊重用户的选择。
        delivered = (
            auto_archive_on
            and is_delivered(current_info)
            and not task_has_event(task_id, number, "auto_archived")
        )

    # 先把这轮的日本邮政履历落库，合并节点才算得上它；单号已被改走就整轮作废
    if record_carrier_records(task_id, number, records) is None:
        return False
    # 内置表认不出、也还没有规则的状态登记为待归类（后台归类见 ai_classify），失败不影响追踪
    try:
        for status in register_unrecognized_statuses(number):
            print(f"{prefix} 新状态待归类：{status}")
    except Exception as exc:
        print(f"{prefix} 登记待归类状态失败：{exc}")
    region = transit_region(system_env)
    new_nodes, silent = select_new_nodes(task_id, number, region)
    # 本轮没有要推的：不推的新节点直接记下；有要推的：其余节点随推送成功一起记，推送失败整批下轮重算
    if silent and not (push_enabled and new_nodes):
        mark_nodes_notified(task_id, number, silent)

    if not push_enabled or not new_nodes:
        if not jp_changed and not new_nodes:
            print(f"{prefix} 暂无更新。")
        update_task_state(
            task_id,
            latest_info=current_info,
            error=ok_error,
            tracking_number=number,
            carrier_records=records,
            query_ok=True,
            new_records=new_records,
            auto_archive=delivered,
            # 没配推送：新节点直接记下，以后配上 Keys 也不补发旧节点
            notified_nodes=new_nodes,
        )
        if delivered:
            print(f"{prefix} 已签收，自动归档，不再轮询。")
        return False

    node = new_nodes[-1]
    body = ""
    try:
        body = forecast.push_body(task_forecast(task_id, transit_region=region), datetime.now(),
                                  with_forecast=push_forecast_enabled(system_env))
    except Exception as exc:  # 预测失败绝不影响推送
        print(f"{prefix} 推送正文的到达预测计算失败：{exc}")
    title, body = build_push_message(config, node, body)
    push_status = node["name"]
    pushed, reason = send_bark_notification(config, title, body)
    if pushed:
        # 签收的这轮先照常推送，推送成功后在同一次回写里归档：事件顺序为 status_change → push_ok → auto_archived
        update_task_state(
            task_id,
            latest_info=current_info,
            error="",
            pushed=True,
            tracking_number=number,
            carrier_records=records,
            query_ok=True,
            new_records=new_records,
            push_status=push_status,
            push_detail=f"已推送到 {len(config['bark_keys'])} 个设备",
            auto_archive=delivered,
            notified_nodes=new_nodes + silent,
        )
        if delivered:
            print(f"{prefix} 已签收，推送成功后自动归档，不再轮询。")
        return False

    # 推送失败：节点不记为已推送，latest_info 也不写（保持旧值），下轮按退避重推同一批节点。
    # 状态变化事件照常记（storage 侧去重，重试时不会重复记）。
    # 签收也一样：这轮不归档、保持启用，推送成功的那一轮再归档（签收通知不能丢）。
    update_task_state(
        task_id,
        error=f"推送失败（{reason}），稍后重试。",
        tracking_number=number,
        carrier_records=records,
        query_ok=True,
        new_records=new_records,
        push_status=push_status,
        push_failed=reason,
    )
    return True


def main():
    print("多任务快递监控程序启动...")
    next_runs: dict[int, float] = {}
    push_failures: dict[int, int] = {}
    last_empty_log_at = 0.0
    last_cn_catchup_at = 0.0

    try:
        while True:
            if time.time() - last_cn_catchup_at >= CN_CATCHUP_INTERVAL:
                last_cn_catchup_at = time.time()
                catchup_env = load_tracker_env()
                catch_up_cn_legs(
                    catchup_env, timeout=_normalize_int(catchup_env.get("REQUEST_TIMEOUT", "15"), 15)
                )

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
            system_env = load_tracker_env()
            # 待归类的新状态：低频顺带处理几条（自己限频、兜住全部异常）
            ai_classify.run_once(system_env)
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

"""到达预测：承运方履历 → 阶段，再用历史单号的阶段耗时估计剩余阶段的到达时间。

本模块是纯函数，不碰数据库与网络（storage 负责取数，再交给 build_forecast），也不 import 本项目其他模块，
所以 storage / tracker 都可以放心 import 它。时间一律是日本时间的 naive datetime（容器 TZ=Asia/Tokyo）。

阶段（STAGE_META）、可信度（CONFIDENCE_META）都是穷举：前端 src/static/forecast.js 各有一份，
测试比对两边一致；前端遇到未知 key 直接报错，不显示原值。
承运方的状态字符串是外部数据无法穷举，认不出的状态不参与阶段判定（返回 None）。
"""
import re
from datetime import datetime, timedelta, timezone

# 认定为「已签收」的状态（履历表「配達状況」一栏）。只做整串精确匹配，
# 「お届け先にお伺いしましたがご不在」「ご不在のため持ち戻り」「お届け予定」之类都不算：
# - お届け先にお届け済み：日本邮政官网对送达收件人的标准写法
# - お届け済み：同义的简写
# - 窓口でお渡し：在邮局窗口交给收件人（局留め、不在后到窗口自取）
# 签收自动归档（tracker.is_delivered）与阶段映射（delivered）共用这一份，tracker 从这里 re-export。
DELIVERED_STATUSES = frozenset({"お届け先にお届け済み", "お届け済み", "窓口でお渡し"})

# 阶段穷举，按时间先后排列（dict 保序，顺序即先后）。前三个是发货前节点（pre_milestones），其余来自承运方履历。
STAGE_META = {
    "ordered": {"label": "下单寄出"},
    "packed": {"label": "打包完成"},
    "handed_over": {"label": "出库交运"},
    "accepted": {"label": "引受"},
    "origin_exchange": {"label": "到达始发互换局"},
    "origin_customs": {"label": "始发国海关"},
    "departed_origin": {"label": "离开始发国"},
    "arrived_japan": {"label": "到达日本"},
    "japan_customs": {"label": "日本通关"},
    "departed_exchange": {"label": "离开日本互换局"},
    "local_arrived": {"label": "到达派送局"},
    "out_for_delivery": {"label": "派送中"},
    "delivered": {"label": "签收"},
}
STAGES = tuple(STAGE_META)
STAGE_ORDER = {stage: index for index, stage in enumerate(STAGES)}
PRE_STAGES = ("ordered", "packed", "handed_over")
# 「第 N 天」的起点候选，按优先级：有下单用下单，没有用出库，再没有用引受
ORIGIN_STAGES = ("ordered", "handed_over", "accepted")

# 可信度穷举：none 没有可参考样本 / borrowed 同渠道 0 单只借鉴其他渠道 / rough 同渠道 1–4 单 / solid 同渠道 ≥5 单
CONFIDENCE_META = {
    "none": {"label": "还没有可参考的历史"},
    "borrowed": {"label": "同渠道还没有记录，借鉴其他渠道"},
    "rough": {"label": "同渠道样本较少，仅供粗估"},
    "solid": {"label": "同渠道样本充足"},
}
SOLID_MIN_SAMPLES = 5

# 同渠道样本权重 1.0，其他渠道 / 未标注 0.3
SAME_CHANNEL_WEIGHT = 1.0
OTHER_CHANNEL_WEIGHT = 0.3
# 合格样本达到这么多单才用加权 20%–80% 分位当区间，否则用 min–max
QUANTILE_MIN_SAMPLES = 3
LOW_QUANTILE = 0.2
HIGH_QUANTILE = 0.8
# 出现率低于它的阶段标为「可能出现」
MAYBE_BELOW = 0.5

CHANNEL_MAX_LENGTH = 50
JST = timezone(timedelta(hours=9))
# 只有日期没有时刻的记录 / 节点按当天 12:00 计
DATE_ONLY_HOUR = 12


# --- 时间 ---

# 履历 info 的日期前缀：「2026/10/21 13:30 状态」或只有日期「2026/10/27 状态」。
# 日期与时刻之间的空白可有可无（单元格里换行时 get_text(strip=True) 会把两者粘在一起）
_INFO_RE = re.compile(r"^\s*(\d{4})/(\d{1,2})/(\d{1,2})(?:\s*(\d{1,2}):(\d{2}))?\s*(.*?)\s*$", re.S)


def parse_record_info(info):
    """把履历 info 拆成 (record_at, status)。record_at 统一成 'YYYY-MM-DD HH:MM'，只有日期时为 'YYYY-MM-DD'。
    没有可解析的日期前缀（或日期不合法）返回 None——放不进时间轴的记录不入库。"""
    match = _INFO_RE.match(str(info or ""))
    if not match:
        return None
    year, month, day, hour, minute, status = match.groups()
    try:
        if hour is None:
            moment = datetime(int(year), int(month), int(day))
            record_at = moment.strftime("%Y-%m-%d")
        else:
            moment = datetime(int(year), int(month), int(day), int(hour), int(minute))
            record_at = moment.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return record_at, status


def parse_stored_time(value):
    """库里的 'YYYY-MM-DD HH:MM' / 'YYYY-MM-DD' → datetime；只有日期按当天 12:00。不合法返回 None。"""
    text = str(value or "").strip()
    for fmt, date_only in (("%Y-%m-%d %H:%M", False), ("%Y-%m-%d", True)):
        try:
            moment = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return moment.replace(hour=DATE_ONLY_HOUR) if date_only else moment
    return None


def format_time(moment) -> str:
    return moment.strftime("%Y-%m-%d %H:%M")


_LOCAL_TIME_FORMATS = (
    ("%Y-%m-%d %H:%M:%S", True),
    ("%Y-%m-%d %H:%M", True),
    ("%Y-%m-%dT%H:%M:%S", True),
    ("%Y-%m-%dT%H:%M", True),
    ("%Y-%m-%d", False),
)


def normalize_milestone_time(value, *, allow_tz: bool = False) -> str:
    """发货前节点的时间输入 → 'YYYY-MM-DD HH:MM'（只给日期的存 'YYYY-MM-DD'）。
    接受 'YYYY-MM-DD'、'YYYY-MM-DD HH:MM[:SS]'（以及表单常见的 'T' 分隔）；
    allow_tz=True 时再接受带时区的 ISO 8601（Z 或 ±HH:MM），换算成日本时间。不合法抛 ValueError。"""
    text = str(value or "").strip()
    if not text:
        raise ValueError("时间不能为空。")
    for fmt, has_time in _LOCAL_TIME_FORMATS:
        try:
            moment = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return moment.strftime("%Y-%m-%d %H:%M") if has_time else moment.strftime("%Y-%m-%d")
    if allow_tz:
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            moment = None
        if moment is not None and moment.tzinfo is not None:
            return moment.astimezone(JST).strftime("%Y-%m-%d %H:%M")
    raise ValueError(f"时间格式不正确：{text}（应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM）")


def normalize_channel(value) -> str:
    """渠道名：去首尾空白，最长 50；空串 = 未标注。超长抛 ValueError。"""
    channel = str(value or "").strip()
    if len(channel) > CHANNEL_MAX_LENGTH:
        raise ValueError(f"渠道名不能超过 {CHANNEL_MAX_LENGTH} 个字符。")
    return channel


# --- 阶段映射 ---

# 日本境内的取扱局：detail 最后一段（「板橋北郵便局 · 東京都」的「東京都」）以都道府县结尾
_JAPAN_SUFFIXES = ("都", "道", "府", "県")


def _in_japan(detail) -> bool:
    parts = [part.strip() for part in str(detail or "").split("·") if part.strip()]
    return bool(parts) and parts[-1].endswith(_JAPAN_SUFFIXES)


def _jp_post_stage(status: str, detail: str):
    """日本邮政履历的状态 → 阶段。同名状态（国際交換局に到着 / 国際交換局から発送 / 税関…）在始发国与日本都会出现，
    靠 detail 判断所在地。认不出的状态（ご不在、持ち戻り 等）返回 None。"""
    if status in DELIVERED_STATUSES:
        return "delivered"
    if status == "持ち出し中":
        return "out_for_delivery"
    if status == "到着":
        return "local_arrived"
    if status == "引受":
        return "accepted"
    japan = _in_japan(detail)
    if status == "国際交換局に到着":
        return "arrived_japan" if japan else "origin_exchange"
    if status == "国際交換局から発送":
        return "departed_exchange" if japan else "departed_origin"
    if "税関" in status or status.startswith("通関"):
        return "japan_customs" if japan else "origin_customs"
    return None


# 承运方来源穷举（carrier_records.source 的 CHECK 约束由它派生）：每个来源一支状态→阶段映射。
# 以后接入始发国承运方时在这里加一项（例如 'cn_ems'），映射函数签名相同。
CARRIER_SOURCES = {
    "jp_post": _jp_post_stage,
}


def record_stage(source: str, status, detail=""):
    """一条承运方记录对应的阶段 key；认不出返回 None。来源不在 CARRIER_SOURCES 里抛 ValueError。"""
    if source not in CARRIER_SOURCES:
        raise ValueError(f"未定义的承运方来源：{source}")
    return CARRIER_SOURCES[source](str(status or "").strip(), str(detail or ""))


def stages_from_records(records, milestones=None) -> dict:
    """一单的阶段时间：每个阶段取最早出现的时间。
    records：[{"source", "record_at", "status", "detail"}]；milestones：{stage: 时间字符串}（发货前节点）。
    返回 {stage: datetime}。"""
    stages: dict = {}

    def put(stage, moment):
        if stage is None or moment is None:
            return
        if stage not in stages or moment < stages[stage]:
            stages[stage] = moment

    for record in records or []:
        put(record_stage(record["source"], record["status"], record.get("detail", "")),
            parse_stored_time(record["record_at"]))
    for stage, at in (milestones or {}).items():
        if stage not in PRE_STAGES:
            raise ValueError(f"未定义的发货前节点：{stage}")
        put(stage, parse_stored_time(at))
    return stages


def merge_stages(*stage_maps) -> dict:
    """同单号挂在多个任务下：阶段时间逐项取最早。"""
    merged: dict = {}
    for stage_map in stage_maps:
        for stage, moment in stage_map.items():
            if stage not in merged or moment < merged[stage]:
                merged[stage] = moment
    return merged


# --- 统计 ---

def weighted_quantile(values, weights, q: float) -> float:
    """加权分位（取下侧值）：按值排序，累计权重第一次达到 q × 总权重的那个值。
    等权且偶数个时中位数取较小的那个，不插值——样本很少时插值出来的「中间值」并没有更可信。"""
    pairs = sorted(zip(values, weights))
    total = sum(weight for _, weight in pairs)
    if not pairs or total <= 0:
        raise ValueError("没有样本。")
    target = q * total
    acc = 0.0
    for value, weight in pairs:
        acc += weight
        if acc >= target - 1e-9:
            return value
    return pairs[-1][0]


def _latest_stage(stages: dict):
    """当前阶段：时间最晚的那个，时间相同取顺序靠后的。"""
    return max(stages, key=lambda stage: (stages[stage], STAGE_ORDER[stage]))


def _origin(stages: dict):
    for stage in ORIGIN_STAGES:
        if stage in stages:
            return stage
    return None


def _confidence(same: int, other: int) -> str:
    if same + other == 0:
        return "none"
    if same == 0:
        return "borrowed"
    if same < SOLID_MIN_SAMPLES:
        return "rough"
    return "solid"


def _leave_seconds(sample_stages: dict, stage: str, t_stage):
    """样本离开 stage 的耗时：顺序在它之后、时间不早于它的阶段里最早到达的那个减去 t_stage。没有返回 None。"""
    later = [
        (moment - t_stage).total_seconds()
        for other, moment in sample_stages.items()
        if STAGE_ORDER[other] > STAGE_ORDER[stage] and moment >= t_stage
    ]
    return min(later) if later else None


def build_forecast(current_stages: dict, current_channel: str, samples, now: datetime) -> dict:
    """预测剩余阶段。

    current_stages：当前单 {stage: datetime}；samples：[{"stages": {...}, "channel": str}]（已按单号去重、排除当前单号）；
    now：当前时间（参数传入，便于测试）。只输出聚合统计，不带样本的单号与账号。

    - 起点：ordered → handed_over → accepted 中最先存在的那个；「第 N 天」按自然日算：起点当天是第 1 天，
      跨过一个午夜加 1（看包裹的人按日历数天，不按 24 小时取整）。
    - 当前阶段 S = 时间最晚的已到阶段，已等 w = now - t_S；已签收不预测，只给全程用时。
    - 可用样本：有 S 且 S 之后（顺序靠后、时间不早于 t_S）至少到过一个阶段的单。
      合格样本：离开 S 的耗时 > w（已经等了 w 还没走，就只跟同样「走得慢」的历史比）；
      没有合格样本时退回全部可用样本并标 overdue（已比历史上最慢的还久）。
    - 每个剩余阶段 X：有 X 的合格样本 d = t_X - t_S，剩余 = max(d - w, 0)，同渠道权重 1.0、其他 0.3，
      输出加权中位数、区间（≥3 单用 20%–80% 分位，否则 min–max）、出现率（<0.5 标「可能出现」，0 不列）。"""
    channel = str(current_channel or "").strip()
    arrived = sorted(current_stages.items(), key=lambda item: (item[1], STAGE_ORDER[item[0]]))
    origin = _origin(current_stages)
    result = {
        "channel": channel,
        "generated_at": format_time(now),
        "origin": {"stage": origin, "at": format_time(current_stages[origin])} if origin else None,
        "day": None,
        "delivered": "delivered" in current_stages,
        "total_seconds": None,
        "current": None,
        "arrived": [{"stage": stage, "at": format_time(moment)} for stage, moment in arrived],
        "remaining": [],
        "confidence": "none",
        "samples": {"same_channel": 0, "other": 0},
        "overdue": False,
    }
    if not current_stages:
        return result

    current = _latest_stage(current_stages)
    t_current = current_stages[current]
    if result["delivered"]:
        delivered_at = current_stages["delivered"]
        result["current"] = {"stage": "delivered", "at": format_time(delivered_at), "waited_seconds": 0}
        if origin:
            result["total_seconds"] = max(0, int((delivered_at - current_stages[origin]).total_seconds()))
        return result

    waited = max(0.0, (now - t_current).total_seconds())
    result["current"] = {"stage": current, "at": format_time(t_current), "waited_seconds": int(waited)}
    if origin:
        result["day"] = (now.date() - current_stages[origin].date()).days + 1

    usable = []
    for sample in samples:
        stages = sample["stages"]
        if current not in stages:
            continue
        leave = _leave_seconds(stages, current, stages[current])
        if leave is None:
            continue
        same = bool(channel) and str(sample.get("channel") or "").strip() == channel
        usable.append({"stages": stages, "leave": leave, "same": same})

    qualified = [sample for sample in usable if sample["leave"] > waited]
    if usable and not qualified:
        qualified = usable
        result["overdue"] = True

    same_count = sum(1 for sample in qualified if sample["same"])
    other_count = len(qualified) - same_count
    result["samples"] = {"same_channel": same_count, "other": other_count}
    result["confidence"] = _confidence(same_count, other_count)
    if not qualified:
        return result

    def weight(sample):
        return SAME_CHANNEL_WEIGHT if sample["same"] else OTHER_CHANNEL_WEIGHT

    total_weight = sum(weight(sample) for sample in qualified)
    for stage in STAGES:
        if STAGE_ORDER[stage] <= STAGE_ORDER[current] or stage in current_stages:
            continue
        values, weights, same_x = [], [], 0
        for sample in qualified:
            stages = sample["stages"]
            t_s = stages[current]
            if stage not in stages or stages[stage] < t_s:
                continue
            values.append(max(0.0, (stages[stage] - t_s).total_seconds() - waited))
            weights.append(weight(sample))
            same_x += 1 if sample["same"] else 0
        if not values:
            continue
        median = weighted_quantile(values, weights, 0.5)
        if len(values) >= QUANTILE_MIN_SAMPLES:
            low = weighted_quantile(values, weights, LOW_QUANTILE)
            high = weighted_quantile(values, weights, HIGH_QUANTILE)
        else:
            low, high = min(values), max(values)
        occurrence = sum(weights) / total_weight
        result["remaining"].append({
            "stage": stage,
            "median_seconds": int(median),
            "low_seconds": int(low),
            "high_seconds": int(high),
            "eta": format_time(now + timedelta(seconds=median)),
            "eta_low": format_time(now + timedelta(seconds=low)),
            "eta_high": format_time(now + timedelta(seconds=high)),
            "occurrence": round(occurrence, 3),
            "maybe": occurrence < MAYBE_BELOW,
            "samples": {"same_channel": same_x, "other": len(values) - same_x},
        })
    return result

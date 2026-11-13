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

# 段穷举，按时间先后排列。每个阶段归属一个段（STAGE_META 的 segment），前端按段分组显示：
# warehouse 仓库段（发货前节点）/ domestic 国内段（始发国邮政）/ transit 中转段（经第三地中转的航班）/ japan 日本段。
# 中转段的显示名取系统设置 TRANSIT_REGION_LABEL（如「韩国」→「韩国段」「到达韩国」），留空用这里的通用名。
SEGMENT_META = {
    "warehouse": {"label": "仓库段"},
    "domestic": {"label": "国内段"},
    "transit": {"label": "中转段"},
    "japan": {"label": "日本段"},
}
SEGMENTS = tuple(SEGMENT_META)

# 阶段穷举，按时间先后排列（dict 保序，顺序即先后）。前三个是发货前节点（pre_milestones），其余来自承运方履历。
# left_collection / handed_to_airline / flight_departed / transit_* / landed_destination 只有始发国段（cn_ems）才有。
STAGE_META = {
    "ordered": {"label": "下单寄出", "segment": "warehouse"},
    "packed": {"label": "打包完成", "segment": "warehouse"},
    "handed_over": {"label": "出库交运", "segment": "warehouse"},
    "accepted": {"label": "引受", "segment": "domestic"},
    "left_collection": {"label": "离开揽收网点", "segment": "domestic"},
    "origin_exchange": {"label": "到达始发互换局", "segment": "domestic"},
    "origin_customs": {"label": "始发国海关", "segment": "domestic"},
    "departed_origin": {"label": "离开始发国", "segment": "domestic"},
    "handed_to_airline": {"label": "交航", "segment": "domestic"},
    "flight_departed": {"label": "航班起飞", "segment": "domestic"},
    "transit_arrived": {"label": "到达中转地", "segment": "transit"},
    "transit_departed": {"label": "离开中转地", "segment": "transit"},
    "landed_destination": {"label": "航班到达日本", "segment": "japan"},
    "arrived_japan": {"label": "到达日本", "segment": "japan"},
    "japan_customs": {"label": "日本通关", "segment": "japan"},
    "departed_exchange": {"label": "离开日本互换局", "segment": "japan"},
    "local_arrived": {"label": "到达派送局", "segment": "japan"},
    "out_for_delivery": {"label": "派送中", "segment": "japan"},
    "delivered": {"label": "签收", "segment": "japan"},
}
STAGES = tuple(STAGE_META)
STAGE_ORDER = {stage: index for index, stage in enumerate(STAGES)}
PRE_STAGES = ("ordered", "packed", "handed_over")
# 「第 N 天」的起点候选，按优先级：有下单用下单，没有用出库，再没有用引受
ORIGIN_STAGES = ("ordered", "handed_over", "accepted")
# 中转段两个阶段的显示名模板（{region} = TRANSIT_REGION_LABEL），没设中转地名时用 STAGE_META 的通用名
TRANSIT_STAGE_TEMPLATES = {"transit_arrived": "到达{region}", "transit_departed": "离开{region}"}


def _check_stage_segments():
    """段归属穷举：每个阶段都有已定义的段，且段沿阶段顺序只前进不回头（分组显示依赖这一点）；
    发货前节点恰好是仓库段；中转段的名称模板只覆盖中转段阶段。"""
    last = -1
    for stage, meta in STAGE_META.items():
        index = SEGMENTS.index(meta["segment"])
        if index < last:
            raise ValueError(f"阶段 {stage} 的段顺序倒退")
        last = index
    if {s for s, m in STAGE_META.items() if m["segment"] == "warehouse"} != set(PRE_STAGES):
        raise ValueError("仓库段必须恰好是发货前节点")
    if {s for s, m in STAGE_META.items() if m["segment"] == "transit"} != set(TRANSIT_STAGE_TEMPLATES):
        raise ValueError("中转段阶段与名称模板不一致")


_check_stage_segments()


def stage_segment(stage: str) -> str:
    return STAGE_META[stage]["segment"]


def stage_label(stage: str, transit_region: str = "") -> str:
    region = str(transit_region or "").strip()
    if region and stage in TRANSIT_STAGE_TEMPLATES:
        return TRANSIT_STAGE_TEMPLATES[stage].format(region=region)
    return STAGE_META[stage]["label"]


def segment_at(stages: dict, moment):
    """某个时刻落在哪一段：时间不晚于它的已到阶段里最晚的那个所在的段（同时刻取顺序靠后的）；
    比所有已到阶段都早时取最早那个阶段的段；没有任何阶段返回 None。用于把履历事件按阶段边界切分到段下。"""
    if not stages or moment is None:
        return None
    before = [stage for stage, at in stages.items() if at <= moment]
    if before:
        return stage_segment(max(before, key=lambda stage: (stages[stage], STAGE_ORDER[stage])))
    return stage_segment(min(stages, key=lambda stage: (stages[stage], STAGE_ORDER[stage])))


def segment_label(segment: str, transit_region: str = "") -> str:
    region = str(transit_region or "").strip()
    if region and segment == "transit":
        return f"{region}段"
    return SEGMENT_META[segment]["label"]


# 可信度穷举：none 没有可参考样本 / borrowed 同类 0 单只借鉴其他单 / rough 同类 1–4 单 / solid 同类 ≥5 单。
# 「同类」看剩余阶段里第一个阶段所在段的比较依据（BASIS_META）：仓库段比渠道，邮政各段比收寄网点。
CONFIDENCE_META = {
    "none": {"label": "还没有可参考的历史"},
    "borrowed": {"label": "还没有同类记录，借鉴其他单"},
    "rough": {"label": "同类样本较少，仅供粗估"},
    "solid": {"label": "同类样本充足"},
}
SOLID_MIN_SAMPLES = 5

# 样本「同类」的比较依据穷举：channel 同渠道 / office 同收寄网点
BASIS_META = {
    "channel": {"label": "同渠道"},
    "office": {"label": "同收寄网点"},
}

# 样本权重按剩余阶段所在的段区分：同类 1.0，其他 0.3。
# - 仓库段（下单→出库）比渠道：这段耗时取决于渠道（发货方）自己的打包与交运节奏；
# - 国内段、中转段、日本段比收寄网点：同一个揽收网点之后走的是同一条邮政线路（同一互换局、同一航线），
#   耗时取决于邮政而与发货渠道无关，渠道不同但网点相同的样本同样有代表性。
#   当前单或样本缺收寄网点（没有始发国段数据）时，这一对样本退回按渠道比较。
SAME_WEIGHT = 1.0
OTHER_WEIGHT = 0.3
# 非常规件（用户在任务上标的，如大件电器走的特殊流程）作为样本时，在上面的权重上再乘这个系数。
# 同单号多个任务合并成一个样本时任一任务标了就算；当前单自己是否非常规不影响别的样本的权重。
ATYPICAL_WEIGHT = 0.3
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


def _cn_ems_stage(status: str, detail: str):
    """中国邮政（17TRACK 转来的 EMS 轨迹）的事件描述 → 阶段。只映射始发国段：
    与日本邮政重复含义的（收取 / 出口海关 / 互换局出口 / 到达寄达地处理中心）映射到同一阶段，同阶段两个来源取最早；
    「到达寄达地处理中心」之后的日本段中文事件（境外进口海关、【日本】投递…）不映射，日本段以 jp_post 为准。"""
    if status.startswith("中国邮政已收取"):
        return "accepted"
    if status.startswith("到达寄达地处理中心"):
        return "arrived_japan"
    if status == "到达寄达地":
        return "landed_destination"
    if "国际互换局" in status and ("已出口直封" in status or status.startswith("邮件离开")):
        return "departed_origin"
    if status.startswith("送交出口海关") or status.startswith("出口海关"):
        return "origin_customs"
    if status.startswith("已交承运商运输") or status.startswith("航空公司接收"):
        return "handed_to_airline"
    if status.startswith("航空公司启运"):
        return "flight_departed"
    if "互换局" not in status and (
        (status.startswith("邮件离开【") and "揽投部" in status) or "完成分拣，准备发出" in status
    ):
        return "left_collection"
    return None


# 承运方来源穷举（carrier_records.source 的 CHECK 约束由它派生）：每个来源一支状态→阶段映射，签名相同。
# jp_post：日本邮政官网履历（tracker 每轮抓取）；cn_ems：中国邮政 EMS 的始发国段（17TRACK，只对 CN 单号）。
# 新增来源后已有库的 CHECK 约束由 storage._migrate_carrier_sources 自动放宽（只重建 carrier_records）。
CARRIER_SOURCES = {
    "jp_post": _jp_post_stage,
    "cn_ems": _cn_ems_stage,
}


def record_stage(source: str, status, detail=""):
    """一条承运方记录对应的阶段 key；认不出返回 None。来源不在 CARRIER_SOURCES 里抛 ValueError。"""
    if source not in CARRIER_SOURCES:
        raise ValueError(f"未定义的承运方来源：{source}")
    return CARRIER_SOURCES[source](str(status or "").strip(), str(detail or ""))


# --- 时区 ---

# 单号后两位国家码 → 当地 UTC 偏移（小时）。日本邮政页面上始发国那几行（引受、始发国互换局与海关）的时刻
# 其实是始发国当地时间，读取时按这里换算成日本时间（库里保留原值）。
# 本期只有 CN；没有映射的国家码按日本时间处理——国家码是外部数据，无法穷举，换算不了就保持原样。
ORIGIN_UTC_OFFSETS = {"CN": 8}
JST_OFFSET_HOURS = 9
# 日本邮政履历里属于始发国的阶段（由 _jp_post_stage 按所在地判定）
ORIGIN_SIDE_STAGES = frozenset({"accepted", "origin_exchange", "origin_customs", "departed_origin"})


def origin_country(tracking_number) -> str:
    """单号后两位字母即寄出国家码（EB000000001CN → CN）；不是两个字母返回空串。"""
    tail = str(tracking_number or "").strip().upper()[-2:]
    return tail if len(tail) == 2 and tail.isascii() and tail.isalpha() else ""


def record_time(record, tracking_number=""):
    """一条承运方记录的日本时间（datetime）。不合法返回 None。
    - jp_post：映射到始发国阶段、且有时刻（不只是日期）的记录，按单号国家码的时区换算成日本时间；
    - cn_ems：入库时已换算成日本时间（见 track17_event_time），原样读取。"""
    moment = parse_stored_time(record["record_at"])
    if moment is None:
        return None
    if record["source"] != "jp_post" or len(str(record["record_at"]).strip()) <= len("YYYY-MM-DD"):
        return moment
    offset = ORIGIN_UTC_OFFSETS.get(origin_country(tracking_number or record.get("tracking_number")))
    if offset is None or record_stage("jp_post", record["status"], record.get("detail", "")) not in ORIGIN_SIDE_STAGES:
        return moment
    return moment + timedelta(hours=JST_OFFSET_HOURS - offset)


# 中国邮政轨迹里「到达寄达地」及之后的日本段事件：时刻其实是日本当地时间，却被标成 +08:00。
# 按描述里的这些字样识别，取字面时刻当日本时间；其余（境内、航班事件）按标注的时区换算。
_CN_DESTINATION_MARKERS = ("寄达地", "境外", "【日本】")


def track17_event_time(time_iso, description):
    """17TRACK 事件的 time_iso → 日本时间 'YYYY-MM-DD HH:MM'；解析不了返回 None。
    没有时区的按中国时间（UTC+8）处理。"""
    text = str(time_iso or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    if any(marker in str(description or "") for marker in _CN_DESTINATION_MARKERS):
        return moment.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone(timedelta(hours=ORIGIN_UTC_OFFSETS["CN"])))
    return moment.astimezone(JST).strftime("%Y-%m-%d %H:%M")


# --- 中转段（要看事件序列，不能逐条映射） ---

_FLIGHT_DEPART = "航空公司启运"
_FLIGHT_ARRIVE = "飞机进港"


def transit_stages(records) -> dict:
    """经第三地中转的两段航班：cn_ems 里第二条「航空公司启运」（第一条启运之后的下一条）= transit_departed，
    第一条启运之后的第一条「飞机进港」= transit_arrived（不晚于第二条启运）。只有一条启运（一段航班）时两者都没有。
    同单号挂在多个任务下会有重复事件，按时刻去重。返回 {stage: datetime}。"""
    departs, arrives = set(), set()
    for record in records or []:
        if record["source"] != "cn_ems":
            continue
        status = str(record["status"] or "").strip()
        moment = parse_stored_time(record["record_at"])
        if moment is None:
            continue
        if status.startswith(_FLIGHT_DEPART):
            departs.add(moment)
        elif status.startswith(_FLIGHT_ARRIVE):
            arrives.add(moment)
    departs = sorted(departs)
    if len(departs) < 2:
        return {}
    first, second = departs[0], departs[1]
    result = {"transit_departed": second}
    landed = sorted(moment for moment in arrives if first <= moment <= second)
    if landed:
        result["transit_arrived"] = landed[0]
    return result


# --- 收寄网点 ---

# 「邮件离开【X】，正在发往…」/「邮件已在【X】完成分拣，准备发出」里的 X；互换局不算收寄网点
_COLLECTION_OFFICE_RES = (
    re.compile(r"^邮件离开【([^】]+)】，正在发往"),
    re.compile(r"^邮件已在【([^】]+)】完成分拣"),
)


def collection_office(records):
    """收寄网点：cn_ems 记录里最早一条能提取出网点名（且不是互换局）的那个；没有返回 None。"""
    candidates = []
    for record in records or []:
        if record["source"] != "cn_ems":
            continue
        status = str(record["status"] or "").strip()
        for pattern in _COLLECTION_OFFICE_RES:
            match = pattern.match(status)
            if match and "互换局" not in match.group(1):
                moment = parse_stored_time(record["record_at"])
                if moment is not None:
                    candidates.append((moment, match.group(1).strip()))
                break
    return min(candidates)[1] if candidates else None


def suggest_channel(office, office_channels) -> dict:
    """渠道推断。office_channels：{收寄网点: {渠道…}}（其他已标渠道的单号）。
    返回 {"collection_office", "suggested_channel", "office_multi_channel"}：
    网点只对应过一个渠道时建议它；对应多个时不建议、标 office_multi_channel。"""
    channels = sorted(office_channels.get(office, ())) if office else []
    return {
        "collection_office": office,
        "suggested_channel": channels[0] if len(channels) == 1 else None,
        "office_multi_channel": len(channels) > 1,
    }


def stages_from_records(records, milestones=None, tracking_number="") -> dict:
    """一单的阶段时间：每个阶段取最早出现的时间（日本时间，见 record_time 的始发国时区换算）。
    records：[{"source", "record_at", "status", "detail"}]；milestones：{stage: 时间字符串}（发货前节点）；
    tracking_number：决定始发国时区（记录自带 tracking_number 时也可不传）。返回 {stage: datetime}。"""
    stages: dict = {}

    def put(stage, moment):
        if stage is None or moment is None:
            return
        if stage not in stages or moment < stages[stage]:
            stages[stage] = moment

    for record in records or []:
        put(record_stage(record["source"], record["status"], record.get("detail", "")),
            record_time(record, tracking_number))
    for stage, moment in transit_stages(records).items():
        put(stage, moment)
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


def _confidence(similar: int, total: int) -> str:
    if total == 0:
        return "none"
    if similar == 0:
        return "borrowed"
    if similar < SOLID_MIN_SAMPLES:
        return "rough"
    return "solid"


def segment_basis(segment: str, current_office) -> str:
    """一个段的「同类」比较依据：仓库段比渠道；邮政各段比收寄网点，当前单没有收寄网点时退回渠道。"""
    if segment == "warehouse" or not current_office:
        return "channel"
    return "office"


def _similar(sample: dict, basis: str) -> bool:
    """样本在某个比较依据下是否「同类」。比网点但样本缺网点时，这一单退回按渠道比。"""
    if basis == "office" and sample["office"]:
        return sample["same_office"]
    return sample["same_channel"]


def _leave_seconds(sample_stages: dict, stage: str, t_stage):
    """样本离开 stage 的耗时：顺序在它之后、时间不早于它的阶段里最早到达的那个减去 t_stage。没有返回 None。"""
    later = [
        (moment - t_stage).total_seconds()
        for other, moment in sample_stages.items()
        if STAGE_ORDER[other] > STAGE_ORDER[stage] and moment >= t_stage
    ]
    return min(later) if later else None


def build_forecast(current_stages: dict, current_channel: str, samples, now: datetime, *,
                   current_office=None, transit_region: str = "") -> dict:
    """预测剩余阶段。

    current_stages：当前单 {stage: datetime}；samples：[{"stages": {...}, "channel": str, "office": str|None, "atypical": bool}]
    （已按单号去重、排除当前单号）；current_office：当前单的收寄网点；now：当前时间（参数传入，便于测试）；
    transit_region：中转地显示名（系统设置 TRANSIT_REGION_LABEL），只原样带进输出给前端拼段名与阶段名。
    只输出聚合统计，不带样本的单号与账号。

    - 起点：ordered → handed_over → accepted 中最先存在的那个；「第 N 天」按自然日算：起点当天是第 1 天，
      跨过一个午夜加 1（看包裹的人按日历数天，不按 24 小时取整）。
    - 当前阶段 S = 时间最晚的已到阶段，已等 w = now - t_S；已签收不预测，只给全程用时。
    - 可用样本：有 S 且 S 之后（顺序靠后、时间不早于 t_S）至少到过一个阶段的单。
      合格样本：离开 S 的耗时 > w（已经等了 w 还没走，就只跟同样「走得慢」的历史比）；
      没有合格样本时退回全部可用样本并标 overdue（已比历史上最慢的还久）。
    - 每个剩余阶段 X：有 X 的合格样本 d = t_X - t_S，剩余 = max(d - w, 0)。权重按 X 所在的段：
      仓库段同渠道 1.0、其他 0.3；国内 / 中转 / 日本段同收寄网点 1.0、其他 0.3（缺网点退回按渠道，见 segment_basis）；
      非常规件样本再乘 ATYPICAL_WEIGHT。
      输出加权中位数、区间（≥3 单用 20%–80% 分位，否则 min–max）、出现率（<0.5 标「可能出现」，0 不列）。
    - 可信度：按剩余阶段里第一个阶段所在段的比较依据数「同类」合格样本（没有剩余阶段时看当前阶段之后的那个阶段）。"""
    channel = str(current_channel or "").strip()
    office = str(current_office or "").strip() or None
    arrived = sorted(current_stages.items(), key=lambda item: (item[1], STAGE_ORDER[item[0]]))
    origin = _origin(current_stages)
    result = {
        "channel": channel,
        "collection_office": office,
        "transit_region": str(transit_region or "").strip(),
        "generated_at": format_time(now),
        "origin": {"stage": origin, "at": format_time(current_stages[origin])} if origin else None,
        "day": None,
        "delivered": "delivered" in current_stages,
        "total_seconds": None,
        "current": None,
        "arrived": [{"stage": stage, "at": format_time(moment)} for stage, moment in arrived],
        "remaining": [],
        "confidence": "none",
        "basis": segment_basis("japan", office),
        "samples": {"total": 0, "same_channel": 0, "same_office": 0, "similar": 0, "atypical": 0, "similar_atypical": 0},
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
        sample_office = str(sample.get("office") or "").strip() or None
        usable.append({
            "stages": stages,
            "leave": leave,
            "same_channel": bool(channel) and str(sample.get("channel") or "").strip() == channel,
            "office": sample_office,
            "same_office": bool(office) and sample_office == office,
            "atypical": bool(sample.get("atypical")),
        })

    qualified = [sample for sample in usable if sample["leave"] > waited]
    if usable and not qualified:
        qualified = usable
        result["overdue"] = True

    for sample in qualified:
        sample["similar"] = {basis: _similar(sample, basis) for basis in BASIS_META}

    def weight(sample, basis):
        base = SAME_WEIGHT if sample["similar"][basis] else OTHER_WEIGHT
        return base * ATYPICAL_WEIGHT if sample["atypical"] else base

    for stage in STAGES:
        if STAGE_ORDER[stage] <= STAGE_ORDER[current] or stage in current_stages:
            continue
        basis = segment_basis(stage_segment(stage), office)
        values, weights, similar_x, atypical_x = [], [], 0, 0
        for sample in qualified:
            stages = sample["stages"]
            t_s = stages[current]
            if stage not in stages or stages[stage] < t_s:
                continue
            values.append(max(0.0, (stages[stage] - t_s).total_seconds() - waited))
            weights.append(weight(sample, basis))
            similar_x += 1 if sample["similar"][basis] else 0
            atypical_x += 1 if sample["atypical"] else 0
        if not values:
            continue
        median = weighted_quantile(values, weights, 0.5)
        if len(values) >= QUANTILE_MIN_SAMPLES:
            low = weighted_quantile(values, weights, LOW_QUANTILE)
            high = weighted_quantile(values, weights, HIGH_QUANTILE)
        else:
            low, high = min(values), max(values)
        occurrence = sum(weights) / sum(weight(sample, basis) for sample in qualified)
        result["remaining"].append({
            "stage": stage,
            "segment": stage_segment(stage),
            "basis": basis,
            "median_seconds": int(median),
            "low_seconds": int(low),
            "high_seconds": int(high),
            "eta": format_time(now + timedelta(seconds=median)),
            "eta_low": format_time(now + timedelta(seconds=low)),
            "eta_high": format_time(now + timedelta(seconds=high)),
            "occurrence": round(occurrence, 3),
            "maybe": occurrence < MAYBE_BELOW,
            "samples": {"total": len(values), "similar": similar_x, "atypical": atypical_x},
        })

    if result["remaining"]:
        lead_stage = result["remaining"][0]["stage"]
    else:
        later = [stage for stage in STAGES if STAGE_ORDER[stage] > STAGE_ORDER[current]]
        lead_stage = later[0] if later else current
    basis = segment_basis(stage_segment(lead_stage), office)
    similar = sum(1 for sample in qualified if sample["similar"][basis])
    result["basis"] = basis
    result["samples"] = {
        "total": len(qualified),
        "same_channel": sum(1 for sample in qualified if sample["same_channel"]),
        "same_office": sum(1 for sample in qualified if sample["same_office"]),
        "similar": similar,
        "atypical": sum(1 for sample in qualified if sample["atypical"]),
        "similar_atypical": sum(1 for sample in qualified if sample["similar"][basis] and sample["atypical"]),
    }
    result["confidence"] = _confidence(similar, len(qualified))
    return result

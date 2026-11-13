"""合并履历：把日本邮政（jp_post）、中国邮政（cn_ems）与发货前节点合成一条按段分组的「节点」列表，
并给每个节点起带方向与地点的中文名。推送（tracker）与网页显示共用这一份。

纯函数：不碰数据库与网络，只 import forecast、jp_status 与 status_rules。

## 没收录的状态
查找顺序：代码内置表 → 已生效的状态规则（status_rules，后台归类或管理员补的：中文名、合并类型、阶段、推不推送）→ 未识别。
未识别的日本邮政状态按原文推送（推送角色 unrecognized），中国邮政的不推送；判断「是否内置认得」见 recognized。

## 节点与归并
- 两个来源里含义相同的事件（KIND 对照：jp_post 状态 + 所在地 ↔ cn_ems 描述），换算到日本时间后相差 ≤ MATCH_SECONDS，
  归并为同一个「共有节点」；其余事件各自成节点（独有节点）。
- 共有节点的 key = `<kind>@<成员里最早的时间>`；判断「推送过没有」时按 kind 相同、任一成员时间与已记录 key 的时间相差
  ≤ MATCH_SECONDS 认定为同一节点（node_notified），所以不论哪边先报，另一边晚到（时间差 1 分钟）都不会被当成新节点。
  独有节点的 key = `<source>:<原始状态>@<record_at>`，发货前节点 `milestone:<stage>`（不推送）。
- 显示：每段有「主来源」——仓库段用发货前节点，国内段 / 中转段用 cn_ems，日本段用 jp_post；
  没有 cn_ems 数据时国内段 / 中转段也用 jp_post（与只有日本邮政时一致）。
  主来源的节点都显示；另一来源的共有节点即使主来源还没报也先显示（标该来源），主来源到了就合并显示两边一致；
  另一来源的独有事件不显示（主来源更密集）；例外是第二段航班落地与「日本邮政接收（寄达地）」（日本段的起点，只有中国邮政报）。
- 显示给人的名字一律经过命名（kind_name / jp_name / cn_display），不直接用两边原文；没收录的外部状态显示原文并记一次日志。

## 命名
始发国名按单号国家码（forecast.origin_country_name），中转地名取 TRANSIT_REGION_LABEL；
互换局英文名查 EXCHANGE_ZH（未收录原样显示英文），cn_ems 的【…】里的名字直接用，日本地名保留原文。
"""
import re
from datetime import timedelta

import forecast
import jp_status
import status_rules

# 两个来源同一事件的最大时间差（秒，换算到日本时间后比较）
MATCH_SECONDS = 120

# 节点来源穷举（前端 forecast.js 有一份一致的映射）
NODE_SOURCE_META = {
    "milestone": {"label": "手填"},
    "cn_ems": {"label": "中邮"},
    "jp_post": {"label": "日邮"},
}

# 每段的主来源（有 cn_ems 数据时）；没有 cn_ems 数据时国内段 / 中转段改用 jp_post
SEGMENT_PRIMARY = {
    "warehouse": "milestone",
    "domestic": "cn_ems",
    "transit": "cn_ems",
    "japan": "jp_post",
}

# 互换局英文名 → 中文名（日本邮政 detail 里的「WEIHAI D EMS」取第一个词查表）；未收录原样显示英文
EXCHANGE_ZH = {
    "WEIHAI": "威海国际互换局",
}

# detail 里表示「在始发国」的英文国名（换成始发国中文名）
_COUNTRY_WORDS = {"CHINA", "CN", "PEOPLE'S REPUBLIC OF CHINA"}

DELIVERED = forecast.DELIVERED_STATUSES


# --- 共有节点的类型（kind）：两个来源各自怎么认 ---

def jp_kind(status: str, stage):
    """日本邮政状态 → kind（含义能与中国邮政对上的那些）；stage 用来区分始发国与日本。"""
    origin = stage in forecast.ORIGIN_SIDE_STAGES
    if status == "引受":
        return "accept"
    if status == "税関検査のため税関へ提示":
        return "export_customs_submit" if origin else "import_customs_submit"
    if status == "税関保留":
        return "export_customs_hold" if origin else "import_customs_hold"
    if status == "税関から受領":
        return "export_customs_release" if origin else "import_customs_release"
    if status == "国際交換局に到着":
        return "arrive_origin_exchange" if origin else "arrive_japan_exchange"
    if status == "国際交換局から発送":
        return "depart_origin" if origin else "depart_japan_exchange"
    if status == "通関手続中":
        return "import_customs"
    if status == "到着":
        return "local_arrive"
    if status == "持ち出し中":
        return "out_for_delivery"
    if status in DELIVERED:
        return "delivered"
    return _rule_kind("jp_post", status) if not jp_status.is_known(status) else None


def _rule_kind(source: str, status: str):
    rule = status_rules.lookup(source, status)
    return rule["kind"] if rule else None


def cn_kind(status: str):
    """中国邮政事件描述 → kind；对不上日本邮政的返回 None（内置认不出的再查状态规则）。"""
    return _cn_builtin_kind(status) or (_rule_kind("cn_ems", status) if not cn_recognized(status) else None)


def _cn_builtin_kind(status: str):
    if status.startswith("中国邮政已收取"):
        return "accept"
    if status.startswith("送交出口海关"):
        return "export_customs_submit"
    if status.startswith("出口海关") and "留存待验" in status:
        return "export_customs_hold"
    if status.startswith("出口海关") and "放行" in status:
        return "export_customs_release"
    if "国际互换局" in status and "已出口直封" in status:
        return "depart_origin"
    if status.startswith("到达寄达地处理中心"):
        return "arrive_japan_exchange"
    # 先按语义细分再兜底：「送交境外进口海关」是提交，「…留存待验」才是通关中
    if status.startswith("送交境外进口海关"):
        return "import_customs_submit"
    if "寄达地海关" in status and ("允许进口" in status or "放行" in status):
        return "import_customs_release"
    if "境外进口海关" in status:
        return "import_customs"
    if "【日本】" in status and status.startswith("离开") and "处理中心" in status:
        return "depart_japan_exchange"
    if "【日本】" in status and "投递局" in status and "到达" in status:
        return "local_arrive"
    if "【日本】" in status and "安排投递" in status:
        return "out_for_delivery"
    if "【日本】" in status and "妥投" in status:
        return "delivered"
    return None


# kind 穷举（前面两个函数只能返回这里的 key）；值是这一类节点归属的阶段（用于判断段）
KIND_STAGE = {
    "accept": "accepted",
    "export_customs_submit": "origin_customs",
    "export_customs_hold": "origin_customs",
    "export_customs_release": "origin_customs",
    "arrive_origin_exchange": "origin_exchange",
    "depart_origin": "departed_origin",
    "arrive_japan_exchange": "arrived_japan",
    "import_customs_submit": "japan_customs",
    "import_customs_hold": "japan_customs",
    "import_customs_release": "japan_customs",
    "import_customs": "japan_customs",
    "depart_japan_exchange": "departed_exchange",
    "local_arrive": "local_arrived",
    "out_for_delivery": "out_for_delivery",
    "delivered": "delivered",
}

# 两边都可能报出的 kind（只有这些参与跨来源归并、算作「共有节点」，任一边报了就显示）；
# 只有日本邮政报的（到达始发国互换局、日本海关扣留）按独有事件处理。
# 日本海关放行（寄达地海关允许进口）多半只有中国邮政报，但信息重要，作为共有类型照样显示。
# 同一来源内也要合并的类型（只在中国邮政内部）：揽收网点的「完成分拣」与「离开」通常同一分钟
SAME_SOURCE_KINDS = frozenset({"left_collection"})

SHARED_KINDS = frozenset({
    "accept", "export_customs_submit", "export_customs_hold", "export_customs_release", "depart_origin",
    "arrive_japan_exchange", "import_customs_submit", "import_customs", "import_customs_release",
    "depart_japan_exchange", "local_arrive", "out_for_delivery", "delivered",
})


# --- 推送白名单 ---

# 只推关键节点（穷举）：其余节点只记录、显示，同样写进 notified_nodes（标记为已处理），以后不会误推。
# 角色由节点成员的语义决定（日本邮政看 jp_kind / 异常状态，中国邮政看 cn_kind / 到达寄达地）。
PUSH_ROLES = {
    "accepted": "始发国收寄",
    "export_hold": "始发国出口海关扣留待验（异常）",
    "departed_origin": "离开始发国（出境）",
    "arrived_japan": "航班到达日本（日本邮政接收寄达地 / 到达日本互换局，先到的那个）",
    "import_hold": "日本进口海关扣留（异常）",
    "departed_exchange": "离开日本互换局",
    "local_arrived": "到达派送局",
    "out_for_delivery": "派送中",
    "delivered": "已妥投 / 窗口领取",
    "anomaly": "日本邮政异常（不在带回、局内保管、退回、调查中、转寄、再次投递）",
    "learned": "状态规则里标了推送、又不属于上面各类的新状态",
    "unrecognized": "还没归类的日本邮政新状态（按原文推送，标题「原文（未识别）」）",
}
# 一单只推一次的角色：「日本邮政接收（寄达地）」与「到达東京…（入境）」是同一件事，先到的推，后到的只记录
ONCE_ROLES = frozenset({"arrived_japan"})
# 日本邮政的异常类状态（外部数据，按 jp_status 的表收）
JP_ANOMALY_STATUSES = frozenset({
    "ご不在のため持ち戻り", "お届け先にお伺いしましたがご不在でした", "保管", "保管期間経過のため返送",
    "返送", "差出人に返送", "差出人に返送済み", "調査中", "転送", "再配達",
})
_KIND_ROLE = {
    "accept": "accepted",
    "export_customs_hold": "export_hold",
    "depart_origin": "departed_origin",
    "arrive_japan_exchange": "arrived_japan",
    "import_customs_hold": "import_hold",
    "depart_japan_exchange": "departed_exchange",
    "local_arrive": "local_arrived",
    "out_for_delivery": "out_for_delivery",
    "delivered": "delivered",
}


if not set(_KIND_ROLE.values()) | {"anomaly", "arrived_japan", "learned", "unrecognized"} <= set(PUSH_ROLES):
    raise ValueError("推送角色未在 PUSH_ROLES 里定义")


def push_role(source: str, status: str, raw_kind):
    """一条事件的推送角色（PUSH_ROLES 的 key）；不推的返回 None。
    内置认不出的状态：有生效规则按规则的 push（推的话按规则 kind 归到对应角色，没有对应角色算 learned）；
    没有规则时日本邮政的按原文推（unrecognized），中国邮政的不推。"""
    if not builtin_known(source, status):
        rule = status_rules.lookup(source, status)
        if rule:
            return (_KIND_ROLE.get(rule["kind"]) or "learned") if rule["push"] else None
        return "unrecognized" if source == "jp_post" else None
    if raw_kind in _KIND_ROLE:
        return _KIND_ROLE[raw_kind]
    if source == "jp_post" and status in JP_ANOMALY_STATUSES:
        return "anomaly"
    if source == "cn_ems" and status == "到达寄达地":
        return "arrived_japan"
    return None


# --- 地点 ---

def _detail_parts(detail) -> list:
    """jp_post detail 按「·」拆开，去掉「配達予定日：…」这类非地点的段。"""
    parts = [part.strip() for part in str(detail or "").split("·") if part.strip()]
    return [part for part in parts if not part.startswith(("配達予定日", "预计投递日"))]


def exchange_name_from_detail(detail):
    """「WEIHAI D EMS · CHINA」→ 威海国际互换局；不是英文互换局名返回 None。"""
    parts = _detail_parts(detail)
    if not parts:
        return None
    first = parts[0]
    if first.upper() in _COUNTRY_WORDS or not re.match(r"^[A-Za-z]", first):
        return None
    word = first.split()[0].upper()
    return EXCHANGE_ZH.get(word, first)


def _city_of(name) -> str:
    """「山东省威海市国际互换局」/「威海国际互换局」/「威海市」→ 威海。"""
    text = str(name or "").strip()
    text = re.sub(r"^.*?省", "", text)
    match = re.match(r"^(.+?)(?:市|国际互换局|$)", text)
    return match.group(1) if match else text


def _bracket(status) -> str | None:
    match = re.search(r"【([^】]+)】", str(status or ""))
    return match.group(1) if match else None


class Context:
    """一单的命名上下文：始发国名、中转地名、始发国互换局与城市、日本互换局、派送局。"""

    def __init__(self, records, tracking_number, transit_region):
        self.origin = forecast.origin_country_name(tracking_number) or forecast.GENERIC_ORIGIN
        self.transit = str(transit_region or "").strip()
        self.origin_exchange = None
        self.japan_exchange = None  # 没有日本邮政的日本段数据时为 None（名字里不写互换局名）
        self.local_office = None
        cn_exchange = None
        for record in records:
            if record["source"] == "jp_post":
                stage = forecast.record_stage("jp_post", record["status"], record.get("detail", ""))
                parts = _detail_parts(record.get("detail"))
                if stage in forecast.ORIGIN_SIDE_STAGES:
                    name = exchange_name_from_detail(record.get("detail"))
                    if name and not self.origin_exchange:
                        self.origin_exchange = name
                elif record["status"] in ("国際交換局に到着", "国際交換局から発送", "通関手続中") and parts:
                    self.japan_exchange = self.japan_exchange or parts[0]
                elif record["status"] in ("到着", "持ち出し中") or record["status"] in DELIVERED:
                    if parts:
                        self.local_office = parts[0]
            elif record["source"] == "cn_ems":
                name = _bracket(record["status"])
                if name and "国际互换局" in name and not cn_exchange:
                    cn_exchange = name
        if not self.origin_exchange and cn_exchange:
            self.origin_exchange = f"{_city_of(cn_exchange)}国际互换局"
        self.origin_exchange = self.origin_exchange or f"{self.origin}国际互换局"
        self.city = _city_of(self.origin_exchange) if (self.origin_exchange != f"{self.origin}国际互换局") else ""

    def city_paren(self) -> str:
        return f"（{self.city}）" if self.city else ""

    def transit_name(self) -> str:
        return self.transit or forecast.GENERIC_TRANSIT


def _jp_place(detail, ctx: Context) -> str:
    """日本邮政 detail 的地点：第一段；是英文国名（CHINA）时换成始发国中文名。"""
    parts = _detail_parts(detail)
    if not parts:
        return ""
    return ctx.origin if parts[0].upper() in _COUNTRY_WORDS else parts[0]


def kind_name(kind: str, ctx: Context, jp_detail=None) -> str:
    """共有节点的显示名（不论哪边报的都一样），地点优先取日本邮政成员的 detail。"""
    place = _detail_parts(jp_detail)[0] if _detail_parts(jp_detail) else None
    exchange = place or ctx.japan_exchange  # 没有日本邮政数据时不知道是哪个互换局
    paren = f"（{exchange}）" if exchange else ""
    local = place or ctx.local_office
    names = {
        "accept": f"{ctx.origin}收寄",
        "export_customs_submit": f"提交{ctx.origin}出口海关查验{ctx.city_paren()}",
        "export_customs_hold": f"{ctx.origin}出口海关扣留待验{ctx.city_paren()}",
        "export_customs_release": f"{ctx.origin}出口海关放行{ctx.city_paren()}",
        "arrive_origin_exchange": f"到达{ctx.origin_exchange}",
        "depart_origin": f"离开{ctx.origin_exchange}（出境）",
        "arrive_japan_exchange": f"到达{exchange or '日本互换局'}（入境）",
        "import_customs_submit": f"提交日本进口海关{paren}",
        "import_customs_hold": f"日本进口海关扣留待验{paren}",
        "import_customs_release": f"日本进口海关放行{paren}",
        "import_customs": f"日本进口通关中{paren}",
        "depart_japan_exchange": f"离开{exchange or '日本互换局'}",
        "local_arrive": f"到达{local or '派送局'}",
        "out_for_delivery": f"{local} 派送中" if local else "派送中",
        "delivered": f"已妥投（{local}）" if local else "已妥投",
    }
    return names[kind]


def jp_name(status, detail, ctx: Context) -> str:
    """日本邮政一条记录的显示名：能归类的用 kind_name，其余「中文（地点）」，未收录的状态显示原文。"""
    status = str(status or "").strip()
    stage = forecast.record_stage("jp_post", status, detail)
    kind = jp_kind(status, stage)
    if kind:
        return kind_name(kind, ctx, detail if stage not in forecast.ORIGIN_SIDE_STAGES else None)
    zh = jp_status.status_zh(status)
    place = _jp_place(detail, ctx)
    return f"{zh}（{place}）" if place else zh


_logged_unknown_cn: set = set()


def _unknown_cn(status: str) -> str:
    """没收录的中国邮政事件：显示原文（外部数据无法穷举），同一进程同一描述只记一次日志，便于以后补表。"""
    if status and status not in _logged_unknown_cn:
        _logged_unknown_cn.add(status)
        print(f"[汉化] 未收录的中国邮政事件：{status}", flush=True)
    return status


def _exchange_display(name: str) -> str:
    """「威海国际」→ 威海国际互换局；「山东省威海市国际互换局」→ 威海国际互换局；其他原样。"""
    text = str(name or "").strip()
    if "国际互换局" in text:
        return f"{_city_of(text)}国际互换局"
    if text.endswith("国际"):
        return f"{text}互换局"
    return text


def cn_display(record, ctx: Context, flights: dict):
    """中国邮政一条事件的 (显示名, 段覆盖, 是否总显示)。共有类型（cn_kind）的名字由 kind_name 统一给，这里不管。
    - 揽收网点的分拣与离开（同一分钟两条）在 build_nodes 里合成「离开揽收网点 X → Y」；
    - 「邮件离开【…国际互换局】」是出口直封之后的物理离开：「威海国际互换局 发往机场」；
    - 航班按序列命名（_flight_names）：第二段航班的「飞机进港」是「韩国→日本 航班落地」，归日本段且总显示；
    - 「到达寄达地」是「日本邮政接收（寄达地）」，日本段起点，总显示。
    其余没收录的显示原文并记日志。"""
    status = str(record["status"] or "").strip()
    at = record["record_at"]
    for prefix, named in flights.items():
        if status.startswith(prefix) and at in named:
            name, segment = named[at]
            return name, segment, segment == "japan"
    city = _city_of(record.get("detail")) if record.get("detail") else ctx.city
    if status.startswith("已交承运商运输"):
        return (f"交航（{city}）" if city else "交航"), None, False
    if status.startswith("航空公司接收"):
        return (f"航空公司接收（{city}）" if city else "航空公司接收"), None, False
    if status.startswith("航空公司启运"):
        return "航班起飞", None, False
    if status.startswith("飞机进港"):
        return "航班落地", None, False
    if status == "到达寄达地":
        return "日本邮政接收（寄达地）", "japan", True
    if status.startswith("邮件离开【") and "国际互换局" in status:
        return f"{_exchange_display(_bracket(status))} 发往机场", None, False
    if forecast.record_stage("cn_ems", status) == "left_collection":
        return f"离开揽收网点 {_bracket(status)}", None, False
    if "境外自提点" in status:
        return "到达自提点", None, False
    match = re.match(r"^邮件到达【([^】]+)】", status)
    if match:
        return f"到达{match.group(1)}", None, False
    if _cn_builtin_kind(status):  # 共有类型的名字由 kind_name 给，这里用不上
        return status, None, False
    rule = status_rules.lookup("cn_ems", status)
    if rule:
        return rule["name_zh"], None, False
    return _unknown_cn(status), None, False


# cn_display 按前缀命名的事件（不论有没有 kind）
_CN_NAMED_PREFIXES = ("航空公司启运", "飞机进港", "已交承运商运输", "航空公司接收", "邮件到达【")


def cn_recognized(status) -> bool:
    """中国邮政事件是否由代码内置规则认得（有 kind、有阶段或有专门的命名）。"""
    status = str(status or "").strip()
    return bool(
        _cn_builtin_kind(status)
        or forecast.builtin_stage("cn_ems", status)
        or status.startswith(_CN_NAMED_PREFIXES)
        or status == "到达寄达地"
        or (status.startswith("邮件离开【") and "国际互换局" in status)
        or "境外自提点" in status
    )


def builtin_known(source: str, status) -> bool:
    """状态是否由代码内置的命名 / 映射认得（不看状态规则）：日本邮政看 jp_status 的汉化表，中国邮政见 cn_recognized。"""
    if source == "jp_post":
        return jp_status.is_known(status)
    return cn_recognized(status)


def recognized(source: str, status) -> bool:
    """状态是否已认得（不需要登记为待归类）：内置认得，或规则已并入代码（status_rules.CODE_RULES）。"""
    return builtin_known(source, status) or status_rules.in_code(source, status)


# --- 状态规则的合法取值（status_rules 表与后台归类共用） ---

# kind：KIND_STAGE 的 key（能与另一来源归并的事件类型）；None 表示独有事件
RULE_KINDS = tuple(KIND_STAGE)
# stage：承运方履历的阶段（不含发货前节点）；None 表示不参与阶段判定
RULE_STAGES = tuple(stage for stage in forecast.STAGES if stage not in forecast.PRE_STAGES)
RULE_SOURCES = ("jp_post", "cn_ems")
RULE_NAME_MAX = 30
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")


def validate_rule(data) -> dict:
    """校验并规整一条状态规则 {name_zh, kind, stage, push}，不合法抛 ValueError（文案可直接给管理员看）。
    name_zh 去掉换行与控制字符、去首尾空白、限长 RULE_NAME_MAX；kind / stage 必须是枚举值或空；push 只认布尔或 0/1。"""
    if not isinstance(data, dict):
        raise ValueError("规则必须是对象。")
    name = _CONTROL_CHARS.sub(" ", str(data.get("name_zh") or "")).strip()
    if not name:
        raise ValueError("中文名不能为空。")
    if len(name) > RULE_NAME_MAX:
        raise ValueError(f"中文名不能超过 {RULE_NAME_MAX} 个字。")
    kind = data.get("kind") or None
    if kind is not None and kind not in RULE_KINDS:
        raise ValueError(f"未知的事件类型：{kind}")
    stage = data.get("stage") or None
    if stage is not None and stage not in RULE_STAGES:
        raise ValueError(f"未知的阶段：{stage}")
    push = data.get("push")
    if isinstance(push, bool):
        push_value = push
    elif push in (0, 1) and not isinstance(push, float):
        push_value = bool(push)
    else:
        raise ValueError("是否推送只能是是或否。")
    return {"name_zh": name, "kind": kind, "stage": stage, "push": push_value}


def _check_code_rules():
    """并入代码的规则同样要合法：来源、枚举、中文名，且与规整后的值一致（导出脚本写进去的就是规整后的）。"""
    for (source, raw), rule in status_rules.CODE_RULES.items():
        if source not in RULE_SOURCES or not str(raw).strip():
            raise ValueError(f"CODE_RULES 的键不合法：{(source, raw)}")
        if validate_rule(rule) != rule:
            raise ValueError(f"CODE_RULES 的规则不合法：{(source, raw)}")


_check_code_rules()


def _left_collection_name(group) -> str:
    """揽收网点的分拣 + 离开合成一个节点：「离开揽收网点 威海市丹东路第二揽投部 → 威海国际互换局」。"""
    office = dest = None
    for member in group:
        name = _bracket(member["status"])
        office = office or name
        match = re.search(r"正在发往【([^】]+)】", member["status"])
        if match:
            dest = _exchange_display(match.group(1))
    return f"离开揽收网点 {office} → {dest}" if dest else f"离开揽收网点 {office}"


def _flight_names(cn_records, ctx: Context) -> dict:
    """航班事件按序列命名：{描述前缀: {record_at: (名字, 段覆盖)}}。
    两段航班：第一条启运「中国→韩国 航班起飞」、其后第一条飞机进港「到达韩国中转」、第二条启运「韩国→日本 航班起飞」、
    之后的飞机进港「韩国→日本 航班落地」（归日本段）。一段航班：启运「中国→日本 航班起飞」，之后的飞机进港「中国→日本 航班落地」。"""
    departs = sorted({r["record_at"] for r in cn_records if str(r["status"]).startswith("航空公司启运")})
    arrives = sorted({r["record_at"] for r in cn_records if str(r["status"]).startswith("飞机进港")})
    result = {"航空公司启运": {}, "飞机进港": {}}
    if not departs:
        return result
    transit = ctx.transit_name()
    if len(departs) >= 2:
        first, second = departs[0], departs[1]
        result["航空公司启运"][first] = (f"{ctx.origin}→{transit} 航班起飞", None)
        result["航空公司启运"][second] = (f"{transit}→日本 航班起飞", None)
        landed = [at for at in arrives if first <= at <= second]
        if landed:
            result["飞机进港"][landed[0]] = (f"到达{transit}中转", None)
        for at in arrives:
            if at > second:
                result["飞机进港"][at] = (f"{transit}→日本 航班落地", "japan")
    else:
        result["航空公司启运"][departs[0]] = (f"{ctx.origin}→日本 航班起飞", None)
        for at in arrives:
            if at > departs[0]:
                result["飞机进港"][at] = (f"{ctx.origin}→日本 航班落地", "japan")
    return result


# --- 节点 ---

def _fmt(moment, date_only=False) -> str:
    return moment.strftime("%Y-%m-%d") if date_only else moment.strftime("%Y-%m-%d %H:%M")


def _dedupe(records) -> list:
    """同单号挂在多个任务下的重复记录：按 (来源, record_at, 状态) 去重，detail 取信息更多（更长）的那条
    （日本邮政同一行有的任务抓到时带「配達予定日」、有的不带）。"""
    best: dict = {}
    for record in records or []:
        key = (record["source"], record["record_at"], record["status"])
        if key not in best or len(str(record.get("detail") or "")) > len(str(best[key].get("detail") or "")):
            best[key] = record
    return list(best.values())


def _close(a, b) -> bool:
    """两个成员时间是否算同一时刻：都有时刻时差 ≤ MATCH_SECONDS；任一只有日期时同一天即可。"""
    if a["date_only"] or b["date_only"]:
        return a["moment"].date() == b["moment"].date()
    return abs((a["moment"] - b["moment"]).total_seconds()) <= MATCH_SECONDS


def build_nodes(records, milestones=None, *, tracking_number="", transit_region="") -> list:
    """合并节点列表（按时间先后）。records：本单号的 carrier_records [{"source", "record_at", "status", "detail"}]
    （多任务重复的会去重）；milestones：{stage: 时间字符串}（发货前节点）。
    每个节点：{"key", "kind", "at", "segment", "stage", "name", "sources", "original", "detail", "visible", "times"}；
    sources 按时间先后（先报的在前），original 是日本邮政原文状态（没有为 None），times 是各成员的日本时间（推送判重用）。"""
    records = _dedupe(records)
    ctx = Context(records, tracking_number, transit_region)
    stages = forecast.stages_from_records(records, milestones, tracking_number=tracking_number)
    has_cn = any(record["source"] == "cn_ems" for record in records)
    flights = _flight_names([r for r in records if r["source"] == "cn_ems"], ctx)

    members = []
    for record in records:
        moment = forecast.record_time(record, tracking_number)
        if moment is None:
            continue
        status = str(record["status"] or "").strip()
        date_only = len(str(record["record_at"]).strip()) <= len("YYYY-MM-DD")
        if record["source"] == "jp_post":
            stage = forecast.record_stage("jp_post", status, record.get("detail", ""))
            kind = jp_kind(status, stage)
            role = push_role("jp_post", status, kind)
            kind = kind if kind in SHARED_KINDS else None
            name = jp_name(status, record.get("detail", ""), ctx)
            detail = jp_status.detail_zh(record.get("detail", ""))
            segment_override, pinned = None, False
        else:
            kind = cn_kind(status)
            role = push_role("cn_ems", status, kind)
            stage = forecast.record_stage("cn_ems", status, record.get("detail", ""))
            name, segment_override, pinned = cn_display(record, ctx, flights)
            if kind is None and stage == "left_collection":
                kind = "left_collection"
            detail = str(record.get("detail") or "")
        members.append({
            "source": record["source"], "moment": moment, "date_only": date_only, "kind": kind,
            "stage": stage, "name": name, "detail": detail, "segment": segment_override, "pinned": pinned,
            "status": status, "record_at": record["record_at"], "role": role,
        })
    members.sort(key=lambda m: (m["moment"], m["source"]))

    # 归并：先按语义（同 kind）再按时间（≤ MATCH_SECONDS），同一语义多个候选取时间最近的。
    # 跨来源的共有节点每组每个来源只一条（同来源的同类事件各自成节点）；
    # 唯一的同来源合并是揽收网点的「完成分拣」与「离开」（SAME_SOURCE_KINDS），同一分钟的两条合成一个节点
    groups = []
    for member in members:
        best, best_gap = None, None
        if member["kind"]:
            for group in groups:
                if group[0]["kind"] != member["kind"]:
                    continue
                if member["kind"] not in SAME_SOURCE_KINDS and any(other["source"] == member["source"] for other in group):
                    continue
                close = [other for other in group if _close(other, member)]
                if not close:
                    continue
                gap = min(abs((other["moment"] - member["moment"]).total_seconds()) for other in close)
                if best_gap is None or gap < best_gap:
                    best, best_gap = group, gap
        if best is None:
            groups.append([member])
        else:
            best.append(member)

    nodes = []
    for group in groups:
        first = min(group, key=lambda m: m["moment"])
        jp_member = next((m for m in group if m["source"] == "jp_post"), None)
        kind = first["kind"]
        segment = (first["segment"] or forecast.segment_at(stages, first["moment"])
                   or forecast.stage_segment(first["stage"] or "accepted"))
        primary = SEGMENT_PRIMARY[segment] if has_cn else ("milestone" if segment == "warehouse" else "jp_post")
        sources = [m["source"] for m in sorted(group, key=lambda m: m["moment"])]
        if kind in SAME_SOURCE_KINDS:
            key = f"{kind}@{_fmt(first['moment'], first['date_only'])}"
            name = _left_collection_name(group)
            visible = first["source"] == primary
        elif kind:
            key = f"{kind}@{_fmt(first['moment'], first['date_only'])}"
            name = kind_name(kind, ctx, jp_member["detail"] if jp_member and first["stage"] not in forecast.ORIGIN_SIDE_STAGES else None)
            # 共有节点：主来源报了就显示，主来源没报、另一来源先报也先显示
            visible = True
        else:
            key = f"{first['source']}:{first['status']}@{first['record_at']}"
            name = first["name"]
            # 例外（pinned）：第二段航班落地与「日本邮政接收（寄达地）」是日本段的起点，只有中国邮政会报，日本段虽以日本邮政为主也显示
            visible = first["source"] == primary or first["pinned"]
        nodes.append({
            "key": key,
            "kind": kind,
            "at": _fmt(first["moment"], first["date_only"]),
            "moment": first["moment"],
            "segment": segment,
            # 中国邮政的日本段事件不映射阶段（日本段以日本邮政为准），共有节点取成员里有的阶段，再退回 kind 对应的阶段
            "stage": next((m["stage"] for m in group if m["stage"]), KIND_STAGE.get(kind)),
            "name": name,
            "sources": list(dict.fromkeys(sources)),
            "original": jp_member["status"] if jp_member else None,
            "detail": (jp_member or first)["detail"],
            "visible": visible,
            "times": [(_fmt(m["moment"], m["date_only"])) for m in group],
            "role": next((m["role"] for m in group if m["role"]), None),
            # 各成员作为独有节点时的 key：状态规则生效后事件可能归到共有类型、key 随之改变，判重时也认旧 key
            "member_keys": [f"{m['source']}:{m['status']}@{m['record_at']}" for m in group],
        })

    for stage, at in (milestones or {}).items():
        moment = forecast.parse_stored_time(at)
        if moment is None:
            continue
        nodes.append({
            "key": f"milestone:{stage}", "kind": None, "at": str(at), "moment": moment, "segment": "warehouse",
            "stage": stage, "name": forecast.stage_label(stage), "sources": ["milestone"], "original": None,
            "detail": "", "visible": True, "times": [], "role": None, "member_keys": [],
        })
    # 只有日期的节点（「持ち出し中」）按当天 12:00 算会排到当天更晚阶段（如上午妥投）之后：
    # 排序时挪到同一天第一个阶段顺序更靠后的有时刻节点之前
    order = lambda node: forecast.STAGE_ORDER.get(node["stage"] or "", -1)  # noqa: E731
    def sort_moment(node):
        if len(node["at"]) > 10 or node["stage"] is None:
            return node["moment"]
        later = [other["moment"] for other in nodes
                 if len(other["at"]) > 10 and other["moment"].date() == node["moment"].date() and order(other) > order(node)]
        return min([node["moment"], *[moment - timedelta(seconds=1) for moment in later]])

    # 先算好排序键：list.sort 排序期间列表本身是空的，键函数里不能再读 nodes
    keys = {id(node): (sort_moment(node), order(node)) for node in nodes}
    nodes.sort(key=lambda node: keys[id(node)])
    return nodes


# --- 推送判重 ---

def _parse_key_time(key):
    if "@" not in key:
        return None, None
    head, at = key.rsplit("@", 1)
    moment = forecast.parse_stored_time(at)
    return head, moment


def node_notified(node, notified_keys) -> bool:
    """节点是否已推送过：key 完全相同；或任一成员作为独有节点的 key 记过（状态规则生效后归到共有类型的事件）；
    或共有节点同 kind 且任一成员时间与已记录 key 的时间相差 ≤ MATCH_SECONDS（另一边晚到、时间差一分钟的同一节点不会被当成新节点）。"""
    if node["key"] in notified_keys or any(key in notified_keys for key in node.get("member_keys", ())):
        return True
    if not node["kind"]:
        return False
    times = [forecast.parse_stored_time(at) for at in node["times"]]
    for key in notified_keys:
        kind, moment = _parse_key_time(key)
        if kind != node["kind"] or moment is None:
            continue
        for at in times:
            if at is not None and abs((at - moment).total_seconds()) <= MATCH_SECONDS:
                return True
    return False


def pushable_nodes(nodes) -> list:
    """可推送的节点：合并履历里可见的承运方节点（发货前节点是手填 / 调用方写入的，不推送）。"""
    return [node for node in nodes if node["visible"] and node["sources"] != ["milestone"]]


def push_eligible(nodes) -> set:
    """nodes（按时间先后）里白名单节点的 key：有推送角色（PUSH_ROLES），且一单只推一次的角色（ONCE_ROLES）只取最早那个。"""
    keys, seen_once = set(), set()
    for node in nodes:
        role = node.get("role")
        if not role:
            continue
        if role in ONCE_ROLES:
            if role in seen_once:
                continue
            seen_once.add(role)
        keys.add(node["key"])
    return keys


def node_title(node) -> str:
    """节点的显示名（推送标题与网页任务卡片的「当前步骤」共用）：还没归类的日本邮政新状态用「原文（未识别）」。"""
    if node.get("role") == "unrecognized" and node.get("original"):
        return f"{node['original']}（未识别）"
    return node["name"]


def public_node(node) -> dict:
    """接口返回用：去掉内部字段（datetime、成员时间）。"""
    return {key: node[key] for key in ("key", "at", "segment", "stage", "name", "sources", "original", "detail")}

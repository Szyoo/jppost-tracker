"""合并履历：把日本邮政（jp_post）、中国邮政（cn_ems）与发货前节点合成一条按段分组的「节点」列表，
并给每个节点起带方向与地点的中文名。推送（tracker）与网页显示共用这一份。

纯函数：不碰数据库与网络，只 import forecast 与 jp_status。

## 节点与归并
- 两个来源里含义相同的事件（KIND 对照：jp_post 状态 + 所在地 ↔ cn_ems 描述），换算到日本时间后相差 ≤ MATCH_SECONDS，
  归并为同一个「共有节点」；其余事件各自成节点（独有节点）。
- 共有节点的 key = `<kind>@<成员里最早的时间>`；判断「推送过没有」时按 kind 相同、任一成员时间与已记录 key 的时间相差
  ≤ MATCH_SECONDS 认定为同一节点（node_notified），所以不论哪边先报，另一边晚到（时间差 1 分钟）都不会被当成新节点。
  独有节点的 key = `<source>:<原始状态>@<record_at>`，发货前节点 `milestone:<stage>`（不推送）。
- 显示：每段有「主来源」——仓库段用发货前节点，国内段 / 中转段用 cn_ems，日本段用 jp_post；
  没有 cn_ems 数据时国内段 / 中转段也用 jp_post（与只有日本邮政时一致）。
  主来源的节点都显示；另一来源的共有节点即使主来源还没报也先显示（标该来源），主来源到了就合并显示两边一致；
  另一来源的独有事件不显示（主来源更密集）；唯一例外是「航班到达日本」（日本段的起点，只有中国邮政报）。

## 命名
始发国名按单号国家码（forecast.origin_country_name），中转地名取 TRANSIT_REGION_LABEL；
互换局英文名查 EXCHANGE_ZH（未收录原样显示英文），cn_ems 的【…】里的名字直接用，日本地名保留原文。
"""
import re

import forecast
import jp_status

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
    return None


def cn_kind(status: str):
    """中国邮政事件描述 → kind；对不上日本邮政的返回 None。"""
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

# 两边都可能报出的 kind（只有这些参与跨来源归并、算作「共有节点」）；
# 只有日本邮政报的（到达始发国互换局、日本海关的提交 / 扣留 / 放行）按独有事件处理
SHARED_KINDS = frozenset({
    "accept", "export_customs_submit", "export_customs_hold", "export_customs_release", "depart_origin",
    "arrive_japan_exchange", "import_customs", "depart_japan_exchange", "local_arrive", "out_for_delivery", "delivered",
})


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
        self.japan_exchange = None
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
        self.origin_exchange = self.origin_exchange or cn_exchange or f"{self.origin}国际互换局"
        self.city = _city_of(self.origin_exchange) if (self.origin_exchange != f"{self.origin}国际互换局") else ""
        self.japan_exchange = self.japan_exchange or "日本国际互换局"

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
    japan_exchange = place or ctx.japan_exchange
    local = place or ctx.local_office or "日本派送局"
    names = {
        "accept": f"{ctx.origin}收寄",
        "export_customs_submit": f"提交{ctx.origin}出口海关查验{ctx.city_paren()}",
        "export_customs_hold": f"{ctx.origin}出口海关扣留待验{ctx.city_paren()}",
        "export_customs_release": f"{ctx.origin}出口海关放行{ctx.city_paren()}",
        "arrive_origin_exchange": f"到达{ctx.origin_exchange}",
        "depart_origin": f"离开{ctx.origin_exchange}（出境）",
        "arrive_japan_exchange": f"到达{japan_exchange}（入境）",
        "import_customs_submit": f"提交日本进口海关查验（{japan_exchange}）",
        "import_customs_hold": f"日本进口海关扣留待验（{japan_exchange}）",
        "import_customs_release": f"日本进口海关放行（{japan_exchange}）",
        "import_customs": f"日本进口通关中（{japan_exchange}）",
        "depart_japan_exchange": f"离开{japan_exchange}",
        "local_arrive": f"到达{local}",
        "out_for_delivery": f"{local} 派送中",
        "delivered": f"已妥投（{local}）",
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


def cn_name(record, ctx: Context, flights: dict) -> str:
    """中国邮政一条事件的显示名。flights：{record_at: 航段名}，由 _flight_names 按序列算好。"""
    status = str(record["status"] or "").strip()
    kind = cn_kind(status)
    if kind:
        return kind_name(kind, ctx)
    if record["record_at"] in flights.get(status, {}):
        return flights[status][record["record_at"]]
    city = _city_of(record.get("detail")) if record.get("detail") else ""
    if status.startswith("已交承运商运输"):
        return f"交航（{city}）" if city else "交航"
    if status.startswith("航空公司接收"):
        return f"航空公司接收（{city}）" if city else "航空公司接收"
    if status == "到达寄达地":
        return "航班到达日本"
    return status


def _flight_names(cn_records, ctx: Context) -> dict:
    """航班事件按序列命名：{描述: {record_at: 名字}}。两段航班：第一条启运「中国→韩国 航班起飞」、
    其后第一条飞机进港「到达韩国中转」、第二条启运「韩国→日本 航班起飞」；之后的飞机进港「飞机抵达日本」。
    一段航班：启运「中国→日本 航班起飞」，之后的飞机进港「飞机抵达日本」。"""
    departs = sorted({r["record_at"] for r in cn_records if str(r["status"]).startswith("航空公司启运")})
    arrives = sorted({r["record_at"] for r in cn_records if str(r["status"]).startswith("飞机进港")})
    result = {"航空公司启运": {}, "飞机进港": {}}
    if not departs:
        return result
    transit = ctx.transit_name()
    if len(departs) >= 2:
        first, second = departs[0], departs[1]
        result["航空公司启运"][first] = f"{ctx.origin}→{transit} 航班起飞"
        result["航空公司启运"][second] = f"{transit}→日本 航班起飞"
        landed = [at for at in arrives if first <= at <= second]
        if landed:
            result["飞机进港"][landed[0]] = f"到达{transit}中转"
        for at in arrives:
            if at > second:
                result["飞机进港"][at] = "飞机抵达日本"
    else:
        result["航空公司启运"][departs[0]] = f"{ctx.origin}→日本 航班起飞"
        for at in arrives:
            if at > departs[0]:
                result["飞机进港"][at] = "飞机抵达日本"
    return result


# --- 节点 ---

def _fmt(moment, date_only=False) -> str:
    return moment.strftime("%Y-%m-%d") if date_only else moment.strftime("%Y-%m-%d %H:%M")


def _dedupe(records) -> list:
    seen, result = set(), []
    for record in records or []:
        key = (record["source"], record["record_at"], record["status"], record.get("detail") or "")
        if key not in seen:
            seen.add(key)
            result.append(record)
    return result


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
            kind = kind if kind in SHARED_KINDS else None
            name = jp_name(status, record.get("detail", ""), ctx)
            detail = jp_status.detail_zh(record.get("detail", ""))
        else:
            kind = cn_kind(status)
            stage = forecast.record_stage("cn_ems", status, record.get("detail", ""))
            name = cn_name(record, ctx, flights)
            detail = str(record.get("detail") or "")
        members.append({
            "source": record["source"], "moment": moment, "date_only": date_only, "kind": kind,
            "stage": stage, "name": name, "detail": detail,
            "status": status, "record_at": record["record_at"],
        })
    members.sort(key=lambda m: (m["moment"], m["source"]))

    # 共有节点：同 kind、不同来源、时间相近的归为一组；同来源的同类事件各自成组
    groups = []
    for member in members:
        target = None
        if member["kind"]:
            for group in groups:
                if group[0]["kind"] != member["kind"]:
                    continue
                if any(other["source"] == member["source"] for other in group):
                    continue
                if any(_close(other, member) for other in group):
                    target = group
                    break
        if target is None:
            groups.append([member])
        else:
            target.append(member)

    nodes = []
    for group in groups:
        first = min(group, key=lambda m: m["moment"])
        jp_member = next((m for m in group if m["source"] == "jp_post"), None)
        kind = first["kind"]
        segment = forecast.segment_at(stages, first["moment"]) or forecast.stage_segment(first["stage"] or "accepted")
        primary = SEGMENT_PRIMARY[segment] if has_cn else ("milestone" if segment == "warehouse" else "jp_post")
        sources = [m["source"] for m in sorted(group, key=lambda m: m["moment"])]
        if kind:
            key = f"{kind}@{_fmt(first['moment'], first['date_only'])}"
            name = kind_name(kind, ctx, jp_member["detail"] if jp_member and first["stage"] not in forecast.ORIGIN_SIDE_STAGES else None)
            # 共有节点：主来源报了就显示，主来源没报、另一来源先报也先显示
            visible = True
        else:
            key = f"{first['source']}:{first['status']}@{first['record_at']}"
            name = first["name"]
            # 例外：「航班到达日本」（landed_destination）是日本段的起点，只有中国邮政会报，日本段虽以日本邮政为主也显示
            visible = first["source"] == primary or first["stage"] == "landed_destination"
        nodes.append({
            "key": key,
            "kind": kind,
            "at": _fmt(first["moment"], first["date_only"]),
            "moment": first["moment"],
            "segment": segment,
            "stage": first["stage"],
            "name": name,
            "sources": list(dict.fromkeys(sources)),
            "original": jp_member["status"] if jp_member else None,
            "detail": (jp_member or first)["detail"],
            "visible": visible,
            "times": [(_fmt(m["moment"], m["date_only"])) for m in group],
        })

    for stage, at in (milestones or {}).items():
        moment = forecast.parse_stored_time(at)
        if moment is None:
            continue
        nodes.append({
            "key": f"milestone:{stage}", "kind": None, "at": str(at), "moment": moment, "segment": "warehouse",
            "stage": stage, "name": forecast.stage_label(stage), "sources": ["milestone"], "original": None,
            "detail": "", "visible": True, "times": [],
        })
    nodes.sort(key=lambda node: (node["moment"], forecast.STAGE_ORDER.get(node["stage"] or "", 0)))
    return nodes


# --- 推送判重 ---

def _parse_key_time(key):
    if "@" not in key:
        return None, None
    head, at = key.rsplit("@", 1)
    moment = forecast.parse_stored_time(at)
    return head, moment


def node_notified(node, notified_keys) -> bool:
    """节点是否已推送过：key 完全相同；或共有节点同 kind 且任一成员时间与已记录 key 的时间相差 ≤ MATCH_SECONDS
    （另一边晚到、时间差一分钟的同一节点不会被当成新节点）。"""
    if node["key"] in notified_keys:
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


def public_node(node) -> dict:
    """接口返回用：去掉内部字段（datetime、成员时间）。"""
    return {key: node[key] for key in ("key", "at", "segment", "stage", "name", "sources", "original", "detail")}


def push_line(node) -> str:
    """推送正文里一个节点：「11/13 07:20 到达東京国際郵便局（入境）（国際交換局に到着）」——日本邮政来源的带原文。"""
    moment = node["moment"]
    when = f"{moment.month}/{moment.day}" + ("" if len(node["at"]) <= 10 else f" {moment:%H:%M}")
    original = f"（{node['original']}）" if node.get("original") else ""
    return f"{when} {node['name']}{original}"


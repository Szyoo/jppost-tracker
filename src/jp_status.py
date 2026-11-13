"""日本邮政履历状态的中文显示。

库里一律存原文（last_tracking_info 的判重、阶段映射都按原文），只在显示与推送时翻译。
状态字符串是日本邮政的外部数据，无法穷举：表里没有的状态原样显示，并在日志里记一次（同一进程同一状态只记一次），
方便以后补表——这不违反「文案穷举、不写兜底」的原则，那条原则管的是本项目自己定义的枚举。
本模块是纯函数（日志只是 print），不 import 本项目其他模块。
"""
import re

# 日本邮政状态 → 中文。地名、局名不翻译（在 detail 里，原样保留）
JP_STATUS_ZH = {
    # 收寄与转运
    "引受": "收寄",
    "通過": "经过",
    "発送": "发出",
    "到着": "到达派送局",
    "中継": "中转",
    # 国际互换局与海关
    "国際交換局に到着": "到达国际互换局",
    "国際交換局から発送": "离开国际互换局",
    "税関検査のため税関へ提示": "提交海关查验",
    "税関保留": "海关扣留待验",
    "税関から受領": "海关放行",
    "通関手続中": "通关中",
    "通関手続終了": "通关完毕",
    "輸入許可": "进口许可",
    "外国から到着": "从国外到达",
    "外国へ発送": "发往国外",
    # 派送
    "持ち出し中": "派送中",
    "お届け先にお届け済み": "已妥投",
    "お届け済み": "已妥投",
    "窓口でお渡し": "窗口领取",
    "お届け先にお伺いしましたがご不在でした": "投递时无人在家",
    "ご不在のため持ち戻り": "无人在家已带回",
    "再配達": "再次投递",
    "配達希望受付": "已受理指定投递",
    "保管": "局内保管",
    "保管期間経過のため返送": "保管期满退回",
    "転送": "转寄",
    "返送": "退回",
    "差出人に返送": "退回寄件人",
    "差出人に返送済み": "已退回寄件人",
    "調査中": "调查中",
}

# detail 里的「配達予定日：10月27日」→「预计投递日：10月27日」
_DETAIL_PATTERNS = (
    (re.compile(r"配達予定日[:：]\s*"), "预计投递日："),
)

# 履历 info 的日期时刻前缀（与 tracker / forecast 解析同一格式）
_INFO_PREFIX = re.compile(r"^(\s*\d{4}/\d{1,2}/\d{1,2}(?:\s*\d{1,2}:\d{2})?\s*)(.*?)\s*$", re.S)

_logged_unknown: set = set()


def _log_unknown(status: str):
    if status and status not in _logged_unknown:
        _logged_unknown.add(status)
        print(f"[汉化] 未收录的日本邮政状态：{status}", flush=True)


def status_zh(status) -> str:
    """状态 → 中文；表里没有的原样返回并记一次日志。"""
    text = str(status or "").strip()
    if text in JP_STATUS_ZH:
        return JP_STATUS_ZH[text]
    _log_unknown(text)
    return text


def is_known(status) -> bool:
    return str(status or "").strip() in JP_STATUS_ZH


def detail_zh(detail) -> str:
    text = str(detail or "")
    for pattern, replacement in _DETAIL_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _split_info(info):
    match = _INFO_PREFIX.match(str(info or ""))
    if not match:
        return "", str(info or "").strip()
    return match.group(1), match.group(2)


def info_zh(info) -> str:
    """「2026/10/27 13:50 お届け済み」→「2026/10/27 13:50 已妥投」（网页显示用）。"""
    prefix, status = _split_info(info)
    return f"{prefix}{status_zh(status)}" if status else str(info or "")


def info_push(info) -> str:
    """推送用「中文（原文）」：「2026/10/26 04:30 通关中（通関手続中）」；未收录的状态只显示原文。"""
    prefix, status = _split_info(info)
    if not status:
        return str(info or "")
    if is_known(status):
        return f"{prefix}{JP_STATUS_ZH[status]}（{status}）"
    _log_unknown(status)
    return f"{prefix}{status}"

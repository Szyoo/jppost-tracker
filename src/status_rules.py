"""已生效的状态规则（数据库 status_rules 里 status=done 的行）的进程内副本。

承运方的状态字符串是外部数据，代码内置表（jp_status / route_nodes / forecast）收不全。没收录的状态由后台归类
（ai_classify）或管理员手工补成规则，存进 status_rules 表；storage 每次读履历时把生效的规则同步到这里，
命名（jp_status / route_nodes）、合并类型与推送角色（route_nodes）、阶段映射（forecast）在内置表之后查它。

查找顺序：代码内置表 → 已并入代码的规则（CODE_RULES）→ 数据库里生效的规则 → 未识别。本模块不 import 本项目其他模块（叶子模块，谁都能 import）。
规则的合法性（kind / stage 枚举、中文名长度）在写库前由 route_nodes.validate_rule 校验，这里只存取。
"""

# 已并入代码的规则：{(source, raw_status): {"name_zh", "kind", "stage", "push"}}，优先于数据库里的规则。
# 两行标记之间由 scripts/export_status_rules.py --write 维护（从数据库导出 status=done 的规则），也可以手工改；
# 合法性在 route_nodes 导入时校验。
CODE_RULES = {
    # --- 导出的规则开始 ---
    # --- 导出的规则结束 ---
}

# 数据库里生效的规则（storage 同步进来），结构同上
_RULES: dict = {}


def set_rules(rows) -> None:
    """整体替换规则：rows 是 [{"source", "raw_status", "name_zh", "kind", "stage", "push"}]。"""
    rules = {}
    for row in rows or []:
        rules[(row["source"], str(row["raw_status"]).strip())] = {
            "name_zh": row["name_zh"],
            "kind": row["kind"] or None,
            "stage": row["stage"] or None,
            "push": bool(row["push"]),
        }
    _RULES.clear()
    _RULES.update(rules)


def lookup(source: str, raw_status):
    """生效的规则（先查已并入代码的，再查数据库的）；没有返回 None。"""
    key = (source, str(raw_status or "").strip())
    return CODE_RULES.get(key) or _RULES.get(key)


def in_code(source: str, raw_status) -> bool:
    """这个状态的规则是否已并入代码（CODE_RULES）。"""
    return (source, str(raw_status or "").strip()) in CODE_RULES

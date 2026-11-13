"""把数据库里已生效（status=done）的状态规则导出成 src/status_rules.py 里 CODE_RULES 的条目，方便并入源码。

用法（在仓库根目录）：
    python scripts/export_status_rules.py                 # 打印片段，手工粘进 CODE_RULES 的两行标记之间
    python scripts/export_status_rules.py --write         # 直接写进 src/status_rules.py 的标记之间（与已有条目合并）
    python scripts/export_status_rules.py --db 路径/app.db  # 指定数据库（默认 data/app.db，只读打开）

导出的条目会先经 route_nodes.validate_rule 规整；并入代码后优先于数据库里的同名规则，
该状态也不会再被登记为待归类。
"""
import argparse
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import route_nodes  # noqa: E402
import status_rules  # noqa: E402

RULES_FILE = os.path.join(ROOT, "src", "status_rules.py")
BEGIN = "    # --- 导出的规则开始 ---\n"
END = "    # --- 导出的规则结束 ---\n"


def read_done_rules(db_path: str) -> dict:
    """数据库里 status=done 的规则：{(source, raw_status): 规整后的规则}。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT source, raw_status, name_zh, kind, stage, push FROM status_rules WHERE status = 'done'"
            " ORDER BY source, raw_status"
        ).fetchall()
    finally:
        conn.close()
    rules = {}
    for row in rows:
        try:
            rules[(row["source"], row["raw_status"].strip())] = route_nodes.validate_rule(dict(row))
        except ValueError as exc:
            print(f"跳过不合法的规则 {row['source']} {row['raw_status']!r}：{exc}", file=sys.stderr)
    return rules


def render(rules: dict) -> str:
    """CODE_RULES 的条目（每条一行，按来源、原文排序）。"""
    lines = []
    for (source, raw), rule in sorted(rules.items()):
        value = {"name_zh": rule["name_zh"], "kind": rule["kind"], "stage": rule["stage"], "push": rule["push"]}
        lines.append(f"    ({source!r}, {raw!r}): {value!r},\n")
    return "".join(lines)


def write_into_file(rules: dict, path: str = RULES_FILE) -> int:
    """与文件里已有的条目合并（数据库的覆盖同名的）后写回两行标记之间，返回写入的条目数。"""
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        raise SystemExit(f"{path} 里找不到导出标记")
    merged = {**status_rules.CODE_RULES, **rules}
    pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.S)
    text = pattern.sub(lambda _: BEGIN + render(merged) + END, text, count=1)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return len(merged)


def main(argv=None):
    parser = argparse.ArgumentParser(description="导出已生效的状态规则")
    parser.add_argument("--db", default=os.path.join(ROOT, "data", "app.db"), help="数据库路径（只读打开）")
    parser.add_argument("--write", action="store_true", help="直接写进 src/status_rules.py 的 CODE_RULES")
    args = parser.parse_args(argv)
    rules = read_done_rules(args.db)
    if args.write:
        count = write_into_file(rules)
        print(f"已写入 {count} 条规则到 {os.path.relpath(RULES_FILE, ROOT)}")
    else:
        sys.stdout.write(render(rules))


if __name__ == "__main__":
    main()

"""运维命令行：不经网页直接维护账号的 portal SSO 映射。

用法（在仓库根目录，或容器里 /app 下）：
    python src/manage.py list-accounts
    python src/manage.py map-account <username> <portal_sub>
    python src/manage.py unmap-account <username>

只 import storage，不会拉起 Web 服务、追踪脚本或 Bark；启动时跑一次 ensure_storage，
所以在新列迁移之前执行也安全（会先补上 portal_sub 列）。
"""
import argparse
import os
import sys

from dotenv import load_dotenv

from storage import ensure_storage, get_account_by_username, list_accounts, set_account_portal_sub

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOTENV_PATH = os.path.join(BASE_DIR, ".env")


def cmd_list(_args) -> int:
    print(f"{'id':>4}  {'username':<24} {'role':<6} {'login':<5} portal_sub")
    for account in list_accounts():
        print(
            f"{account['id']:>4}  {account['username']:<24} {account['role']:<6} "
            f"{'on' if account['login_enabled'] else 'off':<5} {account.get('portal_sub') or '-'}"
        )
    return 0


def _find(username: str):
    account = get_account_by_username(username)
    if not account:
        print(f"账号不存在：{username}", file=sys.stderr)
    return account


def cmd_map(args) -> int:
    account = _find(args.username)
    if not account:
        return 1
    try:
        updated = set_account_portal_sub(account["id"], args.portal_sub)
    except ValueError as exc:
        print(f"映射失败：{exc}", file=sys.stderr)
        return 1
    print(f"已映射：{updated['username']} (id {updated['id']}) ← portal {updated['portal_sub']}")
    return 0


def cmd_unmap(args) -> int:
    account = _find(args.username)
    if not account:
        return 1
    set_account_portal_sub(account["id"], None)
    print(f"已清除映射：{account['username']} (id {account['id']})")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="manage.py", description="jppost-tracker 账号 portal 映射维护")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list-accounts", help="列出账号与 portal 映射").set_defaults(func=cmd_list)
    p_map = sub.add_parser("map-account", help="把本地账号映射到 portal 账号 ID")
    p_map.add_argument("username", help="本地账号用户名（accounts.username）")
    p_map.add_argument("portal_sub", help="portal 账号固定 ID（即 X-Portal-Sub，uuid 字符串；不是可改的用户名）")
    p_map.set_defaults(func=cmd_map)
    p_unmap = sub.add_parser("unmap-account", help="清除本地账号的 portal 映射")
    p_unmap.add_argument("username")
    p_unmap.set_defaults(func=cmd_unmap)

    args = parser.parse_args(argv)
    load_dotenv(DOTENV_PATH)
    ensure_storage(DOTENV_PATH)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

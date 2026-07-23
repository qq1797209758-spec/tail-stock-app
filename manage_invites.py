"""服务器端邀请码管理命令；完整邀请码仅在 generate 命令输出一次。"""

from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import sys

from config import SCAN_HISTORY_DATABASE
from services.access_control import SQLiteAccessRepository, create_password_hash


def repository() -> SQLiteAccessRepository:
    invite_secret = os.getenv("INVITE_HMAC_SECRET", "")
    session_secret = os.getenv("SESSION_SECRET", "")
    if not invite_secret or not session_secret:
        raise SystemExit("缺少 INVITE_HMAC_SECRET 或 SESSION_SECRET")
    database = Path(os.getenv("TAIL_STOCK_DATABASE", SCAN_HISTORY_DATABASE)).expanduser()
    return SQLiteAccessRepository(database, invite_secret, session_secret)


def main() -> int:
    parser = argparse.ArgumentParser(description="尾盘选股助手邀请码管理")
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate", help="生成邀请码")
    generate.add_argument("--count", type=int, default=1)
    generate.add_argument("--max-uses", type=int, default=1)
    generate.add_argument("--expires-at", help="ISO 8601 到期时间；不填为永久")
    generate.add_argument("--note", default="")
    generate.add_argument("--created-by", default="cli-admin")
    status = commands.add_parser("status", help="启用或停用邀请码")
    status.add_argument("invite_id", type=int)
    status.add_argument("--active", choices=("yes", "no"), required=True)
    revoke = commands.add_parser("revoke-sessions", help="撤销邀请码的全部会话")
    revoke.add_argument("invite_id", type=int)
    commands.add_parser("list", help="查看脱敏邀请码")
    password = commands.add_parser("hash-password", help="生成管理员密码哈希")
    password.add_argument("--password", help="不建议在共享终端历史中使用")
    args = parser.parse_args()

    if args.command == "hash-password":
        import getpass

        plain = args.password or getpass.getpass("管理员密码：")
        print(create_password_hash(plain))
        return 0

    repo = repository()
    if args.command == "generate":
        expires_at = datetime.fromisoformat(args.expires_at) if args.expires_at else None
        codes = repo.create_invites(
            args.count,
            max_uses=args.max_uses,
            expires_at=expires_at,
            note=args.note,
            created_by=args.created_by,
        )
        print("完整邀请码仅显示本次：")
        print("\n".join(codes))
    elif args.command == "status":
        if not repo.set_invite_active(args.invite_id, args.active == "yes"):
            raise SystemExit("邀请码记录不存在")
        print("已更新；停用时对应会话已同时撤销。")
    elif args.command == "revoke-sessions":
        print(f"已撤销 {repo.revoke_invite_sessions(args.invite_id)} 个会话。")
    elif args.command == "list":
        for item in repo.list_invites():
            print(
                item["id"], item["code_prefix"], item["used_count"], "/",
                item["max_uses"], "启用" if item["is_active"] else "停用", item["note"] or "",
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())

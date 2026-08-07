"""
restore_cli.py
----------------
core.db の復元をコマンドラインから行うためのスクリプト。

なぜ管理API(/admin/backup)だけでなくCLIも用意するか:
- 「復元」が必要になるのは大抵、core.dbが壊れていてgatewayプロセス自体が
  正常起動できない時。API経由の復元だけに頼ると、直したいはずのプロセスに
  復元機能を頼る循環になってしまう。
- CLIならコンテナ内で直接 `python restore_cli.py ...` を叩けるので、
  gatewayの起動可否に依存しない。

使い方(コンテナ内 / gatewayと同じイメージで実行する想定):

    # 利用可能なバックアップ一覧を確認
    python restore_cli.py list --backup-dir /backups

    # 最新のバックアップから復元(確認プロンプトあり)
    python restore_cli.py restore --backup-dir /backups --db-path /shared/core/core.db --latest

    # 特定のバックアップファイルを指定して復元
    python restore_cli.py restore --db-path /shared/core/core.db \\
        --backup-file /backups/core-20260801-120000.db
"""
from __future__ import annotations

import argparse
import sys

from backup import list_backups, restore_core_db


def _cmd_list(args: argparse.Namespace) -> int:
    backups = list_backups(args.backup_dir)
    if not backups:
        print(f"バックアップが見つかりません: {args.backup_dir}")
        return 1
    print(f"{len(backups)}件のバックアップ(新しい順):")
    for path in backups:
        print(f"  {path}")
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    if args.latest:
        backups = list_backups(args.backup_dir)
        if not backups:
            print(f"バックアップが見つかりません: {args.backup_dir}", file=sys.stderr)
            return 1
        backup_file = backups[0]
    elif args.backup_file:
        backup_file = args.backup_file
    else:
        print("--latest か --backup-file のどちらかを指定してください", file=sys.stderr)
        return 1

    print(f"復元元: {backup_file}")
    print(f"復元先: {args.db_path}")
    if not args.yes:
        answer = input("この内容で復元してよろしいですか？ 現在のDBは"
                        "*.before_restore-<timestamp> として退避されます。[y/N]: ")
        if answer.strip().lower() != "y":
            print("中止しました。")
            return 1

    restored_path = restore_core_db(backup_file, args.db_path)
    print(f"復元が完了しました: {restored_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="core.dbのバックアップ一覧表示・復元")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="バックアップ一覧を表示する")
    p_list.add_argument("--backup-dir", default="/backups")
    p_list.set_defaults(func=_cmd_list)

    p_restore = sub.add_parser("restore", help="バックアップからcore.dbを復元する")
    p_restore.add_argument("--backup-dir", default="/backups")
    p_restore.add_argument("--db-path", required=True)
    p_restore.add_argument("--latest", action="store_true", help="最新のバックアップを使う")
    p_restore.add_argument("--backup-file", help="復元元ファイルを直接指定する")
    p_restore.add_argument("--yes", action="store_true", help="確認プロンプトを省略する")
    p_restore.set_defaults(func=_cmd_restore)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

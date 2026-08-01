"""
backup.py
----------
core.db(全アプリ共通の唯一のデータ基盤)を定期的にバックアップする。

設計方針:
- automation_scheduler.py と同じAPSchedulerパターンをそのまま再利用する
  (新しい仕組みを増やさない、という⑤で確認した方針をここでも踏襲する)。
- SQLiteの標準機能である sqlite3.Connection.backup() を使う。これは
  「オンラインバックアップAPI」で、他プロセスがcore.dbに書き込み中でも
  安全にバックアップを取れる(ファイルを単純にcopyするのとは違い、
  書き込み中の不整合なコピーを作ってしまう心配がない)。
- バックアップ先は /backups (docker-compose側でホストのフォルダに
  bind mountする想定)。core_shared_dataボリュームの中に置かないのは、
  「ボリューム自体が壊れた/誤って削除された」場合にバックアップも
  道連れにならないようにするため。
- 古いバックアップは指定日数を過ぎたら自動削除する(無限に溜め続けない)。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger("life_support_os_gateway.backup")


def backup_core_db(db_path: str, backup_dir: str, retention_days: int) -> str:
    """core.dbのオンラインバックアップを1つ作り、古いバックアップを掃除する。
    戻り値は作成したバックアップファイルのパス。
    """
    os.makedirs(backup_dir, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    dest_path = os.path.join(backup_dir, f"core-{timestamp}.db")

    src = sqlite3.connect(db_path)
    try:
        dest = sqlite3.connect(dest_path)
        try:
            src.backup(dest)  # SQLiteのオンラインバックアップAPI(書き込み中でも安全)
        finally:
            dest.close()
    finally:
        src.close()

    logger.info("core.dbのバックアップを作成しました: %s", dest_path)
    _prune_old_backups(backup_dir, retention_days)
    return dest_path


def _prune_old_backups(backup_dir: str, retention_days: int) -> None:
    if retention_days <= 0:
        return  # 0以下は「無制限に残す」設定として扱う
    cutoff = time.time() - retention_days * 86400
    for path in Path(backup_dir).glob("core-*.db"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                logger.info("古いバックアップを削除しました: %s", path)
        except OSError:
            logger.exception("バックアップの削除に失敗しました: %s", path)

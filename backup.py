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


def restore_core_db(backup_path: str, db_path: str) -> str:
    """指定したバックアップファイルから core.db を復元する。

    設計方針:
    - `sqlite3.Connection.backup()` を復元方向にも使う(バックアップ取得と
      対称な実装にすることで、忘れがちな「戻す」側のロジックを新たに
      増やさない)。バックアップファイル自体は正常なSQLiteファイルなので、
      「バックアップ元 (backup_path) → 復元先 (db_path)」という向きで
      同じAPIをそのまま使える。
    - 復元先を直接上書きする前に、"復元直前の状態"を
      `<db_path>.before_restore-<timestamp>` として必ず退避する。
      誤ったバックアップファイルを指定してしまった場合でも、それ自体が
      なかったことにできるようにするため(復元操作こそ、失敗した時の
      被害が一番大きい操作)。
    - 戻り値は実際に書き込んだ db_path。呼び出し側(CLI/管理API)が
      成否をログ・レスポンスに出せるようにする。
    """
    if not os.path.exists(backup_path):
        raise FileNotFoundError(f"バックアップファイルが見つかりません: {backup_path}")

    # 復元先が既に存在する場合のみ退避する(初回セットアップ等でdb_pathが
    # まだ無いケースもあるため)。
    if os.path.exists(db_path):
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        safety_copy = f"{db_path}.before_restore-{timestamp}"
        src = sqlite3.connect(db_path)
        try:
            dest = sqlite3.connect(safety_copy)
            try:
                src.backup(dest)
            finally:
                dest.close()
        finally:
            src.close()
        logger.info("復元前の状態を退避しました: %s", safety_copy)

    src = sqlite3.connect(backup_path)
    try:
        dest = sqlite3.connect(db_path)
        try:
            dest.execute("PRAGMA foreign_keys = OFF")
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()

    logger.info("core.dbを復元しました: %s -> %s", backup_path, db_path)
    return db_path


def list_backups(backup_dir: str) -> list[str]:
    """管理API/CLIから選ばせるための、既存バックアップファイル一覧(新しい順)。"""
    if not os.path.isdir(backup_dir):
        return []
    paths = sorted(Path(backup_dir).glob("core-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [str(p) for p in paths]


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

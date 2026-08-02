"""
backup.py の回帰テスト。

README/開発ログで「取れることは確認済みだが、戻せることはまだ未検証」と
書かれていた部分。ここでは実際に:

  1. データを書き込んだcore.dbのバックアップを取る
  2. core.dbを壊す/別内容で上書きする(=障害を模す)
  3. バックアップから復元する
  4. 元のデータが正しく戻っていることを確認する

という「復元リハーサル」そのものを自動テストとして固定する。
これを毎回のCIで実行しておけば、「バックアップは取れるが実は壊れていて
戻せない」という事故を将来のリファクタで再発させない。
"""
from __future__ import annotations

import sqlite3
import time

from backup import backup_core_db, restore_core_db, list_backups, _prune_old_backups


def _make_db(path: str, rows: list[str]) -> None:
    conn = sqlite3.connect(path)
    # DROP してから作り直す: 「障害後に別内容で上書きされた」ケースを模すため、
    # 呼び出すたびに完全に置き換わる必要がある(INSERTの追記だけだと
    # 「元データ+壊れたデータ」が混ざってしまい、リハーサルの意味が無くなる)。
    conn.execute("DROP TABLE IF EXISTS memory_items")
    conn.execute("CREATE TABLE memory_items (id INTEGER PRIMARY KEY, value TEXT)")
    for value in rows:
        conn.execute("INSERT INTO memory_items (value) VALUES (?)", (value,))
    conn.commit()
    conn.close()


def _read_values(path: str) -> list[str]:
    conn = sqlite3.connect(path)
    rows = [r[0] for r in conn.execute("SELECT value FROM memory_items ORDER BY id").fetchall()]
    conn.close()
    return rows


def test_backup_creates_a_restorable_snapshot(tmp_path):
    db_path = str(tmp_path / "core.db")
    backup_dir = str(tmp_path / "backups")
    _make_db(db_path, ["before-backup-1", "before-backup-2"])

    dest_path = backup_core_db(db_path, backup_dir, retention_days=14)

    assert dest_path in list_backups(backup_dir)
    assert _read_values(dest_path) == ["before-backup-1", "before-backup-2"]


def test_restore_rehearsal_full_cycle(tmp_path):
    """バックアップ→障害を模す→復元→データが戻ることを一気通貫で確認する。"""
    db_path = str(tmp_path / "core.db")
    backup_dir = str(tmp_path / "backups")

    # 1. 正常な状態でバックアップを取る
    _make_db(db_path, ["important-fact-1", "important-fact-2"])
    backup_path = backup_core_db(db_path, backup_dir, retention_days=14)

    # 2. 障害を模す: core.dbが別内容に書き換わってしまった状態
    _make_db(db_path, ["corrupted-or-unexpected-data"])
    assert _read_values(db_path) == ["corrupted-or-unexpected-data"]

    # 3. バックアップから復元する
    restored_path = restore_core_db(backup_path, db_path)
    assert restored_path == db_path

    # 4. 元のデータが戻っていること
    assert _read_values(db_path) == ["important-fact-1", "important-fact-2"]

    # 5. 復元前の状態(=壊れていた状態)も退避されており、
    #    「復元操作自体で何かを消してしまう」ことが無いのを確認する
    safety_copies = list(tmp_path.glob("core.db.before_restore-*"))
    assert len(safety_copies) == 1
    assert _read_values(str(safety_copies[0])) == ["corrupted-or-unexpected-data"]


def test_restore_with_missing_backup_file_raises(tmp_path):
    db_path = str(tmp_path / "core.db")
    _make_db(db_path, ["some-data"])

    import pytest
    with pytest.raises(FileNotFoundError):
        restore_core_db(str(tmp_path / "does-not-exist.db"), db_path)

    # 存在しないバックアップを指定した場合、元のDBには一切手を付けない
    assert _read_values(db_path) == ["some-data"]


def test_restore_into_fresh_environment_without_existing_db(tmp_path):
    """初回セットアップ相当: 復元先にまだcore.dbが無いケース。"""
    db_path = str(tmp_path / "new_environment" / "core.db")
    backup_dir = str(tmp_path / "backups")

    source_db = str(tmp_path / "source.db")
    _make_db(source_db, ["seed-data"])
    backup_path = backup_core_db(source_db, backup_dir, retention_days=14)

    import os
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    restored_path = restore_core_db(backup_path, db_path)

    assert _read_values(restored_path) == ["seed-data"]


def test_prune_removes_only_old_backups(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    old_file = backup_dir / "core-20200101-000000.db"
    old_file.write_text("old")
    new_file = backup_dir / "core-20990101-000000.db"
    new_file.write_text("new")

    # 古いファイルのmtimeを強制的に過去にする
    old_time = time.time() - 30 * 86400
    import os
    os.utime(old_file, (old_time, old_time))

    _prune_old_backups(str(backup_dir), retention_days=14)

    assert not old_file.exists()
    assert new_file.exists()

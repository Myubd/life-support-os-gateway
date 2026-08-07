"""
schema/migrate.py
------------------
コアスキーマの初期化と、冪等なマイグレーション実行機構。

interview_app `db/database.py` の `init_db` / `_run_migrations` パターンを
アプリ非依存に一般化したもの。各アプリはこのモジュールで初期化した
core.db への読み書きに `sqlite3` を直接使うか、将来的にはこのパッケージが
提供するリポジトリ層(未実装、Phase 3で追加予定)を使う。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_SCHEMA_PATH = Path(__file__).resolve().parent / "core_schema.sql"

# 新しいマイグレーションはこのリストの末尾に追記する。
# (バージョン番号, SQL) の形式。番号は連番で、既存のものは変更・削除しない。
_MIGRATIONS: list[tuple[int, str]] = [
    # (1, "ALTER TABLE profiles ADD COLUMN avatar_path TEXT"),
    (1, "INSERT INTO knowledge_items_fts(rowid, title, summary, tags_json) "
        "SELECT id, title, summary, tags_json FROM knowledge_items"),
    (2, "INSERT INTO documents_fts(rowid, title, tags_json) "
        "SELECT id, title, tags_json FROM documents"),
]

_CREATE_MIGRATION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""


def get_connection(db_path: str) -> sqlite3.Connection:
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def db_session(db_path: str) -> Iterator[sqlite3.Connection]:
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _run_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(_CREATE_MIGRATION_TABLE)
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()}
    for version, sql in _MIGRATIONS:
        if version in applied:
            continue
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # 既にカラムが存在する等は許容(冪等性優先)
        conn.execute("INSERT OR IGNORE INTO schema_migrations (version) VALUES (?)", (version,))


def _ensure_fts_tables(conn: sqlite3.Connection) -> None:
    """knowledge_items_fts / documents_fts を作成する。

    日本語のようにスペースで分かち書きされない言語では、既定の unicode61
    トークナイザだと文がほぼ1つの巨大トークンになり実質検索できない
    (「サブスクの棚卸し」という文の中の「サブスク」だけでは引っかからない)。
    そのため trigram トークナイザ(SQLite 3.34+で利用可能)を優先する。
    動作環境のSQLiteビルドによっては trigram が使えない場合があるため、
    その時だけ unicode61 にフォールバックする(英数字主体のデータであれば
    それでも最低限は機能する)。
    """
    specs = [
        ("knowledge_items_fts", "knowledge_items", "title, summary, tags_json"),
        ("documents_fts", "documents", "title, tags_json"),
    ]
    for fts_table, base_table, columns in specs:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (fts_table,)
        ).fetchone()
        if exists:
            continue
        try:
            conn.execute(
                f"CREATE VIRTUAL TABLE {fts_table} USING fts5("
                f"{columns}, content='{base_table}', content_rowid='id', "
                f"tokenize='trigram')"
            )
        except sqlite3.OperationalError:
            conn.execute(
                f"CREATE VIRTUAL TABLE {fts_table} USING fts5("
                f"{columns}, content='{base_table}', content_rowid='id', "
                f"tokenize='unicode61')"
            )


def init_core_schema(db_path: str = "core.db") -> None:
    """コアスキーマ(profile/schedule_items/knowledge_items 等)を初期化する。
    アプリ起動時に一度呼べばよい(何度呼んでも安全)。
    """
    with db_session(db_path) as conn:
        conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        _ensure_fts_tables(conn)
        _run_migrations(conn)


def register_source_app(db_path: str, app_key: str, display_name: str) -> None:
    """新規アプリを source_apps に登録する。各アプリの起動時に自身を登録する。"""
    with db_session(db_path) as conn:
        conn.execute(
            "INSERT INTO source_apps (app_key, display_name) VALUES (?, ?) "
            "ON CONFLICT(app_key) DO UPDATE SET display_name = excluded.display_name",
            (app_key, display_name),
        )

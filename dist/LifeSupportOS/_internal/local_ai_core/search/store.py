"""
search/store.py
----------------
knowledge_items / documents を対象にした、全アプリ横断の全文検索(SQLite FTS5)。

設計方針:
- 新しいスコープは作らない。検索が触れるデータは既存の "knowledge_items:read"
  "documents:read" と同じものなので、これらのスコープをそのまま流用する
  (search専用のスコープを増やすと、ユーザーが許可を求められる項目が
  実質的な意味もなく増えるだけになるため)。
- 1つのクエリで両方の対象を検索するが、呼び出し元(app_key)が
  どちらか一方しか許可されていない場合でも、検索そのものは失敗させず、
  許可されている対象だけを返す(memory/documents/knowledge の各ストアは
  「未許可なら例外」だが、検索は横断的な性質上「未許可な対象は黙って
  除外する」方が自然な挙動になる)。どちらも未許可の場合のみ
  PermissionDenied を送出する。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from ..schema.migrate import db_session
from ..permissions.gate import PermissionGate, PermissionDenied


@dataclass
class SearchHit:
    source: str  # "knowledge_items" | "documents"
    id: int
    source_app: str
    title: str
    snippet: str
    tags: list


def _row_to_hit(source: str, row) -> SearchHit:
    return SearchHit(
        source=source,
        id=row["id"],
        source_app=row["source_app"],
        title=row["title"],
        snippet=row["snippet"] or "",
        tags=json.loads(row["tags_json"]) if row["tags_json"] else [],
    )


class SearchStore:
    def __init__(self, db_path: str = "core.db", gate: Optional[PermissionGate] = None):
        self.db_path = db_path
        self.gate = gate or PermissionGate(db_path)

    def search(
        self,
        profile_id: int,
        app_key: str,
        query: str,
        limit: int = 20,
    ) -> list[SearchHit]:
        """knowledge_items / documents を横断検索する。

        query はそのままFTS5のMATCH式に渡すのではなく、記号を含む自由入力でも
        落ちないよう簡易にエスケープしたうえで渡す(ユーザーが入力した
        "?" や "-" 等がFTS5のクエリ構文として解釈されて500になるのを防ぐ)。
        """
        query = (query or "").strip()
        if not query:
            return []
        fts_query = _to_fts_query(query)

        can_read_knowledge = self.gate.is_granted(profile_id, app_key, "knowledge_items:read")
        can_read_documents = self.gate.is_granted(profile_id, app_key, "documents:read")

        if not can_read_knowledge and not can_read_documents:
            # どちらも未許可の場合のみ、通常のストアと同様に例外にする
            # (accessログにも残す)。片方だけ許可の場合は例外にせず、
            # 許可されている方だけ検索してaccessログにも両方の判定を残す。
            self.gate.require(profile_id, app_key, "knowledge_items:read")

        hits: list[SearchHit] = []

        if can_read_knowledge:
            self.gate.require(profile_id, app_key, "knowledge_items:read")
            with db_session(self.db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT k.id AS id, k.source_app AS source_app, k.title AS title,
                           k.tags_json AS tags_json,
                           snippet(knowledge_items_fts, 1, '[', ']', '…', 12) AS snippet
                    FROM knowledge_items_fts
                    JOIN knowledge_items k ON k.id = knowledge_items_fts.rowid
                    WHERE knowledge_items_fts MATCH ? AND k.profile_id = ? AND k.is_active = 1
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (fts_query, profile_id, limit),
                ).fetchall()
            hits.extend(_row_to_hit("knowledge_items", row) for row in rows)

        if can_read_documents:
            self.gate.require(profile_id, app_key, "documents:read")
            with db_session(self.db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT d.id AS id, d.source_app AS source_app, d.title AS title,
                           d.tags_json AS tags_json,
                           snippet(documents_fts, 0, '[', ']', '…', 12) AS snippet
                    FROM documents_fts
                    JOIN documents d ON d.id = documents_fts.rowid
                    WHERE documents_fts MATCH ? AND d.profile_id = ? AND d.is_active = 1
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (fts_query, profile_id, limit),
                ).fetchall()
            hits.extend(_row_to_hit("documents", row) for row in rows)

        return hits


def _to_fts_query(raw: str) -> str:
    """ユーザー入力をFTS5のMATCH式として安全な形にする。

    各トークンを二重引用符で囲んだフレーズとして扱い、AND(暗黙のスペース区切り)
    で連結する。これによりFTS5独自の演算子(-, ^, *, : など)がそのまま
    構文として解釈されて構文エラーになることを避ける。
    """
    tokens = raw.replace('"', ' ').split()
    if not tokens:
        return '""'
    return " ".join(f'"{t}"' for t in tokens)

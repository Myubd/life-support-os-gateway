"""
knowledge/store.py
-------------------
全アプリ共通の「資料/ナレッジ台帳」(knowledge_items)への権限ゲート付きアクセス。

設計方針:
- ここに置くのは要約・タグ・参照IDのみ。資料の全文やembeddingは
  引き続き各アプリ側(interview_appのRAG基盤等)に残す。
  横断検索や他アプリからの「この人が何を調べているか」の把握は、
  この要約だけで十分行える設計にする。
- 読み書きは memory と同様に必ず PermissionGate を経由する。
  スコープは "knowledge_items:read" / "knowledge_items:write"(リソース単位。
  カテゴリ単位で絞りたい場合は将来 "knowledge_items:read:category.*" のような
  形式に拡張できるが、現時点ではリソース単位で十分)。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ..schema.migrate import db_session
from ..permissions.gate import PermissionGate


@dataclass
class KnowledgeItem:
    id: int
    source_app: str
    source_ref_id: Optional[str]
    category: Optional[str]
    title: str
    summary: Optional[str]
    tags: list
    is_active: bool
    created_at: str


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class KnowledgeStore:
    def __init__(self, db_path: str = "core.db", gate: Optional[PermissionGate] = None):
        self.db_path = db_path
        self.gate = gate or PermissionGate(db_path)

    def upsert(
        self,
        profile_id: int,
        app_key: str,
        source_ref_id: str,
        title: str,
        category: Optional[str] = None,
        summary: Optional[str] = None,
        tags: Optional[list] = None,
    ) -> int:
        """アプリ側のIDを軸に、なければ作成・あれば更新する(全文は持たず要約のみ)。"""
        self.gate.require(profile_id, app_key, "knowledge_items:write")

        with db_session(self.db_path) as conn:
            existing = conn.execute(
                "SELECT id FROM knowledge_items WHERE profile_id = ? AND source_app = ? AND source_ref_id = ?",
                (profile_id, app_key, source_ref_id),
            ).fetchone()
            tags_json = json.dumps(tags or [], ensure_ascii=False)
            if existing:
                conn.execute(
                    """
                    UPDATE knowledge_items
                    SET title = ?, category = ?, summary = ?, tags_json = ?
                    WHERE id = ?
                    """,
                    (title, category, summary, tags_json, existing["id"]),
                )
                return existing["id"]
            cur = conn.execute(
                """
                INSERT INTO knowledge_items
                    (profile_id, source_app, source_ref_id, category, title, summary, tags_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (profile_id, app_key, source_ref_id, category, title, summary, tags_json),
            )
            return cur.lastrowid

    def list_active(
        self,
        profile_id: int,
        app_key: str,
        category: Optional[str] = None,
    ) -> list[KnowledgeItem]:
        """このprofileの有効なナレッジ一覧(他アプリ発のものも含む)を返す。
        このメソッドは全アプリ横断で読むため "knowledge_items:read" 許可が要る。
        """
        self.gate.require(profile_id, app_key, "knowledge_items:read")

        query = (
            "SELECT id, source_app, source_ref_id, category, title, summary, tags_json, "
            "is_active, created_at FROM knowledge_items "
            "WHERE profile_id = ? AND is_active = 1"
        )
        params: list = [profile_id]
        if category:
            query += " AND category = ?"
            params.append(category)
        query += " ORDER BY created_at DESC"

        with db_session(self.db_path) as conn:
            rows = conn.execute(query, params).fetchall()

        return [
            KnowledgeItem(
                id=row["id"],
                source_app=row["source_app"],
                source_ref_id=row["source_ref_id"],
                category=row["category"],
                title=row["title"],
                summary=row["summary"],
                tags=json.loads(row["tags_json"]) if row["tags_json"] else [],
                is_active=bool(row["is_active"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def deactivate(self, profile_id: int, app_key: str, source_ref_id: str) -> None:
        """アプリ側で資料が削除・非アクティブ化された時に呼ぶ(削除ではなく非表示化)。"""
        self.gate.require(profile_id, app_key, "knowledge_items:write")
        with db_session(self.db_path) as conn:
            conn.execute(
                "UPDATE knowledge_items SET is_active = 0 "
                "WHERE profile_id = ? AND source_app = ? AND source_ref_id = ?",
                (profile_id, app_key, source_ref_id),
            )

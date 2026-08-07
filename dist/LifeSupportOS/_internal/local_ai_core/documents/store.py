"""
documents/store.py
-------------------
全アプリ共通の「ドキュメントセンター」。

設計方針(README「追加したいモジュール」の1つ):
- ここに保存するのは常にファイルの所在(file_path)とメタデータのみ。
  ファイルの中身はこのモジュールを経由してコピー・保存しない。
  「共通データ基盤に個人の資料の中身を全部集める」ことは、単一障害点を増やし、
  かつ「AIが全部知っている」状態そのものになるため、設計上あえて避けている。
  各アプリ(就活支援のES資料、家計管理の領収書、学習支援の教材など)は、
  自分のアプリ領域にファイルを保存したまま、このドキュメントセンターには
  「登録」だけを行う。実ファイルへのアクセス(読み込み・表示)は、この
  モジュールの責務ではなく、各アプリ自身が file_path を使って行う。
- 読み書きは他の共通ストアと同様に必ず PermissionGate を経由する。
  スコープは "documents:read" / "documents:write"。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ..schema.migrate import db_session
from ..permissions.gate import PermissionGate


@dataclass
class DocumentItem:
    id: int
    source_app: str
    source_ref_id: Optional[str]
    title: str
    category: Optional[str]
    file_path: str
    file_hash: Optional[str]
    mime_type: Optional[str]
    tags: list
    is_active: bool
    created_at: str
    updated_at: str


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class DocumentStore:
    def __init__(self, db_path: str = "core.db", gate: Optional[PermissionGate] = None):
        self.db_path = db_path
        self.gate = gate or PermissionGate(db_path)

    def register(
        self,
        profile_id: int,
        app_key: str,
        file_path: str,
        title: str,
        source_ref_id: Optional[str] = None,
        category: Optional[str] = None,
        file_hash: Optional[str] = None,
        mime_type: Optional[str] = None,
        tags: Optional[list] = None,
    ) -> int:
        """アプリがファイルを保存した後に「このファイルの存在」を申告する。
        同じ app_key + source_ref_id が既にあれば更新(重複登録しない)。
        source_ref_id を指定しない場合は file_path を突合キーとして使う。
        """
        self.gate.require(profile_id, app_key, "documents:write")

        key_ref = source_ref_id or file_path
        with db_session(self.db_path) as conn:
            existing = conn.execute(
                "SELECT id FROM documents WHERE profile_id = ? AND source_app = ? "
                "AND COALESCE(source_ref_id, file_path) = ?",
                (profile_id, app_key, key_ref),
            ).fetchone()
            tags_json = json.dumps(tags or [], ensure_ascii=False)
            if existing:
                conn.execute(
                    """
                    UPDATE documents
                    SET title = ?, category = ?, file_path = ?, file_hash = ?,
                        mime_type = ?, tags_json = ?, is_active = 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (title, category, file_path, file_hash, mime_type, tags_json,
                     _now_iso(), existing["id"]),
                )
                return existing["id"]
            cur = conn.execute(
                """
                INSERT INTO documents
                    (profile_id, source_app, source_ref_id, title, category,
                     file_path, file_hash, mime_type, tags_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (profile_id, app_key, source_ref_id, title, category,
                 file_path, file_hash, mime_type, tags_json),
            )
            return cur.lastrowid

    def list_active(
        self,
        profile_id: int,
        app_key: str,
        category: Optional[str] = None,
    ) -> list[DocumentItem]:
        """このprofileの有効なドキュメント一覧(他アプリ発のものも含む)を返す。
        横断的に読むため "documents:read" 許可が必要。
        """
        self.gate.require(profile_id, app_key, "documents:read")

        query = (
            "SELECT id, source_app, source_ref_id, title, category, file_path, "
            "file_hash, mime_type, tags_json, is_active, created_at, updated_at "
            "FROM documents WHERE profile_id = ? AND is_active = 1"
        )
        params: list = [profile_id]
        if category:
            query += " AND category = ?"
            params.append(category)
        query += " ORDER BY updated_at DESC"

        with db_session(self.db_path) as conn:
            rows = conn.execute(query, params).fetchall()

        return [_row_to_item(row) for row in rows]

    def deactivate(self, profile_id: int, app_key: str, document_id: int) -> None:
        """ファイルが削除・非アクティブ化された時に呼ぶ(台帳からの非表示化。
        実ファイルの削除はこのモジュールの責務ではなく、呼び出し側アプリが行う)。
        """
        self.gate.require(profile_id, app_key, "documents:write")
        with db_session(self.db_path) as conn:
            conn.execute(
                "UPDATE documents SET is_active = 0, updated_at = ? "
                "WHERE profile_id = ? AND id = ?",
                (_now_iso(), profile_id, document_id),
            )


def _row_to_item(row) -> DocumentItem:
    return DocumentItem(
        id=row["id"],
        source_app=row["source_app"],
        source_ref_id=row["source_ref_id"],
        title=row["title"],
        category=row["category"],
        file_path=row["file_path"],
        file_hash=row["file_hash"],
        mime_type=row["mime_type"],
        tags=json.loads(row["tags_json"]) if row["tags_json"] else [],
        is_active=bool(row["is_active"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )

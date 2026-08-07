"""
memory/store.py
----------------
全アプリ共通の「AIメモリー」。

設計方針(2番目の優先モジュールだが、権限管理と一体で設計する):
- ここに置くのは「構造化された事実」だけ。生の会話ログ・アップロード資料は
  各アプリ側(将来のDocument Center)に残し、ここには持ち込まない。
  → Memoryが肥大化して「何でも入っている倉庫」になることを防ぐ。
- `confidence` で "user_confirmed"(ユーザーが確認・確定した事実)と
  "ai_inferred"(AIが会話から推測しただけの事実)を必ず区別する。
  他アプリが参照するときは、デフォルトでは両方見えるが、重要な判断
  (提案文の断定表現に使う等)には `only_confirmed=True` を使うことを推奨する。
- 読み書きは必ず PermissionGate を経由する。スコープ形式は
  "memory:read:<key>" / "memory:write:<key>"。
  一覧取得(list_by_prefix)では "memory:read:<prefix>.*" 形式のスコープを要求する。
  これにより「AIが全部知っている」状態を仕組みとして避ける
  (このモジュール単体では、許可されていないキーには一切アクセスできない)。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ..schema.migrate import db_session
from ..permissions.gate import PermissionGate


@dataclass
class MemoryItem:
    key: str
    value: object
    confidence: str  # "user_confirmed" | "ai_inferred"
    source_app: str
    updated_at: str


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class MemoryStore:
    def __init__(self, db_path: str = "core.db", gate: Optional[PermissionGate] = None):
        self.db_path = db_path
        self.gate = gate or PermissionGate(db_path)

    def set(
        self,
        profile_id: int,
        app_key: str,
        key: str,
        value: object,
        confidence: str = "ai_inferred",
    ) -> None:
        """Memoryへの書き込み。confidenceの指定は必須級(既定値に頼らないこと)。
        AIが会話から推測した値は必ず "ai_inferred" のまま書き込み、
        ユーザーが画面上で確認・訂正した時に初めて "user_confirmed" に更新する運用を想定。
        """
        if confidence not in ("user_confirmed", "ai_inferred"):
            raise ValueError("confidence は 'user_confirmed' か 'ai_inferred' のいずれかです")

        self.gate.require(profile_id, app_key, f"memory:write:{key}")

        with db_session(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO memory_items (profile_id, source_app, key, value_json, confidence, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_id, key) DO UPDATE SET
                    source_app = excluded.source_app,
                    value_json = excluded.value_json,
                    confidence = excluded.confidence,
                    updated_at = excluded.updated_at
                """,
                (profile_id, app_key, key, json.dumps(value, ensure_ascii=False), confidence, _now_iso()),
            )

    def get(self, profile_id: int, app_key: str, key: str) -> Optional[MemoryItem]:
        self.gate.require(profile_id, app_key, f"memory:read:{key}")
        with db_session(self.db_path) as conn:
            row = conn.execute(
                "SELECT key, value_json, confidence, source_app, updated_at "
                "FROM memory_items WHERE profile_id = ? AND key = ?",
                (profile_id, key),
            ).fetchone()
        if row is None:
            return None
        return MemoryItem(
            key=row["key"],
            value=json.loads(row["value_json"]),
            confidence=row["confidence"],
            source_app=row["source_app"],
            updated_at=row["updated_at"],
        )

    def list_by_prefix(
        self,
        profile_id: int,
        app_key: str,
        key_prefix: str,
        only_confirmed: bool = False,
    ) -> list[MemoryItem]:
        """例: key_prefix="career" なら "career" 自体と "career.*" 配下をまとめて取得する。
        この呼び出しには "memory:read:<key_prefix>.*" スコープの許可が必要。
        """
        self.gate.require(profile_id, app_key, f"memory:read:{key_prefix}.*")

        query = (
            "SELECT key, value_json, confidence, source_app, updated_at FROM memory_items "
            "WHERE profile_id = ? AND (key = ? OR key LIKE ?)"
        )
        params: list = [profile_id, key_prefix, f"{key_prefix}.%"]
        if only_confirmed:
            query += " AND confidence = 'user_confirmed'"
        query += " ORDER BY key"

        with db_session(self.db_path) as conn:
            rows = conn.execute(query, params).fetchall()

        return [
            MemoryItem(
                key=row["key"],
                value=json.loads(row["value_json"]),
                confidence=row["confidence"],
                source_app=row["source_app"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def forget(self, profile_id: int, app_key: str, key: str) -> None:
        """ユーザーが「この記憶を消してほしい」と言った時に呼ぶ。書き込みと同じ権限を要求する。"""
        self.gate.require(profile_id, app_key, f"memory:write:{key}")
        with db_session(self.db_path) as conn:
            conn.execute(
                "DELETE FROM memory_items WHERE profile_id = ? AND key = ?",
                (profile_id, key),
            )


def _stringify_value(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "、".join(f"{k}: {v}" for k, v in value.items())
    if isinstance(value, list):
        return "、".join(str(v) for v in value)
    return str(value)


def format_items_for_prompt(items: list[MemoryItem]) -> str:
    """MemoryItemのリストを、confidenceラベル付きでプロンプトに埋め込みやすい文字列に整形する。

    各アプリがプロンプトを組み立てる際にこの関数を通すことで、
    "ai_inferred"(AIの推測)を"user_confirmed"(本人確認済み)と同じ重みで
    モデルに渡してしまう事故を防ぐ。ラベルは1件ごとに付与するため、
    一部の項目だけが未確認の場合でも取り違えない。
    空リストの場合は空文字列を返す(呼び出し側でそのままプロンプトに連結してよい)。
    """
    if not items:
        return ""
    lines = []
    for item in items:
        label = "本人確認済み" if item.confidence == "user_confirmed" else "AIの推測・未確認"
        lines.append(f"・[{label}] {item.key}: {_stringify_value(item.value)}")
    return "\n".join(lines)

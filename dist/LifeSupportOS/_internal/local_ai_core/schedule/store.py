"""
schedule/store.py
------------------
全アプリ共通の「予定/タスク/締切」台帳(schedule_items)への権限ゲート付きアクセス。

面接予定・ES締切・生活タスクなど、性質の異なる「いつまでに/いつ」を持つ
情報を同じ器で扱う。読み書きは memory / knowledge と同様に必ず
PermissionGate を経由する。スコープは "schedule_items:read" / "schedule_items:write"。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..schema.migrate import db_session
from ..permissions.gate import PermissionGate


@dataclass
class ScheduleItem:
    id: int
    source_app: str
    source_ref_id: Optional[str]
    item_type: str
    title: str
    detail: Optional[str]
    due_at: Optional[str]
    status: str
    priority: Optional[int]


class ScheduleStore:
    def __init__(self, db_path: str = "core.db", gate: Optional[PermissionGate] = None):
        self.db_path = db_path
        self.gate = gate or PermissionGate(db_path)

    def upsert(
        self,
        profile_id: int,
        app_key: str,
        source_ref_id: str,
        item_type: str,
        title: str,
        due_at: Optional[str] = None,
        detail: Optional[str] = None,
        priority: Optional[int] = None,
        status: str = "open",
    ) -> int:
        """アプリ側のIDを軸に、なければ作成・あれば更新する。
        例: interview_appが確定した面接日程を、この共通テーブルにも反映する。
        """
        self.gate.require(profile_id, app_key, "schedule_items:write")

        with db_session(self.db_path) as conn:
            existing = conn.execute(
                "SELECT id FROM schedule_items WHERE profile_id = ? AND source_app = ? AND source_ref_id = ?",
                (profile_id, app_key, source_ref_id),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE schedule_items
                    SET item_type = ?, title = ?, detail = ?, due_at = ?,
                        status = ?, priority = ?, updated_at = datetime('now', 'localtime')
                    WHERE id = ?
                    """,
                    (item_type, title, detail, due_at, status, priority, existing["id"]),
                )
                return existing["id"]
            cur = conn.execute(
                """
                INSERT INTO schedule_items
                    (profile_id, source_app, source_ref_id, item_type, title, detail, due_at, status, priority)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (profile_id, app_key, source_ref_id, item_type, title, detail, due_at, status, priority),
            )
            return cur.lastrowid

    def list_open(self, profile_id: int, app_key: str) -> list[ScheduleItem]:
        """このprofileの未完了の予定/タスクを期限順に返す(他アプリ発のものも含む)。"""
        self.gate.require(profile_id, app_key, "schedule_items:read")
        with db_session(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, source_app, source_ref_id, item_type, title, detail, due_at, status, priority
                FROM schedule_items
                WHERE profile_id = ? AND status != 'done' AND status != 'cancelled'
                ORDER BY (due_at IS NULL), due_at
                """,
                (profile_id,),
            ).fetchall()
        return [ScheduleItem(**dict(row)) for row in rows]

    def set_status(self, profile_id: int, app_key: str, source_ref_id: str, status: str) -> None:
        self.gate.require(profile_id, app_key, "schedule_items:write")
        with db_session(self.db_path) as conn:
            conn.execute(
                "UPDATE schedule_items SET status = ?, updated_at = datetime('now', 'localtime') "
                "WHERE profile_id = ? AND source_app = ? AND source_ref_id = ?",
                (status, profile_id, app_key, source_ref_id),
            )

"""
automation/store.py
--------------------
「もし〜なら〜する」ルールのCRUD。

設計方針:
- ルールの保存・一覧・有効/無効の切り替えには、データアクセス権限は不要
  (ルール定義そのものは個人情報ではないため)。ただし、実際にルールを
  "実行"してmemory/schedule/knowledge/documentsを読む段階では、
  automation/engine.py が必ず PermissionGate.require() を通す。
  「ルールを登録できること」と「実データにアクセスできること」を分離するのが
  このモジュールの一番の目的(ルール登録だけなら誰でも自由にでき、実行時に
  初めてユーザーの許可が効いてくる)。
- required_scopes は申告のみ。ここに書いても自動で許可はされない。
  ユーザーが PermissionGate.grant() で個別に許可するまで、
  engine.run_rule() はそのスコープを使う手前で必ず止まる。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ..schema.migrate import db_session


@dataclass
class AutomationRule:
    id: int
    profile_id: int
    owner_app: str
    name: str
    trigger_type: str
    trigger_config: dict
    action_type: str
    action_config: dict
    required_scopes: list = field(default_factory=list)
    is_enabled: bool = True
    created_at: str = ""
    updated_at: str = ""


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _row_to_rule(row) -> AutomationRule:
    return AutomationRule(
        id=row["id"],
        profile_id=row["profile_id"],
        owner_app=row["owner_app"],
        name=row["name"],
        trigger_type=row["trigger_type"],
        trigger_config=json.loads(row["trigger_config_json"] or "{}"),
        action_type=row["action_type"],
        action_config=json.loads(row["action_config_json"] or "{}"),
        required_scopes=json.loads(row["required_scopes_json"] or "[]"),
        is_enabled=bool(row["is_enabled"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class AutomationStore:
    def __init__(self, db_path: str = "core.db"):
        self.db_path = db_path

    def create(
        self,
        profile_id: int,
        owner_app: str,
        name: str,
        trigger_type: str,
        action_type: str,
        trigger_config: Optional[dict] = None,
        action_config: Optional[dict] = None,
        required_scopes: Optional[list] = None,
    ) -> int:
        with db_session(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO automation_rules
                    (profile_id, owner_app, name, trigger_type, trigger_config_json,
                     action_type, action_config_json, required_scopes_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile_id, owner_app, name, trigger_type,
                    json.dumps(trigger_config or {}, ensure_ascii=False),
                    action_type,
                    json.dumps(action_config or {}, ensure_ascii=False),
                    json.dumps(required_scopes or [], ensure_ascii=False),
                ),
            )
            return cur.lastrowid

    def get(self, profile_id: int, rule_id: int) -> Optional[AutomationRule]:
        with db_session(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM automation_rules WHERE profile_id = ? AND id = ?",
                (profile_id, rule_id),
            ).fetchone()
        return _row_to_rule(row) if row else None

    def list_enabled(self, profile_id: int) -> list[AutomationRule]:
        with db_session(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM automation_rules WHERE profile_id = ? AND is_enabled = 1 "
                "ORDER BY created_at DESC",
                (profile_id,),
            ).fetchall()
        return [_row_to_rule(row) for row in rows]

    def list_all(self, profile_id: int) -> list[AutomationRule]:
        with db_session(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM automation_rules WHERE profile_id = ? ORDER BY created_at DESC",
                (profile_id,),
            ).fetchall()
        return [_row_to_rule(row) for row in rows]

    def set_enabled(self, profile_id: int, rule_id: int, enabled: bool) -> None:
        with db_session(self.db_path) as conn:
            conn.execute(
                "UPDATE automation_rules SET is_enabled = ?, updated_at = ? "
                "WHERE profile_id = ? AND id = ?",
                (1 if enabled else 0, _now_iso(), profile_id, rule_id),
            )

    def delete(self, profile_id: int, rule_id: int) -> None:
        with db_session(self.db_path) as conn:
            conn.execute(
                "DELETE FROM automation_rules WHERE profile_id = ? AND id = ?",
                (profile_id, rule_id),
            )

    def record_run(
        self,
        profile_id: int,
        rule_id: int,
        status: str,
        result_summary: Optional[str] = None,
    ) -> None:
        with db_session(self.db_path) as conn:
            conn.execute(
                "INSERT INTO automation_runs (rule_id, profile_id, status, result_summary) "
                "VALUES (?, ?, ?, ?)",
                (rule_id, profile_id, status, result_summary),
            )

    def list_runs(self, profile_id: int, rule_id: int, limit: int = 20) -> list[dict]:
        with db_session(self.db_path) as conn:
            rows = conn.execute(
                "SELECT status, result_summary, ran_at FROM automation_runs "
                "WHERE profile_id = ? AND rule_id = ? ORDER BY ran_at DESC LIMIT ?",
                (profile_id, rule_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

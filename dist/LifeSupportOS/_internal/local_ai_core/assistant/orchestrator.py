"""
assistant/orchestrator.py
--------------------------
「ローカルAIデスクトップアシスタント」の中核。長期ビジョンで最も誤解されやすい
部分 — 「ユーザーのすべての情報を理解するAI」という思想を、実装上は
「何でも自動で読めるAI」にしない、という約束を守るためのモジュール。

設計方針:
- アシスタントは質問に答えるために、memory/schedule/knowledge/documents の
  どれを参照したいかを「候補」(ContextSource)として明示的に列挙する。
  各候補は個別に scope を持ち、1つずつ PermissionGate 経由でアクセス可否を
  判定する。許可されていない候補は黙ってスキップし(エラーにしない)、
  「今回はこの情報が許可されていないので使っていません」という事実を
  used_scopes / skipped_scopes として回答に添えて返す。
  → ユーザーは常に「AIが今回何を見て、何を見なかったか」を確認できる
    (「AIが全部知っている」状態を避け、検証可能なプライバシーを担保する)。
- confidence(memory由来の場合)はそのままLLMに渡すのではなく、プロンプト側で
  "ai_inferred" は「推測」、"user_confirmed" は「確定事実」と明示させ、
  LLMが推測を事実であるかのように断定しないよう促す(prompts/guards.py の
  ハルシネーション防止ガードと組み合わせる想定)。
- 実際のLLM呼び出し(ローカルOllama優先・外部APIはオプトイン)は
  local_ai_core.llm.LLMRouter の責務であり、このモジュールは呼び出し方法を
  知らない。呼び出し側(各アプリ/gateway)が llm_call として注入する。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from ..schema.migrate import db_session
from ..permissions.gate import PermissionGate, PermissionDenied

LlmCall = Callable[[str, dict], str]


@dataclass
class ContextSource:
    """アシスタントが参照したい可能性のあるデータの1候補。

    scope: PermissionGateに渡すスコープ文字列(例: "memory:read:career.*")
    label: ユーザー向けの説明(例: "就活の自己分析メモ")
    fetch: 実際に許可された場合だけ呼ばれる、データ取得関数
    """
    scope: str
    label: str
    fetch: Callable[[], object]


@dataclass
class AssistantAnswer:
    text: str
    used_scopes: list = field(default_factory=list)     # 実際に参照できた候補
    skipped_scopes: list = field(default_factory=list)  # 未許可でスキップした候補


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class AssistantOrchestrator:
    def __init__(self, db_path: str = "core.db", gate: Optional[PermissionGate] = None):
        self.db_path = db_path
        self.gate = gate or PermissionGate(db_path)

    def ask(
        self,
        profile_id: int,
        app_key: str,
        question: str,
        candidate_sources: list[ContextSource],
        llm_call: LlmCall,
    ) -> AssistantAnswer:
        """質問に答える。candidate_sourcesのうち許可されたものだけを集めて
        llm_callに渡し、その透明性の記録(used/skipped)を会話履歴に残す。
        """
        context: dict = {}
        used: list[str] = []
        skipped: list[str] = []

        for source in candidate_sources:
            try:
                self.gate.require(profile_id, app_key, source.scope)
            except PermissionDenied:
                skipped.append(source.scope)
                continue
            context[source.label] = source.fetch()
            used.append(source.scope)

        answer_text = llm_call(question, context)

        self._record_session(profile_id, question, used)

        return AssistantAnswer(text=answer_text, used_scopes=used, skipped_scopes=skipped)

    def _record_session(self, profile_id: int, question: str, used_scopes: list[str]) -> None:
        # 質問文そのものは保存しない(会話ログの蓄積はDocument Center/各アプリ側の責務であり、
        # ここでの目的は「何を参照したか」の透明性の記録に限定するため)。
        title = (question[:40] + "…") if len(question) > 40 else question
        with db_session(self.db_path) as conn:
            conn.execute(
                "INSERT INTO assistant_sessions (profile_id, title, used_scopes_json) "
                "VALUES (?, ?, ?)",
                (profile_id, title, json.dumps(used_scopes, ensure_ascii=False)),
            )

    def recent_sessions(self, profile_id: int, limit: int = 20) -> list[dict]:
        with db_session(self.db_path) as conn:
            rows = conn.execute(
                "SELECT title, used_scopes_json, created_at FROM assistant_sessions "
                "WHERE profile_id = ? ORDER BY created_at DESC LIMIT ?",
                (profile_id, limit),
            ).fetchall()
        return [
            {
                "title": row["title"],
                "used_scopes": json.loads(row["used_scopes_json"] or "[]"),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

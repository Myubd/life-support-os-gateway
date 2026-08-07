"""
automation/engine.py
---------------------
ルールの"実行"を担う。このエコシステムの大原則(「AIが全部知っている」を避ける)を
オートメーションでも徹底するため、実行の手順は必ず以下の順序を守る:

  1. ルールの required_scopes を1つずつ PermissionGate.require() に通す。
     どれか1つでも未許可なら、その場でPermissionDeniedを送出し、
     automation_runs に status="denied" を記録して終了する(部分実行はしない)。
  2. すべて許可されている場合のみ、呼び出し側が渡した context_provider を使って
     必要なデータを集める(context_providerの内部実装はengineの関知しない
     ところ — つまりMemoryStore/ScheduleStore/KnowledgeStore/DocumentStore等の
     具体的な読み出しは呼び出し側アプリ/gatewayが用意する)。
  3. suggestion_fn(action_type, action_config, context) で提案文を生成する。
     この関数もengineの外(local_ai_core.llmを使うかどうかも含め)で定義する。
     engine自体はLLM呼び出しに直接依存しない(テスト容易性・関心の分離のため)。
  4. 実行結果を automation_runs に記録する。result_summaryは長文を保存せず、
     監査目的で十分な長さ(既定120文字)に切り詰める。

「自動で何かを外部に実行する」(メール送信・決済など)機能はこの最初の
バージョンでは提供しない。action_typeは "suggest" のみを想定している。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from ..permissions.gate import PermissionGate, PermissionDenied
from .store import AutomationRule, AutomationStore

ContextProvider = Callable[[str, dict], dict]
SuggestionFn = Callable[[str, dict, dict], str]

_SUMMARY_MAX_LEN = 120


@dataclass
class AutomationResult:
    status: str  # "ok" | "denied" | "error"
    suggestion: Optional[str] = None
    denied_scope: Optional[str] = None
    error: Optional[str] = None


class AutomationEngine:
    def __init__(
        self,
        db_path: str = "core.db",
        gate: Optional[PermissionGate] = None,
        store: Optional[AutomationStore] = None,
    ):
        self.db_path = db_path
        self.gate = gate or PermissionGate(db_path)
        self.store = store or AutomationStore(db_path)

    def run_rule(
        self,
        profile_id: int,
        rule: AutomationRule,
        context_provider: ContextProvider,
        suggestion_fn: SuggestionFn,
    ) -> AutomationResult:
        # 1. 必要なスコープを1つずつ確認する。1つでも拒否されたら即座に停止する
        #    (「一部のデータだけ使って動く」ことを許さない = 挙動の予測可能性を優先)。
        for scope in rule.required_scopes:
            try:
                self.gate.require(profile_id, rule.owner_app, scope)
            except PermissionDenied:
                self.store.record_run(profile_id, rule.id, status="denied",
                                       result_summary=f"未許可のスコープ: {scope}")
                return AutomationResult(status="denied", denied_scope=scope)

        # 2〜3. 許可された範囲でコンテキストを集め、提案を生成する
        try:
            context = context_provider(rule.trigger_type, rule.trigger_config)
            suggestion = suggestion_fn(rule.action_type, rule.action_config, context)
        except Exception as exc:  # noqa: BLE001 - 実行時エラーも監査ログに残す
            self.store.record_run(profile_id, rule.id, status="error",
                                   result_summary=str(exc)[:_SUMMARY_MAX_LEN])
            return AutomationResult(status="error", error=str(exc))

        # 4. 実行結果を記録する(全文ではなく要約のみ。access_logと同じ設計思想)
        summary = suggestion[:_SUMMARY_MAX_LEN] if suggestion else None
        self.store.record_run(profile_id, rule.id, status="ok", result_summary=summary)
        return AutomationResult(status="ok", suggestion=suggestion)

    def run_all_enabled(
        self,
        profile_id: int,
        context_provider: ContextProvider,
        suggestion_fn: SuggestionFn,
    ) -> dict[int, AutomationResult]:
        """有効なルールをすべて実行する(定期実行/手動実行の両方から呼ばれる想定)。"""
        results: dict[int, AutomationResult] = {}
        for rule in self.store.list_enabled(profile_id):
            results[rule.id] = self.run_rule(profile_id, rule, context_provider, suggestion_fn)
        return results

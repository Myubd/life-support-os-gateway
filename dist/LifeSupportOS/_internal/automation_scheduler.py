"""
automation_scheduler.py
------------------------
gatewayプロセスの中で AutomationEngine.run_all_enabled を定期的に呼び出すための
薄いラッパー。local_ai_core 本体は変更しない(engine.py の呼び出し規約 —
context_provider/suggestion_fn は呼び出し側が用意する — にそのまま従う)。

設計上の注意(README等にも書いておくこと):
- AutomationEngine.run_rule() は context_provider(trigger_type, trigger_config) しか
  呼ばないため、"どのアプリが作ったルールか"(rule.owner_app)を context_provider 側では
  区別できない。そのため実際のデータ取得は常に gateway 自身のアプリキー
  (SELF_APP_KEY="life_support_os")の権限で行う。
  つまり自動実行が機能するには、
    1. ルールを作ったアプリ(例: archlife)が rule.required_scopes を許可されていること
    2. gateway自身(life_support_os)が同じデータへの読み取りスコープを許可されていること
  の両方をユーザーが権限台帳(static/index.html の「01 権限」)で許可している必要がある。
  plugin_manifest.json が life_support_os 名義で schedule_items:read 等を
  申告しているのはこのため。

- suggestion_fn は AutomationEngine の型定義上「同期関数」(async ではない)。
  一方 LLMRouter.chat() は非同期APIしか持たない。この2つを橋渡しするため、
  run_all_enabled 自体を asyncio.to_thread() で別スレッドに逃がし、
  そのスレッドの中でだけ asyncio.run() を使って suggestion_fn 内から
  LLMRouter.chat() を呼ぶ(すでに動いているイベントループを持つメインスレッドで
  asyncio.run()するとエラーになるため、これを避ける唯一安全な方法)。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from local_ai_core.automation import AutomationEngine
from local_ai_core.schedule import ScheduleStore
from local_ai_core.llm import AiSettings, ChatMessage, LLMRouter
from local_ai_core.prompts import PromptRegistry, PromptTemplate, guards

logger = logging.getLogger("life_support_os_gateway.automation_scheduler")

SELF_APP_KEY = "life_support_os"

# ---------------------------------------------------------------------------
# プロンプトテンプレート登録
# (自動化提案は既定でローカルLLMのみを使う。AiSettings() の既定値が
#  allow_external_api=False であることに意図的に依存しており、
#  「自動で外部APIに投げる」ことを絶対に起こさないための安全策。)
# ---------------------------------------------------------------------------
_registry = PromptRegistry()
_registry.register(PromptTemplate(
    key="automation_suggest",
    system_prompt=guards.build_system_prompt(
        "あなたはライフサポートOSのオートメーション機能です。"
        "渡されたデータだけをもとに、ユーザーへの短い提案文を1つ作ってください。",
        guards.NO_FABRICATION_GUARD,
        guards.JAPANESE_OUTPUT_GUARD,
        guards.MEMORY_CONFIDENCE_GUARD,
    ),
))


# ---------------------------------------------------------------------------
# context_provider: trigger_type ごとに必要な最小限のデータだけを集める
# ---------------------------------------------------------------------------
def _make_context_provider(db_path: str, profile_id: int) -> Callable[[str, dict], dict]:
    schedule_store = ScheduleStore(db_path)

    def context_provider(trigger_type: str, trigger_config: dict) -> dict:
        if trigger_type == "schedule_due_soon":
            days_before = int(trigger_config.get("days_before", 3))
            items = schedule_store.list_open(profile_id, SELF_APP_KEY)
            # due_at は ISO8601 文字列。厳密な日付比較はDB側でなく取得後にPythonで行う
            # (schedule_items のタイムゾーン仕様がアプリ間でまだ揺れているため、
            #  ここでは「文字列としての日付部分」だけを比較する簡易実装に留める)。
            from datetime import datetime, timedelta
            cutoff = (datetime.now() + timedelta(days=days_before)).strftime("%Y-%m-%d")
            due_soon = [
                item for item in items
                if item.due_at and item.due_at[:10] <= cutoff
            ]
            return {
                "trigger_type": trigger_type,
                "due_soon_items": [
                    {"title": i.title, "due_at": i.due_at, "source_app": i.source_app}
                    for i in due_soon
                ],
            }

        # 未対応の trigger_type は空コンテキストを返す(提案は「材料なし」として
        # AutomationEngine 側で自然に空提案・エラーにはしない)。
        logger.warning("未対応のtrigger_typeです(空コンテキストを返します): %s", trigger_type)
        return {"trigger_type": trigger_type}

    return context_provider


# ---------------------------------------------------------------------------
# suggestion_fn: LLMRouter は非同期しか提供しないため、別スレッド内で
# asyncio.run() して同期I/Fに変換する
# ---------------------------------------------------------------------------
def _make_suggestion_fn(llm_router: LLMRouter) -> Callable[[str, dict, dict], str]:
    async def _call_llm(context: dict) -> str:
        system_prompt, user_prompt = _registry.render("automation_suggest", payload=context)
        response = await llm_router.chat(
            [
                ChatMessage(role="system", content=system_prompt),
                ChatMessage(role="user", content=user_prompt),
            ],
            settings=AiSettings(),  # 既定値のまま = 外部APIは使わない(オプトイン制)
        )
        return response.content

    def suggestion_fn(action_type: str, action_config: dict, context: dict) -> str:
        if action_type != "suggest":
            raise ValueError(f"未対応のaction_typeです: {action_type}")
        if not context.get("due_soon_items") and context.get("trigger_type") == "schedule_due_soon":
            return "直近で提案すべき締切はありませんでした。"
        # このsuggestion_fnは別スレッド(asyncio.to_threadの中)からしか
        # 呼ばれない前提。メインのイベントループと衝突しないよう、
        # ここで新しいイベントループを作って完結させる。
        return asyncio.run(_call_llm(context))

    return suggestion_fn


# ---------------------------------------------------------------------------
# スケジューラ本体
# ---------------------------------------------------------------------------
def build_automation_scheduler(
    *,
    db_path: str,
    profile_id_getter: Callable[[], Optional[int]],
    llm_router: LLMRouter,
    interval_seconds: int,
) -> AsyncIOScheduler:
    """gatewayのlifespanから呼ぶ。scheduler.start()は呼び出し側で行う。"""
    scheduler = AsyncIOScheduler()
    engine = AutomationEngine(db_path=db_path)
    suggestion_fn = _make_suggestion_fn(llm_router)

    async def _poll() -> None:
        profile_id = profile_id_getter()
        if profile_id is None:
            logger.warning("profile_id未確定のため今回のオートメーション実行をスキップします")
            return
        context_provider = _make_context_provider(db_path, profile_id)
        try:
            # run_all_enabled は同期関数(内部でsuggestion_fn=asyncio.run(...)を
            # 呼ぶため、既存のイベントループと衝突しないよう別スレッドで実行する)。
            results = await asyncio.to_thread(
                engine.run_all_enabled, profile_id, context_provider, suggestion_fn
            )
        except Exception:
            logger.exception("オートメーション定期実行でエラーが発生しました")
            return
        for rule_id, result in results.items():
            logger.info("automation rule=%s status=%s", rule_id, result.status)

    scheduler.add_job(
        _poll,
        "interval",
        seconds=interval_seconds,
        id="automation_poll",
        max_instances=1,  # 前回の実行が長引いても多重起動しない
        coalesce=True,
    )
    return scheduler

"""
api/router.py
-------------
local_ai_core を HTTP 越しに使いたいアプリ(Node.js製のArchlifeフロントエンド、
将来のデスクトップアシスタント、gateway等)向けの共通FastAPIルーター。

このルーターが「唯一のインターフェース」になることを意図している:
- 各アプリのUIは、直接SQLiteファイルを読み書きするのではなく、必ずこの
  ルーター(を含むFastAPIアプリ)を経由して permission / memory / documents /
  schedule / knowledge / automation / assistant にアクセスする。
- 権限チェックはここでは重複実装しない。すべて各ストア/PermissionGateの
  require() に委譲しており、このルーターはHTTPの皮をかぶせるだけ。
  つまりHTTP層をスキップしてスコープを回避することはできない
  (どのアプリも同じ core.db ・同じ PermissionGate を経由する)。
- 「未許可」は書き込み/参照とも403を返し、フロント側で
  「この機能を使うには許可が必要です」という穏当なUIに変換することを想定する。

使い方(gatewayや各アプリのFastAPI起動時):

    from fastapi import FastAPI
    from local_ai_core.api import build_core_router
    from local_ai_core.llm import LLMRouter, OllamaProvider, ClaudeProvider, OpenAIProvider

    llm_router = LLMRouter(local=OllamaProvider(), external={...})
    app = FastAPI()
    app.include_router(build_core_router(db_path="core.db", llm_router=llm_router))

注意: Pydanticモデルはすべてモジュールトップレベルで定義すること。
FastAPIは型ヒントの解決に関数の`__globals__`(=モジュールの名前空間)を使うため、
関数内で定義したローカルクラスをリクエストボディの型として使うと解決に失敗し、
「クエリパラメータ」として誤って扱われてしまう(実際にこの実装で踏んだ罠)。
"""
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..permissions import PermissionGate, PermissionDenied
from ..memory import MemoryStore
from ..knowledge import KnowledgeStore
from ..schedule import ScheduleStore
from ..documents import DocumentStore
from ..search import SearchStore
from ..automation import AutomationStore, AutomationEngine
from ..assistant import AssistantOrchestrator, ContextSource
from ..llm import ChatMessage, AiSettings, LLMRouter


# ---------------------------------------------------------------------------
# リクエストボディ(モジュールトップレベルで定義。上記の注意を参照)
# ---------------------------------------------------------------------------

class GrantBody(BaseModel):
    profile_id: int
    app_key: str
    scope: str
    expires_at: Optional[str] = None


class RevokeBody(BaseModel):
    profile_id: int
    app_key: str
    scope: str


class MemorySetBody(BaseModel):
    profile_id: int
    app_key: str
    key: str
    value: object
    confidence: str = "ai_inferred"


class DocumentRegisterBody(BaseModel):
    profile_id: int
    app_key: str
    file_path: str
    title: str
    source_ref_id: Optional[str] = None
    category: Optional[str] = None
    file_hash: Optional[str] = None
    mime_type: Optional[str] = None
    tags: Optional[list] = None


class RuleCreateBody(BaseModel):
    profile_id: int
    owner_app: str
    name: str
    trigger_type: str
    action_type: str = "suggest"
    trigger_config: Optional[dict] = None
    action_config: Optional[dict] = None
    required_scopes: Optional[list] = None


class AssistantAskBody(BaseModel):
    profile_id: int
    app_key: str
    question: str
    sources: list = []  # 例: ["memory:career", "schedule", "knowledge", "documents"]
    use_external: bool = False
    external_provider: str = "claude"


def build_core_router(
    db_path: str,
    llm_router: Optional[LLMRouter] = None,
    prefix: str = "/core",
) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["local-ai-core"])

    gate = PermissionGate(db_path)
    memory = MemoryStore(db_path, gate=gate)
    knowledge = KnowledgeStore(db_path, gate=gate)
    schedule = ScheduleStore(db_path, gate=gate)
    documents = DocumentStore(db_path, gate=gate)
    search = SearchStore(db_path, gate=gate)
    automation_store = AutomationStore(db_path)
    automation_engine = AutomationEngine(db_path, gate=gate, store=automation_store)  # noqa: F841 (非LLM自動化の将来利用向けに保持)
    assistant = AssistantOrchestrator(db_path, gate=gate)

    # -----------------------------------------------------------------
    # permissions: ユーザー向け「許可を求められています」/「許可済み」画面
    # -----------------------------------------------------------------
    @router.get("/permissions/pending")
    def get_pending(profile_id: int):
        return gate.pending_requests(profile_id)

    @router.get("/permissions/grants")
    def get_grants(profile_id: int):
        return [g.__dict__ for g in gate.list_grants(profile_id)]

    @router.post("/permissions/grant")
    def post_grant(body: GrantBody):
        try:
            gate.grant(body.profile_id, body.app_key, body.scope, body.expires_at)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True}

    @router.post("/permissions/revoke")
    def post_revoke(body: RevokeBody):
        gate.revoke(body.profile_id, body.app_key, body.scope)
        return {"ok": True}

    # -----------------------------------------------------------------
    # memory
    # -----------------------------------------------------------------
    @router.post("/memory")
    def post_memory(body: MemorySetBody):
        try:
            memory.set(body.profile_id, body.app_key, body.key, body.value, body.confidence)
        except PermissionDenied as e:
            raise HTTPException(status_code=403, detail=str(e))
        return {"ok": True}

    @router.get("/memory/{key}")
    def get_memory(key: str, profile_id: int, app_key: str):
        try:
            item = memory.get(profile_id, app_key, key)
        except PermissionDenied as e:
            raise HTTPException(status_code=403, detail=str(e))
        return item.__dict__ if item else None

    @router.get("/memory")
    def list_memory(profile_id: int, app_key: str, prefix: str, only_confirmed: bool = False):
        try:
            items = memory.list_by_prefix(profile_id, app_key, prefix, only_confirmed)
        except PermissionDenied as e:
            raise HTTPException(status_code=403, detail=str(e))
        return [i.__dict__ for i in items]

    @router.delete("/memory/{key}")
    def delete_memory(key: str, profile_id: int, app_key: str):
        try:
            memory.forget(profile_id, app_key, key)
        except PermissionDenied as e:
            raise HTTPException(status_code=403, detail=str(e))
        return {"ok": True}

    # -----------------------------------------------------------------
    # documents (Document Center)
    # -----------------------------------------------------------------
    @router.post("/documents")
    def post_document(body: DocumentRegisterBody):
        try:
            doc_id = documents.register(
                body.profile_id, body.app_key, body.file_path, body.title,
                body.source_ref_id, body.category, body.file_hash, body.mime_type, body.tags,
            )
        except PermissionDenied as e:
            raise HTTPException(status_code=403, detail=str(e))
        return {"id": doc_id}

    @router.get("/documents")
    def list_documents(profile_id: int, app_key: str, category: Optional[str] = None):
        try:
            items = documents.list_active(profile_id, app_key, category)
        except PermissionDenied as e:
            raise HTTPException(status_code=403, detail=str(e))
        return [i.__dict__ for i in items]

    @router.delete("/documents/{document_id}")
    def delete_document(document_id: int, profile_id: int, app_key: str):
        try:
            documents.deactivate(profile_id, app_key, document_id)
        except PermissionDenied as e:
            raise HTTPException(status_code=403, detail=str(e))
        return {"ok": True}

    # -----------------------------------------------------------------
    # schedule / knowledge (横断参照。既に各アプリのcore_syncが書き込み済みの前提)
    # -----------------------------------------------------------------
    @router.get("/schedule")
    def list_schedule(profile_id: int, app_key: str):
        try:
            items = schedule.list_open(profile_id, app_key)
        except PermissionDenied as e:
            raise HTTPException(status_code=403, detail=str(e))
        return [i.__dict__ for i in items]

    @router.get("/knowledge")
    def list_knowledge(profile_id: int, app_key: str, category: Optional[str] = None):
        try:
            items = knowledge.list_active(profile_id, app_key, category)
        except PermissionDenied as e:
            raise HTTPException(status_code=403, detail=str(e))
        return [i.__dict__ for i in items]

    # -----------------------------------------------------------------
    # search(knowledge_items / documents の横断全文検索)
    # -----------------------------------------------------------------
    @router.get("/search")
    def search_all(profile_id: int, app_key: str, q: str, limit: int = 20):
        """knowledge_items / documents を横断検索する。
        新しいスコープは作らず、既存の knowledge_items:read / documents:read を
        流用する。片方だけ許可されていれば、許可されている方だけを返す
        (両方とも未許可の場合のみ403)。
        """
        try:
            hits = search.search(profile_id, app_key, q, limit)
        except PermissionDenied as e:
            raise HTTPException(status_code=403, detail=str(e))
        return [h.__dict__ for h in hits]

    # -----------------------------------------------------------------
    # automation
    # -----------------------------------------------------------------
    @router.post("/automation/rules")
    def create_rule(body: RuleCreateBody):
        rule_id = automation_store.create(
            body.profile_id, body.owner_app, body.name, body.trigger_type, body.action_type,
            body.trigger_config, body.action_config, body.required_scopes,
        )
        return {"id": rule_id}

    @router.get("/automation/rules")
    def list_rules(profile_id: int):
        return [r.__dict__ for r in automation_store.list_all(profile_id)]

    @router.post("/automation/rules/{rule_id}/enable")
    def enable_rule(rule_id: int, profile_id: int, enabled: bool = True):
        automation_store.set_enabled(profile_id, rule_id, enabled)
        return {"ok": True}

    @router.delete("/automation/rules/{rule_id}")
    def delete_rule(rule_id: int, profile_id: int):
        automation_store.delete(profile_id, rule_id)
        return {"ok": True}

    @router.post("/automation/rules/{rule_id}/run")
    async def run_rule(rule_id: int, profile_id: int):
        """ルールを1件だけ手動実行する。トリガー種別ごとのコンテキスト収集は
        現状 schedule_due_soon のみの簡易実装。本格的な定期実行(スケジューラ)は
        gateway側で行う想定(このエンドポイントを定期的に叩くだけでよい)。

        手順は automation/engine.py の同期版と完全に同一(スコープ確認→
        コンテキスト収集→提案生成→記録)だが、LLM呼び出しがasyncのため
        ここでは直接非同期の手順として実装している。
        """
        rule = automation_store.get(profile_id, rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="rule not found")

        for scope in rule.required_scopes:
            try:
                gate.require(profile_id, rule.owner_app, scope)
            except PermissionDenied:
                automation_store.record_run(profile_id, rule.id, status="denied",
                                             result_summary=f"未許可のスコープ: {scope}")
                return {"status": "denied", "denied_scope": scope}

        if rule.trigger_type == "schedule_due_soon":
            items = schedule.list_open(profile_id, rule.owner_app)
            context = {"open_items": [i.__dict__ for i in items]}
        else:
            context = {}

        if llm_router is None:
            suggestion = "(LLMが設定されていないため提案は生成されませんでした)"
        else:
            try:
                messages = [
                    ChatMessage(
                        role="system",
                        content="あなたはライフ管理アシスタントです。与えられたデータをもとに、"
                                "短い提案を1〜2文で日本語で述べてください。",
                    ),
                    ChatMessage(role="user", content=str(context)),
                ]
                response = await llm_router.chat(messages, settings=AiSettings())
                suggestion = response.content
            except Exception as exc:  # noqa: BLE001
                automation_store.record_run(profile_id, rule.id, status="error",
                                             result_summary=str(exc)[:120])
                return {"status": "error", "error": str(exc)}

        automation_store.record_run(profile_id, rule.id, status="ok",
                                     result_summary=suggestion[:120])
        return {"status": "ok", "suggestion": suggestion}

    # -----------------------------------------------------------------
    # assistant
    # -----------------------------------------------------------------
    @router.post("/assistant/ask")
    async def assistant_ask(body: AssistantAskBody):
        if llm_router is None:
            raise HTTPException(status_code=503, detail="LLMが設定されていません")

        candidate_sources = []
        for src in body.sources:
            if src == "schedule":
                candidate_sources.append(ContextSource(
                    scope="schedule_items:read", label="予定・タスク",
                    fetch=lambda: [i.__dict__ for i in schedule.list_open(body.profile_id, body.app_key)],
                ))
            elif src == "knowledge":
                candidate_sources.append(ContextSource(
                    scope="knowledge_items:read", label="ナレッジ資料",
                    fetch=lambda: [i.__dict__ for i in knowledge.list_active(body.profile_id, body.app_key)],
                ))
            elif src == "documents":
                candidate_sources.append(ContextSource(
                    scope="documents:read", label="ドキュメント",
                    fetch=lambda: [i.__dict__ for i in documents.list_active(body.profile_id, body.app_key)],
                ))
            elif src.startswith("memory:"):
                mem_prefix = src.split(":", 1)[1]
                candidate_sources.append(ContextSource(
                    scope=f"memory:read:{mem_prefix}.*", label=f"メモリー({mem_prefix})",
                    fetch=lambda p=mem_prefix: [
                        i.__dict__ for i in memory.list_by_prefix(body.profile_id, body.app_key, p)
                    ],
                ))

        # 許可されたソースだけを集める(未許可はスキップし、その事実を返す)。
        # AssistantOrchestrator.ask は同期実装のため、ここではLLM呼び出し部分
        # だけを非同期に差し替えた同等の手順を直接実行する。
        used, skipped, context = [], [], {}
        for source in candidate_sources:
            try:
                gate.require(body.profile_id, body.app_key, source.scope)
            except PermissionDenied:
                skipped.append(source.scope)
                continue
            context[source.label] = source.fetch()
            used.append(source.scope)

        settings = AiSettings(allow_external_api=body.use_external, external_provider=body.external_provider)
        messages = [
            ChatMessage(
                role="system",
                content=(
                    "あなたはユーザー専属のプライベートAIアシスタントです。"
                    "与えられたcontextの範囲でのみ回答し、contextにない情報を"
                    "断定的に語らないでください。'ai_inferred'とマークされた情報は"
                    "推測であることを踏まえて回答してください。"
                ),
            ),
            ChatMessage(role="user", content=f"質問: {body.question}\ncontext: {context}"),
        ]
        response = await llm_router.chat(messages, settings=settings)

        # 透明性の記録(このアシスタントが今回何を参照したか)
        assistant._record_session(body.profile_id, body.question, used)  # noqa: SLF001

        return {"text": response.content, "used_scopes": used, "skipped_scopes": skipped}

    @router.get("/assistant/sessions")
    def assistant_sessions(profile_id: int):
        return assistant.recent_sessions(profile_id)

    return router

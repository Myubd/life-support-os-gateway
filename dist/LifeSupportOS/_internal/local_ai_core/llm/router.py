"""
llm/router.py
-------------
「既定はローカルLLM、ユーザーが明示的に許可した時だけ外部APIを使う」という
このエコシステム全体のポリシーを一元管理するルーター。

Archlife の `ai_settings` テーブル(`allow_external_api` / `external_provider`)と
`/api/ai/analyze` の分岐ロジックを、アプリ非依存の形に抽出したもの。
すべてのアプリはこのルーター経由でLLMを呼び出すことで、
「勝手に外部へ送信しない」という保証をアプリ実装者が個別に気にしなくてよくなる。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .base import ChatMessage, ChatResponse, LLMProvider, LLMProviderError


@dataclass
class AiSettings:
    """ユーザーごと(デバイスごと)のAI利用設定。Archlifeの ai_settings テーブル相当。"""

    allow_external_api: bool = False
    external_provider: str = "claude"  # "claude" | "openai"


class LLMRouter:
    def __init__(self, local: LLMProvider, external: dict[str, LLMProvider] | None = None):
        self.local = local
        self.external = external or {}

    async def status(self) -> dict:
        """Archlife `/api/ai/status` 相当。各プロバイダーの利用可否を返す。"""
        local_available = await self.local.is_available()
        external_status = {name: await p.is_available() for name, p in self.external.items()}
        return {
            "local": {"available": local_available, "models": self.local.list_models()},
            "external": external_status,
        }

    async def chat(
        self,
        messages: list[ChatMessage],
        settings: AiSettings | None = None,
        force_external: str | None = None,
        **kwargs,
    ) -> ChatResponse:
        """settings.allow_external_api が False の場合、force_external を指定しても
        必ずローカルにフォールバックする(呼び出し元のバグでプライバシー原則が
        破られないようにするための安全側の実装)。
        """
        settings = settings or AiSettings()
        use_external = force_external if (settings.allow_external_api and force_external) else None
        if not use_external and settings.allow_external_api and settings.external_provider:
            use_external = settings.external_provider

        if use_external:
            provider = self.external.get(use_external)
            if provider is None:
                raise LLMProviderError(f"未対応の外部プロバイダーです: {use_external}")
            try:
                return await provider.chat(messages, **kwargs)
            except LLMProviderError:
                # 外部が失敗してもローカルにフォールバックし、機能自体は止めない
                pass

        return await self.local.chat(messages, **kwargs)

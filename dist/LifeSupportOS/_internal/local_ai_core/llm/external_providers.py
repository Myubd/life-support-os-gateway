"""
llm/external_providers.py
--------------------------
外部API(Claude/OpenAI)プロバイダー。

Archlife `server.js` の `callClaude` / `callOpenAI` を一般化したもの。
**このエコシステムの原則により、これらのプロバイダーはユーザーが明示的に
オプトインした場合にのみ `LLMRouter` から呼び出される。** 個々のアプリが
直接インスタンス化して常用することは想定しない。
"""
from __future__ import annotations

import os
from typing import AsyncIterator

import httpx

from .base import ChatMessage, ChatResponse, LLMProvider, LLMProviderError


class ClaudeProvider(LLMProvider):
    name = "claude"

    def __init__(self, api_key: str | None = None, model: str = "claude-sonnet-4-6"):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.model = model

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=None, format: str | None = None) -> ChatResponse:
        if not self.api_key:
            raise LLMProviderError("ANTHROPIC_API_KEY が設定されていません")
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": model or self.model,
                    "max_tokens": max_tokens or 1000,
                    "messages": [{"role": m.role, "content": m.content} for m in messages if m.role != "system"],
                },
            )
        if r.status_code != 200:
            raise LLMProviderError(f"Claude APIエラー (status {r.status_code})")
        data = r.json()
        text = "\n".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        if not text:
            raise LLMProviderError("Claudeから応答がありませんでした")
        return ChatResponse(content=text, model=model or self.model, provider=self.name)

    async def chat_stream(self, messages, model=None, temperature=0.7) -> AsyncIterator[str]:
        # 簡易実装: ストリーミングが必要な場合はSSEパースを追加する。
        resp = await self.chat(messages, model=model, temperature=temperature)
        yield resp.content

    async def embed(self, texts, model=None):
        raise LLMProviderError("Claudeは埋め込み生成に対応していません。ローカルOllamaを使用してください")

    def list_models(self) -> list[str]:
        return [self.model]

    async def is_available(self) -> bool:
        return bool(self.api_key)


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, api_key: str | None = None, model: str = "gpt-4o-mini"):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.model = model

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=None, format: str | None = None) -> ChatResponse:
        if not self.api_key:
            raise LLMProviderError("OPENAI_API_KEY が設定されていません")
        body = {
            "model": model or self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
        }
        if format == "json":
            body["response_format"] = {"type": "json_object"}
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=body,
            )
        if r.status_code != 200:
            raise LLMProviderError(f"OpenAI APIエラー (status {r.status_code})")
        data = r.json()
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content")
        if not text:
            raise LLMProviderError("OpenAIから応答がありませんでした")
        return ChatResponse(content=text, model=model or self.model, provider=self.name)

    async def chat_stream(self, messages, model=None, temperature=0.7) -> AsyncIterator[str]:
        resp = await self.chat(messages, model=model, temperature=temperature)
        yield resp.content

    async def embed(self, texts, model=None):
        if not self.api_key:
            raise LLMProviderError("OPENAI_API_KEY が設定されていません")
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": model or "text-embedding-3-small", "input": texts},
            )
        if r.status_code != 200:
            raise LLMProviderError(f"OpenAI埋め込みエラー (status {r.status_code})")
        data = r.json()
        return [item["embedding"] for item in data.get("data", [])]

    def list_models(self) -> list[str]:
        return [self.model]

    async def is_available(self) -> bool:
        return bool(self.api_key)

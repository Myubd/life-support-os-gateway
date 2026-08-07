"""
llm/ollama_provider.py
-----------------------
ローカルOllamaを叩く具象プロバイダー。

interview_app の `llm/ollama_provider.py` と Archlife `server.js` の
`callLocalQwen` / `normalizeOllamaBase` を統合した実装。
「サーバー(このプロセス)は個人データを一切外部送信しない」ことが前提の、
このエコシステムの既定プロバイダー。
"""
from __future__ import annotations

import os
from typing import AsyncIterator

import httpx

from .base import ChatMessage, ChatResponse, LLMProvider, LLMProviderError


def normalize_ollama_base(url: str | None) -> str:
    """Archlife server.js の normalizeOllamaBase と同じ発想:
    "host:port" 形式(スキームなし)でもそのまま使えるように補正する。
    """
    if not url:
        return "http://localhost:11434"
    return url if url.startswith("http://") or url.startswith("https://") else f"http://{url}"


class OllamaProvider(LLMProvider):
    name = "local"

    def __init__(
        self,
        base_url: str | None = None,
        model: str = "qwen3:8b",
        embed_model: str = "nomic-embed-text",
        timeout: float = 120.0,
    ):
        self.base_url = normalize_ollama_base(base_url or os.environ.get("OLLAMA_URL"))
        self.model = model
        self.embed_model = embed_model
        self.timeout = timeout

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        format: str | None = None,
    ) -> ChatResponse:
        payload = {
            "model": model or self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            "options": {"temperature": temperature},
        }
        if format:
            payload["format"] = format
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                r = await client.post(f"{self.base_url}/api/chat", json=payload)
            except httpx.RequestError as e:
                raise LLMProviderError(f"ローカルLLMに接続できませんでした: {e}") from e
        if r.status_code != 200:
            raise LLMProviderError(f"ローカルLLMエラー (status {r.status_code})")
        data = r.json()
        content = (data.get("message") or {}).get("content")
        if not content:
            raise LLMProviderError("ローカルLLMから応答がありませんでした")
        return ChatResponse(content=content, model=payload["model"], provider=self.name)

    def chat_sync(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        format: str | None = None,
    ) -> ChatResponse:
        """`chat()` の同期版。rag/utils.py のような同期コードから直接呼べるようにする
        ためのエスケープハッチ(既存コードを非同期化する大改修を避けるための現実的な選択)。
        """
        payload = {
            "model": model or self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            "options": {"temperature": temperature},
        }
        if format:
            payload["format"] = format
        with httpx.Client(timeout=self.timeout) as client:
            try:
                r = client.post(f"{self.base_url}/api/chat", json=payload)
            except httpx.RequestError as e:
                raise LLMProviderError(f"ローカルLLMに接続できませんでした: {e}") from e
        if r.status_code != 200:
            raise LLMProviderError(f"ローカルLLMエラー (status {r.status_code})")
        data = r.json()
        content = (data.get("message") or {}).get("content")
        if not content:
            raise LLMProviderError("ローカルLLMから応答がありませんでした")
        return ChatResponse(content=content, model=payload["model"], provider=self.name)

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
    ) -> AsyncIterator[str]:
        payload = {
            "model": model or self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": True,
            "options": {"temperature": temperature},
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream("POST", f"{self.base_url}/api/chat", json=payload) as r:
                if r.status_code != 200:
                    raise LLMProviderError(f"ローカルLLMエラー (status {r.status_code})")
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    import json

                    chunk = json.loads(line)
                    piece = (chunk.get("message") or {}).get("content")
                    if piece:
                        yield piece

    async def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        vectors: list[list[float]] = []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for text in texts:
                r = await client.post(
                    f"{self.base_url}/api/embeddings",
                    json={"model": model or self.embed_model, "prompt": text},
                )
                if r.status_code != 200:
                    raise LLMProviderError(f"埋め込み生成エラー (status {r.status_code})")
                vectors.append(r.json().get("embedding", []))
        return vectors

    def embed_sync(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        """`embed()` の同期版。rag/core.py のような同期コードから直接呼べるようにする。"""
        vectors: list[list[float]] = []
        with httpx.Client(timeout=self.timeout) as client:
            for text in texts:
                r = client.post(
                    f"{self.base_url}/api/embeddings",
                    json={"model": model or self.embed_model, "prompt": text},
                )
                if r.status_code != 200:
                    raise LLMProviderError(f"埋め込み生成エラー (status {r.status_code})")
                vectors.append(r.json().get("embedding", []))
        return vectors

    def list_models(self) -> list[str]:
        # 同期的な軽量チェックが必要な箇所(起動時の設定画面など)向け。
        import httpx as _httpx

        try:
            r = _httpx.get(f"{self.base_url}/api/tags", timeout=5.0)
            r.raise_for_status()
            return [m.get("name", "") for m in r.json().get("models", [])]
        except _httpx.HTTPError:
            return []

    async def is_available(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.5) as client:
                r = await client.get(f"{self.base_url}/api/tags")
                return r.status_code == 200
        except httpx.RequestError:
            return False

    def is_running(self) -> bool:
        """`is_available()` の同期版。健康チェックエンドポイント等、
        async化されていない呼び出し元(interview_appの health.py 等)向け。
        """
        import httpx as _httpx

        try:
            r = _httpx.get(f"{self.base_url}/api/tags", timeout=2.5)
            return r.status_code == 200
        except _httpx.HTTPError:
            return False

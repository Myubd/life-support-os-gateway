"""
llm/base.py
-----------
LLMプロバイダーの抽象基底クラス。

interview_app (react-fastapi/backend/llm/base.py) にあった設計をベースに、
特定アプリに依存しない形へ一般化したもの。ローカル(Ollama)・外部(Claude/OpenAI)を
問わず、すべてのプロバイダーはこのインターフェースを実装する。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional


@dataclass
class ChatMessage:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class ChatResponse:
    content: str
    model: str
    provider: str  # "local" | "claude" | "openai" など、呼び出し元での出典表示に使う
    usage: Optional[dict] = field(default=None)  # トークン数等（取れる場合のみ）


class LLMProviderError(RuntimeError):
    """LLM呼び出しに関する共通エラー。呼び出し元はこれだけをcatchすればよい。"""


class LLMProvider(ABC):
    """チャット補完・埋め込みを提供するプロバイダーの基底クラス。"""

    #: サブクラスで上書きする。ログや `ChatResponse.provider` に使う識別子。
    name: str = "unknown"

    @abstractmethod
    async def chat(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        format: str | None = None,
    ) -> ChatResponse:
        """非ストリーミングでテキストを返す。
        `format="json"` を指定すると、対応プロバイダーはJSON出力モードで応答する
        (utils.py の call_ollama_with_json_retry 等、構造化出力が必要な用途向け)。
        """
        ...

    @abstractmethod
    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
    ) -> AsyncIterator[str]:
        """SSE等向けにテキストをチャンク単位で返す。"""
        ...

    @abstractmethod
    async def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        """テキストリストをベクトルに変換する（RAG/ナレッジ基盤で使用）。"""
        ...

    @abstractmethod
    def list_models(self) -> list[str]:
        """利用可能なモデル名のリストを返す。"""
        ...

    @abstractmethod
    async def is_available(self) -> bool:
        """このプロバイダーが現在呼び出し可能かを軽量にチェックする。
        Archlifeの `/api/ai/status` 相当の判定をプロバイダー側に持たせる。
        """
        ...

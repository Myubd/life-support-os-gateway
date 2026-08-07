"""
prompts/templates.py
---------------------
用途別プロンプトテンプレートのレジストリ。

Archlife `server.js` の `buildPrompt`(kind別のテンプレート辞書を1ファイルに
ベタ書きしていた実装)を一般化し、各アプリが起動時に自分のテンプレートを
登録できる形にしたもの。テンプレートはPythonの `str.format` 互換のプレース
ホルダーを使う。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class PromptTemplate:
    key: str
    system_prompt: str
    # {payload_json} プレースホルダーにpayloadのJSON文字列を差し込む
    user_template: str = "{payload_json}"


class PromptRegistry:
    """アプリごとに用途別テンプレートを登録・描画するレジストリ。

    例(Archlifeの `today` テンプレートを移植する場合):
        registry.register(PromptTemplate(
            key="today_priorities",
            system_prompt=build_system_prompt(
                "あなたはライフ管理アプリのアシスタントです。",
                NO_FABRICATION_GUARD, JAPANESE_OUTPUT_GUARD,
            ),
        ))
    """

    def __init__(self):
        self._templates: dict[str, PromptTemplate] = {}

    def register(self, template: PromptTemplate) -> None:
        self._templates[template.key] = template

    def render(self, key: str, payload: dict) -> tuple[str, str]:
        """(system_prompt, user_prompt) のタプルを返す。"""
        template = self._templates.get(key)
        if template is None:
            raise KeyError(f"未登録のプロンプトテンプレートです: {key}")
        user_prompt = template.user_template.format(payload_json=json.dumps(payload, ensure_ascii=False))
        return template.system_prompt, user_prompt

    def keys(self) -> list[str]:
        return list(self._templates.keys())

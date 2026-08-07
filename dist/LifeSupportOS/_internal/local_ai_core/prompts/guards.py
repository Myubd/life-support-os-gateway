"""
prompts/guards.py
------------------
全アプリ共通のプロンプトガード。

interview_app `shared/prompts/guards.py`（ハルシネーション防止ガード）の考え方を、
就活支援に限定しない形で一般化したもの。個々のアプリはここに定義された関数を
組み合わせてプロンプトを組み立てる。
"""
from __future__ import annotations


NO_FABRICATION_GUARD = (
    "重要: 与えられたデータに存在しない情報を推測で補わないでください。"
    "分からない場合は「情報が不足しています」と明記してください。"
)

JAPANESE_OUTPUT_GUARD = "回答は必ず日本語で、前置きなしで出力してください。"

NO_MEDICAL_ADVICE_GUARD = (
    "重要: 医学的な診断や断定的な治療方針の指示はしないでください。"
    "一般的な情報提供にとどめ、必要に応じて専門家への相談を勧めてください。"
)

NO_FINANCIAL_ADVICE_GUARD = (
    "重要: 特定の金融商品の売買を推奨するような断定的な投資助言はしないでください。"
    "記録の振り返りや一般的な傾向の指摘にとどめてください。"
)

MEMORY_CONFIDENCE_GUARD = (
    "重要: 【本人が確認した記憶】として渡された情報は事実として扱ってよいですが、"
    "【AIの推測・未確認】として渡された情報は、あくまで推測であり断定しないでください。"
    "推測情報に基づいて回答する場合は「〜という傾向があるようですが、違っていたら教えてください」"
    "のように、確認を促す言い方をしてください。"
)


def build_system_prompt(role_description: str, *guards: str) -> str:
    """役割説明文と任意個数のガードを結合してシステムプロンプトを組み立てる。

    例:
        build_system_prompt(
            "あなたは家計簿アシスタントです。",
            NO_FABRICATION_GUARD, JAPANESE_OUTPUT_GUARD, NO_FINANCIAL_ADVICE_GUARD,
        )
    """
    parts = [role_description, *guards]
    return "\n".join(p.strip() for p in parts if p.strip())

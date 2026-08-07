"""
plugins/manifest.py
--------------------
各アプリが「自分は何者で、何にアクセスしたいか」を自己申告するマニフェスト。

設計方針:
- アプリを追加するたびにコアのPythonコードを書き換えるのではなく、
  各アプリのリポジトリに置く `plugin_manifest.json` を1枚追加するだけで
  source_apps / permission_scopes に登録できるようにする(プラグイン方式)。
- マニフェストに書けるのは「申告」だけで、実際のアクセス許可(grant)は含まない。
  ユーザーが設定画面で個別に許可するまで、申告されたスコープは一切有効にならない。
  これにより新しいアプリを追加しても、既定では何も見えない状態からスタートする。

マニフェストの例は `plugin_manifest.example.json` を参照。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..schema.migrate import register_source_app
from ..permissions.gate import PermissionGate


@dataclass
class ScopeDeclaration:
    scope: str      # 例: "schedule_items:read", "memory:read:career.*"
    purpose: str    # ユーザーに見せる用途説明。空文字は不可(常に理由を明示させる)


@dataclass
class PluginManifest:
    app_key: str
    display_name: str
    version: str = "0.1.0"
    requested_scopes: list[ScopeDeclaration] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "PluginManifest":
        for required in ("app_key", "display_name"):
            if not data.get(required):
                raise ValueError(f"plugin manifest に必須項目 '{required}' がありません")

        scopes = []
        for raw in data.get("requested_scopes", []):
            scope = raw.get("scope")
            purpose = raw.get("purpose")
            if not scope or ":" not in scope:
                raise ValueError(f"不正なscope形式です: {raw!r} (例: 'resource:action')")
            if not purpose:
                raise ValueError(f"scope '{scope}' に purpose(用途説明)がありません。"
                                  "ユーザーに理由を示せないスコープは申告できません。")
            scopes.append(ScopeDeclaration(scope=scope, purpose=purpose))

        return cls(
            app_key=data["app_key"],
            display_name=data["display_name"],
            version=data.get("version", "0.1.0"),
            requested_scopes=scopes,
        )

    @classmethod
    def load(cls, path: str | Path) -> "PluginManifest":
        text = Path(path).read_text(encoding="utf-8")
        return cls.from_dict(json.loads(text))


def register_plugin(
    db_path: str,
    manifest: PluginManifest,
    gate: PermissionGate | None = None,
) -> None:
    """アプリ起動時に1回呼ぶ。source_appsへの登録と、スコープの申告(permission_scopes)を行う。
    冪等なので、何度呼んでも安全(再起動のたびに呼んで良い)。
    ここではユーザーへの許可付与は行わない — それは別途UIから PermissionGate.grant() で行う。
    """
    register_source_app(db_path, manifest.app_key, manifest.display_name)
    gate = gate or PermissionGate(db_path)
    for decl in manifest.requested_scopes:
        gate.register_scope(manifest.app_key, decl.scope, decl.purpose)

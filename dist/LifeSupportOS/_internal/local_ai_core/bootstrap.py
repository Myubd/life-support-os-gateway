"""
bootstrap.py
------------
各アプリの起動時に1回呼ぶ、共通の初期化処理をひとつにまとめたもの。

これがなぜ必要か:
- 以前は interview_app が `core_sync/bootstrap.py` に、コアスキーマ初期化・
  device_identity発行・プロフィール確保・プラグイン登録のロジックを
  自前で実装していた。Archlife 側にも同じロジックを新規に書こうとすると、
  パスの組み立て方が少しでも違うだけで「どちらも local_ai_core を使っている
  つもりなのに、実は別々の core.db / 別々の device_id を見ている」という
  事故につながる(実際に発生していた問題)。
- そこで、この重複しやすい初期化ロジックをコア側に1つだけ実装し、
  各アプリはこの関数を呼ぶだけにする。新しいアプリを追加するときも、
  この関数を呼ぶだけで「共通基盤に正しく参加した」状態になる。

各アプリ側の使い方(FastAPIのlifespan/startupから呼ぶ想定):

    from local_ai_core.bootstrap import bootstrap_app

    profile_id = bootstrap_app(Path(__file__).parent / "plugin_manifest.json")
"""
from __future__ import annotations

import base64
from pathlib import Path

from . import paths
from .identity import DeviceIdentity
from .plugins import PluginManifest, register_plugin
from .schema import db_session, init_core_schema


def _ensure_default_profile(
    db_path: str,
    device_id: str,
    key_salt: bytes,
    display_name: str,
) -> int:
    """device_id に紐づく既定プロフィールを1件だけ用意する。

    同じ device_identity.json を共有する2つ目以降のアプリが呼んだ場合は、
    1つ目のアプリが作った profiles 行(同じ device_id)をそのまま再利用する。
    これにより、アプリをまたいでも profile_id が一致し、schedule_items /
    memory_items 等が本当の意味で共有される。
    """
    with db_session(db_path) as conn:
        conn.execute(
            "INSERT INTO device_identity (id, key_salt) VALUES (?, ?) "
            "ON CONFLICT(id) DO NOTHING",
            (device_id, base64.b64encode(key_salt).decode("ascii")),
        )
        row = conn.execute(
            "SELECT id FROM profiles WHERE device_id = ? ORDER BY id LIMIT 1",
            (device_id,),
        ).fetchone()
        if row is not None:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO profiles (device_id, display_name) VALUES (?, ?)",
            (device_id, display_name),
        )
        return cur.lastrowid


def bootstrap_app(
    plugin_manifest_path: str | Path,
    *,
    default_profile_display_name: str = "デフォルトプロフィール",
) -> int:
    """アプリ起動時に1回呼ぶ。冪等なので何度呼んでも安全。

    行うこと(すべて共有パス `local_ai_core.paths` 経由):
    1. 共通スキーマ(core.db)を初期化する
    2. 端末の device_identity と既定プロフィールを用意する(なければ作る。
       他アプリが既に作っていればそれを再利用する)
    3. plugin_manifest.json を読み込み、このアプリを source_apps /
       permission_scopes に登録する(申告のみ。ユーザーが設定画面で
       許可するまで、他アプリのデータには一切アクセスできない)

    戻り値: profile_id。以後の MemoryStore / PermissionGate 呼び出しに使う。
    """
    db_path = paths.get_core_db_path()
    init_core_schema(db_path)

    identity = DeviceIdentity(storage_path=paths.get_device_identity_path())
    profile_id = _ensure_default_profile(
        db_path, identity.device_id, identity.key_salt, default_profile_display_name
    )

    manifest = PluginManifest.load(plugin_manifest_path)
    register_plugin(db_path, manifest)

    return profile_id

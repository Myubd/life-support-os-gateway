"""
permissions/gate.py
--------------------
「AIが全部知っている」を避けるための唯一の関所(gate)。

設計方針:
- 各アプリは自分が使う可能性のあるデータアクセスを `register_scope()` で
  事前に申告する(= plugins/manifest.py がアプリ起動時に自動で行う)。
  申告しただけではまだ何も読めない。
- ユーザーが実際に許可して初めて `permission_grants` に行が作られ、
  以後そのスコープに一致するアクセスだけが `require()` を通過する。
- `require()` を通過したアクセスは必ず `access_log` に記録される。
  拒否されたアクセスも記録するため、ユーザーは後から
  「どのアプリが何を読もうとしたか」を全件確認できる(検証可能なプライバシー)。
- スコープは "resource:action" または "resource:action:namespace.*" の形式。
  ワイルドカードは末尾の ".*" のみサポートする(例: "memory:read:career.*")。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ..schema.migrate import db_session


class PermissionDenied(Exception):
    """要求されたスコープが許可されていない場合に送出される。"""

    def __init__(self, app_key: str, scope: str):
        self.app_key = app_key
        self.scope = scope
        super().__init__(
            f"'{app_key}' は '{scope}' へのアクセスを許可されていません。"
            "ユーザーの許可が必要です(PermissionGate.grant を参照)。"
        )


@dataclass
class GrantInfo:
    app_key: str
    scope: str
    purpose: str
    granted_at: str
    expires_at: Optional[str]


def _scope_matches(pattern: str, requested: str) -> bool:
    """permission_scopes.scope (パターン) が requested スコープを包含するか判定する。"""
    if pattern == requested:
        return True
    if pattern.endswith(".*"):
        prefix = pattern[:-2]
        return requested == prefix or requested.startswith(prefix + ".")
    return False


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class PermissionGate:
    def __init__(self, db_path: str = "core.db"):
        self.db_path = db_path

    # ---------------------------------------------------------------
    # 申告(アプリ起動時。plugins/manifest.py から呼ばれる想定)
    # ---------------------------------------------------------------
    def register_scope(self, app_key: str, scope: str, purpose: str) -> None:
        with db_session(self.db_path) as conn:
            conn.execute(
                "INSERT INTO permission_scopes (app_key, scope, purpose) VALUES (?, ?, ?) "
                "ON CONFLICT(app_key, scope) DO UPDATE SET purpose = excluded.purpose",
                (app_key, scope, purpose),
            )

    # ---------------------------------------------------------------
    # ユーザーによる許可/失効
    # ---------------------------------------------------------------
    def grant(
        self,
        profile_id: int,
        app_key: str,
        scope: str,
        expires_at: Optional[str] = None,
    ) -> None:
        """ユーザーが同意した時に呼ぶ。scope は事前に register_scope 済みである必要がある。"""
        with db_session(self.db_path) as conn:
            row = conn.execute(
                "SELECT id FROM permission_scopes WHERE app_key = ? AND scope = ?",
                (app_key, scope),
            ).fetchone()
            if row is None:
                raise ValueError(
                    f"未登録のスコープです: app_key={app_key!r} scope={scope!r}。"
                    "先にそのアプリのプラグインマニフェストを登録してください。"
                )
            scope_id = row["id"]
            # 既存の失効済み grant があれば再付与、なければ新規作成
            existing = conn.execute(
                "SELECT id FROM permission_grants WHERE profile_id = ? AND scope_id = ? "
                "AND revoked_at IS NULL",
                (profile_id, scope_id),
            ).fetchone()
            if existing:
                return
            conn.execute(
                "INSERT INTO permission_grants (profile_id, scope_id, expires_at) VALUES (?, ?, ?)",
                (profile_id, scope_id, expires_at),
            )

    def revoke(self, profile_id: int, app_key: str, scope: str) -> None:
        """ユーザーがいつでも取り消せる。過去に付与したスコープに完全一致するもののみ失効させる。"""
        with db_session(self.db_path) as conn:
            conn.execute(
                """
                UPDATE permission_grants
                SET revoked_at = ?
                WHERE revoked_at IS NULL
                  AND scope_id IN (
                      SELECT id FROM permission_scopes WHERE app_key = ? AND scope = ?
                  )
                  AND profile_id = ?
                """,
                (_now_iso(), app_key, scope, profile_id),
            )

    # ---------------------------------------------------------------
    # チェック(データアクセスの直前に必ず通す)
    # ---------------------------------------------------------------
    def is_granted(self, profile_id: int, app_key: str, requested_scope: str) -> bool:
        now = _now_iso()
        with db_session(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT ps.scope AS scope, pg.expires_at AS expires_at
                FROM permission_grants pg
                JOIN permission_scopes ps ON ps.id = pg.scope_id
                WHERE pg.profile_id = ?
                  AND ps.app_key = ?
                  AND pg.revoked_at IS NULL
                  AND (pg.expires_at IS NULL OR pg.expires_at > ?)
                """,
                (profile_id, app_key, now),
            ).fetchall()
        return any(_scope_matches(row["scope"], requested_scope) for row in rows)

    def require(self, profile_id: int, app_key: str, requested_scope: str) -> None:
        """許可されていれば access_log に記録して静かに戻る。未許可なら PermissionDenied を送出し、
        その拒否自体も access_log に記録する(監査のため)。
        呼び出し側のデータアクセス関数は、実データを読む前に必ずこれを呼ぶこと。
        """
        granted = self.is_granted(profile_id, app_key, requested_scope)
        with db_session(self.db_path) as conn:
            conn.execute(
                "INSERT INTO access_log (profile_id, app_key, scope, granted) VALUES (?, ?, ?, ?)",
                (profile_id, app_key, requested_scope, 1 if granted else 0),
            )
        if not granted:
            raise PermissionDenied(app_key, requested_scope)

    # ---------------------------------------------------------------
    # UI向け:未許可の申告一覧 / 現在の許可一覧
    # ---------------------------------------------------------------
    def pending_requests(self, profile_id: int) -> list[dict]:
        """まだこのプロフィールが許可していない(=グラント済みでない)スコープ申告の一覧。
        設定画面の「許可を求められています」リストに使う。
        """
        with db_session(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT ps.app_key AS app_key, ps.scope AS scope, ps.purpose AS purpose
                FROM permission_scopes ps
                WHERE NOT EXISTS (
                    SELECT 1 FROM permission_grants pg
                    WHERE pg.scope_id = ps.id
                      AND pg.profile_id = ?
                      AND pg.revoked_at IS NULL
                )
                ORDER BY ps.app_key, ps.scope
                """,
                (profile_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_grants(self, profile_id: int) -> list[GrantInfo]:
        """現在有効な許可の一覧。設定画面の「許可済み」リスト・個別失効UIに使う。"""
        with db_session(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT ps.app_key AS app_key, ps.scope AS scope, ps.purpose AS purpose,
                       pg.granted_at AS granted_at, pg.expires_at AS expires_at
                FROM permission_grants pg
                JOIN permission_scopes ps ON ps.id = pg.scope_id
                WHERE pg.profile_id = ? AND pg.revoked_at IS NULL
                ORDER BY pg.granted_at DESC
                """,
                (profile_id,),
            ).fetchall()
        return [GrantInfo(**dict(row)) for row in rows]

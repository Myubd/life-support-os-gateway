"""
auth.py
--------
gatewayという「唯一の入口」を守るための、最小限の認証。

設計方針(あえてOAuth/JWT/ユーザーテーブルにしていない理由):
- 今のところ、この系は単一ユーザー・単一プロフィール前提のローカルシステムであり、
  「誰が」ではなく「この端末/このブラウザの持ち主かどうか」だけを確認できれば
  十分。複数人・複数権限レベルを区別する必要が出てきた時に初めて、
  本格的な認証基盤へ拡張すればよい。
- 共有シークレット(GATEWAY_AUTH_TOKEN)1つを、ログイン時にHttpOnly Cookieへ
  変換して保存する。以後のリクエストはこのCookieで判定する。
  Cookieの値そのものが shared secret と同じ強さの情報を持つ以上、
  JWTのような署名検証を足しても実質的な安全性は変わらないため、
  あえて追加の依存(itsdangerous等)を増やしていない。
- 将来、複数プロフィール(家族利用)に対応する際は、この仕組みを
  「共有シークレット」から「プロフィールごとのログイン」に置き換える。
  Cookie名・ミドルウェアの構造はそのまま流用できるように設計してある。

環境変数:
- GATEWAY_AUTH_TOKEN: 必須。未設定のまま起動すると、gatewayは起動を拒否する
  (「うっかり無防備なまま公開してしまう」ことを避けるため、
  安全側にフェイルする設計)。
"""
from __future__ import annotations

import hmac
import logging
import os

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("life_support_os_gateway.auth")

SESSION_COOKIE_NAME = "gw_session"

# ミドルウェアを素通りさせるパス(認証チェックの対象外)。
# "/" はindex.html自体は常に返し、その中のJSが個々のAPI呼び出しで
# 401を受け取ったらログイン画面を出す、という構成にしているため、
# シェル(HTML自体)は誰でも取得できてよい(中身のデータには触れない)。
# "/life" "/career" も同様に、archlife/interview_appの静的フロントエンド
# 本体(HTML/JS/CSS)を配信するだけのマウントであり、実データは
# それぞれのバックエンドAPI呼び出し(/api/life/* /api/career/*)側で
# 別途認証がかかるため、ここでは除外してよい。
_PUBLIC_PATHS = {"/", "/health", "/auth/login", "/auth/logout", "/core/frontend_mounts"}
_PUBLIC_PREFIXES = ("/life", "/career")


def _is_public(path: str) -> bool:
    return path in _PUBLIC_PATHS or path.startswith("/auth/") or path.startswith(_PUBLIC_PREFIXES)


def get_auth_token() -> str:
    token = os.environ.get("GATEWAY_AUTH_TOKEN", "")
    if not token:
        raise RuntimeError(
            "GATEWAY_AUTH_TOKEN が設定されていません。gatewayは「唯一の入口」に"
            "なるため、無防備なまま起動しないようにこのチェックを入れています。"
            "docker-compose.yml の gateway サービスに"
            " GATEWAY_AUTH_TOKEN を設定してください。"
        )
    return token


def _is_authenticated(request: Request) -> bool:
    cookie_value = request.cookies.get(SESSION_COOKIE_NAME, "")
    if not cookie_value:
        return False
    # タイミング攻撃を避けるため、通常の == ではなく定数時間比較を使う
    return hmac.compare_digest(cookie_value, get_auth_token())


async def auth_middleware(request: Request, call_next):
    if request.method == "OPTIONS" or _is_public(request.url.path):
        return await call_next(request)

    if not _is_authenticated(request):
        return JSONResponse(
            status_code=401,
            content={"detail": "認証が必要です。/ を開いてログインしてください。"},
        )
    return await call_next(request)

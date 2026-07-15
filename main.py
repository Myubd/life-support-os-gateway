"""
life-support-os-gateway / main.py
-----------------------------------
「一つのライフサポートOS」の入口となるgatewayプロセス。

このgatewayが行うこと:
  1. local_ai_core を共通の基盤として初期化する(bootstrap_app)。
     app_key="life_support_os" として自己申告するが、これは「アシスタント/
     オートメーションが横断的にデータを使う可能性がある」という申告に過ぎず、
     ユーザーが個別に許可するまでは他アプリのデータには一切アクセスできない
     (このリポジトリ直下の plugin_manifest.json を参照)。
  2. local_ai_core.api.build_core_router を /core 配下にマウントし、
     permissions/memory/documents/schedule/knowledge/automation/assistant を
     1つのHTTPエンドポイント群として提供する。
  3. 既存の2つのアプリ(archlife-fastapi, interview_appのFastAPIバックエンド)を
     それぞれ別プロセス・別ポートのまま起動しておき、gatewayが
     /api/life/* → archlife-fastapi、/api/career/* → interview_app backend
     に単純にリバースプロキシする。これにより、フロントエンドは常に
     gatewayの1つのオリジンだけを見ればよくなる(CORS設定の一本化・
     将来のドメイン統一・単一の起動導線という「一つのOS」感を実現する)。

このgatewayは3つのPythonプロセス(archlife-fastapi / interview_app backend /
このgateway)を前提とした「統合レイヤー」であり、3つのアプリのコードを
1プロセスに強引にマージするものではない。理由:
  - archlife-fastapi と interview_app backend は、それぞれ `db` `core_sync` と
    いう同名モジュールを持っており、同一プロセスにimportすると名前空間が
    衝突する。無理に1プロセス化するより、プロセスを分けたまま
    HTTPで疎結合にする方が、両リポジトリの独立した開発・デプロイを
    維持できるため安全。
  - 将来的にどちらかをマイクロサービスとして複数端末に配置する場合にも、
    この構成の方が素直に対応できる。

起動方法は README.md を参照。
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from local_ai_core.bootstrap import bootstrap_app
from local_ai_core.paths import get_core_db_path
from local_ai_core.api import build_core_router
from local_ai_core.llm import LLMRouter, OllamaProvider, ClaudeProvider, OpenAIProvider

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("life_support_os_gateway")

_PLUGIN_MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "plugin_manifest.json")

# プロキシ先(それぞれ別プロセスで起動している既存アプリ)
ARCHLIFE_BACKEND_URL = os.environ.get("ARCHLIFE_BACKEND_URL", "http://localhost:8080")
INTERVIEW_APP_BACKEND_URL = os.environ.get("INTERVIEW_APP_BACKEND_URL", "http://localhost:8000")

_profile_id: int | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _profile_id
    _profile_id = bootstrap_app(_PLUGIN_MANIFEST_PATH,
                                 default_profile_display_name="デフォルトプロフィール")
    logger.info("gateway bootstrap done (profile_id=%s)", _profile_id)
    yield


app = FastAPI(title="Life Support OS Gateway", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ---------------------------------------------------------------------------
# LLMルーター(既定ローカル、ユーザーがオプトインした時だけ外部API)
# ---------------------------------------------------------------------------
llm_router = LLMRouter(
    local=OllamaProvider(
        base_url=os.environ.get("OLLAMA_URL"),
        model=os.environ.get("OLLAMA_MODEL", "qwen3:8b"),
    ),
    external={"claude": ClaudeProvider(), "openai": OpenAIProvider()},
)

app.include_router(build_core_router(db_path=get_core_db_path(), llm_router=llm_router))


@app.get("/health")
def health():
    return {"ok": True, "profile_id": _profile_id}


@app.get("/me/profile_id")
def get_my_profile_id():
    """フロントエンドがこのgatewayに問い合わせるための、現在のprofile_id取得口。
    今後複数プロフィール(家族利用)に対応する際は、ここに認証/選択ロジックを足す。
    """
    return {"profile_id": _profile_id}


# ---------------------------------------------------------------------------
# 単純なリバースプロキシ(REST限定。WebSocket/SSEはこのバージョンでは非対応)
# ---------------------------------------------------------------------------

_HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host",
}


async def _proxy(request: Request, base_url: str, strip_prefix: str) -> Response:
    target_path = request.url.path[len(strip_prefix):] or "/"
    target_url = f"{base_url}{target_path}"

    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP_HEADERS}
    body = await request.body()

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            upstream = await client.request(
                request.method, target_url, headers=headers, params=request.query_params,
                content=body,
            )
        except httpx.ConnectError:
            logger.warning("upstream unreachable: %s", target_url)
            return Response(
                content='{"detail": "連携先のバックエンドが起動していません"}',
                status_code=502, media_type="application/json",
            )

    response_headers = {
        k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP_HEADERS
    }
    return Response(content=upstream.content, status_code=upstream.status_code,
                     headers=response_headers, media_type=upstream.headers.get("content-type"))


@app.api_route("/api/life/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_archlife(request: Request, full_path: str):
    """ライフサポートOS(Archlife)本体へのプロキシ。例:
    /api/life/api/blobs/{anon_id}/{key} → archlife-fastapi の /api/blobs/{anon_id}/{key}
    """
    return await _proxy(request, ARCHLIFE_BACKEND_URL, "/api/life")


@app.api_route("/api/career/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_interview_app(request: Request, full_path: str):
    """就活支援(interview_app)本体へのプロキシ。"""
    return await _proxy(request, INTERVIEW_APP_BACKEND_URL, "/api/career")


# ---------------------------------------------------------------------------
# フロントエンドの静的配信(任意)
#
# 「1つのexe/1つのURLで完結するデスクトップアプリ」として配布したい場合、
# 各フロントエンドのビルド済み静的ファイル(dist/)をこのgatewayプロセスから
# 直接配信できるようにしておく。環境変数でdistのパスを指定した時だけ有効になる
# (指定がなければ何もマウントしない=開発時は各フロントエンドのdevサーバーを
# 個別に使えばよい)。
# ---------------------------------------------------------------------------
_ARCHLIFE_FRONTEND_DIST = os.environ.get("ARCHLIFE_FRONTEND_DIST")
_INTERVIEW_FRONTEND_DIST = os.environ.get("INTERVIEW_FRONTEND_DIST")

if _ARCHLIFE_FRONTEND_DIST and os.path.isdir(_ARCHLIFE_FRONTEND_DIST):
    app.mount("/life", StaticFiles(directory=_ARCHLIFE_FRONTEND_DIST, html=True), name="life-frontend")
    logger.info("mounted Archlife frontend from %s at /life", _ARCHLIFE_FRONTEND_DIST)

if _INTERVIEW_FRONTEND_DIST and os.path.isdir(_INTERVIEW_FRONTEND_DIST):
    app.mount("/career", StaticFiles(directory=_INTERVIEW_FRONTEND_DIST, html=True), name="career-frontend")
    logger.info("mounted interview_app frontend from %s at /career", _INTERVIEW_FRONTEND_DIST)


@app.get("/", response_class=HTMLResponse)
def landing_page():
    """両方の静的ファイルがマウントされている時だけ、簡易ナビを出す。
    それ以外(開発時)は、各アプリのdevサーバーに直接アクセスする運用のままでよい。
    """
    links = []
    if _ARCHLIFE_FRONTEND_DIST:
        links.append('<a href="/life/">ライフサポートOS</a>')
    if _INTERVIEW_FRONTEND_DIST:
        links.append('<a href="/career/">就活支援</a>')
    if not links:
        return (
            "<html><body style='font-family:sans-serif;padding:2rem'>"
            "<h1>Life Support OS Gateway</h1>"
            "<p>フロントエンドの静的ファイルはマウントされていません"
            "(開発時は各アプリのdevサーバーに直接アクセスしてください)。"
            "<br>APIは <a href='/docs'>/docs</a> を参照。</p>"
            "</body></html>"
        )
    body = "".join(f"<li style='font-size:1.2rem;margin:0.5rem 0'>{link}</li>" for link in links)
    return (
        "<html><body style='font-family:sans-serif;padding:2rem'>"
        "<h1>Life Support OS</h1>"
        f"<ul style='list-style:none;padding:0'>{body}</ul>"
        "</body></html>"
    )

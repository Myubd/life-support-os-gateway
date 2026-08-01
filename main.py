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
     /api/life/* → archlife-fastapi、/api/career/* → interview_app backend、
     /api/study/* → study-support backend に単純にリバースプロキシする。
     これにより、フロントエンドは常にgatewayの1つのオリジンだけを見ればよくなる
     (CORS設定の一本化・将来のドメイン統一・単一の起動導線という
     「一つのOS」感を実現する)。
  4. automation_scheduler.build_automation_scheduler() で、有効な
     automation_rules を一定間隔ごとに自動実行する(APScheduler)。
     このgatewayプロセスだけがスケジューラを持つ(archlife-fastapi /
     interview_app backend / study-support はそれぞれ個別コンテナだが、
     スケジューラは持たせない — 複数プロセスが同じルールを重複実行するのを
     防ぐため)。

このgatewayは複数のPythonプロセス(archlife-fastapi / interview_app backend /
study-support / このgateway)を前提とした「統合レイヤー」であり、各アプリの
コードを1プロセスに強引にマージするものではない。理由:
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

import asyncio
import hmac
import logging
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from local_ai_core.bootstrap import bootstrap_app
from local_ai_core.paths import get_core_db_path
from local_ai_core.api import build_core_router
from local_ai_core.llm import LLMRouter, OllamaProvider, ClaudeProvider, OpenAIProvider

from automation_scheduler import build_automation_scheduler
from auth import auth_middleware, get_auth_token, SESSION_COOKIE_NAME
from backup import backup_core_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("life_support_os_gateway")

_PLUGIN_MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "plugin_manifest.json")

# プロキシ先(それぞれ別プロセスで起動している既存アプリ)
ARCHLIFE_BACKEND_URL = os.environ.get("ARCHLIFE_BACKEND_URL", "http://localhost:8080")
INTERVIEW_APP_BACKEND_URL = os.environ.get("INTERVIEW_APP_BACKEND_URL", "http://localhost:8000")
STUDY_SUPPORT_BACKEND_URL = os.environ.get("STUDY_SUPPORT_BACKEND_URL", "http://localhost:8100")
HEALTH_SUPPORT_BACKEND_URL = os.environ.get("HEALTH_SUPPORT_BACKEND_URL", "http://localhost:8200")

# オートメーション定期実行の間隔(秒)。既定は1時間。
# 開発時に短い間隔で確認したい場合は環境変数で上書きする。
AUTOMATION_POLL_INTERVAL_SECONDS = int(os.environ.get("AUTOMATION_POLL_INTERVAL_SECONDS", "3600"))

# core.dbバックアップの設定。
# BACKUP_DIR は docker-compose 側でホストのフォルダに bind mount する想定
# (core_shared_data ボリュームの中には置かない。ボリューム自体が壊れた/
#  誤って削除された場合にバックアップも道連れにならないようにするため)。
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/backups")
BACKUP_INTERVAL_HOURS = int(os.environ.get("BACKUP_INTERVAL_HOURS", "24"))
BACKUP_RETENTION_DAYS = int(os.environ.get("BACKUP_RETENTION_DAYS", "14"))

# CORSで許可するオリジン(カンマ区切り)。
# 個別フロントエンド(archlife_frontend:8081 / interview_frontend:3001)や、
# 将来のモバイルWebViewなどからgatewayを直接呼ぶ可能性を考慮し、
# 既定では代表的なlocalhostのポートのみを許可する(ワイルドカードにはしない)。
_default_origins = "http://localhost:3000,http://localhost:8081,http://localhost:3001"
ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("GATEWAY_ALLOWED_ORIGINS", _default_origins).split(",")
    if o.strip()
]

_profile_id: int | None = None
_scheduler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _profile_id, _scheduler
    # gatewayは「唯一の入口」なので、認証トークンが無いまま起動しないよう
    # 一番最初に確認する(未設定なら例外を投げてコンテナごと落とす)。
    get_auth_token()

    _profile_id = bootstrap_app(_PLUGIN_MANIFEST_PATH,
                                 default_profile_display_name="デフォルトプロフィール")
    logger.info("gateway bootstrap done (profile_id=%s)", _profile_id)

    _scheduler = build_automation_scheduler(
        db_path=get_core_db_path(),
        profile_id_getter=lambda: _profile_id,
        llm_router=llm_router,
        interval_seconds=AUTOMATION_POLL_INTERVAL_SECONDS,
    )

    async def _backup_job() -> None:
        # backup_core_db は同期(ブロッキング)処理なので、イベントループを
        # 止めないよう別スレッドで実行する(automation_schedulerと同じ配慮)。
        try:
            await asyncio.to_thread(
                backup_core_db, get_core_db_path(), BACKUP_DIR, BACKUP_RETENTION_DAYS
            )
        except Exception:
            logger.exception("core.dbのバックアップに失敗しました")

    _scheduler.add_job(
        _backup_job, "interval", hours=BACKUP_INTERVAL_HOURS,
        id="core_db_backup", max_instances=1, coalesce=True,
    )

    _scheduler.start()
    logger.info(
        "automation scheduler started (automation_interval=%ss, backup_interval=%sh, backup_dir=%s)",
        AUTOMATION_POLL_INTERVAL_SECONDS, BACKUP_INTERVAL_HOURS, BACKUP_DIR,
    )

    yield

    _scheduler.shutdown(wait=False)


app = FastAPI(title="Life Support OS Gateway", lifespan=lifespan)
# 登録順序が重要: CORSMiddlewareを外側(先)、認証を内側(後)にする。
# 逆にすると、ブラウザが送るCORSプリフライト(OPTIONS)が認証ミドルウェアで
# 先に弾かれ、Access-Control-Allow-Originヘッダの付かない401が返ってしまい、
# ブラウザ側では「CORSエラー」としてしか見えなくなる。
app.middleware("http")(auth_middleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,  # Cookieベースの認証を使うため必須
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    return {
        "ok": True,
        "profile_id": _profile_id,
        "automation_scheduler_running": bool(_scheduler and _scheduler.running),
    }


class LoginRequest(BaseModel):
    token: str


@app.post("/auth/login")
def login(body: LoginRequest, response: Response):
    if not hmac.compare_digest(body.token, get_auth_token()):
        raise HTTPException(status_code=401, detail="トークンが正しくありません")
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=body.token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,  # 30日
        # secure=True にしたいところだが、素のHTTP(TLS終端なし)での
        # ローカル/LAN運用を前提にしているため既定はFalse。
        # リバースプロキシ等でHTTPS化した場合は明示的にTrueへ変更すること。
        secure=os.environ.get("GATEWAY_COOKIE_SECURE", "false").lower() == "true",
    )
    return {"ok": True}


@app.post("/auth/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"ok": True}


@app.post("/admin/backup")
async def trigger_backup():
    """手動でcore.dbのバックアップを1つ作る。
    定期実行(BACKUP_INTERVAL_HOURS)を待たずに、リスクのある操作
    (docker compose down -v の前、大きな設定変更の前など)の直前に
    手動で叩くことを想定している。認証済みでないと呼べない
    (auth_middlewareが/adminパスも保護対象にしている)。
    """
    try:
        dest_path = await asyncio.to_thread(
            backup_core_db, get_core_db_path(), BACKUP_DIR, BACKUP_RETENTION_DAYS
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"バックアップに失敗しました: {e}")
    return {"ok": True, "path": dest_path}


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


@app.api_route("/api/study/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_study_support(request: Request, full_path: str):
    """学習支援(study-support)本体へのプロキシ。専用フロントエンドはまだ無いため、
    今はAPI疎通(/health, /logs)のみが対象。
    """
    return await _proxy(request, STUDY_SUPPORT_BACKEND_URL, "/api/study")


@app.api_route("/api/health/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_health_support(request: Request, full_path: str):
    """健康管理(health-support)本体へのプロキシ。
    health-support自身は独立したフロントエンド(static/index.html)を
    自分のポート(8200)で直接配信しているため、gateway経由でのアクセスは
    必須ではないが、他アプリと同じ「/api/<app>/*」の形に揃えておく。
    """
    return await _proxy(request, HEALTH_SUPPORT_BACKEND_URL, "/api/health")


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


_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@app.get("/", response_class=HTMLResponse)
def dashboard():
    """統合コンソール(static/index.html)を返す。

    権限台帳・ドキュメントセンター(横断ビュー)・アシスタント・オートメーションを
    1画面にまとめた、gateway自身が提供する唯一の「見える化」画面。
    archlife/interview_appそれぞれの個別フロントエンドは /life/ /career/ から
    従来通りアクセスできる(このダッシュボードはそれらを置き換えるものではなく、
    どちらのアプリの持ち物でもない横断機能の置き場所)。
    """
    index_path = os.path.join(_STATIC_DIR, "index.html")
    with open(index_path, encoding="utf-8") as f:
        return f.read()

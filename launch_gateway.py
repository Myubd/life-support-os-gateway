"""
launch_gateway.py
-------------------
Life Support OS の統合ランチャー(方式A: gateway中心の統合配布)。

interview_app単体配布(launch_fastapi.py)と違い、こちらは
「gateway + archlife-fastapi + interview_app backend + study-support +
health-support + digital-vault」の6プロセスをまとめて起動し、ブラウザは
http://localhost:3000 (gateway) だけを開く。個々のバックエンドは
GATEWAY_AUTH_TOKENの検証(service_auth.py)が有効なままなので、
統合コンソールを経由しないアクセスは引き続き401になる。

Ollamaのインストール確認・モデル管理・ポート待受・クラッシュログ等の
汎用処理は local_ai_core.launcher_kit をそのまま再利用する
(interview_appのlaunch_fastapi.pyから切り出したもの)。

【現状のスコープ】
このファイルは「ソースから `python launch_gateway.py` で起動する」
開発・検証段階の実装。各バックエンドは `sys.executable -m uvicorn` で
ソースディレクトリから直接起動する(Dockerfileの `uvicorn main:app` と
同じ起動方法)。PyInstallerでの単体exe化(各バックエンドを個別exeにして
同梱する、フロントエンドのビルド済みdistを同梱する等)は次のステップ。
"""
from __future__ import annotations

import json
import multiprocessing
import os
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from local_ai_core import launcher_kit as lk

if __name__ == "__main__":
    # interview_appのlaunch_fastapi.pyと同じ理由(PyInstaller + multiprocessing対策)。
    multiprocessing.freeze_support()


# ============================================================
# 設定
# ============================================================
APP_FOLDER_NAME = "LifeSupportOS"  # %APPDATA%\LifeSupportOS\ 配下にトークン・DB・ログを置く

GATEWAY_PORT = 3000
GATEWAY_URL = f"http://localhost:{GATEWAY_PORT}"

OLLAMA_HOST = "http://localhost:11434"
REQUIRED_MODELS = ["qwen3:8b", "nomic-embed-text"]


@dataclass(frozen=True)
class ServiceSpec:
    """1つのバックエンドプロセスの起動情報。"""
    name: str                      # ログ表示・プロセス識別用
    port: int
    # ソース実行時のディレクトリ(このファイルからの相対パス)。
    # 実際の起動コマンドは `python -m uvicorn main:app --host 127.0.0.1 --port <port>`。
    dev_relative_dir: str
    # このサービス固有の環境変数(db pathなど)。値は build_service_env() で解決する。
    extra_env: dict = field(default_factory=dict)
    # exe化後(frozen)の実行ファイルの場所。LifeSupportOS.exe自身の
    # ディレクトリからの相対パス。インストーラーが作る配置レイアウト
    # (下記コメント参照)に対応させている。
    frozen_exe_relative_path: str = ""


# exe配布時の想定レイアウト(Inno Setupインストーラーがこの形に配置する):
#
#   <インストール先>\
#   ├── LifeSupportOS.exe          (gateway。これがlaunch_gateway.py本体)
#   └── backends\
#       ├── archlife_backend\launch_fastapi.exe
#       ├── interview_backend\interview_backend.exe
#       ├── study_support\study_support.exe
#       ├── health_support\health_support.exe
#       └── digital_vault\digital_vault.exe
#
# 6プロセス中、gateway以外の5つ。gateway自身は別扱い(フロントエンド配信の
# env varsも必要なため main() 内で個別に組み立てる)。
BACKEND_SERVICES: list[ServiceSpec] = [
    ServiceSpec(
        name="archlife_backend",
        port=8080,
        dev_relative_dir="../archlife/archlife-fastapi",
        extra_env={"ARCHLIFE_DB_PATH": "archlife.db", "OLLAMA_URL": OLLAMA_HOST},
        # archlife-fastapiの実行ファイル名は launch_fastapi.spec の
        # name='launch_fastapi' に由来する(Electron配布と共用のため、
        # このリポジトリ側のexe名までは変えていない)。
        frozen_exe_relative_path="backends/archlife_backend/launch_fastapi.exe",
    ),
    ServiceSpec(
        name="interview_backend",
        port=8000,
        dev_relative_dir="../interview_app/react-fastapi/backend",
        extra_env={"INTERVIEW_DB_PATH": "career_support.db", "OLLAMA_HOST": OLLAMA_HOST},
        frozen_exe_relative_path="backends/interview_backend/interview_backend.exe",
    ),
    ServiceSpec(
        name="study_support",
        port=8100,
        dev_relative_dir="../study-support",
        extra_env={"STUDY_DB_PATH": "study.db"},
        frozen_exe_relative_path="backends/study_support/study_support.exe",
    ),
    ServiceSpec(
        name="health_support",
        port=8200,
        dev_relative_dir="../health-support",
        extra_env={"HEALTH_DB_PATH": "health.db"},
        frozen_exe_relative_path="backends/health_support/health_support.exe",
    ),
    ServiceSpec(
        name="digital_vault",
        port=8300,
        dev_relative_dir="../digital-vault",
        extra_env={"VAULT_DB_PATH": "digital_vault.db"},
        frozen_exe_relative_path="backends/digital_vault/digital_vault.exe",
    ),
]


# ============================================================
# GATEWAY_AUTH_TOKEN の自動生成・永続化
# ============================================================
# 単体exe配布では、docker-composeの.envのようにユーザーへトークン入力を
# 求めるのは非現実的。初回起動時に安全な乱数トークンを生成し、
# %APPDATA%\LifeSupportOS\auth_token.json に保存して次回以降も使い回す。
# (ブラウザのCookieはこの値と照合されるだけなので、値自体をユーザーが
# 知る必要はない。ログイン画面の「アクセストークン」入力欄は
# 統合exe版では使わず、gateway自身が起動時にCookieを発行する方式に
# 変えるのが理想だが、それはservice層の変更を伴うため別ステップとする。
# 現状は自動生成したトークンを全プロセスの環境変数として配ることで、
# 「同じマシン上の別プロセスからの直接アクセスを防ぐ」という目的は
# 引き続き満たされる)。

def get_or_create_auth_token(token_path: Path) -> str:
    """トークンファイルが存在すればそれを読み、無ければ生成して保存する。"""
    if token_path.exists():
        try:
            data = json.loads(token_path.read_text(encoding="utf-8"))
            token = data.get("token", "")
            if token:
                return token
        except Exception:
            pass  # 壊れていた場合は再生成する

    token = secrets.token_urlsafe(32)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(json.dumps({"token": token}), encoding="utf-8")
    return token


def app_data_dir(app_folder_name: str = APP_FOLDER_NAME) -> Path:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = Path(base) / app_folder_name
    path.mkdir(parents=True, exist_ok=True)
    return path


# ============================================================
# 環境変数の組み立て(純粋関数: テストしやすいようsubprocess呼び出しと分離)
# ============================================================

def build_service_env(
    spec: ServiceSpec,
    base_env: dict,
    auth_token: str,
    data_dir: Path,
) -> dict:
    """1サービス分の環境変数を組み立てる。

    - base_env(通常は os.environ のコピー)を土台にする
    - GATEWAY_AUTH_TOKENを全サービス共通で設定する
    - LOCAL_AI_CORE_DB_PATH/DEVICE_IDENTITY_PATHは意図的に設定しない
      (local_ai_core.paths が %LOCALAPPDATA%\\ArchLifeEcosystem\\ に
      自動フォールバックし、5プロセス全部が同じcore.dbを見る設計のため。
      ここで明示的に指定すると、その挙動を壊してしまう)
    - spec.extra_env の "*_DB_PATH" で終わるキーは、このアプリ専用の
      非共有DBのファイル名として扱い、data_dir配下の絶対パスに解決する
      (launch_fastapi.pyの_resolve_db_pathと同じ考え方)
    """
    env = dict(base_env)
    env["GATEWAY_AUTH_TOKEN"] = auth_token

    for key, value in spec.extra_env.items():
        if key.endswith("_DB_PATH"):
            env[key] = str(data_dir / value)
        else:
            env[key] = value

    return env


def build_gateway_env(base_env: dict, auth_token: str, frontend_dists: dict[str, str]) -> dict:
    """gateway自身の環境変数を組み立てる。

    frontend_dists は {"ARCHLIFE_FRONTEND_DIST": "...", "INTERVIEW_FRONTEND_DIST": "..."}
    のような辞書。ディレクトリが実際に存在するものだけを設定する
    (未ビルドの場合、gateway側は該当パスが無ければ静的マウントをスキップする
    実装になっているため、存在チェックはgateway側にも任せてよいが、ここでも
    存在しないパスをそのまま渡さないことで意図を明確にする)。
    """
    env = dict(base_env)
    env["GATEWAY_AUTH_TOKEN"] = auth_token
    for key, path in frontend_dists.items():
        if path and os.path.isdir(path):
            env[key] = path
    return env


def is_frozen() -> bool:
    """PyInstallerでexe化された状態で実行されているか。"""
    return bool(getattr(sys, "frozen", False))


def build_frozen_service_env(base_env: dict, auth_token: str, port: int, data_dir: Path) -> dict:
    """exe化後(frozen)のバックエンド起動用環境変数。

    各バックエンドの run_service.py / launch_fastapi.py は、
    build_service_env() が使う個別の "*_DB_PATH" 環境変数ではなく、
    より単純な PORT / DATA_DIR の2つだけを見る契約になっている
    (archlife-fastapi/launch_fastapi.py と同じ契約に統一してある)。
    DBファイル名の組み立てはバックエンド自身の役目にすることで、
    ここではポートとデータの置き場所だけを渡せばよいようにしている。
    """
    env = dict(base_env)
    env["GATEWAY_AUTH_TOKEN"] = auth_token
    env["PORT"] = str(port)
    env["DATA_DIR"] = str(data_dir)
    return env


def resolve_service_command(spec: ServiceSpec, launcher_dir: str, frozen: bool) -> tuple[list[str], str]:
    """1サービス分の起動コマンドと作業ディレクトリを解決する。

    - ソース実行時: `python -m uvicorn main:app --port <port>` を
      spec.dev_relative_dir で実行する(従来通り)。
    - exe化後: spec.frozen_exe_relative_path が指す実行ファイルを
      そのまま起動する(引数無し。設定は環境変数で渡す)。
    """
    if frozen:
        exe_path = os.path.normpath(os.path.join(launcher_dir, spec.frozen_exe_relative_path))
        return [exe_path], os.path.dirname(exe_path)
    cwd = os.path.normpath(os.path.join(launcher_dir, spec.dev_relative_dir))
    return uvicorn_args(spec.port), cwd


# ============================================================
# プロセス管理
# ============================================================
# subprocess.Popen を直接テストで呼びたくないため、popen_fn として
# 注入できるようにする(デフォルトはsubprocess.Popen)。

PopenFn = Callable[..., "subprocess.Popen"]


class ProcessManager:
    """複数の子プロセスをまとめて起動・終了する。"""

    def __init__(self, popen_fn: PopenFn = subprocess.Popen):
        self._popen_fn = popen_fn
        self._processes: list[tuple[str, "subprocess.Popen"]] = []

    def start(self, name: str, args: list[str], cwd: str, env: dict) -> "subprocess.Popen":
        lk.log(f"起動中: {name} (port指定込みのコマンド: {' '.join(args)})", "INFO")
        proc = self._popen_fn(args, cwd=cwd, env=env)
        self._processes.append((name, proc))
        return proc

    def terminate_all(self, timeout: float = 5.0) -> None:
        """全プロセスを終了する。緩やかに終わらないものは強制終了する。"""
        for name, proc in reversed(self._processes):
            if proc.poll() is not None:
                continue  # 既に終了している
            lk.log(f"終了しています: {name}", "INFO")
            try:
                proc.terminate()
            except Exception:
                pass

        deadline = time.time() + timeout
        for name, proc in self._processes:
            remaining = max(0.0, deadline - time.time())
            try:
                proc.wait(timeout=remaining)
            except Exception:
                try:
                    lk.log(f"応答が無いため強制終了します: {name}", "WARNING")
                    proc.kill()
                except Exception:
                    pass

    @property
    def processes(self) -> list[tuple[str, "subprocess.Popen"]]:
        return list(self._processes)


def uvicorn_args(port: int) -> list[str]:
    """開発実行時(ソースから)のuvicorn起動コマンド。"""
    return [
        sys.executable, "-m", "uvicorn", "main:app",
        "--host", "127.0.0.1", "--port", str(port),
    ]


# ============================================================
# メイン
# ============================================================

def resolve_frontend_dists(launcher_dir: str, frozen: bool) -> dict[str, str]:
    """フロントエンドのビルド済みdistの場所を解決する。

    - ソース実行時: 各リポジトリの npm run build 出力をそのまま参照する
      (archlife-frontend: `npm run build:electron`、
       interview_app: `npm run build:gateway`)
    - exe化後: LifeSupportOS.exeのビルド時にdatasとして同梱した
      frontend_dist_archlife / frontend_dist_interview を参照する。
      PyInstaller 6.xのonedirビルドでは、datasは実行ファイルと同じ階層
      ではなく "_internal/" 配下に置かれる(実際にビルドしたexeで確認して
      初めて気づいた挙動なので、ここにコメントを残しておく)。
    """
    if frozen:
        return {
            "ARCHLIFE_FRONTEND_DIST": os.path.join(launcher_dir, "_internal", "frontend_dist_archlife"),
            "INTERVIEW_FRONTEND_DIST": os.path.join(launcher_dir, "_internal", "frontend_dist_interview"),
        }
    return {
        "ARCHLIFE_FRONTEND_DIST": os.path.normpath(
            os.path.join(launcher_dir, "../archlife/archlife-frontend/dist")),
        "INTERVIEW_FRONTEND_DIST": os.path.normpath(
            os.path.join(launcher_dir, "../interview_app/react-fastapi/frontend/dist-gateway")),
    }


def main() -> None:
    lk.hide_console_window()
    lk.fix_stdio()
    lk.suppress_child_console()

    lk.log("=" * 60, "INFO")
    lk.log("Life Support OS を起動しています", "INFO")
    lk.log("=" * 60, "INFO")

    lk.cleanup_old_meipass()

    data_dir = app_data_dir()
    token_path = data_dir / "auth_token.json"
    auth_token = get_or_create_auth_token(token_path)

    # 前回異常終了した際に残っているプロセスがあれば片付ける
    lk.kill_existing_process(GATEWAY_PORT)
    for spec in BACKEND_SERVICES:
        lk.kill_existing_process(spec.port)

    ollama_ok = True
    if os.environ.get("LIFEOS_SKIP_OLLAMA_SETUP") == "1":
        # テスト/CI用の抜け道。実際のOllamaインストール・モデルダウンロードは
        # 重く、かつWindows専用のインストーラーを実行するためLinux CI等では
        # そもそも動かない。オーケストレーション(5プロセスの起動・認証・
        # フロントエンド配信)だけを検証したい場合に使う。
        lk.log("LIFEOS_SKIP_OLLAMA_SETUP=1 のため、Ollamaのセットアップをスキップします", "WARNING")
    else:
        ollama_ok = lk.ensure_ollama(REQUIRED_MODELS)
    if not ollama_ok:
        lk.log("Ollamaのセットアップが完了しませんでした。バックエンドは起動しますが、"
               "AI機能が使えない可能性があります。", "WARNING")

    manager = ProcessManager()
    # ×ボタンでウィンドウを閉じた場合、Ctrl+Cと違ってPythonのfinally節が
    # 実行される保証が無い(launcher_kit.install_console_close_handlerの
    # コメント参照)。実際に子プロセスが残り続ける現象が起きたため、
    # 明示的にハンドラーを登録しておく。
    lk.install_console_close_handler(lambda: manager.terminate_all(timeout=3.0))
    base_env = dict(os.environ)
    frozen = is_frozen()
    # exe化後は sys.executable がインストール先の LifeSupportOS.exe を指すため、
    # そのディレクトリを起点に backends/ 配下の各exeを解決する。
    # ソース実行時は従来通りこのファイル自身のディレクトリが起点。
    launcher_dir = os.path.dirname(os.path.abspath(sys.executable if frozen else __file__))
    lk.log(f"起動モード: {'exe (frozen)' if frozen else 'ソース実行 (python)'}", "INFO")

    try:
        for spec in BACKEND_SERVICES:
            args, cwd = resolve_service_command(spec, launcher_dir, frozen)
            if frozen:
                # 各バックエンド固有のDB保存先を分けるため、サービス名ごとの
                # サブフォルダにする(全部同じdata_dir直下だとファイル名が
                # 衝突する可能性があるため)。
                service_data_dir = data_dir / spec.name
                env = build_frozen_service_env(base_env, auth_token, spec.port, service_data_dir)
            else:
                env = build_service_env(spec, base_env, auth_token, data_dir)
            manager.start(spec.name, args, cwd=cwd, env=env)

        # 各バックエンドがポートを開くまで待つ(gatewayがプロキシ先に
        # すぐアクセスできるようにするため。失敗してもgateway自体は
        # 起動し、個別のAPI呼び出し時にエラーになるだけなので続行する)。
        for spec in BACKEND_SERVICES:
            if not lk.wait_for_port(spec.port, timeout=60.0):
                lk.log(f"{spec.name} の起動がタイムアウトしました(続行します)", "WARNING")
            else:
                lk.log(f"✓ {spec.name} が起動しました (port {spec.port})", "SUCCESS")

        # フロントエンドのビルド済みdistの場所は、ソース実行時とexe化後で異なる
        # (resolve_frontend_dists()参照)。
        frontend_dists = resolve_frontend_dists(launcher_dir, frozen)
        if frozen:
            gateway_args = [sys.executable]
            gateway_cwd = launcher_dir
        else:
            gateway_args = uvicorn_args(GATEWAY_PORT)
            gateway_cwd = launcher_dir

        gateway_env = build_gateway_env(base_env, auth_token, frontend_dists)

        if frozen:
            # exe化後、gateway自身(main:app)はこのプロセス自身の中で
            # 直接起動する(別exeを子プロセスとして再度立ち上げる必要はない。
            # LifeSupportOS.exe自体がgatewayのuvicornを内包しているため)。
            # そのため gateway だけは ProcessManager に登録せず、この後で
            # 直接 uvicorn.run() を呼ぶ。
            pass
        else:
            manager.start("gateway", gateway_args, cwd=gateway_cwd, env=gateway_env)

        if frozen:
            # 環境変数はこのプロセス自身にも反映してから、同一プロセス内で
            # gatewayのASGIアプリを起動する。
            os.environ.update(gateway_env)
            _run_gateway_in_process()
        else:
            if not lk.wait_for_port(GATEWAY_PORT, timeout=30.0):
                lk.log("gatewayの起動がタイムアウトしました", "ERROR")
            else:
                lk.log(f"✓ gateway が起動しました ({GATEWAY_URL})", "SUCCESS")
                _open_browser_with_token(auth_token)

            lk.log("=" * 60, "INFO")
            lk.log("すべてのサービスが起動しました。終了するにはこのウィンドウを閉じてください。", "SUCCESS")
            lk.log("=" * 60, "INFO")

            # gatewayプロセスが生きている間は待ち続ける
            gateway_proc = manager.processes[-1][1]
            gateway_proc.wait()

    except KeyboardInterrupt:
        lk.log("終了処理を開始します...", "INFO")
    finally:
        manager.terminate_all()


def _open_browser_with_token(auth_token: str) -> None:
    # 初回自動生成したトークンをユーザーに手入力させないよう、
    # ?token=... を付けて開く(gateway側のboot()がこれを拾って
    # 自動ログインし、URLからはすぐに消す)。
    import urllib.parse
    login_url = f"{GATEWAY_URL}/?token={urllib.parse.quote(auth_token)}"
    lk.open_browser(login_url, GATEWAY_PORT, timeout=5.0)


def _run_gateway_in_process() -> None:
    """exe化後、gateway(main:app)を子プロセスではなく自プロセス内で起動する。

    理由: LifeSupportOS.exe自体がgatewayのビルド成果物なので、わざわざ
    自分自身をもう一度子プロセスとして起動する必要が無い(むしろ
    sys.executableを再帰的にspawnすると多重起動になってしまう)。
    バックエンド4つだけは別exe(backends/配下)なので引き続き子プロセスで
    起動する。
    """
    import threading
    import uvicorn

    def _run():
        uvicorn.run("main:app", host="127.0.0.1", port=GATEWAY_PORT, log_level="warning", loop="asyncio")

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    if not lk.wait_for_port(GATEWAY_PORT, timeout=30.0):
        lk.log("gatewayの起動がタイムアウトしました", "ERROR")
    else:
        lk.log(f"✓ gateway が起動しました ({GATEWAY_URL})", "SUCCESS")
        auth_token = os.environ.get("GATEWAY_AUTH_TOKEN", "")
        _open_browser_with_token(auth_token)

    lk.log("=" * 60, "INFO")
    lk.log("すべてのサービスが起動しました。終了するにはこのウィンドウを閉じてください。", "SUCCESS")
    lk.log("=" * 60, "INFO")

    thread.join()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            lk.fix_stdio()
        except Exception:
            pass
        lk.write_crash_log(APP_FOLDER_NAME, "launch_gateway.py の main()内で未処理の例外が発生しました", e)
        raise

"""
launch_gateway.py
-------------------
Life Support OS の統合ランチャー(方式A: gateway中心の統合配布)。

interview_app単体配布(launch_fastapi.py)と違い、こちらは
「gateway + archlife-fastapi + interview_app backend + study-support +
health-support」の5プロセスをまとめて起動し、ブラウザは
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


# 5プロセス中、gateway以外の4つ。gateway自身は別扱い(フロントエンド配信の
# env varsも必要なため main() 内で個別に組み立てる)。
BACKEND_SERVICES: list[ServiceSpec] = [
    ServiceSpec(
        name="archlife_backend",
        port=8080,
        dev_relative_dir="../archlife/archlife-fastapi",
        extra_env={"ARCHLIFE_DB_PATH": "archlife.db", "OLLAMA_URL": OLLAMA_HOST},
    ),
    ServiceSpec(
        name="interview_backend",
        port=8000,
        dev_relative_dir="../interview_app/react-fastapi/backend",
        extra_env={"INTERVIEW_DB_PATH": "career_support.db", "OLLAMA_HOST": OLLAMA_HOST},
    ),
    ServiceSpec(
        name="study_support",
        port=8100,
        dev_relative_dir="../study-support",
        extra_env={"STUDY_DB_PATH": "study.db"},
    ),
    ServiceSpec(
        name="health_support",
        port=8200,
        dev_relative_dir="../health-support",
        extra_env={"HEALTH_DB_PATH": "health.db"},
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

    ollama_ok = lk.ensure_ollama(REQUIRED_MODELS)
    if not ollama_ok:
        lk.log("Ollamaのセットアップが完了しませんでした。バックエンドは起動しますが、"
               "AI機能が使えない可能性があります。", "WARNING")

    manager = ProcessManager()
    base_env = dict(os.environ)
    launcher_dir = os.path.dirname(os.path.abspath(__file__))

    try:
        for spec in BACKEND_SERVICES:
            env = build_service_env(spec, base_env, auth_token, data_dir)
            cwd = os.path.normpath(os.path.join(launcher_dir, spec.dev_relative_dir))
            manager.start(spec.name, uvicorn_args(spec.port), cwd=cwd, env=env)

        # 各バックエンドがポートを開くまで待つ(gatewayがプロキシ先に
        # すぐアクセスできるようにするため。失敗してもgateway自体は
        # 起動し、個別のAPI呼び出し時にエラーになるだけなので続行する)。
        for spec in BACKEND_SERVICES:
            if not lk.wait_for_port(spec.port, timeout=60.0):
                lk.log(f"{spec.name} の起動がタイムアウトしました(続行します)", "WARNING")
            else:
                lk.log(f"✓ {spec.name} が起動しました (port {spec.port})", "SUCCESS")

        # フロントエンドのビルド済みdistが同梱されていれば、gatewayから
        # 直接配信する(将来的にexe同梱する想定。ソース実行時は無いので
        # gateway側は単に静的マウントをスキップする)。
        frontend_dists = {
            "ARCHLIFE_FRONTEND_DIST": os.path.normpath(
                os.path.join(launcher_dir, "../archlife/archlife-frontend/dist")),
            "INTERVIEW_FRONTEND_DIST": os.path.normpath(
                os.path.join(launcher_dir, "../interview_app/react-fastapi/frontend/dist")),
        }
        gateway_env = build_gateway_env(base_env, auth_token, frontend_dists)
        manager.start("gateway", uvicorn_args(GATEWAY_PORT), cwd=launcher_dir, env=gateway_env)

        if not lk.wait_for_port(GATEWAY_PORT, timeout=30.0):
            lk.log("gatewayの起動がタイムアウトしました", "ERROR")
        else:
            lk.log(f"✓ gateway が起動しました ({GATEWAY_URL})", "SUCCESS")
            lk.open_browser(GATEWAY_URL, GATEWAY_PORT, timeout=5.0)

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

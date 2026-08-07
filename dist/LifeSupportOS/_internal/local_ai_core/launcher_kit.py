"""
launcher_kit.py
-----------------
PyInstaller配布(単体exe)向けの汎用ランチャー部品。

由来: interview_app/react-fastapi/backend/launch_fastapi.py で実装された
Ollama自動インストール・ポート待受・ブラウザ起動・クラッシュログ等の仕組みを、
アプリ固有のロジック(SSEキュー・DBパス・必須モデル一覧など)から切り離して
ここに集約したもの。gatewayや他のバックエンドの単体exe化でも同じ処理が
必要になるため、新しいランチャーを書くたびに再実装しないで済むようにする。

設計方針:
  - ロジックはlaunch_fastapi.pyのものをそのまま踏襲する(車輪の再発明をしない、
    かつ実際に配布実績のある枯れた実装であるため)。
  - アプリ固有の値(進捗の配信先、必須モデル一覧、crash logの保存フォルダ名等)は
    引数として渡す。グローバル状態を極力持たない。
  - ログ出力はデフォルトで標準出力(色付き)のみ。SSEなどで進捗を配信したい
    アプリは set_log_handler() で自分のキューに積むハンドラーを差し込める。
"""
from __future__ import annotations

import ctypes
import glob
import io
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import webbrowser
from typing import Callable, Optional

# ollama pull が出力する ANSI エスケープシーケンスを除去するための正規表現。
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

OLLAMA_DOWNLOAD_URL = "https://github.com/ollama/ollama/releases/latest/download/OllamaSetup.exe"


# ============================================================
# ログ(差し替え可能)
# ============================================================
# デフォルトは標準出力への色付き表示のみ。アプリ側でSSE配信などをしたい
# 場合は set_log_handler() で `(message, level, group) -> None` を渡す。
LogHandler = Callable[[str, str, Optional[str]], None]

_extra_log_handler: Optional[LogHandler] = None


def set_log_handler(handler: Optional[LogHandler]) -> None:
    """標準出力への出力に加えて呼び出す追加のログハンドラーを登録する。

    例: SSEキューに積んでフロントエンドのセットアップ画面に配信する。
    None を渡すと解除される。
    """
    global _extra_log_handler
    _extra_log_handler = handler


def log(message: str, level: str = "INFO", group: str | None = None) -> None:
    """コンソールに色付きでメッセージを出力し、必要なら追加ハンドラーにも渡す。

    group を指定すると、呼び出し側(SSE配信など)で「同じgroupの前回行を
    新しい内容で上書き」する用途に使える(ダウンロード進捗バー等)。
    """
    colors = {
        "INFO":    "\033[94m",
        "SUCCESS": "\033[92m",
        "WARNING": "\033[93m",
        "ERROR":   "\033[91m",
    }
    reset = "\033[0m"
    color = colors.get(level, "\033[94m")
    timestamp = time.strftime("%H:%M:%S")
    print(f"{color}[{timestamp}] {level:<8}{reset} {message}", flush=True)

    if _extra_log_handler is not None:
        try:
            _extra_log_handler(message, level, group)
        except Exception:
            pass  # ログ配信自体の失敗で起動処理を止めない


# ============================================================
# 文字列・数値ユーティリティ
# ============================================================

def strip_ansi(text: str) -> str:
    """ANSI エスケープシーケンスと制御文字をテキストから取り除く。"""
    text = _ANSI_ESCAPE_RE.sub("", text)
    text = re.sub(r"[\u2800-\u28FF]", "", text)  # スピナーのBraille文字
    text = text.replace("\x1b", "")
    return text.strip()


def format_bytes(bytes_size: int) -> str:
    """バイト数を人間が読みやすいフォーマットに変換する。"""
    for unit in ["B", "KB", "MB", "GB"]:
        if bytes_size < 1024:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.1f} TB"


# ============================================================
# PyInstaller / プロセス・ポート系ユーティリティ
# ============================================================

def base_path() -> str:
    """PyInstaller の一時展開先、または開発時のスクリプトディレクトリを返す。"""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def cleanup_old_meipass() -> None:
    """古い PyInstaller 一時フォルダ(_MEI*)を削除する。"""
    current = getattr(sys, "_MEIPASS", None)
    if current is None:
        return
    temp_dir = os.environ.get("TEMP") or os.environ.get("TMP") or ""
    if not temp_dir:
        return
    for folder in glob.glob(os.path.join(temp_dir, "_MEI*")):
        if os.path.abspath(folder) == os.path.abspath(current):
            continue
        try:
            shutil.rmtree(folder, ignore_errors=True)
        except Exception:
            pass


def kill_existing_process(port: int) -> None:
    """指定ポートを LISTEN しているプロセスを終了する(Windows専用、netstat/taskkillを使用)。"""
    my_pid = os.getpid()
    try:
        result = subprocess.run(["netstat", "-ano"], capture_output=True, text=True)
        target_pids: set[int] = set()
        for line in result.stdout.splitlines():
            if f":{port} " in line and "LISTENING" in line:
                parts = line.split()
                if not parts:
                    continue
                try:
                    pid = int(parts[-1])
                except ValueError:
                    continue
                if pid == 0 or pid == my_pid:
                    continue
                target_pids.add(pid)
        for pid in target_pids:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    except Exception:
        pass

    for _ in range(50):
        time.sleep(0.1)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.1)
                if s.connect_ex(("127.0.0.1", port)) != 0:
                    break
        except Exception:
            break


def wait_for_port(port: int, timeout: float = 30.0) -> bool:
    """ポートが Listen 状態になるまで待つ。timeout 秒以内に開けば True を返す。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def open_browser(url: str, port: int, timeout: float = 30.0) -> None:
    """指定ポートが応答するようになってからブラウザで url を開く。"""
    if wait_for_port(port, timeout=timeout):
        log(f"ブラウザを開いています: {url}", "INFO")
        webbrowser.open(url)


# ============================================================
# Ollama パス解決・状態確認
# ============================================================

def get_ollama_exe() -> str | None:
    """ollama の実行ファイルパスを返す。PATH → LOCALAPPDATA/Programs/Ollama/ の順で探す。"""
    exe = shutil.which("ollama")
    if exe:
        return exe
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        candidate = os.path.join(local_app_data, "Programs", "Ollama", "ollama.exe")
        if os.path.isfile(candidate):
            return candidate
    return None


def is_ollama_installed() -> bool:
    """Ollama が PATH または既定インストール先に存在するか確認する。"""
    return get_ollama_exe() is not None


def is_ollama_running(ollama_host: str = "http://localhost:11434") -> bool:
    """Ollama の HTTP API に疎通できるか確認する。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            host = ollama_host.replace("http://", "").replace("https://", "")
            hostname, _, port_str = host.partition(":")
            port = int(port_str) if port_str else 11434
            return s.connect_ex((hostname, port)) == 0
    except Exception:
        return False


# ============================================================
# ダウンロード・インストール
# ============================================================

def download_with_progress(url: str, filepath: str) -> bool:
    """ダウンロード進捗を表示しながらファイルをダウンロードする。

    PyInstaller 環境では certifi の証明書バンドルが同梱されないため、
    SSL コンテキストを明示的に作成して対応する。
    """
    try:
        log("Ollama セットアップファイルをダウンロード中...", "INFO")

        ctx: ssl.SSLContext | None = None
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
            log("  SSL: certifi 証明書を使用", "INFO")
        except ImportError:
            try:
                ctx = ssl.create_default_context()
                log("  SSL: OS 証明書ストアを使用", "INFO")
            except Exception:
                ctx = ssl._create_unverified_context()
                log("  SSL: 証明書検証をスキップ（フォールバック）", "WARNING")

        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
        urllib.request.install_opener(opener)

        last_update = time.time()
        last_percent = -1

        def _progress(block_num: int, block_size: int, total_size: int) -> None:
            nonlocal last_update, last_percent
            if total_size <= 0:
                log(f"  ダウンロード中... {format_bytes(block_num * block_size)}",
                    "INFO", group="ollama_installer_download")
                return
            downloaded = min(block_num * block_size, total_size)
            percent = int(100 * downloaded / total_size)
            now = time.time()
            if percent != last_percent and (now - last_update > 0.5 or percent == 100):
                bar_length = 30
                filled = int(bar_length * percent / 100)
                bar = "█" * filled + "░" * (bar_length - filled)
                log(f"  {bar} {percent}% ({format_bytes(downloaded)} / {format_bytes(total_size)})",
                    "INFO", group="ollama_installer_download")
                last_update = now
                last_percent = percent

        urllib.request.urlretrieve(url, filepath, _progress)
        log("ダウンロード完了", "SUCCESS")
        return True

    except Exception as e:
        log(f"ダウンロード失敗: {e}", "ERROR")
        return False


def show_message(title: str, message: str, error: bool = False) -> None:
    """Windows のメッセージボックス、または標準エラー出力にメッセージを表示する。"""
    try:
        icon = 0x10 if error else 0x40
        ctypes.windll.user32.MessageBoxW(0, message, title, icon)
    except Exception:
        level = "ERROR" if error else "INFO"
        log(f"[{title}] {message}", level)


def install_ollama() -> bool:
    """Ollama を公式サイトからダウンロードしてサイレントインストールする。"""
    log("=" * 60, "INFO")
    log("Ollama のインストールを開始します", "WARNING")
    log("=" * 60, "INFO")

    tmp_dir = tempfile.mkdtemp()
    installer = os.path.join(tmp_dir, "OllamaSetup.exe")

    log("[1/3] ダウンロード", "INFO")
    if not download_with_progress(OLLAMA_DOWNLOAD_URL, installer):
        show_message(
            "Ollama ダウンロード失敗",
            "Ollama のダウンロードに失敗しました。\n"
            "インターネット接続を確認してください。\n\n"
            "手動でインストール: https://ollama.com",
            error=True,
        )
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return False

    log("[2/3] Ollama をインストール中...", "INFO")

    _install_done = threading.Event()

    def _show_install_progress() -> None:
        start = time.time()
        max_sec = 300
        while not _install_done.wait(timeout=2.0):
            elapsed = int(time.time() - start)
            ratio = min(elapsed / max_sec, 1.0)
            bar_len = 30
            filled = int(bar_len * ratio)
            bar = "█" * filled + "░" * (bar_len - filled)
            m, s = divmod(elapsed, 60)
            log(f"  {bar} インストール中... {m}分{s:02d}秒経過",
                "INFO", group="ollama_install_progress")

    progress_thread = threading.Thread(target=_show_install_progress, daemon=True)
    progress_thread.start()

    try:
        result = subprocess.run(
            [installer, "/verysilent", "/norestart"], check=False, timeout=300,
        )
        _install_done.set()
        progress_thread.join(timeout=3)
        if result.returncode not in (0, 3010):  # 0=成功, 3010=成功(再起動推奨)
            log(f"インストール失敗（終了コード: {result.returncode}）", "ERROR")
            return False
        log("Ollama インストール完了", "SUCCESS")

    except subprocess.TimeoutExpired:
        _install_done.set()
        log("インストール処理がタイムアウトしました", "ERROR")
        return False
    except subprocess.CalledProcessError as e:
        _install_done.set()
        log(f"インストール失敗（終了コード: {e.returncode}）", "ERROR")
        return False
    except Exception as e:
        _install_done.set()
        log(f"インストール中にエラー: {e}", "ERROR")
        return False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    log("[3/3] インストール確認中...", "INFO")
    max_checks = 10
    for i in range(max_checks):
        time.sleep(1.0)
        filled = int(30 * (i + 1) / max_checks)
        bar = "█" * filled + "░" * (30 - filled)
        log(f"  {bar} 確認中 {i + 1}/{max_checks}", "INFO", group="ollama_verify_progress")
        if is_ollama_installed():
            log("Ollama のインストールが確認できました", "SUCCESS")
            log("=" * 60, "INFO")
            return True

    log("Ollama がインストールされていません（確認タイムアウト）", "ERROR")
    return False


def start_ollama_service() -> bool:
    """Ollama サービスをバックグラウンドで起動し、疎通確認するまで待つ。"""
    log("Ollama サービスを起動中...", "INFO")

    ollama_exe = get_ollama_exe()
    if ollama_exe is None:
        log("Ollama の実行ファイルが見つかりません", "ERROR")
        return False

    try:
        subprocess.Popen(
            [ollama_exe, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        log("Ollama プロセスを起動しました", "INFO")
    except Exception as e:
        log(f"Ollama プロセスの起動に失敗: {e}", "ERROR")
        return False

    log("Ollama が応答するまで待機中...", "INFO")
    for i in range(60):
        time.sleep(0.5)
        if is_ollama_running():
            elapsed = (i + 1) * 0.5
            log(f"Ollama が起動しました（{elapsed:.1f}秒）", "SUCCESS")
            return True
        if (i + 1) % 20 == 0:
            log(f"  待機中... {(i + 1) * 0.5:.0f}秒経過", "INFO")

    log("Ollama が起動できませんでした（タイムアウト）", "ERROR")
    return False


# ============================================================
# モデルの確認・自動インストール
# ============================================================

def get_installed_models() -> list[str]:
    """Ollama にインストール済みのモデル一覧を取得する。"""
    ollama_exe = get_ollama_exe()
    if ollama_exe is None:
        log("ollama コマンドが見つかりません（モデル一覧取得スキップ）", "WARNING")
        return []
    try:
        result = subprocess.run([ollama_exe, "list"], capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return []
        models = []
        for line in result.stdout.splitlines()[1:]:
            parts = line.split()
            if parts:
                models.append(parts[0])
        return models
    except Exception as e:
        log(f"モデル一覧の取得に失敗: {e}", "WARNING")
        return []


def pull_model(model_name: str) -> bool:
    """Ollama でモデルをダウンロードする(ollama pull の進捗をリアルタイム表示)。"""
    ollama_exe = get_ollama_exe()
    if ollama_exe is None:
        log(f"✗ ollama コマンドが見つかりません（{model_name} スキップ）", "ERROR")
        return False

    log(f"モデル '{model_name}' をダウンロード中...", "INFO")

    try:
        if sys.platform == "win32":
            _si = subprocess.STARTUPINFO()
            _si.dwFlags = subprocess.STARTF_USESHOWWINDOW
            _si.wShowWindow = 6  # SW_MINIMIZE
            _startupinfo = _si
        else:
            _startupinfo = None

        process = subprocess.Popen(
            [ollama_exe, "pull", model_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            startupinfo=_startupinfo,
        )

        buf = bytearray()
        last_logged_percent = -1
        last_plain_line = ""

        def _emit(raw_line: str) -> None:
            nonlocal last_logged_percent, last_plain_line
            line = strip_ansi(raw_line)
            if not line:
                return
            if "%" in line:
                try:
                    pct_str = [t for t in line.split() if t.endswith("%")]
                    pct = int(pct_str[0].rstrip("%")) if pct_str else -1
                except Exception:
                    pct = -1
                if pct == 100 or (pct >= 0 and pct // 5 != last_logged_percent // 5):
                    log(f"  {line}", "INFO", group=f"pull:{model_name}")
                    last_logged_percent = pct
            else:
                if line == last_plain_line:
                    return
                last_plain_line = line
                log(f"  {line}", "INFO")

        while True:
            ch = process.stdout.read(1)
            if not ch:
                if buf.strip():
                    _emit(buf.decode("utf-8", errors="replace"))
                break
            if ch == b"\n":
                raw_line = buf.decode("utf-8", errors="replace")
                buf.clear()
                _emit(raw_line)
            elif ch == b"\r":
                buf.clear()
            else:
                buf.extend(ch)

        process.wait(timeout=3600)

        if process.returncode == 0:
            log(f"✓ モデル '{model_name}' のダウンロード完了", "SUCCESS")
            return True
        else:
            log(f"✗ モデル '{model_name}' のダウンロード失敗（コード: {process.returncode}）", "ERROR")
            return False

    except subprocess.TimeoutExpired:
        log(f"✗ モデル '{model_name}' のダウンロードがタイムアウト", "ERROR")
        return False
    except Exception as e:
        log(f"✗ モデル '{model_name}' のダウンロード中にエラー: {e}", "ERROR")
        return False


def ensure_models(required_models: list[str]) -> None:
    """必要なモデルがインストール済みか確認し、不足していればダウンロードする。"""
    log("=" * 60, "INFO")
    log("必要なモデルを確認しています", "INFO")
    log("=" * 60, "INFO")

    installed_models = get_installed_models()
    log(f"インストール済みモデル: {', '.join(installed_models) if installed_models else 'なし'}", "INFO")

    models_to_download = []
    for model in required_models:
        base_name = model.split(":")[0]
        is_installed = any(base_name in m for m in installed_models)
        if is_installed:
            log(f"✓ モデル '{model}' は既にインストール済み", "SUCCESS")
        else:
            log(f"✗ モデル '{model}' がインストールされていません", "WARNING")
            models_to_download.append(model)

    if models_to_download:
        log("", "INFO")
        log(f"{len(models_to_download)} 個のモデルをダウンロード開始...", "WARNING")
        log("  （数GB のダウンロードのため、数分～十数分かかります）", "INFO")
        log("", "INFO")
        for i, model in enumerate(models_to_download, 1):
            log(f"[{i}/{len(models_to_download)}] {model}", "INFO")
            success = pull_model(model)
            if not success:
                log(f"⚠️  モデル '{model}' のダウンロードに失敗しました", "WARNING")
                log(f"   手動でダウンロード: ollama pull {model}", "INFO")
            log("", "INFO")
        log("=" * 60, "INFO")
    else:
        log("✓ すべての必要なモデルがインストール済みです", "SUCCESS")
        log("=" * 60, "INFO")


def ensure_ollama(required_models: list[str], on_error: Callable[[], None] | None = None) -> bool:
    """Ollama のインストール・起動・モデル準備を保証する。

    成功したら True を返す。失敗した場合、on_error があれば呼び出す
    (例: 呼び出し元のsetup_error相当のEventをsetする)。
    """
    if is_ollama_installed():
        log("✓ Ollama はインストール済みです", "SUCCESS")
    else:
        log("✗ Ollama がインストールされていません", "WARNING")
        if not install_ollama():
            log("Ollama のインストールに失敗しました（続行）", "WARNING")
            if on_error:
                on_error()
            return False

    if is_ollama_running():
        log("✓ Ollama は既に起動しています", "SUCCESS")
    else:
        log("✗ Ollama が起動していません", "WARNING")
        if not start_ollama_service():
            show_message(
                "Ollama 起動エラー",
                "Ollama サービスの起動に失敗しました。\n"
                "Ollama が正しくインストールされているか確認するか、\n"
                "手動で Ollama を起動してからアプリを再起動してください。",
                error=True,
            )
            if on_error:
                on_error()
            return False

    ensure_models(required_models)
    return True


# ============================================================
# Windows コンソール・stdio対策
# ============================================================

def hide_console_window() -> None:
    """コンソールウィンドウを非表示にする(このプロセス専用の新規コンソールの場合のみ)。

    console=True でビルドしつつ見た目だけコンソールを隠すための対策。
    詳しい経緯はlaunch_fastapi.pyの元コメントを参照(console=Falseビルドで
    特定のWindows環境で原因不明の即時terminationが起きたための回避策)。
    """
    if sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        pids = (ctypes.c_uint * 4)()
        count = kernel32.GetConsoleProcessList(pids, 4)
        if count <= 1:
            hwnd = kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass


def fix_stdio() -> None:
    """PyInstaller 環境で stdout/stderr が None になる場合の対策。"""
    if sys.stdout is None:
        sys.stdout = io.TextIOWrapper(open(os.devnull, "wb"), encoding="utf-8", errors="replace")
    if sys.stderr is None:
        sys.stderr = io.TextIOWrapper(open(os.devnull, "wb"), encoding="utf-8", errors="replace")


def suppress_child_console() -> None:
    """Windows で子プロセスにコンソールウィンドウが出ないようにする(全Popenのデフォルトを上書き)。"""
    if sys.platform != "win32":
        return
    try:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE
        subprocess._default_startupinfo = startupinfo  # type: ignore[attr-defined]
    except Exception:
        pass


# ============================================================
# クラッシュログ
# ============================================================

def crash_log_path(app_folder_name: str) -> str:
    """クラッシュログの保存先。%APPDATA%\\<app_folder_name>\\crash.log に置く。"""
    app_data = os.environ.get("APPDATA") or os.path.expanduser("~")
    log_dir = os.path.join(app_data, app_folder_name)
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, "crash.log")


def write_crash_log(app_folder_name: str, context: str, exc: BaseException) -> None:
    """例外の詳細(トレースバック込み)をファイルに書き出す。

    console=False相当のビルドでは標準出力・標準エラーが読めないため、
    起動時の問題を調査できるよう必ずファイルに残す。
    """
    path = crash_log_path(app_folder_name)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("=" * 60 + "\n")
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {context}\n")
            f.write("=" * 60 + "\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
            f.write("\n")
    except Exception:
        pass

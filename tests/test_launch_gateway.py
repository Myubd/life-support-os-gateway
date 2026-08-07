"""
launch_gateway.py の回帰テスト。

実際にuvicornやOllamaを起動するテストはしない(重い・環境依存のため)。
ここでは、切り出しの過程で壊れやすい「純粋なロジック部分」
(トークンの生成・永続化、環境変数の組み立て、プロセスの起動/終了管理)を
中心に、subprocess.Popenを偽物に差し替えて確認する。
"""
import json
import os
import signal
import sys

import pytest

import launch_gateway as lg


# ── GATEWAY_AUTH_TOKEN の生成・永続化 ────────────────────────

def test_get_or_create_auth_token_creates_new_token(tmp_path):
    token_path = tmp_path / "auth_token.json"
    token = lg.get_or_create_auth_token(token_path)

    assert len(token) > 20  # secrets.token_urlsafe(32)相当の長さ
    assert token_path.exists()
    assert json.loads(token_path.read_text())["token"] == token


def test_get_or_create_auth_token_reuses_existing_token(tmp_path):
    token_path = tmp_path / "auth_token.json"
    first = lg.get_or_create_auth_token(token_path)
    second = lg.get_or_create_auth_token(token_path)

    assert first == second  # 2回目は同じトークンを読み込む(再ログイン不要にするため)


def test_get_or_create_auth_token_regenerates_when_file_corrupted(tmp_path):
    token_path = tmp_path / "auth_token.json"
    token_path.write_text("not valid json{{{", encoding="utf-8")

    token = lg.get_or_create_auth_token(token_path)
    assert len(token) > 20  # 壊れていても例外を出さず新規生成すること


def test_get_or_create_auth_token_regenerates_when_token_field_empty(tmp_path):
    token_path = tmp_path / "auth_token.json"
    token_path.write_text(json.dumps({"token": ""}), encoding="utf-8")

    token = lg.get_or_create_auth_token(token_path)
    assert len(token) > 20


# ── 環境変数の組み立て ────────────────────────────────────────

def test_build_service_env_sets_auth_token_and_resolves_db_path(tmp_path):
    spec = lg.ServiceSpec(
        name="archlife_backend", port=8080,
        dev_relative_dir="../archlife/archlife-fastapi",
        extra_env={"ARCHLIFE_DB_PATH": "archlife.db", "OLLAMA_URL": "http://localhost:11434"},
    )
    env = lg.build_service_env(spec, base_env={"PATH": "/usr/bin"}, auth_token="tok123", data_dir=tmp_path)

    assert env["GATEWAY_AUTH_TOKEN"] == "tok123"
    assert env["ARCHLIFE_DB_PATH"] == str(tmp_path / "archlife.db")  # data_dir配下の絶対パスに解決される
    assert env["OLLAMA_URL"] == "http://localhost:11434"  # *_DB_PATH以外はそのまま
    assert env["PATH"] == "/usr/bin"  # base_envは引き継がれる


def test_build_service_env_does_not_set_shared_core_db_path(tmp_path):
    # LOCAL_AI_CORE_DB_PATH/DEVICE_IDENTITY_PATHは意図的に設定しない
    # (local_ai_core.pathsの共有フォールバックに任せるため)
    spec = lg.ServiceSpec(name="x", port=1, dev_relative_dir=".", extra_env={})
    env = lg.build_service_env(spec, base_env={}, auth_token="t", data_dir=tmp_path)

    assert "LOCAL_AI_CORE_DB_PATH" not in env
    assert "LOCAL_AI_CORE_DEVICE_IDENTITY_PATH" not in env


def test_build_service_env_does_not_mutate_base_env(tmp_path):
    base_env = {"PATH": "/usr/bin"}
    spec = lg.ServiceSpec(name="x", port=1, dev_relative_dir=".", extra_env={"FOO_DB_PATH": "foo.db"})
    lg.build_service_env(spec, base_env=base_env, auth_token="t", data_dir=tmp_path)

    assert base_env == {"PATH": "/usr/bin"}  # 呼び出し元の辞書を書き換えていないこと


def test_build_gateway_env_includes_existing_frontend_dirs_only(tmp_path):
    existing = tmp_path / "archlife_dist"
    existing.mkdir()
    missing = str(tmp_path / "does_not_exist")

    env = lg.build_gateway_env(
        base_env={},
        auth_token="tok",
        frontend_dists={
            "ARCHLIFE_FRONTEND_DIST": str(existing),
            "INTERVIEW_FRONTEND_DIST": missing,
        },
    )

    assert env["ARCHLIFE_FRONTEND_DIST"] == str(existing)
    assert "INTERVIEW_FRONTEND_DIST" not in env  # 存在しないパスは設定しない
    assert env["GATEWAY_AUTH_TOKEN"] == "tok"


def test_uvicorn_args_uses_correct_port_and_host():
    args = lg.uvicorn_args(8080)
    assert "--host" in args
    assert "127.0.0.1" in args
    assert "--port" in args
    assert "8080" in args
    assert "main:app" in args


# ── プロセス管理(偽のPopenを注入) ─────────────────────────────

class _FakeProcess:
    """subprocess.Popen の最小限の偽物。"""

    def __init__(self, args, cwd=None, env=None):
        self.args = args
        self.cwd = cwd
        self.env = env
        self._terminated = False
        self._killed = False
        self._returncode = None

    def poll(self):
        return self._returncode

    def terminate(self):
        self._terminated = True
        self._returncode = 0  # 素直に終了するプロセスを模す

    def kill(self):
        self._killed = True
        self._returncode = -9

    def wait(self, timeout=None):
        if self._returncode is None:
            raise TimeoutError("still running")
        return self._returncode


class _HangingFakeProcess(_FakeProcess):
    """terminate() しても終了しない(kill()が必要な)プロセスを模す。"""

    def terminate(self):
        self._terminated = True  # returncode はセットしない = 終了しない

    def wait(self, timeout=None):
        if not self._killed:
            raise TimeoutError("still running")
        return self._returncode


def test_process_manager_start_records_process():
    fake_processes = []

    def _fake_popen(args, cwd=None, env=None):
        proc = _FakeProcess(args, cwd=cwd, env=env)
        fake_processes.append(proc)
        return proc

    manager = lg.ProcessManager(popen_fn=_fake_popen)
    manager.start("svc1", ["python", "-m", "uvicorn"], cwd="/tmp", env={"A": "1"})

    assert len(manager.processes) == 1
    name, proc = manager.processes[0]
    assert name == "svc1"
    assert proc.cwd == "/tmp"
    assert proc.env == {"A": "1"}


def test_process_manager_terminate_all_terminates_every_process():
    fake_processes = []

    def _fake_popen(args, cwd=None, env=None):
        proc = _FakeProcess(args, cwd=cwd, env=env)
        fake_processes.append(proc)
        return proc

    manager = lg.ProcessManager(popen_fn=_fake_popen)
    manager.start("svc1", ["cmd1"], cwd=".", env={})
    manager.start("svc2", ["cmd2"], cwd=".", env={})

    manager.terminate_all(timeout=1.0)

    assert all(p._terminated for p in fake_processes)


def test_process_manager_kills_hanging_process(monkeypatch):
    fake_processes = []

    def _fake_popen(args, cwd=None, env=None):
        proc = _HangingFakeProcess(args, cwd=cwd, env=env)
        fake_processes.append(proc)
        return proc

    manager = lg.ProcessManager(popen_fn=_fake_popen)
    manager.start("stuck_svc", ["cmd"], cwd=".", env={})

    manager.terminate_all(timeout=0.2)

    assert fake_processes[0]._killed is True  # terminateで終わらないのでkillされる


def test_process_manager_skips_already_terminated_process():
    fake_processes = []

    def _fake_popen(args, cwd=None, env=None):
        proc = _FakeProcess(args, cwd=cwd, env=env)
        proc._returncode = 0  # 既に終了済み
        fake_processes.append(proc)
        return proc

    manager = lg.ProcessManager(popen_fn=_fake_popen)
    manager.start("already_dead", ["cmd"], cwd=".", env={})

    manager.terminate_all(timeout=0.5)  # 例外が出ずに終わることだけを確認
    assert fake_processes[0]._terminated is False  # 既に死んでいるのでterminateは呼ばれない


# ── BACKEND_SERVICES の設定に対する健全性チェック ────────────────

def test_backend_services_have_unique_ports():
    ports = [spec.port for spec in lg.BACKEND_SERVICES]
    assert len(ports) == len(set(ports))
    assert lg.GATEWAY_PORT not in ports


def test_backend_services_have_unique_names():
    names = [spec.name for spec in lg.BACKEND_SERVICES]
    assert len(names) == len(set(names))


# ── frozen(exe化後)モードの起動コマンド・環境変数解決 ─────────────

def test_backend_services_all_have_frozen_exe_path():
    for spec in lg.BACKEND_SERVICES:
        assert spec.frozen_exe_relative_path, f"{spec.name} に frozen_exe_relative_path が無い"
        assert spec.frozen_exe_relative_path.startswith("backends/")


def test_resolve_service_command_dev_mode_uses_uvicorn():
    spec = lg.BACKEND_SERVICES[0]
    args, cwd = lg.resolve_service_command(spec, launcher_dir="/app/gateway", frozen=False)

    assert "uvicorn" in args
    assert cwd == os.path.normpath(os.path.join("/app/gateway", spec.dev_relative_dir))


def test_resolve_service_command_frozen_mode_uses_exe_directly():
    spec = lg.BACKEND_SERVICES[0]
    args, cwd = lg.resolve_service_command(spec, launcher_dir="C:/LifeSupportOS", frozen=True)

    assert len(args) == 1
    assert args[0].endswith(spec.frozen_exe_relative_path.replace("/", os.sep))
    # cwdは実行ファイル自身のディレクトリ(相対import等が期待する場所)
    assert cwd == os.path.dirname(args[0])


def test_build_frozen_service_env_sets_port_and_data_dir(tmp_path):
    env = lg.build_frozen_service_env(base_env={"PATH": "/usr/bin"}, auth_token="tok", port=8080, data_dir=tmp_path)

    assert env["GATEWAY_AUTH_TOKEN"] == "tok"
    assert env["PORT"] == "8080"
    assert env["DATA_DIR"] == str(tmp_path)
    assert env["PATH"] == "/usr/bin"


def test_build_frozen_service_env_does_not_mutate_base_env(tmp_path):
    base_env = {"PATH": "/usr/bin"}
    lg.build_frozen_service_env(base_env=base_env, auth_token="tok", port=8080, data_dir=tmp_path)
    assert base_env == {"PATH": "/usr/bin"}


def test_is_frozen_false_in_normal_python(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert lg.is_frozen() is False


def test_is_frozen_true_when_sys_frozen_set(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert lg.is_frozen() is True


def test_resolve_frontend_dists_frozen_uses_internal_subfolder():
    # PyInstaller 6.xのonedirビルドではdatasが"_internal/"配下に置かれる
    # (実際にビルドしたexeで確認した実挙動。ここで固定しておく)。
    dists = lg.resolve_frontend_dists("C:/LifeSupportOS", frozen=True)

    assert dists["ARCHLIFE_FRONTEND_DIST"] == os.path.join("C:/LifeSupportOS", "_internal", "frontend_dist_archlife")
    assert dists["INTERVIEW_FRONTEND_DIST"] == os.path.join("C:/LifeSupportOS", "_internal", "frontend_dist_interview")


def test_resolve_frontend_dists_dev_mode_uses_sibling_repos():
    dists = lg.resolve_frontend_dists("/app/gateway", frozen=False)

    assert dists["ARCHLIFE_FRONTEND_DIST"].endswith(
        os.path.normpath("archlife/archlife-frontend/dist"))
    assert dists["INTERVIEW_FRONTEND_DIST"].endswith(
        os.path.normpath("interview_app/react-fastapi/frontend/dist-gateway"))

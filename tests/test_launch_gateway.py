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

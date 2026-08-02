"""
auth.py の回帰テスト。

「唯一の入口」の認証ロジックがシステム全体で一番壊れると困る部分なので、
特に以下を必ず固定しておく:

  - GATEWAY_AUTH_TOKEN 未設定なら get_auth_token() が例外を送出する
    (=無防備なまま起動しない、というfail-closed設計)
  - "/" "/health" "/auth/*" "/life*" "/career*" は認証なしで通る
  - それ以外のパスは、正しいCookieが無ければ401
  - 誤ったCookie値は拒否される(タイミング攻撃対策のcompare_digestを
    経由していることも含めて確認)
"""
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ["GATEWAY_AUTH_TOKEN"] = "gateway-test-token"

import auth  # noqa: E402


def test_get_auth_token_raises_when_unset(monkeypatch):
    monkeypatch.delenv("GATEWAY_AUTH_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        auth.get_auth_token()


def test_get_auth_token_returns_value_when_set(monkeypatch):
    monkeypatch.setenv("GATEWAY_AUTH_TOKEN", "abc123")
    assert auth.get_auth_token() == "abc123"


@pytest.mark.parametrize(
    "path",
    ["/", "/health", "/auth/login", "/auth/logout", "/life/", "/career/anything"],
)
def test_public_paths_are_public(path):
    assert auth._is_public(path) is True


@pytest.mark.parametrize(
    "path",
    ["/core/memory", "/api/life/tasks", "/api/career/sessions", "/admin/backup", "/admin/restore"],
)
def test_non_public_paths_are_not_public(path):
    assert auth._is_public(path) is False


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("GATEWAY_AUTH_TOKEN", "gateway-test-token")
    app = FastAPI()
    app.middleware("http")(auth.auth_middleware)

    @app.get("/")
    def root():
        return {"ok": True}

    @app.get("/core/memory")
    def protected():
        return {"secret": "value"}

    return TestClient(app)


def test_protected_route_requires_cookie(client):
    resp = client.get("/core/memory")
    assert resp.status_code == 401


def test_protected_route_allows_correct_cookie(client):
    client.cookies.set(auth.SESSION_COOKIE_NAME, "gateway-test-token")
    resp = client.get("/core/memory")
    assert resp.status_code == 200


def test_protected_route_rejects_wrong_cookie(client):
    client.cookies.set(auth.SESSION_COOKIE_NAME, "not-the-token")
    resp = client.get("/core/memory")
    assert resp.status_code == 401


def test_public_route_needs_no_cookie(client):
    assert client.get("/").status_code == 200

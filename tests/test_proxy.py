"""
main.py の _proxy() ヘルパーの回帰テスト。

health-support/study-supportに追加したservice_auth.pyは、「gateway経由の
リクエストならCookieがそのまま転送されてくる」ことを前提にしている。
その前提が崩れると、gateway経由の正規リクエストまで401になってしまうため、
_proxy() がCookieヘッダーを転送することを明示的に固定しておく。

あわせて、hop-by-hopヘッダー(Content-Lengthなど)は転送先で再計算される
必要があるため、意図的に除外されていることも確認する。
"""
import os

os.environ.setdefault("GATEWAY_AUTH_TOKEN", "t")
os.environ.setdefault("LOCAL_AI_CORE_DB_PATH", "/tmp/proxy_test_core.db")
os.environ.setdefault("LOCAL_AI_CORE_DEVICE_IDENTITY_PATH", "/tmp/proxy_test_device.json")

import httpx
import pytest
from fastapi import Request

import main as gateway_main


class _FakeResponse:
    def __init__(self, status_code=200, headers=None, content=b'{"ok": true}'):
        self.status_code = status_code
        self.headers = headers or {"content-type": "application/json"}
        self.content = content


class _FakeAsyncClient:
    """httpx.AsyncClientの代わりに使い、送信されたheadersを記録するだけの偽物。"""

    captured_headers = None
    captured_url = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def request(self, method, url, headers=None, params=None, content=None):
        _FakeAsyncClient.captured_headers = headers
        _FakeAsyncClient.captured_url = url
        return _FakeResponse()


def _make_request(path: str, cookie_header: str) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [
            (b"cookie", cookie_header.encode()),
            (b"content-length", b"0"),
            (b"host", b"localhost:3000"),
        ],
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


@pytest.mark.anyio
async def test_proxy_forwards_cookie_header(monkeypatch):
    monkeypatch.setattr(gateway_main.httpx, "AsyncClient", _FakeAsyncClient)
    request = _make_request("/api/health/logs", "gw_session=abc123")

    await gateway_main._proxy(request, "http://health_support:8200", "/api/health")

    assert _FakeAsyncClient.captured_url == "http://health_support:8200/logs"
    assert _FakeAsyncClient.captured_headers.get("cookie") == "gw_session=abc123"
    # hop-by-hopヘッダーは転送しない(転送先で再計算されるべきもののため)
    assert "content-length" not in _FakeAsyncClient.captured_headers
    assert "host" not in _FakeAsyncClient.captured_headers


@pytest.mark.anyio
async def test_proxy_returns_502_when_upstream_unreachable(monkeypatch):
    class _UnreachableClient(_FakeAsyncClient):
        async def request(self, *args, **kwargs):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(gateway_main.httpx, "AsyncClient", _UnreachableClient)
    request = _make_request("/api/health/logs", "gw_session=abc123")

    response = await gateway_main._proxy(request, "http://health_support:8200", "/api/health")

    assert response.status_code == 502


@pytest.fixture
def anyio_backend():
    return "asyncio"

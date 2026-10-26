import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(tmp_db):
    with TestClient(app) as c:
        yield c


def test_unconfigured_returns_503(client, monkeypatch):
    monkeypatch.delenv("CIVICPROCUREBOT_API_KEY", raising=False)
    assert client.get("/shortlist/today", params={"firm_id": "f1"}).status_code == 503


def test_proxies_with_api_key(client, monkeypatch):
    monkeypatch.setenv("CIVICPROCUREBOT_API_BASE", "http://bot.test/")
    monkeypatch.setenv("CIVICPROCUREBOT_API_KEY", "k-123")
    seen = {}

    def fake_get(url, params, headers, timeout):
        seen.update(url=url, params=params, headers=headers)
        return httpx.Response(200, json={"matches": [1, 2]}, request=httpx.Request("GET", url))

    monkeypatch.setattr("app.shortlist.httpx.get", fake_get)
    r = client.get("/shortlist/today", params={"firm_id": "f1"})
    assert r.status_code == 200 and r.json() == {"matches": [1, 2]}
    assert seen["url"] == "http://bot.test/shortlist/today"
    assert seen["params"] == {"firm_id": "f1"} and seen["headers"]["X-API-Key"] == "k-123"


def test_upstream_auth_failure_is_masked_as_502(client, monkeypatch):
    monkeypatch.setenv("CIVICPROCUREBOT_API_BASE", "http://bot.test")
    monkeypatch.setenv("CIVICPROCUREBOT_API_KEY", "k")
    monkeypatch.setattr(
        "app.shortlist.httpx.get", lambda url, **kw: httpx.Response(401, request=httpx.Request("GET", url))
    )
    assert client.get("/shortlist/today", params={"firm_id": "f1"}).status_code == 502

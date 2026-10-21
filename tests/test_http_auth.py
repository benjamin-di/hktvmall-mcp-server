"""Bearer auth through the real HTTP app (same options ``serve`` uses); /health is open."""

import os
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from hktvmall_mcp import server
from hktvmall_mcp.auth import AuthConfigError, bearer_tokens, require_bearer

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
}
TOOLS = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}


def _app(service, monkeypatch):
    monkeypatch.setenv("MCP_BEARER_TOKEN", "first-test-token-0001, second-test-token-0002")
    return server.create_server(service, auth=require_bearer()).http_app(**server.HTTP_OPTIONS)


def test_mcp_requires_a_bearer_and_health_is_open(service, monkeypatch):
    headers = {"Accept": "application/json, text/event-stream"}
    with TestClient(_app(service, monkeypatch)) as client:
        assert client.post("/mcp", headers=headers, json=INITIALIZE).status_code == 401
        wrong = {**headers, "Authorization": "Bearer wrong"}
        assert client.post("/mcp", headers=wrong, json=INITIALIZE).status_code == 401
        good = {**headers, "Authorization": "Bearer first-test-token-0001"}
        response = client.post("/mcp", headers=good, json=INITIALIZE)
        assert response.status_code == 200, response.text
        assert "mcp-session-id" not in response.headers  # stateless
        assert response.headers["content-type"].startswith("application/json")
        good["mcp-protocol-version"] = "2025-03-26"
        for token in ("first-test-token-0001", "second-test-token-0002"):  # either of a rotation pair
            listed = client.post("/mcp", json=TOOLS, headers={**good, "Authorization": f"Bearer {token}"})
            assert listed.status_code == 200
            assert "hktv_search" in {t["name"] for t in listed.json()["result"]["tools"]}
        # Host headers of reverse proxies and in-network health checks are not rejected.
        assert client.post("/mcp", json=TOOLS, headers={**good, "Host": "hktvmall-mcp:8000"}).status_code == 200
        health = client.get("/health")
        assert health.status_code == 200 and health.json() == {"status": "healthy"}


def test_serving_http_without_a_good_token_refuses(monkeypatch):
    for value in (None, "", " , ", "short"):
        if value is None:
            monkeypatch.delenv("MCP_BEARER_TOKEN", raising=False)
        else:
            monkeypatch.setenv("MCP_BEARER_TOKEN", value)
        with pytest.raises(AuthConfigError):
            require_bearer()
    assert bearer_tokens({"MCP_BEARER_TOKEN": " a-token , b-token ,"}) == ["a-token", "b-token"]


def test_settings_defaults_and_validation():
    from hktvmall_mcp.config import ConfigError, Settings

    required = {"HKTVMALL_STATE_DIR": "/state"}
    s = Settings.from_env(required)
    assert s.state_dir == Path("/state") and s.snapshot_path is None and s.session_path.name == "session.json"
    assert Settings.from_env({}).state_dir.name == "hktvmall-mcp"  # the per-user state folder
    source = os.path.abspath("/session/snapshot.json")  # absolute on every platform (a drive letter on Windows)
    snapshot = {**required, "HKTVMALL_SESSION_SNAPSHOT": source, "HKTVMALL_EXPECTED_EMAIL": "a@b.c"}
    assert Settings.from_env(snapshot).expected_email == "a@b.c"
    with pytest.raises(ConfigError):  # the two go together
        Settings.from_env({**required, "HKTVMALL_SESSION_SNAPSHOT": source})
    assert (s.daily_budget, s.keepalive_seconds) == (2000, 6 * 3600)
    with pytest.raises(ConfigError):
        Settings.from_env({**required, "HKTVMALL_DAILY_BUDGET": "lots"})
    with pytest.raises(ConfigError):
        Settings.from_env({**required, "HKTVMALL_KEEPALIVE_SECONDS": "5"})

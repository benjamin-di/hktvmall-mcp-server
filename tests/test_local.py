"""Local use: the default account's own session file, several processes sharing one state folder, the command line
and the stdio transport. Nothing here contacts HKTVmall."""

from __future__ import annotations

import http.cookiejar
import json
import os
import subprocess
import sys

import pytest

from hktvmall_mcp import __version__, cli
from hktvmall_mcp.config import Settings
from hktvmall_mcp.errors import HktvError
from hktvmall_mcp.server import build_accounts
from hktvmall_mcp.session import LocalSession, to_jar
from hktvmall_mcp.state import StateStore

EMAIL = "shopper@example.com"


def cookies(value: str = "s-1") -> list[dict]:
    return [
        {
            "name": "JSESSIONID",
            "value": value,
            "domain": "www.hktvmall.com",
            "path": "/",
            "secure": True,
            "expires": None,
        }
    ]


def test_no_file_is_not_signed_in_and_a_login_is_adopted_without_a_restart(tmp_path):
    session = LocalSession(tmp_path / "session.json")
    with pytest.raises(HktvError) as err:
        session.checkout_client()
    assert err.value.code == "not_signed_in" and session.never_signed_in()
    assert session.write(EMAIL, "UA", cookies("first")) == 1
    generation, jar, user_agent = session.checkout_client()
    assert (generation, user_agent, {c.name: c.value for c in jar}["JSESSIONID"]) == (1, "UA", "first")
    assert session.expected_email == EMAIL and session.account_email == EMAIL
    if os.name != "nt":
        assert (tmp_path / "session.json").stat().st_mode & 0o777 == 0o600
    # A second login (here: another account) raises the generation and is adopted on the next call.
    assert LocalSession(tmp_path / "session.json").write("other@example.com", "UA2", cookies("second")) == 2
    assert session.checkout_client()[0] == 2 and session.expected_email == "other@example.com"


def test_cookies_hktvmall_replaces_are_written_back_unless_a_newer_login_won(tmp_path):
    path = tmp_path / "session.json"
    session = LocalSession(path)
    session.write(EMAIL, "UA", cookies("old"))
    generation, jar, _ = session.checkout_client()
    jar.set_cookie(_cookie("access_token", "minted"))
    session.store_jar(jar, generation)
    names = {c["name"]: c["value"] for c in json.loads(path.read_text(encoding="utf-8"))["cookies"]}
    assert names == {"JSESSIONID": "old", "access_token": "minted"}
    LocalSession(path).write(EMAIL, "UA", cookies("newer-login"))  # generation 2 on disk
    session.store_jar(to_jar(cookies("stale")), generation)  # this process still holds generation 1
    assert json.loads(path.read_text(encoding="utf-8"))["generation"] == 2
    assert {c["value"] for c in json.loads(path.read_text(encoding="utf-8"))["cookies"]} == {"newer-login"}


def test_a_malformed_file_is_invalid_and_logout_removes_it(tmp_path):
    path = tmp_path / "session.json"
    path.write_text('{"schema": 1, "email": "not-an-email"}', encoding="utf-8")
    with pytest.raises(HktvError) as err:
        LocalSession(path).checkout_client()
    assert err.value.code == "session_source_invalid"
    assert LocalSession(path).clear() and not path.exists() and not LocalSession(path).clear()


def test_without_a_snapshot_the_default_account_owns_its_session_and_waits_for_a_login(tmp_path):
    service = build_accounts(Settings(state_dir=tmp_path)).default
    assert isinstance(service.session, LocalSession)
    assert service.keepalive_due(None) is False  # nothing to keep alive before a login
    assert service.status(check=False)["signed_in"] is False
    assert service.session.canary_error() == "not_signed_in"


def test_processes_sharing_a_state_folder_share_budget_pause_and_errors(tmp_path):
    clock = lambda: 1_900_000_000.0  # noqa: E731
    first = StateStore(tmp_path, budget=3, clock=clock)
    second = StateStore(tmp_path, budget=3, clock=clock)  # another process's view of the same files
    first.take()
    second.take()
    first.take()
    with pytest.raises(HktvError) as err:
        second.take()
    assert err.value.code == "budget_exhausted"
    second.pause("429 www.hktvmall.com")
    with pytest.raises(HktvError) as err:
        first.check_pause()
    assert err.value.code == "blocked"
    first.fail("hktv_get_cart", "upstream_error")
    assert second.snapshot()["last_error"]["tool"] == "hktv_get_cart"
    second.succeed("hktv_get_cart")
    assert first.snapshot()["last_error"] is None


def test_status_logout_login_and_serve_from_the_command_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HKTVMALL_STATE_DIR", str(tmp_path))
    for name in ("HKTVMALL_SESSION_SNAPSHOT", "HKTVMALL_EXPECTED_EMAIL", "MCP_BEARER_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert cli.main(["status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["signed_in"] is False and status["account"] == "default" and status["state_dir"] == str(tmp_path)
    assert cli.main(["logout"]) == 0 and "Nothing was saved" in capsys.readouterr().out
    assert cli.main(["serve", "--port", "1"]) == 1  # refuses HTTP without a bearer, before listening
    assert "MCP_BEARER_TOKEN" in capsys.readouterr().err
    assert cli.main(["health", "--url", "http://127.0.0.1:9/health"]) == 1
    monkeypatch.setenv("HKTVMALL_SESSION_SNAPSHOT", str(tmp_path / "elsewhere" / "snapshot.json"))
    monkeypatch.setenv("HKTVMALL_EXPECTED_EMAIL", EMAIL)
    assert cli.main(["login"]) == 2  # a snapshot's publisher signs in, not this command
    assert "HKTVMALL_SESSION_SNAPSHOT" in capsys.readouterr().err


def test_http_serves_without_an_access_log(tmp_path, monkeypatch):
    from fastmcp import FastMCP

    from hktvmall_mcp import server

    seen = {}
    monkeypatch.setattr(FastMCP, "run", lambda self, **kwargs: seen.update(kwargs))
    monkeypatch.setenv("MCP_BEARER_TOKEN", "a-bearer-of-sixteen")
    server.serve_http(Settings(state_dir=tmp_path), host="127.0.0.1", port=8000)
    assert seen["transport"] == "streamable-http" and seen["uvicorn_config"] == {"access_log": False}


def test_stdio_answers_on_stdout_and_logs_only_to_stderr(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HKTVMALL_", "MCP_"))}
    env["HKTVMALL_STATE_DIR"] = str(tmp_path)
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "hktv_get_cart", "arguments": {}}},
    ]
    process = subprocess.Popen(
        [sys.executable, "-m", "hktvmall_mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        encoding="utf-8",  # the protocol is UTF-8 on every platform
    )
    replies: dict[int, dict] = {}
    try:
        for message in messages:
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()
            if "id" in message:
                while message["id"] not in replies:
                    line = process.stdout.readline()
                    assert line, process.stderr.read()
                    reply = json.loads(line)  # every stdout line is protocol
                    if "id" in reply:
                        replies[reply["id"]] = reply
    finally:
        process.stdin.close()
        process.wait(timeout=30)
        process.stdout.close()
        process.stderr.close()
    info = replies[1]["result"]["serverInfo"]
    assert (info["name"], info["version"]) == ("hktvmall", __version__)  # ours, not FastMCP's
    assert "hktv_search" in {t["name"] for t in replies[2]["result"]["tools"]}
    error = json.loads(replies[3]["result"]["content"][0]["text"])["error"]
    assert error["code"] == "not_signed_in" and "hktvmall-mcp login" in error["next_step"]


def _cookie(name: str, value: str) -> http.cookiejar.Cookie:
    return http.cookiejar.Cookie(
        0, name, value, None, False, "www.hktvmall.com", True, False, "/", True, True, None, False, None, None, {}
    )

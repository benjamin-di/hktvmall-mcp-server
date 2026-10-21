"""The login service's lifecycle and API with a fake browser and no display: slots, one-time results, expiry,
the bearer on every route but /health."""

import asyncio

import pytest
from starlette.testclient import TestClient

from hktvmall_login.app import build_app
from hktvmall_login.logins import Logins, session_cookies

TOKEN = "x" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


def make_logins(clock, signed_in=None):
    async def browser(login, logins):
        login.state = "waiting"
        while True:
            if signed_in is not None and signed_in.get(login.id):
                logins.proven(login, {"email": "alex@example.com", "user_agent": "UA", "cookies": []})
                return
            await asyncio.sleep(0.01)

    logins = Logins(slots=1, minutes=15, browser=browser, clock=clock)
    logins._start_display = lambda login: None  # no Xvfb in tests
    logins._stop_display = lambda login: login.procs.clear()
    return logins


def test_every_route_but_health_needs_the_bearer(tmp_path):
    app = build_app(make_logins(Clock()), token=TOKEN, novnc_dir=tmp_path)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/logins", json={}).status_code == 401
        assert client.post("/logins", json={}, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/logins/abc", headers=AUTH).status_code == 404
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/logins/abc/vnc"):
            pass  # refused before the handshake: no bearer


def test_one_slot_one_result_then_closed(tmp_path):
    clock, signed_in = Clock(), {}
    app = build_app(make_logins(clock, signed_in), token=TOKEN, novnc_dir=tmp_path)
    with TestClient(app) as client:
        first = client.post("/logins", json={"layout": "phone"}, headers=AUTH)
        assert first.status_code == 201 and first.json()["layout"] == "phone"
        login_id = first.json()["id"]
        assert client.post("/logins", json={}, headers=AUTH).status_code == 503  # the only slot is taken
        assert client.post(f"/logins/{login_id}/result", headers=AUTH).status_code == 409  # not signed in yet
        signed_in[login_id] = True
        for _ in range(100):
            if client.get(f"/logins/{login_id}", headers=AUTH).json()["state"] == "signed_in":
                break
        result = client.post(f"/logins/{login_id}/result", headers=AUTH)
        assert result.status_code == 200 and result.json()["email"] == "alex@example.com"
        assert client.post(f"/logins/{login_id}/result", headers=AUTH).status_code == 404  # once only
        assert client.post("/logins", json={}, headers=AUTH).status_code == 201  # the slot is free again


def test_logins_past_the_limit_end(tmp_path):
    clock = Clock()
    logins = make_logins(clock)
    app = build_app(logins, token=TOKEN, novnc_dir=tmp_path)
    with TestClient(app) as client:
        login_id = client.post("/logins", json={}, headers=AUTH).json()["id"]
        clock.now += 16 * 60
        assert client.get(f"/logins/{login_id}", headers=AUTH).status_code == 404
        assert client.post("/logins", json={}, headers=AUTH).status_code == 201


def test_session_cookies_keep_hktvmall_only_in_the_snapshot_shape():
    cookies = session_cookies(
        [
            {"name": "JSESSIONID", "value": "s", "domain": "www.hktvmall.com", "path": "/", "secure": True,
             "expires": -1},
            {"name": "hktvstorefrontRememberMe", "value": "r", "domain": ".hktvmall.com", "path": "/",
             "secure": True, "expires": 1793706000.5},
            {"name": "_ga", "value": "g", "domain": ".google.com", "path": "/", "secure": False, "expires": 1},
        ]
    )  # fmt: skip
    assert cookies == [
        {"name": "JSESSIONID", "value": "s", "domain": "www.hktvmall.com", "path": "/", "secure": True,
         "expires": None},
        {"name": "hktvstorefrontRememberMe", "value": "r", "domain": ".hktvmall.com", "path": "/", "secure": True,
         "expires": 1793706000},
    ]  # fmt: skip

"""Connecting a friend end to end over HTTP, as Claude does it: register, authorize, the connect page (invite
code, then HKTVmall's login through the login service), the code exchange, tools acting for that account only."""

import base64
import dataclasses
import hashlib
import json
import secrets
from urllib.parse import parse_qs, urlsplit

import pytest
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.testclient import TestClient
from test_accounts import ALEX, cookies

from hktvmall_mcp import server
from hktvmall_mcp.accounts import DEFAULT, Accounts, parse_key
from hktvmall_mcp.invites import Invites
from hktvmall_mcp.oauth import HktvOAuth, redirect_allowed
from hktvmall_mcp.site import Transport
from hktvmall_mcp.state import StateStore

KEY = base64.b64encode(bytes(range(32))).decode()
PUBLIC = "https://hktvmall.example"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"


class FakeLogins:
    """The login service as hktvmall-mcp sees it."""

    def __init__(self):
        self.logins, self.busy, self.taken = {}, False, 0

    def start(self, layout):
        if self.busy:
            return None
        login_id = f"login-{len(self.logins):012d}"
        self.logins[login_id] = {"id": login_id, "state": "waiting", "host": "www.hktvmall.com", "layout": layout}
        return self.logins[login_id]

    def state(self, login_id):
        return self.logins.get(login_id)

    def sign_in(self, email=ALEX, session="alex-session"):
        login = list(self.logins.values())[-1]
        login.update(state="signed_in", result={"email": email, "user_agent": "Phone/1", "cookies": cookies(session)})

    def result(self, login_id):
        login = self.logins.get(login_id)
        if not login or login["state"] != "signed_in":
            return None
        self.taken += 1
        login["state"] = "closed"
        return login.pop("result")

    def close(self, login_id):
        pass

    def static(self, path):
        return 200, b"<html>viewer</html>", "text/html"


@pytest.fixture
def setup(settings, service, clock):
    keyed = dataclasses.replace(
        settings, session_key=parse_key(KEY), login_url="http://hktvmall-mcp-login:8080", login_token="t" * 40,
        public_url=PUBLIC,
    )  # fmt: skip
    shared = StateStore(keyed.state_dir, budget=1000, clock=clock, name="shared.json")
    accounts = Accounts(keyed, Transport(shared, sleep=lambda _s: None), service, clock=clock)
    static = StaticTokenVerifier(tokens={"default-token": {"client_id": "bearer", "scopes": []}})
    oauth = HktvOAuth(public_url=PUBLIC, state_dir=keyed.state_dir, accounts=accounts, static=static)
    logins = FakeLogins()
    app = server.create_server(accounts, auth=oauth, login=logins).http_app(**server.HTTP_OPTIONS)
    return accounts, Invites(keyed.state_dir), logins, app


MCP = {"Accept": "application/json, text/event-stream", "mcp-protocol-version": "2025-03-26"}


def tool(client, token, name, arguments=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}
    response = client.post("/mcp", json=body, headers={**MCP, "Authorization": f"Bearer {token}"})
    if response.status_code != 200:
        return response.status_code
    result = response.json()["result"]
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])


def register(client):
    response = client.post("/register", json={"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none",
                                              "client_name": "Claude"})  # fmt: skip
    assert response.status_code == 201, response.text
    return response.json()["client_id"]


def authorize(client, client_id):
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    response = client.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK, "code_challenge": challenge,
        "code_challenge_method": "S256", "state": "st-1", "resource": f"{PUBLIC}/mcp",
    }, follow_redirects=False)  # fmt: skip
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(f"{PUBLIC}/connect?req=")
    return location.removeprefix(PUBLIC), verifier


def open_page(client, path):
    page = client.get(path)
    assert page.status_code == 200 and page.headers["x-frame-options"] == "DENY"
    return page.text.split('const cid = "', 1)[1].split('"', 1)[0], page.text


def exchange(client, client_id, redirect, verifier):
    query = parse_qs(urlsplit(redirect).query)
    assert redirect.startswith(CALLBACK) and query["state"] == ["st-1"]
    response = client.post("/token", data={
        "grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": CALLBACK,
        "client_id": client_id, "code_verifier": verifier,
    })  # fmt: skip
    assert response.status_code == 200, response.text
    return response.json()


def test_a_friend_connects_from_their_assistant_and_acts_only_for_their_account(setup):
    accounts, invites, logins, app = setup
    code = invites.create("guest-1")
    with TestClient(app, base_url=PUBLIC) as client:
        # Without a token the MCP endpoint points the assistant at this server's own sign-in.
        unauthenticated = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=MCP)
        assert unauthenticated.status_code == 401
        assert "oauth-protected-resource" in unauthenticated.headers["www-authenticate"]

        client_id = register(client)
        path, verifier = authorize(client, client_id)
        cid, page = open_page(client, path)
        assert "welcome = null;" in page  # a new browser: the invite box first
        assert client.post(f"/connect/{cid}/start", json={"layout": "phone"}).json()["state"] == "new"
        assert logins.logins == {}  # no login window before an invite code
        assert client.post(f"/connect/{cid}/invite", json={"code": "nope"}).json()["state"] == "invite_wrong"
        typed = code.lower().replace("-", " ")  # as a person might type it
        assert client.post(f"/connect/{cid}/invite", json={"code": typed}).json()["state"] == "ready"
        assert client.post(f"/connect/{cid}/start", json={"layout": "phone"}).json()["state"] == "starting"
        assert client.get(f"/connect/{cid}/state").json()["state"] == "waiting"
        logins.sign_in()
        done = client.get(f"/connect/{cid}/state")
        assert done.json()["state"] == "connected" and done.json()["account"] == "a•••@example.com"
        assert "hktv_device=" in done.headers["set-cookie"] and "HttpOnly" in done.headers["set-cookie"]
        tokens = exchange(client, client_id, done.json()["redirect"], verifier)
        assert logins.taken == 1

        status = tool(client, tokens["access_token"], "hktv_status")
        assert status["account"] == "a•••@example.com"
        assert tool(client, "default-token", "hktv_status")["account"] == DEFAULT  # the default account is unchanged

        refreshed = client.post("/token", data={"grant_type": "refresh_token", "client_id": client_id,
                                                "refresh_token": tokens["refresh_token"]}).json()  # fmt: skip
        assert tool(client, refreshed["access_token"], "hktv_status")["account"] == "a•••@example.com"

        assert accounts.remove(accounts.ids()[0])  # disconnected: its tokens stop working at once
        assert tool(client, refreshed["access_token"], "hktv_status") == 401
        refused = client.post("/token", data={"grant_type": "refresh_token", "client_id": client_id,
                                              "refresh_token": tokens["refresh_token"]})  # fmt: skip
        assert refused.status_code in (400, 401)


def test_a_remembered_browser_signs_its_own_account_in_again_and_only_that(setup):
    accounts, invites, logins, app = setup
    code = invites.create("guest-1")
    with TestClient(app, base_url=PUBLIC) as client:
        client_id = register(client)
        path, verifier = authorize(client, client_id)
        cid, _ = open_page(client, path)
        client.post(f"/connect/{cid}/invite", json={"code": code})
        client.post(f"/connect/{cid}/start", json={"layout": "desktop"})
        logins.sign_in()
        first = client.get(f"/connect/{cid}/state").json()
        account_id = accounts.ids()[0]

        # Some weeks later HKTVmall ended the session; the friend reconnects from the same browser.
        path, verifier = authorize(client, client_id)
        cid, page = open_page(client, path)
        assert f"welcome = {json.dumps('a•••@example.com')};" in page
        assert client.post(f"/connect/{cid}/start", json={"layout": "desktop"}).json()["state"] == "starting"
        logins.sign_in(session="alex-later")
        again = client.get(f"/connect/{cid}/state").json()
        assert again["state"] == "connected" and accounts.ids() == [account_id]
        exchange(client, client_id, again["redirect"], verifier)
        assert first["redirect"] != again["redirect"]

        # The remembered browser cannot connect someone else's account.
        path, _ = authorize(client, client_id)
        cid, _ = open_page(client, path)
        client.post(f"/connect/{cid}/start", json={"layout": "desktop"})
        logins.sign_in(email="mallory@example.com", session="mallory")
        assert client.get(f"/connect/{cid}/state").json()["state"] == "wrong_account"
        assert accounts.ids() == [account_id]


def test_an_invite_belongs_to_the_first_account_that_used_it(setup):
    accounts, invites, logins, app = setup
    code = invites.create("guest-1")
    with TestClient(app, base_url=PUBLIC) as first, TestClient(app, base_url=PUBLIC) as second:
        for client, email, expected in ((first, ALEX, "connected"), (second, "mallory@example.com", "wrong_account")):
            client_id = register(client)
            path, _ = authorize(client, client_id)
            cid, _ = open_page(client, path)
            client.post(f"/connect/{cid}/invite", json={"code": code})
            client.post(f"/connect/{cid}/start", json={"layout": "phone"})
            logins.sign_in(email=email)
            assert client.get(f"/connect/{cid}/state").json()["state"] == expected
    assert len(accounts.ids()) == 1


def test_wrong_codes_are_slowed_down(setup):
    accounts, invites, logins, app = setup
    invites.create("guest-1")
    with TestClient(app, base_url=PUBLIC) as client:
        client_id = register(client)
        path, _ = authorize(client, client_id)
        cid, _ = open_page(client, path)
        headers = {"X-Forwarded-For": "203.0.113.9"}
        states = [client.post(f"/connect/{cid}/invite", json={"code": f"AAAA-AAAA-AA{i:02d}"}, headers=headers)
                  .json()["state"] for i in range(10)]  # fmt: skip
        assert states[:8] == ["invite_wrong"] * 8 and states[8:] == ["invite_limited"] * 2


def test_only_the_assistants_may_receive_codes_and_stale_pages_say_so(setup):
    _, _, logins, app = setup
    with TestClient(app, base_url=PUBLIC) as client:
        evil = client.post("/register", json={"redirect_uris": ["https://evil.example/cb"],
                                              "token_endpoint_auth_method": "none"})  # fmt: skip
        assert evil.status_code == 400
        page = client.get("/connect?req=made-up")
        assert page.status_code == 404 and "expired" in page.text
        assert client.get("/connect/made-up/novnc/vnc.html").status_code == 404
    assert logins.logins == {}
    for uri, ok in ((CALLBACK, True), ("https://chatgpt.com/connector_platform_oauth_redirect", True),
                    ("http://127.0.0.1:33418/callback", True), ("http://claude.ai/cb", False),
                    ("https://claude.ai.evil.example/cb", False)):  # fmt: skip
        assert redirect_allowed(uri) is ok, uri


def test_a_full_login_service_says_busy(setup):
    _, invites, logins, app = setup
    code = invites.create("guest-1")
    logins.busy = True
    with TestClient(app, base_url=PUBLIC) as client:
        path, _ = authorize(client, register(client))
        cid, _ = open_page(client, path)
        client.post(f"/connect/{cid}/invite", json={"code": code})
        assert client.post(f"/connect/{cid}/start", json={"layout": "phone"}).json()["state"] == "busy"


def test_without_the_public_url_there_is_no_sign_in_only_the_default_account(settings, service):
    accounts = Accounts(settings, None, service)
    static = StaticTokenVerifier(tokens={"default-token": {"client_id": "bearer", "scopes": []}})
    srv = server.create_server(accounts, auth=static)
    assert "/connect" not in {getattr(r, "path", None) for r in srv._additional_http_routes}

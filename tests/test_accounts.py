"""Several accounts in one server: sealed sessions, who a token acts for, separate budgets, no crossing over."""

import base64
import dataclasses
import http.cookiejar
import json
import os
import stat

import pytest
from conftest import FakeSite, product
from starlette.testclient import TestClient
from test_site import HOME_IN, ScriptedTransport

from hktvmall_mcp import server
from hktvmall_mcp.accounts import DEFAULT, Accounts, AccountSession, AccountTransport, parse_key
from hktvmall_mcp.errors import HktvError
from hktvmall_mcp.server import HktvService
from hktvmall_mcp.session import SessionStore, to_jar
from hktvmall_mcp.site import PATHS, Account, Site
from hktvmall_mcp.state import StateStore

KEY = base64.b64encode(bytes(range(32))).decode()
ALEX = "alex@example.com"


def cookies(session_id="alex-session", **extra):
    jar = [
        {
            "name": "JSESSIONID",
            "value": session_id,
            "domain": "www.hktvmall.com",
            "path": "/",
            "secure": True,
            "expires": None,
        }
    ]
    jar += [
        {"name": n, "value": v, "domain": "www.hktvmall.com", "path": "/", "secure": True, "expires": None}
        for n, v in extra.items()
    ]
    return jar


@pytest.fixture
def keyed(settings):
    return dataclasses.replace(settings, session_key=parse_key(KEY))


@pytest.fixture
def accounts(keyed, service, clock):
    shared = StateStore(keyed.state_dir, budget=1000, clock=clock, name="shared.json")
    from hktvmall_mcp.site import Transport

    return Accounts(keyed, Transport(shared, sleep=lambda _s: None), service, clock=clock)


def test_a_connected_session_is_sealed_private_and_keeps_its_id_across_logins(accounts, keyed):
    first = accounts.adopt(ALEX, cookies(), "Phone Browser/1")
    path = keyed.state_dir / "accounts" / first / "session.bin"
    raw = path.read_bytes()
    assert b"alex-session" not in raw and b"alex@example" not in raw  # sealed
    if os.name != "nt":  # Windows has no mode bits; the file sits in the user's own profile
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    again = accounts.adopt(ALEX.upper(), cookies("alex-session-2"), "Phone Browser/1")
    assert again == first  # a new login for the same account keeps the connection's id
    session = accounts.get(first).session
    generation, jar, user_agent = session.checkout_client()
    assert generation == 2 and {c.name: c.value for c in jar}["JSESSIONID"] == "alex-session-2"
    assert user_agent == "Phone Browser/1"
    assert accounts.describe() == [{"id": first, "email": "a•••@example.com", "generation": 2, "state": "unverified"}]


def test_a_login_without_a_usable_session_is_refused(accounts):
    with pytest.raises(HktvError) as err:
        accounts.adopt(
            ALEX,
            [
                {
                    "name": "other",
                    "value": "x",
                    "domain": "www.hktvmall.com",
                    "path": "/",
                    "secure": True,
                    "expires": None,
                }
            ],
            "UA",
        )
    assert err.value.code == "invalid_argument"
    with pytest.raises(HktvError):
        accounts.adopt(ALEX, cookies() + [{**cookies()[0], "name": "x", "domain": "evil.example"}], "UA")
    assert accounts.ids() == []


def test_a_sealed_session_moved_to_another_account_does_not_open(accounts, keyed):
    alex = accounts.adopt(ALEX, cookies(), "UA")
    sam = accounts.adopt("sam@example.com", cookies("sam-session"), "UA")
    root = keyed.state_dir / "accounts"
    (root / sam / "session.bin").write_bytes((root / alex / "session.bin").read_bytes())
    with pytest.raises(HktvError) as err:
        accounts.get(sam)  # its email can no longer be read: the seal is bound to the account id
    assert err.value.code == "not_connected"


def test_cookies_hktvmall_replaces_are_kept_unless_a_newer_login_won(accounts, keyed, clock):
    account_id = accounts.adopt(ALEX, cookies(), "UA")
    session = accounts.get(account_id).session
    generation, jar, _ = session.checkout_client()
    rotated = to_jar(cookies("rotated-by-remember-me", access_token="t-1"))
    session.store_jar(rotated, generation)
    reread = AccountSession(keyed.state_dir / "accounts" / account_id, account_id, ALEX, keyed.session_key, clock=clock)
    assert {c.name: c.value for c in reread.checkout_client()[1]} == {
        "JSESSIONID": "rotated-by-remember-me",
        "access_token": "t-1",
    }

    accounts.adopt(ALEX, cookies("fresh-login"), "UA")  # generation 2 lands while generation 1 is in use
    session.store_jar(to_jar(cookies("late-write-from-gen-1")), generation)
    reread = AccountSession(keyed.state_dir / "accounts" / account_id, account_id, ALEX, keyed.session_key, clock=clock)
    generation, jar, _ = reread.checkout_client()
    assert generation == 2 and {c.name: c.value for c in jar}["JSESSIONID"] == "fresh-login"


def test_the_token_decides_the_account_and_unknown_callers_get_nothing(accounts):
    alex = accounts.adopt(ALEX, cookies(), "UA")
    assert accounts.resolve("bearer") is accounts.default  # every MCP_BEARER_TOKEN
    assert accounts.resolve(f"account:{alex}").account_id == alex
    for client in ("account:acct_000000000000", "account:../default", "some-gateway", "someone", None):
        with pytest.raises(HktvError) as err:
            accounts.resolve(client)
        assert err.value.code == "not_connected"
    assert accounts.resolve(None, open_default=True) is accounts.default  # a server built without auth


def test_without_a_session_key_only_the_default_account_exists(settings, service):
    accounts = Accounts(settings, None, service)
    with pytest.raises(HktvError):
        accounts.adopt(ALEX, cookies(), "UA")
    assert accounts.services() == [service]


def test_each_account_spends_its_own_budget_and_a_pause_stops_everyone(tmp_path, clock):
    from hktvmall_mcp.site import Transport

    shared = StateStore(tmp_path, budget=100, clock=clock, name="shared.json")
    transport = Transport(shared, sleep=lambda _s: None)
    alex = AccountTransport(transport, StateStore(tmp_path / "alex", budget=1, clock=clock))
    sam = AccountTransport(transport, StateStore(tmp_path / "sam", budget=5, clock=clock))

    class Opener:
        def open(self, req, timeout):
            raise OSError("no network in tests")

    with pytest.raises(HktvError) as err:
        alex.send(Opener(), "GET", "https://www.hktvmall.com/hktv/en/")
    assert err.value.code == "upstream_error"  # sent (and failed): the one request was spent
    with pytest.raises(HktvError) as err:
        alex.send(Opener(), "GET", "https://www.hktvmall.com/hktv/en/")
    assert err.value.code == "budget_exhausted"
    with pytest.raises(HktvError) as err:
        sam.send(Opener(), "GET", "https://www.hktvmall.com/hktv/en/")
    assert err.value.code == "upstream_error"  # Sam still has budget
    shared.pause("429")
    with pytest.raises(HktvError) as err:
        sam.send(Opener(), "GET", "https://www.hktvmall.com/hktv/en/")
    assert err.value.code == "blocked"


class Sealed:
    """An AccountSession stand-in for Account.verify: records marks, carries the connected email."""

    generation = 1
    account_email = ALEX

    def __init__(self):
        self.marks = []

    def mark(self, state, generation):
        self.marks.append(state)


def _verify(home):
    site = Site.__new__(Site)
    site.transport, site.session = ScriptedTransport({PATHS["home"]: [(200, home)]}), Sealed()
    acct = Account(site, http.cookiejar.CookieJar(), 1)
    return acct, site


def test_a_connected_session_must_still_show_its_own_account():
    acct, site = _verify(HOME_IN.replace("});", ", 'realContactEmail' : \"Alex@Example.com\" });"))
    acct.verify()
    assert site.session.marks == ["live"]

    acct, site = _verify(HOME_IN.replace("});", ", 'realContactEmail' : \"someone@example.org\" });"))
    with pytest.raises(HktvError) as err:
        acct.verify()
    assert err.value.code == "account_mismatch" and site.session.marks == ["dead"]
    with pytest.raises(HktvError):
        acct.basket()  # nothing more is sent with a client that saw another account

    acct, site = _verify(HOME_IN)  # no contact email on the page: a format change, never a pass
    with pytest.raises(HktvError) as err:
        acct.verify()
    assert err.value.code == "schema_changed"


def test_two_callers_see_only_their_own_baskets_and_status(service, site, settings, clock, monkeypatch):
    alex_site = FakeSite(lines={"ALEX-ITEM": 3}, products=[product("ALEX-ITEM")])
    site.lines = {"DEFAULT-ITEM": 1}
    alex_state = StateStore(settings.state_dir / "accounts" / "acct_aaaaaaaaaaaa", budget=50, clock=clock)
    alex_session = SessionStore(settings.snapshot_path, ALEX, clock=clock)
    alex = HktvService(alex_site, alex_state, alex_session, settings, account_id="acct_aaaaaaaaaaaa")
    accounts = Accounts(settings, None, service)
    accounts._services["acct_aaaaaaaaaaaa"] = alex  # a connected account backed by a fake site
    alex_dir = settings.state_dir / "accounts" / "acct_aaaaaaaaaaaa"
    alex_dir.mkdir(parents=True, exist_ok=True)
    (alex_dir / "session.bin").touch()  # still connected

    monkeypatch.setenv("MCP_BEARER_TOKEN", "default-token")
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

    auth = StaticTokenVerifier(
        tokens={
            "default-token": {"client_id": "bearer", "scopes": []},
            "alex-token": {"client_id": "account:acct_aaaaaaaaaaaa", "scopes": []},
            "stranger-token": {"client_id": "account:acct_bbbbbbbbbbbb", "scopes": []},
        }
    )
    app = server.create_server(accounts, auth=auth).http_app(**server.HTTP_OPTIONS)
    headers = {"Accept": "application/json, text/event-stream", "mcp-protocol-version": "2025-03-26"}

    def call(token, tool, arguments=None):
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}},
        }
        response = client.post("/mcp", json=body, headers={**headers, "Authorization": f"Bearer {token}"})
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        return result.get("structuredContent") or json.loads(result["content"][0]["text"])

    with TestClient(app) as client:
        default = call("default-token", "hktv_get_cart")
        mine = call("alex-token", "hktv_get_cart")
        assert [line["id"] for line in default["line_items"]] == ["DEFAULT-ITEM"]
        assert [line["id"] for line in mine["line_items"]] == ["ALEX-ITEM"]
        assert call("default-token", "hktv_status")["account"] == DEFAULT
        assert call("alex-token", "hktv_status")["account"] == "a•••@example.com"
        stranger = call("stranger-token", "hktv_get_cart")
        assert stranger["error"]["code"] == "not_connected"
    assert alex_site.basket_reads >= 1 and alex_state.snapshot()["last_error"] is None


def test_the_production_wiring_builds_with_and_without_a_session_key(settings, keyed):
    # The same construction the release workflow's image smoke test runs.
    for config in (settings, keyed):
        accounts = server.build_accounts(config)
        assert server.create_server(accounts).name == "hktvmall"
        assert [s.account_id for s in accounts.services()] == [DEFAULT]
        default = accounts.default
        assert default.site.transport.state is default.shared  # a pause is shared by every account

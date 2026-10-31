"""People and their accounts: sealed sessions, records on first contact, sharing, and no crossing over."""

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
from hktvmall_mcp.accounts import BEARER_CLIENT, DEFAULT, Accounts, AccountSession, AccountTransport, parse_key
from hktvmall_mcp.config import ConfigError, Settings
from hktvmall_mcp.errors import HktvError
from hktvmall_mcp.server import HktvService
from hktvmall_mcp.session import to_jar
from hktvmall_mcp.site import PATHS, Account, Site
from hktvmall_mcp.state import StateStore

KEY = base64.b64encode(bytes(range(32))).decode()
ALEX = "alex@example.com"
SAM = "sam@example.com"


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
    """People mode: the gateway names the verified person; each person has their own record."""
    return dataclasses.replace(
        settings,
        snapshot_path=None,
        expected_email=None,
        session_key=parse_key(KEY),
        trusted_account_header="x-fleet-person",
    )


@pytest.fixture
def accounts(keyed, service, clock):
    shared = StateStore(keyed.state_dir, budget=1000, clock=clock, name="shared.json")
    from hktvmall_mcp.site import Transport

    return Accounts(keyed, Transport(shared, sleep=lambda _s: None), service, clock=clock)


def connect(accounts, person, email=ALEX, session_id="alex-session"):
    """A person's own sign-in, as the Connections page completes it."""
    epoch = accounts.person(person).require_active()
    return accounts.adopt_owned(person, epoch, email, cookies(session_id), "Phone Browser/1")


def control_of(accounts, person):
    current = accounts.control(person).read()
    return current["epoch"], current["state"]


def choose(accounts, person, owner):
    current = accounts.control(person).read()
    grant = None if person == owner else accounts.control(owner).read()["shares"][person]["revision"]
    accounts.select(person, owner, current["selection_revision"], grant)


def revoke(accounts, owner, member):
    grant = accounts.control(owner).read()["shares"][member]["revision"]
    accounts.unshare(owner, member, *control_of(accounts, owner), grant=grant)


def test_a_person_gets_a_record_on_first_contact_and_never_by_name_alone(accounts, keyed):
    assert accounts.known() == []
    service = accounts.resolve(BEARER_CLIENT, person="alex")
    assert service.account_id == "alex" and accounts.known() == ["alex"]
    folder = keyed.state_dir / "people" / "alex"
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700
    with pytest.raises(HktvError) as err:
        service.basket()
    assert err.value.code == "not_signed_in"  # search works; the basket needs their own sign-in
    for person in (None, "", "Alex", "../alex", "a" * 40, "default"):
        with pytest.raises(HktvError) as err:
            accounts.resolve(BEARER_CLIENT, person=person)
        assert err.value.code in ("not_connected", "connection_disconnected")
    with pytest.raises(HktvError):
        accounts.resolve("someone-else", person="alex")
    assert accounts.known() == ["alex"]


def test_a_lost_control_record_is_never_recreated_around_stored_state(accounts, keyed):
    connect(accounts, "alex")
    folder = keyed.state_dir / "people" / "alex"
    (folder / "connection.json").unlink()
    with pytest.raises(HktvError) as err:
        accounts.resolve(BEARER_CLIENT, person="alex")
    assert err.value.code == "connection_disconnected"
    assert (folder / "session.bin").exists() and not (folder / "connection.json").exists()


def test_a_connected_session_is_sealed_private_and_reconnect_raises_the_generation(accounts, keyed):
    connect(accounts, "alex")
    path = keyed.state_dir / "people" / "alex" / "session.bin"
    raw = path.read_bytes()
    assert b"alex-session" not in raw and b"alex@example" not in raw  # sealed
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    connect(accounts, "alex", ALEX.upper(), "alex-session-2")
    session = accounts.get("alex").session
    generation, jar, user_agent = session.checkout_client()
    assert generation == 2 and {c.name: c.value for c in jar}["JSESSIONID"] == "alex-session-2"
    assert user_agent == "Phone Browser/1"
    assert accounts.describe() == [
        {
            "person": "alex",
            "name": None,
            "email": "a•••@example.com",
            "state": "active",
            "signed_in": True,
            "shared_with": [],
        }
    ]


def test_nobody_can_connect_someone_elses_provider_account(accounts):
    connect(accounts, "alex")
    with pytest.raises(HktvError) as err:
        connect(accounts, "sam", ALEX, "sam-copy")  # Sam signed in to Alex's HKTVmall account
    assert err.value.code == "account_mismatch"
    assert not accounts.owns("sam")
    with pytest.raises(HktvError) as err:
        connect(accounts, "alex", SAM, "switched")  # a reconnect must be the same account
    assert err.value.code == "account_mismatch"


def test_a_member_can_connect_their_own_without_changing_selection_and_lost_records_block_new_bindings(accounts, keyed):
    connect(accounts, "alex")
    accounts.person("sam")
    accounts.share("alex", "sam", *control_of(accounts, "alex"))
    choose(accounts, "sam", "alex")
    connect(accounts, "sam", SAM, "sam-session")
    assert accounts.owns("sam") and accounts.account_for("sam") == "alex"
    accounts.person("kim")
    (keyed.state_dir / "people" / "missing").mkdir(mode=0o700)
    epoch = accounts.control("kim").require_active()
    with pytest.raises(HktvError):
        accounts.adopt_owned("kim", epoch, "kim@example.com", cookies("kim-session"), "Synthetic Browser")
    connect(accounts, "alex", ALEX, "alex-reconnect")  # an existing binding is unaffected


def test_a_login_without_a_usable_session_is_refused(accounts):
    epoch = accounts.person("alex").require_active()
    with pytest.raises(HktvError) as err:
        accounts.adopt_owned(
            "alex",
            epoch,
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
    assert not accounts.owns("alex")


def test_a_sealed_session_moved_to_another_person_does_not_open(accounts, keyed):
    connect(accounts, "alex")
    connect(accounts, "sam", SAM, "sam-session")
    root = keyed.state_dir / "people"
    (root / "sam" / "session.bin").write_bytes((root / "alex" / "session.bin").read_bytes())
    accounts.forget("sam")
    with pytest.raises(HktvError) as err:
        accounts.get("sam")  # the seal is bound to the person
    assert err.value.code == "not_connected"


def test_cookies_hktvmall_replaces_are_kept_unless_a_newer_login_won(accounts, keyed, clock):
    connect(accounts, "alex")
    folder = keyed.state_dir / "people" / "alex"
    session = accounts.get("alex").session
    generation, jar, _ = session.checkout_client()
    session.store_jar(to_jar(cookies("rotated-by-remember-me", access_token="t-1")), generation)
    reread = AccountSession(folder, "alex", ALEX, keyed.session_key, clock=clock)
    assert {c.name: c.value for c in reread.checkout_client()[1]} == {
        "JSESSIONID": "rotated-by-remember-me",
        "access_token": "t-1",
    }
    connect(accounts, "alex", ALEX, "fresh-login")  # generation 2 lands while generation 1 is in use
    session.store_jar(to_jar(cookies("late-write-from-gen-1")), generation)
    reread = AccountSession(folder, "alex", ALEX, keyed.session_key, clock=clock)
    generation, jar, _ = reread.checkout_client()
    assert generation == 2 and {c.name: c.value for c in jar}["JSESSIONID"] == "fresh-login"


def test_a_person_without_a_connection_is_monitored_as_a_guest(accounts, monkeypatch):
    service = accounts.resolve(BEARER_CLIENT, person="monitor-owner")
    searched = []
    monkeypatch.setattr(service.site, "search", lambda queries: searched.append(queries) or [{"hits": [], "total": 0}])
    monkeypatch.setattr(service, "reviews", lambda *args: {"reviews": []})
    canary = service.status(True)["canary"]
    assert canary["ok"] is True and canary["guest"] is True and canary["deep"]["ok"] is True
    assert searched and service.status(False)["signed_in"] is False


def test_sharing_is_an_offer_and_revocation_never_falls_back(accounts):
    connect(accounts, "alex")
    accounts.person("sam")
    assert accounts.resolve(BEARER_CLIENT, person="sam").account_id == "sam"
    accounts.share("alex", "sam", *control_of(accounts, "alex"))
    assert accounts.control("alex").read()["shared_with"] == ["sam"]
    assert accounts.resolve(BEARER_CLIENT, person="sam").account_id == "sam"
    choose(accounts, "sam", "alex")
    assert accounts.resolve(BEARER_CLIENT, person="sam").account_id == "alex"
    assert accounts.resolve(BEARER_CLIENT, person="alex").account_id == "alex"
    revoke(accounts, "alex", "sam")
    with pytest.raises(HktvError, match="shared_access_removed"):
        accounts.resolve(BEARER_CLIENT, person="sam")


def test_sharing_refuses_strangers_but_owners_can_receive_several_offers(accounts):
    connect(accounts, "alex")
    connect(accounts, "sam", SAM, "sam-session")
    accounts.person("kim")
    for member in ("alex", "nobody"):
        with pytest.raises(HktvError):
            accounts.share("alex", member, *control_of(accounts, "alex"))
    accounts.person("lee")
    with pytest.raises(HktvError):
        accounts.share("lee", "kim", *control_of(accounts, "lee"))
    accounts.share("alex", "sam", *control_of(accounts, "alex"))
    assert accounts.account_for("sam") == "sam"
    accounts.share("alex", "kim", *control_of(accounts, "alex"))
    accounts.share("sam", "kim", *control_of(accounts, "sam"))
    offers, unavailable = accounts.offers("kim")
    assert not unavailable and [offer["owner"] for offer in offers] == ["alex", "sam"]
    assert accounts.account_for("kim") == "kim"
    with pytest.raises(HktvError):
        accounts.share("alex", "kim", "stale-epoch", "active")


def test_a_member_never_falls_back_when_the_owner_disconnects(accounts):
    connect(accounts, "alex")
    accounts.person("sam")
    accounts.share("alex", "sam", *control_of(accounts, "alex"))
    choose(accounts, "sam", "alex")
    epoch = accounts.control("alex").suspend()
    accounts.disconnect_owned("alex", epoch)
    with pytest.raises(HktvError) as err:
        accounts.resolve(BEARER_CLIENT, person="sam")
    assert err.value.code == "connection_disconnected"  # never Sam's own empty account instead
    assert accounts.account_for("sam") == "alex"
    assert accounts.owns("alex")  # a disconnected connection is still Alex's own: no fallback for Alex either


def test_people_mode_requires_a_key_and_refuses_retired_or_shared_settings(settings, tmp_path):
    with pytest.raises(ConfigError, match="SESSION_KEY"):
        dataclasses.replace(settings, snapshot_path=None, expected_email=None, trusted_account_header="x-fleet-person")
    with pytest.raises(ConfigError, match="People mode"):
        dataclasses.replace(settings, session_key=parse_key(KEY), trusted_account_header="x-fleet-person")
    for name in (
        "HKTVMALL_CONNECTIONS_ACCOUNTS_FILE",
        "HKTVMALL_CONNECTIONS_MANAGERS_FILE",
        "HKTVMALL_DEFAULT_LOGIN_URL",
    ):
        with pytest.raises(ConfigError, match="Retired"):
            Settings.from_env({"HKTVMALL_STATE_DIR": str(tmp_path), name: "/connections-config/accounts.json"})


def test_without_people_mode_every_bearer_is_the_default_account(settings, service):
    accounts = Accounts(settings, None, service)
    assert accounts.resolve(BEARER_CLIENT, person="alex") is service
    assert accounts.resolve(None, open_default=True) is service
    with pytest.raises(HktvError):
        accounts.person("alex")
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


def test_owner_and_member_see_the_same_basket_and_strangers_see_their_own(accounts, keyed, clock, monkeypatch):
    """Over real MCP HTTP: the gateway's person picks the account; sharing adds a member, unsharing removes them."""
    connect(accounts, "alex")
    alex_site = FakeSite(lines={"ALEX-ITEM": 3}, products=[product("ALEX-ITEM")])
    real = accounts.get("alex")
    accounts._services["alex"] = HktvService(alex_site, real.state, real.session, keyed, account_id="alex")
    accounts.note_name("alex", "Alex <b>Example</b>")
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

    auth = StaticTokenVerifier(tokens={"backend-token": {"client_id": BEARER_CLIENT, "scopes": []}})
    app = server.create_server(accounts, auth=auth).http_app(**server.HTTP_OPTIONS)
    headers = {"Accept": "application/json, text/event-stream", "mcp-protocol-version": "2025-03-26"}

    def call(person, tool, arguments=None):
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}},
        }
        response = client.post(
            "/mcp",
            json=body,
            headers={**headers, "Authorization": "Bearer backend-token", "X-Fleet-Person": person},
        )
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        return result.get("structuredContent") or json.loads(result["content"][0]["text"])

    with TestClient(app) as client:
        assert [line["id"] for line in call("alex", "hktv_get_cart")["line_items"]] == ["ALEX-ITEM"]
        assert call("alex", "hktv_status")["connection"] == {"owner": "alex", "shared": False}
        assert call("sam", "hktv_get_cart")["error"]["code"] == "not_signed_in"
        accounts.share("alex", "sam", *control_of(accounts, "alex"))
        choose(accounts, "sam", "alex")
        assert [line["id"] for line in call("sam", "hktv_get_cart")["line_items"]] == ["ALEX-ITEM"]
        assert call("sam", "hktv_status")["connection"]["shared"] is True
        revoke(accounts, "alex", "sam")
        assert call("sam", "hktv_get_cart")["error"]["code"] == "shared_access_removed"
        accounts.share("alex", "sam", *control_of(accounts, "alex"))
        choose(accounts, "sam", "alex")
        alex_site.signed_in = False  # Alex's HKTVmall sign-in ended
        accounts._services["alex"].session.mark("dead", accounts._services["alex"].session.generation)
        member = call("sam", "hktv_get_cart")["error"]
        assert member["code"] == "owner_reconnect" and member["action"] == "human_account_check"
        assert member["next_step"].startswith("alex shares their HKTVmall connection")
        assert call("alex", "hktv_get_cart")["error"]["code"] == "session_expired"
        link = call("sam", "hktv_connect")
        assert link == {"url": None, "expires_at": None, "how": link["how"]}  # no Connections page configured
        epoch = accounts.control("alex").suspend()
        accounts.disconnect_owned("alex", epoch)
        assert call("sam", "hktv_get_cart")["error"]["code"] == "owner_reconnect"
        assert call("alex", "hktv_get_cart")["error"]["code"] == "connection_disconnected"


def test_the_production_wiring_builds_in_both_modes(settings, keyed):
    # The same construction the release workflow's image smoke test runs.
    for config in (settings, keyed):
        accounts = server.build_accounts(config)
        assert server.create_server(accounts).name == "hktvmall"
        assert [s.account_id for s in accounts.services()] == ([] if accounts.people else [DEFAULT])
        default = accounts.default
        assert default.site.transport.state is default.shared  # a pause is shared by every account
        assert PATHS  # the allowlist module loaded with the server


def test_a_failed_record_creation_leaves_nothing_behind(accounts, monkeypatch):
    connect(accounts, "alex")
    from hktvmall_mcp.connections_state import ConnectionState

    def fail(self):
        raise OSError("disk full")

    with monkeypatch.context() as patched:
        patched.setattr(ConnectionState, "initialize", fail)
        with pytest.raises(OSError):
            accounts.person("sam")
    assert not (accounts.directory / "sam").exists() and not list(accounts.directory.glob(".new-*"))
    assert accounts.known() == ["alex"]
    accounts.person("sam")  # a later visit records them normally
    assert accounts.sharer("sam") is None

"""My connections: each person's own connection, owner-granted sharing, and durable publication barriers."""

import dataclasses
import json
import os
import secrets
import time
from urllib.parse import urlsplit

import pytest
from signin_window.oidc import BrowserAuth, OIDCConfig
from starlette.testclient import TestClient
from test_accounts import accounts as accounts  # noqa: F401
from test_accounts import connect, cookies
from test_accounts import keyed as keyed  # noqa: F401

from hktvmall_mcp.accounts import BEARER_CLIENT
from hktvmall_mcp.connections import Connections
from hktvmall_mcp.errors import HktvError
from hktvmall_mcp.session import to_jar

PUBLIC = "https://app.example.com"
EMAIL = "second@example.com"
pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="hosted OIDC session storage requires POSIX private-state semantics"
)


class Login:
    def __init__(self):
        self.calls = []

    def start(self, layout, **params):
        self.calls.append("start")
        return {"id": "window", "state": "waiting", "view_token": "synthetic-token"}

    def state(self, window):
        return {"state": "waiting", "view_token": "synthetic-token"}

    def close(self, window):
        self.calls.append("close")

    def static(self, path, window=None):
        return 200, b"<p>Synthetic viewer</p>", "text/html"


def forms(page):
    """Each form on a page: (action, fields)."""
    out = []
    for chunk in page.split('<form method="post" action="')[1:]:
        action, rest = chunk.split('"', 1)
        fields = {}
        for part in rest.split("</form>", 1)[0].split('<input type="hidden" name="')[1:]:
            name, value = part.split('" value="', 1)
            fields[name] = value.split('"', 1)[0]
        out.append((action, fields))
    return out


class Browser(TestClient):
    def __init__(self, portal):
        self.connection_portal = portal
        super().__init__(portal.app, base_url=PUBLIC, follow_redirects=False)

    def post(self, url, **kwargs):
        """Fill the form as the page would, from the signed-in person's own record."""
        if url in ("/connections/start", "/connections/disconnect") and "data" not in kwargs:
            value = kwargs.get("headers", {}).get("Cookie", "").split("=", 1)[-1]
            person = self.connection_portal.auth.store.load(value).get("claims", {}).get("person_id")
            try:
                current = self.connection_portal.accounts.control(person).read()
                kwargs["data"] = {"epoch": current["epoch"], "state": current["state"]}
            except HktvError:
                pass
        return super().post(url, **kwargs)

    def submit(self, page_headers, action, person=None):
        """Press the page's button for ``action`` (and ``person``), as a browser would."""
        page = self.get("/connections/", headers=page_headers).text
        for path, fields in forms(page):
            if path.endswith("/" + action) and (person is None or fields.get("person") == person):
                return self.post(path, headers=page_headers, data=fields)
        raise AssertionError(f"no {action} form for {person} on the page")


@pytest.fixture
def configured(accounts, tmp_path):  # noqa: F811
    connect(accounts, "second", EMAIL, "synthetic-session")
    accounts.settings = dataclasses.replace(
        accounts.settings,
        public_url=PUBLIC,
        login_url="http://login.example.test:8080",
        login_token="x" * 40,
    )
    auth = BrowserAuth(
        OIDCConfig(
            "https://identity.example.com",
            "hktvmall-connections",
            PUBLIC,
            tmp_path / "browser",
            cookie_name="__Host-hktvmall-connections",
            required_group="example-users",
        )
    )
    portal = Connections(accounts, auth=auth, client=Login())
    return accounts, "second", auth, portal


def cookie(auth, person="second", subject=None, *, fresh=True, name="Example person"):
    sid = secrets.token_urlsafe(32)
    now = time.time()
    auth.store.save(
        sid,
        {
            "issuer": auth.config.issuer,
            "sub": subject or "subject-" + person,
            "name": name,
            "groups": ["example-users"],
            "claims": {"person_id": person},
            "expires": now + 900,
            "auth_time": now,
            "reauthenticated_at": now if fresh else None,
        },
    )
    return {"Cookie": f"{auth.cookie}={sid}", "Origin": PUBLIC}


def client(portal):
    return Browser(portal)


def test_ui_reads_do_not_claim_a_connection_and_start_requires_fresh_owner_auth(configured):
    accounts, account, auth, portal = configured
    with client(portal) as browser:
        assert browser.get("/connections/status").status_code == 401
        assert browser.get("/connections/status", headers=cookie(auth)).status_code == 200
        assert accounts.control(account).read()["owner"] is None
        assert (
            browser.post("/connections/start", headers=cookie(auth, fresh=False))
            .headers["location"]
            .endswith("reauth=1")
        )
        assert accounts.control(account).read()["owner"] is None
        made = browser.post("/connections/start", headers=cookie(auth))
        assert made.status_code == 303
        assert accounts.control(account).read()["owner"] == {"issuer": auth.config.issuer, "sub": "subject-second"}
        assert browser.get("/connections/status", headers=cookie(auth, subject="other-subject")).status_code == 401


def test_first_visit_records_the_person_with_their_display_name_and_nothing_shared(configured):
    accounts, _, auth, portal = configured
    with client(portal) as browser:
        page = browser.get("/connections/", headers=cookie(auth, "newcomer", name="New <Comer>"))
    assert page.status_code == 200 and "No HKTVmall account connected yet" in page.text
    assert "Connect HKTVmall" in page.text and "ask them to share it" in page.text
    assert "newcomer" in accounts.known()
    record = accounts.control("newcomer").read()
    assert record["name"] == "New Comer" and record["owner"] is None and record.get("shared_with", []) == []
    assert accounts.control("second").read().get("shared_with", []) == []


def test_the_owner_shares_from_the_page_and_the_member_sees_whose_account(configured):
    accounts, _, auth, portal = configured
    owner = cookie(auth, name="Alex Owner")
    member = cookie(auth, "third", name="Sam Member")
    with client(portal) as browser:
        assert "Nobody else has used HKTVmall here yet" in browser.get("/connections/", headers=owner).text
        browser.get("/connections/", headers=member)  # Sam opens the page once: now Sam is on the roster
        page = browser.get("/connections/", headers=owner).text
        assert "Shared with" in page and "Sam Member" in page and "cannot see your password" in page
        assert accounts.resolve(BEARER_CLIENT, person="third").account_id == "third"
        stale = cookie(auth, fresh=False, name="Alex Owner")
        assert browser.submit(stale, "share", "third").headers["location"].endswith("reauth=1")
        assert accounts.control("second").read().get("shared_with", []) == []
        assert browser.submit(owner, "share", "third").status_code == 303
        assert accounts.control("second").read()["shared_with"] == ["third"]
        assert accounts.resolve(BEARER_CLIENT, person="third").account_id == "second"
        member_page = browser.get("/connections/", headers=member).text
        assert "Using Alex Owner's HKTVmall account." in member_page and "Stop using" in member_page
        for action in ("start", "disconnect", "share", "unshare"):
            assert f'action="/connections/{action}"' not in member_page
        assert '✓ Sam Member <span class="muted">(third)</span>' in browser.get("/connections/", headers=owner).text
        assert browser.submit(member, "leave", "second").status_code == 303
        assert accounts.control("second").read()["shared_with"] == []
        assert accounts.resolve(BEARER_CLIENT, person="third").account_id == "third"
        browser.submit(owner, "share", "third")
        assert browser.submit(cookie(auth, fresh=False, name="Alex Owner"), "unshare", "third").status_code == 303
        assert accounts.control("second").read()["shared_with"] == []


def test_members_cannot_manage_and_strangers_cannot_reach_a_connection(configured):
    accounts, _, auth, portal = configured
    member = cookie(auth, "third")
    with client(portal) as browser:
        browser.get("/connections/", headers=member)
        browser.submit(cookie(auth), "share", "third")
        record = accounts.control("second").read()
        fields = {"epoch": record["epoch"], "state": record["state"]}
        # A member's start, disconnect and share act on their own record only, never the owner's connection.
        assert browser.post("/connections/start", headers=member).status_code == 409
        assert browser.post("/connections/disconnect", headers=member, data=fields).status_code == 409
        refused = browser.post("/connections/share", headers=member, data={**fields, "person": "second"})
        assert refused.status_code == 409
        refused = browser.post("/connections/unshare", headers=member, data={**fields, "person": "third"})
        assert refused.status_code == 409
        assert accounts.control("second").read() == record
        # Another person cannot remove a member, and nobody can stop using a connection not shared with them.
        browser.get("/connections/", headers=cookie(auth, "fourth"))
        leave = browser.post("/connections/leave", headers=cookie(auth, "fourth"), data={**fields, "person": "second"})
        assert leave.status_code == 409 and accounts.control("second").read()["shared_with"] == ["third"]
        # No origin, no action.
        no_origin = {"Cookie": cookie(auth)["Cookie"]}
        assert (
            browser.post("/connections/unshare", headers=no_origin, data={**fields, "person": "third"}).status_code
            == 403
        )
        page = browser.get("/connections/", headers=cookie(auth)).text
        assert "has their own HKTVmall connection" not in page and "✓ Example person" in page


def test_a_member_never_sees_the_owners_sign_in_link(configured, monkeypatch):
    accounts, account, auth, portal = configured
    with client(portal) as browser:
        browser.get("/connections/", headers=cookie(auth, "third"))
        browser.submit(cookie(auth), "share", "third")
        monkeypatch.setattr(portal, "status", lambda person: {"state": "connecting", "url": PUBLIC + "/connect/x"})
        member = browser.get("/connections/status", headers=cookie(auth, "third")).json()
        page = browser.get("/connections/", headers=cookie(auth, "third")).text
    assert member == {"role": "member", "owner": account, "state": "connecting"}
    assert "/connect/x" not in page and "is reconnecting HKTVmall" in page


def test_sharing_stays_available_while_the_owner_reconnects(configured):
    accounts, _, auth, portal = configured
    with client(portal) as browser:
        browser.get("/connections/", headers=cookie(auth, "third"))
        browser.submit(cookie(auth), "share", "third")
        browser.post("/connections/start", headers=cookie(auth))
        page = browser.get("/connections/", headers=cookie(auth)).text
        assert "Continue sign-in" in page and "Shared with" in page
        assert browser.submit(cookie(auth), "unshare", "third").status_code == 303
    assert accounts.control("second").read()["shared_with"] == []


def test_first_sign_in_explains_sharing_without_offering_an_empty_account(configured):
    _, _, auth, portal = configured
    with client(portal) as browser:
        headers = cookie(auth, "third")
        for connecting in (False, True):
            if connecting:
                browser.post("/connections/start", headers=headers)
            page = browser.get("/connections/", headers=headers).text
            assert "<h2>Sharing</h2>" in page and "You do not need to connect an account of your own" in page
            assert 'action="/connections/share"' not in page


def test_people_with_the_same_display_name_can_be_distinguished_when_sharing(configured):
    _, _, auth, portal = configured
    with client(portal) as browser:
        for person in ("third", "fourth"):
            browser.get("/connections/", headers=cookie(auth, person, name="Same Name"))
        page = browser.get("/connections/", headers=cookie(auth)).text
    assert 'Same Name <span class="muted">(third)</span>' in page
    assert 'Same Name <span class="muted">(fourth)</span>' in page


@pytest.mark.parametrize("missing", [True, False])
def test_lost_or_damaged_saved_session_is_not_shown_as_connected(configured, missing):
    accounts, account, auth, portal = configured
    with client(portal) as browser:
        assert browser.get("/connections/status", headers=cookie(auth)).json()["state"] == "connected"
        blob = accounts.directory / account / "session.bin"
        if missing:
            blob.unlink()
        else:
            blob.write_bytes(b"damaged session")
        expected = "sign_in_needed" if missing else "check_needed"
        assert browser.get("/connections/status", headers=cookie(auth)).json()["state"] == expected
        accounts.forget(account)  # the same result after rebuilding the reader, as on a restart
        assert browser.get("/connections/status", headers=cookie(auth)).json()["state"] == expected


def test_people_who_cannot_be_added_are_shown_with_the_reason(configured):
    accounts, _, auth, portal = configured
    connect(accounts, "third", "third@example.com", "third-session")
    accounts.note_name("third", "Kim")
    accounts.person("fourth")
    accounts.note_name("fourth", "Lee")
    third = accounts.control("third").read()
    accounts.share("third", "fourth", third["epoch"], third["state"])
    with client(portal) as browser:
        page = browser.get("/connections/", headers=cookie(auth)).text
    assert 'Kim <span class="muted">(third)</span>: has their own HKTVmall connection' in page
    assert 'Lee <span class="muted">(fourth)</span>: uses Kim&#x27;s' in page
    assert not [fields for action, fields in forms(page) if action.endswith("/share")]


def test_every_viewer_route_denies_wrong_owner_bearer_and_forwarded_identity(configured):
    _, account, auth, portal = configured
    epoch = portal.accounts.control(account).require_active()
    from signin_window.relay import AttemptContext

    link = portal.relay.create(account, context=AttemptContext(owner=account, target=epoch))
    path = "/connect/" + link.id
    with client(portal) as browser:
        for method, suffix in (
            ("GET", ""),
            ("GET", "/state"),
            ("POST", "/start"),
            ("POST", "/cancel"),
            ("GET", "/novnc/vnc.html"),
        ):
            assert (
                browser.request(
                    method,
                    path + suffix,
                    headers={"Origin": PUBLIC, "Authorization": "Bearer backend-only", "X-Fleet-Person": "second"},
                ).status_code
                == 404
            )
            assert browser.request(method, path + suffix, headers=cookie(auth, "first")).status_code == 404
        assert portal.client.calls == []
        assert browser.post(path + "/start", headers=cookie(auth)).status_code == 200
        assert browser.get(path + "/novnc/vnc.html", headers=cookie(auth)).status_code == 200


def test_disconnect_preserves_record_budgets_identity_sharing_and_generation_but_deletes_credentials(configured):
    accounts, account, auth, portal = configured
    service = accounts.get(account)
    old_epoch = accounts.control(account).require_active()
    retained = service.state.path
    before = retained.read_bytes() if retained.exists() else None
    with client(portal) as browser:
        browser.get("/connections/", headers=cookie(auth, "third"))
        browser.submit(cookie(auth), "share", "third")
        assert browser.post("/connections/start", headers=cookie(auth)).status_code == 303
        assert browser.post("/connections/disconnect", headers=cookie(auth)).status_code == 303
    record = accounts.control(account).read()
    assert record["state"] == "disconnected" and record["provider_email"] == EMAIL
    assert record["owner"]["sub"] == "subject-second" and record["generation"] == 1
    assert record["shared_with"] == ["third"]
    assert not (accounts.directory / account / "session.bin").exists()
    assert accounts.account_for("second") == account and accounts.account_for("third") == account
    assert (retained.read_bytes() if retained.exists() else None) == before
    with pytest.raises(HktvError, match="connection_disconnected"):
        accounts.get(account)
    with pytest.raises(HktvError):
        service.session.checkout_client()
    with pytest.raises(HktvError):
        accounts.adopt_owned(account, old_epoch, EMAIL, cookies("late"), "Synthetic Browser")
    new_epoch = accounts.control(account).resume()
    assert new_epoch != old_epoch
    accounts.adopt_owned(account, new_epoch, EMAIL, cookies("new"), "Synthetic Browser")
    assert accounts.get(account).session.generation == 2
    with pytest.raises(HktvError):
        service.session.checkout_client()  # cached object from the previous epoch stays invalid


def test_wrong_account_reconnect_preserves_previous_sealed_bytes(configured):
    accounts, account, _, _ = configured
    path = accounts.directory / account / "session.bin"
    before = path.read_bytes()
    epoch = accounts.control(account).require_active()
    with pytest.raises(HktvError, match="account_mismatch"):
        accounts.adopt_owned(account, epoch, "different@example.com", cookies(), "Browser")
    assert path.read_bytes() == before


def test_known_dead_session_does_not_look_connected(configured):
    accounts, account, _, portal = configured
    session = accounts.get(account).session
    generation, _, _ = session.checkout_client()
    session.mark("dead", generation)
    assert portal.status(account)["state"] == "sign_in_needed"


def test_disconnected_controls_remain_effective_without_browser_configuration(configured):
    accounts, account, _, _ = configured
    epoch = accounts.control(account).suspend()
    accounts.disconnect_owned(account, epoch)
    accounts.settings = dataclasses.replace(accounts.settings, public_url=None, login_url=None, login_token=None)
    with pytest.raises(HktvError):
        accounts.resolve(BEARER_CLIENT, person="second")


def test_late_cookie_refresh_cannot_restore_a_disconnected_session(configured):
    accounts, account, _, _ = configured
    session = accounts.get(account).session
    generation, _, _ = session.checkout_client()
    session.mark("live", generation)
    epoch = accounts.control(account).suspend()
    accounts.disconnect_owned(account, epoch)
    session.store_jar(to_jar(cookies("late-refresh")), generation)
    assert not (accounts.directory / account / "session.bin").exists()


def test_real_http_mcp_requires_bearer_and_a_verified_person(configured):
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
    from starlette.middleware import Middleware

    from hktvmall_mcp import server
    from hktvmall_mcp.connections import BrowserRoutes

    accounts, account, auth, _ = configured
    verifier = StaticTokenVerifier(tokens={"backend-only": {"client_id": BEARER_CLIENT, "scopes": []}})
    mcp = server.create_server(accounts, auth=verifier, browser_auth=auth, login=Login())
    app = mcp.http_app(**server.HTTP_OPTIONS, middleware=[Middleware(BrowserRoutes, connections=mcp.connections)])
    headers = {"Accept": "application/json, text/event-stream", "Mcp-Protocol-Version": "2025-03-26"}

    def call(tool, extra):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": {}}}
        return browser.post("/mcp", json=body, headers=[*headers.items(), *extra])

    with TestClient(app) as browser:
        assert call("hktv_status", [("X-Fleet-Person", "second")]).status_code == 401
        for values in ([], ["Unknown!"], ["second", "third"], ["default"]):
            response = call(
                "hktv_status", [("Authorization", "Bearer backend-only")] + [("X-Fleet-Person", v) for v in values]
            )
            result = response.json()["result"]
            assert result["isError"] and "not_connected" in result["content"][0]["text"]
        result = call("hktv_status", [("Authorization", "Bearer backend-only"), ("X-Fleet-Person", "second")]).json()
        payload = result["result"].get("structuredContent") or json.loads(result["result"]["content"][0]["text"])
        assert payload["account"] == "s•••@example.com" and payload["connection"] == {
            "owner": "second",
            "shared": False,
        }
        link = call("hktv_connect", [("Authorization", "Bearer backend-only"), ("X-Fleet-Person", "second")]).json()
        content = link["result"].get("structuredContent") or json.loads(link["result"]["content"][0]["text"])
        assert content == {"url": PUBLIC + "/connections/", "expires_at": None}
        # MCP calls and rejected retired entry routes allocate no human attempt.
        assert browser.get("/connect?req=retired").status_code == 404
        assert not mcp.connections.relay.links(account)
        assert mcp.connections.client.calls == []


def test_production_http_refuses_browser_sign_in_without_oidc(configured, monkeypatch):
    from hktvmall_mcp import server
    from hktvmall_mcp.config import ConfigError

    accounts, _, _, _ = configured
    monkeypatch.setenv("MCP_BEARER_TOKEN", "backend-only-token-at-least-sixteen")
    monkeypatch.setattr(server, "build_accounts", lambda settings: accounts)
    monkeypatch.setattr(server, "_start_keepalive", lambda accounts: pytest.fail("startup must refuse before upkeep"))
    with pytest.raises(ConfigError, match="Configure connection OIDC"):
        server.serve_http(accounts.settings, host="127.0.0.1", port=0)


def test_missing_shared_control_mount_refuses_configuration(settings, tmp_path):
    from hktvmall_mcp.config import ConfigError

    with pytest.raises(ConfigError, match="mounted before startup"):
        dataclasses.replace(settings, controls_dir=tmp_path / "missing-controls")


@pytest.mark.parametrize(
    "raw", ["{}", "malformed", "[]", '{"version":true}', '{"shared_with": "everyone"}', '{"shared_with": ["../x"]}']
)
def test_corrupt_control_state_never_reactivates_cached_account_access(configured, raw):
    accounts, account, _, _ = configured
    cached = accounts.get(account)
    control = accounts.control(account)
    if raw.startswith('{"shared_with"'):
        record = control.read()
        record.update(json.loads(raw))
        raw = json.dumps(record)
    control.path.write_text(raw)
    with pytest.raises(HktvError, match="connection_disconnected"):
        accounts.get(account)
    with pytest.raises(HktvError, match="connection_disconnected"):
        cached.session.checkout_client()
    accounts.person("third")
    with pytest.raises(HktvError, match="session_source_missing"):
        accounts.resolve(BEARER_CLIENT, person="third")  # it might be the record that shares with them: no fallback


def test_stale_owner_forms_and_cancel_cannot_mutate_new_attempt_or_credentials(configured):
    accounts, account, auth, portal = configured
    control = accounts.control(account)
    initial = control.read()
    stale = {"epoch": initial["epoch"], "state": initial["state"]}
    blob = accounts.directory / account / "session.bin"
    before = blob.read_bytes()
    with client(portal) as browser:
        browser.get("/connections/", headers=cookie(auth, "third"))
        first = browser.post("/connections/start", headers=cookie(auth), data=stale)
        assert first.status_code == 303
        old_epoch = control.read()["epoch"]
        assert browser.post("/connections/start", headers=cookie(auth)).status_code == 303
        assert control.read()["epoch"] != old_epoch
        current = control.read()
        for action in ("start", "disconnect"):
            rejected = browser.post("/connections/" + action, headers=cookie(auth), data=stale)
            assert rejected.status_code == 409 and 'href="/connections/"' in rejected.text
        rejected = browser.post("/connections/share", headers=cookie(auth), data={**stale, "person": "third"})
        assert rejected.status_code == 409
        assert (
            browser.post(first.headers["location"].replace(PUBLIC, "") + "/cancel", headers=cookie(auth)).status_code
            == 404
        )
        assert control.read() == current and blob.read_bytes() == before
        data = {"epoch": current["epoch"], "state": current["state"]}
        assert browser.post("/connections/disconnect", headers=cookie(auth), data=data).status_code == 303
        barrier = control.read()["epoch"]
        assert browser.post("/connections/disconnect", headers=cookie(auth), data=data).status_code == 409
        assert browser.post("/connections/start", headers=cookie(auth)).status_code == 303
        active = control.read()
        accounts.adopt_owned(account, active["epoch"], EMAIL, cookies("new-accepted-session"), "Synthetic Browser")
        newer = blob.read_bytes()
        with pytest.raises(HktvError):
            accounts.disconnect_owned(account, barrier)
        assert blob.read_bytes() == newer and control.read()["state"] == "active"


def test_owner_forms_require_captured_epoch_and_state(configured):
    accounts, account, auth, portal = configured
    with client(portal) as browser:
        browser.get("/connections/", headers=cookie(auth))  # records the display name
        before = accounts.control(account).read()
        for action in ("start", "disconnect", "share", "unshare", "leave"):
            assert (
                browser.post(
                    "/connections/" + action, headers=cookie(auth), data={"epoch": before["epoch"]}
                ).status_code
                == 409
            )
    assert accounts.control(account).read() == before


def test_signed_out_home_goes_straight_to_sign_in_and_owner_links_open_the_window(configured):
    accounts, account, auth, portal = configured
    with client(portal) as browser:
        home = browser.get("/connections/", follow_redirects=False)
        assert home.status_code == 303 and home.headers["location"].endswith("/connections/login")
        browser.post("/connections/start", headers=cookie(auth))  # pins the owner
        mismatch = browser.get("/connections/", headers=cookie(auth, subject="another-subject"))
        assert mismatch.status_code == 200 and "cannot manage this HKTVmall record" in mismatch.text
        assert 'action="/connections/logout"' in mismatch.text
        made = browser.post("/connections/start", headers=cookie(auth), follow_redirects=False)
        attempt = browser.get(urlsplit(made.headers["location"]).path, headers=cookie(auth))
        assert attempt.status_code == 200 and '"autoStart": true' in attempt.text


def test_disconnect_deletes_a_session_it_cannot_read(configured):
    accounts, account, auth, portal = configured
    blob = accounts.directory / account / "session.bin"
    blob.write_bytes(b"not a sealed session")  # e.g. sealed with a key since rotated
    before = accounts.control(account).read()["generation"]
    with client(portal) as browser:
        assert browser.post("/connections/disconnect", headers=cookie(auth)).status_code == 303
    record = accounts.control(account).read()
    assert record["state"] == "disconnected" and record["generation"] > before
    assert not blob.exists()
    with client(portal) as browser:
        assert browser.post("/connections/start", headers=cookie(auth)).status_code == 303  # reconnecting works


def test_too_many_sign_in_links_get_a_page_not_a_crash(configured):
    _, _, auth, portal = configured
    with client(portal) as browser:
        codes = [browser.post("/connections/start", headers=cookie(auth)).status_code for _ in range(8)]
    assert codes[: portal.relay.links_per_hour] == [303] * portal.relay.links_per_hour
    assert codes[-1] == 429


def test_oversized_forms_are_refused(configured):
    _, _, auth, portal = configured
    with client(portal) as browser:
        made = browser.post("/connections/disconnect", headers=cookie(auth), data={"epoch": "x" * 5000, "state": "a"})
    assert made.status_code == 409

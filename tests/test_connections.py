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
from test_accounts import choose, connect, control_of, cookies
from test_accounts import keyed as keyed  # noqa: F401
from test_site import OK_BASKET, ScriptedTransport

from hktvmall_mcp.accounts import BEARER_CLIENT
from hktvmall_mcp.connections import Connections
from hktvmall_mcp.errors import HktvError
from hktvmall_mcp.session import to_jar
from hktvmall_mcp.site import PATHS

PUBLIC = "https://app.example.com"
EMAIL = "second@example.com"
pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="hosted OIDC session storage requires POSIX private-state semantics"
)


class Login:
    def __init__(self):
        self.calls = []
        self.window_state = "waiting"

    def start(self, layout, **params):
        self.calls.append("start")
        self.window_state = "waiting"
        return {"id": "window", "state": "waiting", "view_token": "synthetic-token"}

    def state(self, window):
        return {"state": self.window_state, "view_token": "synthetic-token"}

    def result(self, window):
        return {"email": EMAIL, "cookies": cookies("replacement-session"), "user_agent": "Synthetic Browser"}

    def close(self, window, *, cancel_only=False):
        self.calls.append("close")
        if self.window_state in ("starting", "waiting"):
            self.window_state = "cancelled"

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

    def submit(self, page_headers, action, person=None, *, extra=None):
        """Press the page's button for ``action`` (and ``person``), as a browser would."""
        page = self.get("/connections/", headers=page_headers).text
        for path, fields in forms(page):
            if path.endswith("/" + action) and (person is None or fields.get("person") == person):
                return self.post(path, headers=page_headers, data={**fields, **(extra or {})})
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


def cookie(auth, person="second", subject=None, *, fresh=True, expired=False, name="Example person"):
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
            "expires": now - 60 if expired else now + 900,
            "auth_time": now if fresh else now - 600,
            "reauthenticated_at": now if fresh else None,
        },
    )
    return {"Cookie": f"{auth.cookie}={sid}", "Origin": PUBLIC}


def client(portal):
    return Browser(portal)


def synthetic_cart(accounts, *, home=None, basket=(200, OK_BASKET)):
    transport = ScriptedTransport(
        {
            PATHS["home"]: [
                home or (200, f"'loggedin' : true; 'realContactEmail' : \"{EMAIL}\"; CSRFToken = \"csrf\";")
            ],
            PATHS["cart_page"]: [(200, "Synthetic cart page")],
            PATHS["basket"]: [basket],
        }
    )
    accounts.transport.send = transport.send
    return transport


@pytest.mark.parametrize("ending", ["cancelled", "expired"])
def test_incomplete_reconnect_does_not_report_the_previous_session_as_connected(configured, ending):
    accounts, account, auth, portal = configured
    synthetic_cart(accounts)
    service = accounts.get(account)
    service.keepalive()
    service.session.mark("dead", service.session.generation)
    blob = accounts.directory / account / "session.bin"
    saved = blob.read_bytes()
    with client(portal) as browser:
        headers = cookie(auth)
        path = urlsplit(browser.post("/connections/start", headers=headers).headers["location"]).path
        assert browser.post(path + "/start", headers=headers).status_code == 200
        if ending == "cancelled":
            assert browser.post(path + "/cancel", headers=headers).status_code == 204
        else:
            portal.client.window_state = ending
        assert browser.get(path + "/state", headers=headers).json()["state"] in (
            "cancelled",
            "reconnect_incomplete",
        )
        assert "Sign-in did not complete" in browser.get("/connections/", headers=headers).text
        assert browser.get("/connections/status", headers=headers).json()["state"] == "reconnect_incomplete"
    assert blob.read_bytes() == saved
    accounts.forget(account)
    restarted = Connections(accounts, auth=auth, client=Login())
    accounts.get(account).keepalive()
    assert restarted.status(account)["state"] == "reconnect_incomplete"


@pytest.mark.parametrize(
    ("basket", "code"),
    [((403, "Synthetic denial"), "basket_unreadable"), ((200, '{"status":"SUCCESS"}'), "schema_changed")],
)
def test_new_sign_in_needs_cart_proof_for_its_own_generation(configured, caplog, basket, code):
    accounts, account, auth, portal = configured
    synthetic_cart(accounts)
    accounts.get(account).keepalive()
    with client(portal) as browser:
        headers = cookie(auth)
        path = urlsplit(browser.post("/connections/start", headers=headers).headers["location"]).path
        assert browser.post(path + "/start", headers=headers).status_code == 200
        portal.client.window_state = "signed_in"
        assert browser.get(path + "/state", headers=headers).json()["state"] == "verifying"
        assert "Verifying cart access" in browser.get("/connections/", headers=headers).text

        transport = synthetic_cart(accounts, basket=basket)
        service = accounts.get(account)
        with pytest.raises(HktvError, match=code):
            service.keepalive()
        assert PATHS["basket"] in transport.calls and service.session.status()["signed_in"]
        assert browser.get("/connections/status", headers=headers).json()["state"] == "check_needed"
        assert f"cart verification failed ({code})" in caplog.text
        assert EMAIL not in caplog.text and "Synthetic denial" not in caplog.text

        synthetic_cart(
            accounts,
            home=(200, f"'loggedin' : true, 'realContactEmail' : \"{EMAIL}\", 'CSRFToken' : \"csrf\""),
        )
        service.keepalive()
        assert browser.get("/connections/status", headers=headers).json()["state"] == "connected"
        assert "Connected ·" in browser.get("/connections/", headers=headers).text


def test_cart_verification_cannot_promote_a_provider_rejected_session(configured, caplog):
    accounts, account, auth, portal = configured
    with client(portal) as browser:
        headers = cookie(auth)
        path = urlsplit(browser.post("/connections/start", headers=headers).headers["location"]).path
        assert browser.post(path + "/start", headers=headers).status_code == 200
        portal.client.window_state = "signed_in"
        assert browser.get(path + "/state", headers=headers).json()["state"] == "verifying"
        transport = synthetic_cart(accounts, home=(200, "'loggedin' : false; CSRFToken = \"csrf\";"))
        with pytest.raises(HktvError, match="session_expired"):
            accounts.get(account).keepalive()
        assert transport.calls == [PATHS["home"]]
        assert browser.get("/connections/status", headers=headers).json()["state"] == "sign_in_needed"
        assert "provider sign-in proof failed (session_expired)" in caplog.text


def test_ui_reads_do_not_claim_a_connection_and_aged_owner_session_starts(configured):
    accounts, account, auth, portal = configured
    with client(portal) as browser:
        assert browser.get("/connections/status").status_code == 401
        assert browser.get("/connections/status", headers=cookie(auth)).status_code == 200
        assert accounts.control(account).read()["owner"] is None
        made = browser.post("/connections/start", headers=cookie(auth, fresh=False))
        assert made.status_code == 303 and "?reauth=1" not in made.headers["location"]
        assert accounts.control(account).read()["owner"] == {"issuer": auth.config.issuer, "sub": "subject-second"}
        assert browser.get("/connections/status", headers=cookie(auth, subject="other-subject")).status_code == 401


def test_first_visit_records_the_person_with_their_display_name_and_nothing_shared(configured):
    accounts, _, auth, portal = configured
    with client(portal) as browser:
        page = browser.get("/connections/", headers=cookie(auth, "newcomer", name="New <Comer>"))
    assert page.status_code == 200 and "No HKTVmall account connected yet" in page.text
    assert "Signed in to Connections as New &lt;Comer&gt;" in page.text
    assert "New <Comer>" not in page.text
    assert "Connect HKTVmall" in page.text and "ask them to share it" in page.text
    assert "newcomer" in accounts.known()
    record = accounts.control("newcomer").read()
    assert record["name"] == "New Comer" and record["owner"] is None and record.get("shared_with", []) == []
    assert accounts.control("second").read().get("shared_with", []) == []


def test_the_owner_shares_from_the_page_and_the_member_sees_whose_account(configured):
    accounts, _, auth, portal = configured
    synthetic_cart(accounts)
    accounts.get("second").keepalive()
    owner = cookie(auth, name="Alex Owner")
    member = cookie(auth, "third", name="Sam Member")
    with client(portal) as browser:
        assert "Nobody else has used HKTVmall here yet" in browser.get("/connections/", headers=owner).text
        browser.get("/connections/", headers=member)  # Sam opens the page once: now Sam is on the roster
        page = browser.get("/connections/", headers=owner).text
        assert "Shared with" in page and "Sam Member" in page and "cannot see your password" in page
        assert accounts.resolve(BEARER_CLIENT, person="third").account_id == "third"
        aged = cookie(auth, fresh=False, name="Alex Owner")
        assert browser.submit(aged, "share", "third").status_code == 303
        assert accounts.control("second").read()["shared_with"] == ["third"]
        assert accounts.resolve(BEARER_CLIENT, person="third").account_id == "third"
        assert browser.submit(member, "select", "second").status_code == 303
        assert accounts.resolve(BEARER_CLIENT, person="third").account_id == "second"
        member_page = browser.get("/connections/", headers=member).text
        assert "Using Alex Owner's HKTVmall account." in member_page and "Leave shared account" in member_page
        for action in ("disconnect", "share", "unshare"):
            assert f'action="/connections/{action}"' not in member_page
        owner_page = browser.get("/connections/", headers=owner).text
        assert "<strong>✓ Sam Member</strong>" in owner_page and "· Shared</span>" in owner_page
        # The order-history checkbox sits outside the share form so the buttons share a row; form= keeps it bound.
        assert 'form="share-third"' in owner_page and 'action="/connections/share" id="share-third"' in owner_page
        assert browser.submit(member, "leave", "second").status_code == 303
        assert accounts.control("second").read()["shared_with"] == []
        with pytest.raises(HktvError, match="account_selection_needed"):
            accounts.resolve(BEARER_CLIENT, person="third")
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
        assert browser.post("/connections/start", headers=member).status_code == 303
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
        browser.submit(cookie(auth, "third"), "select", "second")
        original = portal.status
        monkeypatch.setattr(
            portal,
            "status",
            lambda person: (
                {"state": "connecting", "url": PUBLIC + "/connect/x"} if person == account else original(person)
            ),
        )
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
    synthetic_cart(accounts)
    accounts.get(account).keepalive()
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


def test_people_with_owned_connections_and_other_shares_can_receive_offers(configured):
    accounts, _, auth, portal = configured
    connect(accounts, "third", "third@example.com", "third-session")
    accounts.note_name("third", "Kim")
    accounts.person("fourth")
    accounts.note_name("fourth", "Lee")
    accounts.share("third", "fourth", *control_of(accounts, "third"))
    choose(accounts, "fourth", "third")
    with client(portal) as browser:
        page = browser.get("/connections/", headers=cookie(auth)).text
        assert "Kim" in page
        assert "Lee" in page
        assert browser.submit(cookie(auth), "share", "third").status_code == 303
        assert browser.submit(cookie(auth), "share", "fourth").status_code == 303
    assert accounts.account_for("third") == "third"
    assert accounts.account_for("fourth") == "third"


def test_owned_and_shared_controls_are_independent_and_stale_selection_forms_change_nothing(configured):
    accounts, _, auth, portal = configured
    connect(accounts, "third", "third@example.com", "third-session")
    owner, member = cookie(auth), cookie(auth, "third")
    with client(portal) as browser:
        assert browser.submit(owner, "share", "third", extra={"history": "1"}).status_code == 303
        page = browser.get("/connections/", headers=member).text
        assert page.index("Shared accounts available to me") < page.index("<h2>Shared with</h2>")
        offered = next(
            fields for action, fields in forms(page) if action.endswith("/select") and fields["person"] == "second"
        )
        assert "and read its order history" in page
        assert browser.post("/connections/select", headers=member, data=offered).status_code == 303
        assert accounts.account_for("third") == "second"
        assert "Shared with" in browser.get("/connections/", headers=member).text  # manages their own slot
        assert browser.submit(member, "select", "third").status_code == 303
        assert browser.post("/connections/select", headers=member, data=offered).status_code == 409
        assert accounts.account_for("third") == "third"
        assert browser.submit(member, "select", "second").status_code == 303
        selected = accounts.control("third").read()["selection"]
        assert browser.submit(member, "disconnect").status_code == 303
        assert accounts.control("third").read()["selection"] == selected
        assert accounts.account_for("third") == "second"
        assert accounts.control("second").read()["state"] == "active"
        assert browser.submit(owner, "share", "third").status_code == 303  # history unchecked: new scope
        assert browser.get("/connections/status", headers=member).json()["state"] == "access_removed"
        assert browser.post("/connections/select", headers=member, data=offered).status_code == 409


@pytest.mark.parametrize("legacy_records", [False, True], ids=["retained-v2", "migrated-v1"])
def test_existing_grants_remain_selectable_through_authenticated_forms(configured, legacy_records):
    from test_selection import legacy

    accounts, _, auth, portal = configured
    connect(accounts, "third", "third@example.com", "third-session")
    accounts.share("second", "third", *control_of(accounts, "second"))
    if legacy_records:
        for person in ("second", "third"):
            legacy(accounts, person)
        accounts.person("third")  # resume the established native migration
    else:
        with accounts.control("second").edit() as data:
            data["shares"]["third"]["revision"] = "Aa_-" * 8
    grant = accounts.control("second").read()["shares"]["third"]["revision"]
    assert any(character not in "abcdef0123456789" for character in grant)
    before = {person: accounts.control(person).read() for person in ("second", "third")}
    member = cookie(auth, "third")
    with client(portal) as browser:
        for target in ("second", "third", "second"):
            assert browser.submit(member, "select", target).status_code == 303
            assert accounts.account_for("third") == target
    for person in before:
        after = accounts.control(person).read()
        for field in ("epoch", "state", "generation", "provider_email", "shares"):
            assert after[field] == before[person][field]


def test_selection_cannot_target_the_default_account_or_accept_forged_grants(configured):
    accounts, _, auth, portal = configured
    member = cookie(auth, "third")
    with client(portal) as browser:
        browser.get("/connections/", headers=member)
        browser.submit(cookie(auth), "share", "third")
        offered = next(
            fields
            for action, fields in forms(browser.get("/connections/", headers=member).text)
            if action.endswith("/select")
        )
        for target, grant in (
            ("default", "own"),
            ("second", "own"),
            ("second", "x" * 32),
            ("second", "x" * 1025),
            ("../second", "own"),
        ):
            forged = {**offered, "person": target, "grant": grant}
            assert browser.post("/connections/select", headers=member, data=forged).status_code == 409
        assert accounts.account_for("third") == "third"
        assert browser.post("/connections/select", data=offered).status_code == 401
        assert (
            browser.post("/connections/select", headers={"Cookie": member["Cookie"]}, data=offered).status_code == 403
        )


@pytest.mark.parametrize("damaged", ["second", "unrelated"])
@pytest.mark.parametrize("missing", [False, True])
def test_unavailable_records_preserve_healthy_choices_and_owner_management(configured, damaged, missing):
    accounts, _, auth, portal = configured
    connect(accounts, "third", "third@example.com", "third-session")
    connect(accounts, "fourth", "fourth@example.com", "fourth-session")
    accounts.person("unrelated")
    accounts.share("second", "third", *control_of(accounts, "second"))
    accounts.share("fourth", "third", *control_of(accounts, "fourth"))
    accounts.share("third", "second", *control_of(accounts, "third"))
    choose(accounts, "third", "second")
    control_path = accounts.directory / damaged / "connection.json"
    if missing:
        control_path.unlink()
    else:
        control_path.write_text("damaged")
    member = cookie(auth, "third")
    with client(portal) as browser:
        page = browser.get("/connections/", headers=member)
        assert page.status_code == 200 and "administrator’s attention" in page.text
        assert "Use my account" in page.text and "Disconnect" in page.text and "Stop sharing" in page.text
        assert any(action.endswith("/select") and fields["person"] == "fourth" for action, fields in forms(page.text))
        if damaged == "second":
            with pytest.raises(HktvError, match="session_source_missing"):
                accounts.account_for("third")
        else:
            assert accounts.account_for("third") == "second"
        assert browser.submit(member, "select", "fourth").status_code == 303
        assert accounts.account_for("third") == "fourth"
        assert browser.submit(member, "select", "third").status_code == 303
        assert accounts.account_for("third") == "third"
        assert browser.submit(member, "unshare", "second").status_code == 303
        assert accounts.control("third").read()["shares"] == {}
        assert browser.submit(member, "disconnect").status_code == 303
        assert accounts.control("third").read()["state"] == "disconnected"
    assert not control_path.exists() if missing else control_path.read_text() == "damaged"


@pytest.mark.parametrize("switch_first", [False, True])
def test_leaving_after_scope_change_clears_stale_selection_but_preserves_another_choice(configured, switch_first):
    accounts, _, auth, portal = configured
    connect(accounts, "third", "third@example.com", "third-session")
    owner, member = cookie(auth), cookie(auth, "third")
    with client(portal) as browser:
        browser.submit(owner, "share", "third")
        browser.submit(member, "select", "second")
        old_leave = next(
            fields
            for action, fields in forms(browser.get("/connections/", headers=member).text)
            if action.endswith("/leave")
        )
        browser.submit(owner, "share", "third", extra={"history": "1"})
        before = accounts.control("third").read()
        assert browser.post("/connections/leave", headers=member, data=old_leave).status_code == 409
        assert accounts.control("third").read() == before
        leave = next(
            fields
            for action, fields in forms(browser.get("/connections/", headers=member).text)
            if action.endswith("/leave")
        )
        if switch_first:
            browser.submit(member, "select", "third")
        before = accounts.control("third").read()
        assert browser.post("/connections/leave", headers=member, data=leave).status_code == 303
        assert accounts.control("second").read()["shares"] == {}
        after = accounts.control("third").read()
        if switch_first:
            assert after == before and accounts.account_for("third") == "third"
        else:
            assert after["selection"] is None and after["selection_revision"] != before["selection_revision"]
            with pytest.raises(HktvError, match="account_selection_needed"):
                accounts.account_for("third")


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
        browser.submit(cookie(auth, "third"), "select", "second")
        assert browser.post("/connections/start", headers=cookie(auth)).status_code == 303
        floor = accounts.control(account).read()["generation"]
        assert browser.post("/connections/disconnect", headers=cookie(auth)).status_code == 303
    record = accounts.control(account).read()
    assert record["state"] == "disconnected" and record["provider_email"] == EMAIL
    assert record["owner"]["sub"] == "subject-second" and record["generation"] == floor
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
    assert accounts.get(account).session.generation > floor
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
    accounts.person("third")
    accounts.share(account, "third", *control_of(accounts, account))
    choose(accounts, "third", account)
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
        accounts.resolve(BEARER_CLIENT, person="third")  # selected owner controls are unavailable: no fallback


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
        for action in ("start", "disconnect", "share", "unshare", "leave", "select"):
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
        assert "?reauth=1" not in home.headers["location"]
        browser.post("/connections/start", headers=cookie(auth))  # pins the owner
        mismatch = browser.get("/connections/", headers=cookie(auth, subject="another-subject"))
        assert mismatch.status_code == 200 and "cannot manage this HKTVmall connection" in mismatch.text
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


def test_aged_sign_in_renders_connect_form_and_refusals_explain(configured):
    _, _, auth, portal = configured
    with client(portal) as browser:
        aged = browser.get("/connections/", headers=cookie(auth, fresh=False)).text
        assert 'action="/connections/start"' in aged
        assert "?reauth=1" not in aged and "Verify and" not in aged
        signed_out = browser.post("/connections/start", headers={"Origin": PUBLIC})
        assert signed_out.status_code == 401 and "Your sign-in has ended" in signed_out.text
        expired = browser.post("/connections/start", headers=cookie(auth, expired=True))
        assert expired.status_code == 401 and "Your sign-in has ended" in expired.text
        foreign = browser.post("/connections/start", headers={**cookie(auth), "Origin": "https://other.example.com"})
        assert foreign.status_code == 403 and "Request refused" in foreign.text


def test_aged_visit_to_a_sign_in_window_enters_directly_and_signed_out_is_refused(configured):
    from starlette.applications import Starlette

    from hktvmall_mcp.connections import BrowserRoutes

    _, _, auth, portal = configured
    app = BrowserRoutes(Starlette(), portal)
    aged = cookie(auth, fresh=False)
    with client(portal) as portal_browser:
        made = portal_browser.post("/connections/start", headers=aged)
        assert made.status_code == 303
        attempt = urlsplit(made.headers["location"]).path
    with TestClient(app, base_url=PUBLIC, follow_redirects=False) as browser:
        page = browser.get(attempt, headers=aged)
        assert page.status_code == 200 and '"autoStart": true' in page.text
        assert "?reauth=1" not in page.text
        assert browser.get(attempt).status_code == 404
        assert browser.get(attempt, headers=cookie(auth, "third")).status_code == 404


@pytest.mark.parametrize("action", ["start", "disconnect", "share", "unshare", "leave", "select"])
@pytest.mark.parametrize("origin", [None, "https://other.example.com", PUBLIC + "/", "http://app.example.com"])
def test_every_connection_mutation_requires_the_exact_origin_and_a_browser_session(configured, action, origin):
    accounts, account, auth, portal = configured
    connect(accounts, "third", "third@example.com", "third-session")
    owner, member = cookie(auth), cookie(auth, "third")
    with client(portal) as browser:
        browser.get("/connections/", headers=member)
        browser.submit(owner, "share", "third")
        browser.submit(member, "select", account)
        actor = "third" if action in ("leave", "select") else account
        headers = member if actor == "third" else owner
        candidates = forms(browser.get("/connections/", headers=headers).text)
        path, fields = next((path, fields) for path, fields in candidates if path.endswith("/" + action))
        before = {person: accounts.control(person).read() for person in accounts.known()}
        blobs = {person: (accounts.directory / person / "session.bin").read_bytes() for person in before}
        invalid = {"Cookie": headers["Cookie"]}
        if origin is not None:
            invalid["Origin"] = origin
        refused = browser.post(path, headers=invalid, data=fields)
        assert refused.status_code == 403 and "Request refused" in refused.text
        signed_out = browser.post(path, headers={"Origin": PUBLIC}, data=fields)
        assert signed_out.status_code == 401 and "Your sign-in has ended" in signed_out.text
        expired = browser.post(path, headers=cookie(auth, actor, expired=True), data=fields)
        assert expired.status_code == 401 and "Your sign-in has ended" in expired.text
        assert "location" not in refused.headers and "location" not in signed_out.headers
        assert "location" not in expired.headers
    assert {person: accounts.control(person).read() for person in before} == before
    assert {person: (accounts.directory / person / "session.bin").read_bytes() for person in before} == blobs
    assert portal.client.calls == [] and not portal.relay.links(account)


@pytest.mark.parametrize("action", ["start", "share"])
def test_aged_session_executes_start_and_share_without_step_up(configured, action):
    accounts, account, auth, portal = configured
    with client(portal) as browser:
        browser.get("/connections/", headers=cookie(auth, "third"))
        aged = cookie(auth, fresh=False)
        page = browser.get("/connections/", headers=aged).text
        assert "?reauth=1" not in page and "Verify and" not in page
        path, fields = next((path, fields) for path, fields in forms(page) if path.endswith("/" + action))
        before = accounts.control(account).read()
        response = browser.post(path, headers=aged, data=fields)
        assert response.status_code == 303 and "?reauth=1" not in response.headers["location"]
        assert accounts.control(account).read() != before


def test_interrupted_disconnect_stays_blocked_after_restart_even_with_retained_credentials(
    configured, service, clock, monkeypatch
):
    from hktvmall_mcp.accounts import Accounts

    accounts, account, auth, portal = configured
    with client(portal) as browser:
        browser.get("/connections/", headers=cookie(auth, "third"))
        browser.submit(cookie(auth), "share", "third")
        browser.submit(cookie(auth, "third"), "select", account)
        blob = accounts.directory / account / "session.bin"
        sealed = blob.read_bytes()

        def unavailable(*args):
            raise OSError("synthetic unavailable credential storage")

        with monkeypatch.context() as interrupted:
            interrupted.setattr(accounts, "disconnect_owned", unavailable)
            assert browser.submit(cookie(auth), "disconnect").status_code == 303
        barrier = accounts.control(account).read()
        assert barrier["state"] == "disconnecting" and blob.read_bytes() == sealed

    restarted = Accounts(accounts.settings, accounts.transport, service, clock=clock)
    assert restarted.control(account).read() == barrier
    for person in (account, "third"):
        with pytest.raises(HktvError, match="connection_disconnected"):
            restarted.resolve(BEARER_CLIENT, person=person)
    rebuilt = Connections(restarted, auth=auth, client=Login())
    with client(rebuilt) as browser:
        page = browser.get("/connections/", headers=cookie(auth))
        assert "Access stopped" in page.text and "Finish disconnecting" in page.text
        assert browser.submit(cookie(auth), "disconnect").status_code == 303
    after = restarted.control(account).read()
    assert after["state"] == "disconnected" and not blob.exists()
    assert after["provider_email"] == barrier["provider_email"] and after["shares"] == barrier["shares"]


def test_start_new_sign_in_shows_view_layout_preserves_window_on_429_and_fences_stuck_teardown(configured):
    import threading

    accounts, account, auth, portal = configured
    headers = cookie(auth)
    with client(portal) as browser:
        first = browser.post("/connections/start", headers=headers)
        assert first.status_code == 303
        first_path = urlsplit(first.headers["location"]).path
        assert browser.post(first_path + "/start", headers=headers, json={"layout": "phone"}).status_code == 200
        page = browser.get("/connections/", headers=headers).text
        assert "Sign-in in progress (mobile view)" in page
        assert "Continue sign-in" in page
        assert "Start new sign-in" in page
        aged_page = browser.get("/connections/", headers=cookie(auth, fresh=False)).text
        assert "Start new sign-in" in aged_page
        assert 'action="/connections/start"' in aged_page
        assert "Verify and" not in aged_page and "?reauth=1" not in aged_page

        # Exhaust rate limit: 429 must preserve the currently open window and epoch.
        portal.relay.links_per_hour = 1
        before_epoch = accounts.control(account).read()["epoch"]
        limited = browser.post("/connections/start", headers=headers)
        assert limited.status_code == 429
        assert accounts.control(account).read()["epoch"] == before_epoch
        assert browser.get(first_path + "/state", headers=headers).json()["state"] in ("starting", "waiting")
        portal.relay.links_per_hour = 6

        # Stuck backend teardown returns 409 and closes the replacement link.
        original_close = portal.client.close
        portal.client.close = lambda window, *, cancel_only=False: portal.client.calls.append("stuck-close")
        stuck = browser.post("/connections/start", headers=headers)
        assert stuck.status_code == 409
        assert "still closing" in stuck.text
        assert accounts.control(account).read()["epoch"] == before_epoch
        assert portal.status(account)["state"] != "connecting"
        # Retry while still stuck also returns 409 without creating a usable link.
        assert browser.post("/connections/start", headers=headers).status_code == 409
        # Once teardown finishes, the next start succeeds.
        portal.client.close = original_close
        recovered = browser.post("/connections/start", headers=headers)
        assert recovered.status_code == 303

        # Saving sign-in cannot be overwritten by a replacement start.
        saved_path = urlsplit(recovered.headers["location"]).path
        assert browser.post(saved_path + "/start", headers=headers).status_code == 200
        link = portal.relay.links(account)[-1]
        link.state = "saving"
        assert browser.post("/connections/start", headers=headers).status_code == 409
        assert "Saving connection" in browser.get("/connections/", headers=headers).text
        assert "Start new sign-in" not in browser.get("/connections/", headers=headers).text

        # Concurrent tab during teardown cannot open or start the replacement link.
        link.state = "waiting"
        entered = threading.Event()
        release = threading.Event()

        def slow_close(window, *, cancel_only=False):
            entered.set()
            release.wait(5)
            original_close(window, cancel_only=cancel_only)

        portal.client.close = slow_close
        result = {}
        worker = threading.Thread(
            target=lambda: result.setdefault("r", browser.post("/connections/start", headers=headers))
        )
        worker.start()
        assert entered.wait(5)
        assert portal.status(account)["state"] != "connecting"
        assert all(item is link for item in portal.relay.links(account))
        release.set()
        worker.join(5)
        assert result["r"].status_code == 303


def test_replacement_preserves_just_completed_sign_in_and_closes_failed_candidate(configured, monkeypatch):
    accounts, account, auth, portal = configured
    headers = cookie(auth)
    with client(portal) as browser:
        first = browser.post("/connections/start", headers=headers)
        assert first.status_code == 303
        first_path = urlsplit(first.headers["location"]).path
        assert browser.post(first_path + "/start", headers=headers).status_code == 200
        old_link = portal.relay.links(account)[-1]
        epoch_before = accounts.control(account).read()["epoch"]
        gen_before = accounts.control(account).read()["generation"]

        # Simulate old link completing adoption (state -> verifying) while teardown runs.
        orig_cancel = portal.relay.cancel

        def complete_on_cancel(target, *, for_restart=False):
            if target is old_link:
                target.window_id = None
                target.state = "verifying"
                return
            orig_cancel(target, for_restart=for_restart)

        monkeypatch.setattr(portal.relay, "cancel", complete_on_cancel)
        raced = browser.post("/connections/start", headers=headers)
        assert raced.status_code == 409
        assert accounts.control(account).read()["epoch"] == epoch_before
        assert accounts.control(account).read()["generation"] == gen_before
        assert all(link.closed for link in portal.relay.links(account) if link is not old_link)
        monkeypatch.setattr(portal.relay, "cancel", orig_cancel)

        # If control.resume raises OSError, candidate link must be closed and unusable under old epoch.
        old_link.state = "cancelled"
        control = accounts.control(account)
        monkeypatch.setattr(control, "resume", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
        monkeypatch.setattr(accounts, "control", lambda person: control)
        with pytest.raises(OSError, match="disk full"):
            browser.post("/connections/start", headers=headers)
        assert portal.status(account)["state"] != "connecting"
        for candidate in portal.relay.links(account):
            if candidate is not old_link:
                assert candidate.closed is True
                assert browser.post(urlsplit(candidate.url).path + "/start", headers=headers).status_code == 404


def test_aged_session_toolbar_restart_cancels_active_window(configured):
    _, _, auth, portal = configured
    with client(portal) as browser:
        first = browser.post("/connections/start", headers=cookie(auth))
        assert first.status_code == 303
        first_path = urlsplit(first.headers["location"]).path
        assert browser.post(first_path + "/start", headers=cookie(auth)).status_code == 200
        wrong_owner = browser.post(
            first_path + "/cancel",
            headers={**cookie(auth, "third"), "Content-Type": "application/json"},
            content=json.dumps({"for_restart": True}),
        )
        assert wrong_owner.status_code == 404
        aged_restart = browser.post(
            first_path + "/cancel",
            headers={**cookie(auth, fresh=False), "Content-Type": "application/json"},
            content=json.dumps({"for_restart": True}),
        )
        assert aged_restart.status_code == 200
        assert "close" in portal.client.calls
        assert browser.post(first_path + "/start", headers=cookie(auth, fresh=False)).status_code == 200


def test_pending_close_completion_expired_link_and_terminal_reconnect(configured, monkeypatch):
    accounts, account, auth, portal = configured
    headers = cookie(auth)
    with client(portal) as browser:
        first = browser.post("/connections/start", headers=headers)
        assert first.status_code == 303
        first_path = urlsplit(first.headers["location"]).path
        assert browser.post(first_path + "/start", headers=headers).status_code == 200
        old_link = portal.relay.links(account)[-1]
        epoch_before = accounts.control(account).read()["epoch"]
        gen_before = accounts.control(account).read()["generation"]

        # 1. Pending close retry that observes completion (verifying) must return 409 and keep generation/epoch.
        orig_close = portal.client.close
        portal.client.close = lambda window, *, cancel_only=False: None
        assert browser.post("/connections/start", headers=headers).status_code == 409
        assert old_link in portal._pending_close.get(account, [])

        orig_cancel = portal.relay.cancel

        def finish_on_retry(target, *, for_restart=False):
            if target is old_link:
                target.window_id = None
                target.closing = False
                target.state = "verifying"
                return
            orig_cancel(target, for_restart=for_restart)

        monkeypatch.setattr(portal.relay, "cancel", finish_on_retry)
        retry = browser.post("/connections/start", headers=headers)
        assert retry.status_code == 409
        assert "just completed" in retry.text
        assert accounts.control(account).read()["epoch"] == epoch_before
        assert accounts.control(account).read()["generation"] == gen_before
        monkeypatch.setattr(portal.relay, "cancel", orig_cancel)
        portal.client.close = orig_close

        # 2. An already-terminal verifying link must allow intentional Reconnect without 409 or wasted attempts.
        portal.relay.links_per_hour = 3
        reconnect = browser.post("/connections/start", headers=headers)
        assert reconnect.status_code == 303
        rec_path = urlsplit(reconnect.headers["location"]).path
        assert browser.post(rec_path + "/start", headers=headers).status_code == 200
        active_link = portal.relay.links(account)[-1]

        # 3. An expired waiting link popped from _links before _retired is populated must still be fenced.
        epoch_mid = accounts.control(account).read()["epoch"]
        with portal.relay._lock:
            portal.relay._links.pop(active_link.id, None)
            portal.relay._retired.pop(active_link.id, None)
        portal.client.close = lambda window, *, cancel_only=False: None
        portal.relay.links_per_hour = 6
        blocked = browser.post("/connections/start", headers=headers)
        assert blocked.status_code == 409
        assert "still closing" in blocked.text
        assert accounts.control(account).read()["epoch"] == epoch_mid
        portal.client.close = orig_close


def test_adoption_lock_race_cancelled_retry_and_disconnect_stuck_close(configured):
    import threading

    accounts, account, auth, portal = configured
    headers = cookie(auth)
    with client(portal) as browser:
        first = browser.post("/connections/start", headers=headers)
        first_path = urlsplit(first.headers["location"]).path
        assert browser.post(first_path + "/start", headers=headers).status_code == 200
        old_link = portal.relay.links(account)[-1]
        epoch_before = accounts.control(account).read()["epoch"]
        gen_before = accounts.control(account).read()["generation"]

        # 1a. Adoption holding old_link.lock while Start new sign-in arrives must return 409, not 303.
        locked = threading.Event()
        snapshotted = threading.Event()
        release = threading.Event()
        orig_snapshot = portal._snapshot_links

        def observe_snapshot(p):
            result = orig_snapshot(p)
            snapshotted.set()
            release.wait(5)
            return result

        portal._snapshot_links = observe_snapshot

        def finish_adoption():
            with old_link.lock:
                locked.set()
                assert snapshotted.wait(5)
                old_link.window_id = None
                old_link.state = "verifying"
            release.set()

        adopter = threading.Thread(target=finish_adoption)
        adopter.start()
        assert locked.wait(5)
        res = {}
        requester = threading.Thread(
            target=lambda: res.setdefault("r", browser.post("/connections/start", headers=headers))
        )
        requester.start()
        adopter.join(5)
        requester.join(5)
        portal._snapshot_links = orig_snapshot
        assert res["r"].status_code == 409
        assert accounts.control(account).read()["epoch"] == epoch_before
        assert accounts.control(account).read()["generation"] == gen_before

        # 1b. Repeated stale replace=old_link.id submissions after completion
        # or expiry return 409 and consume 0 attempts.
        cur = accounts.control(account).read()
        before_made = len(portal.relay._made.get(account, ()))
        for _ in range(3):
            bound = browser.post(
                "/connections/start",
                headers=headers,
                data={"epoch": cur["epoch"], "state": cur["state"], "replace": old_link.id},
            )
            assert bound.status_code == 409
            assert accounts.control(account).read()["epoch"] == epoch_before
            assert accounts.control(account).read()["generation"] == gen_before
        assert len(portal.relay._made.get(account, ())) == before_made

        # 1c. Expired target link still in _known_links must return 409 and consume 0 attempts.
        exp_resp = browser.post("/connections/start", headers=headers)
        assert exp_resp.status_code == 303
        exp_link = portal.relay.links(account)[-1]
        cur_exp = accounts.control(account).read()
        exp_made = len(portal.relay._made.get(account, ()))
        with portal.relay._lock:
            portal.relay._links.pop(exp_link.id, None)
        exp_bound = browser.post(
            "/connections/start",
            headers=headers,
            data={"epoch": cur_exp["epoch"], "state": cur_exp["state"], "replace": exp_link.id},
        )
        assert exp_bound.status_code == 409
        assert accounts.control(account).read()["epoch"] == cur_exp["epoch"]
        assert len(portal.relay._made.get(account, ())) == exp_made

        # 2. Retryable cancelled link reopened concurrently is still drained and fenced.
        exp_path = urlsplit(exp_resp.headers["location"]).path
        exp_link.state = "cancelled"
        exp_link.closed = False
        with portal.relay._lock:
            portal.relay._links[exp_link.id] = exp_link
        assert browser.post(exp_path + "/start", headers=headers).status_code == 200
        orig_close = portal.client.close
        portal.client.close = lambda window, *, cancel_only=False: None
        assert browser.post("/connections/start", headers=headers).status_code == 409
        assert exp_link in portal._pending_close.get(account, [])

        # 3. Disconnect with stuck backend close preserves _pending_close until backend confirms teardown.
        accounts.control(account).read()
        assert browser.submit(headers, "disconnect").status_code == 303
        assert exp_link in portal._pending_close.get(account, [])
        still_blocked = browser.post("/connections/start", headers=headers)
        assert still_blocked.status_code == 409
        assert "still closing" in still_blocked.text
        portal.client.close = orig_close
        assert browser.post("/connections/start", headers=headers).status_code == 303

        # 4. Pre-reserved shared capacity survives another account retiring a link during drain.
        from signin_window.relay import Link

        portal.relay.max_links = 2
        with portal.relay._lock:
            portal.relay._retired["r1"] = Link(
                "r1", "other", 0.0, 1.0, "https://app.example.com/connect/r1", {}, "other"
            )
            portal.relay._links["live1"] = Link(
                "live1", "other3", 1.0, 9_999_999_999.0, "https://app.example.com/connect/live1", {}, "other3"
            )
            portal.relay._links["live2"] = Link(
                "live2", "other4", 2.0, 9_999_999_999.0, "https://app.example.com/connect/live2", {}, "other4"
            )
        orig_cancel = portal._cancel_and_check

        def fill_retired_during_drain(link):
            with portal.relay._lock:
                portal.relay._retired["r2"] = Link(
                    "r2", "other2", 0.0, 1.0, "https://app.example.com/connect/r2", {}, "other2"
                )
            return orig_cancel(link)

        portal._cancel_and_check = fill_retired_during_drain
        try:
            assert browser.post("/connections/start", headers=headers).status_code == 303
            with portal.relay._lock:
                assert len(portal.relay._links) <= portal.relay.max_links
        finally:
            portal._cancel_and_check = orig_cancel
            with portal.relay._lock:
                portal.relay._retired.pop("r1", None)
                portal.relay._retired.pop("r2", None)
                portal.relay._links.pop("live1", None)
                portal.relay._links.pop("live2", None)
            portal.relay.max_links = 50

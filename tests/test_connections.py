"""Owner-authenticated UI, trusted account routing and durable publication barriers."""

import dataclasses
import json
import os
import secrets
import time
from contextlib import nullcontext
from datetime import UTC, datetime
from urllib.parse import urlsplit

import pytest
from signin_window.oidc import BrowserAuth, OIDCConfig
from starlette.testclient import TestClient
from test_accounts import accounts as accounts  # noqa: F401
from test_accounts import cookies
from test_accounts import keyed as keyed

from hktvmall_mcp.accounts import BEARER_CLIENT, DEFAULT
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


class Browser(TestClient):
    def __init__(self, portal):
        self.connection_portal = portal
        super().__init__(portal.app, base_url=PUBLIC, follow_redirects=False)

    def post(self, url, **kwargs):
        if url in ("/connections/start", "/connections/disconnect") and "data" not in kwargs:
            headers = kwargs.get("headers", {})
            value = headers.get("Cookie", "").split("=", 1)[-1]
            identity = self.connection_portal.auth.store.load(value)
            alias = identity.get("claims", {}).get("person_id")
            account = self.connection_portal.accounts.bindings().get(alias)
            if account is not None:
                current = self.connection_portal.accounts.control(account).read()
                kwargs["data"] = {"epoch": current["epoch"], "state": current["state"]}
        return super().post(url, **kwargs)


@pytest.fixture
def configured(accounts, tmp_path):
    account = accounts.adopt(EMAIL, cookies("synthetic-session"), "Synthetic Browser")
    bindings = tmp_path / "accounts.json"
    bindings.write_text(json.dumps({"first": DEFAULT, "second": account}))
    accounts.settings = dataclasses.replace(
        accounts.settings,
        accounts_file=bindings,
        public_url=PUBLIC,
        trusted_account_header="x-fleet-person",
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
    return accounts, account, auth, portal


def cookie(auth, person="second", subject=None, *, fresh=True):
    sid = secrets.token_urlsafe(32)
    now = time.time()
    auth.store.save(
        sid,
        {
            "issuer": auth.config.issuer,
            "sub": subject or "subject-" + person,
            "name": "Example person",
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
        assert portal.client.normal.calls == []
        assert browser.post(path + "/start", headers=cookie(auth)).status_code == 200
        assert browser.get(path + "/novnc/vnc.html", headers=cookie(auth)).status_code == 200


def test_disconnect_preserves_ids_budgets_identity_and_generation_but_deletes_owned_credentials(configured):
    accounts, account, auth, portal = configured
    service = accounts.get(account)
    old_epoch = accounts.control(account).require_active()
    retained = service.state.path
    before = retained.read_bytes() if retained.exists() else None
    with client(portal) as browser:
        assert browser.post("/connections/start", headers=cookie(auth)).status_code == 303
        assert browser.post("/connections/disconnect", headers=cookie(auth)).status_code == 303
    record = accounts.control(account).read()
    assert record["state"] == "disconnected" and record["provider_email"] == EMAIL
    assert record["owner"]["sub"] == "subject-second" and record["generation"] == 1
    assert not (accounts.directory / account / "session.bin").exists()
    assert accounts.account_for("second") == account
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
    with pytest.raises(HktvError, match="account_mismatch"):
        accounts.adopt_owned(account, "initial", "different@example.com", cookies(), "Browser")
    assert path.read_bytes() == before


def test_known_dead_session_does_not_look_connected(configured):
    accounts, account, _, portal = configured
    session = accounts.get(account).session
    generation, _, _ = session.checkout_client()
    session.mark("dead", generation)
    assert portal.status(account)["state"] == "sign_in_needed"


def test_shared_account_use_does_not_grant_management(configured, tmp_path):
    accounts, _, auth, _ = configured
    accounts.settings.accounts_file.write_text(json.dumps({"first": DEFAULT, "second": DEFAULT}))
    with pytest.raises(HktvError):
        accounts.managers()
    managers = tmp_path / "managers.json"
    managers.write_text(json.dumps({DEFAULT: "first"}))
    accounts.settings = dataclasses.replace(accounts.settings, managers_file=managers)
    portal = Connections(accounts, auth=auth, client=Login())
    assert accounts.resolve(BEARER_CLIENT, person="second") is accounts.default
    assert accounts.resolve(BEARER_CLIENT, person="first") is accounts.default
    with client(portal) as browser:
        assert browser.get("/connections/status", headers=cookie(auth, "second")).status_code == 401
        assert browser.post("/connections/start", headers=cookie(auth, "second")).status_code == 403
        assert browser.get("/connections/status", headers=cookie(auth, "first")).status_code == 200
    for person in (None, "unknown", ""):
        with pytest.raises(HktvError):
            accounts.resolve(BEARER_CLIENT, person=person)


def test_default_disconnect_retains_snapshot_and_recovery_opens_a_real_window(configured):
    accounts, _, auth, portal = configured
    path = accounts.settings.snapshot_path
    path.parent.mkdir(parents=True)
    path.write_text("externally owned synthetic snapshot")
    with client(portal) as browser:
        assert browser.post("/connections/disconnect", headers=cookie(auth, "first")).status_code == 303
        assert accounts.control(DEFAULT).read()["owner"] == {"issuer": auth.config.issuer, "sub": "subject-first"}
        assert (
            browser.get("/connections/status", headers=cookie(auth, "first", subject="replacement-subject")).status_code
            == 401
        )
        page = browser.get("/connections/", headers=cookie(auth, "first"))
        assert "retains this account's saved profile" in page.text
        assert ">Connect<" in page.text
        with pytest.raises(HktvError):
            accounts.resolve(BEARER_CLIENT, person="first")
        assert path.read_text() == "externally owned synthetic snapshot"
        response = browser.post("/connections/start", headers=cookie(auth, "first"))
        assert response.status_code == 303 and response.headers["location"].startswith(PUBLIC + "/connect/")
        link = portal.relay.links(DEFAULT)[0]
        portal.relay.start(link, "desktop")
        assert link.window_id.startswith("m_")
    assert accounts.control(DEFAULT).read()["state"] == "active"
    assert path.read_text() == "externally owned synthetic snapshot"
    assert portal.client.normal.calls == ["start"]


def test_native_positive_signed_out_survives_reader_restart_and_new_generation_supersedes_it(configured, tmp_path):
    from signin_window.sessions import AttemptBudget

    from hktvmall_maintenance.keeper import IdentityKeeper
    from hktvmall_maintenance.models import PersistenceError
    from hktvmall_maintenance.outcomes import IdentityOutcome, SessionDead
    from hktvmall_maintenance.state import TargetStateStore
    from hktvmall_mcp.fsio import atomic_write_json
    from hktvmall_mcp.server import build_accounts
    from hktvmall_mcp.session import session_payload

    accounts, _, auth, portal = configured
    settings = accounts.settings
    expected = accounts.default.session.expected_email
    path = settings.snapshot_path
    path.parent.mkdir(parents=True)
    atomic_write_json(path, session_payload(1, expected, "Synthetic Browser", cookies(), time.time()))
    accounts = build_accounts(settings)
    portal = Connections(accounts, auth=auth, client=Login())
    assert portal.status(DEFAULT)["state"] == "connected"
    control = accounts.control(DEFAULT)
    original_epoch = control.require_active()

    class DeadBrowser:
        def warm(self):
            raise SessionDead()  # Actual positive native observation, not an outcome-label inference.

    now = datetime.now(UTC)
    keeper = IdentityKeeper(
        target="hktvmall",
        expected_email=expected,
        export_dir=path.parent,
        state=TargetStateStore(tmp_path / "keeper-state", utc_now=lambda: now),
        budget=AttemptBudget(tmp_path / "keeper-budget", utc_now=lambda: now, error_type=PersistenceError),
        readiness=lambda: pytest.fail("no automatic credential sign-in"),
        open_browser=lambda: nullcontext(DeadBrowser()),
        lease=nullcontext,
        utc_now=lambda: now,
        snapshot_generation=lambda _: 1,
        connection_control=control,
    )
    accepted = path.read_bytes()
    assert keeper.run(allow_credential_mint=False).outcome == IdentityOutcome.MINT_NOT_PERMITTED
    assert path.read_bytes() == accepted
    restarted = Connections(build_accounts(settings), auth=auth, client=Login())
    assert restarted.status(DEFAULT)["state"] == "sign_in_needed"
    # Failed/cancelled reconnect must not erase evidence about the retained generation.
    control.resume()
    assert restarted.status(DEFAULT)["state"] == "sign_in_needed"
    barrier = control.suspend()
    control.finish(barrier)
    control.resume()
    assert restarted.status(DEFAULT)["state"] == "sign_in_needed"
    hint = control.path.with_suffix(".health.json").read_bytes()
    with pytest.raises(HktvError):
        control.note_signed_out(original_epoch, 2)
    assert control.path.with_suffix(".health.json").read_bytes() == hint
    # Verified publication of a newer generation makes the old fact inapplicable.
    atomic_write_json(path, session_payload(2, expected, "Synthetic Browser", cookies("new"), time.time()))
    assert Connections(build_accounts(settings), auth=auth, client=Login()).status(DEFAULT)["state"] == "connected"


def test_signed_out_hint_cannot_be_lost_between_hint_and_control_atomic_renames(configured, monkeypatch):
    import hktvmall_mcp.connections_state as module

    accounts, _, _, _ = configured
    control = accounts.control(DEFAULT)
    old = control.require_active()
    control.note_signed_out(old, 1)
    write = module.atomic_write_json

    def fail_control_commit(path, data):
        if path == control.path:
            raise OSError("synthetic interrupted control commit")
        write(path, data)

    monkeypatch.setattr(module, "atomic_write_json", fail_control_commit)
    with pytest.raises(OSError):
        control.resume()
    assert control.read()["epoch"] == old
    assert control.health(1) == "sign_in_needed"


@pytest.mark.parametrize("resume_during_warm", [False, True])
def test_maintenance_cannot_publish_after_disconnect_or_disconnect_resume(configured, tmp_path, resume_during_warm):
    from signin_window.sessions import AttemptBudget

    from hktvmall_maintenance.keeper import IdentityKeeper
    from hktvmall_maintenance.models import PersistenceError
    from hktvmall_maintenance.outcomes import IdentityOutcome
    from hktvmall_maintenance.state import TargetStateStore

    accounts, _, _, _ = configured
    control = accounts.control(DEFAULT)
    now = datetime.now(UTC)
    writes = []

    class Browser:
        def warm(self):
            epoch = control.suspend()
            control.finish(epoch)
            if resume_during_warm:
                control.resume()
            return "default@example.com"

    keeper = IdentityKeeper(
        target="hktvmall",
        expected_email="default@example.com",
        export_dir=tmp_path / "exports",
        state=TargetStateStore(tmp_path / "maintenance", utc_now=lambda: now),
        budget=AttemptBudget(tmp_path / "budget", utc_now=lambda: now, error_type=PersistenceError),
        readiness=lambda: pytest.fail("no credential readiness during warm"),
        open_browser=lambda: nullcontext(Browser()),
        lease=nullcontext,
        utc_now=lambda: now,
        snapshot_writer=lambda *args: writes.append(args),
        snapshot_generation=lambda path: 0,
        connection_control=control,
    )
    result = keeper.run(allow_credential_mint=False)
    assert result.outcome == IdentityOutcome.DISCONNECTED
    assert result.exit_code == 0 and result.generation is None
    assert writes == []
    if not resume_during_warm:
        assert keeper.run(allow_credential_mint=True).outcome == IdentityOutcome.DISCONNECTED


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


def test_real_http_mcp_requires_bearer_and_exact_mapped_person(configured):
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
    from starlette.middleware import Middleware

    from hktvmall_mcp import server
    from hktvmall_mcp.connections import BrowserRoutes

    accounts, account, auth, _ = configured
    verifier = StaticTokenVerifier(tokens={"backend-only": {"client_id": BEARER_CLIENT, "scopes": []}})
    mcp = server.create_server(accounts, auth=verifier, browser_auth=auth, login=Login())
    app = mcp.http_app(**server.HTTP_OPTIONS, middleware=[Middleware(BrowserRoutes, connections=mcp.connections)])
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "hktv_status", "arguments": {}}}
    headers = {"Accept": "application/json, text/event-stream", "Mcp-Protocol-Version": "2025-03-26"}
    with TestClient(app) as browser:
        assert browser.post("/mcp", json=body, headers={**headers, "X-Fleet-Person": "first"}).status_code == 401
        for values in ([], ["unknown"], ["first", "second"]):
            response = browser.post(
                "/mcp",
                json=body,
                headers=[
                    *headers.items(),
                    ("Authorization", "Bearer backend-only"),
                    *[("X-Fleet-Person", value) for value in values],
                ],
            )
            result = response.json()["result"]
            assert result["isError"] and "not_connected" in result["content"][0]["text"]
        for person, account_label in (("first", DEFAULT), ("second", "s•••@example.com")):
            response = browser.post(
                "/mcp", json=body, headers={**headers, "Authorization": "Bearer backend-only", "X-Fleet-Person": person}
            )
            result = response.json()["result"]
            assert not result.get("isError")
            payload = result.get("structuredContent") or json.loads(result["content"][0]["text"])
            assert payload["account"] == account_label
        # MCP status and rejected retired entry routes allocate no human attempt.
        assert browser.get("/connect?req=retired").status_code == 404
        assert not mcp.connections.relay.links(account)
        assert not mcp.connections.relay.links(DEFAULT)
        assert mcp.connections.client.normal.calls == []
        assert accounts.control(DEFAULT).read()["owner"] is None


def test_production_http_cannot_restore_the_local_oauth_issuer(configured, monkeypatch):
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


@pytest.mark.parametrize("raw", ["{}", "malformed", "[]", '{"version":true}'])
def test_corrupt_control_state_never_reactivates_cached_account_access(configured, raw):
    accounts, account, _, _ = configured
    cached = accounts.get(account)
    control = accounts.control(account)
    control.path.parent.mkdir(parents=True, exist_ok=True)
    control.path.write_text(raw)
    with pytest.raises(HktvError, match="connection_disconnected"):
        accounts.get(account)
    with pytest.raises(HktvError, match="connection_disconnected"):
        cached.session.checkout_client()


def test_stale_owner_forms_and_cancel_cannot_mutate_new_attempt_or_credentials(configured):
    accounts, account, auth, portal = configured
    control = accounts.control(account)
    initial = control.read()
    stale = {"epoch": initial["epoch"], "state": initial["state"]}
    blob = accounts.directory / account / "session.bin"
    before = blob.read_bytes()
    with client(portal) as browser:
        first = browser.post("/connections/start", headers=cookie(auth), data=stale)
        assert first.status_code == 303
        old_epoch = control.read()["epoch"]
        assert browser.post("/connections/start", headers=cookie(auth)).status_code == 303
        assert control.read()["epoch"] != old_epoch
        current = control.read()
        for action in ("start", "disconnect"):
            rejected = browser.post("/connections/" + action, headers=cookie(auth), data=stale)
            assert rejected.status_code == 409 and 'href="/connections/"' in rejected.text
            assert browser.get("/connections/", headers=cookie(auth)).status_code == 200
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
    before = accounts.control(account).read()
    with client(portal) as browser:
        for action in ("start", "disconnect"):
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
        unmapped = browser.get("/connections/", headers=cookie(auth, "nobody"), follow_redirects=False)
        assert unmapped.status_code == 200 and "cannot manage an HKTVmall connection" in unmapped.text
        made = browser.post("/connections/start", headers=cookie(auth), follow_redirects=False)
        attempt = browser.get(urlsplit(made.headers["location"]).path, headers=cookie(auth))
        assert attempt.status_code == 200 and '"autoStart": true' in attempt.text

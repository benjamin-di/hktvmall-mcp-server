"""Host build forwarding with synthetic local pages, on old and identity-capable renderers."""

import dataclasses
import time
from functools import wraps
from types import SimpleNamespace

import pytest
from signin_window import connections as renderer
from signin_window.service import Window
from starlette.testclient import TestClient
from test_accounts import accounts as accounts  # noqa: F401
from test_accounts import keyed as keyed  # noqa: F401
from test_connections import PUBLIC, Login, client, cookie
from test_connections import configured as configured  # noqa: F401

from hktvmall_login import app as login_app
from hktvmall_maintenance import human
from hktvmall_mcp import connections, ui
from hktvmall_mcp.build_identity import BuildIdentity

IDENTITY = BuildIdentity("hktvmall-mcp", "9.8.7", "1234567890abcdef" * 2 + "12345678")
TOKEN = "x" * 40


@pytest.fixture(autouse=True)
def host_build(monkeypatch):
    monkeypatch.setattr(ui, "BUILD", IDENTITY)


def assert_identity(page):
    identity_footers = sum(page.count(f'<footer id="{name}"') for name in ("connection-identity", "page-identity"))
    if hasattr(renderer, "PageIdentity"):
        assert identity_footers == 1
        assert f'data-service="{IDENTITY.service}"' in page
        assert f'data-version="{IDENTITY.version}"' in page
        assert f'data-source-revision="{IDENTITY.source_revision}"' in page
        assert f"{IDENTITY.service} v{IDENTITY.version} · {IDENTITY.source_revision[:12]}<" in page
    else:
        assert identity_footers == 0
        assert IDENTITY.source_revision not in page


def test_missing_identity_type_and_old_factory_signature_are_noops(monkeypatch):
    def old_factory(title):
        return title

    def new_factory(*, identity=None):
        return identity

    monkeypatch.delattr(renderer, "PageIdentity", raising=False)
    assert ui.renderer_options(new_factory) == {}
    monkeypatch.setattr(renderer, "PageIdentity", BuildIdentity, raising=False)
    assert ui.renderer_options(old_factory) == {}
    assert ui.renderer_options(new_factory) == {"identity": IDENTITY}


def test_configured_auth_home_refusals_and_relay_pages_use_host_build(configured):
    accounts, _, _, _ = configured
    auth, claim = connections.configured_auth(
        accounts.settings,
        {
            "HKTVMALL_CONNECTIONS_ISSUER": "https://identity.example.com",
            "HKTVMALL_CONNECTIONS_CLIENT_ID": "synthetic-client",
            "HKTVMALL_CONNECTIONS_GROUP": "example-users",
        },
    )
    portal = connections.Connections(accounts, auth=auth, claim=claim, client=Login())
    with client(portal) as browser:
        home = browser.get("/connections/", headers=cookie(auth))
        assert home.status_code == 200
        assert_identity(home.text)
        refused = browser.post("/connections/start", headers={"Origin": PUBLIC})
        assert refused.status_code == 401
        assert_identity(refused.text)
        stale = browser.post("/connections/start", headers=cookie(auth, fresh=False))
        assert stale.status_code == 403
        assert_identity(stale.text)
        conflict = browser.post("/connections/start", headers=cookie(auth), data={"epoch": "stale", "state": "active"})
        assert conflict.status_code == 409
        assert_identity(conflict.text)
        made = browser.post("/connections/start", headers=cookie(auth))
        assert made.status_code == 303
        attempt = browser.get(made.headers["location"], headers=cookie(auth))
        assert attempt.status_code == 200
        assert_identity(attempt.text)
        expired = browser.get("/connect/" + "a" * 32, headers=cookie(auth))
        assert expired.status_code == 404
        assert_identity(expired.text)
        status = browser.get("/connections/status", headers=cookie(auth))
        assert status.status_code == 200
        assert IDENTITY.source_revision not in status.text
        assert "connection-identity" not in status.text
        signed_out = browser.post("/connections/logout", headers=cookie(auth))
        assert signed_out.status_code == 200
        assert_identity(signed_out.text)


@pytest.mark.parametrize("entrypoint", ["login", "human"])
def test_both_service_factories_forward_host_build_without_exposing_direct_view(entrypoint, monkeypatch):
    import uvicorn

    module = login_app if entrypoint == "login" else human
    factory = module.build_app
    seen = {}

    @wraps(factory)
    def capture(windows, **options):
        seen.update(windows=windows, options=options)
        return factory(windows, **options)

    monkeypatch.setattr(module, "build_app", capture)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: seen.update(app=app))
    monkeypatch.setenv("HKTVMALL_LOGIN_TOKEN", TOKEN)
    monkeypatch.setattr(login_app, "policy_installed", lambda: True)
    if entrypoint == "human":

        async def no_browser(window):
            pytest.fail("no browser should start")

        recovery = SimpleNamespace(recover=lambda: None, run=no_browser)
        monkeypatch.setattr(human, "configured", lambda: recovery)
        human.main([])
    else:
        login_app.serve()
    options = seen["options"]
    assert options["direct_view"] is False
    if hasattr(renderer, "PageIdentity"):
        assert dataclasses.asdict(options["identity"]) == dataclasses.asdict(IDENTITY)
    else:
        assert "identity" not in options
    window = Window("w" * 24, "desktop", 1, time.monotonic(), 60.0)
    window.waiting("example.test")
    monkeypatch.setattr(seen["windows"], "get", lambda key: window if key == window.id else None)
    path = f"/view/{window.id}?t={window.view_token}"
    browser = TestClient(seen["app"])
    assert browser.get(path).status_code == 401
    assert browser.get(path, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 404
    assert browser.get("/health").json() == {"status": "healthy"}
    state = browser.get(f"/logins/{window.id}", headers={"Authorization": f"Bearer {TOKEN}"})
    assert state.status_code == 200
    assert IDENTITY.source_revision not in state.text and "identity" not in state.json()

    # Exercise the real renderer's shell with the captured options; production keeps direct view disabled.
    viewer = TestClient(factory(seen["windows"], **{**options, "direct_view": True}))
    page = viewer.get(path)
    assert page.status_code == 200
    assert_identity(page.text)

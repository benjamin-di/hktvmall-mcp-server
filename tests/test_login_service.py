"""The login service (signin-window's, tested there) as hktvmall-login wires it: its bearer on every route but /health,
the live view behind both the bearer and the window's token, one session handed over once, and the runner showing
HKTVmall's login page on the window's display. No browser, no display."""

import asyncio
import time
from pathlib import Path

import pytest
from signin_window.service import Window, Windows, build_app
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hktvmall_login import app as login_app
from hktvmall_login.window import session_cookies

TOKEN = "x" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SESSION = {"email": "alex@example.com", "user_agent": "UA", "cookies": []}


def client(release: dict) -> TestClient:
    async def browser(window):
        window.waiting("www.hktvmall.com")
        while not release.get(window.id):
            await asyncio.sleep(0.01)
        return SESSION

    windows = Windows(browser, slots=1)
    windows.start_display = lambda window: None  # type: ignore[method-assign]
    windows.stop_display = lambda window: window.procs.clear()  # type: ignore[method-assign]
    return TestClient(build_app(windows, token=TOKEN, novnc_dir=Path("/nonexistent"), direct_view=False))


def poll(http: TestClient, window_id: str, until: str) -> None:
    for _ in range(300):
        if http.get(f"/logins/{window_id}", headers=AUTH).json().get("state") == until:
            return
        time.sleep(0.01)
    raise AssertionError(until)


def test_every_route_but_health_needs_the_bearer_and_the_view_needs_both():
    with client({}) as http:
        assert http.get("/health").status_code == 200
        assert http.post("/logins", json={}).status_code == 401
        started = http.post("/logins", json={"layout": "phone"}, headers=AUTH).json()
        poll(http, started["id"], "waiting")
        assert http.get(f"/view/{started['id']}?t={started['view_token']}").status_code == 401  # no direct view
        with (
            pytest.raises(WebSocketDisconnect),
            http.websocket_connect(f"/logins/{started['id']}/vnc?t={started['view_token']}"),
        ):
            pass  # the token alone opens nothing


def test_one_slot_one_result_then_closed():
    release: dict = {}
    with client(release) as http:
        window_id = http.post("/logins", json={}, headers=AUTH).json()["id"]
        assert http.post("/logins", json={}, headers=AUTH).status_code == 503  # the only slot is taken
        assert http.post(f"/logins/{window_id}/result", headers=AUTH).status_code == 409  # not signed in yet
        release[window_id] = True
        poll(http, window_id, "signed_in")
        result = http.post(f"/logins/{window_id}/result", headers=AUTH)
        assert result.status_code == 200 and result.json()["email"] == "alex@example.com"
        assert http.post(f"/logins/{window_id}/result", headers=AUTH).status_code == 409  # once only
        assert http.post("/logins", json={}, headers=AUTH).status_code == 201  # the slot is free again


def test_the_runner_opens_hktvmalls_page_on_the_windows_display(monkeypatch):
    seen = {}

    async def sign_in(**kwargs):
        seen.update(kwargs)
        kwargs["on_page"]("www.hktvmall.com")
        return SESSION

    monkeypatch.setattr(login_app, "sign_in", sign_in)
    monkeypatch.setattr(login_app, "policy_installed", lambda: True)
    window = Window("w" * 24, "phone", 1, 0.0, 60.0)
    assert asyncio.run(login_app.run_browser(window)) == SESSION
    assert seen["env"]["DISPLAY"] == ":91" and seen["layout"] == "phone" and seen["profile"] in window.profiles
    assert seen["channel"] == "chrome"
    assert "--enable-unsafe-swiftshader" in seen["extra_args"]
    assert seen["on_saving"] == window.saving
    assert window.state == "waiting" and window.host == "www.hktvmall.com"
    assert seen["locked"] is True  # kiosk mode, Ctrl+T/Ctrl+N closed (signin-window's browser)


def test_no_window_opens_without_the_managed_policy(monkeypatch):
    monkeypatch.setattr(login_app, "policy_installed", lambda: False)
    with pytest.raises(login_app.PolicyMissing):
        asyncio.run(login_app.run_browser(Window("w" * 24, "desktop", 1, 0.0, 60.0)))
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile.login").read_text()
    assert "python -m signin_window.browser install-policy" in dockerfile


def test_dockerfile_login_caches_patchright_chromium_before_login_requirements():
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "Dockerfile.login").read_text(encoding="utf-8")

    copy_patchright = dockerfile.index("COPY requirements-patchright.lock /tmp/requirements-patchright.lock")
    install_patchright = dockerfile.index(
        "uv pip install --system --compile-bytecode --require-hashes -r /tmp/requirements-patchright.lock"
    )
    install_chromium = dockerfile.index("patchright install --with-deps chromium")
    copy_login = dockerfile.index("COPY requirements-login.lock /tmp/requirements-login.lock")
    install_login = dockerfile.index(
        "uv pip install --system --compile-bytecode --require-hashes -r /tmp/requirements-login.lock"
    )
    install_policy = dockerfile.index("python -m signin_window.browser install-policy")

    assert copy_patchright < install_patchright < install_chromium < copy_login < install_login < install_policy
    assert "apt-get upgrade" in dockerfile
    assert "CHROME_SHA256" in dockerfile
    assert "NOVNC_SHA256" in dockerfile
    assert "scripts/embed-build-identity.py" in dockerfile
    assert "symlink_to('/usr/local/bin/browser-node')" in dockerfile
    assert "USER 1000:1000" in dockerfile
    assert dockerfile.count("--mount=type=cache,target=/root/.cache/uv") == 3
    assert dockerfile.count("UV_CACHE_DIR=/root/.cache/uv uv pip install") == 3
    assert "--no-shell" not in dockerfile

    server_dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    assert server_dockerfile.count("--mount=type=cache,target=/root/.cache/uv") == 2
    assert server_dockerfile.count("UV_CACHE_DIR=/root/.cache/uv uv pip install") == 2


def test_patchright_lock_aligns_with_login_lock_and_uv_lock():
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("export_patchright_lock", root / "scripts/export-patchright-lock.py")
    assert spec is not None and spec.loader is not None
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)

    patchright_lock = (root / "requirements-patchright.lock").read_text(encoding="utf-8")
    login_lock = (root / "requirements-login.lock").read_text(encoding="utf-8")

    expected = exporter.render_patchright_lock(root / "uv.lock", root / "requirements-login.lock")
    assert patchright_lock == expected

    patchright_blocks = exporter.parse_requirements_blocks(patchright_lock)
    login_blocks = exporter.parse_requirements_blocks(login_lock)
    assert "patchright" in patchright_blocks
    assert "signin-window" not in patchright_blocks
    for pkg, lines in patchright_blocks.items():
        assert pkg in login_blocks
        assert lines == login_blocks[pkg]
        assert any("--hash=sha256:" in line for line in lines), f"{pkg} missing hashes"


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

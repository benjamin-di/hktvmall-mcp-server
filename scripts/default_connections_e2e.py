"""Real Pocket ID passkeys and noVNC recovery of a synthetic shared default account.

Requires POCKET_ID_BIN, Chromium, Xvfb, x11vnc, noVNC and the installed browser
policy. No request reaches HKTVmall and no personal account or credential is used.
Run against the installed application wheel, without its source on PYTHONPATH.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import secrets
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from patchright.async_api import async_playwright
from pocket_fixture import PocketFixture, port, serve, wait_for
from signin_window.browser import LAUNCH_ARGS, browser_environment, harden
from signin_window.oidc import BrowserAuth, OIDCConfig
from signin_window.service import Windows, build_app
from starlette.middleware import Middleware

from hktvmall_maintenance.cli import lease, stores
from hktvmall_maintenance.human import HumanRecovery, VerifiedSession, start_params
from hktvmall_maintenance.human_state import profile_path
from hktvmall_maintenance.keeper import IdentityKeeper
from hktvmall_maintenance.outcomes import HoldReason, IdentityOutcome
from hktvmall_maintenance.snapshot import publish
from hktvmall_mcp.auth import BEARER_CLIENT, require_bearer
from hktvmall_mcp.config import Settings
from hktvmall_mcp.connections import BrowserRoutes
from hktvmall_mcp.connections_state import ConnectionState
from hktvmall_mcp.errors import HktvError
from hktvmall_mcp.server import HTTP_OPTIONS, build_accounts, create_server

EMAIL = "owner@example.com"
TOKEN = secrets.token_urlsafe(32)
FIELD = """<!doctype html><title>Synthetic provider</title>
<form onsubmit="event.preventDefault();window.confirmed=true">
<input id=email autofocus style="position:fixed;inset:0 0 120px;width:100%;font-size:30px">
<button style="position:fixed;bottom:0;height:100px;width:100%">Confirm synthetic account</button></form>"""


async def main():
    servers, contexts = [], []
    with tempfile.TemporaryDirectory(prefix="hktvmall-default-native-") as temp:
        root = Path(temp)
        native, export, controls, reader = [root / name for name in ("maintenance", "export", "controls", "reader")]
        for folder in (native, export, controls, reader):
            folder.mkdir(mode=0o700)
        profile_path(native).mkdir(mode=0o700, parents=True)
        (profile_path(native) / "accepted-marker").write_text("accepted")
        state, budget = stores(native)
        assert budget.reserve("hktvmall", limit=1)
        state.record_hold("hktvmall", HoldReason.SIGN_IN_INTERRUPTED)
        initial = {
            "email": EMAIL,
            "user_agent": "Synthetic Browser",
            "cookies": [
                {
                    "name": "JSESSIONID",
                    "value": "accepted-synthetic",
                    "domain": "www.hktvmall.com",
                    "path": "/",
                    "secure": True,
                },
            ],
        }
        publish(export, VerifiedSession(initial), 1, EMAIL, datetime.now(UTC))
        old_snapshot = (export / "snapshot.json").read_bytes()
        ledger = {p: p.read_bytes() for p in (native / "session-keeper").rglob("*.json")}
        human_port, ui_port = port(), port()
        origin = f"http://127.0.0.1:{ui_port}"
        fixture = PocketFixture(root / "identity", origin)
        remote = {}
        async with fixture, async_playwright() as pw:
            try:

                async def synthetic_provider(**kwargs):
                    # A local HTML stand-in, with native persistent Chromium and noVNC input.
                    runtime = await async_playwright().start()
                    context = await runtime.chromium.launch_persistent_context(
                        kwargs["profile"],
                        executable_path=os.environ.get("CHROME_PATH"),
                        headless=False,
                        no_viewport=True,
                        env=browser_environment(kwargs["env"]),
                        args=[*LAUNCH_ARGS, "--window-position=0,0", "--window-size=1280,860"],
                    )
                    try:
                        await context.route("**/*", lambda route: route.abort())
                        await harden(context)
                        page = context.pages[0]
                        await page.set_content(FIELD)
                        await page.focus("#email")
                        remote["page"] = page
                        kwargs["on_page"]("www.hktvmall.com")
                        while not await page.evaluate("window.confirmed === true", isolated_context=False):
                            await asyncio.sleep(0.05)
                        email = (await page.input_value("#email")).strip().casefold()
                        remote["confirmed"] = email
                        await context.add_cookies(
                            [
                                {
                                    "name": "JSESSIONID",
                                    "value": "candidate-synthetic",
                                    "domain": "www.hktvmall.com",
                                    "path": "/",
                                    "secure": True,
                                }
                            ]
                        )
                        result = {
                            "email": email,
                            "user_agent": await page.evaluate("navigator.userAgent"),
                            "cookies": await context.cookies(),
                        }
                        await asyncio.sleep(0.5)  # Close the owner page before the kept save completes.
                        Path(kwargs["profile"], "human-proof").write_text("verified synthetic candidate")
                        return result
                    finally:
                        remote.pop("page", None)
                        await context.close()
                        await runtime.stop()

                control = ConnectionState(controls, "default")
                recovery = HumanRecovery(native, export, EMAIL, control, login=synthetic_provider)
                recovery.recover()
                windows = Windows(
                    recovery.run,
                    slots=1,
                    minutes=5,
                    xvfb=os.environ.get("HKTVMALL_LOGIN_XVFB", "Xvfb"),
                    x11vnc=os.environ.get("HKTVMALL_LOGIN_X11VNC", "x11vnc"),
                    display_base=1000 + secrets.randbelow(10000),
                    vnc_base=port(),
                )
                servers.append(
                    serve(
                        build_app(
                            windows,
                            token=TOKEN,
                            novnc_dir=Path(os.environ["NOVNC_DIR"]),
                            direct_view=False,
                            start_params=start_params,
                        ),
                        human_port,
                    )
                )
                # Both aliases use the existing default; only the first is its designated manager.
                for name, value in (
                    ("accounts", {"first": "default", "second": "default"}),
                    ("managers", {"default": "first"}),
                ):
                    path = reader / (name + ".json")
                    path.write_text(json.dumps(value))
                    path.chmod(0o600)
                settings = Settings.from_env(
                    {
                        "HKTVMALL_STATE_DIR": str(reader),
                        "HKTVMALL_SESSION_SNAPSHOT": str(export / "snapshot.json"),
                        "HKTVMALL_EXPECTED_EMAIL": EMAIL,
                        "HKTVMALL_SESSION_KEY": base64.b64encode(secrets.token_bytes(32)).decode(),
                        "HKTVMALL_LOGIN_URL": f"http://127.0.0.1:{human_port}",
                        "HKTVMALL_DEFAULT_LOGIN_URL": f"http://127.0.0.1:{human_port}",
                        "HKTVMALL_LOGIN_TOKEN": TOKEN,
                        "HKTVMALL_PUBLIC_URL": origin,
                        "HKTVMALL_TRUSTED_ACCOUNT_HEADER": "x-fleet-person",
                        "HKTVMALL_CONNECTIONS_ACCOUNTS_FILE": str(reader / "accounts.json"),
                        "HKTVMALL_CONNECTIONS_MANAGERS_FILE": str(reader / "managers.json"),
                        "HKTVMALL_CONNECTIONS_CONTROLS_DIR": str(controls),
                    }
                )
                accounts = build_accounts(settings)
                auth = BrowserAuth(
                    OIDCConfig(
                        fixture.issuer,
                        "hktvmall-ui",
                        origin,
                        reader / "browser",
                        "__Host-hktvmall-native",
                        required_group="hktvmall-users",
                        allow_loopback_http=True,
                    )
                )
                server = create_server(accounts, auth=require_bearer({"MCP_BEARER_TOKEN": TOKEN}), browser_auth=auth)
                app = server.http_app(
                    **HTTP_OPTIONS, middleware=[Middleware(BrowserRoutes, connections=server.connections)]
                )
                servers.append(serve(app, ui_port))
                await wait_for(lambda: all(s.started for s, _ in servers))

                def forbidden():
                    raise AssertionError("upkeep opened a browser during human recovery")

                upkeep = IdentityKeeper(
                    target="hktvmall",
                    expected_email=EMAIL,
                    export_dir=export,
                    state=state,
                    budget=budget,
                    readiness=forbidden,
                    open_browser=forbidden,
                    lease=lambda: lease(native),
                    utc_now=lambda: datetime.now(UTC),
                    connection_control=control,
                )
                initial_upkeep = await asyncio.to_thread(upkeep.run, allow_credential_mint=True)
                assert initial_upkeep.outcome == IdentityOutcome.HELD
                assert control.health(1) == "check_needed"
                viewer = await pw.chromium.launch(executable_path=os.environ.get("CHROME_PATH"), headless=True)
                first, page = await fixture.login(viewer, fixture.users[0])
                contexts.append(first)
                assert await page.get_by_role("status").inner_text() == "Connection needs attention"
                second, other_page = await fixture.login(viewer, fixture.users[1])
                contexts.append(second)
                assert (await second.request.get(origin + "/connections/status")).status == 401
                assert (
                    await second.request.post(origin + "/connections/start", headers={"Origin": origin})
                ).status == 403
                assert accounts.resolve(BEARER_CLIENT, person="first") is accounts.default
                assert accounts.resolve(BEARER_CLIENT, person="second") is accounts.default
                print("PASS: real Pocket ID passkeys, shared use mapping and designated-manager isolation", flush=True)

                async def open_window():
                    nonlocal page
                    await page.goto(origin + "/connections/")
                    await page.get_by_role("button", name=re.compile(r"^(Connect|Reconnect)$")).click()
                    await page.wait_for_url(origin + "/connect/*")
                    link = page.url
                    assert (await second.request.get(link)).status == 404
                    assert (await second.request.get(link + "/novnc/vnc.html")).status == 404
                    existing_windows = set(windows._windows)
                    await page.get_by_role("button", name="Continue to HKTVmall's sign-in", exact=True).click()
                    await wait_for(
                        lambda: (
                            remote.get("page") is not None
                            or any(
                                w.state in ("failed", "busy", "cancelled")
                                for key, w in windows._windows.items()
                                if key not in existing_windows
                            )
                        ),
                        seconds=60,
                    )
                    assert remote.get("page") is not None, [(w.state, w.error) for w in windows._windows.values()]
                    frame_element = await page.wait_for_selector("iframe", timeout=60000)
                    frame = await frame_element.content_frame()
                    await frame.wait_for_function(
                        "document.documentElement.classList.contains('noVNC_connected')", timeout=60000
                    )
                    await wait_for(lambda: remote.get("page") is not None)
                    return link, frame

                async def submit(frame, email):
                    remote.pop("confirmed", None)
                    canvas = await frame.wait_for_selector("#noVNC_container canvas")
                    await canvas.click(position={"x": 200, "y": 200})
                    await page.keyboard.type(email, delay=20)
                    await page.keyboard.press("Tab")
                    await page.keyboard.press("Enter")
                    # noVNC forwards keyboard events asynchronously. Observe provider
                    # receipt before closing its viewer so that close tests the save.
                    await wait_for(
                        lambda: remote.get("confirmed") == email,
                    )

                _, frame = await open_window()

                # Actual upkeep must respect the existing lease while the human owns its window.
                result = await asyncio.to_thread(upkeep.run, allow_credential_mint=True)
                assert result.outcome == IdentityOutcome.SKIPPED_LEASE_BUSY
                await page.close()
                page = await first.new_page()
                await page.goto(origin + "/connections/")
                await page.get_by_role("link", name="Continue sign-in", exact=True).click()
                await page.get_by_role("button", name="Cancel", exact=True).click()
                await wait_for(
                    lambda: not any(w.state in ("starting", "waiting", "saving") for w in windows._windows.values())
                )
                assert (export / "snapshot.json").read_bytes() == old_snapshot
                assert not (profile_path(native) / "human-proof").exists()
                print(
                    "PASS: real noVNC window, native upkeep exclusion, page-close recovery and cancellation", flush=True
                )
                _, frame = await open_window()
                await submit(frame, "other@example.com")
                await wait_for(lambda: any(w.state == "wrong_account" for w in windows._windows.values()))
                await page.get_by_role("heading", name="Different account", exact=True).wait_for()
                assert (export / "snapshot.json").read_bytes() == old_snapshot
                assert state.load("hktvmall").hold is not None
                assert not (profile_path(native) / "human-proof").exists()
                _, frame = await open_window()
                await submit(frame, EMAIL)
                await page.close()
                await wait_for(lambda: json.loads((export / "snapshot.json").read_bytes())["generation"] == 2)
                await wait_for(
                    lambda: all(w.state not in ("starting", "waiting", "saving") for w in windows._windows.values())
                )
                assert (profile_path(native) / "human-proof").is_file()
                assert state.load("hktvmall").hold is None
                assert all(p.read_bytes() == raw for p, raw in ledger.items())
                assert not any(w.result is not None for w in windows._windows.values())
                print(
                    "PASS: same-account profile/snapshot commit after page closure; "
                    "no credential handover or attempt refund",
                    flush=True,
                )
                recovery.recover()
                page = await first.new_page()
                await page.goto(origin + "/connections/")
                await page.get_by_role("button", name="Disconnect HKTVmall", exact=True).click()
                assert control.read()["state"] == "disconnected"
                for alias in ("first", "second"):
                    try:
                        accounts.resolve(BEARER_CLIENT, person=alias)
                    except HktvError:
                        pass
                    else:
                        raise AssertionError("disconnect did not stop shared account access")
                assert (profile_path(native) / "human-proof").exists()
                print("PASS: durable shared-default disconnect retains managed data and blocks both users", flush=True)
                await viewer.close()
            finally:
                for context in contexts:
                    await context.close()
                for server, _ in servers:
                    server.should_exit = True
                for _, thread in servers:
                    thread.join(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

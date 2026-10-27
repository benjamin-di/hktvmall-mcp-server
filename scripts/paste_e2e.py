"""End-to-end check of the connect page and the paste add-on, with real browsers and no HKTVmall.

Runs the login service (``hktvmall_login``) with a stand-in for HKTVmall's login page (a local page with one text
field, in a headed Chromium on the login's virtual display), and the server (``hktvmall_mcp``) with its connect pages.
Then the owner authenticates through signed synthetic OIDC, starts the connection and opens its live view in a
headless Chromium. A second owner is denied access. The owner puts text on the clipboard, presses Ctrl+V and checks it
arrived in the remote field, typed. Clipboard *reading* is not granted to the page, so the text can only have come
from the browser's own paste event. Closing the page retains a resumable window; explicit cancellation closes it.

Needs Xvfb, x11vnc, patchright's Chromium and noVNC's files (the login image has them all):

    python scripts/paste_e2e.py                     # in the login image: Xvfb, x11vnc, /opt/novnc
    HKTVMALL_LOGIN_XVFB=... HKTVMALL_LOGIN_NOVNC=... python scripts/paste_e2e.py

Exits 0 when everything worked. Nothing leaves the machine.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import socket
import sys
import tempfile
from pathlib import Path

import uvicorn
from patchright.async_api import async_playwright
from signin_window.oidc import BrowserAuth, OIDCConfig
from signin_window.service import Window, Windows, build_app
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import RedirectResponse

from hktvmall_mcp.auth import require_bearer
from hktvmall_mcp.config import Settings
from hktvmall_mcp.connections import BrowserRoutes
from hktvmall_mcp.server import HTTP_OPTIONS, build_accounts, create_server

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from oidc_provider import Provider  # noqa: E402

# Typed without the trailing newline. ASCII only: the virtual display's keyboard has no keys for other characters
# (x11vnc drops their keysyms), as for anyone typing them in the window.
TEXT = "Hello, HKTVmall 123 @#%&*()[]{}<>?/|~ pass:Word_-+=.\n"
EXPECTED = TEXT.rstrip("\n") + "x"  # and a normal key afterwards
# The field covers the whole window, so the click that focuses the viewer also focuses it.
FIELD_PAGE = (
    "<!doctype html><title>stand-in</title>"
    "<input id=f autofocus style='position:fixed;inset:0;width:100%;height:100%;font-size:24px'>"
)
BEARER, LOGIN_TOKEN = "e2e-bearer-0123456789abcdef", "e2e-login-token-0123456789abcdef0123"
WINDOWS: Windows | None = None
SERVERS = []


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def serve(app, port: int) -> uvicorn.Server:  # type: ignore[no-untyped-def]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on", access_log=False)
    )
    SERVERS.append((server, asyncio.get_running_loop().create_task(server.serve())))
    for _ in range(100):
        if server.started:
            return server
        await asyncio.sleep(0.05)
    raise SystemExit("a server did not start")


async def main() -> int:
    global WINDOWS
    state = Path(tempfile.mkdtemp(prefix="hktvmall-e2e-"))
    login_port, mcp_port = free_port(), free_port()
    remote: dict = {}

    async with async_playwright() as pw:

        async def stand_in(window: Window) -> dict:
            """Instead of HKTVmall's page: one text field, in a headed Chromium filling the window's display (with no
            window manager, keys go to the window under the pointer)."""
            width, height = window.size
            browser = await pw.chromium.launch(
                executable_path=os.environ.get("CHROME_PATH"),
                headless=False,
                env=window.env(),
                args=["--window-position=0,0", f"--window-size={width},{height}"],
            )
            try:
                page = await browser.new_page(no_viewport=True)
                await page.set_content(FIELD_PAGE)
                await page.focus("#f")
                remote["page"] = page
                remote["finish"] = asyncio.Event()
                window.waiting("www.hktvmall.com")
                await remote["finish"].wait()
                return {
                    "email": "second@example.com",
                    "user_agent": "Synthetic Browser",
                    "cookies": [
                        {
                            "name": "JSESSIONID",
                            "value": "synthetic-session",
                            "domain": "www.hktvmall.com",
                            "path": "/",
                            "secure": True,
                            "expires": None,
                        }
                    ],
                }
            finally:
                remote.pop("page", None)
                await browser.close()

        WINDOWS = windows = Windows(
            stand_in,
            slots=1,
            minutes=5,
            xvfb=os.environ.get("HKTVMALL_LOGIN_XVFB") or "Xvfb",
            x11vnc=os.environ.get("HKTVMALL_LOGIN_X11VNC") or "x11vnc",
        )
        novnc = Path(os.environ.get("HKTVMALL_LOGIN_NOVNC") or "/opt/novnc")
        await serve(build_app(windows, token=LOGIN_TOKEN, novnc_dir=novnc, direct_view=False), login_port)

        base = f"http://127.0.0.1:{mcp_port}"
        settings = Settings.from_env(
            {
                "HKTVMALL_STATE_DIR": str(state),
                "HKTVMALL_SESSION_KEY": base64.b64encode(secrets.token_bytes(32)).decode(),
                "HKTVMALL_LOGIN_URL": f"http://127.0.0.1:{login_port}",
                "HKTVMALL_LOGIN_TOKEN": LOGIN_TOKEN,
                "HKTVMALL_PUBLIC_URL": base,
            }
        )
        bindings = state / "accounts.json"
        bindings.write_text(json.dumps({"second": "acct_aaaaaaaaaaaa"}))
        import dataclasses

        settings = dataclasses.replace(settings, accounts_file=bindings, trusted_account_header="x-fleet-person")
        accounts = build_accounts(settings)
        static = require_bearer({"MCP_BEARER_TOKEN": BEARER})
        provider = Provider(issuer=base)
        auth = BrowserAuth(
            OIDCConfig(
                base,
                "hktvmall-connections",
                base,
                state / "browser",
                cookie_name="__Host-hktvmall-connections",
                required_group="example-users",
                allow_loopback_http=True,
            ),
            transport=provider.transport(),
        )
        mcp = create_server(accounts, auth=static, browser_auth=auth)
        app = mcp.http_app(**HTTP_OPTIONS, middleware=[Middleware(BrowserRoutes, connections=mcp.connections)])

        async def with_test_issuer(scope, receive, send):
            if scope["type"] == "http" and scope["path"] == "/authorize":
                request = Request(scope)
                response = RedirectResponse(
                    provider.authorize(
                        str(request.url), subject=request.cookies.get("synthetic-subject", "subject-two")
                    ),
                    status_code=302,
                )
                return await response(scope, receive, send)
            return await app(scope, receive, send)

        await serve(with_test_issuer, mcp_port)
        viewer = await pw.chromium.launch(executable_path=os.environ.get("CHROME_PATH"), headless=True)
        context = await viewer.new_context(viewport={"width": 1280, "height": 800})
        await context.add_cookies([{"name": "synthetic-subject", "value": "subject-two", "url": base}])
        await context.grant_permissions(["clipboard-write"], origin=base)
        page = await context.new_page()
        await page.goto(base + "/connections/login?reauth=1")
        await page.wait_for_url(base + "/connections/")
        await page.get_by_role("button", name="Connect", exact=True).click()
        await page.wait_for_url(base + "/connect/*")
        await page.get_by_role("button", name="Continue to HKTVmall's sign-in", exact=True).click()
        frame_element = await page.wait_for_selector("iframe", timeout=60_000)
        frame = await frame_element.content_frame()
        assert frame is not None
        await frame.wait_for_function("document.documentElement.classList.contains('noVNC_connected')", timeout=60_000)
        print("live view connected through the connect page")
        other = await viewer.new_context()
        await other.add_cookies([{"name": "synthetic-subject", "value": "subject-one", "url": base}])
        wrong = await other.new_page()
        await wrong.goto(base + "/connections/login?reauth=1")
        await wrong.wait_for_url(base + "/connections/")
        assert (await other.request.get(page.url + "/state")).status == 404
        assert (await other.request.get(page.url + "/novnc/vnc.html")).status == 404
        assert (await other.request.post(page.url + "/start", headers={"Origin": base})).status == 404
        print("signed OIDC and wrong-owner denial protect the real viewer")
        canvas = await frame.wait_for_selector("#noVNC_container canvas")
        await canvas.click()  # focuses the viewer, and the remote field under the pointer
        await remote["page"].focus("#f")
        await page.keyboard.press("y")  # plain typing reaches the field before the paste is tried
        for _ in range(50):
            if await remote["page"].input_value("#f") == "y":
                break
            await asyncio.sleep(0.1)
        await page.keyboard.press("Backspace")
        await page.evaluate("text => navigator.clipboard.writeText(text)", TEXT)
        await page.keyboard.press("Control+V")
        await asyncio.sleep(0.3 + 0.03 * len(TEXT))
        await page.keyboard.press("x")
        value = ""
        for _ in range(50):
            value = await remote["page"].input_value("#f")
            if value == EXPECTED:
                break
            await asyncio.sleep(0.1)
        if value != EXPECTED:
            print(f"FAIL: the remote field holds {value!r}, expected {EXPECTED!r}", file=sys.stderr)
            return 1
        print("Ctrl+V typed the clipboard into the remote page (paste event; clipboard reading not granted)")

        await page.close(run_before_unload=True)
        assert open_windows(windows), "closing the page must retain a resumable window"
        page = await context.new_page()
        await page.goto(base + "/connections/")
        await page.get_by_role("link", name="Continue sign-in", exact=True).click()
        await page.wait_for_selector("iframe")
        await page.get_by_role("button", name="Cancel", exact=True).click()
        for _ in range(300):  # closing ends Chromium, x11vnc and Xvfb, which can take a few seconds
            if not open_windows(windows):
                break
            await asyncio.sleep(0.1)
        if open_windows(windows):
            print("FAIL: explicit cancellation did not close the login window", file=sys.stderr)
            for w in list(windows._windows.values()):  # did the cancel arrive (cancelled), or did the window not end
                print(f"  window {w.id[:6]}: state={w.state} cancelled={w.cancelled.is_set()} ended={w.ended_at} "
                      f"error={w.error}", file=sys.stderr)  # fmt: skip
            return 1
        print("page closure retained the window; explicit cancellation closed it")
        await page.goto(base + "/connections/")
        await page.get_by_role("button", name="Connect", exact=True).click()
        await page.wait_for_url(base + "/connect/*")
        await page.get_by_role("button", name="Continue to HKTVmall's sign-in", exact=True).click()
        await page.wait_for_selector("iframe")
        for _ in range(100):
            if remote.get("finish") is not None and remote.get("page") is not None:
                break
            await asyncio.sleep(0.05)
        remote["finish"].set()
        await page.close(run_before_unload=True)
        saved = state / "accounts" / "acct_aaaaaaaaaaaa" / "session.bin"
        for _ in range(200):
            if saved.exists():
                break
            await asyncio.sleep(0.05)
        assert saved.exists(), "page closure lost the completed sign-in"
        page = await context.new_page()
        await page.goto(base + "/connections/")
        await page.get_by_text("Connection saved", exact=True).wait_for()
        await page.get_by_role("button", name="Disconnect HKTVmall", exact=True).click()
        await page.get_by_text("Server access stopped", exact=True).wait_for()
        assert not saved.exists()
        restarted = build_accounts(settings)
        assert restarted.control("acct_aaaaaaaaaaaa").read()["state"] == "disconnected"
        assert (
            await context.request.post(base + "/connections/logout", headers={"Origin": base}, max_redirects=0)
        ).status == 303
        assert (await context.request.get(base + "/connections/status")).status == 401
        print("completed session survived page closure; disconnect survived restart and logout ended browser access")
        await viewer.close()
    print("paste e2e: ok")
    return 0


def open_windows(windows: Windows) -> list[Window]:
    return [w for w in list(windows._windows.values()) if w.state in ("starting", "waiting")]


async def run() -> int:
    try:
        return await main()
    finally:
        if WINDOWS is not None:
            await WINDOWS.close_all()  # never leave a display or browser behind
        for server, _ in SERVERS:
            server.should_exit = True
        await asyncio.gather(*(task for _, task in SERVERS))


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))

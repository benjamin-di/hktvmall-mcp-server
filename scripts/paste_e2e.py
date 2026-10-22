"""End-to-end check of the connect page and the paste add-on, with real browsers and no HKTVmall.

Runs the login service (``hktvmall_login``) with a stand-in for HKTVmall's login page (a local page with one text
field, in a headed Chromium on the login's virtual display), and the server (``hktvmall_mcp``) with its connect pages.
Then, as a friend: registers an assistant, starts its sign-in, opens the connect page in a headless Chromium, enters
an invite code, waits for the live view, focuses it, puts text on the clipboard, presses Ctrl+V and checks the text
arrived in the remote field, typed. Clipboard *reading* is not granted to the page, so the text can only have come
from the browser's own paste event. Last, closing the page must close the login window.

Needs Xvfb, x11vnc, patchright's Chromium and noVNC's files (the login image has them all):

    python scripts/paste_e2e.py                     # in the login image: Xvfb, x11vnc, /opt/novnc
    HKTVMALL_LOGIN_XVFB=... HKTVMALL_LOGIN_NOVNC=... python scripts/paste_e2e.py

Exits 0 when everything worked. Nothing leaves the machine.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import socket
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

import uvicorn
from patchright.async_api import async_playwright

from hktvmall_login.app import build_app
from hktvmall_login.logins import Login, Logins
from hktvmall_mcp.auth import require_bearer
from hktvmall_mcp.config import Settings
from hktvmall_mcp.invites import Invites
from hktvmall_mcp.oauth import HktvOAuth
from hktvmall_mcp.server import HTTP_OPTIONS, build_accounts, create_server

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
CALLBACK = "http://127.0.0.1:9/callback"  # an assistant on this computer; never called

LOGINS: Logins | None = None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def serve(app, port: int) -> uvicorn.Server:  # type: ignore[no-untyped-def]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    asyncio.get_running_loop().create_task(server.serve())
    for _ in range(100):
        if server.started:
            return server
        await asyncio.sleep(0.05)
    raise SystemExit("a server did not start")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None


def connect_page(base: str) -> str:
    """Register an assistant and start its sign-in, as Claude does; returns the connect page's address."""
    register = urllib.request.Request(
        f"{base}/register",
        data=json.dumps({"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(register, timeout=30) as response:  # noqa: S310 - the local server above
        client_id = json.loads(response.read())["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "e2e",
            "resource": f"{base}/mcp",
        }
    )
    opener = urllib.request.build_opener(NoRedirect)
    try:
        opener.open(f"{base}/authorize?{query}", timeout=30)
    except urllib.error.HTTPError as redirect:
        location = redirect.headers["Location"]
        assert redirect.code == 302 and location.startswith(f"{base}/connect?req="), location
        return location
    raise SystemExit("authorize did not redirect to the connect page")


async def main() -> int:
    global LOGINS
    state = Path(tempfile.mkdtemp(prefix="hktvmall-e2e-"))
    login_port, mcp_port = free_port(), free_port()
    remote: dict = {}

    async with async_playwright() as pw:

        async def stand_in(login: Login, logins: Logins) -> None:
            """Instead of HKTVmall's page: one text field, in a headed Chromium on the login's display."""
            browser = await pw.chromium.launch(
                headless=False, env={**os.environ, "DISPLAY": login.display}, args=["--window-position=0,0"]
            )
            try:
                page = await browser.new_page(viewport={"width": 1000, "height": 600})
                await page.set_content(FIELD_PAGE)
                await page.focus("#f")
                remote["page"] = page
                login.state, login.host = "waiting", "www.hktvmall.com"
                await asyncio.Event().wait()  # until the login is closed
            finally:
                remote.pop("page", None)
                await browser.close()

        LOGINS = logins = Logins(
            slots=1,
            minutes=5,
            xvfb=os.environ.get("HKTVMALL_LOGIN_XVFB") or "Xvfb",
            x11vnc=os.environ.get("HKTVMALL_LOGIN_X11VNC") or "x11vnc",
            browser=stand_in,
        )
        novnc = Path(os.environ.get("HKTVMALL_LOGIN_NOVNC") or "/opt/novnc")
        await serve(build_app(logins, token=LOGIN_TOKEN, novnc_dir=novnc), login_port)

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
        accounts = build_accounts(settings)
        static = require_bearer({"MCP_BEARER_TOKEN": BEARER})
        oauth = HktvOAuth(public_url=base, state_dir=settings.state_dir, accounts=accounts, static=static)
        app = create_server(accounts, auth=oauth).http_app(**HTTP_OPTIONS)
        await serve(app, mcp_port)
        code = Invites(settings.state_dir).create("e2e")

        url = await asyncio.to_thread(connect_page, base)
        print("connect page made for a new sign-in")

        viewer = await pw.chromium.launch(headless=True)
        context = await viewer.new_context(viewport={"width": 1280, "height": 800})
        await context.grant_permissions(["clipboard-write"], origin=base)
        page = await context.new_page()
        await page.goto(url)
        await page.fill("#code", code)
        await page.click("#inviteForm button")
        frame_element = await page.wait_for_selector("#view iframe", timeout=60_000)
        frame = await frame_element.content_frame()
        assert frame is not None
        await frame.wait_for_function("document.documentElement.classList.contains('noVNC_connected')", timeout=60_000)
        print("live view connected through the connect page")
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

        await page.close(run_before_unload=True)  # pagehide sends the cancel
        for _ in range(300):  # closing ends Chromium, x11vnc and Xvfb, which can take a few seconds
            if not logins._logins:
                break
            await asyncio.sleep(0.1)
        if logins._logins:
            print("FAIL: closing the page did not close the login window", file=sys.stderr)
            return 1
        print("closing the page closed the login window")
        await viewer.close()
    print("paste e2e: ok")
    return 0


async def run() -> int:
    try:
        return await main()
    finally:
        for login_id in list(LOGINS._logins) if LOGINS else []:
            await LOGINS.close(login_id)  # never leave a display or browser behind


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))

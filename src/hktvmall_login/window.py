"""HKTVmall's own login page in a real browser window, until the person has signed in; then the proven session.

Used by the login service (a window on a virtual display, shown through noVNC) and by ``hktvmall-mcp login`` (a
window on the person's own screen). Nothing here clicks, types or fills: the person does every step of the login,
HKTVmall's own checks included (``tests/test_login_safety.py`` pins that). Only HKTVmall's pages (and Facebook's,
for Facebook login) may be shown. The login service's window is also locked down (``locked``, signin-window's
``browser``): kiosk mode with no address bar, no passkey offers, and the image's managed policy (no offer to save the
password).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from signin_window.browser import LAUNCH_ARGS, harden
from signin_window.hosts import top_level

ORIGIN = "https://www.hktvmall.com"
LOGIN_URL = ORIGIN + "/hktv/en/login"
HOME_URL = ORIGIN + "/hktv/en/"
# Pages the window may show; anything else is refused (images, scripts and frames load freely).
DOCUMENT_HOSTS = ("hktvmall.com", "facebook.com")
LAYOUTS = {
    "desktop": {"size": (1280, 860), "user_agent": None},
    # A phone-sized window gets HKTVmall's mobile pages only with a mobile browser identity.
    "phone": {
        "size": (430, 900),
        "user_agent": "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/149.0.0.0 Mobile Safari/537.36",
    },
}
LOGGED_IN = re.compile(r"['\"]loggedin['\"]\s*:\s*(true|false)")
CONTACT = re.compile(r"['\"]realContactEmail['\"]\s*:\s*\"([^\"]*)\"")
WATCH_EVERY = 1.5


def allowed_document(url: str) -> bool:
    if url.startswith(("about:", "data:", "blob:")):
        return True
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == h or host.endswith("." + h) for h in DOCUMENT_HOSTS)


def session_cookies(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """HKTVmall's cookies in the session file shape (hktvmall-mcp validates them again)."""
    out = []
    for c in cookies:
        domain = str(c.get("domain", "")).lower()
        if not (domain.lstrip(".") == "hktvmall.com" or domain.endswith(".hktvmall.com")):
            continue
        expires = c.get("expires")
        out.append(
            {
                "name": c["name"],
                "value": c["value"],
                "domain": domain,
                "path": c.get("path") or "/",
                "secure": bool(c.get("secure")),
                "expires": int(expires) if isinstance(expires, int | float) and expires > 0 else None,
            }
        )
    return out


async def sign_in(
    *,
    profile: str,
    layout: str = "desktop",
    env: Mapping[str, str] | None = None,
    channel: str | None = None,
    extra_args: tuple[str, ...] = (),
    on_page: Callable[[str], None] | None = None,
    locked: bool = False,
) -> dict[str, Any]:
    """Open HKTVmall's login page in a browser using ``profile`` and wait until the person has signed in.

    Returns ``{email, user_agent, cookies}`` once HKTVmall's own home page, read with the same cookies, says signed
    in. ``channel="chrome"`` uses the installed Google Chrome; None uses the browser patchright installed.
    ``on_page`` hears the host of the page shown, every time the window is looked at. ``locked``: a window someone
    else's screen shows (the login service): kiosk mode, passkeys hidden, Ctrl+T/Ctrl+N closed.
    """
    from patchright.async_api import async_playwright

    width, height = LAYOUTS[layout]["size"]
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            profile,
            channel=channel,
            headless=False,
            no_viewport=True,
            locale="en-HK",
            timezone_id="Asia/Hong_Kong",
            user_agent=LAYOUTS[layout]["user_agent"],
            env=dict(env) if env is not None else None,
            args=[
                f"--window-size={width},{height}",
                *extra_args,
                "--no-first-run",
                "--disable-features=Translate",
                *(LAUNCH_ARGS if locked else ()),
            ],
        )
        try:
            if locked:
                await harden(ctx)

            async def guard(route: Any) -> None:
                request = route.request
                if top_level(request) and not allowed_document(request.url):
                    await route.abort()
                    return
                await route.continue_()

            await ctx.route("**/*", guard)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
            while True:
                open_pages = [pg for pg in ctx.pages if not pg.is_closed()]
                if not open_pages:
                    raise WindowClosed()
                if on_page is not None:
                    on_page(urlsplit(open_pages[0].url).hostname or "")
                if await _signed_in(ctx):
                    result = await _prove(ctx)
                    if result is not None:
                        return result
                await asyncio.sleep(WATCH_EVERY)
        finally:
            await ctx.close()


class WindowClosed(Exception):
    """The person closed the login window before signing in."""


async def _signed_in(ctx: Any) -> bool:
    """What the open pages themselves say (the site's own variable, read in the page's main world)."""
    for pg in [pg for pg in ctx.pages if not pg.is_closed()]:
        try:
            flag = await pg.evaluate(
                "() => (window.hktvCommon && hktvCommon.env) ? hktvCommon.env.loggedin === true : false",
                isolated_context=False,
            )
        except Exception:
            continue
        if flag:
            return True
    return False


async def _prove(ctx: Any) -> dict[str, Any] | None:
    """Our own read of the home page with the same cookies: signed in, and whose account."""
    home = await ctx.request.get(HOME_URL)
    html = await home.text()
    flags = set(LOGGED_IN.findall(html))
    contact = CONTACT.search(html)
    if flags != {"true"} or not contact or "@" not in contact.group(1):
        return None
    user_agent = await ctx.pages[0].evaluate("() => navigator.userAgent") if ctx.pages else ""
    return {
        "email": contact.group(1).strip().casefold(),
        "user_agent": user_agent,
        "cookies": session_cookies(await ctx.cookies()),
    }

"""The login service (signin-window's): hktvmall-mcp holds its bearer; a window's live view also needs that window's
token, and there is no direct view page.

Each window runs ``run_browser``: HKTVmall's login page in a fresh Chromium profile on the window's virtual display
(``window.sign_in``), until HKTVmall says signed in and the session is proven; then the session is handed to
hktvmall-mcp once. The API is signin-window's (``signin_window.service.build_app``): ``POST /logins``,
``GET /logins/{id}``, ``POST /logins/{id}/result``, ``DELETE /logins/{id}``, the live view, noVNC's files and
``/health``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from signin_window.browser import policy_installed
from signin_window.service import Window, Windows, build_app

from hktvmall_mcp.logs import diagnostic, setup
from hktvmall_mcp.ui import renderer_options

from .window import sign_in

log = logging.getLogger("hktvmall-login")


class PolicyMissing(Exception):
    """The image's managed Chromium policy is not installed (``python -m signin_window.browser install-policy``)."""


async def run_browser(window: Window) -> dict[str, Any]:
    """HKTVmall's login page in a throwaway, locked-down browser on the window's display; the proven session."""
    if not policy_installed():
        log.error("no sign-in window: the managed Chromium policy is not installed")
        raise PolicyMissing()
    return await sign_in(
        profile=window.temp_profile("hktv-login-"),
        layout=window.layout,
        env=window.env(),
        extra_args=("--window-position=0,0",),
        on_page=window.waiting,
        locked=True,
    )


def main() -> None:
    setup()
    try:
        serve()
    except (ValueError, OSError, OverflowError) as error:
        diagnostic(
            "Check HKTVMALL_LOGIN_SLOTS, HKTVMALL_LOGIN_MINUTES, PORT and browser service configuration "
            f"({type(error).__name__})"
        )
        raise SystemExit(1) from None


def serve() -> None:
    import uvicorn

    token = (os.environ.get("HKTVMALL_LOGIN_TOKEN") or "").strip()
    if len(token) < 32:
        diagnostic("HKTVMALL_LOGIN_TOKEN (at least 32 characters) is required")
        raise SystemExit(1)
    if not policy_installed():
        log.error("the managed Chromium policy is not installed: sign-in windows stay closed")
    windows = Windows(
        run_browser,
        slots=int(os.environ.get("HKTVMALL_LOGIN_SLOTS") or 2),
        minutes=int(os.environ.get("HKTVMALL_LOGIN_MINUTES") or 15),
        xvfb=os.environ.get("HKTVMALL_LOGIN_XVFB") or "Xvfb",
        x11vnc=os.environ.get("HKTVMALL_LOGIN_X11VNC") or "x11vnc",
    )
    novnc = Path(os.environ.get("HKTVMALL_LOGIN_NOVNC") or "/usr/share/novnc")
    app = build_app(windows, token=token, novnc_dir=novnc, direct_view=False, **renderer_options(build_app))
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT") or 8080), log_level="warning", log_config=None)  # noqa: S104

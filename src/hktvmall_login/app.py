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

from signin_window.service import Window, Windows, build_app

from .window import sign_in

log = logging.getLogger("hktvmall-login")


async def run_browser(window: Window) -> dict[str, Any]:
    """HKTVmall's login page in a throwaway profile on the window's display; the proven session."""
    return await sign_in(
        profile=window.temp_profile("hktv-login-"),
        layout=window.layout,
        env=window.env(),
        extra_args=("--window-position=0,0",),
        on_page=window.waiting,
    )


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    token = (os.environ.get("HKTVMALL_LOGIN_TOKEN") or "").strip()
    if len(token) < 32:
        raise SystemExit("HKTVMALL_LOGIN_TOKEN (at least 32 characters) is required")
    windows = Windows(
        run_browser,
        slots=int(os.environ.get("HKTVMALL_LOGIN_SLOTS") or 2),
        minutes=int(os.environ.get("HKTVMALL_LOGIN_MINUTES") or 15),
        xvfb=os.environ.get("HKTVMALL_LOGIN_XVFB") or "Xvfb",
        x11vnc=os.environ.get("HKTVMALL_LOGIN_X11VNC") or "x11vnc",
    )
    novnc = Path(os.environ.get("HKTVMALL_LOGIN_NOVNC") or "/usr/share/novnc")
    app = build_app(windows, token=token, novnc_dir=novnc, direct_view=False)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT") or 8080), log_level="warning")  # noqa: S104

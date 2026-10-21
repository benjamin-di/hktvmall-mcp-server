"""The ``hktvmall-mcp`` command.

hktvmall-mcp                  serve over stdio, for an MCP client on this computer (Claude Desktop, Claude Code,
                              Cursor, ...); the client starts it and talks to it on stdin/stdout
hktvmall-mcp serve            serve over streamable HTTP at http://HOST:PORT/mcp (needs MCP_BEARER_TOKEN)
hktvmall-mcp login            sign in to HKTVmall in a browser window; the session is saved for the server
hktvmall-mcp logout           delete the saved session and the sign-in window's browser profile
hktvmall-mcp status           the saved session and today's budget (asks HKTVmall nothing)
hktvmall-mcp health           exit 0 when the HTTP server's /health answers (container health checks)
hktvmall-mcp admin ...        invites and connected accounts of a server that hosts several people
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import urllib.request

from . import __version__
from .config import ConfigError, Settings


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["admin"]:
        from .admin import main as admin

        return admin(args[1:])
    parser = argparse.ArgumentParser(prog="hktvmall-mcp", description="MCP server for HKTVmall (Hong Kong) baskets.")
    parser.add_argument("--version", action="version", version=f"hktvmall-mcp {__version__}")
    commands = parser.add_subparsers(dest="command")
    serve = commands.add_parser("serve", help="serve over streamable HTTP (needs MCP_BEARER_TOKEN)")
    serve.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8000, help="port to listen on (default 8000)")
    login = commands.add_parser("login", help="sign in to HKTVmall in a browser window")
    login.add_argument(
        "--browser",
        choices=("chrome", "chromium"),
        default="chrome",
        help="your installed Google Chrome (default) or the Chromium that `patchright install chromium` adds",
    )
    commands.add_parser("logout", help="delete the saved session")
    commands.add_parser("status", help="show the saved session and today's budget")
    health = commands.add_parser("health", help="check the HTTP server's /health")
    health.add_argument("--url", default="http://127.0.0.1:8000/health")
    commands.add_parser("admin", help="invites and connected accounts (multi-account servers)")
    options = parser.parse_args(args)

    if options.command == "health":
        return _health(options.url)
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"hktvmall-mcp: {exc}", file=sys.stderr)
        return 2
    if options.command == "login":
        return _login(settings, options.browser)
    if options.command == "logout":
        return _logout(settings)
    if options.command == "status":
        return _status(settings)

    from .auth import AuthConfigError
    from .logs import setup
    from .server import serve_http, serve_stdio

    setup()
    try:
        if options.command == "serve":
            serve_http(settings, host=options.host, port=options.port)
        else:
            serve_stdio(settings)
    except AuthConfigError as exc:
        print(f"hktvmall-mcp: {exc}", file=sys.stderr)
        return 1
    return 0


def _health(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - a local URL the operator passes
            return 0 if response.status == 200 else 1
    except OSError:
        return 1


def _own_session(settings: Settings) -> bool:
    if settings.snapshot_path is not None:
        print(
            "hktvmall-mcp: this server reads its session from HKTVMALL_SESSION_SNAPSHOT; sign in with the program "
            "that publishes it.",
            file=sys.stderr,
        )
        return False
    return True


def _login(settings: Settings, browser: str) -> int:
    if not _own_session(settings):
        return 2
    try:
        from hktvmall_login.window import WindowClosed, sign_in
    except ImportError:
        print(
            "hktvmall-mcp: signing in needs the browser extra: install hktvmall-mcp with its [login] extra, as the\n"
            "README's Quick start shows (in a checkout: uv run --extra login hktvmall-mcp login).",
            file=sys.stderr,
        )
        return 2
    from .session import LocalSession
    from .snapshot import SnapshotError

    profile = settings.state_dir / "browser"
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    print("A browser window opens on HKTVmall's login page. Log in as usual; it closes once you are signed in.")
    try:
        result = asyncio.run(sign_in(profile=str(profile), channel="chrome" if browser == "chrome" else None))
    except WindowClosed:
        print("hktvmall-mcp: the window was closed before signing in; nothing was saved.", file=sys.stderr)
        return 1
    except Exception as exc:  # patchright reports a missing browser as a plain Error
        print(f"hktvmall-mcp: the browser could not start ({type(exc).__name__}).", file=sys.stderr)
        if browser == "chrome":
            print(
                "Install Google Chrome, or use --browser chromium after `patchright install chromium`.", file=sys.stderr
            )
        else:
            print("Run `patchright install chromium` first.", file=sys.stderr)
        return 1
    try:
        generation = LocalSession(settings.session_path).write(result["email"], result["user_agent"], result["cookies"])
    except SnapshotError:
        print("hktvmall-mcp: HKTVmall's answer did not contain a usable session; try again.", file=sys.stderr)
        return 1
    from .accounts import mask_email

    print(f"Signed in as {mask_email(result['email'])}. Saved to {settings.session_path} (generation {generation}).")
    print("A running server picks it up with its next call; no restart is needed.")
    return 0


def _logout(settings: Settings) -> int:
    if not _own_session(settings):
        return 2
    from .session import LocalSession

    removed = LocalSession(settings.session_path).clear()
    profile = settings.state_dir / "browser"
    if profile.exists():
        shutil.rmtree(profile)
        removed = True
    print("Signed out: the saved session and the browser profile are deleted." if removed else "Nothing was saved.")
    return 0


def _status(settings: Settings) -> int:
    from .server import build_accounts

    service = build_accounts(settings).default
    out = service.status(check=False)
    if service.session.expected_email:
        from .accounts import mask_email

        out["email"] = mask_email(service.session.expected_email)
    out["state_dir"] = str(settings.state_dir)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0

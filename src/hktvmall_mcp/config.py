"""Settings from environment variables.

The default account's session comes from one of two places:

* its own session file, ``HKTVMALL_STATE_DIR/session.json``, written by ``hktvmall-mcp login`` (the person signs in
  to HKTVmall in a browser window) and kept up to date by this server; or
* an external session snapshot (``HKTVMALL_SESSION_SNAPSHOT`` with ``HKTVMALL_EXPECTED_EMAIL``), published by
  another program that keeps a signed-in browser session, and only read here (README "Session snapshot").
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

RETIRED = (
    "HKTVMALL_CONNECTIONS_ACCOUNTS_FILE",
    "HKTVMALL_CONNECTIONS_MANAGERS_FILE",
    "HKTVMALL_DEFAULT_LOGIN_URL",
)


class ConfigError(Exception):
    """Bad or missing configuration; raised at startup only."""


def default_state_dir() -> Path:
    """The per-user state folder (``~/.local/state/hktvmall-mcp``, ``~/Library/Application Support/hktvmall-mcp``,
    ``%LOCALAPPDATA%\\hktvmall-mcp``); container images set ``HKTVMALL_STATE_DIR=/state``."""
    from platformdirs import user_state_dir

    return Path(user_state_dir("hktvmall-mcp", appauthor=False))


@dataclass(frozen=True)
class Settings:
    state_dir: Path
    snapshot_path: Path | None = None  # set: the default account reads an external snapshot (read-only)
    expected_email: str | None = None  # the snapshot's account, required with snapshot_path
    daily_budget: int = 2000  # the default account's own
    keepalive_seconds: int = 6 * 3600
    account_daily_budget: int = 500  # each connected person's
    total_daily_budget: int = 4000  # every account together: they share one IP address
    session_key: bytes | None = field(default=None, repr=False)  # connected accounts are off without it
    login_url: str | None = None  # the login service (hktvmall_login); the Connections page is off without it
    login_token: str | None = field(default=None, repr=False)
    public_url: str | None = None  # where people open the Connections page
    trusted_account_header: str | None = None  # set: people mode, the gateway names the verified person
    controls_dir: Path | None = None  # the default account's controls, shared with its custodian

    def __post_init__(self) -> None:
        if (self.snapshot_path is None) != (self.expected_email is None):
            raise ConfigError("HKTVMALL_SESSION_SNAPSHOT and HKTVMALL_EXPECTED_EMAIL go together")
        if self.snapshot_path is not None:
            if not self.snapshot_path.is_absolute() or ".." in self.snapshot_path.parts:
                raise ConfigError("HKTVMALL_SESSION_SNAPSHOT must be an absolute path without '..'")
            if self.snapshot_path.resolve() in {(self.state_dir / n).resolve() for n in ("state.json", "session.json")}:
                raise ConfigError("HKTVMALL_SESSION_SNAPSHOT must be separate from the writable state")
        if self.expected_email is not None and (
            "@" not in self.expected_email or any(c.isspace() for c in self.expected_email)
        ):
            raise ConfigError("HKTVMALL_EXPECTED_EMAIL must be the snapshot account's email")
        if self.login_url and (not self.login_token or len(self.login_token) < 32 or not self.session_key):
            raise ConfigError("HKTVMALL_LOGIN_URL needs HKTVMALL_LOGIN_TOKEN (32+ characters) and HKTVMALL_SESSION_KEY")
        if self.trusted_account_header is not None:
            if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9-]{0,63}", self.trusted_account_header):
                raise ConfigError("HKTVMALL_TRUSTED_ACCOUNT_HEADER must be a header name")
            if self.session_key is None:
                raise ConfigError("People mode (HKTVMALL_TRUSTED_ACCOUNT_HEADER) needs HKTVMALL_SESSION_KEY")
            if self.snapshot_path is not None or self.controls_dir is not None:
                raise ConfigError(
                    "People mode keeps every account in its person's record: unset HKTVMALL_SESSION_SNAPSHOT, "
                    "HKTVMALL_EXPECTED_EMAIL and HKTVMALL_CONNECTIONS_CONTROLS_DIR"
                )
        if self.controls_dir is not None and (
            not self.controls_dir.is_absolute() or ".." in self.controls_dir.parts or not self.controls_dir.is_dir()
        ):
            raise ConfigError("The shared connection-control directory must be mounted before startup")

    @property
    def session_path(self) -> Path:
        """The default account's own session file (used when there is no external snapshot)."""
        return self.state_dir / "session.json"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        retired = sorted(name for name in RETIRED if (env.get(name) or "").strip())
        if retired:
            # Operator-assigned account use is gone: an image that read these would hand one person's account
            # to another without the owner's say (docs/PERMISSIONS.md).
            raise ConfigError(f"Retired setting(s) {', '.join(retired)}: people share their own connections now")
        snapshot = (env.get("HKTVMALL_SESSION_SNAPSHOT") or "").strip()
        email = (env.get("HKTVMALL_EXPECTED_EMAIL") or "").strip()
        state_dir = (env.get("HKTVMALL_STATE_DIR") or "").strip()
        return cls(
            snapshot_path=Path(snapshot) if snapshot else None,
            expected_email=email or None,
            state_dir=Path(state_dir).expanduser() if state_dir else default_state_dir(),
            daily_budget=_int(env, "HKTVMALL_DAILY_BUDGET", 2000, 0, 100_000),
            keepalive_seconds=_int(env, "HKTVMALL_KEEPALIVE_SECONDS", 6 * 3600, 60, 86_400),
            account_daily_budget=_int(env, "HKTVMALL_ACCOUNT_DAILY_BUDGET", 500, 0, 100_000),
            total_daily_budget=_int(env, "HKTVMALL_TOTAL_DAILY_BUDGET", 4000, 0, 1_000_000),
            session_key=_key(env),
            login_url=_internal_url(env),
            login_token=(env.get("HKTVMALL_LOGIN_TOKEN") or "").strip() or None,
            public_url=(env.get("HKTVMALL_PUBLIC_URL") or "").strip().rstrip("/") or None,
            trusted_account_header=(env.get("HKTVMALL_TRUSTED_ACCOUNT_HEADER") or "").strip().lower() or None,
            controls_dir=Path(env["HKTVMALL_CONNECTIONS_CONTROLS_DIR"])
            if env.get("HKTVMALL_CONNECTIONS_CONTROLS_DIR")
            else None,
        )


def _internal_url(env: Mapping[str, str], name="HKTVMALL_LOGIN_URL") -> str | None:
    """The login service runs beside this server: plain http on the private network, host and port only."""
    raw = (env.get(name) or "").strip().rstrip("/")
    if not raw:
        return None
    if not re.fullmatch(r"http://[a-z0-9][a-z0-9.-]{0,62}:[0-9]{2,5}", raw):
        raise ConfigError(f"{name} must name a private HTTP host and port")
    return raw


def _key(env: Mapping[str, str]) -> bytes | None:
    raw = (env.get("HKTVMALL_SESSION_KEY") or "").strip()
    if not raw:
        return None
    from .accounts import parse_key

    try:
        return parse_key(raw)
    except ValueError as exc:
        raise ConfigError(str(exc)) from None


def _int(env: Mapping[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value

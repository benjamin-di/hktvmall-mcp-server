"""An HKTVmall session, held in memory and adopted from a session file (snapshot schema 1).

``SessionStore`` reads an external snapshot that another program publishes, one immutable file per generation,
and never writes it. ``OwnedSession`` is a session this server owns (the default account's ``session.json``
written by ``hktvmall-mcp login``, or a connected person's sealed file): nobody republishes it, so the cookies
HKTVmall replaces during calls are written back. Either way every call stats the file and parses it again only
when it changed; a strictly newer generation replaces the held session, and the sign-in verdict belongs to the
held generation, in memory only. No cookie value reaches logs or status.
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import HktvError
from .fsio import atomic_write_bytes, locked, read_private
from .snapshot import MAX_SNAPSHOT, Snapshot, SnapshotError, parse_snapshot, read_snapshot

log = logging.getLogger(__name__)


def _domain_ok(domain: str) -> bool:
    d = domain.lstrip(".").lower()
    return d == "hktvmall.com" or d.endswith(".hktvmall.com")


def to_jar(cookies: list[dict[str, Any]]) -> http.cookiejar.CookieJar:
    jar = http.cookiejar.CookieJar()
    for c in cookies:
        dot = c["domain"].startswith(".")
        jar.set_cookie(
            http.cookiejar.Cookie(
                0,
                c["name"],
                c["value"],
                None,
                False,
                c["domain"],
                dot,
                dot,
                c["path"],
                True,
                c["secure"],
                c["expires"],
                c["expires"] is None,
                None,
                None,
                {},
            )
        )
    return jar


def from_jar(jar: http.cookiejar.CookieJar) -> list[dict[str, Any]]:
    return [
        {
            "name": c.name,
            "value": c.value or "",
            "domain": c.domain,
            "path": c.path,
            "secure": c.secure,
            "expires": c.expires,
        }
        for c in jar
        if _domain_ok(c.domain)
    ]


@dataclass
class _Held:
    """One adopted generation: unverified until the home page says signed in; dead stays dead."""

    generation: int
    cookies: list[dict[str, Any]]
    user_agent: str
    state: str = "unverified"
    verified_at: float | None = None
    dead_at: float | None = None


class SessionStore:
    missing_code = "session_source_missing"  # what a missing or unreadable file means to the caller

    def __init__(self, snapshot_path: Path, expected_email: str, clock=time.time) -> None:
        self.snapshot_path = Path(snapshot_path)
        self.expected_email = expected_email
        self._clock = clock
        self._lock = threading.Lock()
        self._stat: tuple[int, int, int] | None = None
        self._held: _Held | None = None
        self._source_state = "missing"
        self._source_generation: int | None = None

    def _read(self) -> Snapshot:
        return read_snapshot(self.snapshot_path, self.expected_email, self._clock())

    def _refresh(self) -> str | None:
        """Adopt a newer published generation; return the source's error code, if any."""
        try:
            st = os.stat(self.snapshot_path)
            key = (st.st_ino, st.st_mtime_ns, st.st_size)
        except OSError:
            key = None
        if key is not None and key == self._stat:
            return None
        self._stat, self._source_generation = None, None
        try:
            snapshot = self._read()
        except SnapshotError as exc:
            self._source_state = "missing" if exc.reason in ("missing", "unavailable") else "invalid"
            return self.missing_code if self._source_state == "missing" else "session_source_invalid"
        self._stat, self._source_state, self._source_generation = key, "ready", snapshot.generation
        if self._held is None or snapshot.generation > self._held.generation:
            log.info("session generation %s -> %d", self._held and self._held.generation, snapshot.generation)
            self._held = _Held(snapshot.generation, snapshot.cookies, snapshot.user_agent)
        return None

    def _usable(self) -> _Held:
        error = self._refresh()
        held = self._held  # a ready source always leaves a held generation
        if error or held is None:
            raise HktvError(error or self.missing_code)
        if held.state == "dead":
            raise HktvError("session_expired")
        return held

    @property
    def generation(self) -> int:
        with self._lock:
            self._refresh()
            return self._held.generation if self._held else 0

    def checkout_client(self) -> tuple[int, http.cookiejar.CookieJar, str]:
        """The generation, its cookie jar and the signed-in browser's User-Agent, read together."""
        with self._lock:
            held = self._usable()
            return held.generation, to_jar(held.cookies), held.user_agent

    def needs_verification(self, generation: int) -> bool:
        """A newly adopted generation must prove signed-in before even a basket read (guests have carts)."""
        with self._lock:
            return self._held is None or self._held.generation != generation or self._held.state != "live"

    def awaiting_verification(self) -> bool:
        """A usable generation nobody has proven signed in yet (after a restart or a newer session file)."""
        with self._lock:
            return self._refresh() is None and self._held is not None and self._held.state == "unverified"

    def store_jar(self, jar: http.cookiejar.CookieJar, generation: int) -> None:
        """Keep the cookies HKTVmall set during a call (fresh access_token) for that generation only."""
        with self._lock:
            if self._held is not None and self._held.generation == generation:
                self._held.cookies = from_jar(jar)

    def mark(self, state: str, generation: int) -> None:
        """Record what HKTVmall said about ``generation``'s sign-in; a newer one is never touched."""
        with self._lock:
            held = self._held
            if held is None or held.generation != generation or held.state == "dead":
                return
            held.state = state
            if state == "live":
                held.verified_at = self._clock()
            else:
                held.dead_at = self._clock()

    def never_signed_in(self) -> bool:
        """True only for a session file nobody has written yet (an external snapshot is always expected)."""
        return False

    def canary_error(self) -> str | None:
        with self._lock:
            try:
                self._usable()
            except HktvError as exc:
                return exc.code
            return None

    def status(self) -> dict[str, Any]:
        with self._lock:
            error = self._refresh()
            held = self._held
            token = next((c["expires"] for c in held.cookies if c["name"] == "access_token"), None) if held else None
            return {
                "signed_in": error is None and held is not None and held.state == "live",
                "state": held.state if held else "none",
                "generation": held.generation if held else 0,
                "verified_at": held.verified_at if held else None,
                "dead_at": held.dead_at if held else None,
                "token_expires_at": token,
                "session_source": {"state": self._source_state, "generation": self._source_generation},
            }


def session_payload(generation: int, email: str, user_agent: str, cookies: list[dict[str, Any]], now: float) -> dict:
    """A session file's content (snapshot schema 1), as ``parse_snapshot`` reads it."""
    return {
        "schema": 1,
        "target": "hktvmall",
        "generation": generation,
        "email": email,
        "user_agent": user_agent,
        "verified_at": datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds"),
        "cookies": cookies,
    }


class OwnedSession(SessionStore):
    """A session file this server owns. Writers (this server and a new login) hold ``lock_path``; a login raises
    the generation, and a write-back never overwrites a newer login."""

    seal_overhead = 0

    def __init__(self, path: Path, email: str, *, lock_path: Path, clock=time.time) -> None:
        super().__init__(path, email, clock=clock)
        self.lock_path = lock_path
        self.account_email = email.casefold()  # Account.verify also checks the home page's own contact email

    def _decode(self, blob: bytes) -> bytes:
        return blob

    def _encode(self, raw: bytes) -> bytes:
        return raw

    def _raw(self) -> bytes:
        try:
            blob = read_private(self.snapshot_path, MAX_SNAPSHOT + self.seal_overhead)
        except FileNotFoundError:
            raise SnapshotError("missing") from None
        except OSError:
            raise SnapshotError("unavailable") from None
        return self._decode(blob)

    def _read(self) -> Snapshot:
        return parse_snapshot(self._raw(), self.expected_email, self._clock())

    def _current_generation(self) -> int:
        """The file's generation, whoever it belongs to (a login for another account must still win), or 0."""
        try:
            generation = json.loads(self._raw()).get("generation")
        except (SnapshotError, ValueError, AttributeError):
            return 0
        return generation if isinstance(generation, int) and not isinstance(generation, bool) and generation > 0 else 0

    def write(self, email: str, user_agent: str, cookies: list[dict[str, Any]]) -> int:
        """Store a session from a login the person did themselves, one generation above the current file."""
        email = email.strip().casefold()
        with locked(self.lock_path):
            generation = self._current_generation() + 1
            payload = session_payload(generation, email, user_agent, cookies, self._clock())
            raw = json.dumps(payload).encode()
            parse_snapshot(raw, email, self._clock())  # SnapshotError: the login did not produce a usable session
            atomic_write_bytes(self.snapshot_path, self._encode(raw))
        return generation

    def store_jar(self, jar: http.cookiejar.CookieJar, generation: int) -> None:
        """Keep and persist the cookies HKTVmall set during a call, unless a newer login has replaced the file."""
        with self._lock:
            held = self._held
            if held is None or held.generation != generation:
                return
            cookies = from_jar(jar)
            changed = cookies != held.cookies
            held.cookies = cookies
            if not changed or held.state == "dead":
                return
            try:
                with locked(self.lock_path):
                    if self._read().generation != generation:
                        return  # a newer login won; the next call adopts it
                    payload = session_payload(generation, self.expected_email, held.user_agent, cookies, self._clock())
                    atomic_write_bytes(self.snapshot_path, self._encode(json.dumps(payload).encode()))
            except (SnapshotError, OSError) as exc:  # the in-memory cookies still apply to this process
                log.error("could not persist the session (%s)", type(exc).__name__)


class LocalSession(OwnedSession):
    """The default account's own ``session.json``: whichever account last signed in with ``hktvmall-mcp login``.
    No file yet is ``not_signed_in``."""

    missing_code = "not_signed_in"

    def __init__(self, path: Path, clock=time.time) -> None:
        super().__init__(path, "", lock_path=path.with_name(".session.lock"), clock=clock)
        self.account_email = None  # set from the file: Account.verify compares the home page with it

    def _read(self) -> Snapshot:
        raw = self._raw()
        try:
            email = json.loads(raw).get("email")
        except (ValueError, AttributeError):
            raise SnapshotError() from None
        if not isinstance(email, str) or "@" not in email:
            raise SnapshotError()
        snapshot = parse_snapshot(raw, email, self._clock())
        self.expected_email = self.account_email = snapshot.email
        return snapshot

    def never_signed_in(self) -> bool:
        return not self.snapshot_path.exists()

    def clear(self) -> bool:
        """Forget the saved session (``hktvmall-mcp logout``)."""
        with locked(self.lock_path):
            try:
                self.snapshot_path.unlink()
            except FileNotFoundError:
                return False
        return True

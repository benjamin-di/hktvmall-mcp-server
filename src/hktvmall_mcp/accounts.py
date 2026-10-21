"""Several HKTVmall accounts in one server: the default account and, optionally, connected people.

Each account is its own ``HktvService``: its own session, budget, unresolved errors, keep-alive and order
cache, so nothing of one account can reach another's tool results. What all accounts share is the server's IP
address: one request at a time, one pause when HKTVmall pushes back and a total daily budget (``Transport`` with
the shared state). Which account a call acts for comes from the caller's token, never from a tool argument.

A connected person's session lives in ``HKTVMALL_STATE_DIR/accounts/<id>/session.bin``: the session file format
(``snapshot.parse_snapshot`` validates it the same way), encrypted with AES-GCM under ``HKTVMALL_SESSION_KEY`` and
bound to the account id. Nobody republishes it, so the cookies HKTVmall replaces during calls (a remember-me
sign-in, a re-minted ``access_token``) are written back. A new login for the same email keeps the account id and
raises the generation.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .auth import BEARER_CLIENT
from .errors import HktvError
from .fsio import read_private
from .session import OwnedSession
from .snapshot import MAX_SNAPSHOT, SnapshotError
from .state import StateStore

if TYPE_CHECKING:
    from .config import Settings
    from .server import HktvService
    from .site import Transport

log = logging.getLogger(__name__)

DEFAULT = "default"  # the default account: the server's own session (local login or an external snapshot)
ACCOUNT_CLIENT = "account:"  # client_id prefix of a connected person's token
ACCOUNT_ID = re.compile(r"acct_[0-9a-f]{12}\Z")
MAGIC = b"HKTVS1"
NONCE = 12


def parse_key(raw: str) -> bytes:
    """HKTVMALL_SESSION_KEY: 32 random bytes, base64."""
    try:
        key = base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("HKTVMALL_SESSION_KEY must be base64") from None
    if len(key) != 32:
        raise ValueError("HKTVMALL_SESSION_KEY must be 32 bytes")
    return key


def seal(key: bytes, account_id: str, payload: dict[str, Any]) -> bytes:
    nonce = secrets.token_bytes(NONCE)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, json.dumps(payload).encode(), account_id.encode())


def unseal(key: bytes, account_id: str, blob: bytes) -> bytes:
    if not blob.startswith(MAGIC) or len(blob) > MAX_SNAPSHOT + 64:
        raise SnapshotError()
    nonce, sealed = blob[len(MAGIC) : len(MAGIC) + NONCE], blob[len(MAGIC) + NONCE :]
    try:
        return AESGCM(key).decrypt(nonce, sealed, account_id.encode())
    except InvalidTag:
        raise SnapshotError() from None


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}•••@{domain}" if domain else "•••"


class AccountSession(OwnedSession):
    """A connected person's session: the same generation and verification rules as any session file, sealed with
    AES-GCM under HKTVMALL_SESSION_KEY and bound to the account id."""

    seal_overhead = 64

    def __init__(self, directory: Path, account_id: str, email: str, key: bytes, clock=time.time) -> None:
        super().__init__(directory / "session.bin", email, lock_path=directory / ".lock", clock=clock)
        self.directory, self.account_id, self._key = directory, account_id, key

    def _decode(self, blob: bytes) -> bytes:
        return unseal(self._key, self.account_id, blob)

    def _encode(self, raw: bytes) -> bytes:
        return seal(self._key, self.account_id, json.loads(raw))


class AccountTransport:
    """One account's view of the shared transport: its own daily budget on top of the shared pacing, pause and
    total budget. ``state`` is the shared state, where a pause belongs (one IP for every account)."""

    def __init__(self, inner: Transport, account_state: StateStore) -> None:
        self.inner, self.account_state = inner, account_state

    @property
    def state(self) -> StateStore:
        return self.inner.state

    def send(self, *args: Any, **kwargs: Any) -> tuple[int, str]:
        self.inner.state.check_pause()
        self.account_state.take()
        return self.inner.send(*args, **kwargs)


class Accounts:
    """The default account plus every connected account found under ``accounts/``, built on first use."""

    def __init__(self, settings: Settings, transport: Transport | None, default: HktvService, clock=time.time):
        self.settings, self.transport, self._clock = settings, transport, clock
        self.directory = settings.state_dir / "accounts"
        self._services: dict[str, HktvService] = {DEFAULT: default}
        self._lock = threading.Lock()

    @property
    def default(self) -> HktvService:
        return self._services[DEFAULT]

    # -- who is calling ---------------------------------------------------------------------

    def resolve(self, client_id: str | None, *, open_default: bool = False) -> HktvService:
        """The account a caller acts for. ``open_default`` for a server without auth (stdio, tests)."""
        if client_id is None:
            if open_default:
                return self.default
            raise HktvError("not_connected", log_detail="no token")
        if client_id == BEARER_CLIENT:
            return self.default
        if client_id.startswith(ACCOUNT_CLIENT):
            return self.get(client_id[len(ACCOUNT_CLIENT) :])
        raise HktvError("not_connected", log_detail="unknown client")

    def get(self, account_id: str) -> HktvService:
        with self._lock:
            service = self._services.get(account_id)
            if service is not None:
                if account_id == DEFAULT or (self.directory / account_id / "session.bin").exists():
                    return service
                del self._services[account_id]  # disconnected meanwhile (admin disconnect)
                raise HktvError("not_connected", log_detail="account disconnected")
            if not ACCOUNT_ID.fullmatch(account_id) or self.settings.session_key is None:
                raise HktvError("not_connected", log_detail="no such account")
            email = self._email(account_id)
            if email is None:
                raise HktvError("not_connected", log_detail="no such account")
            service = self._build(account_id, email)
            self._services[account_id] = service
            return service

    def services(self) -> list[HktvService]:
        """Every account, for the keep-alive loop; accounts whose files went away are dropped."""
        ids = self.ids()
        with self._lock:
            for gone in [i for i in self._services if i != DEFAULT and i not in ids]:
                del self._services[gone]
        out = [self.default]
        for account_id in ids:
            try:
                out.append(self.get(account_id))
            except HktvError:
                continue
        return out

    def ids(self) -> list[str]:
        if self.settings.session_key is None:
            return []
        try:
            entries = list(self.directory.iterdir())
        except FileNotFoundError:
            return []
        return sorted(p.name for p in entries if ACCOUNT_ID.fullmatch(p.name) and (p / "session.bin").exists())

    def email(self, account_id: str) -> str | None:
        """A connected account's HKTVmall email (for the connect page's own checks; never shown unmasked)."""
        if not ACCOUNT_ID.fullmatch(account_id) or self.settings.session_key is None:
            return None
        return self._email(account_id)

    def _email(self, account_id: str) -> str | None:
        """The account's email, from its sealed session (read loosely; the session parse checks the rest)."""
        assert self.settings.session_key is not None
        try:
            blob = read_private(
                self.directory / account_id / "session.bin", MAX_SNAPSHOT + AccountSession.seal_overhead
            )
            data = json.loads(unseal(self.settings.session_key, account_id, blob))
        except (OSError, ValueError, SnapshotError):
            return None
        email = data.get("email") if isinstance(data, dict) else None
        return email.casefold() if isinstance(email, str) and "@" in email else None

    def _build(self, account_id: str, email: str) -> HktvService:
        from .server import HktvService
        from .site import Site

        assert self.settings.session_key is not None and self.transport is not None
        directory = self.directory / account_id
        state = StateStore(directory, budget=self.settings.account_daily_budget, clock=self._clock)
        session = AccountSession(directory, account_id, email, self.settings.session_key, clock=self._clock)
        site = Site(AccountTransport(self.transport, state), session)  # type: ignore[arg-type]
        return HktvService(site, state, session, self.settings, shared=self.transport.state, account_id=account_id)

    # -- connecting and removing ------------------------------------------------------------

    def adopt(self, email: str, cookies: list[dict[str, Any]], user_agent: str) -> str:
        """Store a session from a login the person did themselves; the same email keeps its account id.
        The result is validated exactly like any session file before it is written."""
        if self.settings.session_key is None:
            raise HktvError("not_connected", log_detail="HKTVMALL_SESSION_KEY unset")
        email = email.strip().casefold()
        account_id = next((i for i in self.ids() if self._email(i) == email), None) or f"acct_{secrets.token_hex(6)}"
        directory = self.directory / account_id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        session = AccountSession(directory, account_id, email, self.settings.session_key, clock=self._clock)
        try:
            generation = session.write(email, user_agent, cookies)
        except SnapshotError:
            raise HktvError("invalid_argument", "the login did not produce a usable HKTVmall session") from None
        log.info("account %s connected (generation %d)", account_id, generation)
        return account_id

    def remove(self, account_id: str) -> bool:
        if not ACCOUNT_ID.fullmatch(account_id):
            return False
        with self._lock:
            self._services.pop(account_id, None)
        directory = self.directory / account_id
        if not directory.exists():
            return False
        shutil.rmtree(directory)
        log.info("account %s removed", account_id)
        return True

    def describe(self) -> list[dict[str, Any]]:
        """For the operator: ids, masked emails and sign-in state; never cookies."""
        out = []
        for service in self.services()[1:]:
            status = service.session.status()
            out.append(
                {
                    "id": service.account_id,
                    "email": mask_email(service.session.expected_email),
                    "generation": status["generation"],
                    "state": status["state"],
                }
            )
        return out

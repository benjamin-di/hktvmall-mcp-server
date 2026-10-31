"""Accounts in one server: the default account (stdio and single-account HTTP) or, behind an authenticating
gateway, one record per person.

Each account is its own ``HktvService``: its own session, budget, unresolved errors, keep-alive and order
cache, so nothing of one account can reach another's tool results. What all accounts share is the server's IP
address: one request at a time, one pause when HKTVmall pushes back and a total daily budget (``Transport`` with
the shared state). Which account a call acts for comes from the gateway's verified person, never from a tool
argument.

People come from the identity provider: the first verified call or Connections sign-in of a person creates their
record, ``HKTVMALL_STATE_DIR/people/<person>/``, holding their connection control (owner, sharing, disconnect
barrier) beside their session, budget and caches, so the two can never be restored apart. A person's session
(``session.bin``) is the session file format, encrypted with AES-GCM under ``HKTVMALL_SESSION_KEY`` and bound to
the person. Nobody republishes it, so the cookies HKTVmall replaces during calls are written back.

A connection belongs to the person who signed in; only they connect, disconnect or share it. A person uses their
own connection or the one connection shared with them, never both, and never falls back to another account
(docs/PERMISSIONS.md).
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import account_connections as sharing
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .auth import BEARER_CLIENT
from .connections_state import MAX_SHARES, ConnectionState, revision
from .errors import HktvError
from .fsio import locked, read_private
from .session import OwnedSession
from .snapshot import MAX_SNAPSHOT, SnapshotError
from .state import StateStore

if TYPE_CHECKING:
    from .config import Settings
    from .server import HktvService
    from .site import Transport

log = logging.getLogger(__name__)

DEFAULT = "default"  # the default account: the server's own session (local login or an external snapshot)
PERSON = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")  # the identity provider's administrator-owned person_id
CONTROL = "connection"  # people/<person>/connection.json
MAX_PEOPLE = 100
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
    """A person's session: the same generation and verification rules as any session file, sealed with AES-GCM
    under HKTVMALL_SESSION_KEY and bound to the person. No file yet is ``not_signed_in``."""

    seal_overhead = 64
    missing_code = "not_signed_in"

    def __init__(
        self, directory: Path, account_id: str, email: str, key: bytes, clock=time.time, *, generation_floor=0
    ) -> None:
        super().__init__(directory / "session.bin", email, lock_path=directory / ".lock", clock=clock)
        self.directory, self.account_id, self._key = directory, account_id, key
        self.generation_floor = generation_floor

    def _current_generation(self):
        return max(super()._current_generation(), self.generation_floor)

    def _decode(self, blob: bytes) -> bytes:
        return unseal(self._key, self.account_id, blob)

    def _encode(self, raw: bytes) -> bytes:
        return seal(self._key, self.account_id, json.loads(raw))

    def never_signed_in(self) -> bool:
        return not self.snapshot_path.exists()


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
    """The default account, plus one record per person when a gateway asserts people; services built on first use."""

    def __init__(self, settings: Settings, transport: Transport | None, default: HktvService, clock=time.time):
        self.settings, self.transport, self._clock = settings, transport, clock
        self.directory = settings.state_dir / "people"
        self._services: dict[str, HktvService] = {DEFAULT: default}
        self._epochs: dict[str, str] = {}
        self._lock = threading.Lock()
        default_control = self.control(DEFAULT)
        default.session.access_check = default_control.require_active
        default.session.read_guard = default_control.admission
        default.session.write_guard = default_control.active

    @property
    def people(self) -> bool:
        """People mode: the gateway asserts a verified person and each person has their own record."""
        return self.settings.trusted_account_header is not None

    def control(self, account: str) -> ConnectionState:
        if account == DEFAULT:
            return ConnectionState(
                self.settings.controls_dir or self.settings.state_dir / "connection-controls", DEFAULT
            )
        if not isinstance(account, str) or not PERSON.fullmatch(account):
            raise HktvError("not_connected")
        return ConnectionState(self.directory / account, CONTROL, require_existing=True)

    # -- people ------------------------------------------------------------------------------

    def known(self) -> list[str]:
        """Every person with a record (the roster), sorted."""
        try:
            entries = list(self.directory.iterdir())
        except FileNotFoundError:
            return []
        return sorted(
            p.name
            for p in entries
            if PERSON.fullmatch(p.name) and p.name != DEFAULT and (p / (CONTROL + ".json")).exists()
        )

    def person(self, person: Any, *, create: bool = True) -> ConnectionState:
        """The verified person's control, creating their record on first contact. A folder without its control
        (lost or replaced) refuses; it is never recreated around stored state."""
        if not self.people or self.settings.session_key is None or person == DEFAULT:
            raise HktvError("not_connected")  # "default" is the single-account mode's own, never a person
        control = self.control(person)
        folder = self.directory / person
        if (folder / (CONTROL + ".json")).exists() or not create:
            with self._people_lock():
                self._migrate_people()
            return control
        with self._people_lock():
            self._migrate_people()
            if folder.exists() or folder.is_symlink():
                return control  # created meanwhile, or stored state without its control: read() decides
            if len(self.known()) >= MAX_PEOPLE:
                raise HktvError("not_connected", log_detail="person limit reached")
            staging = self.directory / f".new-{person}-{secrets.token_hex(4)}"  # never a person name
            staging.mkdir(mode=0o700)
            try:
                staged = ConnectionState(staging, CONTROL, require_existing=False)
                staged.initialize()
                with staged.edit() as data:
                    data.update(
                        version=2,
                        selection={"account": person, "grant": None},
                        selection_revision=revision(),
                        shares={},
                        shared_with=[],
                    )
                staging.rename(folder)  # a record appears whole or not at all
            except BaseException:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            log.info("person %s recorded", _tag(person))
        return control

    def _migrate_people(self):
        """Under the people lock, preserve legacy routing and grants. New controls reject old readers.

        Each rename is durable independently. Deterministic legacy grant revisions let a partial migration
        resume without changing the grant references already written to v2 recipients.
        Missing or unreadable retained controls are never repaired.
        """
        records, damaged = {}, False
        for person in self._folders():
            try:
                records[person] = self.control(person).read()
            except HktvError:
                damaged = True
        legacy = {p for p, data in records.items() if data["version"] == 1}
        if not legacy:
            return
        if damaged:
            raise HktvError("connection_disconnected")
        grants = {
            p: data["shares"]
            if data["version"] == 2
            else {
                m: {
                    "revision": base64.urlsafe_b64encode(
                        hmac.digest(
                            self.settings.session_key,
                            f"legacy-share:{p}:{m}".encode(),
                            "sha256",
                        )[:24]
                    ).decode(),
                    "order_history": True,
                }
                for m in data.get("shared_with", [])
            }
            for p, data in records.items()
        }
        for person in sorted(legacy):
            data = records[person]
            own = data["provider_email"] is not None or (self.directory / person / "session.bin").exists()
            incoming = [p for p, members in grants.items() if person in members and p != person]
            if not own and len(incoming) > 1:
                raise HktvError("not_connected", log_detail="ambiguous legacy selection")
            owner = person if own or not incoming else incoming[0]
            with self.control(person).edit() as current:
                current.update(
                    version=2,
                    shares=grants[person],
                    shared_with=sorted(grants[person]),
                    selection={
                        "account": owner,
                        "grant": None if owner == person else grants[owner][person]["revision"],
                    },
                    selection_revision=revision(),
                )

    def _people_lock(self):
        """Serialises record creation and every decision that spans several people (connect, share)."""
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return locked(self.directory / ".people.lock")

    def owns(self, person: str) -> bool:
        """Whether the person has a connection of their own: bound to a provider account or holding a session,
        also while disconnected (a disconnected connection never falls back to a shared one)."""
        if (self.directory / person / "session.bin").exists():
            return True
        return self.control(person).read()["provider_email"] is not None

    def offers(self, person: str) -> tuple[list[dict], bool]:
        """Readable grants and whether any records were unavailable; never infer grants from damaged state."""
        found, unavailable = [], False
        for owner in self._folders():
            if owner == person:
                continue
            try:
                grant = self.control(owner).read()["shares"].get(person)
            except HktvError:
                unavailable = True
                continue
            if grant is not None:
                found.append({"owner": owner, **grant})
        return found, unavailable

    def sharer(self, person: str) -> str | None:
        """The selected shared owner, even if their grant has since been revoked (never a fallback)."""
        selected = self.control(person).read()["selection"]
        return selected["account"] if selected and selected["account"] != person else None

    def _folders(self) -> list[str]:
        """Every person folder, including one whose control record is missing."""
        try:
            entries = list(self.directory.iterdir())
        except FileNotFoundError:
            return []
        return sorted(p.name for p in entries if PERSON.fullmatch(p.name) and p.name != DEFAULT and p.is_dir())

    def name(self, person: str) -> str:
        """A person's display name (identity provider ``name`` at their latest Connections sign-in), for display."""
        try:
            name = self.control(person).read().get("name")
        except HktvError:
            name = None
        return name or person

    def note_name(self, person: str, name: Any) -> None:
        clean = display_name(name)
        if clean is None:
            return
        control = self.control(person)
        if control.read().get("name") == clean:
            return
        with control.edit() as data:
            data["name"] = clean

    def account_for(self, person: Any) -> str:
        """The explicitly selected account. Ownership and offers never imply a routing change."""
        self.person(person)
        with self._people_lock():
            return self._selected(person)[0]

    @staticmethod
    def _selection(data):
        choice = data["selection"]
        return sharing.Selection(
            sharing.Choice(choice["account"], choice["grant"]) if choice else None,
            data["selection_revision"],
        )

    @staticmethod
    def _grant(data):
        return (
            sharing.Grant(data["revision"], frozenset({"shopping", *(["history"] if data["order_history"] else [])}))
            if data
            else None
        )

    @staticmethod
    def _save_selection(data, selected):
        data.update(
            selection={"account": selected.choice.owner, "grant": selected.choice.grant_revision}
            if selected.choice
            else None,
            selection_revision=selected.revision,
        )

    @staticmethod
    def _sharing_call(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except sharing.ConnectionError as error:
            raise HktvError("history_not_shared" if error.code == "capability_not_shared" else error.code) from None

    def _selected(self, person):
        data = self.control(person).read()
        selected = self._selection(data)
        grant = None
        if selected.choice and selected.choice.owner != person:
            try:
                grant = self.control(selected.choice.owner).read()["shares"].get(person)
            except HktvError:
                raise HktvError("session_source_missing", log_detail="selected owner control unavailable") from None
        choice = self._sharing_call(sharing.resolve, person, selected, self._grant(grant))
        return choice.owner, data, grant

    def select(self, person, owner, expected, grant=None):
        self.person(person)
        with self._people_lock(), self.control(person).edit() as data:
            if owner == DEFAULT or owner not in self.known():
                raise HktvError("not_connected")
            offered = self.control(owner).read()["shares"].get(person) if owner != person else None
            # Preserve the established public stale-own-form outcome.
            if owner == person and grant is not None:
                raise HktvError("account_changed")
            if grant is not None and (not isinstance(grant, str) or re.fullmatch(r"[A-Za-z0-9_-]{32}", grant) is None):
                raise HktvError("shared_access_removed")
            selected = self._sharing_call(
                sharing.choose,
                person,
                self._selection(data),
                sharing.Choice(owner, grant),
                self._grant(offered),
                expected=expected,
            )
            self._save_selection(data, selected)

    def share(self, owner: str, member: str, epoch: str, state: str, *, history=False, grant="new") -> None:
        with self._people_lock():  # no connect or other share can change ownership between check and write
            if member == owner or member not in self.known():
                raise HktvError("not_connected")
            self.control(member).read()  # a retained but unreadable identity cannot receive a new grant
            if not self.owns(owner) or type(history) is not bool:
                raise HktvError("not_connected")
            with self.control(owner).edit() as data:
                ConnectionState.expected(data, epoch, state)
                shared = list(data.get("shared_with", []))
                previous = data["shares"].get(member)
                offered = self._sharing_call(
                    sharing.offer,
                    self._grant(previous),
                    expected=None if grant == "new" else grant,
                    capabilities=frozenset({"shopping", *(["history"] if history else [])}),
                )
                if member not in shared:
                    if len(shared) >= MAX_SHARES:
                        raise HktvError("not_connected")
                    data["shared_with"] = sorted([*shared, member])
                data["shares"][member] = {"revision": offered.revision, "order_history": history}

        log.info("person %s shared their connection with %s", _tag(owner), _tag(member))

    def unshare(self, owner: str, member: str, epoch: str, state: str, *, grant: str, leaving=False) -> None:
        if owner == DEFAULT:
            raise HktvError("not_connected")
        with self._people_lock():
            with self.control(owner).edit() as data:
                ConnectionState.expected(data, epoch, state)
                current = data["shares"].get(member)
                self._sharing_call(sharing.revoke, self._grant(current), expected=grant)
                data["shared_with"] = [p for p in data["shared_with"] if p != member]
                del data["shares"][member]
            if leaving:
                # Revocation commits first. A crash remains denied. Keep the people lock until selection
                # is cleared, including a stale scope revision, so no new choice can be cleared by this leave.
                with self.control(member).edit() as data:
                    self._save_selection(data, sharing.leave(self._selection(data), owner))
        log.info("person %s stopped sharing with %s", _tag(owner), _tag(member))

    def forget(self, account: str) -> None:
        if account != DEFAULT:
            with self._lock:
                self._services.pop(account, None)
                self._epochs.pop(account, None)

    @property
    def default(self) -> HktvService:
        return self._services[DEFAULT]

    # -- who is calling ---------------------------------------------------------------------

    def resolve(self, client_id: str | None, *, open_default: bool = False, person: str | None = None) -> HktvService:
        """The account a caller acts for. ``open_default`` for a server without auth (stdio, tests)."""
        if client_id is None:
            if open_default:
                return self.get(DEFAULT)
            raise HktvError("not_connected", log_detail="no token")
        if client_id == BEARER_CLIENT:
            return self.get(self.account_for(person)) if self.people else self.get(DEFAULT)
        raise HktvError("not_connected", log_detail="unknown client")

    def admit(self, client_id, *, person=None, open_default=False, context=None, writing=False, history=False):
        """Snapshot authorization and routing once per tool call, before any cache or provider access.

        Previously admitted work may finish on its captured service. It never resolves a different account
        halfway through a call. A context is an opaque MAC, not permission to choose an account.
        """
        if not self.people:
            return self.resolve(client_id, open_default=open_default), None
        if client_id != BEARER_CLIENT:
            raise HktvError("not_connected")
        self.person(person)
        with self._people_lock():
            owner, data, grant = self._selected(person)
            control = self.control(owner).read()
            raw = json.dumps(
                ["basket-context-v1", person, owner, data["selection_revision"], control["epoch"], grant],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            token = hmac.digest(self.settings.session_key, raw, "sha256").hex()
            if writing and (
                not isinstance(context, str)
                or not re.fullmatch(r"[0-9a-f]{64}", context)
                or not hmac.compare_digest(context, token)
            ):
                raise HktvError("account_changed")
            if history:
                self._sharing_call(
                    sharing.resolve, person, self._selection(data), self._grant(grant), capability="history"
                )
            return self.get(owner), token

    def get(self, account: str) -> HktvService:
        epoch = self.control(account).require_active()
        with self._lock:
            service = self._services.get(account)
            if service is not None and (account == DEFAULT or self._epochs.get(account) == epoch):
                return service
            if account == DEFAULT or self.settings.session_key is None:
                raise HktvError("not_connected", log_detail="no such account")
            service = self._build(account)
            self._services[account] = service
            self._epochs[account] = epoch
            return service

    def services(self) -> list[HktvService]:
        """Every active signed-in account, for the keep-alive loop."""
        out = []
        try:
            if self.control(DEFAULT).read()["state"] == "active" and not self.people:
                out.append(self.default)
        except HktvError:
            pass
        for person in self.known() if self.people else []:
            if not (self.directory / person / "session.bin").exists():
                continue
            try:
                out.append(self.get(person))
            except HktvError:
                continue
        return out

    def _email(self, person: str) -> str | None:
        """The person's provider email, from their sealed session (read loosely; the session parse checks the
        rest), else the email their control bound."""
        assert self.settings.session_key is not None
        try:
            blob = read_private(self.directory / person / "session.bin", MAX_SNAPSHOT + AccountSession.seal_overhead)
            data = json.loads(unseal(self.settings.session_key, person, blob))
            email = data.get("email") if isinstance(data, dict) else None
        except FileNotFoundError:
            email = None
        except (OSError, ValueError, SnapshotError):
            raise HktvError("not_connected", log_detail="unreadable session") from None
        if isinstance(email, str) and "@" in email:
            return email.casefold()
        return self.control(person).read()["provider_email"]

    def email(self, person: str) -> str | None:
        try:
            return self._email(person)
        except HktvError:
            return None

    def _build(self, person: str) -> HktvService:
        from .server import HktvService
        from .site import Site

        assert self.settings.session_key is not None and self.transport is not None
        directory = self.directory / person
        state = StateStore(directory, budget=self.settings.account_daily_budget, clock=self._clock)
        email = self._email(person) or ""
        session = AccountSession(directory, person, email, self.settings.session_key, clock=self._clock)
        control = self.control(person)
        epoch = control.require_active()
        session.access_check = lambda: control.require_active(epoch)
        session.read_guard = lambda: control.admission(epoch)
        session.write_guard = lambda: control.active(epoch)
        site = Site(AccountTransport(self.transport, state), session)  # type: ignore[arg-type]
        return HktvService(site, state, session, self.settings, shared=self.transport.state, account_id=person)

    # -- connecting and disconnecting ----------------------------------------------------------

    def adopt_owned(self, person, epoch, email, cookies, user_agent):
        """Store the session from a sign-in the person did themselves. The first provider account a person connects
        is theirs; another person's provider account is refused, so nobody signs in to someone else's account."""
        if person == DEFAULT or self.settings.session_key is None:
            raise HktvError("not_connected")
        email = email.strip().casefold()
        control = self.control(person)
        with self._people_lock(), control.active(epoch) as data:
            expected = data["provider_email"] or self.email(person)
            if expected and expected != email:
                raise HktvError("account_mismatch")
            if not expected:  # binding a provider account for the first time: it must be nobody else's
                for other in self._folders():
                    if other != person and self._bound(other) in (email, None):
                        raise HktvError("account_mismatch")  # theirs, or unreadable and possibly theirs
            directory = self.directory / person
            session = AccountSession(
                directory,
                person,
                email,
                self.settings.session_key,
                clock=self._clock,
                generation_floor=data["generation"],
            )
            try:
                generation = session.write(email, user_agent, cookies)
            except SnapshotError:
                raise HktvError("invalid_argument", "the login did not produce a usable HKTVmall session") from None
            data.update(provider_email=email, generation=generation)
        self.forget(person)
        log.info("person %s connected (generation %d)", _tag(person), generation)
        return person

    def _bound(self, person: str) -> str | None:
        """The provider account a person's connection is bound to; "" when none; None when unreadable."""
        try:
            return self.control(person).read()["provider_email"] or self._email(person) or ""
        except HktvError:
            return None

    def disconnect_owned(self, person, epoch):
        control = self.control(person)
        with control.edit() as data:
            if data["state"] != "disconnecting" or data["epoch"] != epoch:
                raise HktvError("connection_disconnected")
            self.forget(person)
            if person != DEFAULT:
                data["provider_email"] = data["provider_email"] or self.email(person)
                blob = self.directory / person / "session.bin"
                if blob.exists() and self.settings.session_key:
                    try:
                        snapshot = json.loads(
                            unseal(self.settings.session_key, person, read_private(blob, MAX_SNAPSHOT + 64))
                        )
                        data["generation"] = max(data["generation"], int(snapshot.get("generation", 0)))
                    except (OSError, ValueError, TypeError, AttributeError, SnapshotError):
                        # an unreadable session (a rotated key, damage) is still deleted; past any watermark
                        log.warning("person %s: unreadable session removed on disconnect", _tag(person))
                        data["generation"] += 1
                blob.unlink(missing_ok=True)
            data["state"] = "disconnected"

    def describe(self) -> list[dict[str, Any]]:
        """For the operator: people, masked emails, sign-in state and sharing; never cookies."""
        out = []
        for person in self.known():
            try:
                control = self.control(person).read()
            except HktvError:
                out.append({"person": person, "state": "unreadable"})
                continue
            email = self.email(person)
            out.append(
                {
                    "person": person,
                    "name": control.get("name"),
                    "email": mask_email(email) if email else None,
                    "state": control["state"],
                    "signed_in": (self.directory / person / "session.bin").exists(),
                    "shared_with": control.get("shared_with", []),
                }
            )
        return out


def display_name(value: Any) -> str | None:
    """A display name safe to show and to quote to a model: letters, digits, spaces and . ' - only, at most 40."""
    if not isinstance(value, str):
        return None
    clean = " ".join(re.sub(r"[^\w .'-]", "", value, flags=re.UNICODE).split())[:40].strip()
    return clean or None


def _tag(person: str) -> str:
    """A person in logs: a short stable hash, never the identifier itself."""
    import hashlib

    return hashlib.sha256(person.encode()).hexdigest()[:10]

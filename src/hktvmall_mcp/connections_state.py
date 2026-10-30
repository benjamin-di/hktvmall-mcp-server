"""Connection ownership and durable access barriers, independent of browser sessions."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import stat
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from .errors import HktvError
from .fsio import atomic_write_bytes, atomic_write_json, locked, read_private

BASE_FIELDS = {"version", "epoch", "state", "owner", "provider_email", "generation"}
OPTIONAL_FIELDS = {"shared_with", "name"}  # a person's record only: who they share with, their display name
MAX_SHARES = 20
_PERSON = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")


class ConnectionState:
    def __init__(self, folder: Path, account: str, *, require_existing=None):
        self.path = folder / (account + ".json")
        self.marker = folder / (account + ".initialized")
        self.required = (
            os.environ.get("CONNECTIONS_REQUIRE_CONTROLS", "0") != "0" if require_existing is None else require_existing
        )

    def mark(self):
        if not self.marker.exists():
            atomic_write_bytes(self.marker, b"1\n")

    def initialize(self):
        with self.edit():
            pass
        return self.read()

    def read(self):
        try:
            data = json.loads(read_private(self.path, 4096))
        except FileNotFoundError:
            if self.marker.exists() or self.marker.is_symlink() or self.required:
                raise HktvError("connection_disconnected") from None
            return {
                "version": 1,
                "epoch": "initial",
                "state": "active",
                "owner": None,
                "provider_email": None,
                "generation": 0,
            }
        except (ValueError, OSError):
            raise HktvError("connection_disconnected") from None
        if (
            not isinstance(data, dict)
            or not BASE_FIELDS <= set(data) <= BASE_FIELDS | OPTIONAL_FIELDS
            or type(data["version"]) is not int
            or data["version"] != 1
            or not isinstance(data["epoch"], str)
            or not re.fullmatch(r"initial|[A-Za-z0-9_-]{32}", data["epoch"])
            or data["state"] not in ("active", "disconnecting", "disconnected")
            or type(data["generation"]) is not int
            or data["generation"] < 0
            or (data["provider_email"] is not None and not isinstance(data["provider_email"], str))
            or (
                "shared_with" in data
                and (
                    not isinstance(data["shared_with"], list)
                    or len(data["shared_with"]) > MAX_SHARES
                    or len(set(map(str, data["shared_with"]))) != len(data["shared_with"])
                    or not all(isinstance(p, str) and _PERSON.fullmatch(p) for p in data["shared_with"])
                )
            )
            or (
                "name" in data
                and data["name"] is not None
                and (not isinstance(data["name"], str) or len(data["name"]) > 40)
            )
        ):
            raise HktvError("connection_disconnected")
        owner = data["owner"]
        if owner is not None and (
            not isinstance(owner, dict)
            or set(owner) != {"issuer", "sub"}
            or not all(isinstance(value, str) and value for value in owner.values())
        ):
            raise HktvError("connection_disconnected")
        self.mark()
        return data

    @contextmanager
    def edit(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with locked(self.path.with_suffix(".lock")):
            data = self.read()
            yield data
            self.mark()
            atomic_write_json(self.path, data)

    @asynccontextmanager
    async def lifecycle(self):
        import fcntl

        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path.with_suffix(".lifecycle"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise HktvError("connection_disconnected")
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.025)
            yield
        finally:
            os.close(fd)

    @staticmethod
    def expected(data, epoch, state):
        if epoch is not None and (data["epoch"] != epoch or data["state"] != state):
            raise HktvError("connection_disconnected")

    @staticmethod
    def bind_data(data, identity):
        if identity is not None:
            owner = {"issuer": identity["issuer"], "sub": identity["sub"]}
            if data["owner"] not in (None, owner):
                raise HktvError("not_connected")
            data["owner"] = owner

    def require_active(self, epoch=None):
        data = self.read()
        if (
            (self.path.with_suffix(".pending").exists() or self.path.with_suffix(".pending").is_symlink())
            or data["state"] != "active"
            or (epoch is not None and data["epoch"] != epoch)
        ):
            raise HktvError("connection_disconnected")
        return data["epoch"]

    @contextmanager
    def admission(self, epoch=None):
        """Keep a snapshot borrow on one side of a native commit or owner barrier."""
        with locked(self.path.with_suffix(".lock")):
            self.require_active(epoch)
            yield

    @contextmanager
    def active(self, epoch=None):
        with self.edit() as data:
            if (
                (self.path.with_suffix(".pending").exists() or self.path.with_suffix(".pending").is_symlink())
                or data["state"] != "active"
                or (epoch is not None and data["epoch"] != epoch)
            ):
                raise HktvError("connection_disconnected")
            yield data

    def owner_matches(self, identity):
        owner = self.read()["owner"]
        return owner is None or owner == {"issuer": identity["issuer"], "sub": identity["sub"]}

    def bind(self, identity):
        with self.edit() as data:
            owner = {"issuer": identity["issuer"], "sub": identity["sub"]}
            if data["owner"] is not None and data["owner"] != owner:
                raise HktvError("not_connected")
            data["owner"] = owner

    def _health(self):
        hint = json.loads(read_private(self.path.with_suffix(".health.json"), 4096))
        if (
            not isinstance(hint, dict)
            or set(hint)
            not in (
                {"version", "epoch", "generation", "state"},
                {"version", "epoch", "previous_epoch", "generation", "state"},
            )
            or type(hint["version"]) is not int
            or hint["version"] != 1
            or not isinstance(hint["epoch"], str)
            or not re.fullmatch(r"initial|[A-Za-z0-9_-]{32}", hint["epoch"])
            or type(hint["generation"]) is not int
            or hint["generation"] < 1
            or hint["state"] not in ("sign_in_needed", "check_needed")
            or (
                "previous_epoch" in hint
                and (
                    not isinstance(hint["previous_epoch"], str)
                    or not re.fullmatch(r"initial|[A-Za-z0-9_-]{32}", hint["previous_epoch"])
                )
            )
        ):
            raise ValueError("invalid connection health")
        return hint

    def health(self, generation):
        """Read the hint and its control epoch together, never across an owner mutation."""
        path, pending = self.path.with_suffix(".health.json"), self.path.with_suffix(".pending")
        if not (path.exists() or path.is_symlink() or pending.exists() or pending.is_symlink()):
            return None
        with locked(self.path.with_suffix(".lock")):
            if pending.exists() or pending.is_symlink():
                return "check_needed"
            try:
                hint = self._health()
            except FileNotFoundError:
                return None
            except (ValueError, OSError):
                return "check_needed"
            if self.read()["epoch"] in (hint["epoch"], hint.get("previous_epoch")) and hint["generation"] == generation:
                return hint["state"]
            return None

    def note_signed_out(self, epoch, generation):
        self._note_health(epoch, generation, "sign_in_needed")

    def note_attention(self, epoch, generation):
        self._note_health(epoch, generation, "check_needed")

    def _note_health(self, epoch, generation, state):
        if type(generation) is not int or generation < 1:
            return  # No accepted snapshot exists; the normal reader already reports missing sign-in.
        with self.active(epoch):
            if state == "check_needed":
                try:
                    existing = self._health()
                except (ValueError, OSError):
                    existing = None
                if (
                    existing is not None
                    and epoch in (existing["epoch"], existing.get("previous_epoch"))
                    and existing["generation"] == generation
                    and existing["state"] == "sign_in_needed"
                ):
                    return  # A less specific outcome cannot erase positive signed-out proof.
            atomic_write_json(
                self.path.with_suffix(".health.json"),
                {
                    "version": 1,
                    "epoch": epoch,
                    "generation": generation,
                    "state": state,
                },
            )

    def _carry_health(self, previous, current):
        try:
            hint = self._health()
        except (ValueError, OSError):
            return
        if previous in (hint["epoch"], hint.get("previous_epoch")):
            # Written before the control rename: either side of a crash still
            # identifies the same retained dead generation. The next native
            # observation uses only its captured current control epoch.
            hint.update(epoch=current, previous_epoch=previous)
            atomic_write_json(self.path.with_suffix(".health.json"), hint)

    def suspend(self, epoch=None, state=None, identity=None):
        with self.edit() as data:
            self.expected(data, epoch, state)
            self.bind_data(data, identity)
            previous = data["epoch"]
            data.update(state="disconnecting", epoch=secrets.token_urlsafe(24))
            self._carry_health(previous, data["epoch"])
            return data["epoch"]

    def finish(self, epoch):
        with self.edit() as data:
            if data["epoch"] == epoch and data["state"] == "disconnecting":
                data["state"] = "disconnected"

    def resume(self, epoch=None, state=None, identity=None):
        with self.edit() as data:
            self.expected(data, epoch, state)
            self.bind_data(data, identity)
            previous = data["epoch"]
            if data["state"] == "disconnecting":
                raise HktvError("connection_disconnected")
            data.update(state="active", epoch=secrets.token_urlsafe(24))
            self._carry_health(previous, data["epoch"])
            return data["epoch"]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Initialize the default account's connection controls once (people get theirs on first contact)"
    )
    parser.add_argument("--initialize", action="store_true", required=True)
    parser.add_argument("--account", default="default", choices=["default"])
    args = parser.parse_args()
    folder = Path(os.environ["HKTVMALL_CONNECTIONS_CONTROLS_DIR"])
    ConnectionState(folder, args.account, require_existing=False).initialize()

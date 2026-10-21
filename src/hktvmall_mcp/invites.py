"""Invites: personal codes the operator gives each friend, entered on the connect page. Only the code's hash
is stored. An invite belongs to the first HKTVmall account that connects with it; after that it works only for
that account again (a new device, or logging in again), so a forwarded code cannot connect someone else."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from .fsio import atomic_write_json

# Crockford base32: no I, L, O or U, so a code read aloud or retyped survives; 12 characters = 60 bits, and
# wrong codes are rate-limited (connect.Tries).
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CONFUSABLE = str.maketrans({"O": "0", "I": "1", "L": "1", "U": "V"})


def normalise(code: str) -> str:
    return "".join(c for c in code.upper().translate(CONFUSABLE) if c in ALPHABET)


def _hash(code: str) -> str:
    return hashlib.sha256(normalise(code).encode()).hexdigest()


class Invites:
    def __init__(self, state_dir: Path, clock=time.time) -> None:
        self.path = Path(state_dir) / "invites.json"
        self._clock = clock
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, data)

    def create(self, label: str) -> str:
        """A new code, shown as XXXX-XXXX-XXXX (any case, spacing or dashes are accepted back)."""
        raw = "".join(secrets.choice(ALPHABET) for _ in range(12))
        code = "-".join(raw[i : i + 4] for i in range(0, 12, 4))
        with self._lock:
            data = self._load()
            data[_hash(code)] = {"label": label[:80], "created_at": self._clock(), "email": None, "account": None}
            self._save(data)
        return code

    def find(self, code: str) -> str | None:
        """The invite's key, if the code is a live invite."""
        if not code or len(code) > 64 or len(normalise(code)) != 12:
            return None
        key = _hash(code)
        with self._lock:
            entry = self._load().get(key)
        return key if entry and not entry.get("revoked") else None

    def allows(self, key: str, email: str) -> bool:
        with self._lock:
            entry = self._load().get(key)
        return bool(entry) and not entry.get("revoked") and entry.get("email") in (None, email.casefold())

    def bind(self, key: str, email: str, account_id: str) -> None:
        with self._lock:
            data = self._load()
            entry = data.get(key)
            if entry and entry.get("email") is None:
                entry.update(email=email.casefold(), account=account_id, connected_at=self._clock())
                self._save(data)

    def revoke(self, prefix: str) -> bool:
        with self._lock:
            data = self._load()
            keys = [k for k in data if k.startswith(prefix)] if len(prefix) >= 6 else []
            if len(keys) != 1:
                return False
            data[keys[0]]["revoked"] = True
            self._save(data)
        return True

    def describe(self) -> list[dict[str, Any]]:
        with self._lock:
            data = self._load()
        return [
            {"invite": key[:8], "label": e.get("label"), "account": e.get("account"), "revoked": bool(e.get("revoked"))}
            for key, e in sorted(data.items(), key=lambda item: item[1].get("created_at", 0))
        ]

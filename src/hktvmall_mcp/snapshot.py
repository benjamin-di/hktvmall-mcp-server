"""Session files (schema 1): parse and validate them; read an external snapshot without ever writing it."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_SNAPSHOT = 512 * 1024
MAX_COOKIES = 512
FUTURE_TOLERANCE = 5 * 60
COOKIE_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,256}\Z")
DOMAIN = re.compile(r"\.?[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\Z")
TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)\Z")


class SnapshotError(Exception):
    """A fixed classification, never text from the authority file."""

    def __init__(self, reason: str = "invalid") -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Snapshot:
    generation: int
    email: str
    user_agent: str
    verified_at: float
    cookies: list[dict[str, Any]]


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise SnapshotError()
        result[key] = value
    return result


def _printable(value: Any, maximum: int, *, empty: bool = False) -> bool:
    return (
        isinstance(value, str)
        and (empty or bool(value))
        and len(value) <= maximum
        and all(32 <= ord(c) <= 126 for c in value)
    )


def parse_snapshot(raw: bytes, expected_email: str, now: float) -> Snapshot:
    """Strict validation: an invalid cookie invalidates the whole snapshot, rather than dropping it."""
    if len(raw) > MAX_SNAPSHOT:
        raise SnapshotError()
    try:
        data = json.loads(raw, object_pairs_hook=_object)
    except (ValueError, UnicodeError, RecursionError):
        raise SnapshotError() from None
    if not isinstance(data, dict) or type(data.get("schema")) is not int or data["schema"] != 1:
        raise SnapshotError()
    generation = data.get("generation")
    email = data.get("email")
    ua = data.get("user_agent")
    verified = data.get("verified_at")
    if (
        data.get("target") != "hktvmall"
        or type(generation) is not int
        or not 1 <= generation <= 2**63 - 1
        or not isinstance(email, str)
        or email.casefold() != expected_email.casefold()
        or not _printable(ua, 1024)
        or not isinstance(verified, str)
        or not TIMESTAMP.fullmatch(verified)
    ):
        raise SnapshotError()
    try:
        verified_at = datetime.fromisoformat(verified).astimezone(UTC).timestamp()
    except (ValueError, OverflowError):
        raise SnapshotError() from None
    if verified_at <= 0 or verified_at > now + FUTURE_TOLERANCE:
        raise SnapshotError()
    cookies = data.get("cookies")
    if not isinstance(cookies, list) or not 1 <= len(cookies) <= MAX_COOKIES:
        raise SnapshotError()
    normalised = []
    seen = set()
    has_session = False
    for cookie in cookies:
        if not isinstance(cookie, dict):
            raise SnapshotError()
        name, value, domain, path = (cookie.get(key) for key in ("name", "value", "domain", "path"))
        secure, expires = cookie.get("secure"), cookie.get("expires")
        if (
            not isinstance(name, str)
            or not COOKIE_NAME.fullmatch(name)
            or not _printable(value, 16 * 1024, empty=True)
            or ";" in value
            or not isinstance(domain, str)
            or not DOMAIN.fullmatch(domain.lower())
            or any(
                not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                for label in domain.lstrip(".").split(".")
            )
            or not (domain.lstrip(".").lower() == "hktvmall.com" or domain.lower().endswith(".hktvmall.com"))
            or not _printable(path, 2048)
            or not path.startswith("/")
            or ";" in path
            or type(secure) is not bool
            or (expires is not None and (type(expires) is not int or not 0 <= expires <= 253_402_300_799))
            or "expires" not in cookie
        ):
            raise SnapshotError()
        domain = domain.lower()
        key = (domain, path, name)
        if key in seen:
            raise SnapshotError()
        seen.add(key)
        if (
            name == "JSESSIONID"
            and value
            and path == "/"
            and domain.lstrip(".") in ("hktvmall.com", "www.hktvmall.com")
            and (expires is None or expires > now)
        ):
            has_session = True
        normalised.append(
            {"name": name, "value": value, "domain": domain, "path": path, "secure": secure, "expires": expires}
        )
    if not has_session:
        raise SnapshotError()
    return Snapshot(generation, email.casefold(), ua, verified_at, normalised)


def read_snapshot(path: Path, expected_email: str, now: float) -> Snapshot:
    """Open every component without following symlinks, then read a bounded regular file.

    The publisher replaces the file atomically. Directory descriptors keep a concurrent directory rename
    from redirecting the read, and O_NONBLOCK prevents a substituted FIFO from hanging the server.
    """
    if not path.is_absolute() or ".." in path.parts:
        raise SnapshotError()
    if os.open not in os.supports_dir_fd:  # Windows: no directory descriptors; refuse symlinks on the path instead
        return parse_snapshot(_read_plain(path), expected_email, now)
    directory = None
    fd = None
    try:
        directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SNAPSHOT:
            raise SnapshotError()
        with os.fdopen(fd, "rb") as stream:
            fd = None
            raw = stream.read(MAX_SNAPSHOT + 1)
    except FileNotFoundError:
        raise SnapshotError("missing") from None
    except OSError:
        raise SnapshotError("unavailable") from None
    finally:
        if fd is not None:
            os.close(fd)
        if directory is not None:
            os.close(directory)
    return parse_snapshot(raw, expected_email, now)


def _read_plain(path: Path) -> bytes:
    try:
        for part in [path, *path.parents]:
            if part.is_symlink():
                raise SnapshotError()
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SNAPSHOT:
            raise SnapshotError()
        return path.read_bytes()[: MAX_SNAPSHOT + 1]
    except FileNotFoundError:
        raise SnapshotError("missing") from None
    except OSError:
        raise SnapshotError("unavailable") from None

"""HKTVmall's single-file snapshot, committed with one private atomic rename.

An export the browser cannot complete is ``VerifyFailed(EXPORT_INCOMPLETE)``;
``SnapshotError`` stays for metadata and the published file's own state.
"""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from signin_window.state import PrivateStateError, atomic_private_write

from .outcomes import VerifyFailed, VerifyFailure


class SnapshotError(ValueError):
    """Snapshot authority could not be verified or published."""


def format_utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def prepare_export_dir(path: Path) -> None:
    if path.is_symlink():
        raise SnapshotError("snapshot directory cannot be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


if TYPE_CHECKING:
    from .keeper import TargetBrowser

SNAPSHOT_FILE = "snapshot.json"
MAX_BYTES = 512 * 1024
MAX_COOKIES = 512
_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,256}\Z")
_TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)\Z")


def _printable(value: object, maximum: int, *, empty: bool = False) -> bool:
    return (
        isinstance(value, str)
        and (empty or bool(value))
        and len(value) <= maximum
        and all(32 <= ord(char) <= 126 for char in value)
    )


def _incomplete() -> VerifyFailed:
    return VerifyFailed(VerifyFailure.EXPORT_INCOMPLETE)


def _domain_ok(domain: object) -> bool:
    if not isinstance(domain, str) or not domain or len(domain) > 253:
        return False
    labels = domain.split(".")
    return (
        domain == domain.lower()
        and (domain == "hktvmall.com" or domain.endswith(".hktvmall.com"))
        and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
    )


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _published(root: Path) -> dict:
    path = root / SNAPSHOT_FILE
    if root.is_symlink() or path.is_symlink():
        raise SnapshotError("HKTVmall snapshot is not a regular file")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise SnapshotError("HKTVmall snapshot is not a regular file")
            raw = stream.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise SnapshotError("HKTVmall snapshot is unreadable") from error
    try:
        if len(raw) > MAX_BYTES:
            raise ValueError("oversized")
        data = json.loads(raw, object_pairs_hook=_object)
        generation = data["generation"]
        if (
            type(data.get("schema")) is not int
            or data["schema"] != 1
            or data.get("target") != "hktvmall"
            or type(generation) is not int
            or not 1 <= generation <= 2**63 - 1
        ):
            raise ValueError("invalid generation")
        return data
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError) as error:
        raise SnapshotError("HKTVmall snapshot is malformed") from error


def published_generation(root: Path) -> int:
    return _published(root).get("generation", 0)


def publish(
    root: Path,
    browser: TargetBrowser,
    generation: int,
    email: str,
    verified_at: datetime,
) -> None:
    if (
        type(generation) is not int
        or not 1 <= generation <= 2**63 - 1
        or not _printable(email, 320)
        or "@" not in email
        or any(char.isspace() for char in email)
        or not isinstance(verified_at, datetime)
        or verified_at.tzinfo is None
        or verified_at.timestamp() <= 0
    ):
        raise SnapshotError("HKTVmall snapshot metadata is invalid")
    previous = _published(root)
    if previous:
        previous_time = previous.get("verified_at")
        if not isinstance(previous_time, str) or not _TIMESTAMP.fullmatch(previous_time):
            raise SnapshotError("HKTVmall prior snapshot timestamp is malformed")
        try:
            old_time = datetime.fromisoformat(previous_time)
        except ValueError as error:
            raise SnapshotError("HKTVmall prior snapshot timestamp is malformed") from error
        if generation <= previous["generation"] or verified_at < old_time:
            raise SnapshotError("HKTVmall snapshot generation or time regressed")
    user_agent = browser.user_agent()
    if not _printable(user_agent, 1024):
        raise _incomplete()
    cookies = []
    seen = set()
    has_session = False
    for cookie in browser.export_cookies():
        if not (
            isinstance(cookie.name, str)
            and _NAME.fullmatch(cookie.name)
            and _printable(cookie.value, 16 * 1024, empty=True)
            and ";" not in cookie.value
        ):
            # A cookie no Cookie header can carry is left out rather than
            # failing the publish, unless it is the required session.
            # Neither its name nor its value is ever reported.
            if cookie.name == "JSESSIONID":
                raise _incomplete()
            continue
        if (
            not _domain_ok(cookie.domain)
            or not _printable(cookie.path, 2048)
            or not cookie.path.startswith("/")
            or ";" in cookie.path
            or type(cookie.secure) is not bool
            or type(cookie.host_only) is not bool
            or type(cookie.expires) is not int
            or not 0 <= cookie.expires <= 253_402_300_799
        ):
            raise _incomplete()
        key = (cookie.netscape_domain, cookie.path, cookie.name)
        if key in seen:
            raise _incomplete()
        seen.add(key)
        if (
            cookie.name == "JSESSIONID"
            and cookie.value
            and cookie.domain in {"hktvmall.com", "www.hktvmall.com"}
            and cookie.path == "/"
            and (cookie.expires == 0 or cookie.expires > verified_at.timestamp())
        ):
            has_session = True
        cookies.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.netscape_domain,
                "path": cookie.path,
                "secure": cookie.secure,
                "expires": cookie.expires or None,
            }
        )
    if not has_session or len(cookies) > MAX_COOKIES:
        raise _incomplete()
    payload = json.dumps(
        {
            "schema": 1,
            "target": "hktvmall",
            "generation": generation,
            "email": email,
            "user_agent": user_agent,
            "verified_at": format_utc(verified_at),
            "cookies": cookies,
        },
        separators=(",", ":"),
    ).encode()
    if len(payload) > MAX_BYTES:
        raise _incomplete()
    try:
        atomic_private_write(root / SNAPSHOT_FILE, payload)
    except PrivateStateError as error:
        raise SnapshotError("HKTVmall snapshot publication failed") from error

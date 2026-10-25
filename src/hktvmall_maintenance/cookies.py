"""Allowlisted portable cookies for a verified site snapshot."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

HeaderTemplate = Any


@dataclass(frozen=True, slots=True)
class ExportCookie:
    domain: str
    host_only: bool
    path: str
    secure: bool
    expires: int
    name: str
    value: str

    @property
    def netscape_domain(self) -> str:
        return self.domain if self.host_only else f".{self.domain}"


def select_export_cookies(
    cookies: Iterable[Mapping[str, object]],
    *,
    hosts: frozenset[str],
    names: frozenset[str],
) -> list[ExportCookie]:
    """Filter browser-context cookies by exact host and name allowlists.

    Playwright reports a host-only cookie with a bare domain and a domain
    cookie with a leading dot; that is the only host-only signal it exposes.
    """

    selected: dict[tuple[str, bool, str, str], ExportCookie] = {}
    for raw in cookies:
        name = raw.get("name")
        value = raw.get("value")
        domain = raw.get("domain")
        if not isinstance(name, str) or name not in names:
            continue
        if not isinstance(value, str) or not isinstance(domain, str):
            continue
        host_only = not domain.startswith(".")
        bare = domain.lstrip(".").lower()
        if bare not in hosts:
            continue
        path = raw.get("path")
        expires = raw.get("expires")
        cookie = ExportCookie(
            domain=bare,
            host_only=host_only,
            path=path if isinstance(path, str) and path.startswith("/") else "/",
            secure=bool(raw.get("secure", False)),
            expires=(
                int(expires)
                if isinstance(expires, int | float) and not isinstance(expires, bool) and expires > 0
                else 0
            ),
            name=name,
            value=value,
        )
        selected[(cookie.domain, cookie.host_only, cookie.path, cookie.name)] = cookie
    return sorted(
        selected.values(),
        key=lambda item: (item.domain, item.host_only, item.path, item.name),
    )

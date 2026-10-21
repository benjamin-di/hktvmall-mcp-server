"""Product pages and photos, taken from HKTVmall's own fields: a link is never built from an id.

Search hits carry ``urlEn`` (``main/<store>/s/<store code>/<categories>/<name>/p/<id>``) and basket lines
``product.url`` (the same path with a leading slash). That is the canonical page HKTVmall itself links to, so it
needs no redirect: the short ``/hktv/en/p/<id>`` form only works as a redirect on the web, and HKTVmall's iPhone
app, which claims every hktvmall.com link (apple-app-site-association ``"/": "*"``), receives it as tapped.

Photos are on HKTVmall's image CDNs (IMAGE_HOSTS: 2,293 of 2,295 gallery photos in 500 hits, 2026-10-21; the
other host, hktv-img.hkmpcl.com.hk, answered 404). The server never fetches them; the person's browser does, inside
the product cards, whose content security policy lists exactly these hosts.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

PAGE_BASE = "https://www.hktvmall.com/hktv/en/"
SHORT_PAGE = PAGE_BASE + "p/{id}"  # redirects to the canonical page; only when HKTVmall gave none
IMAGE_HOSTS = ("images.hktv-img.com", "images.hktvmall.com", "cdn-media.hktvmall.com", "cdn-mms.hktvmall.com")
_PAGE = re.compile(r"main/[A-Za-z0-9%._~!$&'()*+,;=:@/-]+/p/([A-Za-z0-9_-]+)")
_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")


def product_url(code: Any, path: Any) -> str | None:
    """HKTVmall's English page for ``code`` from its own path; the short redirect when the path is unusable."""
    if not isinstance(code, str) or not _ID.fullmatch(code):
        return None
    page = path.lstrip("/") if isinstance(path, str) else ""
    m = _PAGE.fullmatch(page)
    if m and m.group(1) == code and "//" not in page and "/../" not in page and "/./" not in page:
        return PAGE_BASE + page
    return SHORT_PAGE.format(id=code)


def image_url(link: Any) -> str | None:
    """A product photo on HKTVmall's CDNs, else nothing (never a host the cards could not load anyway)."""
    if not isinstance(link, str) or len(link) > 500:
        return None
    try:
        parts = urllib.parse.urlsplit(link)
    except ValueError:
        return None
    host = parts.hostname or ""
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.port is not None
        or host not in IMAGE_HOSTS
        or any(c in link for c in "\"'<>\\ ")
    ):
        return None
    return link


MAX_PHOTOS = 8
_FULL_SIZE = re.compile(r"_1200(\.(?:jpe?g|png|webp))$")


def photos(gallery: Any, fallback: str | None = None) -> list[str]:
    """A product's photos for its card, the primary one first, at 515 px: HKTVmall serves every gallery photo at
    65, 96, 300, 515 and 1200 px (all four CDNs, 2026-10-21); 515 is sharp on a phone card at 30-65 KB a JPEG."""
    rows = [g for g in gallery if isinstance(g, dict)] if isinstance(gallery, list) else []
    rows.sort(key=lambda g: g.get("imageType") != "PRIMARY")  # stable: the merchant's order after the primary
    out: list[str] = []
    for row in rows:
        url = image_url(row.get("url"))
        if url and (url := _FULL_SIZE.sub(r"_515\1", url)) not in out:
            out.append(url)
        if len(out) == MAX_PHOTOS:
            break
    return out or ([fallback] if fallback else [])

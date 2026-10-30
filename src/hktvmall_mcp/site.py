"""HKTVmall over plain HTTP: the only module that does network I/O.

Behaviour observed on the live site (docs/hktvmall-site.md):
* search: keyword-search-server takes many queries in one POST; ``attributesToRetrieve`` shrinks it ~14x;
  a browser User-Agent is required.
* ``access_token`` is minted by a plain GET of /hktv/en/cart and cleared by every legacy (Hybris) cart call.
* adding exists only as legacy POST /hktv/en/cart/add; updating is update_item_in_cart, never with quantity 0
  (that leaves a zombie line and every basket read then returns a 403 "maintenance" page); removal is
  remove_item_from_cart, which also repairs a zombie line.
* write responses are not evidence: callers read the basket back.
* order history (read-only): GET /hktv/en/ajax/getOrderList is JSON, five orders a page; an order's items are
  only in its page, /hktv/en/my-account/order/<number>, as ``var orderWithBatches = {...}`` (~2 MB of HTML;
  the same object carries the address and payment, which are never read).
* search takes a price range as ``range: {"sellingPrice": "-19.9"}`` (inclusive), as HKTVmall's own client does.
* delivery slots (read-only): the checkout reads its first address's ``zoneId`` from get_checkout_addresses and
  asks get_available_delivery_time_slots for that zone; only the zone is read from the address list.
* checkout warnings are plain strings, merged by the checkout page from the basket and get_checkout_error_message.
* reviews are public on ucapi.comms.hktvmall.com, keyed by the product's ``baseProduct`` (from search; usually the id
  without "_S_", but variants share another one); no session is sent.
"""

from __future__ import annotations

import http.client
import http.cookiejar
import json
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

from .errors import HktvError
from .session import SessionStore
from .state import StateStore

BASE = "https://www.hktvmall.com"
KWS = "https://keyword-search-server.hktvmall.com/api/search"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
REVIEWS = "https://ucapi.comms.hktvmall.com/hktvmall/products/"
HOSTS = frozenset({"www.hktvmall.com", "keyword-search-server.hktvmall.com", "ucapi.comms.hktvmall.com"})
# A 429 from the shop or its search pauses everything (same account and IP); the public review service only
# fails its own call, so throttled reviews never block the basket.
PAUSE_HOSTS = frozenset({"www.hktvmall.com", "keyword-search-server.hktvmall.com"})
PROBE_QUERY = "milk"  # an empty search answer is believed only if this also has hits
PATHS = {  # every HKTVmall path this server can reach; tests/test_safety.py pins the set
    "home": "/hktv/en/",
    "cart_page": "/hktv/en/cart",
    "legacy_add": "/hktv/en/cart/add",
    "basket": "/en/hktvfrontend/v1/cart/express/checkout",
    "update": "/en/hktvfrontend/v1/cart/express/update_item_in_cart",
    "remove": "/en/hktvfrontend/v1/cart/express/remove_item_from_cart",
    "order_list": "/hktv/en/ajax/getOrderList",
    "order_page": "/hktv/en/my-account/order/",  # + an order number from order_list
    "addresses": "/en/hktvfrontend/v1/address/express/get_checkout_addresses",  # read for zoneId only
    "delivery_slots": "/en/hktvfrontend/v1/address/express/get_available_delivery_time_slots",
    "checkout_errors": "/en/hktvfrontend/v1/cart/express/get_checkout_error_message",
}
REVIEW_PATHS = ("reviews/", "reviews/stat")  # under REVIEWS + a base product code
ZONE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")
NO_SLOTS = "_noSlotsForEmptyBasket"  # marks delivery_slots' empty-basket answer
PRODUCT_ID = re.compile(r"^[A-Z][0-9]{7}_S_[A-Za-z0-9_-]{1,70}$")  # 500 of 500 sampled ids; 130 carry "-"
BASE_PRODUCT = re.compile(r"^[A-Z][0-9]{7}[A-Za-z0-9_-]{1,80}$")
ORDER_NUMBER = re.compile(r"^[A-Z]{0,2}[0-9]{6,20}$")
ORDER_PAGE_SIZE = 5
FIELDS = [
    "code",
    "nameEn",
    "nameZh",
    "brandEn",
    "packingSpecEn",
    "sellingPrice",
    "priceList",
    "stock",
    "storeNameEn",
    "storeCode",
    "averageRating",
    "numberOfReviews",
    "deliveryLabelCode",
    "promotionTextEn",
    "mainCatNameEn",
    "storeClosedStatus",
    "deliveryLabelScheduleCode",
    "summaryEn",
    "countryOfOriginEn",
    "imageLink",
    "urlEn",  # the canonical product page (links.product_url)
]
# Looking products up by id (details, cards, basket checks, reviews) also reads the review key and the photo gallery;
# search hits do not need them and a gallery is ~1 KB per hit.
LOOKUP_FIELDS = [*FIELDS, "baseProduct", "gallery"]
MIN_INTERVAL, JITTER, QUEUE_WAIT = 0.35, 0.15, 30.0  # at most ~2 requests a second, one at a time
# A call waits this long for the account's previous call: an order-history page reads up to ten order pages
# (~20 s), and agents send several at once (with a 30 s wait, most of seven parallel calls failed busy).
ACCOUNT_WAIT = 90.0
MAX_BODY = 8 * 1024 * 1024
TIMEOUT = 30


def cents(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int((Decimal(str(value)) * 100).to_integral_value())
    except (ArithmeticError, ValueError):  # ValueError: NaN
        return None


class AllowlistedRedirects(urllib.request.HTTPRedirectHandler):
    """urllib follows redirects on its own; refuse any hop that leaves the allowlisted hosts."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        parts = urllib.parse.urlsplit(newurl)
        if parts.scheme != "https" or parts.hostname not in HOSTS:  # never hand cookies to plain http
            raise HktvError("schema_changed", log_detail="redirect off HKTVmall https")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def opener_for(jar: http.cookiejar.CookieJar) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), AllowlistedRedirects())


class Transport:
    """One HKTVmall request at a time, paced, budgeted, paused on pushback."""

    def __init__(
        self,
        state: StateStore,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        queue_wait: float = QUEUE_WAIT,
    ) -> None:
        self.state = state
        self._lock = threading.Lock()
        self._sleep, self._monotonic, self._queue_wait = sleep, monotonic, queue_wait
        self._last = -1e9

    def send(
        self,
        opener: urllib.request.OpenerDirector,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        if urllib.parse.urlsplit(url).hostname not in HOSTS:
            raise HktvError("internal_error", log_detail="host not allowlisted")
        if not self._lock.acquire(timeout=self._queue_wait):
            raise HktvError("busy")
        try:
            self.state.check_pause()
            self.state.take()
            wait = MIN_INTERVAL + random.random() * JITTER - (self._monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            req = urllib.request.Request(url, data=body, method=method, headers={"User-Agent": UA, **(headers or {})})
            try:
                with opener.open(req, timeout=TIMEOUT) as resp:
                    status, raw = resp.status, resp.read(MAX_BODY + 1)
            except urllib.error.HTTPError as err:
                status, raw = err.code, err.read(MAX_BODY + 1)
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
                raise HktvError("upstream_error", log_detail="network") from None
            finally:
                self._last = self._monotonic()
        finally:
            self._lock.release()
        if len(raw) > MAX_BODY:
            raise HktvError("schema_changed", log_detail="body too large")
        if status == 429:
            host = urllib.parse.urlsplit(url).hostname
            if host not in PAUSE_HOSTS:  # throttled reviews: only this call fails, and there is no pause to wait for
                raise HktvError("upstream_error", log_detail=f"429 {host}")
            self.state.pause("429")
            raise HktvError("blocked", log_detail=f"429 {host}")
        return status, raw.decode("utf-8", "replace")


class Site:
    def __init__(self, transport: Transport, session: SessionStore, *, account_wait: float = ACCOUNT_WAIT) -> None:
        self.transport = transport
        self._account_wait = account_wait
        self.session = session
        self._public_jar = http.cookiejar.CookieJar()
        self._public = opener_for(self._public_jar)
        self.search_key = ""
        self._account_lock = threading.Lock()

    # -- public (no sign-in) --------------------------------------------------------------

    def _nav(self, opener, key: str, user_agent: str = UA) -> str:
        status, text = self.transport.send(
            opener,
            "GET",
            BASE + PATHS[key],
            headers={
                "Accept": "text/html,*/*",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "User-Agent": user_agent,
            },
        )
        if status == 403 and key == "home":
            self.transport.state.pause("home 403")
            raise HktvError("blocked", log_detail="home 403")
        if status != 200:
            raise HktvError("upstream_error", log_detail=f"{key} {status}")
        return text

    def _ensure_key(self) -> None:
        if self.search_key:
            return
        home = self._nav(self._public, "home")
        key = re.search(r"keywordSearchServerApiKey'\s*:\s*\"([^\"]+)\"", home)
        if not key:
            raise HktvError("schema_changed", log_detail="search key missing from home page")
        self.search_key = key.group(1)

    def search(self, queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self._ensure_key()
        reqs = []
        for q in queries:
            flt: dict[str, Any] = {}
            if q.get("zone") == "supermarket":
                flt["mainCatNameEn"] = ["Supermarket"]
            if q.get("brand"):
                flt["brandEn"] = [q["brand"]]
            if q.get("codes"):
                flt["code"] = q["codes"]
            req: dict[str, Any] = {
                "indexName": "hktvProduct",
                "keyword": q.get("q", ""),
                "attributesToRetrieve": q.get("fields", FIELDS),
                "filter": flt,
                "page": {"pageNumber": 0, "pageSize": q.get("limit", 3)},
            }
            if q.get("max_price_cents"):
                req["range"] = {"sellingPrice": f"-{Decimal(q['max_price_cents']) / 100}"}
            reqs.append(req)
        status, text = self._post_search(reqs)
        if status in (401, 403):
            # The page-embedded key may have rotated: re-read it and retry once before calling it pushback.
            self.search_key = ""
            self._ensure_key()
            status, text = self._post_search(reqs)
        try:
            results = json.loads(text)["results"]
            return [
                {"total": r.get("totalValue", 0), "hits": [h["source"] for h in r.get("hits", [])]} for r in results
            ]
        except (ValueError, KeyError, TypeError, AttributeError):
            if status == 403:
                self.transport.state.pause("search 403")
                raise HktvError("blocked", log_detail="search 403") from None
            raise HktvError("schema_changed", log_detail=f"search {status}") from None

    def _post_search(self, reqs: list[dict[str, Any]]) -> tuple[int, str]:
        return self.transport.send(
            self._public,
            "POST",
            KWS,
            json.dumps({"requests": reqs}).encode(),
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"ApiKey {self.search_key}",
                "Origin": BASE,
                "Referer": BASE + "/",
            },
        )

    def search_works(self) -> bool:
        """Whether search answers at all: HKTVmall's search has returned success with no hits for every query
        for minutes at a time (2026-10-19), so an all-empty answer is checked once before it is believed."""
        return bool(self.search([{"q": PROBE_QUERY, "zone": "supermarket", "limit": 1}])[0]["total"])

    @staticmethod
    def review_key(product_id: str, hit: dict[str, Any] | None) -> str:
        """The key HKTVmall files a product's reviews under: search's ``baseProduct`` (variants share one, e.g.
        H9990001_S_RICE_1KG -> H999000110_DAYS), else the id without "_S_" (a product search
        does not list right now)."""
        if not PRODUCT_ID.fullmatch(product_id):
            raise HktvError("invalid_argument", "id is not an HKTVmall product id")
        base = (hit or {}).get("baseProduct")
        return base if isinstance(base, str) and BASE_PRODUCT.fullmatch(base) else product_id.replace("_S_", "")

    def reviews(self, base: str, page_size: int, rating: int | None) -> dict[str, Any]:
        """Public reviews under one review key (review_key): the rating breakdown and the latest comments (raw rows)."""
        if not BASE_PRODUCT.fullmatch(base):
            raise HktvError("invalid_argument", "id is not an HKTVmall product id")
        headers = {"Accept": "application/json", "Origin": BASE, "Referer": BASE + "/"}
        # Pages start at 0, newest first: HKTVmall's own client calls requestReviewsAction(productCode, 0, ...).
        query = {"lang": "en", "current_page": 0, "page_size": page_size, **({"rating": rating} if rating else {})}
        out: dict[str, Any] = {}
        for key, path, params in (
            ("stat", REVIEW_PATHS[1], {"count_by_ratings": "true"}),
            ("page", REVIEW_PATHS[0], query),
        ):
            url = REVIEWS + base + "/" + path + "?" + urllib.parse.urlencode(params)
            status, text = self.transport.send(self._public, "GET", url, headers=headers)
            if status == 204 or not text.strip():
                out[key] = {}  # no reviews
                continue
            if status != 200:
                raise HktvError("upstream_error", log_detail=f"reviews {status}")
            try:
                out[key] = json.loads(text)
            except ValueError:
                raise HktvError("schema_changed", log_detail="reviews not json") from None
        return out

    def lookup(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        if not ids:
            return {}
        res = self.search([{"q": "", "codes": ids, "limit": len(ids), "fields": LOOKUP_FIELDS}])[0]
        return {h["code"]: h for h in res["hits"]}

    # -- the signed-in account --------------------------------------------------------------

    @contextmanager
    def account(self) -> Iterator[Account]:
        """One signed-in client per call; cookies HKTVmall rotates are saved even when the call fails."""
        if not self._account_lock.acquire(timeout=self._account_wait):
            raise HktvError("busy")
        try:
            generation, jar, user_agent = self.session.checkout_client()
            acct = Account(self, jar, generation, user_agent=user_agent or UA)
            try:
                if self.session.needs_verification(generation):
                    acct.verify()
                yield acct
            finally:
                self.session.store_jar(jar, generation)
        finally:
            self._account_lock.release()


class Account:
    def __init__(self, site: Site, jar: http.cookiejar.CookieJar, generation: int = 0, *, user_agent: str = UA) -> None:
        self.site, self.jar, self.generation = site, jar, generation
        self.opener = opener_for(jar)
        self.csrf = ""  # set by verify(): this client has proven the sign-in
        self.user_agent = user_agent
        self.dead = False

    def _guard(self) -> None:
        """Every request of this client passes here. Once HKTVmall said this generation is signed out, nothing
        else may be sent with it: a re-mint would quietly yield a guest token, and a guest basket read back
        would look like the account's (a removal "applied" that never happened)."""
        if self.dead:
            raise HktvError("session_expired", log_detail="generation signed out during this call")

    def _token(self) -> bool:
        return any(c.name == "access_token" and c.value and not c.is_expired() for c in self.jar)

    def verify(self) -> None:
        """Load the home page as the account: CSRF for legacy calls, and proof the sign-in is alive."""
        self._guard()
        home = self.site._nav(self.opener, "home", self.user_agent)
        flag = re.search(r"'loggedin'\s*:\s*(true|false)", home)
        csrf = re.search(r'CSRFToken = "([^"]+)"', home)
        if not flag or not csrf:
            raise HktvError("schema_changed", log_detail="home page config missing")
        if flag.group(1) != "true":
            self.dead = True
            self.site.session.mark("dead", self.generation)
            raise HktvError("session_expired", log_detail="loggedin false")
        expected = getattr(self.site.session, "account_email", None)
        if expected is not None:
            # A session this server owns must still be its account's (a snapshot's publisher checks its own).
            email = re.search(r"'realContactEmail'\s*:\s*\"([^\"]*)\"", home)
            if not email:
                raise HktvError("schema_changed", log_detail="home page contact email missing")
            if email.group(1).strip().casefold() != expected:
                self.dead = True
                self.site.session.mark("dead", self.generation)
                raise HktvError("account_mismatch", log_detail="contact email differs")
        self.site.session.mark("live", self.generation)
        self.csrf = csrf.group(1)

    def remint(self) -> None:
        self._guard()
        self.site._nav(self.opener, "cart_page", self.user_agent)

    def _drop_token(self) -> None:
        for c in [c for c in self.jar if c.name == "access_token"]:
            self.jar.clear(c.domain, c.path, c.name)

    def _send_json(
        self, method: str, key: str, body: bytes | None, query: dict[str, Any] | None = None
    ) -> tuple[int, Any]:
        self._guard()
        status, text = self.site.transport.send(
            self.opener,
            method,
            BASE + PATHS[key] + ("?" + urllib.parse.urlencode(query) if query else ""),
            body,
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": BASE,
                "Referer": BASE + PATHS["cart_page"],
                "User-Agent": self.user_agent,
            },
        )
        try:
            return status, json.loads(text)
        except ValueError:
            return status, None  # HTML: maintenance page, WAF page or login redirect

    def _json(
        self, method: str, key: str, payload: dict | None = None, query: dict[str, Any] | None = None
    ) -> tuple[int, Any]:
        """Every authenticated frontend call. A 401 usually means the access_token lapsed (it lives ~30 days),
        but re-minting succeeds for a guest too, so the sign-in is proven on the home page before the retry:
        a retry that succeeded is never taken as proof that this is still the account's basket."""
        if not self._token():
            self.remint()
            if not self.csrf:
                self.verify()  # a re-mint works for a guest too: prove the sign-in once per client
        body = json.dumps(payload).encode() if payload is not None else None
        status, data = self._send_json(method, key, body, query)
        if status == 401:
            self._drop_token()
            self.remint()
            self.verify()  # raises session_expired (and marks the generation dead) when HKTVmall says so
            status, data = self._send_json(method, key, body, query)
            if status == 401:
                raise HktvError("schema_changed", log_detail=f"{key} 401 while signed in")
        return status, data

    def _unexpected(self, detail: str) -> HktvError:
        """An account page that is not what it should be is first asked whether the sign-in is gone (a login
        page in its place); only a signed-in mismatch is a change of HKTVmall's format."""
        self.verify()
        return HktvError("schema_changed", log_detail=detail)

    def basket(self) -> dict[str, Any]:
        status, data = self._json("GET", "basket")
        if status == 200 and isinstance(data, dict) and data.get("status") == "SUCCESS":
            return data["data"]
        if status == 403 and data is None:
            raise HktvError("basket_unreadable", log_detail="basket 403 html")
        if status >= 500:  # maintenance or a gateway error: retry, like every other account call
            raise HktvError("upstream_error", log_detail=f"basket {status}")
        raise self._unexpected(f"basket {status}")

    def add(self, sku: str, qty: int) -> dict[str, Any]:
        """Legacy add; clears access_token, so the caller re-mints once after all adds. Returns a short summary
        of HKTVmall's reply for the logs: it says success even for items it drops, so it is never the outcome."""
        form = urllib.parse.urlencode(
            {"productCodePost": sku, "qty": str(qty), "pageType": "", "CSRFToken": self.csrf}
        ).encode()
        self._guard()
        status, text = self.site.transport.send(
            self.opener,
            "POST",
            BASE + PATHS["legacy_add"],
            form,
            {
                "X-Requested-With": "XMLHttpRequest",
                "CSRFToken": self.csrf,
                "Referer": BASE + PATHS["home"],
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "User-Agent": self.user_agent,
            },
        )
        try:
            reply = json.loads(text)
        except ValueError:
            return {"http": status, "json": False}
        reply = reply if isinstance(reply, dict) else {}
        return {
            "http": status,
            "status": reply.get("statusCode"),
            "added": reply.get("quantityAdded"),
            "entry": bool(reply.get("orderEntryData")),
            "products": len((reply.get("cartData") or {}).get("products") or []),
        }

    # -- order history (read-only) --------------------------------------------------------

    def _get_page(self, url: str, accept: str) -> str:
        self._guard()
        status, text = self.site.transport.send(
            self.opener,
            "GET",
            url,
            headers={
                "Accept": accept,
                "Referer": BASE + "/hktv/en/my-account/orders",
                "User-Agent": self.user_agent,
            },
        )
        if status != 200:
            raise HktvError("upstream_error", log_detail=f"order page {status}")
        return text

    def _frontend_get(self, key: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        status, data = self._json("GET", key, None, query)
        if status != 200:
            raise HktvError("upstream_error", log_detail=f"{key} {status}")
        if not isinstance(data, dict):
            raise self._unexpected(f"{key} not json")
        return data

    def delivery_zone(self) -> str:
        """The delivery zone of the first address, as HKTVmall's checkout uses; nothing else is read."""
        data = self._frontend_get("addresses").get("data")
        first = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
        zone = str(first.get("zoneId") or "")
        if not ZONE.fullmatch(zone):
            raise HktvError("schema_changed", log_detail="no usable delivery zone")
        return zone

    def delivery_slots(self, zone: str) -> dict[str, Any]:
        """The zone's slots; for an empty basket HKTVmall answers FAIL with only the fees, returned marked
        NO_SLOTS (the caller confirms the basket is empty; any other FAIL is a format change)."""
        data = self._frontend_get("delivery_slots", {"deliveryZoneId": zone})
        raw = data.get("data")
        if not isinstance(raw, dict):
            raise HktvError("schema_changed", log_detail="delivery slots")
        if data.get("status") == "SUCCESS":
            return raw
        if data.get("status") == "FAIL" and "deliveryFee" in raw and "deliveryTimeSlots" not in raw:
            return {**raw, NO_SLOTS: True}
        raise HktvError("schema_changed", log_detail="delivery slots")

    def checkout_errors(self) -> list[str]:
        """The second half of the checkout page's warnings (the first is the basket's own errorMessages)."""
        data = self._frontend_get("checkout_errors").get("data")
        messages = data.get("errorMessages") if isinstance(data, dict) else None
        if isinstance(messages, str):
            messages = [messages]
        return [m for m in messages if isinstance(m, str)] if isinstance(messages, list) else []

    def order_list(self, page: int, processing_page: int = 0) -> dict[str, Any]:
        """One page of completed orders and one of processing orders (each newest first), as the orders page
        loads them; each kind has its own page counter and total."""
        query = urllib.parse.urlencode(
            {
                "lang": "en",
                "completedCurrentPage": page,
                "processingCurrentPage": processing_page,
                "pageSize": ORDER_PAGE_SIZE,
            }
        )
        text = self._get_page(BASE + PATHS["order_list"] + "?" + query, "application/json")
        try:
            data = json.loads(text)
            closed = data["closedPageData"]
            processing = data.get("processingPageData") or {}
            return {
                "completed": list(closed.get("results") or []),
                "processing": list(processing.get("results") or []),
                "total": int((closed.get("pagination") or {}).get("totalNumberOfResults") or 0),
                "processing_total": int((processing.get("pagination") or {}).get("totalNumberOfResults") or 0),
            }
        except (ValueError, KeyError, TypeError, AttributeError):
            raise self._unexpected("order list") from None

    def order_entries(self, number: str) -> list[dict[str, Any]]:
        """The items of one order, from its page; nothing else of the page is kept."""
        if not ORDER_NUMBER.fullmatch(number):
            raise HktvError("schema_changed", log_detail="order number format")
        page = self._get_page(BASE + PATHS["order_page"] + number, "text/html,*/*")
        m = re.search(r"var orderWithBatches\s*=\s*", page)
        if not m:
            raise self._unexpected("order page without orderWithBatches")
        try:
            order, _ = json.JSONDecoder().raw_decode(page, m.end())
            return [e for b in order.get("deliveryBatches") or [] for e in b.get("entries") or []]
        except (ValueError, TypeError, AttributeError):
            raise HktvError("schema_changed", log_detail="order page data") from None

    def set_quantity(self, sku: str, qty: int) -> None:
        if qty < 1:
            raise ValueError("quantity 0 goes through remove()")  # the zombie-line bug
        self._json("POST", "update", {"sku": sku, "quantity": qty})

    def remove(self, sku: str) -> None:
        self._json("POST", "remove", {"sku": sku})

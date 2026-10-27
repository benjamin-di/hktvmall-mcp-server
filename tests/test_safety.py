"""The safety boundary, checked statically: what this server can reach and say.

Nothing can place, pay for or change an order, address, voucher or list (order history is read-only); only
allowlisted HKTVmall hosts and paths exist; only site.py does network I/O; tool errors never carry exception text.
"""

import ast
import re
from pathlib import Path

from hktvmall_mcp import site

SRC = Path(__file__).resolve().parents[1] / "src" / "hktvmall_mcp"
FILES = sorted(SRC.glob("*.py"))
FORBIDDEN = [
    "place_order",
    "change_payment_type",
    "apply_credits",
    "apply_all_credits",
    "apply_voucher",
    "apply_paid_voucher",
    "vouchers/remove",
    "save_new_address",
    "set_default_address",
    "update_delivery_contact_info",
    "update_delivery_timeslot",
    "user/profile",
    "oauth/revoke",
    "add_share_list",
    "add_items_to_my_list",
    "create_custom_list",
    "add_item_to_wish_list",
    "wishlist",
    "j_spring_security_check",
    "two-factor",
    "addToCartWithOrderNumber",  # re-adds a whole order on top of the basket: re-orders go line by line instead
    "amend-delivery",
    "invoice",
    "cancel-order",
    "return-request",
    "reveal_checkout_addresses",  # unmasked addresses
    "review_tokens",  # writing reviews
    "reviewImage",
    "private_message",
    "productReviewForm",
]
NETWORK = {
    "urllib.request",
    "urllib.error",
    "http.client",
    "http.cookiejar",
    "httpx",
    "requests",
    "urllib3",
    "socket",
    "aiohttp",
    "curl_cffi",
}


def test_no_forbidden_endpoint_anywhere():
    for f in FILES:
        text = f.read_text(encoding="utf-8")
        for bad in FORBIDDEN:
            assert bad not in text, f"{bad} in {f.name}"


def test_reachable_hosts_and_paths_are_pinned():
    assert {"www.hktvmall.com", "keyword-search-server.hktvmall.com", "ucapi.comms.hktvmall.com"} == site.HOSTS
    assert site.REVIEW_PATHS == ("reviews/", "reviews/stat")
    assert site.PATHS == {
        "home": "/hktv/en/",
        "cart_page": "/hktv/en/cart",
        "legacy_add": "/hktv/en/cart/add",
        "basket": "/en/hktvfrontend/v1/cart/express/checkout",
        "update": "/en/hktvfrontend/v1/cart/express/update_item_in_cart",
        "remove": "/en/hktvfrontend/v1/cart/express/remove_item_from_cart",
        "order_list": "/hktv/en/ajax/getOrderList",
        "order_page": "/hktv/en/my-account/order/",
        "addresses": "/en/hktvfrontend/v1/address/express/get_checkout_addresses",
        "delivery_slots": "/en/hktvfrontend/v1/address/express/get_available_delivery_time_slots",
        "checkout_errors": "/en/hktvfrontend/v1/cart/express/get_checkout_error_message",
    }
    for f in FILES:
        for host in re.findall(r"https://([a-z0-9.-]+)", f.read_text(encoding="utf-8")):
            assert host in site.HOSTS, f"{host} in {f.name}"


def test_network_io_only_in_site():
    for f in FILES:
        mods = set()
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                mods |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module)
        if f.name not in ("site.py", "session.py", "cli.py"):
            assert not mods & NETWORK, f"{f.name} imports {mods & NETWORK}"
        if f.name == "cli.py":  # `hktvmall-mcp health` asks this server's own /health, never HKTVmall
            assert mods & NETWORK <= {"urllib.request"}
        if f.name == "session.py":  # builds cookie jars, never sends
            assert mods & NETWORK <= {"http.cookiejar"}


def test_the_login_client_reaches_only_the_configured_internal_service():
    """The client is signin-window's (its URLs are the base URL plus fixed paths and validated ids, tested there); the
    base is HKTVMALL_LOGIN_URL, plain http, host:port, checked at startup."""
    from hktvmall_mcp.config import ConfigError, Settings

    assert "from signin_window.client import LoginClient" in (SRC / "connections.py").read_text(encoding="utf-8")
    assert "from signin_window.client import LoginClient" in (SRC / "server.py").read_text(encoding="utf-8")
    base = {"HKTVMALL_SESSION_SNAPSHOT": "/s/x.json", "HKTVMALL_EXPECTED_EMAIL": "a@b.c"}
    for name in ("HKTVMALL_LOGIN_URL", "HKTVMALL_DEFAULT_LOGIN_URL"):
        for bad in ("https://evil.example", "http://hktvmall-mcp-login:8080/x", "http://user@host:80", "file:///x"):
            try:
                Settings.from_env({**base, name: bad})
            except ConfigError:
                continue
            raise AssertionError(f"{bad} accepted")


def _calls(path: Path, name: str) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(n, ast.Call) and (getattr(n.func, "attr", None) == name or getattr(n.func, "id", None) == name)
    ]


def test_only_get_and_post():
    verbs = {c.args[1].value for c in _calls(SRC / "site.py", "send") if isinstance(c.args[1], ast.Constant)}
    assert verbs == {"GET", "POST"}


def test_tool_errors_never_carry_exception_text():
    server = SRC / "server.py"
    raises = _calls(server, "ToolError")
    assert raises
    for call in raises:
        arg = call.args[0]
        assert isinstance(arg, ast.Call) and getattr(arg.func, "id", None) == "_json", ast.unparse(call)
        payload = arg.args[0]
        assert isinstance(payload, ast.Dict) and [k.value for k in payload.keys] == ["error"], ast.unparse(call)
    assert not re.search(r"str\(exc\)|\{exc\}|repr\(exc\)", server.read_text(encoding="utf-8"))


def test_order_history_is_read_only_and_never_reads_address_or_payment():
    text = (SRC / "site.py").read_text(encoding="utf-8")
    for key in ("order_list", "order_page"):
        uses = [line for line in text.splitlines() if f'PATHS["{key}"]' in line]
        assert uses and all("_get_page(" in line for line in uses), uses  # GET only
    orders_src = (SRC / "orders.py").read_text(encoding="utf-8") + (SRC / "site.py").read_text(encoding="utf-8")
    for field in ('"address"', '"payment"', '"voucher"', "deliveryAddress"):
        assert field not in orders_src, field


def test_new_read_paths_are_get_only_and_reviews_never_carry_the_session():
    text = (SRC / "site.py").read_text(encoding="utf-8")
    for key in ("addresses", "delivery_slots", "checkout_errors"):
        assert f'_frontend_get("{key}"' in text and f'PATHS["{key}"]' not in text.replace("PATHS[key]", "")
    review_calls = [line for line in text.splitlines() if "REVIEWS +" in line]
    assert review_calls and all(
        "self._public" in text.split("def reviews", 1)[1].split("def lookup", 1)[0] for _ in review_calls
    )
    assert "self.opener" not in text.split("def reviews", 1)[1].split("def lookup", 1)[0]


def test_reviews_copy_nothing_about_reviewers():
    views = (SRC / "views.py").read_text(encoding="utf-8")
    for field in ('"user"', '"_id"', "nickname"):
        assert field not in views, field


def test_the_product_cards_load_hktvmall_photos_and_connect_nowhere():
    """hktv_show_products' page runs in the person's chat client: photos from HKTVmall's CDNs, no network calls, no
    HTML from data (titles come from merchants), links only to HKTVmall's pages and only through the host."""
    from hktvmall_mcp import links, server

    hosts = ["images.hktv-img.com", "images.hktvmall.com", "cdn-media.hktvmall.com", "cdn-mms.hktvmall.com"]
    assert list(links.IMAGE_HOSTS) == hosts and not set(hosts) & site.HOSTS  # shown to people, never fetched here
    assert {"resourceDomains": [f"https://{h}" for h in hosts]} == server.CARDS_CSP  # no connectDomains: none
    page = (SRC / "products.html").read_text(encoding="utf-8")
    network = ("fetch(", "XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon", "<script src", "<link", "@import")
    markup = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function")
    for api in network + markup:
        assert api not in page, api
    assert "const IMAGE_HOSTS = " + str(hosts).replace("'", '"') + ";" in page
    assert 'const PAGE_PREFIX = "https://www.hktvmall.com/hktv/";' in page
    assert "event.source !== window.parent" in page

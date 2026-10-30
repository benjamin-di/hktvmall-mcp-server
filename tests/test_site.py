"""Site/Account behaviour with a scripted transport: token re-mint on 401, sign-in checks."""

import http.cookiejar
import json

import pytest

from hktvmall_mcp.errors import HktvError
from hktvmall_mcp.site import NO_SLOTS, PATHS, Account, Site

HOME_IN = "<script>hktvCommon.init({ 'loggedin' : true }); var CSRFToken = \"c-1\";</script>"
HOME_OUT = HOME_IN.replace("true", "false")
OK_BASKET = json.dumps({"status": "SUCCESS", "data": {"cartSubTotal": {"value": "0"}}})


class ScriptedTransport:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def send(self, opener, method, url, body=None, headers=None):
        path = url.split("hktvmall.com", 1)[1]
        self.calls.append(path)
        queue = self.answers[path]
        return queue.pop(0) if len(queue) > 1 else queue[0]


class Session:
    def __init__(self):
        self.marks = []

    generation = 1

    def mark(self, state, generation):
        if generation == self.generation:
            self.marks.append(state)


def account(answers):
    site = Site.__new__(Site)
    site.transport, site.session = ScriptedTransport(answers), Session()
    jar = http.cookiejar.CookieJar()
    jar.set_cookie(
        http.cookiejar.Cookie(
            0,
            "access_token",
            "old",
            None,
            False,
            "www.hktvmall.com",
            False,
            False,
            "/",
            True,
            True,
            None,
            True,
            None,
            None,
            {},
        )
    )
    return Account(site, jar, 1), site


def test_a_401_is_a_lapsed_token_first_but_the_sign_in_is_proven_before_the_retry():
    acct, site = account(
        {
            PATHS["basket"]: [(401, "{}"), (200, OK_BASKET)],
            PATHS["cart_page"]: [(200, "<html>")],
            PATHS["home"]: [(200, HOME_IN)],
        }
    )
    assert acct.basket() == {"cartSubTotal": {"value": "0"}}
    assert site.transport.calls == [PATHS["basket"], PATHS["cart_page"], PATHS["home"], PATHS["basket"]]
    assert not [c for c in acct.jar if c.name == "access_token"]  # the lapsed token was dropped
    assert site.session.marks == ["live"]


def test_a_401_after_the_sign_in_ended_never_returns_a_guest_basket():
    # Re-minting works for guests too: a successful retry would read a guest basket as the account's.
    acct, site = account(
        {
            PATHS["basket"]: [(401, "{}"), (200, OK_BASKET)],
            PATHS["cart_page"]: [(200, "<html>")],
            PATHS["home"]: [(200, HOME_OUT)],
        }
    )
    with pytest.raises(HktvError) as err:
        acct.basket()
    assert err.value.code == "session_expired" and site.session.marks == ["dead"]
    assert site.transport.calls.count(PATHS["basket"]) == 1  # the guest basket was never read


def test_slots_and_warnings_recover_a_lapsed_token_like_the_basket():
    raw = json.dumps({"status": "SUCCESS", "data": {"errorMessages": ["Item A is out of stock"]}})
    acct, site = account(
        {
            PATHS["checkout_errors"]: [(401, "{}"), (200, raw)],
            PATHS["cart_page"]: [(200, "<html>")],
            PATHS["home"]: [(200, HOME_IN)],
        }
    )
    assert acct.checkout_errors() == ["Item A is out of stock"]


def test_a_login_page_instead_of_an_order_page_is_a_lost_sign_in_not_a_format_change():
    acct, site = account(
        {PATHS["order_page"] + "H900000000001": [(200, "<html>login</html>")], PATHS["home"]: [(200, HOME_OUT)]}
    )
    with pytest.raises(HktvError) as err:
        acct.order_entries("H900000000001")
    assert err.value.code == "session_expired"


def test_a_429_from_the_review_host_does_not_pause_the_basket(tmp_path):
    from hktvmall_mcp.site import Transport
    from hktvmall_mcp.state import StateStore

    class Opener:
        def __init__(self, code):
            self.code = code

        def open(self, req, timeout):
            import io
            import urllib.error

            raise urllib.error.HTTPError(req.full_url, self.code, "x", {}, io.BytesIO(b""))

    state = StateStore(tmp_path, budget=10)
    t = Transport(state, sleep=lambda _s: None)
    with pytest.raises(HktvError) as err:
        t.send(Opener(429), "GET", "https://ucapi.comms.hktvmall.com/hktvmall/products/X/reviews/")
    # no pause exists to wait for, so not "blocked" (whose next step points at the pause)
    assert err.value.code == "upstream_error" and state.snapshot()["paused_until"] is None
    with pytest.raises(HktvError):
        t.send(Opener(429), "GET", "https://www.hktvmall.com/hktv/en/")
    assert state.snapshot()["paused_until"] is not None


def test_a_401_that_persists_while_signed_out_marks_the_session_dead():
    acct, site = account(
        {PATHS["basket"]: [(401, "{}")], PATHS["cart_page"]: [(200, "<html>")], PATHS["home"]: [(200, HOME_OUT)]}
    )
    with pytest.raises(HktvError) as err:
        acct.basket()
    assert err.value.code == "session_expired" and site.session.marks == ["dead"]


def test_a_401_that_persists_while_signed_in_is_an_api_change():
    acct, site = account(
        {PATHS["basket"]: [(401, "{}")], PATHS["cart_page"]: [(200, "<html>")], PATHS["home"]: [(200, HOME_IN)]}
    )
    with pytest.raises(HktvError) as err:
        acct.basket()
    assert err.value.code == "schema_changed" and site.session.marks == ["live"]


def test_verify_reads_csrf_and_the_sign_in_flag():
    acct, site = account({PATHS["home"]: [(200, HOME_IN)]})
    acct.verify()
    assert acct.csrf == "c-1" and site.session.marks == ["live"]


class Resp:
    def __init__(self, status, text):
        self.status, self.text = status, text


def public_site(answers):
    site = Site.__new__(Site)
    site.transport, site.session, site.search_key = ScriptedTransport(answers), Session(), ""
    site._public = None
    return site


HOME_KEY = "<script>'keywordSearchServerApiKey' : \"k-%d\"</script>"
KWS_OK = json.dumps({"results": [{"totalValue": 1, "hits": [{"source": {"code": "A"}}]}]})


def test_search_rereads_a_rotated_key_and_retries_once_before_pausing():
    calls = []

    class T(ScriptedTransport):
        def send(self, opener, method, url, body=None, headers=None):
            if "keyword-search-server" in url:
                calls.append(headers["Authorization"])
                first = sum(1 for c in calls if c.startswith("ApiKey")) == 1
                return (403, "<html>") if first else (200, KWS_OK)
            calls.append(url)
            return 200, HOME_KEY % len(calls)

    site = public_site({})
    site.transport = T({})
    assert site.search([{"q": "eggs"}])[0]["hits"] == [{"code": "A"}]
    assert calls[0] == "https://www.hktvmall.com/hktv/en/" and calls[1] == "ApiKey k-1" and calls[-1] == "ApiKey k-3"


def test_account_saves_rotated_cookies_even_when_the_call_fails(tmp_path, clock):
    from tests_support import EMAIL, publish_snapshot

    from hktvmall_mcp.session import SessionStore

    publish_snapshot(tmp_path / "snapshot.json", clock())
    store = SessionStore(tmp_path / "snapshot.json", EMAIL, clock=clock)
    store.checkout_client()
    store.mark("live", 1)  # already verified: no home-page check before the call
    site = Site.__new__(Site)
    site.session, site._account_wait = store, 1.0
    import threading

    site._account_lock = threading.Lock()
    with pytest.raises(RuntimeError), site.account() as acct:
        for c in acct.jar:
            if c.name == "JSESSIONID":
                c.value = "rotated"
        raise RuntimeError("boom")
    assert {c.name: c.value for c in store.checkout_client()[1]}["JSESSIONID"] == "rotated"


def test_the_account_lock_times_out_as_busy(tmp_path, clock):
    import threading

    site = Site.__new__(Site)
    site._account_wait, site._account_lock = 0.01, threading.Lock()
    site._account_lock.acquire()
    with pytest.raises(HktvError) as err, site.account():
        pass
    assert err.value.code == "busy"


def _basket(lines):
    entries = [
        {
            "quantity": q,
            "product": {"code": s, "name": s, "stock": {"stockLevel": 50}},
            "basePrice": {"value": "10.0"},
            "discountedUnitPrice": {"value": "10.0"},
            "totalPrice": {"value": str(10 * q)},
        }
        for s, q in lines.items()
    ]
    data = {
        "cartDataGroupByDeliveryDate": {
            "deliveryDateOrderEntries": [
                {"batchNumber": 1, "deliveryMode": "hktv-standard-delivery", "orderEntries": entries}
            ]
        },
        "cartSubTotal": {"value": str(10 * sum(lines.values()))},
    }
    return json.dumps({"status": "SUCCESS", "data": data})


def test_a_sign_out_during_an_update_never_reads_a_guest_basket_back():
    # The independent 0.5.0 audit: remove -> 401 -> re-mint -> home says signed out -> the read-back used to
    # re-mint a guest token and report the removal "applied" on an empty guest basket.
    from contextlib import contextmanager

    from hktvmall_mcp import cart

    acct, site = account(
        {
            PATHS["basket"]: [(200, _basket({"A": 2, "B": 1})), (200, _basket({}))],
            PATHS["remove"]: [(401, "{}")],
            PATHS["cart_page"]: [(200, "<html>")],
            PATHS["home"]: [(200, HOME_OUT)],
        }
    )

    class OneAccount:
        @contextmanager
        def account(self):
            yield acct

        def lookup(self, ids):
            return {}

        def search_works(self):
            return True

    with pytest.raises(HktvError) as err:
        cart.update(OneAccount(), [{"id": "A", "quantity": 0}], False)
    assert err.value.code == "outcome_unknown"  # the agent re-reads the basket, which says session_expired
    assert site.transport.calls.count(PATHS["basket"]) == 1 and site.transport.calls.count(PATHS["cart_page"]) == 1
    assert site.session.marks == ["dead"]


def test_a_basket_5xx_is_retryable_and_costs_no_extra_request():
    acct, site = account({PATHS["basket"]: [(503, "<html>Service Unavailable</html>")]})
    with pytest.raises(HktvError) as err:
        acct.basket()
    assert err.value.code == "upstream_error" and err.value.payload()["retryable"] is True
    assert site.transport.calls == [PATHS["basket"]]


def test_checkout_warnings_parser_accepts_any_shape():
    for payload, expected in [
        ({"status": "FAIL", "data": ["x"]}, []),
        ({"data": {"errorMessages": "Item out of stock"}}, ["Item out of stock"]),
        ({"data": {"errorMessages": ["a", 3, None, "b"]}}, ["a", "b"]),
        ({"data": None}, []),
    ]:
        acct, _ = account({PATHS["checkout_errors"]: [(200, json.dumps(payload))]})
        assert acct.checkout_errors() == expected, payload


def test_an_empty_baskets_no_slots_answer_is_marked_and_any_other_fail_is_a_format_change():
    # HKTVmall's answer for an empty basket: FAIL with only the fees, no slot keys
    slots_path = PATHS["delivery_slots"] + "?deliveryZoneId=ZONE_TEST_1"
    fees = {"deliveryFee": 0.0, "expressDeliveryFee": 0.0, "deliveryThreshold": 88.0, "expressDeliveryThreshold": 300.0}
    acct, _ = account({slots_path: [(200, json.dumps({"status": "FAIL", "data": fees}))]})
    assert acct.delivery_slots("ZONE_TEST_1") == {**fees, NO_SLOTS: True}
    for other in ({"status": "FAIL", "data": {}}, {"status": "FAIL", "data": {**fees, "deliveryTimeSlots": []}}):
        acct, _ = account({slots_path: [(200, json.dumps(other))]})
        with pytest.raises(HktvError) as err:
            acct.delivery_slots("ZONE_TEST_1")
        assert err.value.code == "schema_changed"


def test_a_re_minted_token_is_never_taken_as_proof_of_the_sign_in():
    acct, site = account({PATHS["cart_page"]: [(200, "<html></html>")], PATHS["home"]: [(200, HOME_OUT)],
                          PATHS["basket"]: [(200, OK_BASKET)]})  # fmt: skip
    acct._drop_token()  # the token lapsed since the last check; a re-mint would work for a guest too
    with pytest.raises(HktvError, match="session_expired"):
        acct.basket()
    assert PATHS["basket"] not in site.transport.calls and site.session.marks == ["dead"]


def test_malformed_numbers_are_unknown_not_a_crash():
    from hktvmall_mcp.site import cents
    from hktvmall_mcp.views import _cents

    assert cents("NaN") is None and cents("Infinity") is None and _cents("inf") is None

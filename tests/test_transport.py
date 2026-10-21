"""Transport rules: allowlisted hosts and redirects, pacing, busy, budget, pause on pushback."""

import io
import threading
import urllib.error
import urllib.request

import pytest

from hktvmall_mcp.errors import HktvError
from hktvmall_mcp.site import AllowlistedRedirects, Transport
from hktvmall_mcp.state import StateStore


class Resp(io.BytesIO):
    def __init__(self, status, body=b"{}"):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Opener:
    def __init__(self, *answers):
        self.answers, self.urls = list(answers), []

    def open(self, req, timeout):
        self.urls.append(req.full_url)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def transport(tmp_path, clock, budget=10, **kw):
    sleeps = []
    t = Transport(StateStore(tmp_path, budget=budget, clock=clock), sleep=sleeps.append, monotonic=clock, **kw)
    return t, sleeps


def test_hosts_and_redirects_are_allowlisted(tmp_path, clock):
    t, _ = transport(tmp_path, clock)
    with pytest.raises(HktvError):
        t.send(Opener(), "GET", "https://evil.example/")
    req = urllib.request.Request("https://www.hktvmall.com/hktv/en/")
    with pytest.raises(HktvError) as err:
        AllowlistedRedirects().redirect_request(req, None, 302, "Found", {}, "https://evil.example/x")
    assert err.value.code == "schema_changed"
    assert AllowlistedRedirects().redirect_request(req, None, 302, "Found", {}, "https://www.hktvmall.com/hktv/en/cart")


def test_pacing_budget_and_pause(tmp_path, clock):
    t, sleeps = transport(tmp_path, clock, budget=3)
    opener = Opener(Resp(200), Resp(200), Resp(429))
    t.send(opener, "GET", "https://www.hktvmall.com/hktv/en/")
    t.send(opener, "GET", "https://www.hktvmall.com/hktv/en/")
    assert sleeps and 0.35 <= sleeps[0] <= 0.5
    with pytest.raises(HktvError) as err:
        t.send(opener, "GET", "https://www.hktvmall.com/hktv/en/")
    assert err.value.code == "blocked"
    with pytest.raises(HktvError) as err:  # the pause holds without touching HKTVmall
        t.send(Opener(), "GET", "https://www.hktvmall.com/hktv/en/")
    assert err.value.code == "blocked"
    clock.now += 6 * 60  # a first pushback pauses 5 minutes
    with pytest.raises(HktvError) as err:
        t.send(Opener(Resp(200)), "GET", "https://www.hktvmall.com/hktv/en/")
    assert err.value.code == "budget_exhausted"


def test_network_failure_and_http_errors(tmp_path, clock):
    t, _ = transport(tmp_path, clock)
    with pytest.raises(HktvError) as err:
        t.send(Opener(urllib.error.URLError("down")), "GET", "https://www.hktvmall.com/hktv/en/")
    assert err.value.code == "upstream_error"
    http403 = urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b"<html>"))
    assert t.send(Opener(http403), "GET", "https://www.hktvmall.com/hktv/en/cart")[0] == 403


def test_busy_when_the_queue_wait_runs_out(tmp_path, clock):
    t, _ = transport(tmp_path, clock, queue_wait=0.01)
    t._lock.acquire()
    try:
        with pytest.raises(HktvError) as err:
            threading.Thread(target=lambda: None).start()
            t.send(Opener(Resp(200)), "GET", "https://www.hktvmall.com/hktv/en/")
        assert err.value.code == "busy"
    finally:
        t._lock.release()


def test_a_second_pushback_within_two_hours_pauses_for_long(tmp_path, clock):
    from hktvmall_mcp.state import StateStore

    state = StateStore(tmp_path, budget=100, clock=clock)

    def paused_for():
        return state.snapshot()["paused_until"] and round((state._data["paused_until"] - clock()) / 60)

    state.pause("home 403")
    assert paused_for() == 5
    clock.now += 60 * 60
    state.pause("home 403")  # again within two hours: real blocking
    assert paused_for() == 30
    clock.now += 3 * 3600
    state.pause("429")  # long after: a blip again
    assert paused_for() == 5

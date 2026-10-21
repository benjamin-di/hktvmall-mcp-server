"""The session snapshot as the only session authority, held in memory per generation."""

import json
import shutil
from pathlib import Path

import pytest
from tests_support import EMAIL, publish_snapshot

from hktvmall_mcp import session as session_module
from hktvmall_mcp.config import Settings
from hktvmall_mcp.errors import HktvError
from hktvmall_mcp.server import HktvService
from hktvmall_mcp.session import SessionStore
from hktvmall_mcp.site import PATHS, Site
from hktvmall_mcp.snapshot import read_snapshot
from hktvmall_mcp.state import StateStore

FIXTURE = Path(__file__).parent / "fixtures" / "session-snapshot.json"


def published(tmp_path, clock, publish=True):
    source = tmp_path / "source" / "snapshot.json"
    store = SessionStore(source, EMAIL, clock=clock)
    if publish:
        publish_snapshot(source, clock())
        assert store.generation == 1  # adopted lazily, on the first call
    return store, source


def values(store):
    _, jar, _ = store.checkout_client()
    return {cookie.name: cookie.value for cookie in jar}


def code(store):
    with pytest.raises(HktvError) as err:
        store.checkout_client()
    return err.value.code


def rotate(store, value):
    generation, jar, _ = store.checkout_client()
    for cookie in jar:
        cookie.value = value
    store.store_jar(jar, generation)


def test_missing_snapshot_is_session_source_missing(tmp_path, clock):
    store, source = published(tmp_path, clock, publish=False)
    assert code(store) == "session_source_missing" and store.canary_error() == "session_source_missing"
    assert store.status()["session_source"] == {"state": "missing", "generation": None}
    assert store.generation == 0 and not store.status()["signed_in"]
    publish_snapshot(source, clock())
    assert values(store)["JSESSIONID"] == "session-1"
    source.unlink()  # fails closed; the held generation comes back with the file
    assert code(store) == "session_source_missing"


@pytest.mark.parametrize(
    "overrides", [{"email": "other@example.com"}, {"cookies": []}, {"schema": 2}, {"generation": 0}]
)
def test_invalid_snapshot_is_session_source_invalid(tmp_path, clock, overrides):
    store, source = published(tmp_path, clock, publish=False)
    publish_snapshot(source, clock(), **overrides)
    assert code(store) == "session_source_invalid" and store.canary_error() == "session_source_invalid"
    source.write_text("partial JSON", encoding="utf-8")
    assert code(store) == "session_source_invalid"
    assert store.status()["session_source"]["state"] == "invalid"


def test_rotated_cookies_are_used_on_the_next_call_and_never_written_to_the_source(tmp_path, clock):
    store, source = published(tmp_path, clock)
    before = source.read_bytes(), source.stat().st_mtime_ns
    rotate(store, "rotated")
    assert values(store)["JSESSIONID"] == "rotated"
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before
    assert list((tmp_path / "source").iterdir()) == [source]


def test_unchanged_snapshot_is_not_parsed_again(tmp_path, clock, monkeypatch):
    store, source = published(tmp_path, clock)  # parsed once on adoption
    parses = []

    def counting(*args):
        parses.append(1)
        return read_snapshot(*args)

    monkeypatch.setattr(session_module, "read_snapshot", counting)
    for _ in range(3):
        store.checkout_client()
        store.status()
    assert parses == []
    publish_snapshot(source, clock())  # a new file (new inode) is parsed again, once
    store.checkout_client()
    store.status()
    assert parses == [1]


def test_newer_generation_replaces_cookies_and_must_verify_again(tmp_path, clock):
    store, source = published(tmp_path, clock)
    assert store.needs_verification(1)
    store.mark("live", 1)
    assert not store.needs_verification(1) and store.status()["signed_in"]
    rotate(store, "rotated")
    publish_snapshot(source, clock(), 2)
    assert values(store)["JSESSIONID"] == "session-2" and store.generation == 2
    assert store.needs_verification(2) and store.status()["state"] == "unverified"


def test_a_call_on_an_older_generation_never_touches_the_newer_one(tmp_path, clock):
    store, source = published(tmp_path, clock)
    old_generation, old_jar, _ = store.checkout_client()
    for cookie in old_jar:
        cookie.value = "old-call-rotation"
    publish_snapshot(source, clock(), 2)
    assert store.generation == 2
    store.store_jar(old_jar, old_generation)
    store.mark("dead", old_generation)
    assert values(store)["JSESSIONID"] == "session-2" and store.status()["state"] == "unverified"


def test_dead_generation_is_expired_until_a_newer_one(tmp_path, clock):
    store, source = published(tmp_path, clock)
    store.mark("dead", 1)
    store.mark("live", 1)  # dead stays dead
    assert code(store) == "session_expired" and store.canary_error() == "session_expired"
    assert store.status()["state"] == "dead" and store.status()["dead_at"] == clock()
    publish_snapshot(source, clock(), 1, user_agent="Republished")  # same generation: ignored
    assert code(store) == "session_expired"
    publish_snapshot(source, clock(), 2)
    assert values(store)["JSESSIONID"] == "session-2" and store.canary_error() is None


def test_older_or_equal_generation_is_ignored(tmp_path, clock):
    store, source = published(tmp_path, clock)
    publish_snapshot(source, clock(), 3)
    rotate(store, "rotated")
    for generation in (1, 3):
        publish_snapshot(source, clock(), generation)
        assert store.generation == 3 and values(store)["JSESSIONID"] == "rotated"
        assert store.status()["session_source"] == {"state": "ready", "generation": generation}


def test_a_publishers_session_file_is_accepted(tmp_path, clock):
    # A snapshot as a session publisher writes it (tests/fixtures/README.md): one line, its keys in that order.
    clock.now = 1_894_694_400 + 60  # just after its verified_at, 2030-01-15T08:00:00Z
    snapshot = read_snapshot(FIXTURE, EMAIL, clock())
    assert snapshot.generation == 1 and snapshot.user_agent.startswith("Mozilla/5.0")
    assert {c["name"]: c["value"] for c in snapshot.cookies}["JSESSIONID"] == "test-jsessionid"
    source = tmp_path / "source" / "snapshot.json"
    source.parent.mkdir()
    shutil.copy(FIXTURE, source)
    assert values(SessionStore(source, EMAIL, clock=clock))["JSESSIONID"] == "test-jsessionid"


def test_canary_reports_source_failures_despite_a_cached_success(tmp_path, clock, site):
    store, source = published(tmp_path, clock)
    settings = Settings(snapshot_path=source, expected_email=EMAIL, state_dir=tmp_path / "state")
    service = HktvService(site, StateStore(settings.state_dir, budget=100, clock=clock), store, settings)
    assert service.canary()["ok"]
    source.unlink()
    assert service.canary()["code"] == "session_source_missing"
    source.write_text("bad", encoding="utf-8")
    assert service.canary()["code"] == "session_source_invalid"
    publish_snapshot(source, clock())
    assert service.canary()["ok"]


def test_cold_basket_uses_the_snapshots_cookies_and_user_agent_without_mutating_the_basket(tmp_path, clock):
    store, source = published(tmp_path, clock)
    before = source.read_bytes()
    calls = []

    class Transport:
        def send(self, opener, method, url, body=None, headers=None):
            cookies = {
                cookie.name: cookie.value for handler in opener.handlers for cookie in getattr(handler, "cookiejar", [])
            }
            assert cookies["JSESSIONID"] == "session-1"
            assert headers["User-Agent"] == "Browser/156"
            assert method == "GET" and body is None
            path = url.split("hktvmall.com", 1)[1]
            calls.append(path)
            if path == PATHS["home"]:
                return 200, "'loggedin': true; var CSRFToken = \"csrf\";"
            if path == PATHS["cart_page"]:
                return 200, "<html>"
            assert path == PATHS["basket"]
            return 200, json.dumps({"status": "SUCCESS", "data": {"cartSubTotal": {"value": "0"}}})

    site = Site(Transport(), store)
    with site.account() as account:
        assert account.basket() == {"cartSubTotal": {"value": "0"}}
    assert calls == [PATHS["home"], PATHS["cart_page"], PATHS["basket"]]
    assert store.status()["signed_in"] and source.read_bytes() == before


def test_cold_snapshot_that_is_signed_out_never_returns_a_guest_basket(tmp_path, clock):
    store, _ = published(tmp_path, clock)

    class Transport:
        def send(self, opener, method, url, body=None, headers=None):
            assert url.endswith(PATHS["home"])
            return 200, "'loggedin': false; var CSRFToken = \"csrf\";"

    with pytest.raises(HktvError) as err, Site(Transport(), store).account() as account:
        account.basket()
    assert err.value.code == "session_expired" and code(store) == "session_expired"

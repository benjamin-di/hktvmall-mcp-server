"""HKTVmall atomic publication and the unchanged Google snapshot contract."""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

if sys.platform != "linux":
    pytest.skip("optional maintenance requires Linux", allow_module_level=True)

from signin_window import state as private_state

from hktvmall_maintenance import snapshot as hktv
from hktvmall_maintenance.cookies import ExportCookie
from hktvmall_maintenance.outcomes import VerifyFailed, VerifyFailure
from hktvmall_maintenance.snapshot import SnapshotError

EMAIL = "account@example.com"
NOW = datetime(2026, 10, 18, 8, tzinfo=UTC)
UA = "Mozilla/5.0 Chrome/156.0.0.0 Safari/537.36"
SESSION = ExportCookie("www.hktvmall.com", True, "/", True, 0, "JSESSIONID", "fake-j1")
FIXTURES = Path(__file__).parent / "fixtures" / "identity_snapshot"


class Browser:
    def __init__(self, cookies=None, user_agent=UA):
        self.cookies = [SESSION] if cookies is None else cookies
        self.agent = user_agent

    def export_cookies(self):
        return self.cookies

    def user_agent(self):
        return self.agent


def test_single_atomic_file_matches_consumer_contract_and_is_private(tmp_path, monkeypatch):
    target = tmp_path / hktv.SNAPSHOT_FILE
    replacements = []
    original_replace = private_state.os.replace

    def record_replace(source, destination):
        # Before the commit, the temporary file is already a complete new snapshot.
        incoming = json.loads(Path(source).read_bytes())
        assert incoming["target"] == "hktvmall"
        assert stat.S_IMODE(Path(source).stat().st_mode) == 0o600
        if Path(destination).exists():
            assert json.loads(Path(destination).read_bytes())["generation"] == 1
        replacements.append(Path(destination).name)
        original_replace(source, destination)

    monkeypatch.setattr(private_state.os, "replace", record_replace)
    domain_cookie = ExportCookie("hktvmall.com", False, "/", True, 1_900_000_000, "a", "fake-a")
    hktv.publish(tmp_path, Browser([SESSION, domain_cookie]), 1, EMAIL, NOW)
    with target.open("rb") as previous:
        old = previous.read()
        hktv.publish(tmp_path, Browser([replace(SESSION, value="fake-j2")]), 2, EMAIL, NOW)
        previous.seek(0)
        assert previous.read() == old  # readers holding the old inode retain a complete old file
    payload = json.loads(target.read_bytes())
    assert set(payload) == {
        "schema",
        "target",
        "generation",
        "email",
        "user_agent",
        "verified_at",
        "cookies",
    }
    assert payload == {
        "schema": 1,
        "target": "hktvmall",
        "generation": 2,
        "email": EMAIL,
        "user_agent": UA,
        "verified_at": "2026-10-18T08:00:00Z",
        "cookies": [
            {
                "name": "JSESSIONID",
                "value": "fake-j2",
                "domain": "www.hktvmall.com",
                "path": "/",
                "secure": True,
                "expires": None,
            }
        ],
    }
    first = json.loads(old)
    assert first["cookies"][1]["domain"] == ".hktvmall.com"
    assert first["cookies"][1]["expires"] == 1_900_000_000
    assert replacements == [hktv.SNAPSHOT_FILE, hktv.SNAPSHOT_FILE]
    assert sorted(path.name for path in tmp_path.iterdir()) == [hktv.SNAPSHOT_FILE]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert hktv.published_generation(tmp_path) == 2


@pytest.mark.parametrize("generation", [True, False, 0, -1, 1.0, "2", 2**63])
def test_bad_generation_never_publishes(tmp_path, generation):
    with pytest.raises(SnapshotError):
        hktv.publish(tmp_path, Browser(), generation, EMAIL, NOW)
    assert not list(tmp_path.iterdir())


def test_generations_are_immutable_and_time_never_regresses(tmp_path):
    assert hktv.published_generation(tmp_path) == 0
    hktv.publish(tmp_path, Browser(), 4, EMAIL, NOW)
    prior = (tmp_path / hktv.SNAPSHOT_FILE).read_bytes()
    for generation, moment in ((4, NOW), (3, NOW), (5, NOW - timedelta(seconds=1))):
        with pytest.raises(SnapshotError):
            hktv.publish(tmp_path, Browser(), generation, EMAIL, moment)
        assert (tmp_path / hktv.SNAPSHOT_FILE).read_bytes() == prior
    # Publication may share UTCsecond resolution with its predecessor.
    hktv.publish(tmp_path, Browser(), 5, EMAIL, NOW)
    assert hktv.published_generation(tmp_path) == 5


def incomplete():
    return pytest.raises(VerifyFailed, match=VerifyFailure.EXPORT_INCOMPLETE.value)


HEADER_UNSAFE = [
    ("name", "bad\r\nname"),
    ("name", ""),
    ("name", "bad=name"),
    ("name", "bad name"),
    ("name", "x" * 257),
    ("name", None),
    ("value", "cookie\x00"),
    ("value", "cookie\r\nheader"),
    ("value", "x;y=z"),
    ("value", "nonascii-é"),
    ("value", "x" * 16385),
    ("value", None),
]


@pytest.mark.parametrize("field,value", HEADER_UNSAFE)
def test_header_unsafe_cookie_is_dropped_from_the_export(tmp_path, field, value):
    odd = replace(
        ExportCookie("hktvmall.com", False, "/", True, 0, "odd", "fake"),
        **{field: value},
    )
    kept = ExportCookie("hktvmall.com", False, "/", True, 0, "kept", "fake-k")
    hktv.publish(tmp_path, Browser([odd, SESSION, kept]), 1, EMAIL, NOW)
    raw = (tmp_path / hktv.SNAPSHOT_FILE).read_bytes()
    names = [cookie["name"] for cookie in json.loads(raw)["cookies"]]
    assert names == ["JSESSIONID", "kept"]
    assert b'"odd"' not in raw
    if isinstance(value, str) and value:
        assert value.encode() not in raw


@pytest.mark.parametrize("value", ["cookie\x00", "cookie\r\nheader", "x;y=z", "nonascii-é", None])
def test_header_unsafe_session_cookie_still_fails_the_export(tmp_path, value):
    with incomplete():
        hktv.publish(
            tmp_path,
            Browser([replace(SESSION, value=value), replace(SESSION, path="/x")]),
            1,
            EMAIL,
            NOW,
        )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "field,value",
    [
        ("domain", "google.com"),
        ("domain", "evilhktvmall.com"),
        ("domain", "hktvmall.com.evil.com"),
        ("domain", ".hktvmall.com"),
        ("domain", "bad..hktvmall.com"),
        ("domain", "bad_.hktvmall.com"),
        ("path", "relative"),
        ("path", "/\t"),
        ("path", "/x;foo=bar"),
        ("path", "/" * 2049),
        ("secure", 1),
        ("host_only", "false"),
        ("expires", True),
        ("expires", -1),
        ("expires", 1.0),
        ("expires", 253_402_300_800),
    ],
)
def test_invalid_cookie_rejects_whole_export(tmp_path, field, value):
    cookie = replace(SESSION, **{field: value})
    with incomplete():
        hktv.publish(tmp_path, Browser([SESSION, cookie]), 1, EMAIL, NOW)
    assert not list(tmp_path.iterdir())


def test_all_hktvmall_subdomains_allowed_but_main_site_session_required(tmp_path):
    cookie = ExportCookie("assets.hktvmall.com", False, "/", True, 0, "analytics", "fake")
    hktv.publish(tmp_path, Browser([SESSION, cookie]), 1, EMAIL, NOW)
    payload = json.loads((tmp_path / hktv.SNAPSHOT_FILE).read_bytes())
    assert payload["cookies"][1]["domain"] == ".assets.hktvmall.com"
    for bad in (
        [],
        [replace(SESSION, name="other")],
        [replace(SESSION, value="")],
        [replace(SESSION, path="/hktv/en/")],
        [replace(SESSION, domain="assets.hktvmall.com")],
        [replace(SESSION, expires=int(NOW.timestamp()))],
    ):
        with incomplete():
            hktv.publish(tmp_path, Browser(bad), 2, EMAIL, NOW)
    assert hktv.published_generation(tmp_path) == 1


def test_duplicate_cookie_and_export_size_limits(tmp_path):
    with incomplete():
        hktv.publish(tmp_path, Browser([SESSION, SESSION]), 1, EMAIL, NOW)
    many = [replace(SESSION, name=f"cookie-{index}") for index in range(512)]
    with incomplete():
        hktv.publish(tmp_path, Browser([SESSION, *many]), 1, EMAIL, NOW)
    large = [replace(SESSION, name=f"cookie-{index}", value="x" * 16384) for index in range(33)]
    with incomplete():
        hktv.publish(tmp_path, Browser([SESSION, *large]), 1, EMAIL, NOW)
    assert not list(tmp_path.iterdir())
    hktv.publish(tmp_path, Browser([SESSION, *many[:511]]), 1, EMAIL, NOW)
    assert len(json.loads((tmp_path / hktv.SNAPSHOT_FILE).read_bytes())["cookies"]) == 512


@pytest.mark.parametrize("user_agent", ["", "x\r\nInjected: header", "x" * 1025, None])
def test_invalid_user_agent_never_publishes(tmp_path, user_agent):
    with incomplete():
        hktv.publish(tmp_path, Browser(user_agent=user_agent), 1, EMAIL, NOW)


@pytest.mark.parametrize("email", ["", "not-an-email", "x\n@example.com", None])
def test_invalid_email_never_publishes(tmp_path, email):
    with pytest.raises(SnapshotError):
        hktv.publish(tmp_path, Browser(), 1, email, NOW)


def test_naive_or_pre_epoch_verification_time_never_publishes(tmp_path):
    for moment in (NOW.replace(tzinfo=None), datetime(1969, 1, 1, tzinfo=UTC), None):
        with pytest.raises(SnapshotError):
            hktv.publish(tmp_path, Browser(), 1, EMAIL, moment)


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"[]",
        b"{}",
        b"\xff",
        b"{" * 2000,
        b'{"schema":1,"schema":1,"target":"hktvmall","generation":1}',
        b'{"schema":true,"target":"hktvmall","generation":1}',
        b'{"schema":1,"target":"google","generation":1}',
        b'{"schema":1,"target":"hktvmall","generation":true}',
        b'{"schema":1,"target":"hktvmall","generation":0}',
        b'{"schema":1,"target":"hktvmall","generation":9223372036854775808}',
    ],
)
def test_malformed_previous_snapshot_never_silently_resets_generation(tmp_path, payload):
    (tmp_path / hktv.SNAPSHOT_FILE).write_bytes(payload)
    with pytest.raises(SnapshotError):
        hktv.published_generation(tmp_path)
    with pytest.raises(SnapshotError):
        hktv.publish(tmp_path, Browser(), 2, EMAIL, NOW)
    assert (tmp_path / hktv.SNAPSHOT_FILE).read_bytes() == payload


def test_unsafe_previous_snapshot_and_failed_write_preserve_authority(tmp_path, monkeypatch):
    target = tmp_path / hktv.SNAPSHOT_FILE
    target.write_bytes(b"x" * (hktv.MAX_BYTES + 1))
    with pytest.raises(SnapshotError):
        hktv.published_generation(tmp_path)
    target.unlink()
    os.mkfifo(target)
    with pytest.raises(SnapshotError):
        hktv.published_generation(tmp_path)
    target.unlink()
    other = tmp_path / "other"
    other.write_text("do not overwrite")
    target.symlink_to(other)
    with pytest.raises(SnapshotError):
        hktv.publish(tmp_path, Browser(), 1, EMAIL, NOW)
    assert other.read_text() == "do not overwrite"
    target.unlink()
    hktv.publish(tmp_path, Browser(), 1, EMAIL, NOW)
    before = target.read_bytes()

    def fail_write(path, payload):
        raise private_state.PrivateStateError("scripted publication failure")

    monkeypatch.setattr(hktv, "atomic_private_write", fail_write)
    with pytest.raises(SnapshotError, match="publication failed"):
        hktv.publish(tmp_path, Browser(), 2, EMAIL, NOW)
    assert target.read_bytes() == before

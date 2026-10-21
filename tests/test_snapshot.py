"""Untrusted session snapshot validation, account binding and safe, bounded file reads."""

import json
import os

import pytest
from tests_support import EMAIL, session_snapshot

from hktvmall_mcp.config import ConfigError, Settings
from hktvmall_mcp.snapshot import MAX_SNAPSHOT, SnapshotError, parse_snapshot, read_snapshot


def test_snapshot_contract_and_casefold_email(clock):
    data = session_snapshot(clock())
    data["email"] = EMAIL.upper()
    result = parse_snapshot(json.dumps(data).encode(), EMAIL, clock())
    assert result.generation == 1 and result.email == EMAIL
    assert result.user_agent == "Browser/156" and result.verified_at == clock()
    assert result.cookies[0]["value"] == "session-1"


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", True),
        ("schema", 2),
        ("target", "google"),
        ("generation", True),
        ("generation", 0),
        ("generation", -1),
        ("generation", 2**64),
        ("email", "other@example.net"),
        ("email", "shopper+alias@example.com"),
        ("user_agent", ""),
        ("user_agent", "unsafe\r\nCookie: secret"),
        ("user_agent", "x" * 1025),
        ("verified_at", "2026-10-18T00:00:00"),
        ("verified_at", "2026-10-18T08:00:00+08:00"),
        ("verified_at", "2099-01-01T00:00:00Z"),
        ("verified_at", "2026-99-18T00:00:00Z"),
        ("cookies", []),
        ("cookies", {}),
        ("cookies", [None]),
    ],
)
def test_invalid_snapshot_fields_reject_the_entire_authority(clock, field, value):
    data = {**session_snapshot(clock()), field: value}
    with pytest.raises(SnapshotError):
        parse_snapshot(json.dumps(data).encode(), EMAIL, clock())


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", ""),
        ("name", "JSESSIONID\n"),
        ("name", "bad=name"),
        ("name", "x" * 257),
        ("value", "code\x00"),
        ("value", "cookie\r\nInjected: value"),
        ("value", "x;y=z"),
        ("value", "x" * 16385),
        ("value", None),
        ("domain", "hktvmall.com.evil.com"),
        ("domain", "evilhktvmall.com"),
        ("domain", "..hktvmall.com"),
        ("domain", "www..hktvmall.com"),
        ("domain", "-evil.hktvmall.com"),
        ("domain", "www.hktvmall.com\r\n"),
        ("domain", "google.com"),
        ("domain", "other.hktvmall.com"),
        ("path", "relative"),
        ("path", "/\x00"),
        ("path", "/x;another=value"),
        ("path", "x" * 2049),
        ("secure", "true"),
        ("secure", 1),
        ("expires", True),
        ("expires", -1),
        ("expires", 2.1),
        ("expires", 253_402_300_800),
        ("expires", 1),
    ],
)
def test_cookie_scalar_validation(clock, field, value):
    data = session_snapshot(clock())
    data["cookies"][0][field] = value
    with pytest.raises(SnapshotError):
        parse_snapshot(json.dumps(data).encode(), EMAIL, clock())


def test_bad_json_limits_duplicate_keys_and_cookies(clock):
    data = session_snapshot(clock())
    cases = [
        b"{}",
        b"null",
        b"\xff",
        b"x" * (MAX_SNAPSHOT + 1),
        b"{" * 2000,
        json.dumps(data).replace('"schema": 1', '"schema": 1, "schema": 1').encode(),
    ]
    data["cookies"] *= 2
    cases.append(json.dumps(data).encode())
    data["cookies"] *= 257
    cases.append(json.dumps(data).encode())
    for raw in cases:
        with pytest.raises(SnapshotError):
            parse_snapshot(raw, EMAIL, clock())


def test_missing_expiry_field_is_not_a_session_cookie(clock):
    data = session_snapshot(clock())
    del data["cookies"][0]["expires"]
    with pytest.raises(SnapshotError):
        parse_snapshot(json.dumps(data).encode(), EMAIL, clock())


def test_source_regular_file_bounded_no_symlinks_or_fifo(tmp_path, clock):
    source = tmp_path / "source.json"
    source.write_text(json.dumps(session_snapshot(clock())))
    assert read_snapshot(source, EMAIL, clock()).generation == 1
    symlink = tmp_path / "symlink.json"
    symlink.symlink_to(source)
    directory = tmp_path / "linked-directory"
    directory.symlink_to(tmp_path, target_is_directory=True)
    special = []
    if hasattr(os, "mkfifo"):  # no FIFOs on Windows
        special.append(tmp_path / "fifo")
        os.mkfifo(special[0])
    huge = tmp_path / "huge"
    huge.write_bytes(b"x" * (MAX_SNAPSHOT + 1))
    for path in (symlink, directory / "source.json", *special, huge, tmp_path):
        with pytest.raises(SnapshotError):
            read_snapshot(path, EMAIL, clock())
    with pytest.raises(SnapshotError) as err:
        read_snapshot(tmp_path / "missing", EMAIL, clock())
    assert err.value.reason == "missing"


def test_snapshot_env_requires_bound_identity_and_separate_absolute_source(tmp_path):
    env = {
        "HKTVMALL_STATE_DIR": str(tmp_path),
        "HKTVMALL_SESSION_SNAPSHOT": str(tmp_path / "source.json"),
        "HKTVMALL_EXPECTED_EMAIL": EMAIL,
    }
    settings = Settings.from_env(env)
    assert settings.snapshot_path == tmp_path / "source.json" and settings.expected_email == EMAIL
    for overrides in (
        {"HKTVMALL_EXPECTED_EMAIL": ""},
        {"HKTVMALL_SESSION_SNAPSHOT": ""},
        {"HKTVMALL_SESSION_SNAPSHOT": "relative"},
        {"HKTVMALL_EXPECTED_EMAIL": "no-at-sign"},
        {"HKTVMALL_SESSION_SNAPSHOT": str(tmp_path / "state.json")},
        {"HKTVMALL_SESSION_SNAPSHOT": str(tmp_path / ".." / "source.json")},
    ):
        with pytest.raises(ConfigError):
            Settings.from_env({**env, **overrides})
    for missing in ("HKTVMALL_SESSION_SNAPSHOT", "HKTVMALL_EXPECTED_EMAIL"):
        with pytest.raises(ConfigError):  # a snapshot always comes with the account it must belong to
            Settings.from_env({k: v for k, v in env.items() if k != missing})

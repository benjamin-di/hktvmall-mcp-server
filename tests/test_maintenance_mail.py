"""Freshness, account binding and actual MCP envelopes for HKTVmall OTP mail."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

if sys.platform != "linux":
    pytest.skip("optional maintenance requires Linux", allow_module_level=True)

from hktvmall_maintenance import mail as hktvmall_mail
from hktvmall_maintenance.mail import HktvmallMailClient
from hktvmall_maintenance.outcomes import HoldReason, SignInHold

EMAIL = "account@example.com"
SUBJECT = "[HKTVmall] 偵測到用戶從網頁版登入"
PROMPT = "請輸入以下的一次性驗證密碼以完成登入驗證。"
EN_SUBJECT = "[HKTVmall] New website login on HKTVmall platform"
EN_PROMPT = "Please enter below one-time password to complete the login process on the web version."
NOW = datetime(2026, 10, 18, 10, 2, tzinfo=UTC)
REQUESTED = NOW - timedelta(seconds=12, microseconds=123456)
SENT = NOW - timedelta(seconds=10)
TRUST_PREFIX = "[UNTRUSTED_EXTERNAL_CONTENT] "


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(hktvmall_mail, "_now", lambda: NOW)
    monkeypatch.setattr(hktvmall_mail, "_SENDER_DOMAINS", frozenset({"codes.example.com", "shop.example.com"}))
    monkeypatch.delenv("HKTVMALL_MAINTENANCE_MAIL_URL", raising=False)
    monkeypatch.delenv("HKTVMALL_MAINTENANCE_MAIL_BEARER", raising=False)
    monkeypatch.setenv("HKTVMALL_MAINTENANCE_MAIL_HTTP_HOSTS", "mail.example.com")


def message(**changes):
    return {
        "id": "message-one",
        "from": {"address": "donotreply@codes.example.com"},
        "toRecipients": [{"emailAddress": {"address": EMAIL}}],
        "subject": TRUST_PREFIX + SUBJECT,
        "sentDateTime": SENT.isoformat(),
        "receivedDateTime": (SENT + timedelta(seconds=1)).isoformat(),
        "body": TRUST_PREFIX + "登入驗證\n" + PROMPT + "\n\n012345\n\nHKTVmall",
        **changes,
    }


def client(result):
    return HktvmallMailClient(EMAIL, call_tool=lambda _name, _args: result)


def assert_hold(reader, reason, *, preflight=False):
    with pytest.raises(SignInHold) as failure:
        if preflight:
            reader.preflight()
        else:
            reader.poll(REQUESTED)
    assert failure.value.reason is reason
    assert str(failure.value) == reason.value


def test_poll_preserves_leading_zero_and_only_reads_bounded_bot_search():
    calls = []

    def call(name, args):
        calls.append((name, args))
        return [message()]

    reader = HktvmallMailClient(EMAIL.upper(), call_tool=call)
    assert reader.configured
    assert reader.poll(REQUESTED) == "012345"
    assert calls == [
        (
            "hktvmall_login_mail",
            {
                "received_after": REQUESTED.replace(microsecond=0).isoformat(),
            },
        )
    ]
    # No body or code survives on the client between polls.
    assert "012345" not in repr(vars(reader))


@pytest.mark.parametrize(
    "wrap",
    [
        lambda rows: rows,
        lambda rows: {"structuredContent": {"result": rows}, "isError": False},
        lambda rows: SimpleNamespace(data=rows, content=[], is_error=False),
        lambda rows: SimpleNamespace(
            data=None,
            structured_content={"result": rows},
            content=[],
            is_error=False,
        ),
        lambda rows: {
            "content": [{"type": "text", "text": json.dumps(rows)}],
            "isError": False,
        },
        lambda rows: SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(rows))],
            is_error=False,
        ),
        lambda rows: {"content": [{"type": "text", "text": json.dumps(row)} for row in rows]},
    ],
)
def test_recognizes_supported_mcp_envelopes(wrap):
    assert client(wrap([message()])).poll(REQUESTED) == "012345"


@pytest.mark.parametrize(
    "result",
    [
        [],
        {"content": [], "isError": False},
        SimpleNamespace(data=None, content=[], is_error=False),
        {"structuredContent": {"result": []}},
        {"content": [{"type": "text", "text": "[]"}]},
    ],
)
def test_empty_successful_search_is_valid_preflight_and_not_a_code(result):
    reader = client(result)
    assert reader.preflight() is None
    assert reader.poll(REQUESTED) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"subject": "Your HKTVmall Order"},
        {"subject": "RE: " + SUBJECT},
        {"subject": SUBJECT + " unexpected suffix"},
        {"subject": " " + EN_SUBJECT},
        {"subject": "FW: " + EN_SUBJECT},
        {"from": {"address": "donotreply@codes.example.com.attacker.example"}},
        {"from": {"address": "donotreply@not-codes.example.com"}},
        {"from": {"address": "codes.example.com@attacker.example"}},
        {"from": {"address": "donotreply@sub.codes.example.com"}},
        {"from": {"address": "HKTV <donotreply@codes.example.com>"}},
        {"toRecipients": [{"emailAddress": {"address": "other@example.com"}}]},
        {"toRecipients": [{"emailAddress": {"address": "assistant@example.com"}}]},
        {"toRecipients": [], "ccRecipients": [{"emailAddress": {"address": EMAIL}}]},
        {"isDraft": True},
    ],
)
def test_unrelated_sender_subject_and_wrong_original_account_are_ignored(changes):
    # Invalid bodies on unrelated mail must never turn into a matching OTP.
    assert client([message(body="ignored", **changes)]).poll(REQUESTED) is None


@pytest.mark.parametrize(
    "sender",
    [
        {"address": "another@shop.example.com"},
        {"emailAddress": {"address": "DONOTREPLY@CODES.EXAMPLE.COM"}},
    ],
)
def test_exact_allowed_sender_domains_and_normalized_graph_sender(sender):
    assert client([message(**{"from": sender})]).poll(REQUESTED) == "012345"


@pytest.mark.parametrize(
    ("field", "stamp"),
    [
        ("receivedDateTime", REQUESTED.replace(microsecond=0) - timedelta(seconds=1)),
        ("receivedDateTime", NOW + timedelta(seconds=6)),
        (
            "sentDateTime",
            REQUESTED.replace(microsecond=0) - timedelta(minutes=2, seconds=1),
        ),
        ("sentDateTime", NOW - timedelta(minutes=5, seconds=1)),
        ("sentDateTime", NOW + timedelta(minutes=2, seconds=1)),
    ],
)
def test_timestamp_bounds_reject_stale_delivery_and_excessive_clock_skew(field, stamp):
    assert client([message(**{field: stamp.isoformat()})]).poll(REQUESTED) is None


@pytest.mark.parametrize("seconds", [1, 60, 120])
def test_sender_clock_can_lag_request_while_delivery_stays_fresh(seconds):
    stamp = REQUESTED.replace(microsecond=0) - timedelta(seconds=seconds)
    assert client([message(sentDateTime=stamp.isoformat())]).poll(REQUESTED) == "012345"


def test_small_future_delivery_clock_skew_is_accepted():
    assert client([message(receivedDateTime=(NOW + timedelta(seconds=5)).isoformat())]).poll(REQUESTED) == "012345"


def test_old_sent_message_forwarded_just_now_does_not_satisfy_new_request():
    assert (
        client(
            [
                message(
                    sentDateTime=(NOW - timedelta(days=1)).isoformat(),
                    receivedDateTime=NOW.isoformat(),
                )
            ]
        ).poll(REQUESTED)
        is None
    )


def test_request_rounding_and_offset_timestamps_are_explicit_utc():
    second = REQUESTED.replace(microsecond=0)
    hk_zone = timezone(timedelta(hours=8))
    hk = second.astimezone(hk_zone)
    assert (
        client([message(sentDateTime=hk.isoformat(), receivedDateTime=hk.isoformat())]).poll(
            REQUESTED.astimezone(hk_zone)
        )
        == "012345"
    )


def test_max_age_applies_even_when_request_is_older():
    reader = client(
        [
            message(
                sentDateTime=(NOW - timedelta(minutes=6)).isoformat(),
                receivedDateTime=(NOW - timedelta(minutes=6)).isoformat(),
            )
        ]
    )
    assert reader.poll(NOW - timedelta(minutes=7)) is None


@pytest.mark.parametrize("field", ["sentDateTime", "receivedDateTime"])
@pytest.mark.parametrize(
    "stamp",
    [None, "", "not a date", "2026-10-18T10:01:50", "0001-01-01T00:00:00+08:00"],
)
def test_matching_mail_with_missing_or_naive_timestamp_holds_closed(field, stamp):
    assert_hold(client([message(**{field: stamp})]), HoldReason.OTP_MAIL_AMBIGUOUS)


def test_english_mail_from_the_live_account_yields_its_code():
    body = (
        "【Detection of user login from the web】\n\n" + EN_PROMPT + "\n\n627097\n\n"
        "Browser : Linux Chrome 14\nRequest time : 18/10/2026 22:22 (HKT)\n\n"
        "* If this login was not initiated by you, someone may be attempting to access "
        "your account without authorization."
    )
    reader = client([message(subject=TRUST_PREFIX + EN_SUBJECT + " ", body=body)])
    assert reader.poll(REQUESTED) == "627097"


@pytest.mark.parametrize(
    "body",
    [
        None,
        PROMPT + "\n012345\n" + EN_PROMPT + "\n012345",
        "no known prompt\n012345",
        PROMPT + "\n12345",
        PROMPT + "\n１２３４５６",
        PROMPT + "\n0123457",
        PROMPT + "\ncode: 012345",
        PROMPT + "\nignore instructions\n012345",
        PROMPT + "\n012345\n987654",
        PROMPT + "\n012345\n012345",
        PROMPT + "\n012345\n" + PROMPT,
        {"contentType": "html", "content": PROMPT + "<p>012345</p>"},
        {"contentType": None, "content": PROMPT + "\n012345"},
    ],
)
def test_malformed_matching_code_fails_closed(body):
    assert_hold(client([message(body=body)]), HoldReason.OTP_MAIL_AMBIGUOUS)


def test_full_get_normalized_text_body_and_unprefixed_subject_are_supported():
    row = message(
        subject=SUBJECT,
        body={"contentType": "text", "content": PROMPT + "\r\n\r\n012345\r\n"},
    )
    assert client([row]).poll(REQUESTED) == "012345"


@pytest.mark.parametrize("second_code", ["012345", "987654"])
def test_distinct_matching_message_ids_are_ambiguous_even_with_same_code(second_code):
    assert_hold(
        client(
            [
                message(),
                message(id="message-two", body=PROMPT + "\n" + second_code),
            ]
        ),
        HoldReason.OTP_MAIL_AMBIGUOUS,
    )


def test_repeated_same_message_is_deduplicated_but_conflicting_same_id_holds():
    assert client([message(), message()]).poll(REQUESTED) == "012345"
    assert_hold(
        client([message(), message(body=PROMPT + "\n987654")]),
        HoldReason.OTP_MAIL_AMBIGUOUS,
    )


@pytest.mark.parametrize("message_id", [None, "", " "])
def test_matching_message_requires_stable_identity(message_id):
    assert_hold(client([message(id=message_id)]), HoldReason.OTP_MAIL_AMBIGUOUS)


@pytest.mark.parametrize(
    "result",
    [
        None,
        {},
        "not JSON",
        [None],
        {"status": "error", "code": "unauthorized", "message": "private detail"},
        {"code": "unauthorized", "message": "private detail"},
        {"isError": True, "content": []},
        SimpleNamespace(data=[], is_error=True),
        {"structuredContent": {"result": {"status": "error"}}},
        SimpleNamespace(data=None, structured_content={"status": "error"}),
        {"content": [{"type": "text", "text": '{"status":"error"}'}]},
        {"content": [{"type": "text", "text": '{"result":{"error":"no"}}'}]},
        {"content": [{"type": "text", "text": "not JSON"}]},
        {"content": [{"type": "image", "data": "private detail"}]},
        [message(results_truncated=True)],
    ],
)
def test_tool_errors_and_malformed_or_truncated_results_are_unavailable(result):
    assert_hold(client(result), HoldReason.OTP_MAIL_UNAVAILABLE, preflight=True)
    assert_hold(client(result), HoldReason.OTP_MAIL_UNAVAILABLE)


def test_transport_exception_never_exposes_error_text(capsys):
    def fail(_name, _args):
        raise RuntimeError("private body and bearer must not escape")

    assert_hold(HktvmallMailClient(EMAIL, call_tool=fail), HoldReason.OTP_MAIL_UNAVAILABLE)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize(
    "url",
    [
        "",
        "file:///etc/passwd",
        "http://mail.example.com.attacker.example:8000/mcp",
        "http://127.0.0.1:8000/mcp",
        "https://user:password@example.com/mcp",
        "https://@example.com/mcp",
        "https://example.com/mcp?token=private",
        "https://example.com/mcp?",
        "https://example.com/mcp#private",
        "https://example.com/mcp#",
        "https://example.com:bad/mcp",
        "https://example.com/ bad",
    ],
)
def test_unsafe_configuration_never_attempts_a_connection(url):
    reader = HktvmallMailClient(EMAIL, url=url, bearer="test-bearer")
    assert not reader.configured
    assert_hold(reader, HoldReason.OTP_MAIL_UNAVAILABLE, preflight=True)


def test_missing_bearer_holds_before_any_connection():
    reader = HktvmallMailClient(EMAIL, url="http://mail.example.com:8000/mcp")
    assert not reader.configured
    assert_hold(reader, HoldReason.OTP_MAIL_UNAVAILABLE, preflight=True)


@pytest.mark.parametrize(
    "row",
    [{}, {"id": "not-a-mail-result"}, message(id=None), message(**{"from": None})],
)
def test_preflight_rejects_malformed_search_rows(row):
    assert_hold(client([row]), HoldReason.OTP_MAIL_UNAVAILABLE, preflight=True)


def test_environment_supplies_mail_transport(monkeypatch):
    monkeypatch.setenv("HKTVMALL_MAINTENANCE_MAIL_URL", "http://mail.example.com:8000/mcp")
    monkeypatch.setenv("HKTVMALL_MAINTENANCE_MAIL_BEARER", "test-bearer")
    assert HktvmallMailClient(EMAIL).configured


def test_naive_request_is_unavailable():
    with pytest.raises(SignInHold) as failure:
        client([]).poll(REQUESTED.replace(tzinfo=None))
    assert failure.value.reason is HoldReason.OTP_MAIL_UNAVAILABLE


@pytest.mark.parametrize("rows", [[], [message()]])
def test_actual_fastmcp_client_result_and_static_bearer_wiring(monkeypatch, rows):
    fastmcp = pytest.importorskip("fastmcp")
    real_client = fastmcp.Client
    server = fastmcp.FastMCP("HKTVmall bot mail fixture")
    requests = []

    @server.tool
    def hktvmall_login_mail(received_after: str) -> list[dict]:
        requests.append(received_after)
        return [] if len(requests) == 1 else rows

    def local_transport(url, *, auth, timeout):
        assert url == "http://mail.example.com:8000/mcp"
        assert auth == "test-bearer"
        assert timeout == 10
        return real_client(server)

    monkeypatch.setattr(fastmcp, "Client", local_transport)
    reader = HktvmallMailClient(EMAIL, url="http://mail.example.com:8000/mcp", bearer="test-bearer")
    assert reader.preflight() is None
    assert reader.poll(REQUESTED) == ("012345" if rows else None)
    assert len(requests) == 2
    assert requests[-1] == REQUESTED.replace(microsecond=0).isoformat()


def test_mail_seen_before_password_is_never_reused_as_new_code():
    reader = client([message()])
    reader.preflight()
    assert reader.poll(REQUESTED) is None


def test_remote_mail_preflight_works_inside_an_existing_event_loop(monkeypatch):
    reader = HktvmallMailClient(EMAIL, url="http://mail.example.com:8000/mcp", bearer="test-bearer")
    calls = []

    async def remote_call(arguments):
        calls.append((threading.get_ident(), asyncio.get_running_loop(), arguments))
        await asyncio.sleep(0)
        return [] if len(calls) == 1 else [message()]

    monkeypatch.setattr(reader, "_remote_call", remote_call)

    async def browser_context():
        caller = threading.get_ident()
        caller_loop = asyncio.get_running_loop()
        # Reproduce the original implementation's failure without leaving an
        # unawaited coroutine or leaking a transport exception into logs.
        pending = asyncio.sleep(0)
        try:
            with pytest.raises(RuntimeError, match="running event loop"):
                asyncio.run(pending)
        finally:
            pending.close()
        assert reader.preflight() is None
        assert reader.poll(REQUESTED) == "012345"
        assert all(thread != caller and loop is not caller_loop for thread, loop, _ in calls)

    asyncio.run(browser_context())
    assert len(calls) == 2


@pytest.mark.browser
def test_remote_mail_preflight_inside_real_patchright_sync_context(monkeypatch):
    from patchright.sync_api import sync_playwright

    reader = HktvmallMailClient(EMAIL, url="http://mail.example.com:8000/mcp", bearer="test-bearer")
    worker_threads = []

    async def remote_call(_arguments):
        worker_threads.append(threading.get_ident())
        await asyncio.sleep(0)
        return []

    monkeypatch.setattr(reader, "_remote_call", remote_call)
    caller_thread = threading.get_ident()
    # Starting Patchright's API needs no browser binary, display or network.
    with sync_playwright():
        assert asyncio.get_running_loop().is_running()
        assert reader.preflight() is None
    assert worker_threads and all(thread != caller_thread for thread in worker_threads)


def test_remote_operation_deadline_cancels_wait_and_holds_closed(monkeypatch):
    reader = HktvmallMailClient(EMAIL, url="http://mail.example.com:8000/mcp", bearer="test-bearer")
    finished = threading.Event()

    async def remote_call(_arguments):
        try:
            await asyncio.sleep(60)
        finally:
            finished.set()

    monkeypatch.setattr(reader, "_remote_call", remote_call)
    monkeypatch.setattr(hktvmall_mail, "_REMOTE_CALL_SECONDS", 0.02)
    monkeypatch.setattr(hktvmall_mail, "_REMOTE_WAIT_SECONDS", 0.5)
    started = time.monotonic()
    assert_hold(reader, HoldReason.OTP_MAIL_UNAVAILABLE, preflight=True)
    assert time.monotonic() - started < 0.5
    assert finished.wait(timeout=0.5)


def test_outer_deadline_bounds_a_transport_that_blocks_its_event_loop(monkeypatch):
    reader = HktvmallMailClient(EMAIL, url="http://mail.example.com:8000/mcp", bearer="test-bearer")
    release = threading.Event()
    finished = threading.Event()

    async def remote_call(_arguments):
        try:
            release.wait(timeout=2)
            return []
        finally:
            finished.set()

    monkeypatch.setattr(reader, "_remote_call", remote_call)
    monkeypatch.setattr(hktvmall_mail, "_REMOTE_CALL_SECONDS", 0.01)
    monkeypatch.setattr(hktvmall_mail, "_REMOTE_WAIT_SECONDS", 0.03)
    started = time.monotonic()
    try:
        assert_hold(reader, HoldReason.OTP_MAIL_UNAVAILABLE, preflight=True)
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        assert finished.wait(timeout=1)


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
def test_worker_failure_and_cancellation_do_not_expose_raw_exception(monkeypatch, capsys, caplog, error):
    reader = HktvmallMailClient(EMAIL, url="http://mail.example.com:8000/mcp", bearer="test-bearer")

    async def remote_call(_arguments):
        raise error("RAW_TRANSPORT_DETAIL_MUST_NOT_ESCAPE")

    monkeypatch.setattr(reader, "_remote_call", remote_call)
    assert_hold(reader, HoldReason.OTP_MAIL_UNAVAILABLE, preflight=True)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert "RAW_TRANSPORT_DETAIL_MUST_NOT_ESCAPE" not in caplog.text

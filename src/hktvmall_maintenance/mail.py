"""Read one fresh HKTVmall sign-in code from the bot mailbox.

Mail remains untrusted data. Neither bodies nor codes are retained on the
client, logged, or included in exceptions. The browser owns the wait budget
and may request a code only once; this reader never sends or changes mail.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
from threading import Thread
from typing import Any
from urllib.parse import urlsplit

from .outcomes import HoldReason, SignInHold

# HKTVmall writes the code mail in the account's language. The English subject
# arrives with a trailing space, so subjects compare after rstrip().
_SUBJECTS = frozenset(
    {
        "[HKTVmall] 偵測到用戶從網頁版登入",
        "[HKTVmall] New website login on HKTVmall platform",
    }
)
_PROMPTS = (
    "請輸入以下的一次性驗證密碼以完成登入驗證。",
    "Please enter below one-time password to complete the login process on the web version.",
)
_TRUST_PREFIX = "[UNTRUSTED_EXTERNAL_CONTENT] "
_SENDER_DOMAINS = frozenset({"hktv.com.hk", "hktvmall.com"})
_MAX_AGE = timedelta(minutes=5)
_PREFLIGHT_AGE = timedelta(minutes=4)
_SENDER_CLOCK_SKEW = timedelta(minutes=2)
_RECEIVED_CLOCK_SKEW = timedelta(seconds=5)
_LIMIT = 20
_MAX_BODY_CHARS = 65536
_REMOTE_CALL_SECONDS = 15.0
_REMOTE_WAIT_SECONDS = 16.0
_ADDRESS = re.compile(r"[^@\s<>]+@[A-Za-z0-9.-]+")
_CODE = re.compile(r"[0-9]{6}")


def _now() -> datetime:
    return datetime.now(UTC)


def _unavailable() -> SignInHold:
    return SignInHold(HoldReason.OTP_MAIL_UNAVAILABLE)


def _ambiguous() -> SignInHold:
    return SignInHold(HoldReason.OTP_MAIL_AMBIGUOUS)


def _valid_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return bool(
            parsed.hostname
            and parsed.port != 0
            and parsed.username is None
            and parsed.password is None
            and "?" not in value
            and "#" not in value
            and not any(char.isspace() for char in value)
            and (
                parsed.scheme == "https"
                or (
                    parsed.scheme == "http"
                    and parsed.hostname in os.environ.get("HKTVMALL_MAINTENANCE_MAIL_HTTP_HOSTS", "").split(",")
                )
            )
        )
    except (ValueError, OverflowError):
        return False


def _address(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("emailAddress", value)
        if isinstance(value, dict):
            value = value.get("address")
    if not isinstance(value, str):
        return ""
    value = value.strip().casefold()
    return value if _ADDRESS.fullmatch(value) else ""


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _check_error(value: Mapping[str, Any]) -> None:
    if (
        value.get("isError")
        or value.get("is_error")
        or value.get("error")
        or value.get("status") == "error"
        or ("code" in value and "message" in value)
    ):
        raise _unavailable()


def _rows(result: object, *, depth: int = 0) -> list[dict[str, Any]]:
    """Decode FastMCP 4, protocol, and text-only tool results fail closed."""

    if depth > 6:
        raise _unavailable()
    if isinstance(result, list):
        if not result:
            return []
        if all(isinstance(item, dict) and "type" not in item for item in result):
            for item in result:
                _check_error(item)
            return result
        # A list return may be represented by one JSON list or by one text
        # block per message. No image/resource blocks belong in mail_search.
        decoded: list[dict[str, Any]] = []
        for item in result:
            kind = item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
            text = item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
            if kind != "text" or not isinstance(text, str):
                raise _unavailable()
            try:
                payload = json.loads(text)
            except (TypeError, ValueError):
                raise _unavailable() from None
            if isinstance(payload, dict) and "id" in payload:
                _check_error(payload)
                decoded.append(payload)
            else:
                decoded.extend(_rows(payload, depth=depth + 1))
        return decoded
    if isinstance(result, dict):
        _check_error(result)
        for key in ("data", "structured_content", "structuredContent", "result"):
            if result.get(key) is not None:
                return _rows(result[key], depth=depth + 1)
        if "content" in result:
            return _rows(result["content"], depth=depth + 1)
        raise _unavailable()
    if getattr(result, "is_error", False) or getattr(result, "isError", False):
        raise _unavailable()
    for name in ("data", "structured_content", "structuredContent", "content"):
        payload = getattr(result, name, None)
        if payload is not None:
            return _rows(payload, depth=depth + 1)
    raise _unavailable()


def _code(message: dict[str, Any]) -> str:
    body = message.get("body")
    if isinstance(body, dict):
        content_type = body.get("contentType")
        body = body.get("content") if isinstance(content_type, str) and content_type.lower() == "text" else None
    if not isinstance(body, str) or len(body) > _MAX_BODY_CHARS:
        raise _ambiguous()
    prompts = [prompt for prompt in _PROMPTS for _ in range(body.count(prompt))]
    if len(prompts) != 1:
        raise _ambiguous()
    remaining = body.partition(prompts[0])[2]
    lines = [line.strip() for line in remaining.splitlines() if line.strip()]
    codes = [line for line in lines if _CODE.fullmatch(line)]
    if len(codes) != 1 or not lines or lines[0] != codes[0]:
        raise _ambiguous()
    return codes[0]


class HktvmallMailClient:
    """A bounded read-only MCP adapter, independent of browser sign-in policy."""

    def __init__(
        self,
        expected_email: str,
        *,
        url: str | None = None,
        bearer: str | None = None,
        call_tool: Callable[[str, dict], object] | None = None,
    ) -> None:
        self._expected_email = _address(expected_email)
        self._url = os.environ.get("HKTVMALL_MAINTENANCE_MAIL_URL", "") if url is None else url
        self._bearer = (os.environ.get("HKTVMALL_MAINTENANCE_MAIL_BEARER", "") if bearer is None else bearer).strip()
        self._call_tool = call_tool
        self._preflight_ids: set[str] = set()

    @property
    def configured(self) -> bool:
        return bool(self._expected_email and (self._call_tool is not None or (self._bearer and _valid_url(self._url))))

    def _search(self, after: datetime, *, limit: int) -> list[dict[str, Any]]:
        if not self.configured:
            raise _unavailable()
        arguments = {
            "received_after": after.isoformat(),
        }
        try:
            if self._call_tool is not None:
                result = self._call_tool("hktvmall_login_mail", arguments)
            else:
                result = self._remote_sync(arguments)
            rows = _rows(result)
            if len(rows) > limit or any(row.get("results_truncated") for row in rows):
                raise _unavailable()
            return rows
        except SignInHold:
            raise
        except Exception:
            # Transport failures may contain credentials, bodies or codes.
            raise _unavailable() from None

    def _remote_sync(self, arguments: dict[str, Any]) -> object:
        # Patchright's synchronous API keeps its asyncio loop running in a
        # greenlet on this thread. Always isolate the mail transport's loop;
        # asyncio.run here would otherwise fail before making any MCP call.
        result: Future[object] = Future()

        async def bounded_call() -> object:
            async with asyncio.timeout(_REMOTE_CALL_SECONDS):
                return await self._remote_call(arguments)

        def run() -> None:
            try:
                result.set_result(asyncio.run(bounded_call()))
            except BaseException:
                # Never retain raw transport exceptions, which can include
                # bearer values or mail content, in the cross-thread result.
                result.set_exception(_unavailable())

        Thread(target=run, name="hktvmall-mail", daemon=True).start()
        # The extra outer bound also covers transport startup and cleanup
        # should a library fail to honour asyncio cancellation. A daemon
        # worker cannot keep this scheduled keeper process alive afterward.
        return result.result(timeout=_REMOTE_WAIT_SECONDS)

    async def _remote_call(self, arguments: dict[str, Any]) -> object:
        # The normal CLI deliberately does not require the optional MCP extra.
        from fastmcp import Client

        async with Client(self._url, auth=self._bearer, timeout=10) as client:
            return await client.call_tool("hktvmall_login_mail", arguments, raise_on_error=False)

    def preflight(self) -> None:
        """Prove the bot-mail transport works before any password submission."""

        # Leave transport slack at the server's five-minute query boundary.
        rows = self._search((_now() - _PREFLIGHT_AGE).replace(microsecond=0), limit=_LIMIT)
        for row in rows:
            if (
                not isinstance(row.get("id"), str)
                or not row["id"].strip()
                or not isinstance(row.get("subject"), str)
                or not _address(row.get("from"))
                or not isinstance(row.get("toRecipients"), list)
                or _timestamp(row.get("receivedDateTime")) is None
            ):
                raise _unavailable()
        self._preflight_ids.update(row["id"] for row in rows)

    def poll(self, requested_at: datetime) -> str | None:
        """Return exactly one fresh account-bound code, without resending."""

        if requested_at.utcoffset() is None:
            raise _unavailable()
        after = requested_at.astimezone(UTC).replace(microsecond=0)
        now = _now()
        candidates: dict[str, str] = {}
        for message in self._search(after, limit=_LIMIT):
            if isinstance(message.get("id"), str) and message["id"] in self._preflight_ids:
                continue
            sender = _address(message.get("from"))
            if not sender or sender.rpartition("@")[2] not in _SENDER_DOMAINS:
                continue
            subject = message.get("subject")
            if isinstance(subject, str):
                subject = subject.removeprefix(_TRUST_PREFIX).rstrip()
            if subject not in _SUBJECTS or message.get("isDraft"):
                continue
            recipients = message.get("toRecipients")
            if not isinstance(recipients, list) or self._expected_email not in {
                _address(recipient) for recipient in recipients
            }:
                continue
            sent = _timestamp(message.get("sentDateTime"))
            received = _timestamp(message.get("receivedDateTime"))
            if sent is None or received is None:
                raise _ambiguous()
            # Delivery must follow this request. Only the sender's clock has
            # a wider allowance; an old message forwarded now still expires.
            if (
                received < after
                or received < now - _MAX_AGE
                or received > now + _RECEIVED_CLOCK_SKEW
                or sent < after - _SENDER_CLOCK_SKEW
                or sent < now - _MAX_AGE
                or sent > now + _SENDER_CLOCK_SKEW
            ):
                continue
            message_id = message.get("id")
            if not isinstance(message_id, str) or not message_id.strip():
                raise _ambiguous()
            code = _code(message)
            if message_id in candidates and candidates[message_id] != code:
                raise _ambiguous()
            candidates[message_id] = code
        if len(candidates) > 1:
            raise _ambiguous()
        return next(iter(candidates.values()), None)


__all__ = ["HktvmallMailClient"]

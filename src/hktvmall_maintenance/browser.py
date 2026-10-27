"""HKTVmall's bounded password, checkbox and forwarded-email sign-in.

The shared keeper owns holds, the daily credential budget and publication.
The driver's only challenge actions are the reCAPTCHA checkbox and the one
image challenge it may open, solved through the bounded visual solver; the
driver never requests a second code, writes a basket, or changes account
settings. Only positive signed-out evidence can request a new login; a warm
snapshot proves the account as well as liveness.
The password POST's own response decides the next step: the observed
``2FA_REQUIRED`` answer continues to the emailed code, a redirect back to the
login page is a rejected credential, and a redirect elsewhere on the shop ends
the sign-in, which the keeper's independent warm-up must then prove.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from signin_window.automation.browser import BrowserSession as PatchrightMintContext
from signin_window.automation.plans import VisionPlanner
from signin_window.automation.pointer import X11Pointer
from signin_window.automation.recaptcha import RecaptchaImageSelectSolver
from signin_window.automation.secret_filler import configured_credential_filler
from signin_window.state import atomic_private_write

from .config import HktvmallTargetSpec
from .cookies import ExportCookie, HeaderTemplate, select_export_cookies
from .mail import HktvmallMailClient
from .outcomes import (
    AccountMismatch,
    BlockReason,
    HoldReason,
    ProviderBlocked,
    SessionDead,
    SignInHold,
    VerifyFailed,
    VerifyFailure,
)
from .runtime import PatchrightRuntime

SecretField = Literal["username", "password", "totp"]

ORIGIN = "https://www.hktvmall.com"
LOGIN_PATH = "/hktv/en/j_spring_security_check"
OTP_REQUEST_PATH = "/hktv/en/ajax/login/two-factor-authentication/request"
OTP_VERIFY_PATH = "/hktv/en/ajax/login/two-factor-authentication/verify"
_OTP_DELIVERY_METHODS = frozenset({"EMAIL_ONLY", "HSS_ONLY", "HSS_AND_EMAIL"})
_log = logging.getLogger(__name__)
_LOGGED_IN = re.compile(r"['\"]loggedin['\"]\s*:\s*(true|false)")
_CONTACT_EMAIL = re.compile(r"""(?<!\\)(["'])realContactEmail\1\s*:\s*("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')""")
_BLOCK_MARKERS = (
    "automated queries",
    "try again later",
    "unusual traffic",
    "your computer or network",
)


def account_from_html(html: str) -> tuple[bool | None, str | None]:
    """Read the site's bootstrap fields; never infer an account from text."""

    flags = {match.group(1) for match in _LOGGED_IN.finditer(html)}
    logged_in = next(iter(flags)) == "true" if len(flags) == 1 else None
    emails = set()
    for match in _CONTACT_EMAIL.finditer(html):
        try:
            email = ast.literal_eval(match.group(2))
        except (ValueError, SyntaxError):
            continue
        if isinstance(email, str) and "@" in email:
            emails.add(email.strip().casefold())
    return logged_in, next(iter(emails)) if len(emails) == 1 else None


class HktvmallTargetBrowser:
    def __init__(
        self,
        browser: PatchrightMintContext,
        spec: HktvmallTargetSpec,
        *,
        expected_email: str,
        mail: HktvmallMailClient,
        evidence_dir: Path,
        secrets: Any | None = None,
        planner: VisionPlanner | None = None,
        utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._browser = browser
        self._page = browser.page
        self._spec = spec
        self._expected_email = expected_email.strip().casefold()
        self._mail = mail
        self._evidence_dir = evidence_dir
        self._secrets = secrets or configured_credential_filler(
            os.environ.get("HKTVMALL_MAINTENANCE_CREDENTIAL_READER", "")
        )
        self._utc_now = utc_now
        self._clock = monotonic
        self._counts = {LOGIN_PATH: 0, OTP_REQUEST_PATH: 0, OTP_VERIFY_PATH: 0}
        self._login_enabled = False
        self._verify_enabled = False
        self._requested_at: datetime | None = None
        self._responses: dict[str, str] = {}
        self._otp_delivery_method = "unknown"
        # One bounded visual attempt per sign-in; the typed hold stays the
        # only answer to a second challenge or a failed attempt.
        self._solver = (
            RecaptchaImageSelectSolver(
                page=self._page,
                find_frame=self._frame,
                visible=self._visible,
                planner=planner,
                clock=self._clock,
            )
            if planner is not None
            else None
        )
        self._image_challenge_attempted = False
        self._page.set_default_timeout(12_000)
        browser.context.route("**/*", self._guard)
        self._page.on("response", self._observe_response)

    def _guard(self, route: Any) -> None:
        request = route.request
        parts = urlsplit(request.url)
        host = parts.hostname or ""
        if parts.scheme != "https":
            route.abort("blockedbyclient")
            return
        shop = host == "hktvmall.com" or host.endswith(".hktvmall.com")
        if not shop:
            # A top-level departure is never an identity continuation. Child
            # reCAPTCHA frames and their assets use Google's own origins.
            if request.resource_type == "document" and request.frame == self._page.main_frame:
                route.abort("blockedbyclient")
            elif (
                (
                    parts.scheme == "https"
                    and host
                    in {
                        "www.google.com",
                        "www.recaptcha.net",
                        "recaptcha.google.com",
                        "www.gstatic.com",
                    }
                )
                or request.method in {"GET", "HEAD"}
                and request.resource_type in {"script", "stylesheet", "image", "font", "media"}
            ):
                route.continue_()
            else:
                route.abort("blockedbyclient")
            return
        allowed = request.method in {"GET", "HEAD"} and request.resource_type in {
            "document",
            "script",
            "stylesheet",
            "image",
            "font",
            "media",
        }
        if (
            request.method == "POST"
            and parts.scheme == "https"
            and host == "www.hktvmall.com"
            and parts.path in self._counts
        ):
            # Counts alone do not enforce ordering: a hostile or drifted page
            # must not request a code before dispatching the password, or
            # verify before the one permitted request has succeeded.
            permitted = self._login_enabled
            if parts.path == OTP_REQUEST_PATH:
                permitted = permitted and self._counts[LOGIN_PATH] == 1
            elif parts.path == OTP_VERIFY_PATH:
                permitted = (
                    permitted
                    and self._verify_enabled
                    and self._counts[OTP_REQUEST_PATH] == 1
                    and self._responses.get(OTP_REQUEST_PATH) == "success"
                )
            allowed = permitted and self._counts[parts.path] == 0
            if allowed:
                self._counts[parts.path] += 1
                if parts.path == OTP_REQUEST_PATH:
                    self._requested_at = self._utc_now()
        if allowed:
            route.continue_()
        else:
            route.abort("blockedbyclient")

    def _observe_response(self, response: Any) -> None:
        parts = urlsplit(response.url)
        if parts.scheme != "https" or parts.hostname != "www.hktvmall.com" or parts.path not in self._counts:
            return
        if parts.path == LOGIN_PATH:
            self._observe_login(response)
            return
        if parts.path == OTP_REQUEST_PATH and response.request.method != "POST":
            return
        # "unreadable" is defensive: Chromium drops a response body once its
        # page navigates. The live verify answer was read before the site
        # navigated, but a faster redirect must not turn a working code into a
        # hold. A body that was read and is not a success JSON is a rejection.
        status = "rejected"
        delivery_method = "unknown"
        try:
            if response.status == 200:
                try:
                    body = response.body()
                except Exception:
                    status = "unreadable"
                else:
                    with suppress(ValueError):
                        payload = json.loads(body)
                        answer = payload.get("status") if isinstance(payload, dict) else {}
                        if isinstance(answer, dict) and answer.get("code") == "success":
                            status = "success"
                            data = payload.get("data")
                            method = data.get("twoFactorType") if isinstance(data, dict) else None
                            if isinstance(method, str) and method in _OTP_DELIVERY_METHODS:
                                delivery_method = method
        except Exception:
            status = "unreadable"
        # Retain no UID, code, token, raw response or arbitrary error message.
        self._responses[parts.path] = status
        if parts.path == OTP_REQUEST_PATH:
            self._otp_delivery_method = delivery_method
            self._record_otp_delivery(status, delivery_method)

    def _record_otp_delivery(self, status: str, method: str) -> None:
        """Retain closed delivery facts, never the UID, OTP or response body.

        The site's request accepts reCAPTCHA and CSRF fields only; its answer
        selects EMAIL_ONLY, HSS_ONLY or HSS_AND_EMAIL. Its email/message labels
        are explanations, not delivery selectors. Success is not mail delivery.
        """

        record = {
            "schema_version": 1,
            "observed_at": self._utc_now().isoformat(),
            "requested_at": (self._requested_at.isoformat() if self._requested_at else None),
            "response_status": status,
            "delivery_method": method,
        }
        try:
            self._evidence_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            atomic_private_write(
                self._evidence_dir / f"otp-request-{uuid4().hex}.json",
                json.dumps(record).encode(),
            )
        except OSError:
            # Diagnostic persistence must neither resend nor reject a code.
            _log.warning("hktvmall_otp_delivery_evidence_unavailable")

    def _observe_login(self, response: Any) -> None:
        """Reduce the password POST's answer to one closed status.

        Only ``2FA_REQUIRED`` has been observed live. A rejected password and
        a direct success follow Spring Security's redirect convention.
        """

        status = "unrecognised"
        try:
            # The guard also lets any shop document be read with GET.
            if response.request.method != "POST":
                return
            code = response.status
            if 300 <= code < 400:
                # Playwright reports each redirect hop as its own response.
                headers = response.headers
                location = headers.get("location") if isinstance(headers, dict) else None
                if isinstance(location, str) and location:
                    target = urlsplit(urljoin(response.url, location))
                    if target.scheme == "https" and target.hostname == "www.hktvmall.com":
                        login_paths = {urlsplit(self._spec.login_url).path, LOGIN_PATH}
                        status = "rejected" if target.path in login_paths else "redirected"
            elif code == 200:
                payload = response.json()
                # Any other result is unobserved: hold it for a person rather
                # than guess that the password was wrong.
                if isinstance(payload, dict) and payload.get("result") == "2FA_REQUIRED":
                    status = "2fa_required"
        except Exception:
            pass
        # Retain no location, token, account or response body.
        self._responses[LOGIN_PATH] = status

    def _navigate(self, url: str) -> None:
        try:
            response = self._page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        except Exception as error:
            raise VerifyFailed(VerifyFailure.PAGE_INVALID) from error
        if response is None:
            raise VerifyFailed(VerifyFailure.PAGE_INVALID)
        if response.status == 429:
            raise ProviderBlocked(BlockReason.RATE_LIMITED)
        if response.status == 403:
            raise ProviderBlocked(BlockReason.ACCESS_DENIED)
        parts = urlsplit(self._page.url)
        if response.status != 200 or parts.scheme != "https" or parts.hostname != "www.hktvmall.com":
            raise VerifyFailed(VerifyFailure.PAGE_INVALID)

    def warm(self) -> str:
        self._navigate(self._spec.warm_url)
        logged_in, email = account_from_html(self._page.content())
        if logged_in is False:
            raise SessionDead()
        if logged_in is not True or email is None:
            raise VerifyFailed(VerifyFailure.PAGE_INVALID)
        if email != self._expected_email:
            raise AccountMismatch()
        return email

    def _frame(self, path: str) -> Any | None:
        for frame in self._page.frames:
            parts = urlsplit(frame.url)
            if parts.scheme != "https" or parts.path != path:
                continue
            if path.startswith("/recaptcha/"):
                if parts.hostname not in {"www.google.com", "www.recaptcha.net"}:
                    continue
            elif parts.scheme != "https" or parts.hostname != "www.hktvmall.com":
                continue
            return frame
        return None

    def _visible(self, locator: Any) -> bool:
        if not locator.count() or not locator.first.is_visible():
            return False
        box = locator.first.bounding_box()
        size = self._page.evaluate("({width:innerWidth,height:innerHeight})")
        return bool(
            box
            and box["x"] + box["width"] > 0
            and box["y"] + box["height"] > 0
            and box["x"] < size["width"]
            and box["y"] < size["height"]
        )

    def _click_checkbox(self, locator: Any) -> None:
        if not self._visible(locator):
            raise SignInHold(HoldReason.UNKNOWN_FLOW)
        box = locator.bounding_box()
        pointer = X11Pointer(self._page, display=getattr(self._browser, "display", None))
        try:
            pointer.move(
                box["x"] + box["width"] / 2,
                box["y"] + box["height"] / 2,
                steps=8,
            )
            pointer.down()
            pointer.up()
        finally:
            pointer.close()

    def _challenge_hold(self, frame: Any, *, captured_visible: bool | None = None) -> None:
        body = frame.locator("body").inner_text(timeout=5_000).casefold()
        if any(marker in body for marker in _BLOCK_MARKERS):
            raise ProviderBlocked(BlockReason.CAPTCHA)
        challenge = frame.locator("#rc-imageselect")
        if captured_visible is False or not self._visible(challenge):
            return
        # Capture only the challenge, never the account/credential/OTP form.
        with suppress(Exception):
            self._evidence_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            atomic_private_write(
                self._evidence_dir / f"captcha-{uuid4().hex}.png",
                challenge.screenshot(timeout=5_000),
            )
        raise SignInHold(HoldReason.CAPTCHA_IMAGE_REQUIRED)

    def sign_in(self) -> None:
        self._image_challenge_attempted = False
        # The keeper's readiness check has already proven the mail transport
        # on this same client, before reserving the daily budget.
        self._navigate(self._spec.login_url)
        self._page.get_by_role("button", name="Email Address", exact=True).click()
        form = self._page.locator("form#loginForm").filter(has=self._page.locator("#j_username_email"))
        if form.get_attribute("action") not in {LOGIN_PATH, ORIGIN + LOGIN_PATH}:
            raise SignInHold(HoldReason.UNKNOWN_FLOW)
        fields: tuple[tuple[SecretField, str], ...] = (
            ("username", "#j_username_email"),
            ("password", "#j_pwd_email"),
        )
        for field, selector in fields:
            self._secrets.fill(
                self._browser,
                secret_ref=self._spec.secret_ref,
                field=field,
                selectors=(selector,),
                allowed_origins=frozenset({ORIGIN}),
            )
            if field == "username" and (
                self._page.locator(selector).input_value().strip().casefold() != self._expected_email
            ):
                raise SignInHold(HoldReason.ACCOUNT_MISMATCH)
        self._login_enabled = True
        form.locator("button[type=submit],input[type=submit]").click()
        if self._await_code_request() == "authenticated":
            self._login_enabled = False
            return
        if self._requested_at is None:
            raise SignInHold(HoldReason.OTP_REQUEST_REJECTED)
        if self._otp_delivery_method == "HSS_ONLY":
            # Email is the keeper's supported channel. The site offered no
            # selector: do not misreport a message-only code as missing mail.
            raise SignInHold(HoldReason.SECOND_FACTOR_UNSUPPORTED)
        deadline = self._clock() + 150
        code = None
        mail_unavailable = False
        while self._clock() < deadline:
            try:
                code = self._mail.poll(self._requested_at)
            except SignInHold as hold:
                # A transport failure may be transient within the code's
                # lifetime; an ambiguous mailbox is never retried.
                if hold.reason is not HoldReason.OTP_MAIL_UNAVAILABLE:
                    raise
                mail_unavailable = True
            else:
                mail_unavailable = False
                if code is not None:
                    break
            self._page.wait_for_timeout(3_000)
        if code is None:
            raise SignInHold(HoldReason.OTP_MAIL_UNAVAILABLE if mail_unavailable else HoldReason.OTP_MAIL_TIMEOUT)
        if not re.fullmatch(r"[0-9]{6}", code):
            raise SignInHold(HoldReason.OTP_MAIL_AMBIGUOUS)
        try:
            modal = self._frame("/hktv/en/login/two-factor-authentication")
            if modal is None:
                raise SignInHold(HoldReason.UNKNOWN_FLOW)
            inputs = modal.locator(".account-2fa-codeInput")
            if inputs.count() != 6:
                raise SignInHold(HoldReason.UNKNOWN_FLOW)
            for index, digit in enumerate(code):
                inputs.nth(index).fill(digit)
            self._verify_enabled = True
            modal.locator(".account-2fa-otp-codeInput-submitButton").click()
        finally:
            del code
        deadline = self._clock() + 20
        while OTP_VERIFY_PATH not in self._responses and self._clock() < deadline:
            self._page.wait_for_timeout(250)
        if self._responses.get(OTP_VERIFY_PATH) not in {"success", "unreadable"}:
            raise SignInHold(HoldReason.OTP_REJECTED)
        # An unreadable answer is not proof either way: the keeper's warm()
        # decides, and a sign-in that did not take is never repeated that day.
        # Let the site's successful verification redirect finish before warm.
        self._page.wait_for_timeout(3_000)

    def _await_code_request(self) -> Literal["authenticated", "otp_requested"]:
        deadline = self._clock() + 45
        checked = False
        while self._clock() < deadline:
            login = self._responses.get(LOGIN_PATH)
            if login == "rejected":
                raise SignInHold(HoldReason.CREDENTIAL_REJECTED)
            if login == "unrecognised":
                raise SignInHold(HoldReason.UNKNOWN_FLOW)
            if login == "redirected":
                # A redirect proves nothing about the account. The keeper
                # runs warm() independently before publishing cookies.
                return "authenticated"
            if OTP_REQUEST_PATH in self._responses:
                if self._responses[OTP_REQUEST_PATH] != "success":
                    raise SignInHold(HoldReason.OTP_REQUEST_REJECTED)
                return "otp_requested"
            frame = self._frame("/recaptcha/api2/bframe")
            # Google preloads this frame before the checkbox opens the grid.
            # A hidden frame must not spend the one bounded solve attempt.
            if frame is not None:
                visible = self._visible(frame.locator("#rc-imageselect"))
                if visible and self._solver is not None and not self._image_challenge_attempted:
                    self._image_challenge_attempted = True
                    if self._solver.solve():
                        # The challenge cleared; the code request follows on
                        # its own, so the wait restarts on a full window.
                        deadline = self._clock() + 45
                        continue
                self._challenge_hold(frame, captured_visible=visible)
            anchor = self._frame("/recaptcha/api2/anchor")
            if anchor is not None and not checked:
                checkbox = anchor.locator("#recaptcha-anchor")
                if self._visible(checkbox):
                    checked = True
                    self._click_checkbox(checkbox)
            modal = self._frame("/hktv/en/login/two-factor-authentication")
            if modal is not None and self._visible(modal.locator(".account-2fa-error-alert-section")):
                raise SignInHold(HoldReason.ACCOUNT_ALERT)
            self._page.wait_for_timeout(250)
        raise SignInHold(HoldReason.TIMEOUT)

    def export_cookies(self) -> list[ExportCookie]:
        cookies = self._browser.context.cookies()
        return select_export_cookies(
            cookies,
            hosts=frozenset({"hktvmall.com", "www.hktvmall.com"}),
            names=frozenset(str(c["name"]) for c in cookies),
        )

    def request_headers(self) -> dict[str, HeaderTemplate]:
        return {}

    def user_agent(self) -> str:
        return self._browser.user_agent()


@contextmanager
def open_hktvmall_browser(
    spec: HktvmallTargetSpec,
    *,
    profile: Path,
    expected_email: str,
    mail: HktvmallMailClient,
    runtime: PatchrightRuntime | None = None,
    planner: VisionPlanner | None = None,
) -> Iterator[HktvmallTargetBrowser]:
    selected_runtime = runtime or PatchrightRuntime()
    try:
        with selected_runtime.open(
            profile, require_virtual_display=True, launch_config=spec.launch_config()
        ) as browser:
            yield HktvmallTargetBrowser(
                browser,
                spec,
                expected_email=expected_email,
                mail=mail,
                evidence_dir=profile.parent / "challenge-evidence",
                planner=planner,
            )
    finally:
        selected_runtime.close()

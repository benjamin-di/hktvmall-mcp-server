"""Bounded HKTVmall sign-in, account proof and identity-keeper contracts."""

from __future__ import annotations

import json as jsonlib
import stat
import sys
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

if sys.platform != "linux":
    pytest.skip("optional maintenance requires Linux", allow_module_level=True)

from signin_window.sessions import AttemptBudget

from hktvmall_maintenance.browser import (
    LOGIN_PATH,
    ORIGIN,
    OTP_REQUEST_PATH,
    OTP_VERIFY_PATH,
    HktvmallTargetBrowser,
    account_from_html,
)
from hktvmall_maintenance.config import HktvmallTargetSpec, load_identity_target
from hktvmall_maintenance.keeper import IdentityKeeper
from hktvmall_maintenance.models import PersistenceError, ProviderReadiness
from hktvmall_maintenance.outcomes import (
    AccountMismatch,
    BlockReason,
    HoldReason,
    IdentityOutcome,
    ProviderBlocked,
    SessionDead,
    SignInHold,
    VerifyFailed,
)
from hktvmall_maintenance.state import TargetStateStore


def CredentialBudget(root, **kwargs):
    return AttemptBudget(root, error_type=PersistenceError, **kwargs)


EMAIL = "owner@example.com"
NOW = datetime(2026, 10, 18, 10, tzinfo=UTC)
ACCOUNT_HTML = '{"loggedin":true,"realContactEmail":"owner@example.com"}'
LOGIN_PAGE = ORIGIN + "/hktv/en/login"


def response(url, *, status=200, payload=None, location=None, method="POST"):
    def json():
        if isinstance(payload, Exception):
            raise payload
        return payload

    def body():
        if isinstance(payload, Exception):
            raise payload
        return payload if isinstance(payload, bytes) else jsonlib.dumps(payload).encode()

    return SimpleNamespace(
        url=url,
        status=status,
        json=json,
        body=body,
        headers={} if location is None else {"location": location},
        request=SimpleNamespace(method=method),
    )


TWO_FACTOR = response(ORIGIN + LOGIN_PATH, payload={"result": "2FA_REQUIRED"})


class Route:
    def __init__(self, url, *, method="GET", resource_type="xhr", frame=None):
        self.request = SimpleNamespace(url=url, method=method, resource_type=resource_type, frame=frame)
        self.allowed = False
        self.aborted = False

    def continue_(self):
        assert not self.aborted
        self.allowed = True

    def abort(self, reason):
        assert reason == "blockedbyclient"
        assert not self.allowed
        self.aborted = True


class Locator:
    def __init__(self, *, click=None, text="", visible=False, screenshot=None):
        self.first = self
        self._click = click or (lambda: None)
        self._text = text
        self._visible = visible
        self._screenshot = screenshot
        self.values = []

    def click(self):
        self._click()

    def count(self):
        return int(self._visible)

    def is_visible(self):
        return self._visible

    def bounding_box(self):
        return {"x": 10, "y": 10, "width": 120, "height": 120}

    def inner_text(self, **_kwargs):
        return self._text

    def screenshot(self, **_kwargs):
        assert self._screenshot is not None
        return self._screenshot

    def filter(self, **_kwargs):
        return self

    def get_attribute(self, name):
        assert name == "action"
        return LOGIN_PATH

    def locator(self, selector):
        assert selector == "button[type=submit],input[type=submit]"
        return self

    def input_value(self):
        return EMAIL

    def fill(self, value):
        self.values.append(value)


class CodeInputs:
    def __init__(self):
        self.inputs = [Locator() for _ in range(6)]

    def count(self):
        return len(self.inputs)

    def nth(self, index):
        return self.inputs[index]


class Page:
    def __init__(self):
        self.main_frame = object()
        self.frames = []
        self.html = ACCOUNT_HTML
        self.url = ORIGIN + "/hktv/en/"
        self.status = 200
        self.elapsed = 0.0
        self.password_clicks = 0
        self.code_inputs = CodeInputs()
        self.after_password = lambda: None
        self.after_verify = lambda: None

    def set_default_timeout(self, _timeout):
        pass

    def on(self, event, callback):
        assert event == "response"
        self.on_response = callback

    def goto(self, url, **_kwargs):
        self.url = url
        return SimpleNamespace(status=self.status)

    def content(self):
        return self.html

    def evaluate(self, _expression):
        return {"width": 1600, "height": 1000}

    def wait_for_timeout(self, milliseconds):
        self.elapsed += milliseconds / 1000

    def get_by_role(self, role, *, name, exact):
        assert (role, name, exact) == ("button", "Email Address", True)
        return Locator()

    def locator(self, selector):
        if selector == "form#loginForm":

            def password():
                self.password_clicks += 1
                self.after_password()

            return Locator(click=password)
        assert selector in {"#j_username_email", "#j_pwd_email"}
        return Locator()


class Mail:
    configured = True

    def __init__(self, *, code="012345", polls=None):
        # Each poll consumes the next scripted answer; the last one repeats.
        self.polls = list(polls) if polls is not None else [code]
        self.preflights = 0
        self.requests = []

    def preflight(self):
        self.preflights += 1

    def poll(self, requested_at):
        self.requests.append(requested_at)
        answer = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


class Harness:
    def __init__(self, tmp_path, *, mail=None):
        self.page = Page()
        self.mail = mail or Mail()
        self.secrets = []
        self.context = SimpleNamespace(route=lambda pattern, guard: None)
        self.mint = SimpleNamespace(page=self.page, context=self.context)
        spec = load_identity_target("hktvmall")
        assert isinstance(spec, HktvmallTargetSpec)
        self.driver = HktvmallTargetBrowser(
            self.mint,
            spec,
            expected_email=EMAIL,
            mail=self.mail,
            evidence_dir=tmp_path / "evidence",
            secrets=SimpleNamespace(fill=lambda _browser, **args: self.secrets.append(args)),
            utc_now=lambda: NOW,
            monotonic=lambda: self.page.elapsed,
        )
        self.page.after_password = lambda: self.submit(LOGIN_PATH)

    def submit(self, path, *, success=True, answer=None):
        route = self.route(ORIGIN + path, method="POST")
        if route.allowed:
            if answer is None and path == LOGIN_PATH:
                answer = TWO_FACTOR
            self.driver._observe_response(
                answer
                or response(
                    ORIGIN + path,
                    payload={"status": {"code": "success" if success else "failure"}},
                )
            )
        return route

    def route(self, url, **kwargs):
        route = Route(url, **kwargs)
        self.driver._guard(route)
        return route

    def enable_otp_flow(self, monkeypatch, *, verify_success=True):
        monkeypatch.setattr(self.driver, "_await_code_request", lambda: self.submit(OTP_REQUEST_PATH))
        inputs = self.page.code_inputs
        submit = Locator(click=lambda: self.submit(OTP_VERIFY_PATH, success=verify_success))
        modal = SimpleNamespace(
            url=ORIGIN + "/hktv/en/login/two-factor-authentication",
            locator=lambda selector: inputs if selector == ".account-2fa-codeInput" else submit,
        )
        self.page.frames = [modal]


def test_guard_denies_credentials_and_verify_before_authorization(tmp_path):
    harness = Harness(tmp_path)
    for path in (LOGIN_PATH, OTP_REQUEST_PATH, OTP_VERIFY_PATH):
        assert harness.submit(path).aborted
    assert harness.driver._requested_at is None
    assert all(count == 0 for count in harness.driver._counts.values())


def test_guard_spends_each_password_request_and_verify_budget_once(tmp_path, monkeypatch):
    harness = Harness(tmp_path)
    harness.driver._login_enabled = True
    assert harness.submit(LOGIN_PATH).allowed
    assert harness.submit(LOGIN_PATH).aborted
    assert harness.submit(OTP_REQUEST_PATH).allowed
    assert harness.driver._requested_at == NOW
    monkeypatch.setattr(harness.driver, "_utc_now", lambda: NOW + timedelta(seconds=10))
    assert harness.submit(OTP_REQUEST_PATH).aborted
    assert harness.driver._requested_at == NOW
    assert harness.submit(OTP_VERIFY_PATH).aborted
    harness.driver._verify_enabled = True
    assert harness.submit(OTP_VERIFY_PATH).allowed
    assert harness.submit(OTP_VERIFY_PATH).aborted
    assert harness.driver._counts == {
        LOGIN_PATH: 1,
        OTP_REQUEST_PATH: 1,
        OTP_VERIFY_PATH: 1,
    }


def test_guard_requires_password_then_confirmed_code_request(tmp_path):
    harness = Harness(tmp_path)
    harness.driver._login_enabled = True
    harness.driver._verify_enabled = True
    assert harness.submit(OTP_REQUEST_PATH).aborted
    assert harness.submit(OTP_VERIFY_PATH).aborted
    assert harness.submit(LOGIN_PATH).allowed
    request = harness.route(ORIGIN + OTP_REQUEST_PATH, method="POST")
    assert request.allowed
    assert harness.submit(OTP_VERIFY_PATH).aborted
    for unproven in ("unreadable", "rejected"):
        harness.driver._responses[OTP_REQUEST_PATH] = unproven
        assert harness.submit(OTP_VERIFY_PATH).aborted
    harness.driver._responses[OTP_REQUEST_PATH] = "success"
    assert harness.submit(OTP_VERIFY_PATH).allowed


@pytest.mark.parametrize("location", ["/hktv/en/", ORIGIN + "/hktv/en/main", "https://www.hktvmall.com/"])
def test_login_redirect_elsewhere_ends_sign_in_without_mail_or_page_proof(tmp_path, location):
    harness = Harness(tmp_path)
    # The landing page is never read: only the keeper's warm() proves it.
    harness.page.html = "<p>no bootstrap fields</p>"
    harness.page.after_password = lambda: harness.submit(
        LOGIN_PATH,
        answer=response(ORIGIN + LOGIN_PATH, status=302, location=location),
    )
    assert harness.driver.sign_in() is None
    assert harness.page.elapsed == 0
    assert harness.mail.requests == []
    assert harness.driver._counts[OTP_REQUEST_PATH] == 0
    assert harness.submit(OTP_REQUEST_PATH).aborted
    assert harness.driver._responses == {LOGIN_PATH: "redirected"}


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (
            response(ORIGIN + LOGIN_PATH, status=302, location="/hktv/en/login?error"),
            HoldReason.CREDENTIAL_REJECTED,
        ),
        (
            response(ORIGIN + LOGIN_PATH, status=303, location=LOGIN_PAGE + "?error=true"),
            HoldReason.CREDENTIAL_REJECTED,
        ),
        (
            response(ORIGIN + LOGIN_PATH, status=302, location=LOGIN_PATH),
            HoldReason.CREDENTIAL_REJECTED,
        ),
        (
            response(ORIGIN + LOGIN_PATH, payload={"result": "BAD_CREDENTIALS"}),
            HoldReason.UNKNOWN_FLOW,
        ),
        (
            response(ORIGIN + LOGIN_PATH, payload={"result": None}),
            HoldReason.UNKNOWN_FLOW,
        ),
        (
            response(ORIGIN + LOGIN_PATH, status=302, location="https://evil.example/"),
            HoldReason.UNKNOWN_FLOW,
        ),
        (
            response(ORIGIN + LOGIN_PATH, status=302, location="http://www.hktvmall.com/"),
            HoldReason.UNKNOWN_FLOW,
        ),
        (response(ORIGIN + LOGIN_PATH, status=302), HoldReason.UNKNOWN_FLOW),
        (
            response(ORIGIN + LOGIN_PATH, payload={"status": "ok"}),
            HoldReason.UNKNOWN_FLOW,
        ),
        (
            response(ORIGIN + LOGIN_PATH, payload=ValueError("not json")),
            HoldReason.UNKNOWN_FLOW,
        ),
        (
            response(ORIGIN + LOGIN_PATH, status=500, payload={"result": "x"}),
            HoldReason.UNKNOWN_FLOW,
        ),
        (
            response(ORIGIN + LOGIN_PATH, status=401, payload={"result": "x"}),
            HoldReason.UNKNOWN_FLOW,
        ),
    ],
)
def test_login_response_holds_immediately_without_code_request(tmp_path, answer, reason):
    harness = Harness(tmp_path)
    harness.page.after_password = lambda: harness.submit(LOGIN_PATH, answer=answer)
    with pytest.raises(SignInHold) as failure:
        harness.driver.sign_in()
    assert failure.value.reason is reason
    assert harness.page.elapsed == 0
    assert harness.mail.requests == []
    assert harness.driver._counts == {
        LOGIN_PATH: 1,
        OTP_REQUEST_PATH: 0,
        OTP_VERIFY_PATH: 0,
    }


def test_login_observer_keeps_only_closed_status(tmp_path):
    harness = Harness(tmp_path)
    secret = "must-not-be-retained"
    for answer, expected in (
        (TWO_FACTOR, "2fa_required"),
        (
            # An unobserved result is held for a person, not called a bad password.
            response(ORIGIN + LOGIN_PATH, payload={"result": secret, "uid": secret}),
            "unrecognised",
        ),
        (
            response(ORIGIN + LOGIN_PATH, status=302, location=f"/hktv/en/?token={secret}"),
            "redirected",
        ),
        (
            response(ORIGIN + LOGIN_PATH, status=302, location=f"https://{secret}/"),
            "unrecognised",
        ),
        (
            SimpleNamespace(url=ORIGIN + LOGIN_PATH, status=302, headers=None),
            "unrecognised",
        ),
    ):
        harness.driver._observe_response(answer)
        assert harness.driver._responses == {LOGIN_PATH: expected}
        assert secret not in repr(harness.driver._responses)
    # A shop page read at the login path is not the password's answer.
    harness.driver._responses.clear()
    harness.driver._observe_response(
        response(
            ORIGIN + LOGIN_PATH,
            status=302,
            location="/hktv/en/login?error",
            method="GET",
        )
    )
    assert harness.driver._responses == {}


def test_sign_in_never_runs_mail_preflight_itself(tmp_path, monkeypatch):
    # Readiness proves the transport before the budget; see test_hktvmall_keeper.
    harness = Harness(tmp_path)
    harness.enable_otp_flow(monkeypatch)
    harness.driver.sign_in()
    assert harness.mail.preflights == 0


@pytest.mark.parametrize(
    ("url", "method", "kind"),
    [
        (ORIGIN + "/hktv/en/cart/add", "POST", "xhr"),
        (ORIGIN + "/hktv/en/cart/remove", "GET", "xhr"),
        (ORIGIN + "/hktv/en/ajax/account/update", "POST", "fetch"),
        (ORIGIN + "/hktv/en/order/place", "POST", "document"),
        (ORIGIN + "/hktv/en/ajax/login/unknown", "GET", "fetch"),
        ("https://other.hktvmall.com" + LOGIN_PATH, "POST", "document"),
        ("http://www.hktvmall.com" + LOGIN_PATH, "POST", "document"),
        ("https://evil.example/collect", "POST", "xhr"),
        ("https://www.google.com.evil.example/recaptcha/api2/reload", "POST", "xhr"),
        ("http://www.google.com/recaptcha/api2/reload", "POST", "xhr"),
        ("http://www.hktvmall.com/app.js", "GET", "script"),
        ("file:///private.js", "GET", "script"),
        ("data:image/png;base64,AAAA", "GET", "image"),
    ],
)
def test_guard_blocks_unrelated_shop_requests_mutations_and_unsafe_origins(tmp_path, url, method, kind):
    harness = Harness(tmp_path)
    harness.driver._login_enabled = True
    harness.driver._verify_enabled = True
    assert harness.route(url, method=method, resource_type=kind).aborted
    assert harness.driver._requested_at is None


@pytest.mark.parametrize("host", ["www.google.com", "www.recaptcha.net", "www.gstatic.com"])
def test_guard_allows_google_child_challenge_but_denies_top_level_departure(tmp_path, host):
    harness = Harness(tmp_path)
    url = "https://" + host + "/recaptcha/api2/anchor"
    assert harness.route(url, resource_type="document", frame=object()).allowed
    assert harness.route(url, method="POST", frame=object()).allowed
    assert harness.route(url, resource_type="document", frame=harness.page.main_frame).aborted
    assert harness.route("https://evil.example/", resource_type="document", frame=harness.page.main_frame).aborted


@pytest.mark.parametrize("kind", ["document", "script", "stylesheet", "image", "font"])
def test_guard_preserves_shop_page_and_static_resource_reads(tmp_path, kind):
    harness = Harness(tmp_path)
    assert harness.route(ORIGIN + "/resource", resource_type=kind).allowed


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        (ACCOUNT_HTML, (True, EMAIL)),
        ('{"loggedin":false}', (False, None)),
        (
            '{"loggedin":true,"realContactEmail":"OWNER\\u0040EXAMPLE.COM"}',
            (True, EMAIL),
        ),
        ("<p>Signed in as owner@example.com</p>", (None, None)),
        ('{"loggedin":true,"email":"owner@example.com"}', (True, None)),
        ('{"loggedin":true,"realContactEmail":null}', (True, None)),
        (ACCOUNT_HTML + '{"realContactEmail":"other@example.com"}', (True, None)),
        (ACCOUNT_HTML + '{"loggedin":false}', (None, EMAIL)),
        (ACCOUNT_HTML + '{"realContactEmail":"OWNER@EXAMPLE.COM"}', (True, EMAIL)),
        (
            '{"loggedin":true,"value":"\\"realContactEmail\\":\\"owner@example.com\\""}',
            (True, None),
        ),
    ],
)
def test_account_proof_requires_unambiguous_bootstrap_fields(html, expected):
    assert account_from_html(html) == expected


def test_warm_proves_exact_account_before_export(tmp_path):
    harness = Harness(tmp_path)
    assert harness.driver.warm() == EMAIL
    harness.page.html = ACCOUNT_HTML.replace(EMAIL, "another@example.com")
    with pytest.raises(AccountMismatch):
        harness.driver.warm()


def test_credential_account_mismatch_holds_before_password_or_submission(tmp_path):
    harness = Harness(tmp_path)
    harness.driver._expected_email = "another@example.com"
    with pytest.raises(SignInHold) as failure:
        harness.driver.sign_in()
    assert failure.value.reason is HoldReason.ACCOUNT_MISMATCH
    assert harness.driver._counts[LOGIN_PATH] == 0


@pytest.mark.parametrize(
    ("html", "exception"),
    [
        ('{"loggedin":false}', SessionDead),
        ('{"loggedin":true}', VerifyFailed),
        ('{"realContactEmail":"owner@example.com"}', VerifyFailed),
        (ACCOUNT_HTML + '{"loggedin":false}', VerifyFailed),
    ],
)
def test_warm_guest_is_dead_but_missing_or_conflicting_proof_is_not(tmp_path, html, exception):
    harness = Harness(tmp_path)
    harness.page.html = html
    with pytest.raises(exception):
        harness.driver.warm()


@pytest.mark.parametrize(
    ("status", "reason"),
    [(403, BlockReason.ACCESS_DENIED), (429, BlockReason.RATE_LIMITED)],
)
def test_warm_blocks_are_not_credential_retry_signals(tmp_path, status, reason):
    harness = Harness(tmp_path)
    harness.page.status = status
    with pytest.raises(ProviderBlocked) as failure:
        harness.driver.warm()
    assert failure.value.reason is reason


def test_request_failure_stops_before_polling_or_any_resend(tmp_path):
    harness = Harness(tmp_path)

    def password_and_failed_request():
        harness.submit(LOGIN_PATH)
        harness.submit(OTP_REQUEST_PATH, success=False)

    harness.page.after_password = password_and_failed_request
    with pytest.raises(SignInHold) as failure:
        harness.driver.sign_in()
    assert failure.value.reason is HoldReason.OTP_REQUEST_REJECTED
    assert harness.mail.requests == []
    assert harness.driver._counts == {
        LOGIN_PATH: 1,
        OTP_REQUEST_PATH: 1,
        OTP_VERIFY_PATH: 0,
    }


def test_response_observer_keeps_only_closed_status_and_ignores_external_paths(
    tmp_path,
):
    harness = Harness(tmp_path)
    for path in (OTP_REQUEST_PATH, LOGIN_PATH):
        harness.driver._observe_response(
            response(
                "https://www.hktvmall.com.attacker.example" + path,
                payload={"status": {"code": "success"}, "result": "2FA_REQUIRED"},
            )
        )
    assert harness.driver._responses == {}
    for payload, expected in (
        ({"status": {"code": "success"}, "secret": "must-not-be-retained"}, "success"),
        ({"status": {"code": "arbitrary message must-not-be-retained"}}, "rejected"),
        ({"status": None}, "rejected"),
        ([], "rejected"),
        # A WAF or error page that was read is a rejection, not "unreadable".
        (b"<html>must-not-be-retained</html>", "rejected"),
    ):
        harness.driver._observe_response(response(ORIGIN + OTP_VERIFY_PATH, payload=payload))
        assert harness.driver._responses[OTP_VERIFY_PATH] == expected
        assert "must-not-be-retained" not in repr(harness.driver._responses)

    def discarded():
        raise RuntimeError("No resource with given identifier found")

    for status, expected in ((200, "unreadable"), (500, "rejected")):
        harness.driver._observe_response(SimpleNamespace(url=ORIGIN + OTP_VERIFY_PATH, status=status, body=discarded))
        assert harness.driver._responses[OTP_VERIFY_PATH] == expected


def test_verify_answer_discarded_by_navigation_defers_to_warm(tmp_path, monkeypatch):
    harness = Harness(tmp_path)
    harness.enable_otp_flow(monkeypatch)

    def discarded():
        raise RuntimeError("No resource with given identifier found")

    def verify():
        route = harness.route(ORIGIN + OTP_VERIFY_PATH, method="POST")
        assert route.allowed
        harness.driver._observe_response(SimpleNamespace(url=ORIGIN + OTP_VERIFY_PATH, status=200, body=discarded))

    harness.page.frames[0].locator = lambda selector: (
        harness.page.code_inputs if selector == ".account-2fa-codeInput" else Locator(click=verify)
    )
    harness.driver.sign_in()  # no hold: the keeper's warm() is the proof
    assert harness.driver._counts[OTP_VERIFY_PATH] == 1


def test_successful_flow_uses_actual_request_timestamp_and_verifies_once(tmp_path, monkeypatch):
    harness = Harness(tmp_path)
    harness.enable_otp_flow(monkeypatch)
    assert harness.driver.sign_in() is None
    assert harness.mail.requests == [NOW]
    assert [locator.values for locator in harness.page.code_inputs.inputs] == [[digit] for digit in "012345"]
    assert harness.driver._counts == {
        LOGIN_PATH: 1,
        OTP_REQUEST_PATH: 1,
        OTP_VERIFY_PATH: 1,
    }
    assert all(args["allowed_origins"] == frozenset({ORIGIN}) for args in harness.secrets)
    assert [args["field"] for args in harness.secrets] == ["username", "password"]


def test_mail_timeout_never_requests_a_second_code_or_submits_verification(tmp_path, monkeypatch):
    harness = Harness(tmp_path, mail=Mail(code=None))
    harness.enable_otp_flow(monkeypatch)
    with pytest.raises(SignInHold) as failure:
        harness.driver.sign_in()
    assert failure.value.reason is HoldReason.OTP_MAIL_TIMEOUT
    assert harness.driver._counts == {
        LOGIN_PATH: 1,
        OTP_REQUEST_PATH: 1,
        OTP_VERIFY_PATH: 0,
    }
    assert len(harness.mail.requests) > 1
    assert set(harness.mail.requests) == {NOW}
    assert harness.page.elapsed <= 153


def test_transient_mail_failure_is_retried_within_the_code_wait(tmp_path, monkeypatch):
    unavailable = SignInHold(HoldReason.OTP_MAIL_UNAVAILABLE)
    harness = Harness(tmp_path, mail=Mail(polls=[unavailable, None, unavailable, "012345"]))
    harness.enable_otp_flow(monkeypatch)
    assert harness.driver.sign_in() is None
    assert harness.mail.requests == [NOW] * 4
    assert [locator.values for locator in harness.page.code_inputs.inputs] == [[digit] for digit in "012345"]
    assert harness.driver._counts == {
        LOGIN_PATH: 1,
        OTP_REQUEST_PATH: 1,
        OTP_VERIFY_PATH: 1,
    }


@pytest.mark.parametrize(
    ("polls", "reason"),
    [
        (
            [SignInHold(HoldReason.OTP_MAIL_UNAVAILABLE)],
            HoldReason.OTP_MAIL_UNAVAILABLE,
        ),
        (
            [None, SignInHold(HoldReason.OTP_MAIL_UNAVAILABLE)],
            HoldReason.OTP_MAIL_UNAVAILABLE,
        ),
        (
            [SignInHold(HoldReason.OTP_MAIL_UNAVAILABLE), None],
            HoldReason.OTP_MAIL_TIMEOUT,
        ),
    ],
)
def test_code_wait_reports_the_last_poll_outcome_at_its_deadline(tmp_path, monkeypatch, polls, reason):
    harness = Harness(tmp_path, mail=Mail(polls=polls))
    harness.enable_otp_flow(monkeypatch)
    with pytest.raises(SignInHold) as failure:
        harness.driver.sign_in()
    assert failure.value.reason is reason
    assert len(harness.mail.requests) > 2
    assert set(harness.mail.requests) == {NOW}
    assert 150 <= harness.page.elapsed <= 153
    assert harness.driver._counts[OTP_VERIFY_PATH] == 0


def test_ambiguous_mail_stops_the_code_wait_immediately(tmp_path, monkeypatch):
    harness = Harness(
        tmp_path,
        mail=Mail(
            polls=[
                SignInHold(HoldReason.OTP_MAIL_UNAVAILABLE),
                SignInHold(HoldReason.OTP_MAIL_AMBIGUOUS),
                "012345",
            ]
        ),
    )
    harness.enable_otp_flow(monkeypatch)
    with pytest.raises(SignInHold) as failure:
        harness.driver.sign_in()
    assert failure.value.reason is HoldReason.OTP_MAIL_AMBIGUOUS
    assert len(harness.mail.requests) == 2
    assert harness.driver._counts[OTP_VERIFY_PATH] == 0


def test_rejected_verification_stops_without_resend_or_second_submission(tmp_path, monkeypatch):
    harness = Harness(tmp_path)
    harness.enable_otp_flow(monkeypatch, verify_success=False)
    with pytest.raises(SignInHold) as failure:
        harness.driver.sign_in()
    assert failure.value.reason is HoldReason.OTP_REJECTED
    assert harness.driver._counts == {
        LOGIN_PATH: 1,
        OTP_REQUEST_PATH: 1,
        OTP_VERIFY_PATH: 1,
    }
    assert harness.mail.requests == [NOW]


def test_image_challenge_captures_only_private_challenge_and_stops_before_otp(tmp_path):
    harness = Harness(tmp_path)
    challenge = Locator(visible=True, screenshot=b"challenge-only-image")
    body = Locator(text="Select all images containing traffic lights")
    frame = SimpleNamespace(
        url="https://www.google.com/recaptcha/api2/bframe",
        locator=lambda selector: body if selector == "body" else challenge,
    )
    harness.page.frames = [frame]
    with pytest.raises(SignInHold) as failure:
        harness.driver.sign_in()
    assert failure.value.reason is HoldReason.CAPTCHA_IMAGE_REQUIRED
    assert harness.driver._counts == {
        LOGIN_PATH: 1,
        OTP_REQUEST_PATH: 0,
        OTP_VERIFY_PATH: 0,
    }
    assert harness.mail.requests == []
    evidence = list((tmp_path / "evidence").glob("*.png"))
    assert len(evidence) == 1
    assert evidence[0].read_bytes() == b"challenge-only-image"
    assert stat.S_IMODE(evidence[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(evidence[0].parent.stat().st_mode) == 0o700


def test_automated_queries_block_does_not_capture_or_request_code(tmp_path):
    harness = Harness(tmp_path)
    frame = SimpleNamespace(
        url="https://www.google.com/recaptcha/api2/bframe",
        locator=lambda _selector: Locator(text="Your computer or network sends automated queries"),
    )
    harness.page.frames = [frame]
    with pytest.raises(ProviderBlocked) as failure:
        harness.driver.sign_in()
    assert failure.value.reason is BlockReason.CAPTCHA
    assert harness.driver._counts[OTP_REQUEST_PATH] == 0
    assert not (tmp_path / "evidence").exists()


def test_checkbox_wait_is_bounded_and_checkbox_is_never_clicked_twice(tmp_path, monkeypatch):
    harness = Harness(tmp_path)
    checkbox = Locator(visible=True)
    harness.page.frames = [
        SimpleNamespace(
            url="https://www.google.com/recaptcha/api2/anchor",
            locator=lambda _selector: checkbox,
        )
    ]
    clicks = []
    monkeypatch.setattr(harness.driver, "_click_checkbox", lambda value: clicks.append(value))
    with pytest.raises(SignInHold) as failure:
        harness.driver.sign_in()
    assert failure.value.reason is HoldReason.TIMEOUT
    assert clicks == [checkbox]
    assert harness.page.elapsed == 45
    assert harness.driver._counts[OTP_REQUEST_PATH] == 0


@pytest.mark.parametrize(
    "url",
    [
        "http://www.google.com/recaptcha/api2/anchor",
        "https://www.google.com.evil.example/recaptcha/api2/anchor",
        "https://evil.example/recaptcha/api2/anchor",
    ],
)
def test_untrusted_frame_cannot_become_checkbox_target(tmp_path, url):
    harness = Harness(tmp_path)
    harness.page.frames = [SimpleNamespace(url=url)]
    assert harness.driver._frame("/recaptcha/api2/anchor") is None


@pytest.mark.parametrize(
    "reason",
    [
        HoldReason.CAPTCHA_IMAGE_REQUIRED,
        HoldReason.OTP_MAIL_AMBIGUOUS,
        HoldReason.OTP_MAIL_TIMEOUT,
        HoldReason.OTP_REJECTED,
    ],
)
def test_keeper_hktvmall_holds_survive_new_daily_budget(tmp_path, reason):
    now = [NOW]
    state = TargetStateStore(tmp_path / "state", utc_now=lambda: now[0])
    budget = CredentialBudget(tmp_path / "budget", utc_now=lambda: now[0])
    sign_ins = []
    opened = []

    def warm():
        raise SessionDead()

    def sign_in():
        sign_ins.append(True)
        assert budget.attempts_today("hktvmall") == 1
        hold = state.load("hktvmall").hold
        assert hold is not None and hold.reason is HoldReason.SIGN_IN_INTERRUPTED
        raise SignInHold(reason)

    browser = SimpleNamespace(warm=warm, sign_in=sign_in)

    def open_browser():
        opened.append(True)
        return nullcontext(browser)

    keeper = IdentityKeeper(
        target="hktvmall",
        expected_email=EMAIL,
        export_dir=tmp_path / "export",
        state=state,
        budget=budget,
        readiness=lambda: ProviderReadiness(provider="hktvmall", missing=()),
        open_browser=open_browser,
        lease=lambda: nullcontext(),
        utc_now=lambda: now[0],
        snapshot_writer=lambda *_args: pytest.fail("held login must not publish"),
        snapshot_generation=lambda _directory: 0,
    )
    result = keeper.run(allow_credential_mint=True)
    assert result.outcome is IdentityOutcome.HELD
    assert result.reason == reason.value
    hold = state.load("hktvmall").hold
    assert hold is not None and hold.reason is reason
    now[0] += timedelta(days=2)
    again = keeper.run(allow_credential_mint=True)
    assert again.hold_id == hold.hold_id
    assert len(opened) == len(sign_ins) == 1
    assert budget.attempts_today("google") == 0


def test_keeper_uses_target_snapshot_writer_and_generation_without_google_headers(
    tmp_path,
):
    state = TargetStateStore(tmp_path / "state", utc_now=lambda: NOW)
    budget = CredentialBudget(tmp_path / "budget", utc_now=lambda: NOW)
    state.record_publish("hktvmall", 5)
    browser = SimpleNamespace(warm=lambda: EMAIL)
    writes = []
    directory = tmp_path / "export"
    keeper = IdentityKeeper(
        target="hktvmall",
        expected_email=EMAIL,
        export_dir=directory,
        state=state,
        budget=budget,
        readiness=lambda: pytest.fail("warm session needs no credential readiness"),
        open_browser=lambda: nullcontext(browser),
        lease=lambda: nullcontext(),
        utc_now=lambda: NOW,
        snapshot_writer=lambda *args: writes.append(args),
        snapshot_generation=lambda _directory: 7,
    )
    result = keeper.run(allow_credential_mint=False)
    assert result.outcome is IdentityOutcome.PUBLISHED
    assert result.generation == 8
    assert writes == [(directory, browser, 8, EMAIL, NOW)]
    assert state.load("hktvmall").last_generation == 8
    assert budget.attempts_today("hktvmall") == 0


class RecordingSolver:
    def __init__(self, harness, *, outcomes, submit_on_success=True):
        self.harness = harness
        self.outcomes = list(outcomes)
        self.submit_on_success = submit_on_success
        self.calls = 0

    def solve(self) -> bool:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is True and self.submit_on_success:
            self.harness.submit(OTP_REQUEST_PATH)
        return bool(outcome)


def challenge_frame(*, challenge_visible: bool = True) -> SimpleNamespace:
    challenge = Locator(visible=challenge_visible, screenshot=b"challenge-only")
    body = Locator(text="Select all images containing traffic lights")
    return SimpleNamespace(
        url="https://www.google.com/recaptcha/api2/bframe",
        locator=lambda selector: challenge if selector == "#rc-imageselect" else body,
    )


def two_factor_modal(submit_click):
    inputs = CodeInputs()
    submit = Locator(click=submit_click)
    return SimpleNamespace(
        url=ORIGIN + "/hktv/en/login/two-factor-authentication",
        locator=lambda selector: inputs if selector == ".account-2fa-codeInput" else submit,
    )


def test_solved_image_challenge_completes_the_code_flow(tmp_path):
    harness = Harness(tmp_path)
    harness.page.frames = [
        challenge_frame(),
        two_factor_modal(lambda: harness.submit(OTP_VERIFY_PATH, success=True)),
    ]
    solver = RecordingSolver(harness, outcomes=[True])
    harness.driver._solver = solver
    assert harness.driver.sign_in() is None
    assert solver.calls == 1
    assert harness.driver._counts == {
        LOGIN_PATH: 1,
        OTP_REQUEST_PATH: 1,
        OTP_VERIFY_PATH: 1,
    }
    assert harness.mail.requests == [NOW]
    harness.page.html = ACCOUNT_HTML
    harness.page.url = ORIGIN + "/hktv/en/"
    assert harness.driver.warm() == EMAIL


def test_hidden_challenge_does_not_consume_the_solver_attempt(tmp_path):
    harness = Harness(tmp_path)
    harness.page.frames = [challenge_frame(challenge_visible=False)]
    harness.driver._solver = RecordingSolver(harness, outcomes=[True], submit_on_success=False)
    with pytest.raises(SignInHold) as failure:
        harness.driver.sign_in()
    assert failure.value.reason is HoldReason.TIMEOUT
    assert harness.driver._solver.calls == 0
    assert harness.driver._counts[OTP_REQUEST_PATH] == 0
    assert not (tmp_path / "evidence").exists()


def test_preloaded_challenge_is_solved_only_after_it_becomes_visible(tmp_path):
    harness = Harness(tmp_path)
    frame = challenge_frame(challenge_visible=False)
    challenge = frame.locator("#rc-imageselect")
    harness.page.frames = [
        frame,
        two_factor_modal(lambda: harness.submit(OTP_VERIFY_PATH, success=True)),
    ]
    wait = harness.page.wait_for_timeout

    def reveal(milliseconds):
        wait(milliseconds)
        challenge._visible = True

    harness.page.wait_for_timeout = reveal
    solver = RecordingSolver(harness, outcomes=[True])
    harness.driver._solver = solver
    solve = solver.solve

    def solve_visible():
        assert challenge.is_visible(), "a hidden frame must not consume the attempt"
        return solve()

    solver.solve = solve_visible
    harness.driver.sign_in()
    assert solver.calls == 1
    assert harness.page.password_clicks == 1
    assert harness.driver._counts[OTP_REQUEST_PATH] == 1


def test_failed_solver_attempt_falls_back_to_the_private_hold(tmp_path):
    harness = Harness(tmp_path)
    harness.page.frames = [challenge_frame()]
    harness.driver._solver = RecordingSolver(harness, outcomes=[False])
    with pytest.raises(SignInHold) as failure:
        harness.driver.sign_in()
    assert failure.value.reason is HoldReason.CAPTCHA_IMAGE_REQUIRED
    assert harness.driver._solver.calls == 1
    assert harness.driver._counts[OTP_REQUEST_PATH] == 0
    assert harness.mail.requests == []
    evidence = list((tmp_path / "evidence").glob("*.png"))
    assert [item.read_bytes() for item in evidence] == [b"challenge-only"]


def test_solver_malfunction_propagates_as_infrastructure(tmp_path):
    harness = Harness(tmp_path)
    harness.page.frames = [challenge_frame()]
    harness.driver._solver = RecordingSolver(harness, outcomes=[RuntimeError("pointer unavailable")])
    with pytest.raises(RuntimeError):
        harness.driver.sign_in()
    assert harness.driver._counts[OTP_REQUEST_PATH] == 0


def test_default_driver_has_no_solver(tmp_path):
    harness = Harness(tmp_path)
    assert harness.driver._solver is None


@pytest.mark.parametrize("method", ["EMAIL_ONLY", "HSS_ONLY", "HSS_AND_EMAIL", "unrecognised"])
def test_otp_request_records_delivery_without_retaining_private_response(tmp_path, method):
    harness = Harness(tmp_path)
    harness.driver._login_enabled = True
    harness.submit(LOGIN_PATH)
    secret = "private-code-token-recipient"
    harness.submit(
        OTP_REQUEST_PATH,
        answer=response(
            ORIGIN + OTP_REQUEST_PATH,
            payload={
                "status": {"code": "success", "message": secret},
                "data": {
                    "twoFactorType": method,
                    "uid": secret,
                    "token": secret,
                    "code": secret,
                },
            },
        ),
    )
    files = list((tmp_path / "evidence").glob("otp-request-*.json"))
    assert len(files) == 1
    raw = files[0].read_text()
    saved = jsonlib.loads(raw)
    assert saved["delivery_method"] == (method if method != "unrecognised" else "unknown")
    assert saved["response_status"] == "success"
    assert saved["requested_at"] == NOW.isoformat()
    assert secret not in raw
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert harness.driver._counts == {
        LOGIN_PATH: 1,
        OTP_REQUEST_PATH: 1,
        OTP_VERIFY_PATH: 0,
    }


def test_otp_document_read_cannot_overwrite_delivery_response(tmp_path):
    harness = Harness(tmp_path)
    harness.driver._observe_response(
        response(
            ORIGIN + OTP_REQUEST_PATH,
            method="GET",
            payload={
                "status": {"code": "success"},
                "data": {"twoFactorType": "EMAIL_ONLY"},
            },
        )
    )
    assert harness.driver._responses == {}
    assert not list(tmp_path.rglob("otp-request-*.json"))


@pytest.mark.parametrize("method", ["EMAIL_ONLY", "HSS_AND_EMAIL", "HSS_ONLY"])
def test_sign_in_uses_email_when_available_and_holds_message_only(tmp_path, monkeypatch, method):
    harness = Harness(tmp_path)
    harness.enable_otp_flow(monkeypatch)
    monkeypatch.setattr(
        harness.driver,
        "_await_code_request",
        lambda: harness.submit(
            OTP_REQUEST_PATH,
            answer=response(
                ORIGIN + OTP_REQUEST_PATH,
                payload={
                    "status": {"code": "success"},
                    "data": {"twoFactorType": method},
                },
            ),
        ),
    )
    if method == "HSS_ONLY":
        with pytest.raises(SignInHold) as failure:
            harness.driver.sign_in()
        assert failure.value.reason is HoldReason.SECOND_FACTOR_UNSUPPORTED
        assert harness.mail.requests == []
        assert harness.driver._counts[OTP_VERIFY_PATH] == 0
    else:
        harness.driver.sign_in()
        assert harness.mail.requests == [NOW]
        assert harness.driver._counts[OTP_VERIFY_PATH] == 1
    saved = jsonlib.loads(next((tmp_path / "evidence").glob("otp-request-*.json")).read_text())
    assert saved["delivery_method"] == method
    assert harness.driver._counts[OTP_REQUEST_PATH] == 1


@pytest.mark.parametrize(
    "payload,expected_status",
    [
        (
            {"status": {"code": "failure"}, "data": {"twoFactorType": "EMAIL_ONLY"}},
            "rejected",
        ),
        (
            {"status": {"code": "success"}, "data": {"twoFactorType": ["private"]}},
            "success",
        ),
        (
            {"status": {"code": "success"}, "data": {"twoFactorType": "private"}},
            "success",
        ),
        (b"<html>private</html>", "rejected"),
        (ValueError("private"), "unreadable"),
    ],
)
def test_delivery_diagnostics_do_not_guess_missing_or_rejected_method(tmp_path, payload, expected_status):
    harness = Harness(tmp_path)
    harness.driver._observe_response(response(ORIGIN + OTP_REQUEST_PATH, payload=payload))
    saved = next((tmp_path / "evidence").glob("otp-request-*.json")).read_text()
    assert jsonlib.loads(saved)["delivery_method"] == "unknown"
    assert jsonlib.loads(saved)["response_status"] == expected_status
    assert "private" not in saved


def test_delivery_evidence_failure_does_not_change_request_or_verify(tmp_path, monkeypatch, caplog):
    harness = Harness(tmp_path)
    # A stale file prevents directory creation, without changing the login.
    (tmp_path / "evidence").write_text("inert")
    harness.enable_otp_flow(monkeypatch)
    harness.driver.sign_in()
    assert harness.mail.requests == [NOW]
    assert harness.driver._counts == {
        LOGIN_PATH: 1,
        OTP_REQUEST_PATH: 1,
        OTP_VERIFY_PATH: 1,
    }
    assert "hktvmall_otp_delivery_evidence_unavailable" in caplog.text

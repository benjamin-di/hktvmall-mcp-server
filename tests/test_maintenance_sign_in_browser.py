"""HKTVmall's real request guard and login observer in Chromium.

Chromium resolves ``www.hktvmall.com`` to a local self-signed HTTPS stub and
nothing else at all, so every guard decision (continue or abort), redirect and
response event is the browser's own. The preload regression also fulfills one
inert Google frame locally; no request reaches Google's servers.
"""

from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

if sys.platform != "linux":
    pytest.skip("optional maintenance requires Linux", allow_module_level=True)

pytest.importorskip("patchright.sync_api")
from patchright.sync_api import sync_playwright
from signin_window.automation.browser import BrowserSession as PatchrightMintContext

from hktvmall_maintenance.browser import (
    LOGIN_PATH,
    ORIGIN,
    OTP_REQUEST_PATH,
    OTP_VERIFY_PATH,
    HktvmallTargetBrowser,
)
from hktvmall_maintenance.config import HktvmallTargetSpec, load_identity_target
from hktvmall_maintenance.outcomes import HoldReason, SignInHold

pytestmark = pytest.mark.browser

EMAIL = "owner@example.com"
PASSWORD = "fake-password"
CODE = "012345"
MODAL_PATH = "/hktv/en/login/two-factor-authentication"
FORBIDDEN_PATH = "/hktv/en/cart/add"

LOGIN_PAGE = """<!doctype html><html><body>
<button type="button">Email Address</button>
<form id="loginForm" action="/hktv/en/j_spring_security_check" method="post">
  <input id="j_username_email" name="j_username">
  <input id="j_pwd_email" name="j_password" type="password">
  <button type="submit">Log in</button>
</form>
<script>
  // A drifted page must not reach verification before the password.
  fetch("VERIFY", {method: "POST"}).catch(() => {});
  document.querySelector("#loginForm").addEventListener("submit", async (event) => {
    if ("MODE" !== "fetch") return;
    event.preventDefault();
    const response = await fetch(event.target.action, {
      method: "POST", body: new URLSearchParams(new FormData(event.target)),
    });
    if (response.redirected) { location.href = response.url; return; }
    if ((await response.json()).result !== "2FA_REQUIRED") return;
    const modal = document.createElement("iframe");
    modal.src = "MODAL";
    modal.style = "width:640px;height:320px";
    document.body.append(modal);
  });
</script>
</body></html>"""

MODAL_PAGE = """<!doctype html><html><body>
<div>INPUTS<button class="account-2fa-otp-codeInput-submitButton">Verify</button></div>
<script>
  fetch("REQUEST", {method: "POST"})
    // The one permitted code request: a resend or a mutation is aborted.
    .then(() => fetch("REQUEST", {method: "POST"})).catch(() => {})
    .then(() => fetch("FORBIDDEN", {method: "POST"})).catch(() => {});
  document.querySelector("button").addEventListener("click", async () => {
    const code = [...document.querySelectorAll(".account-2fa-codeInput")]
      .map((input) => input.value).join("");
    const response = await fetch("VERIFY", {
      method: "POST", body: new URLSearchParams({code}),
    });
    if ((await response.json()).status.code === "success") {
      // Navigate at once: Chromium then discards the answer's body, which
      // the driver must not mistake for a rejected code.
      top.location.href = "/hktv/en/";
    }
  });
</script>
</body></html>""".replace("INPUTS", '<input class="account-2fa-codeInput" maxlength="1">' * 6)

ACCOUNT_PAGE = (
    "<!doctype html><script>var site = "
    + json.dumps({"loggedin": True, "realContactEmail": EMAIL})
    + ";</script><p>Welcome</p>"
)


class Stub(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.mode = "fetch"
        self.login = "2fa"
        self.requests: list[tuple[str, str]] = []
        self.bodies: dict[str, dict[str, list[str]]] = {}

    def posts(self) -> list[str]:
        return [path for method, path in self.requests if method == "POST"]


class Handler(BaseHTTPRequestHandler):
    server: Stub

    def log_message(self, *_args: object) -> None:
        pass

    def _send(self, status: int, body: str = "", **headers: str) -> None:
        payload = body.encode()
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _html(self, body: str) -> None:
        self._send(200, body, Content_Type="text/html; charset=utf-8")

    def _json(self, value: object) -> None:
        self._send(200, json.dumps(value), Content_Type="application/json")

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        self.server.requests.append(("GET", path))
        if path == "/hktv/en/login":
            self._html(
                LOGIN_PAGE.replace("MODE", self.server.mode)
                .replace("VERIFY", OTP_VERIFY_PATH)
                .replace("MODAL", MODAL_PATH)
            )
        elif path == MODAL_PATH:
            self._html(
                MODAL_PAGE.replace("REQUEST", OTP_REQUEST_PATH)
                .replace("VERIFY", OTP_VERIFY_PATH)
                .replace("FORBIDDEN", FORBIDDEN_PATH)
            )
        elif path == "/hktv/en/":
            self._html(ACCOUNT_PAGE)
        else:
            self._send(404)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode()
        self.server.requests.append(("POST", path))
        self.server.bodies[path] = parse_qs(body)
        if path == LOGIN_PATH and self.server.login == "2fa":
            self._json({"result": "2FA_REQUIRED"})
        elif path == LOGIN_PATH and self.server.login == "direct":
            self._send(302, Location="/hktv/en/")
        elif path == LOGIN_PATH:
            self._send(302, Location=ORIGIN + "/hktv/en/login?error=true")
        elif path == OTP_REQUEST_PATH:
            self._json(
                {
                    "status": {"code": "success"},
                    "data": {"twoFactorType": "EMAIL_ONLY", "uid": EMAIL},
                }
            )
        elif path == OTP_VERIFY_PATH:
            self._json({"status": {"code": "success"}})
        else:
            self._send(404)


class Secrets:
    """Stands in for 1Password only; the fields are filled in the real page."""

    def fill(self, browser, *, secret_ref, field, selectors, allowed_origins):
        parts = urlsplit(browser.page.url)
        assert f"{parts.scheme}://{parts.netloc}" in allowed_origins
        value = {"username": EMAIL, "password": PASSWORD}[field]
        browser.page.locator(selectors[0]).fill(value)


class Mail:
    configured = True

    def __init__(self) -> None:
        self.requests = []

    def preflight(self) -> None:
        pytest.fail("sign-in must not run the mail preflight")

    def poll(self, requested_at):
        self.requests.append(requested_at)
        return CODE


@pytest.fixture(scope="module")
def stub(tmp_path_factory):
    openssl = shutil.which("openssl")
    assert openssl is not None, "the HTTPS stub needs openssl for its certificate"
    directory = tmp_path_factory.mktemp("hktvmall-tls")
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=www.hktvmall.com",
            "-addext",
            "subjectAltName=DNS:www.hktvmall.com",
            "-keyout",
            str(directory / "key.pem"),
            "-out",
            str(directory / "cert.pem"),
        ],
        check=True,
        capture_output=True,
    )
    server = Stub()
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(directory / "cert.pem", directory / "key.pem")
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="module")
def chromium(stub):
    port = stub.server_address[1]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            # Chromium otherwise follows a proxy from the environment, even
            # with --no-proxy-server; the resolver rules still fail closed.
            env={name: value for name, value in os.environ.items() if "proxy" not in name.casefold()},
            args=[
                # Only the shop resolves, and only to the stub.
                f"--host-resolver-rules=MAP www.hktvmall.com 127.0.0.1:{port}, MAP * ~NOTFOUND",
                "--no-proxy-server",
            ],
        )
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def driver(chromium, stub, tmp_path):
    stub.requests.clear()
    stub.bodies.clear()
    context = chromium.new_context(ignore_https_errors=True)
    try:
        spec = load_identity_target("hktvmall")
        assert isinstance(spec, HktvmallTargetSpec)
        mail = Mail()
        yield (
            HktvmallTargetBrowser(
                PatchrightMintContext(context, context.new_page()),
                spec,
                expected_email=EMAIL,
                mail=mail,
                evidence_dir=tmp_path / "evidence",
                secrets=Secrets(),
            ),
            mail,
        )
    finally:
        context.close()


def test_two_factor_flow_posts_password_request_and_verify_once_in_order(driver, stub):
    browser, mail = driver
    stub.mode, stub.login = "fetch", "2fa"
    browser.sign_in()
    # The early verify, the resend and the cart write never left the browser.
    assert stub.posts() == [LOGIN_PATH, OTP_REQUEST_PATH, OTP_VERIFY_PATH]
    assert stub.bodies[LOGIN_PATH] == {"j_username": [EMAIL], "j_password": [PASSWORD]}
    assert stub.bodies[OTP_VERIFY_PATH] == {"code": [CODE]}
    # The page navigates as soon as it reads the verify answer, so the driver
    # may or may not have read the body first; neither is a rejection.
    assert browser._responses.pop(OTP_VERIFY_PATH) in {"success", "unreadable"}
    assert browser._responses == {
        LOGIN_PATH: "2fa_required",
        OTP_REQUEST_PATH: "success",
    }
    assert mail.requests == [browser._requested_at]
    records = list(browser._evidence_dir.glob("otp-request-*.json"))
    assert len(records) == 1
    saved = json.loads(records[0].read_text())
    assert saved["delivery_method"] == "EMAIL_ONLY"
    assert saved["response_status"] == "success"
    assert EMAIL not in records[0].read_text()
    assert browser.warm() == EMAIL


@pytest.mark.parametrize("mode", ["fetch", "form"])
def test_direct_sign_in_redirect_returns_for_independent_warm(driver, stub, mode):
    browser, mail = driver
    stub.mode, stub.login = mode, "direct"
    started = time.monotonic()
    browser.sign_in()
    assert time.monotonic() - started < 15
    assert stub.posts() == [LOGIN_PATH]
    assert browser._responses == {LOGIN_PATH: "redirected"}
    assert mail.requests == []
    assert browser.warm() == EMAIL


@pytest.mark.parametrize("mode", ["fetch", "form"])
def test_wrong_password_redirect_holds_as_rejected_credential(driver, stub, mode):
    browser, mail = driver
    stub.mode, stub.login = mode, "wrong"
    started = time.monotonic()
    with pytest.raises(SignInHold) as failure:
        browser.sign_in()
    assert failure.value.reason is HoldReason.CREDENTIAL_REJECTED
    assert time.monotonic() - started < 15
    assert stub.posts() == [LOGIN_PATH]
    assert browser._responses == {LOGIN_PATH: "rejected"}
    assert mail.requests == []


def test_preloaded_google_frame_waits_for_the_visible_grid(driver, stub):
    browser, mail = driver
    page = browser._page
    browser._browser.context.route(
        "https://www.google.com/recaptcha/api2/bframe*",
        lambda route: route.fulfill(
            content_type="text/html",
            body='<div id="rc-imageselect">Select all images with cars</div>',
        ),
    )
    page.set_content(
        '<iframe id="challenge" style="display:none" src="https://www.google.com/recaptcha/api2/bframe"></iframe>'
    )
    grid = page.frame_locator("#challenge").locator("#rc-imageselect")
    grid.wait_for(state="attached")
    assert not browser._visible(grid)
    calls = []

    def solve():
        assert browser._visible(grid), "a hidden frame must not spend the attempt"
        calls.append(True)
        browser._responses[OTP_REQUEST_PATH] = "success"
        return True

    browser._solver = SimpleNamespace(solve=solve)
    page.evaluate("setTimeout(() => document.querySelector('#challenge').style.display = 'block', 100)")
    assert browser._await_code_request() == "otp_requested"
    assert calls == [True]
    assert stub.posts() == []
    assert mail.requests == []

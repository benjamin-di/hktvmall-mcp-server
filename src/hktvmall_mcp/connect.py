"""The connect page: the sign-in screen Claude or ChatGPT opens when a friend adds the HKTVmall connector.

The friend enters their invite code (a browser that connected before is remembered and skips it), logs in on
HKTVmall's own page (shown live from the login service's browser) and is sent back to their assistant with a
one-time code for their account (oauth.HktvOAuth.complete). Nothing here is behind the MCP bearer: the page
needs a live sign-in request from /authorize, everything after it the unguessable connect id it was given,
and no login window opens before an invite code or a remembered browser.

  GET  /connect?req=REQUEST           the page (from /authorize)
  POST /connect/{cid}/invite          {"code": ...} -> ready | invite_wrong | invite_limited
  POST /connect/{cid}/start           open a login window (layout from the person's screen)
  GET  /connect/{cid}/state           progress; adopts the session once HKTVmall says signed in
  POST /connect/{cid}/cancel
  GET  /connect/{cid}/novnc/...       the viewer's files (noVNC, from the login service, plus paste.js)
  WS   /connect/{cid}/vnc             the live view of HKTVmall's page
"""

from __future__ import annotations

import collections
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import anyio.to_thread
from signin_window.client import LoginBusy, LoginClient, LoginUnavailable
from signin_window.relay import PASTE_JS, with_paste
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from .accounts import ACCOUNT_ID, Accounts, mask_email
from .errors import HktvError
from .invites import Invites

if TYPE_CHECKING:
    from .oauth import HktvOAuth

log = logging.getLogger(__name__)

SESSION_TTL = 20 * 60
MAX_SESSIONS = 20
DEVICE_COOKIE = "hktv_device"
DEVICE_TTL = 180 * 24 * 3600
TRIES_WINDOW, TRIES_PER_ADDRESS, TRIES_TOTAL = 10 * 60, 8, 60  # wrong invite codes
PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


@dataclass
class ConnectSession:
    cid: str
    request: str  # the waiting sign-in request (oauth)
    created: float
    known: str | None = None  # account id of a browser that connected before
    invite: str | None = None
    state: str = "new"  # new, ready, starting, waiting, connected, wrong_account, busy, failed, expired, cancelled
    login_id: str | None = None
    view_token: str | None = field(default=None, repr=False)  # the window's live view (signin-window's service)
    account: str | None = None
    host: str = ""
    redirect: str | None = None
    account_id: str | None = None


@dataclass
class Tries:
    """Wrong invite codes, per address and in total: guessing is slowed long before it could matter."""

    by_address: dict[str, collections.deque] = field(default_factory=dict)
    total: collections.deque = field(default_factory=collections.deque)

    def allowed(self, address: str, now: float) -> bool:
        for queue in (self.total, self.by_address.setdefault(address, collections.deque())):
            while queue and now - queue[0] > TRIES_WINDOW:
                queue.popleft()
        return len(self.total) < TRIES_TOTAL and len(self.by_address[address]) < TRIES_PER_ADDRESS

    def failed(self, address: str, now: float) -> None:
        self.total.append(now)
        self.by_address.setdefault(address, collections.deque()).append(now)


class ConnectFlow:
    def __init__(
        self,
        accounts: Accounts,
        invites: Invites,
        client: LoginClient,
        oauth: HktvOAuth,
        device_key: bytes,
        *,
        clock=time.monotonic,
        wall=time.time,
    ) -> None:
        self.accounts, self.invites, self.client, self.oauth = accounts, invites, client, oauth
        self._device_key = hmac.new(device_key, b"hktvmall connect device cookie", hashlib.sha256).digest()
        self._clock, self._wall = clock, wall
        self._sessions: dict[str, ConnectSession] = {}
        self._tries = Tries()

    # -- a browser that connected before -----------------------------------------------------

    def device_cookie(self, account_id: str) -> str:
        expires = int(self._wall() + DEVICE_TTL)
        body = f"{account_id}.{expires}"
        return f"{body}.{hmac.new(self._device_key, body.encode(), hashlib.sha256).hexdigest()}"

    def known_account(self, cookie: str | None) -> str | None:
        try:
            account_id, expires, signature = (cookie or "").split(".")
        except ValueError:
            return None
        expected = hmac.new(self._device_key, f"{account_id}.{expires}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected) or not expires.isdigit() or int(expires) < self._wall():
            return None
        return account_id if ACCOUNT_ID.fullmatch(account_id) and account_id in self.accounts.ids() else None

    # -- sessions ---------------------------------------------------------------------------

    def _session(self, cid: str) -> ConnectSession | None:
        now = self._clock()
        for old in [s for s in self._sessions.values() if now - s.created > SESSION_TTL]:
            self._sessions.pop(old.cid, None)
        return self._sessions.get(cid)

    def open(self, request: str, known: str | None) -> ConnectSession:
        if len(self._sessions) >= MAX_SESSIONS:
            oldest = min(self._sessions.values(), key=lambda s: s.created)
            self._sessions.pop(oldest.cid, None)
        session = ConnectSession(secrets.token_urlsafe(24), request, self._clock(), known=known)
        session.state = "ready" if known else "new"
        self._sessions[session.cid] = session
        return session

    def enter_invite(self, session: ConnectSession, code: str, address: str) -> None:
        now = self._clock()
        if not self._tries.allowed(address, now):
            session.state = "invite_limited"
            return
        key = self.invites.find(code)
        if key is None:
            self._tries.failed(address, now)
            session.state = "invite_wrong"
            log.info("connect: a wrong invite code")
            return
        session.invite, session.known = key, None
        if session.state in ("new", "invite_wrong", "invite_limited"):
            session.state = "ready"

    def start(self, session: ConnectSession, layout: str) -> None:
        if session.login_id is not None or session.state != "ready":
            return
        try:
            login = self.client.start(layout if layout in ("phone", "desktop") else "desktop")
        except LoginBusy:
            session.state = "busy"
            return
        session.login_id, session.view_token, session.state = login["id"], login["view_token"], "starting"

    def advance(self, session: ConnectSession) -> None:
        """Follow the login; once HKTVmall says signed in, take the session, connect the account and send the
        person back to their assistant with a code for it."""
        if session.login_id is None or session.state not in ("starting", "waiting"):
            return
        login = self.client.state(session.login_id)
        if login.get("state") == "unknown":
            session.state = "expired"
            return
        session.host = str(login.get("host") or "")
        if login.get("state") in ("starting", "waiting"):
            session.state = login["state"]
            return
        if login.get("state") != "signed_in":
            session.state = "failed" if login.get("state") == "failed" else "expired"
            return
        result = self.client.result(session.login_id)
        if result is None:
            session.state = "failed"
            return
        email = str(result.get("email") or "").casefold()
        if session.invite is not None:
            allowed = self.invites.allows(session.invite, email)
        else:  # a remembered browser may only sign its own account in again
            allowed = session.known is not None and self.accounts.email(session.known) == email
        if not allowed:
            session.state = "wrong_account"
            log.warning("connect: a login for a different HKTVmall account than the invite or browser")
            return
        try:
            account_id = self.accounts.adopt(email, result.get("cookies") or [], str(result.get("user_agent") or ""))
        except HktvError:
            session.state = "failed"
            return
        if session.invite is not None:
            self.invites.bind(session.invite, email, account_id)
        redirect = self.oauth.complete(session.request, account_id)
        if redirect is None:
            session.state = "expired"  # connected, but the assistant stopped waiting: connect again from it
            return
        session.state, session.account, session.redirect = "connected", mask_email(email), redirect
        session.account_id = account_id
        log.info("connect: account %s connected", account_id)

    def cancel(self, session: ConnectSession) -> None:
        # Close the window whatever the last poll said: the service ignores a window that has already ended, and a
        # cancel never waits for a state poll to have seen the window open.
        if session.login_id and session.state != "connected":
            self.client.close(session.login_id)
        if session.state != "connected":
            session.state = "cancelled"

    # -- HTTP -------------------------------------------------------------------------------

    async def page(self, request: Request) -> Response:
        """GET /connect?req=REQUEST (registered on the server itself; the rest is mounted under /connect/)."""
        if self.oauth.pending(request.query_params.get("req", "")) is None:
            return HTMLResponse(_page(None, None), status_code=404, headers=PAGE_HEADERS)
        known = self.known_account(request.cookies.get(DEVICE_COOKIE))
        session = self.open(request.query_params["req"], known)
        welcome = mask_email(self.accounts.email(known) or "") if known else None
        return HTMLResponse(_page(session.cid, welcome), headers={**PAGE_HEADERS, "X-Frame-Options": "DENY"})

    def app(self) -> Starlette:
        def found(request: Request) -> ConnectSession | None:
            return self._session(request.path_params["cid"])

        async def body(request: Request) -> dict[str, Any]:
            try:
                data = await request.json()
            except ValueError:
                return {}
            return data if isinstance(data, dict) else {}

        def reply(session: ConnectSession) -> JSONResponse:
            response = JSONResponse(_public(session), headers=PAGE_HEADERS)
            if session.state == "connected" and session.account_id:
                response.set_cookie(
                    DEVICE_COOKIE,
                    self.device_cookie(session.account_id),
                    max_age=DEVICE_TTL,
                    path="/connect",
                    secure=True,
                    httponly=True,
                    samesite="lax",
                )
            return response

        async def invite(request: Request) -> Response:
            session = found(request)
            if session is None:
                return JSONResponse({"state": "expired"}, status_code=404)
            code = str((await body(request)).get("code", ""))[:64]
            # The reverse proxy in front must overwrite X-Forwarded-For with the client's address (not append to it);
            # without one, every client shares one bucket.
            address = (request.headers.get("x-forwarded-for") or "unknown").split(",")[0].strip()[:64]
            await anyio.to_thread.run_sync(self.enter_invite, session, code, address)
            return reply(session)

        async def start(request: Request) -> Response:
            session = found(request)
            if session is None:
                return JSONResponse({"state": "expired"}, status_code=404)
            layout = str((await body(request)).get("layout"))
            try:
                await anyio.to_thread.run_sync(self.start, session, layout)
            except LoginUnavailable:
                session.state = "failed"
            return reply(session)

        async def state(request: Request) -> Response:
            session = found(request)
            if session is None:
                return JSONResponse({"state": "expired"}, status_code=404)
            try:
                await anyio.to_thread.run_sync(self.advance, session)
            except LoginUnavailable:
                session.state = "failed"
            return reply(session)

        async def cancel(request: Request) -> Response:
            session = found(request)
            if session is not None:
                try:
                    await anyio.to_thread.run_sync(self.cancel, session)
                except LoginUnavailable:
                    pass
            return Response(status_code=204)

        async def novnc(request: Request) -> Response:
            if found(request) is None:
                return Response(status_code=404)
            path = request.path_params["path"]
            if path == "paste.js":
                return Response(
                    PASTE_JS, media_type="text/javascript", headers={"Cache-Control": "private, max-age=600"}
                )
            status, content, content_type = await anyio.to_thread.run_sync(self.client.static, path)
            if path == "vnc.html" and status == 200:
                content = with_paste(content)
            headers = {"Cache-Control": "private, max-age=600", "X-Frame-Options": "SAMEORIGIN"}
            return Response(content, status_code=status, media_type=content_type or None, headers=headers)

        async def vnc(ws: WebSocket) -> None:
            session = self._session(ws.path_params["cid"])
            if session is None or not session.login_id or not session.view_token or session.state != "waiting":
                await ws.close(code=4404)
                return
            await ws.accept(subprotocol="binary" if "binary" in ws.scope.get("subprotocols", []) else None)
            await self.client.bridge(session.login_id, session.view_token, ws)

        return Starlette(
            routes=[
                Route("/{cid}/invite", invite, methods=["POST"]),
                Route("/{cid}/start", start, methods=["POST"]),
                Route("/{cid}/state", state),
                Route("/{cid}/cancel", cancel, methods=["POST"]),
                Route("/{cid}/novnc/{path:path}", novnc),
                WebSocketRoute("/{cid}/vnc", vnc),
            ]
        )


def _public(session: ConnectSession) -> dict[str, Any]:
    return {"state": session.state, "host": session.host, "account": session.account, "redirect": session.redirect}


def _page(cid: str | None, welcome: str | None) -> str:
    if cid is None:
        body = """<main class=msg><h1>This sign-in has expired</h1>
<p>Go back to Claude or ChatGPT and connect HKTVmall again.</p></main>"""
        return PAGE.replace("{{BODY}}", body).replace("{{SCRIPT}}", "")
    script = SCRIPT.replace("{{CID}}", json.dumps(cid)).replace("{{WELCOME}}", json.dumps(welcome))
    return PAGE.replace("{{BODY}}", BODY).replace("{{SCRIPT}}", script)


BODY = """<header><h1>Connect HKTVmall</h1>
<p>Log in on HKTVmall's own page, as you usually do. It runs in a browser on the private server of the person who
invited you; once you're in, your assistant can search HKTVmall and fill your basket. You always pay at
HKTVmall yourself.</p>
<div class=bar hidden><span id=host>🔒 www.hktvmall.com</span><span id=status></span></div></header>
<main id=invite class=msg hidden>
<form id=inviteForm autocomplete=off>
<label for=code>Your invite code</label>
<input id=code name=code inputmode=text autocapitalize=characters spellcheck=false
 placeholder="XXXX-XXXX-XXXX" required>
<p id=inviteError class=err hidden></p>
<button type=submit>Continue</button>
</form></main>
<div id=view></div>
<main id=done class=msg hidden></main>"""

PAGE = """<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Connect HKTVmall</title>
<style>
:root{color-scheme:light;font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;color:#1d2a1d}
html,body{margin:0;height:100%;background:#f5f7f5}
body{display:flex;flex-direction:column}
header{padding:12px 16px 8px;background:#fff;border-bottom:1px solid #dde3dd}
h1{font-size:18px;margin:0 0 4px}
header p{margin:0 0 8px;font-size:13px;color:#4a574a;max-width:60em}
.bar{display:flex;justify-content:space-between;gap:12px;font-size:13px}
#host{color:#1f7a32;font-weight:600}#status{color:#4a574a;text-align:right}
#view{flex:1;min-height:0;display:flex}
#view iframe{flex:1;border:0;width:100%;height:100%;background:#fff}
.msg{margin:auto;padding:28px 20px;text-align:center;max-width:26em;width:100%;box-sizing:border-box}
.msg h1{font-size:22px}.ok{font-size:44px;color:#1f7a32;margin:0}
label{display:block;font-weight:600;margin-bottom:8px}
input{font:600 20px ui-monospace,monospace;letter-spacing:2px;text-align:center;width:100%;box-sizing:border-box;
 padding:12px;border:1px solid #b9c4b9;border-radius:8px;text-transform:uppercase}
button{font:inherit;font-weight:600;margin-top:14px;width:100%;padding:12px 18px;border-radius:8px;
 border:1px solid #1f7a32;background:#1f7a32;color:#fff}
.err{color:#a3261b;margin:10px 0 0}.link{background:none;border:0;color:#1f7a32;width:auto;padding:4px;font-weight:400}
[hidden]{display:none!important}
@media (max-width:600px){header p{font-size:12px}}
</style>
{{BODY}}
{{SCRIPT}}
</html>"""

SCRIPT = """<script>
const cid = {{CID}}, welcome = {{WELCOME}};
const base = `/connect/${cid}`;
const $ = id => document.getElementById(id);
const esc = t => String(t ?? "").replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`);
const post = (path, data) => fetch(`${base}/${path}`, {method: "POST", headers: {"content-type": "application/json"},
                                                      body: JSON.stringify(data || {})}).then(r => r.json());
const TEXT = {starting: "Opening HKTVmall…", waiting: "Log in as you usually do. This page notices when you're in."};
const AGAIN = `<button onclick="location.reload()">Try again</button>`;
const STOP = {
  connected: s => `<p class=ok>✓</p><h1>Connected</h1><p>Your HKTVmall account ${esc(s.account)} is connected.
    Taking you back to your assistant…</p>`,
  wrong_account: () => `<h1>That's a different account</h1><p>Log in with the HKTVmall account your invite is for.
    If this is a new account, ask the person who invited you for a new code.</p>${AGAIN}`,
  busy: () => `<h1>Someone else is connecting right now</h1><p>Please try again in a few minutes.</p>${AGAIN}`,
  expired: () => `<h1>This sign-in has expired</h1><p>Go back to your assistant and connect HKTVmall again.</p>`,
  failed: () => `<h1>Something went wrong</h1><p>Go back to your assistant and connect HKTVmall again.</p>`,
  cancelled: () => `<h1>Cancelled</h1>`,
};
function finish(html) {
  for (const id of ["view", "invite"]) $(id).hidden = true;
  document.querySelector("header .bar").hidden = true;
  const done = $("done"); done.innerHTML = html; done.hidden = false;
}
let viewer = false;
function show(s) {
  if (STOP[s.state]) {
    finish(STOP[s.state](s));
    if (s.state === "connected" && s.redirect) setTimeout(() => location.replace(s.redirect), 1200);
    return true;
  }
  if (s.host) $("host").textContent = (s.host.endsWith("hktvmall.com") ? "🔒 " : "") + s.host;
  $("status").textContent = TEXT[s.state] || "";
  if (s.state === "waiting" && !viewer) {
    viewer = true;
    const frame = document.createElement("iframe");
    const q = new URLSearchParams({autoconnect: 1, resize: "scale", reconnect: 1, reconnect_delay: 1500,
                                   path: `/connect/${cid}/vnc`, show_dot: 1});
    frame.src = `${base}/novnc/vnc.html?${q}`;
    frame.title = "HKTVmall login";
    $("view").appendChild(frame);
  }
  return false;
}
async function poll() {
  try {
    const r = await fetch(`${base}/state`, {cache: "no-store"});
    if (show(await r.json())) return;
  } catch (e) {}
  setTimeout(poll, 1500);
}
async function begin() {
  $("invite").hidden = true;
  document.querySelector("header .bar").hidden = false;
  $("status").textContent = (welcome ? `Welcome back, ${welcome}. ` : "") + TEXT.starting;
  const layout = Math.min(innerWidth, screen.width) < 700 ? "phone" : "desktop";
  if (!show(await post("start", {layout}))) poll();
}
$("inviteForm").addEventListener("submit", async e => {
  e.preventDefault();
  const s = await post("invite", {code: $("code").value});
  const error = $("inviteError");
  if (s.state === "ready") return begin();
  error.hidden = false;
  error.textContent = s.state === "invite_limited" ? "Too many tries. Please wait ten minutes and try again."
    : s.state === "invite_wrong" ? "That code doesn't work. Check it and try again." : "Something went wrong.";
  if (STOP[s.state]) show(s);
});
if (welcome) {
  begin();
} else {
  $("invite").hidden = false;
  $("code").focus();
}
addEventListener("pagehide", () => navigator.sendBeacon && navigator.sendBeacon(`${base}/cancel`));
</script>"""

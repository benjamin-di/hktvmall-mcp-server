"""My connections: each person connects their own HKTVmall account and decides whom they share it with.

People authenticate through OIDC (signin-window's ``BrowserAuth``); the administrator-owned ``person_id`` claim
selects their record, created on first contact. Only the owner connects, reconnects, disconnects or shares a
connection; a member of a shared connection can stop using it and nothing else (docs/PERMISSIONS.md).
"""

from __future__ import annotations

import html
import logging
import os
from urllib.parse import parse_qs, urlsplit

from signin_window.client import LoginClient
from signin_window.oidc import BrowserAuth, OIDCConfig
from signin_window.page import PageText
from signin_window.relay import AttemptContext, LinkLimited, Relay
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .accounts import _tag, mask_email
from .config import ConfigError
from .errors import HktvError

log = logging.getLogger(__name__)

FRESH_SECONDS = 300  # Connect and Share need an identity-provider confirmation at most this old
DEFAULT_NAME = "Your account"  # signin-window's stand-in when the provider sends no name


def configured_auth(settings, env=None):
    env = os.environ if env is None else env
    try:
        claim = env.get("HKTVMALL_CONNECTIONS_ACCOUNT_CLAIM", "person_id")
        return BrowserAuth(
            OIDCConfig(
                env["HKTVMALL_CONNECTIONS_ISSUER"].rstrip("/"),
                env["HKTVMALL_CONNECTIONS_CLIENT_ID"],
                settings.public_url,
                settings.state_dir / "browser-sessions",
                cookie_name="__Host-hktvmall-connections",
                required_group=env.get("HKTVMALL_CONNECTIONS_GROUP", "mcp-hktvmall"),
                claim_names=(claim,),
            )
        ), claim
    except (ValueError, KeyError, OSError):
        raise ConfigError("Configure connection OIDC before enabling browser sign-in") from None


class Connections:
    def __init__(self, accounts, *, auth=None, claim="person_id", client=None):
        self.accounts, self.settings = accounts, accounts.settings
        if not accounts.people or self.settings.session_key is None:
            raise ConfigError("My connections needs people mode (HKTVMALL_TRUSTED_ACCOUNT_HEADER) and a session key")
        if auth is None:
            auth, claim = configured_auth(self.settings)
        self.auth, self.claim = auth, claim
        if auth.config.public_url != self.settings.public_url:
            raise ConfigError("Connection authentication must use the fixed application address")
        self.prefix = urlsplit(self.settings.public_url).path.rstrip("/")
        self.path = self.prefix + "/connections/"
        self.client = client or LoginClient(self.settings.login_url, self.settings.login_token)
        self.relay = Relay(
            self.client,
            self.settings.public_url + "/connect",
            authorize=self.authorize,
            on_signed_in=self.adopt,
            on_ended=self.ended,
            before_start=self.before_start,
            text=PageText(
                service="HKTVmall",
                title="Connect your HKTVmall account",
                reconnect=True,
                # Every link is bound to the signed-in person who just pressed Connect (authorize refuses anyone
                # else), so the window opens at once without the intro.
                auto_start=True,
                return_url=self.path,
                intro_html="Sign in to your own HKTVmall account.",
            ).with_outcomes(
                wrong_account="<h1>Different account</h1><p>Nothing was changed. Sign in to the HKTVmall account "
                "already connected here; an account another person connected cannot be connected again.</p>"
            ),
        )
        self.app = auth.middleware(
            Starlette(
                routes=[
                    Route("/connections/", self.index),
                    Route("/connections/status", self.status_route),
                    Route("/connections/start", self.start, methods=["POST"]),
                    Route("/connections/disconnect", self.disconnect, methods=["POST"]),
                    Route("/connections/share", self.share, methods=["POST"]),
                    Route("/connections/unshare", self.unshare, methods=["POST"]),
                    Route("/connections/leave", self.leave, methods=["POST"]),
                    *auth.routes(),
                    *self.relay.routes("/connect"),
                ]
            )
        )

    # -- who is asking ---------------------------------------------------------------------------

    def account(self, request):
        """The signed-in person, when their record's owner binding (pinned on their first action) matches."""
        identity = self.auth.identity(request)
        if identity is None:
            return None
        try:
            person = identity["claims"].get(self.claim)
            control = self.accounts.person(person)
            if not control.owner_matches(identity):
                return None
            if identity.get("name") != DEFAULT_NAME:
                self.accounts.note_name(person, identity.get("name"))
            return person
        except HktvError:
            return None

    def fresh(self, request):
        identity = self.auth.identity(request)
        when = identity.get("reauthenticated_at") if identity else None
        return isinstance(when, (int, float)) and 0 <= self.auth.store.clock() - when <= FRESH_SECONDS

    # -- the sign-in window ----------------------------------------------------------------------

    def authorize(self, request, link, operation):
        person = self.account(request)
        if person is None or link.context is None or link.context.owner != person:
            return False
        control = self.accounts.control(person)
        try:
            control.require_active(link.context.target)
        except HktvError:
            return False
        if operation == "start":
            if not self.fresh(request):
                return False
            control.bind(self.auth.identity(request))
        return True

    def before_start(self, link):
        try:
            self.accounts.control(link.key).require_active(link.context.target)
        except HktvError:
            return "expired"
        return None

    def adopt(self, link, result):
        try:
            self.accounts.adopt_owned(
                link.key, link.context.target, result["email"], result["cookies"], result["user_agent"]
            )
        except HktvError as exc:
            return "wrong_account" if exc.code == "account_mismatch" else "failed"
        except (KeyError, TypeError, ValueError, OSError):
            return "failed"
        return "connected"

    @staticmethod
    def ended(link, result):
        return result.get("state")

    # -- state -----------------------------------------------------------------------------------

    def status(self, person):
        """The person's own connection: connected, sign-in needed, attention, connecting or stopped."""
        control = self.accounts.control(person).read()
        if control["state"] != "active":
            return {"state": control["state"]}
        pending = next(
            (link for link in self.relay.links(person) if link.state in ("ready", "starting", "waiting", "saving")),
            None,
        )
        if pending:
            return {"state": "connecting", "url": pending.url}
        try:
            state = self.accounts.get(person).session.status()
        except HktvError:
            state = {"session_source": {"state": "invalid"}}
        email = self.accounts.email(person)
        source = state.get("session_source", {}).get("state")
        connection = (
            "sign_in_needed"
            if state.get("state") == "dead" or source == "missing"
            else (
                "check_needed"
                if source == "invalid"
                else "connected"
                if state.get("signed_in") or state.get("generation", 0)
                else "sign_in_needed"
            )
        )
        return {"state": connection, "account": mask_email(email) if email else None}

    def view(self, person):
        """What the page shows: the person's own connection, or the one shared with them, or none yet."""
        own = self.accounts.owns(person)
        owner = None if own else self.accounts.sharer(person)
        if owner is not None:
            state = {k: v for k, v in self.status(owner).items() if k == "state"}  # never the owner's sign-in link
            return {"role": "member", "owner": owner, **state}
        return {"role": "owner" if own else "none", **self.status(person)}

    def candidates(self, person):
        """Everyone else on the roster and whether this owner can share with them."""
        shared = set(self.accounts.control(person).read().get("shared_with", []))
        out = []
        for other in self.accounts.known():
            if other == person:
                continue
            entry = {"person": other, "name": self.accounts.name(other), "shared": other in shared}
            if not entry["shared"]:
                try:
                    if self.accounts.owns(other):
                        entry["blocked"] = "has their own HKTVmall connection"
                    elif (current := self.accounts.sharer(other)) not in (None, person):
                        entry["blocked"] = f"uses {self.accounts.name(current)}'s"
                except HktvError:
                    entry["blocked"] = "cannot be added right now"
            out.append(entry)
        return out

    # -- forms -----------------------------------------------------------------------------------

    def changed(self):
        return self.page(
            '<p role="alert">This connection changed or could not complete the action.</p>'
            f'<p><a href="{html.escape(self.path, quote=True)}">Open My connections</a></p>',
            status_code=409,
        )

    def form(self, action, label, *, control=None, person=None, kind="button"):
        control = control or {}
        hidden = "".join(
            f'<input type="hidden" name="{name}" value="{html.escape(str(value), quote=True)}">'
            for name, value in (("epoch", control.get("epoch", "")), ("state", control.get("state", "")))
        )
        if person is not None:
            hidden += f'<input type="hidden" name="person" value="{html.escape(person, quote=True)}">'
        return (
            f'<form method="post" action="{self.path}{action}" style="display:inline">{hidden}'
            f'<button class="{kind}">{html.escape(label)}</button></form>'
        )

    @staticmethod
    async def intent(request, *, person=False):
        raw = b""
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > 4096:
                raise HktvError("connection_disconnected")
        expected = {"epoch", "state", "person"} if person else {"epoch", "state"}
        try:
            fields = parse_qs(raw.decode(), keep_blank_values=True, max_num_fields=4)
            if set(fields) != expected or any(len(value) != 1 or not value[0] for value in fields.values()):
                raise ValueError
            values = [fields[name][0] for name in ("epoch", "state", "person") if name in expected]
            return tuple(values)
        except (ValueError, UnicodeError):
            raise HktvError("connection_disconnected") from None

    # -- pages -----------------------------------------------------------------------------------

    def page(self, body, *, headers=None, status_code=200):
        return HTMLResponse(
            '<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>My HKTVmall connection</title><style>"
            "main{max-width:38rem;margin:3rem auto;padding:1rem;font:18px system-ui;line-height:1.5}"
            "button{font:inherit;padding:.3rem .9rem;margin:.2rem .4rem .2rem 0;cursor:pointer}"
            "ul{list-style:none;padding:0}li{padding:.4rem 0;border-bottom:1px solid #ddd}"
            ".muted{color:#666}.quiet{background:none;border:1px solid #999}"
            "</style><main><h1>My HKTVmall connection</h1>" + body + "</main></html>",
            headers={"Cache-Control": "no-store", **(headers or {})},
            status_code=status_code,
        )

    async def index(self, request):
        if self.auth.identity(request) is None:
            return RedirectResponse(self.path + "login", status_code=303)
        person = self.account(request)
        if person is None:
            return self.page(
                '<p role="alert">This sign-in cannot manage this HKTVmall record. Ask the administrator.</p>'
                + self.form("logout", "Sign out", kind="quiet")
            )
        try:
            view = self.view(person)
        except HktvError:
            return self.page(
                '<p role="alert">HKTVmall cannot be shown right now: a connection record needs the administrator\'s '
                "attention.</p>" + self.form("logout", "Sign out", kind="quiet")
            )
        headers = {}
        if view["state"] in ("connecting", "disconnecting"):
            headers["Refresh"] = "2"  # follow a kept save that completes just after this read
        body = self.member_body(person, view) if view["role"] == "member" else self.owner_body(person, view)
        body += (
            '<p class="muted">Connecting grants no new assistant access. Disconnect affects this server, not your '
            "other devices.</p>" + self.form("logout", "Sign out", kind="quiet")
        )
        return self.page(body, headers=headers)

    def owner_body(self, person, view):
        control = self.accounts.control(person).read()
        state = view["state"]
        if view["role"] == "none" and state == "sign_in_needed":
            return (
                '<p role="status">No HKTVmall account connected yet.</p>'
                "<p>Connect your own HKTVmall account: you sign in on HKTVmall's own page in a window shown here. "
                "Or, if someone in your household already connected theirs, ask them to share it with you "
                "here.</p>" + self.form("start", "Connect HKTVmall", control=control) + self.sharing_help()
            )
        label = {
            "connected": "Connected",
            "sign_in_needed": "Sign-in needed",
            "check_needed": "Connection needs attention",
            "connecting": "Sign-in in progress",
            "disconnecting": "Disconnect pending",
            "disconnected": "Server access stopped",
        }[state]
        body = '<p role="status">' + label
        if view.get("account"):
            body += " · " + html.escape(view["account"])
        body += "</p>"
        if state == "disconnecting":
            body += self.form("disconnect", "Retry disconnect", control=control)
        elif state == "connecting":
            body += f'<p><a href="{html.escape(view["url"], quote=True)}">Continue sign-in</a></p>'
        else:
            body += self.form("start", "Reconnect" if state == "connected" else "Connect", control=control)
            if state != "disconnected":
                body += self.form("disconnect", "Disconnect", control=control, kind="quiet")
        return body + (self.sharing_body(person, control) if view["role"] == "owner" else self.sharing_help())

    @staticmethod
    def sharing_help():
        return (
            "<h2>Sharing</h2><p>After connecting your account, use <strong>Shared with</strong> on this page to "
            "share your basket and order history. Only the person who connected the account can share it. "
            "To use someone else's account, ask them to open this page with their own sign-in and share with you. "
            "You do not need to connect an account of your own first.</p>"
        )

    def sharing_body(self, person, control):
        body = (
            "<h2>Shared with</h2><p>People you share with can shop in this basket and see its order history "
            "through their assistant. They cannot see your password, reconnect or disconnect it.</p>"
        )
        candidates = self.candidates(person)
        if not candidates:
            return body + (
                '<p class="muted">Nobody else has used HKTVmall here yet. People appear once they have used '
                "HKTVmall through their assistant or opened this page. To add someone, the administrator adds "
                "them to the HKTVmall group; then send them this page.</p>"
            )
        items = []
        for entry in candidates:
            name = html.escape(entry["name"])
            if entry["name"] != entry["person"]:
                name += f' <span class="muted">({html.escape(entry["person"])})</span>'
            if entry["shared"]:
                items.append(
                    f"<li>✓ {name} "
                    + self.form("unshare", "Stop sharing", control=control, person=entry["person"], kind="quiet")
                    + "</li>"
                )
            elif "blocked" in entry:
                items.append(f'<li class="muted">{name}: {html.escape(entry["blocked"])}</li>')
            else:
                items.append(
                    f"<li>{name} " + self.form("share", "Share", control=control, person=entry["person"]) + "</li>"
                )
        return body + "<ul>" + "".join(items) + "</ul>"

    def member_body(self, person, view):
        owner = view["owner"]
        name = html.escape(self.accounts.name(owner))
        control = self.accounts.control(owner).read()
        state = view["state"]
        if state == "connected":
            status = f"Using {name}'s HKTVmall account."
            detail = (
                f"{name} shares it with you: your assistant shops in this household basket. Only {name} can "
                "reconnect or disconnect it."
            )
        elif state == "connecting":
            status, detail = f"{name} is reconnecting HKTVmall.", "Nothing to do here."
        else:
            status = f"{name} needs to reconnect HKTVmall."
            detail = f"Ask {name} to open My connections; there is nothing to do here."
        return (
            f'<p role="status">{status}</p><p>{detail}</p>'
            + self.form("leave", "Stop using", control=control, person=owner, kind="quiet")
            + '<p class="muted">Stop using it to connect your own HKTVmall account instead.</p>'
        )

    async def status_route(self, request):
        person = self.account(request)
        if person is None:
            return Response(status_code=401)
        try:
            return JSONResponse(self.view(person))
        except HktvError:
            return JSONResponse({"state": "check_needed"}, status_code=503)

    # -- actions ---------------------------------------------------------------------------------

    async def start(self, request):
        person = self.account(request)
        if person is None or not self.auth.origin_ok(request):
            return Response(status_code=403)
        if not self.fresh(request):
            return RedirectResponse(self.path + "login?reauth=1", status_code=303)
        control = self.accounts.control(person)
        try:
            expected, state = await self.intent(request)
            if not self.accounts.owns(person) and self.accounts.sharer(person) is not None:
                return self.changed()  # a member stops using the shared connection first
            async with control.lifecycle():
                if self.account(request) != person or not self.fresh(request):
                    return Response(status_code=403)
                epoch = control.resume(expected, state, self.auth.identity(request))
                self.accounts.forget(person)
                link = self.relay.create(
                    person,
                    params={"key": person, "epoch": epoch},
                    context=AttemptContext(owner=person, target=epoch),
                )
        except HktvError:
            return self.changed()
        except LinkLimited:
            return self.page(
                '<p role="alert">Too many sign-in attempts in the last hour. Try again later.</p>'
                f'<p><a href="{html.escape(self.path, quote=True)}">Open My connections</a></p>',
                status_code=429,
            )
        return RedirectResponse(link.url, status_code=303)

    async def disconnect(self, request):
        person = self.account(request)
        if person is None or not self.auth.origin_ok(request):
            return Response(status_code=403)
        control = self.accounts.control(person)
        try:
            expected, state = await self.intent(request)
            if not self.accounts.owns(person) and control.read()["state"] != "disconnecting":
                return self.changed()  # nothing of their own to disconnect (a member cannot stop the owner's)
            epoch = control.suspend(expected, state, self.auth.identity(request))
            async with control.lifecycle():
                current = control.read()
                if current["epoch"] != epoch or current["state"] != "disconnecting":
                    return self.changed()
                for link in self.relay.links(person):
                    await run_in_threadpool(self.relay.cancel, link)
                await run_in_threadpool(self.accounts.disconnect_owned, person, epoch)
        except HktvError:
            return self.changed()
        except Exception as e:
            log.warning("person %s: disconnect incomplete (%s); the barrier stays", _tag(person), type(e).__name__)
        return RedirectResponse(self.path, status_code=303)

    async def share(self, request):
        person = self.account(request)
        if person is None or not self.auth.origin_ok(request):
            return Response(status_code=403)
        if not self.fresh(request):
            return RedirectResponse(self.path + "login?reauth=1", status_code=303)
        try:
            epoch, state, member = await self.intent(request, person=True)
            self.accounts.control(person).bind(self.auth.identity(request))
            await run_in_threadpool(self.accounts.share, person, member, epoch, state)
        except HktvError:
            return self.changed()
        return RedirectResponse(self.path, status_code=303)

    async def unshare(self, request):
        person = self.account(request)
        if person is None or not self.auth.origin_ok(request):
            return Response(status_code=403)
        try:
            epoch, state, member = await self.intent(request, person=True)
            self.accounts.control(person).bind(self.auth.identity(request))
            await run_in_threadpool(self.accounts.unshare, person, member, epoch, state)
        except HktvError:
            return self.changed()
        return RedirectResponse(self.path, status_code=303)

    async def leave(self, request):
        person = self.account(request)
        if person is None or not self.auth.origin_ok(request):
            return Response(status_code=403)
        try:
            epoch, state, owner = await self.intent(request, person=True)
            if self.accounts.owns(person) or self.accounts.sharer(person) != owner:
                return self.changed()
            await run_in_threadpool(self.accounts.unshare, owner, person, epoch, state)
        except HktvError:
            return self.changed()
        return RedirectResponse(self.path, status_code=303)


class BrowserRoutes:
    def __init__(self, app, connections):
        self.app, self.connections = app, connections

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and scope.get("path", "").startswith(("/connections/", "/connect/")):
            if (
                scope["type"] == "http"
                and scope["path"].count("/") == 2
                and scope["path"].startswith("/connect/")
                and not self.connections.fresh(Request(scope))
            ):
                response = RedirectResponse(
                    self.connections.path + "login?reauth=1&next=" + self.connections.prefix + scope["path"],
                    status_code=303,
                )
                return await response(scope, receive, send)
            return await self.connections.app(scope, receive, send)
        return await self.app(scope, receive, send)

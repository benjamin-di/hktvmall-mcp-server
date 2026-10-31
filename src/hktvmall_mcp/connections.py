"""My connections: each person connects their own HKTVmall account and decides whom they share it with.

People authenticate through OIDC (signin-window's ``BrowserAuth``); the administrator-owned ``person_id`` claim
selects their record, created on first contact. Only the owner connects, reconnects, disconnects or shares a
connection. A recipient chooses or leaves shared access and independently manages their own slot (docs/PERMISSIONS.md).
"""

from __future__ import annotations

import html
import logging
import os
from urllib.parse import parse_qs, urlsplit

from account_connections.ui import action_form, selection_controls
from signin_window.client import LoginClient
from signin_window.connections import page as connection_page
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
                    Route("/connections/select", self.select, methods=["POST"]),
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
        """The selected connection; management of the owned connection is independent."""
        own = self.accounts.owns(person)
        selected = self.accounts.control(person).read()["selection"]
        if selected is None:
            return {"role": "none", "state": "selection_needed"}
        owner = selected["account"]
        if owner != person:
            grant = self.accounts.control(owner).read()["shares"].get(person)
            if grant is None or grant["revision"] != selected["grant"]:
                return {"role": "member", "owner": owner, "state": "access_removed"}
            state = {k: v for k, v in self.status(owner).items() if k == "state"}  # never the owner's sign-in link
            return {"role": "member", "owner": owner, **state}
        return {"role": "owner" if own else "none", **self.status(person)}

    def candidates(self, person):
        """Everyone else on the roster and whether this owner can share with them."""
        shared = set(self.accounts.control(person).read().get("shared_with", []))
        out = []
        # Existing grants must remain revocable even if the recipient's control record is lost.
        for other in sorted(set(self.accounts.known()) | shared):
            if other == person:
                continue
            entry = {"person": other, "name": self.accounts.name(other), "shared": other in shared}
            out.append(entry)
        return out

    # -- forms -----------------------------------------------------------------------------------

    def changed(self, exc=None):
        message = {
            "account_changed": "The account choice or sharing access changed in another window. Refresh and try again.",
            "shared_access_removed": "This sharing offer was removed or changed. Choose an available account.",
        }.get(exc.code if exc else None, "This connection changed or could not complete the action.")
        return self.page(
            f'<p role="alert">{message}</p>'
            f'<p><a href="{html.escape(self.path, quote=True)}">Back to HKTVmall accounts</a></p>',
            status_code=409,
        )

    def form(self, action, label, *, control=None, person=None, kind="button", extra=None, content=""):
        control = control or {}
        hidden = "".join(
            f'<input type="hidden" name="{name}" value="{html.escape(str(value), quote=True)}">'
            for name, value in (("epoch", control.get("epoch", "")), ("state", control.get("state", "")))
        )
        if person is not None:
            hidden += f'<input type="hidden" name="person" value="{html.escape(person, quote=True)}">'
        hidden += "".join(
            f'<input type="hidden" name="{name}" value="{html.escape(str(value), quote=True)}">'
            for name, value in (extra or {}).items()
        )
        if action == "disconnect":
            kind = "danger"
        elif label == "Reconnect":
            kind = "secondary"
        return (
            f'<form method="post" action="{self.path}{action}">{hidden}'
            + content
            + f'<button class="{kind}">{html.escape(label)}</button></form>'
        )

    @staticmethod
    async def intent(request, *, person=False, extra=(), optional=()):
        raw = b""
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > 4096:
                raise HktvError("connection_disconnected")
        expected = {"epoch", "state", "person"} if person else {"epoch", "state"}
        expected.update(extra)
        try:
            fields = parse_qs(raw.decode(), keep_blank_values=True, max_num_fields=8)
            if not expected <= set(fields) <= expected | set(optional) or any(
                len(value) != 1 or not value[0] for value in fields.values()
            ):
                raise ValueError
            values = [fields[name][0] for name in ("epoch", "state", "person") if name in expected]
            values += [fields[name][0] for name in extra]
            values += [fields[name][0] if name in fields else None for name in optional]
            return tuple(values)
        except (ValueError, UnicodeError):
            raise HktvError("connection_disconnected") from None

    # -- pages -----------------------------------------------------------------------------------

    def page(self, body, *, headers=None, status_code=200, refresh=False):
        return HTMLResponse(
            connection_page("HKTVmall accounts", body, refresh=refresh, login_url=self.path + "login"),
            headers={"Cache-Control": "no-store", **(headers or {})},
            status_code=status_code,
        )

    async def index(self, request):
        if self.auth.identity(request) is None:
            return RedirectResponse(self.path + "login", status_code=303)
        person = self.account(request)
        if person is None:
            return self.page(
                '<p role="alert">This sign-in cannot manage this HKTVmall connection. '
                "Sign in with another account, or ask your administrator to check access.</p>"
                + self.form("logout", "Sign out of Connections", kind="quiet")
            )
        body = "<p>Choose which basket your assistant uses. Switching never moves or changes basket items.</p>"
        view, own = {}, {}
        try:
            view = self.view(person)
            body += self.selection_body(person, view)
        except HktvError:
            body += self.unavailable("The selected account")
        try:
            own = {"role": "owner" if self.accounts.owns(person) else "none", **self.status(person)}
            body += self.owner_body(person, own)
        except HktvError:
            body += self.unavailable("Your own connection")
        try:
            body += self.offers_body(person)
        except HktvError:
            body += self.unavailable("Shared account choices")
        try:
            body += (
                self.sharing_body(person, self.accounts.control(person).read())
                if self.accounts.owns(person)
                else self.sharing_help()
            )
        except HktvError:
            body += self.unavailable("Sharing management")
        refresh = any(v.get("state") in ("connecting", "disconnecting") for v in (view, own))
        body += (
            '<p class="muted">Connecting HKTVmall does not give an assistant permission to use it. '
            "Signing out of Connections keeps your saved connections.</p>"
            + self.form("logout", "Sign out of Connections", kind="quiet")
        )
        return self.page(body, refresh=refresh)

    @staticmethod
    def unavailable(section):
        return (
            f'<p role="alert">{section} cannot be shown: a connection record needs the administrator’s attention. '
            "Other available account controls can still be used.</p>"
        )

    def selection_body(self, person, view):
        if view["state"] == "selection_needed":
            text = "No account selected. Choose an account below to continue."
        elif view["role"] == "member":
            name = self.accounts.name(view["owner"])
            text = {
                "access_removed": f"Access to {name}'s account was removed or changed. Choose an available account.",
                "connected": f"Using {name}'s HKTVmall account.",
                "connecting": f"{name} is reconnecting HKTVmall. Your assistant is waiting for this account.",
            }.get(view["state"], f"{name} needs to reconnect HKTVmall. Ask them to open Connections and reconnect.")
        elif view["role"] == "none":
            text = "No HKTVmall account connected yet. Connect your own or choose a shared account below."
        else:
            text = "Using my HKTVmall account."
            if view["state"] != "connected":
                text += " Reconnect your account to continue."
        return (
            '<section class="card panel active" aria-labelledby="selected-heading"><h2 id="selected-heading">'
            "Your assistant’s account</h2>" + selection_controls(text, ()) + "</section>"
        )

    def choice_form(self, person, owner, grant, label):
        control = self.accounts.control(person).read()
        return action_form(
            self.path + "select",
            {
                "epoch": control["epoch"],
                "state": control["state"],
                "person": owner,
                "selection_revision": control["selection_revision"],
                "grant": grant or "own",
            },
            label,
        )

    def owner_body(self, person, view):
        control = self.accounts.control(person).read()
        state = view["state"]
        selected = control["selection"] == {"account": person, "grant": None}
        label = {
            "connected": "Connection saved",
            "sign_in_needed": "Sign-in needed",
            "check_needed": "We could not check this connection — try reconnecting",
            "connecting": "Sign-in in progress",
            "disconnecting": "Access stopped — finish disconnecting",
            "disconnected": "Disconnected",
        }[state]
        body = '<section class="card panel"><h2>My account</h2><p>' + label
        if view.get("account"):
            body += " · " + html.escape(view["account"])
        if view["role"] == "owner":
            body += " · " + ("Selected" if selected else "Not currently in use")
        body += "</p>"
        if view["role"] == "owner" and not selected:
            body += self.choice_form(person, person, None, "Use my account")
        if state == "disconnecting":
            body += self.form("disconnect", "Finish disconnecting", control=control)
        elif state == "connecting":
            body += f'<p><a href="{html.escape(view["url"], quote=True)}">Continue sign-in</a></p>'
        else:
            body += self.form("start", "Reconnect" if view["role"] == "owner" else "Connect HKTVmall", control=control)
            if view["role"] == "owner" and state != "disconnected":
                members = len(control["shared_with"])
                effect = (
                    f"This also stops shared access for {members} {'person' if members == 1 else 'people'} "
                    "until you reconnect. "
                    if members
                    else ""
                )
                body += (
                    "<details class=manage><summary>Disconnect my account</summary><p>"
                    + effect
                    + "Your selected shared account, if any, is unaffected. "
                    "Your saved sign-in is removed from this application. "
                    "Your order history and browser profile are kept. "
                    "It does not sign you out on other devices.</p>"
                    + self.form("disconnect", "Disconnect", control=control, kind="quiet")
                    + "</details>"
                )
        body += "</section>"
        return body

    @staticmethod
    def sharing_help():
        return (
            "<h2>Sharing</h2><p>After connecting your account, use <strong>Shared with</strong> on this page. "
            "To use someone else's account, ask them to share it with you, then choose it below. "
            "You do not need to connect an account of your own first.</p>"
        )

    def sharing_body(self, person, control):
        body = (
            "<h2>Shared with</h2><p>Share shopping access to your own account. People you share with can read and "
            "change this basket through their assistant. Order history is optional. They cannot see your password, "
            "reconnect or disconnect it. They choose when to use it; sharing never switches their account.</p>"
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
            member = entry["person"]
            name = html.escape(entry["name"])
            if entry["name"] != member and sum(other["name"] == entry["name"] for other in candidates) > 1:
                name += f' <span class="muted">({html.escape(member)})</span>'
            grant = control["shares"].get(member)
            check_id = "history-" + member
            checkbox = (
                f'<label for="{check_id}"><input id="{check_id}" type="checkbox" name="history" value="1"'
                + (" checked" if grant and grant["order_history"] else "")
                + "> Also allow reading order history</label>"
            )
            item = "<li>" + ("✓ " if grant else "") + name
            item += self.form(
                "share",
                "Update access" if grant else "Share",
                control=control,
                person=member,
                extra={"grant": grant["revision"] if grant else "new"},
                content=checkbox,
            )
            if grant:
                item += self.form(
                    "unshare",
                    "Stop sharing",
                    control=control,
                    person=member,
                    kind="quiet",
                    extra={"grant": grant["revision"]},
                )
            items.append(item + "</li>")
        return body + "<ul>" + "".join(items) + "</ul>"

    def offers_body(self, person):
        body = "<h2>Shared accounts available to me</h2>"
        offers, unavailable = self.accounts.offers(person)
        if unavailable:
            body += self.unavailable("Some shared accounts")
        if not offers:
            return body + (
                '<p class="muted">No shared accounts available. Ask the account owner to share with you.</p>'
                if not unavailable
                else ""
            )
        selected = self.accounts.control(person).read()["selection"]
        for offer in offers:
            owner, grant = offer["owner"], offer["revision"]
            name = html.escape(self.accounts.name(owner))
            body += f'<section class="card panel"><h3>{name} <span class="muted">({html.escape(owner)})</span></h3>'
            body += "<p>Your assistant can read and change this basket"
            body += (
                " and read its order history.</p>" if offer["order_history"] else ". Order history is not shared.</p>"
            )
            if selected == {"account": owner, "grant": grant}:
                body += "<p>Selected. Your own connection stays available.</p>"
            else:
                body += self.choice_form(person, owner, grant, f"Use {self.accounts.name(owner)}'s account")
            body += self.form(
                "leave",
                "Leave shared account" if selected and selected["account"] == owner else "Decline access",
                control=self.accounts.control(owner).read(),
                person=owner,
                kind="quiet",
                extra={"grant": grant},
            )
            body += "</section>"
        return body

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
                f'<p><a href="{html.escape(self.path, quote=True)}">Back to HKTVmall accounts</a></p>',
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
            epoch, state, member, grant, history = await self.intent(
                request, person=True, extra=("grant",), optional=("history",)
            )
            if history not in (None, "1"):
                return self.changed()
            self.accounts.control(person).bind(self.auth.identity(request))
            await run_in_threadpool(
                self.accounts.share, person, member, epoch, state, history=history == "1", grant=grant
            )
        except HktvError as exc:
            return self.changed(exc)
        return RedirectResponse(self.path, status_code=303)

    async def unshare(self, request):
        person = self.account(request)
        if person is None or not self.auth.origin_ok(request):
            return Response(status_code=403)
        try:
            epoch, state, member, grant = await self.intent(request, person=True, extra=("grant",))
            self.accounts.control(person).bind(self.auth.identity(request))
            await run_in_threadpool(self.accounts.unshare, person, member, epoch, state, grant=grant)
        except HktvError as exc:
            return self.changed(exc)
        return RedirectResponse(self.path, status_code=303)

    async def leave(self, request):
        person = self.account(request)
        if person is None or not self.auth.origin_ok(request):
            return Response(status_code=403)
        try:
            epoch, state, owner, grant = await self.intent(request, person=True, extra=("grant",))
            self.accounts.control(person).bind(self.auth.identity(request))
            await run_in_threadpool(self.accounts.unshare, owner, person, epoch, state, grant=grant, leaving=True)
        except HktvError as exc:
            return self.changed(exc)
        return RedirectResponse(self.path, status_code=303)

    async def select(self, request):
        person = self.account(request)
        if person is None or not self.auth.origin_ok(request):
            return Response(status_code=403)
        try:
            epoch, state, owner, expected, grant = await self.intent(
                request, person=True, extra=("selection_revision", "grant")
            )
            control = self.accounts.control(person)
            control.expected(control.read(), epoch, state)
            control.bind(self.auth.identity(request))
            await run_in_threadpool(self.accounts.select, person, owner, expected, None if grant == "own" else grant)
        except HktvError as exc:
            return self.changed(exc)
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

"""OIDC-owned connections around the existing account and session boundaries."""

from __future__ import annotations

import html
import os
from urllib.parse import parse_qs, urlsplit

from signin_window.client import LoginClient
from signin_window.oidc import BrowserAuth, OIDCConfig
from signin_window.page import PageText
from signin_window.relay import AttemptContext, Relay
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .accounts import DEFAULT, mask_email
from .config import ConfigError
from .errors import HktvError
from .login_client import RoutedLoginClient


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
    def __init__(self, accounts, *, auth=None, claim="person_id", client=None, default_client=None):
        self.accounts, self.settings = accounts, accounts.settings
        try:
            if not accounts.bindings():
                raise HktvError("not_connected")
            accounts.managers()  # ambiguous shared account ownership refuses startup
        except HktvError:
            raise ConfigError("Configure reviewed account-use and connection-management bindings") from None
        if auth is None:
            auth, claim = configured_auth(self.settings)
        self.auth, self.claim = auth, claim
        if auth.config.public_url != self.settings.public_url:
            raise ConfigError("Connection authentication must use the fixed application address")
        self.prefix = urlsplit(self.settings.public_url).path.rstrip("/")
        self.path = self.prefix + "/connections/"
        normal = client or LoginClient(self.settings.login_url, self.settings.login_token)
        default_client = default_client or (
            LoginClient(self.settings.default_login_url, self.settings.login_token)
            if self.settings.default_login_url
            else None
        )
        if DEFAULT in accounts.bindings().values() and default_client is None and client is None:
            raise ConfigError("Configure the default account's private custodian recovery backend")
        self.client = RoutedLoginClient(normal, default_client or client)
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
                # Every link is bound to the signed-in account that just pressed Connect (authorize refuses any
                # other), so the window opens at once without the intro.
                auto_start=True,
                return_url=self.path,
                intro_html="Sign in to the HKTVmall account already bound to this connection.",
            ).with_outcomes(
                wrong_account="<h1>Different account</h1><p>The saved connection was not changed. "
                "Sign in to the account already connected here.</p>"
            ),
        )
        self.app = auth.middleware(
            Starlette(
                routes=[
                    Route("/connections/", self.index),
                    Route("/connections/status", self.status_route),
                    Route("/connections/start", self.start, methods=["POST"]),
                    Route("/connections/disconnect", self.disconnect, methods=["POST"]),
                    *auth.routes(),
                    *self.relay.routes("/connect"),
                ]
            )
        )

    def account(self, request):
        identity = self.auth.identity(request)
        if identity is None:
            return None
        try:
            person = identity["claims"].get(self.claim)
            account = self.accounts.account_for(person)
            if self.accounts.managers().get(account) != person:
                return None
            return account if self.accounts.control(account).owner_matches(identity) else None
        except HktvError:
            return None

    def fresh(self, request):
        identity = self.auth.identity(request)
        when = identity.get("reauthenticated_at") if identity else None
        return isinstance(when, (int, float)) and 0 <= self.auth.store.clock() - when <= 300

    def authorize(self, request, link, operation):
        account = self.account(request)
        if account is None or link.context is None or link.context.owner != account:
            return False
        control = self.accounts.control(account)
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

    def ended(self, link, result):
        state = result.get("state")
        if link.key == DEFAULT and state == "connected":
            try:
                self.accounts.control(DEFAULT).require_active(link.context.target)
            except HktvError:
                return "expired"
        return state

    def status(self, account):
        control = self.accounts.control(account).read()
        if control["state"] != "active":
            return {"state": control["state"], "managed": account == DEFAULT}
        pending = next(
            (link for link in self.relay.links(account) if link.state in ("new", "starting", "waiting", "saving")), None
        )
        if pending:
            return {"state": "connecting", "url": pending.url, "managed": False}
        try:
            service = self.accounts.get(account)
            state = service.session.status()
            email = service.session.expected_email or None
        except HktvError:
            state, email = {}, self.accounts.control(account).read()["provider_email"]
        source = state.get("session_source", {}).get("state")
        connection = (
            "sign_in_needed"
            if state.get("state") == "dead"
            else (
                "check_needed"
                if source in ("invalid", "missing") and state.get("generation", 0)
                else "connected"
                if state.get("signed_in") or state.get("generation", 0)
                else "sign_in_needed"
            )
        )
        if account == DEFAULT and self.settings.snapshot_path is not None:
            connection = self.accounts.control(account).health(state.get("generation", 0)) or connection
        return {
            "state": connection,
            "account": mask_email(email) if email else None,
            "managed": account == DEFAULT,
        }

    def changed(self):
        return HTMLResponse(
            '<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>My HKTVmall connection</title><main style="max-width:38rem;margin:3rem auto;padding:1rem;'
            'font:18px system-ui;line-height:1.5"><h1>My HKTVmall connection</h1>'
            '<p role="alert">This connection changed or could not complete the action.</p>'
            f'<p><a href="{html.escape(self.path, quote=True)}">Open current connection</a></p></main></html>',
            status_code=409,
        )

    def form(self, action, label, *, control=None):
        control = control or {}
        return (
            f'<form method="post" action="{self.path}{action}">'
            f'<input type="hidden" name="epoch" value="{control.get("epoch", "")}">'
            f'<input type="hidden" name="state" value="{control.get("state", "")}">'
            f"<button>{label}</button></form>"
        )

    @staticmethod
    async def intent(request):
        raw = await request.body()
        if len(raw) > 4096:
            raise HktvError("connection_disconnected")
        try:
            fields = parse_qs(raw.decode(), keep_blank_values=True, max_num_fields=4)
            if set(fields) != {"epoch", "state"} or any(len(value) != 1 or not value[0] for value in fields.values()):
                raise ValueError
            return fields["epoch"][0], fields["state"][0]
        except (ValueError, UnicodeError):
            raise HktvError("connection_disconnected") from None

    async def index(self, request):
        if self.auth.identity(request) is None:
            return RedirectResponse(self.path + "login", status_code=303)
        account = self.account(request)
        headers = {"Cache-Control": "no-store"}
        if account is None:
            body = (
                "<p>HKTVmall uses one shared household account, which its owner manages. "
                "You can still use HKTVmall through your assistant without connecting anything here, "
                "so there is nothing you need to do.</p>" + self.form("logout", "Sign out")
            )
        else:
            state = self.status(account)
            # Follow a kept save that completes just after this page's read.
            if state["state"] in ("connecting", "disconnecting"):
                headers["Refresh"] = "2"
            control = self.accounts.control(account).read()

            def form(action, label):
                return self.form(action, label, control=control)

            label = {
                "connected": "Connection saved",
                "sign_in_needed": "Sign-in needed",
                "check_needed": "Connection needs attention",
                "connecting": "Sign-in in progress",
                "disconnecting": "Disconnect pending",
                "disconnected": "Server access stopped",
            }[state["state"]]
            body = '<p role="status">' + label + "</p>"
            if state.get("account"):
                body += "<p>" + html.escape(state["account"]) + "</p>"
            if state["state"] == "disconnecting":
                body += form("disconnect", "Retry disconnect")
            elif state["state"] == "connecting":
                body += f'<a href="{html.escape(state["url"], quote=True)}">Continue sign-in</a>'
            else:
                body += form("start", "Reconnect" if state["state"] == "connected" else "Connect")
                if state["managed"]:
                    body += "<p>Disconnect stops server access and retains this account's saved profile.</p>"
                if state["state"] != "disconnected":
                    body += form("disconnect", "Disconnect HKTVmall")
            body += "<p>Connecting grants no new agent access. Disconnect affects this server, not other devices.</p>"
            body += form("logout", "Sign out")
        return HTMLResponse(
            '<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>My HKTVmall connection</title><main style="max-width:38rem;margin:3rem auto;padding:1rem;'
            'font:18px system-ui;line-height:1.5"><h1>My HKTVmall connection</h1>' + body + "</main></html>",
            headers=headers,
        )

    async def status_route(self, request):
        account = self.account(request)
        return JSONResponse(self.status(account)) if account else Response(status_code=401)

    async def start(self, request):
        account = self.account(request)
        if account is None or not self.auth.origin_ok(request):
            return Response(status_code=403)
        if not self.fresh(request):
            return RedirectResponse(self.path + "login?reauth=1", status_code=303)
        control = self.accounts.control(account)
        try:
            expected, state = await self.intent(request)
            async with control.lifecycle():
                if self.account(request) != account or not self.fresh(request):
                    return Response(status_code=403)
                epoch = control.resume(expected, state, self.auth.identity(request))
                self.accounts.forget(account)
                link = self.relay.create(
                    account,
                    params={"key": account, "epoch": epoch},
                    context=AttemptContext(owner=account, target=epoch),
                )
        except HktvError:
            return self.changed()
        return RedirectResponse(link.url, status_code=303)

    async def disconnect(self, request):
        account = self.account(request)
        if account is None or not self.auth.origin_ok(request):
            return Response(status_code=403)
        control = self.accounts.control(account)
        try:
            expected, state = await self.intent(request)
            epoch = control.suspend(expected, state, self.auth.identity(request))
            async with control.lifecycle():
                current = control.read()
                if current["epoch"] != epoch or current["state"] != "disconnecting":
                    return self.changed()
                for link in self.relay.links(account):
                    await run_in_threadpool(self.relay.cancel, link)
                await run_in_threadpool(self.accounts.disconnect_owned, account, epoch)
        except HktvError:
            return self.changed()
        except Exception:
            pass  # retain the barrier until cleanup can be confirmed
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

"""My connections: each person connects their own HKTVmall account and decides whom they share it with.

People authenticate through OIDC (signin-window's ``BrowserAuth``); the administrator-owned ``person_id`` claim
selects their record, created on first contact. Only the owner connects, reconnects, disconnects or shares a
connection. A recipient chooses or leaves shared access and independently manages their own slot (docs/PERMISSIONS.md).
"""

from __future__ import annotations

import collections
import html
import json
import logging
import os
import secrets
import threading
from urllib.parse import parse_qs, urlsplit

from account_connections.ui import action_form, selection_controls
from signin_window.client import LoginClient
from signin_window.connections import page as connection_page
from signin_window.oidc import BrowserAuth, OIDCConfig
from signin_window.page import NO_RETRY, PageText
from signin_window.relay import AttemptContext, Link, LinkLimited, Relay
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .accounts import _tag, mask_email
from .config import ConfigError
from .errors import HktvError
from .ui import renderer_options

log = logging.getLogger(__name__)

FRESH_SECONDS = 300  # Connect and Share need an identity-provider confirmation at most this old
DEFAULT_NAME = "Your account"  # signin-window's stand-in when the provider sends no name
INCOMPLETE = "<h1>Sign-in did not complete</h1><p>Return to Connections to start a new sign-in.</p>"


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
            ),
            **renderer_options(BrowserAuth),
        ), claim
    except (ValueError, KeyError, OSError):
        raise ConfigError("Configure connection OIDC before enabling browser sign-in") from None


class _ReservedRelay(Relay):
    """Relay with binding pre-teardown slot reservation so 429 never cancels a live window."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = threading.RLock()
        self._reserved_slots = 0

    def reserve_slot(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            made = self._made.setdefault(key, collections.deque())
            while made and now - made[0] > 3600:
                made.popleft()
            if len(made) >= self.links_per_hour:
                return False
            self._expire(now)
            if len(self._retired) + self._reserved_slots >= self.max_links:
                return False
            self._reserved_slots += 1
            return True

    def release_slot(self) -> None:
        with self._lock:
            if self._reserved_slots > 0:
                self._reserved_slots -= 1

    def create(
        self,
        key: str,
        *,
        params: dict | None = None,
        label: str | None = None,
        context: AttemptContext | None = None,
        reserved: bool = False,
    ) -> Link:
        if self._authorize is not None and (context is None or not context.owner):
            raise ValueError("authenticated links require a host-assigned owner")
        to_retire: list[Link] = []
        with self._lock:
            if reserved and self._reserved_slots > 0:
                self._reserved_slots -= 1
            now = self._clock()
            made = self._made.setdefault(key, collections.deque())
            while made and now - made[0] > 3600:
                made.popleft()
            if len(made) >= self.links_per_hour:
                raise LinkLimited(key)
            self._expire(now)
            if not reserved and len(self._retired) + self._reserved_slots >= self.max_links:
                raise LinkLimited(key)
            made.append(now)
            for old in [item for item in self._links.values() if item.key == key]:
                self._links.pop(old.id, None)
                old.closed = True
                old.view_token = None
                if old.window_id is not None:
                    self._track_retired_locked(old)
                    if old.state != "saving":
                        to_retire.append(old)
            while len(self._links) >= self.max_links:
                oldest = self._links.pop(min(self._links.values(), key=lambda item: item.created).id)
                oldest.closed = True
                oldest.view_token = None
                if oldest.window_id is not None:
                    self._track_retired_locked(oldest)
                    if oldest.state != "saving":
                        to_retire.append(oldest)
            link_id = secrets.token_urlsafe(24)
            link = Link(
                link_id,
                key,
                now,
                now + self.link_ttl,
                f"{self.public_url}/{link_id}",
                dict(params or {}),
                key if label is None else label,
            )
            link.context = context
            self._links[link.id] = link
        for old in to_retire:
            threading.Thread(target=self._retire, args=(old,), daemon=True).start()
        return link


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
        self._pending_close: dict[str, list] = {}
        self._known_links: dict[str, dict[str, object]] = {}
        self._draining_accounts: set[str] = set()
        self.relay = _ReservedRelay(
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
                notice="Use your password or email code — device passkeys aren't available in this remote window.",
                intro_html="Sign in to your own HKTVmall account.",
                no_retry=(*NO_RETRY, "verifying", "reconnect_incomplete"),
                **renderer_options(PageText),
            ).with_outcomes(
                verifying="<h1>Verifying cart access</h1><p>Return to Connections to see when your cart is ready.</p>",
                cancelled=INCOMPLETE,
                expired=INCOMPLETE,
                reconnect_incomplete=INCOMPLETE,
                wrong_account="<h1>Different account</h1><p>Nothing was changed. Sign in to the HKTVmall account "
                "already connected here; an account another person connected cannot be connected again.</p>",
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
        return self.auth.fresh(request, FRESH_SECONDS)

    # -- the sign-in window ----------------------------------------------------------------------

    @staticmethod
    def _link_still_open(link) -> bool:
        with link.lock:
            return link.window_id is not None or link.closing or link.state in ("starting", "waiting", "saving")

    @staticmethod
    def _is_already_completed_idle(link) -> bool:
        """True only if the link was already terminal and idle (lock uncontended) before this request."""
        if not link.lock.acquire(blocking=False):
            return False
        try:
            return link.window_id is None and not link.closing and link.state in ("verifying", "connected")
        finally:
            link.lock.release()

    def _cancel_and_check(self, link) -> tuple[bool, bool]:
        self.relay.cancel(link)
        with link.lock:
            still_open = link.window_id is not None or link.closing or link.state in ("starting", "waiting", "saving")
            completed = link.state in ("verifying", "connected")
            return still_open, completed

    def _snapshot_links(self, person) -> tuple[list, set[str], set[str]]:
        """Return known/live/retired links, idle+completed IDs, and live relay link IDs in one pass."""
        seen = {}
        known = self._known_links.get(person)
        if known:
            for link_id, link in list(known.items()):
                if link.key == person:
                    seen[link_id] = link
        live_links = self.relay.links(person)
        active_relay_ids = {link.id for link in live_links if link.key == person and not link.closed}
        for link in live_links:
            if link.key == person:
                seen[link.id] = link
        with self.relay._lock:
            for link in self.relay._retired.values():
                if link.key == person:
                    seen[link.id] = link
            live_ids = set(self.relay._links) | set(self.relay._retired)
        already_completed: set[str] = set()
        for link_id, link in list(seen.items()):
            if link.lock.acquire(blocking=False):
                try:
                    still_open = (
                        link.window_id is not None or link.closing or link.state in ("starting", "waiting", "saving")
                    )
                    if not still_open and link.state in ("verifying", "connected"):
                        already_completed.add(link_id)
                finally:
                    link.lock.release()
                if known and link_id not in live_ids and not still_open:
                    known.pop(link_id, None)
        return list(seen.values()), already_completed, active_relay_ids

    def _account_links(self, person) -> list:
        links, _, _ = self._snapshot_links(person)
        return links

    async def authorize(self, request, link, operation):
        person = self.account(request)
        if person is None or link.context is None or link.context.owner != person:
            return False
        if link.closed or person in self._draining_accounts:
            return False
        control = self.accounts.control(person)
        try:
            control.require_active(link.context.target)
        except HktvError:
            return False
        if operation == "cancel" and not self.fresh(request):
            content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
            if content_type == "application/json":
                try:
                    payload = json.loads(await request.body())
                except (ValueError, UnicodeError):
                    payload = {}
                if isinstance(payload, dict) and payload.get("for_restart"):
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
        return "verifying"

    @staticmethod
    def ended(link, result):
        state = result.get("state")
        return "reconnect_incomplete" if state == "expired" else state

    # -- state -----------------------------------------------------------------------------------

    def status(self, person):
        """The person's own connection, based on the sign-in attempt and the current generation's cart check."""
        control = self.accounts.control(person).read()
        if control["state"] != "active":
            return {"state": control["state"]}
        matching = [
            link for link in self.relay.links(person) if link.context and link.context.target == control["epoch"]
        ]
        pending = (
            []
            if person in self._draining_accounts
            else [
                link
                for link in matching
                if not link.closed and link.state in ("ready", "starting", "waiting", "saving")
            ]
        )
        if pending:
            link = pending[-1]
            saving = link.state == "saving"
            layout = getattr(link, "layout", "")
            return {"state": "saving" if saving else "connecting", "url": link.url, "layout": layout}
        try:
            session = self.accounts.get(person).session
            state = session.status()
            generation = state["generation"]
            source = state["session_source"]["state"]
            cart_ok = session.cart_check(generation)
            self.accounts.control(person).require_active(control["epoch"])
        except HktvError:
            return {"state": "check_needed"}
        email = self.accounts.email(person)
        ended = next(
            (link for link in matching if not link.closed and link.state in ("cancelled", "reconnect_incomplete")),
            None,
        )
        if ended is not None or (source == "ready" and generation < control["generation"]):
            connection = "reconnect_incomplete"
        elif state["state"] == "dead" or source == "missing":
            connection = "sign_in_needed"
        elif source == "invalid" or cart_ok is False:
            connection = "check_needed"
        elif state["signed_in"] and cart_ok is True:
            connection = "connected"
        else:
            connection = "verifying"
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

    def form(self, action, label, *, control=None, person=None, kind="button", extra=None, form_id=None):
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
        elif label in ("Reconnect", "Start new sign-in"):
            kind = "secondary"
        return (
            f'<form method="post" action="{self.path}{action}"'
            + (f' id="{html.escape(form_id, quote=True)}"' if form_id else "")
            + f">{hidden}"
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
            connection_page(
                "HKTVmall accounts",
                body,
                refresh=refresh,
                login_url=self.path + "login",
                **renderer_options(connection_page),
            ),
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
        body = (
            '<p class="muted">Signed in to Connections as '
            + html.escape(self.auth.identity(request)["name"])
            + "</p><p>Choose which basket your assistant uses. Switching never moves or changes basket items.</p>"
        )
        view, own = {}, {}
        try:
            view = self.view(person)
            body += self.selection_body(person, view)
        except HktvError:
            body += self.unavailable("The selected account")
        try:
            own = {"role": "owner" if self.accounts.owns(person) else "none", **self.status(person)}
            body += self.owner_body(person, own, fresh=self.fresh(request))
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
        refresh = any(v.get("state") in ("connecting", "saving", "verifying", "disconnecting") for v in (view, own))
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
                "saving": f"{name} is saving their HKTVmall connection. Your assistant is waiting for this account.",
                "verifying": f"Verifying cart access for {name}'s HKTVmall account.",
                "check_needed": f"Cart access for {name}'s HKTVmall account could not be verified.",
            }.get(view["state"], f"{name} needs to reconnect HKTVmall. Ask them to open Connections and reconnect.")
        elif view["role"] == "none":
            text = "No HKTVmall account connected yet. Connect your own or choose a shared account below."
        else:
            text = {
                "connected": "Using my HKTVmall account.",
                "connecting": "Sign-in in progress for your selected HKTVmall account.",
                "saving": "Saving connection for your selected HKTVmall account.",
                "verifying": "Verifying cart access for your selected HKTVmall account.",
                "check_needed": "Your HKTVmall account is selected, but cart access could not be verified.",
            }.get(view["state"], "Your HKTVmall account is selected. Reconnect to continue.")
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

    def owner_body(self, person, view, fresh=True):
        control = self.accounts.control(person).read()
        state = view["state"]
        selected = control["selection"] == {"account": person, "grant": None}
        layout = view.get("layout", "")
        view_note = " (mobile view)" if layout == "phone" else " (desktop view)" if layout == "desktop" else ""
        label = {
            "connected": "Connected",
            "sign_in_needed": "Sign-in needed",
            "check_needed": "We could not verify cart access — check again later or try reconnecting",
            "connecting": f"Sign-in in progress{view_note}",
            "saving": "Saving connection",
            "verifying": "Verifying cart access",
            "reconnect_incomplete": "Sign-in did not complete — try reconnecting",
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
        elif state == "saving":
            body += "<p>You can close this page while the connection is saved.</p>"
        elif state == "connecting":
            body += f'<p><a class="button" href="{html.escape(view["url"], quote=True)}">Continue sign-in</a></p>'
            if fresh:
                body += self.form(
                    "start",
                    "Start new sign-in",
                    control=control,
                    extra={"replace": view["url"].rsplit("/", 1)[-1]},
                )
            else:
                body += (
                    f'<p><a class="button" href="{html.escape(self.auth.step_up_url(), quote=True)}">'
                    "Verify and start new sign-in</a></p>"
                )
        else:
            label = "Reconnect" if view["role"] == "owner" else "Connect HKTVmall"
            if fresh:
                body += self.form("start", label, control=control)
            else:
                # Confirm first by navigation; a submitted form cannot be redirected to the identity provider.
                body += (
                    f'<p><a class="button" href="{html.escape(self.auth.step_up_url(), quote=True)}">'
                    f"Verify and {html.escape(label[0].lower() + label[1:])}</a></p>"
                )
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
            '<section class="card panel"><h2>Shared with</h2><p>Share shopping access to your own account. '
            "People you share with can read and change this basket through their assistant. "
            "Order history is optional. They cannot see your password, reconnect or disconnect it. "
            "They choose when to use it; sharing never switches their account.</p>"
        )
        candidates = self.candidates(person)
        if not candidates:
            return body + (
                '<p class="muted">Nobody else has used HKTVmall here yet. People appear once they have used '
                "HKTVmall through their assistant or opened this page. To add someone, the administrator adds "
                "them to the HKTVmall group; then send them this page.</p></section>"
            )
        items = []
        for entry in candidates:
            member = entry["person"]
            name = html.escape(entry["name"])
            if entry["name"] != member and sum(other["name"] == entry["name"] for other in candidates) > 1:
                name += f' <span class="muted">({html.escape(member)})</span>'
            grant = control["shares"].get(member)
            check_id, form_id = "history-" + member, "share-" + member
            checkbox = (
                f'<label for="{check_id}"><input id="{check_id}" form="{html.escape(form_id, quote=True)}" '
                'type="checkbox" name="history" value="1"'
                + (" checked" if grant and grant["order_history"] else "")
                + "> Also allow reading order history</label>"
            )
            # The shell styles forms inline-block and labels block. Name, checkbox and buttons therefore each
            # get their own line; the checkbox joins the share form through form= so both buttons share a row.
            status = "Shared" + (" with order history" if grant["order_history"] else "") if grant else "Not shared"
            item = (
                '<li class="share"><p><strong>' + ("✓ " if grant else "") + name + "</strong>"
                f' <span class="muted">· {status}</span></p>' + checkbox
            )
            item += self.form(
                "share",
                "Update access" if grant else "Share",
                control=control,
                person=member,
                extra={"grant": grant["revision"] if grant else "new"},
                form_id=form_id,
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
        return body + "<ul>" + "".join(items) + "</ul></section>"

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
            return self.auth.refused(request)
        if confirm := self.auth.require_fresh(request, max_age=FRESH_SECONDS, action="connect HKTVmall"):
            return confirm
        control = self.accounts.control(person)
        try:
            expected, state, replace_id = await self.intent(request, optional=("replace",))
            async with control.lifecycle():
                if self.account(request) != person or not self.fresh(request):
                    return self.auth.refused(request)
                still_open = []
                pending_completed = False
                for held in self._pending_close.get(person, ()):
                    open_now, done_now = await run_in_threadpool(self._cancel_and_check, held)
                    if open_now:
                        still_open.append(held)
                    if done_now:
                        pending_completed = True
                if still_open:
                    self._pending_close[person] = still_open
                    return self.page(
                        '<p role="alert">The previous sign-in window is still closing. '
                        "Wait a moment and try again.</p>"
                        f'<p><a href="{html.escape(self.path, quote=True)}">Back to HKTVmall accounts</a></p>',
                        status_code=409,
                    )
                self._pending_close.pop(person, None)
                if pending_completed:
                    return self.page(
                        '<p role="alert">Your previous sign-in just completed. '
                        "Return to HKTVmall accounts to see its status.</p>"
                        f'<p><a href="{html.escape(self.path, quote=True)}">Back to HKTVmall accounts</a></p>',
                        status_code=409,
                    )
                identity = self.auth.identity(request)
                current = control.read()
                control.expected(current, expected, state)
                if current["state"] == "disconnecting":
                    raise HktvError("connection_disconnected")
                if not control.owner_matches(identity):
                    raise HktvError("not_connected")
                if not await run_in_threadpool(self.relay.reserve_slot, person):
                    raise LinkLimited(person)
                slot_reserved = True
                link = None
                committed = False
                draining_entered = False
                try:
                    if replace_id is not None:
                        with self.relay._lock:
                            pre_target = self.relay._links.get(replace_id)
                        if (
                            pre_target is None
                            or pre_target.key != person
                            or pre_target.closed
                            or pre_target.state not in ("ready", "starting", "waiting")
                        ):
                            return self.page(
                                '<p role="alert">This sign-in attempt is no longer active. '
                                "Return to HKTVmall accounts to see its status.</p>"
                                f'<p><a href="{html.escape(self.path, quote=True)}">Back to HKTVmall accounts</a></p>',
                                status_code=409,
                            )
                    self._draining_accounts.add(person)
                    draining_entered = True
                    previous, already_completed, active_relay_ids = await run_in_threadpool(
                        self._snapshot_links, person
                    )
                    if replace_id is not None:
                        target_link = next((old for old in previous if old.id == replace_id), None)
                        if (
                            target_link is None
                            or target_link.id not in active_relay_ids
                            or target_link.closed
                            or target_link.id in already_completed
                            or target_link.state not in ("ready", "starting", "waiting")
                        ):
                            return self.page(
                                '<p role="alert">This sign-in attempt is no longer active. '
                                "Return to HKTVmall accounts to see its status.</p>"
                                f'<p><a href="{html.escape(self.path, quote=True)}">Back to HKTVmall accounts</a></p>',
                                status_code=409,
                            )
                        already_completed.discard(replace_id)
                    if any(old.state == "saving" for old in previous):
                        return self.page(
                            '<p role="alert">A previous sign-in is still finishing. '
                            "Wait a moment and try again.</p>"
                            f'<p><a href="{html.escape(self.path, quote=True)}">Back to HKTVmall accounts</a></p>',
                            status_code=409,
                        )
                    for old in previous:
                        old.closed = True
                    # Cancel every replaced link unconditionally (including retryable cancelled links) BEFORE
                    # allocating a new relay link so a busy/completing/stuck teardown consumes 0 hourly attempts.
                    completed = False
                    for old in previous:
                        open_now, done_now = await run_in_threadpool(self._cancel_and_check, old)
                        if open_now:
                            still_open.append(old)
                        if done_now and old.id not in already_completed:
                            completed = True
                    if still_open:
                        self._pending_close[person] = still_open
                        return self.page(
                            '<p role="alert">The previous sign-in window is still closing. '
                            "Wait a moment and try again.</p>"
                            f'<p><a href="{html.escape(self.path, quote=True)}">Back to HKTVmall accounts</a></p>',
                            status_code=409,
                        )
                    if completed:
                        return self.page(
                            '<p role="alert">Your previous sign-in just completed. '
                            "Return to HKTVmall accounts to see its status.</p>"
                            f'<p><a href="{html.escape(self.path, quote=True)}">Back to HKTVmall accounts</a></p>',
                            status_code=409,
                        )
                    slot_reserved = False
                    link = self.relay.create(
                        person,
                        params={"key": person, "epoch": ""},
                        context=AttemptContext(owner=person, target=""),
                        reserved=True,
                    )
                    self._known_links.setdefault(person, {})[link.id] = link
                    epoch = control.resume(expected, state, identity, reconnect=True)
                    link.params["epoch"] = epoch
                    link.context = AttemptContext(owner=person, target=epoch)
                    self.accounts.forget(person)
                    committed = True
                finally:
                    if slot_reserved:
                        self.relay.release_slot()
                    if link is not None and not committed:
                        link.closed = True
                        if person in self._known_links:
                            self._known_links[person].pop(link.id, None)
                    if draining_entered:
                        self._draining_accounts.discard(person)
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
            return self.auth.refused(request)
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
                still_open = []
                account_links = await run_in_threadpool(self._account_links, person)
                for link in (*self._pending_close.get(person, ()), *account_links):
                    open_now, _ = await run_in_threadpool(self._cancel_and_check, link)
                    if open_now:
                        still_open.append(link)
                if still_open:
                    self._pending_close[person] = still_open
                else:
                    self._pending_close.pop(person, None)
                await run_in_threadpool(self.accounts.disconnect_owned, person, epoch)
                open_ids = {item.id for item in still_open}
                if person in self._known_links:
                    for link_id in list(self._known_links[person]):
                        if link_id not in open_ids:
                            self._known_links[person].pop(link_id, None)
                    if not self._known_links[person]:
                        self._known_links.pop(person, None)
                with self.relay._lock:
                    for active_id, active_link in list(self.relay._links.items()):
                        if active_link.key == person and active_id not in open_ids:
                            active_link.closed = True
                            self.relay._links.pop(active_id, None)
                    for retired_id, retired_link in list(self.relay._retired.items()):
                        if retired_link.key == person and retired_id not in open_ids:
                            retired_link.closed = True
                            self.relay._retired.pop(retired_id, None)
        except HktvError:
            return self.changed()
        except Exception as e:
            log.warning("person %s: disconnect incomplete (%s); the barrier stays", _tag(person), type(e).__name__)
        return RedirectResponse(self.path, status_code=303)

    async def share(self, request):
        person = self.account(request)
        if person is None or not self.auth.origin_ok(request):
            return self.auth.refused(request)
        if confirm := self.auth.require_fresh(request, max_age=FRESH_SECONDS, action="share your HKTVmall account"):
            return confirm
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
            return self.auth.refused(request)
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
            return self.auth.refused(request)
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
            return self.auth.refused(request)
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
            if scope["type"] == "http" and scope["path"].count("/") == 2 and scope["path"].startswith("/connect/"):
                # A page with a link, never a redirect: this address is also reached from the Connect form.
                confirm = self.connections.auth.require_fresh(
                    Request(scope),
                    max_age=FRESH_SECONDS,
                    next=self.connections.prefix + scope["path"],
                    action="open this HKTVmall sign-in",
                )
                if confirm is not None:
                    return await confirm(scope, receive, send)
            return await self.connections.app(scope, receive, send)
        return await self.app(scope, receive, send)

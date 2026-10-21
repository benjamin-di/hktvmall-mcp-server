"""Sign-in for connected people: this server is its own OAuth 2.1 authorization server on HKTVMALL_PUBLIC_URL.

Claude or ChatGPT registers itself (dynamic client registration; only their own and loopback redirect hosts
are accepted), sends the person to /authorize, and this provider hands them to the connect page (connect.py):
invite code, then HKTVmall's own login. When that connects an account, ``complete`` issues a one-time code
for it; the token exchange turns it into an access token (24 h) and a refresh token (180 days) that act for
that account only (``client_id`` ``account:<id>``, which accounts.Accounts.resolve maps). The static bearers
(MCP_BEARER_TOKEN) keep meaning the default account and are checked first.

Tokens are random and stored only as hashes (``HKTVMALL_STATE_DIR/oauth.json``, with the registered clients);
a disconnected account's tokens stop working at once, because every lookup checks the account still exists.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastmcp.server.auth.auth import ClientRegistrationOptions, OAuthProvider, RevocationOptions
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .accounts import ACCOUNT_CLIENT, ACCOUNT_ID, Accounts
from .fsio import atomic_write_json

log = logging.getLogger(__name__)

SCOPE = "hktvmall"
ACCESS_TTL = 24 * 3600
REFRESH_TTL = 180 * 24 * 3600
CODE_TTL = 5 * 60
PENDING_TTL = 20 * 60
MAX_CLIENTS = 500  # registration is open; the oldest unused clients go first
MAX_PENDING = 50
# Only these may receive codes: the assistants' own callbacks and native apps on loopback (RFC 8252).
REDIRECT_HOSTS = frozenset({"claude.ai", "claude.com", "chatgpt.com"})
LOOPBACK = frozenset({"localhost", "127.0.0.1"})


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def redirect_allowed(uri: str) -> bool:
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    if parts.scheme == "https":
        return parts.hostname in REDIRECT_HOSTS
    return parts.scheme == "http" and parts.hostname in LOOPBACK


class HktvOAuth(OAuthProvider):
    def __init__(self, *, public_url: str, state_dir: Path, accounts: Accounts, static: Any = None, clock=time.time):
        super().__init__(
            base_url=public_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
        )
        self.public_url, self.accounts, self.static, self._clock = public_url.rstrip("/"), accounts, static, clock
        self.path = Path(state_dir) / "oauth.json"
        self._lock = threading.Lock()
        self._codes: dict[str, AuthorizationCode] = {}
        self._pending: dict[str, dict[str, Any]] = {}
        self._data = self._load()

    # -- storage ----------------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError):
            log.error("oauth.json unreadable; connected clients must sign in again")
            data = {}
        for key in ("clients", "access", "refresh"):
            if not isinstance(data.get(key), dict):
                data[key] = {}
        return data

    def _save(self) -> None:
        now = self._clock()
        for key in ("access", "refresh"):
            self._data[key] = {h: t for h, t in self._data[key].items() if t.get("expires_at", 0) > now}
        clients = self._data["clients"]
        if len(clients) > MAX_CLIENTS:
            for client_id in sorted(clients, key=lambda c: clients[c].get("used_at", 0))[: len(clients) - MAX_CLIENTS]:
                del clients[client_id]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, self._data)

    def _connected(self, account_id: str) -> bool:
        return bool(ACCOUNT_ID.fullmatch(account_id)) and account_id in self.accounts.ids()

    # -- clients ----------------------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self._lock:
            entry = self._data["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(entry["info"]) if entry else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in client_info.redirect_uris or []]
        if not uris or not all(redirect_allowed(u) for u in uris):
            raise RegistrationError("invalid_redirect_uri", "only Claude, ChatGPT or a loopback app may connect")
        if client_info.client_id is None:
            raise RegistrationError("invalid_client_metadata", "client_id missing")
        with self._lock:
            self._data["clients"][client_info.client_id] = {
                "info": client_info.model_dump(mode="json", exclude_none=True),
                "used_at": self._clock(),
            }
            self._save()
        log.info("oauth: registered client %s (%s)", client_info.client_id, client_info.client_name or "-")

    # -- authorization: the connect page does the work --------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if not redirect_allowed(str(params.redirect_uri)):
            raise AuthorizeError("invalid_request", "redirect_uri not allowed")
        now = self._clock()
        with self._lock:
            for old in [k for k, p in self._pending.items() if now - p["created"] > PENDING_TTL]:
                del self._pending[old]
            if len(self._pending) >= MAX_PENDING:
                del self._pending[min(self._pending, key=lambda k: self._pending[k]["created"])]
            request_id = secrets.token_urlsafe(24)
            self._pending[request_id] = {"client_id": client.client_id, "params": params, "created": now}
        return f"{self.public_url}/connect?req={request_id}"

    def pending(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._pending.get(request_id)
        if entry and self._clock() - entry["created"] <= PENDING_TTL:
            return entry
        return None

    def complete(self, request_id: str, account_id: str) -> str | None:
        """The connect page connected ``account_id``: a one-time code for the waiting client, as the URL to send
        the person back to (None if the request expired)."""
        with self._lock:
            entry = self._pending.pop(request_id, None)
        if entry is None or self._clock() - entry["created"] > PENDING_TTL:
            return None
        params: AuthorizationParams = entry["params"]
        code = secrets.token_urlsafe(32)
        self._codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [SCOPE],
            expires_at=self._clock() + CODE_TTL,
            client_id=entry["client_id"],
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=account_id,
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        if code.expires_at < self._clock():
            self._codes.pop(authorization_code, None)
            return None
        return code

    # -- tokens -----------------------------------------------------------------------------

    def _issue(self, client_id: str, account_id: str, scopes: list[str], resource: str | None) -> str:
        token = secrets.token_urlsafe(32)
        self._data["access"][_hash(token)] = {
            "account": account_id,
            "client_id": client_id,
            "scopes": scopes,
            "resource": resource,
            "expires_at": int(self._clock() + ACCESS_TTL),
        }
        return token

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if self._codes.pop(authorization_code.code, None) is None:
            raise TokenError("invalid_grant", "authorization code not found or already used")
        account_id = authorization_code.subject or ""
        if not self._connected(account_id):
            raise TokenError("invalid_grant", "the account is no longer connected")
        refresh = secrets.token_urlsafe(32)
        with self._lock:
            access = self._issue(
                client.client_id or "", account_id, authorization_code.scopes, authorization_code.resource
            )
            self._data["refresh"][_hash(refresh)] = {
                "account": account_id,
                "client_id": client.client_id,
                "scopes": authorization_code.scopes,
                "resource": authorization_code.resource,
                "expires_at": int(self._clock() + REFRESH_TTL),
            }
            self._data["clients"].get(client.client_id or "", {})["used_at"] = self._clock()
            self._save()
        log.info("oauth: account %s signed in a client", account_id)
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            refresh_token=refresh,
            scope=" ".join(authorization_code.scopes),
        )

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        with self._lock:
            entry = self._data["refresh"].get(_hash(refresh_token))
        if (
            entry is None
            or entry["client_id"] != client.client_id
            or entry["expires_at"] < self._clock()
            or not self._connected(entry["account"])
        ):
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=entry["client_id"],
            scopes=entry["scopes"],
            expires_at=entry["expires_at"],
            resource=entry.get("resource"),
            subject=entry["account"],
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        """A new access token; the refresh token stays (a rotated token lost
        in a client's retry would disconnect the person)."""
        account_id = refresh_token.subject or ""
        if not self._connected(account_id):
            raise TokenError("invalid_grant", "the account is no longer connected")
        granted = [s for s in scopes if s in refresh_token.scopes] or refresh_token.scopes
        with self._lock:
            access = self._issue(refresh_token.client_id, account_id, granted, refresh_token.resource)
            self._data["clients"].get(refresh_token.client_id, {})["used_at"] = self._clock()
            self._save()
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            refresh_token=refresh_token.token,
            scope=" ".join(granted),
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        if self.static is not None:
            default = await self.static.verify_token(token)
            if default is not None:
                return default  # a static bearer: the default account
        with self._lock:
            entry = self._data["access"].get(_hash(token))
        if entry is None or entry["expires_at"] < self._clock() or not self._connected(entry["account"]):
            return None
        return AccessToken(
            token=token,
            client_id=ACCOUNT_CLIENT + entry["account"],
            scopes=entry["scopes"],
            expires_at=entry["expires_at"],
            resource=entry.get("resource"),
            subject=entry["account"],
            claims={"oauth_client": entry["client_id"]},
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with self._lock:
            hashed = _hash(token.token)
            removed = self._data["access"].pop(hashed, None) or self._data["refresh"].pop(hashed, None)
            if removed is not None:
                self._save()

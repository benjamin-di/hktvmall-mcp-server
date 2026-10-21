"""The one way to the login service (``hktvmall_login``) beside this server: its internal HTTP API and the
remote-desktop stream, always with its bearer. It never reaches HKTVmall (only ``site`` does); every URL it
builds is ``HKTVMALL_LOGIN_URL`` plus a fixed path and validated ids (``tests/test_safety.py`` pins this)."""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

LOGIN_ID = re.compile(r"[A-Za-z0-9_-]{16,40}\Z")
STATIC = re.compile(r"(?:[A-Za-z0-9_-][A-Za-z0-9_.-]{0,80}/){0,4}[A-Za-z0-9_-][A-Za-z0-9_.-]{0,80}\Z")
TIMEOUT = 15
MAX_STATIC = 4 * 1024 * 1024


class LoginUnavailable(Exception):
    """The login service did not answer as expected."""


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class LoginClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base, self._auth = base_url.rstrip("/"), f"Bearer {token}"
        self._opener = urllib.request.build_opener(NoRedirects())

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, bytes, str]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Authorization": self._auth, "Content-Type": "application/json"},
        )
        try:
            with self._opener.open(request, timeout=TIMEOUT) as response:
                return response.status, response.read(MAX_STATIC + 1), response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as err:
            return err.code, err.read(MAX_STATIC + 1), err.headers.get("Content-Type", "")
        except (urllib.error.URLError, TimeoutError, OSError):
            raise LoginUnavailable("unreachable") from None

    def _json(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        status, raw, _ = self._call(method, path, body)
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            raise LoginUnavailable("not json") from None

    @staticmethod
    def _id(login_id: str) -> str:
        if not LOGIN_ID.fullmatch(login_id):
            raise LoginUnavailable("bad id")
        return login_id

    def start(self, layout: str) -> dict[str, Any] | None:
        """A new login window, or None when every slot is busy."""
        status, data = self._json("POST", "/logins", {"layout": layout})
        if status == 503:
            return None
        if status != 201 or not isinstance(data, dict) or not LOGIN_ID.fullmatch(str(data.get("id", ""))):
            raise LoginUnavailable(f"start {status}")
        return data

    def state(self, login_id: str) -> dict[str, Any] | None:
        status, data = self._json("GET", f"/logins/{self._id(login_id)}")
        if status == 404:
            return None
        if status != 200 or not isinstance(data, dict):
            raise LoginUnavailable(f"state {status}")
        return data

    def result(self, login_id: str) -> dict[str, Any] | None:
        """The proven session, once."""
        status, data = self._json("POST", f"/logins/{self._id(login_id)}/result")
        if status in (404, 409):
            return None
        if status != 200 or not isinstance(data, dict):
            raise LoginUnavailable(f"result {status}")
        return data

    def close(self, login_id: str) -> None:
        self._call("DELETE", f"/logins/{self._id(login_id)}")

    def static(self, path: str) -> tuple[int, bytes, str]:
        """One of noVNC's files, for the viewer."""
        if not STATIC.fullmatch(path) or ".." in path:
            return 404, b"", "text/plain"
        status, raw, content_type = self._call("GET", f"/novnc/{path}")
        if len(raw) > MAX_STATIC:
            return 502, b"", "text/plain"
        return status, raw, content_type

    async def bridge(self, login_id: str, ws: WebSocket) -> None:
        """Relay the person's viewer to the login's remote-desktop stream, bytes unchanged."""
        from websockets.asyncio.client import connect
        from websockets.exceptions import WebSocketException

        url = "ws" + self.base[len("http") :] + f"/logins/{self._id(login_id)}/vnc"
        try:
            async with connect(
                url, additional_headers={"Authorization": self._auth}, subprotocols=["binary"], max_size=None
            ) as upstream:

                async def to_login() -> None:
                    while True:
                        message = await ws.receive()
                        if message["type"] == "websocket.disconnect":
                            return
                        await upstream.send(message.get("bytes") or (message.get("text") or "").encode())

                async def to_person() -> None:
                    async for data in upstream:
                        await ws.send_bytes(data if isinstance(data, bytes) else data.encode())

                tasks = [asyncio.create_task(to_login()), asyncio.create_task(to_person())]
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in tasks:
                    task.cancel()
        except (OSError, WebSocketException, WebSocketDisconnect, RuntimeError):
            pass

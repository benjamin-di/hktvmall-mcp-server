"""The login service's internal API; only hktvmall-mcp holds its bearer.

POST   /logins               {"layout": "phone"|"desktop"} -> 201 {id, state, ...} | 503 busy
GET    /logins/{id}          -> {id, state, host, error, layout}
POST   /logins/{id}/result   -> the proven session once {email, user_agent, cookies} | 409 not yet
DELETE /logins/{id}          -> 204
WS     /logins/{id}/vnc      -> the login's remote-desktop stream (RFB), for noVNC
GET    /novnc/...            -> noVNC's own files (the viewer)
GET    /health               -> open
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket, WebSocketDisconnect

from .logins import Busy, Logins

log = logging.getLogger("hktvmall-login")


class BearerOnly:
    """Every route but /health needs the bearer, WebSockets included (checked before the handshake)."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app, self.expected = app, f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and scope.get("path") != "/health":
            given = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(given, self.expected):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 4401})
                else:
                    await Response(status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_app(logins: Logins, *, token: str, novnc_dir: Path) -> Starlette:
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "healthy"})

    async def start(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            body = {}
        layout = body.get("layout") if isinstance(body, dict) else None
        try:
            login = await logins.start(layout if layout in ("phone", "desktop") else "desktop")
        except Busy:
            return JSONResponse({"error": "busy"}, status_code=503)
        return JSONResponse(login.public(), status_code=201)

    async def state(request: Request) -> JSONResponse:
        login = logins.get(request.path_params["id"])
        if login is None:
            return JSONResponse({"error": "unknown"}, status_code=404)
        return JSONResponse(login.public())

    async def result(request: Request) -> JSONResponse:
        login_id = request.path_params["id"]
        if logins.get(login_id) is None:
            return JSONResponse({"error": "unknown"}, status_code=404)
        session = logins.take(login_id)
        if session is None:
            return JSONResponse({"error": "not_signed_in"}, status_code=409)
        return JSONResponse(session)

    async def close(request: Request) -> Response:
        await logins.close(request.path_params["id"])
        return Response(status_code=204)

    async def vnc(ws: WebSocket) -> None:
        login = logins.get(ws.path_params["id"])
        if login is None or login.state not in ("starting", "waiting"):
            await ws.close(code=4404)
            return
        await ws.accept(subprotocol="binary" if "binary" in ws.scope.get("subprotocols", []) else None)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", login.vnc_port)
        except OSError:
            await ws.close(code=1011)
            return

        async def upstream() -> None:
            try:
                while True:
                    message = await ws.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    writer.write(message.get("bytes") or (message.get("text") or "").encode())
                    await writer.drain()
            except (WebSocketDisconnect, ConnectionError):
                return

        async def downstream() -> None:
            try:
                while data := await reader.read(65536):
                    await ws.send_bytes(data)
            except (WebSocketDisconnect, ConnectionError, RuntimeError):
                return

        tasks = [asyncio.create_task(upstream()), asyncio.create_task(downstream())]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in tasks:
            task.cancel()
        writer.close()

    routes = [
        Route("/health", health),
        Route("/logins", start, methods=["POST"]),
        Route("/logins/{id}", state, methods=["GET"]),
        Route("/logins/{id}", close, methods=["DELETE"]),
        Route("/logins/{id}/result", result, methods=["POST"]),
        WebSocketRoute("/logins/{id}/vnc", vnc),
    ]
    if novnc_dir.is_dir():
        routes.append(Mount("/novnc", app=StaticFiles(directory=novnc_dir)))
    return Starlette(routes=routes, middleware=[Middleware(BearerOnly, token=token)])


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    token = (os.environ.get("HKTVMALL_LOGIN_TOKEN") or "").strip()
    if len(token) < 32:
        raise SystemExit("HKTVMALL_LOGIN_TOKEN (at least 32 characters) is required")
    logins = Logins(
        slots=int(os.environ.get("HKTVMALL_LOGIN_SLOTS") or 2),
        minutes=int(os.environ.get("HKTVMALL_LOGIN_MINUTES") or 15),
        xvfb=os.environ.get("HKTVMALL_LOGIN_XVFB") or "Xvfb",
        x11vnc=os.environ.get("HKTVMALL_LOGIN_X11VNC") or "x11vnc",
    )
    novnc = Path(os.environ.get("HKTVMALL_LOGIN_NOVNC") or "/usr/share/novnc")
    app = build_app(logins, token=token, novnc_dir=novnc)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT") or 8080), log_level="warning")

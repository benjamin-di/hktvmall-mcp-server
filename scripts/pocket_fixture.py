"""Disposable native Pocket ID fixture; synthetic users and loopback listeners only."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx2
import uvicorn
from patchright.async_api import Error as BrowserError

SHA256 = "fbe58b3af6e225600136c7da033810d2d86436fbd13a135fa52205f467544436"


def port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def serve(app, listen):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=listen, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server, thread


async def wait_for(check, seconds=30):
    until = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < until, "acceptance deadline exceeded"
        await asyncio.sleep(0.05)


def provider_listeners(pid, http_port, actors_port):
    """Qualify actual owned TCP/UDP listeners, including the QUIC actor socket."""
    owned = set()
    for fd in (Path("/proc") / str(pid) / "fd").iterdir():
        try:
            link = fd.readlink().as_posix()
        except FileNotFoundError:
            continue
        match = re.fullmatch(r"socket:\[(\d+)\]", link)
        if match:
            owned.add(match[1])
    found = set()
    for table in ("tcp", "tcp6", "udp", "udp6"):
        for line in (Path("/proc") / str(pid) / "net" / table).read_text().splitlines()[1:]:
            fields = line.split()
            if fields[9] not in owned:
                continue
            if table.startswith("tcp") and fields[3] != "0A":
                continue
            if table.startswith("udp") and int(fields[2].replace(":", ""), 16):
                continue
            host, raw_port = fields[1].split(":")
            raw = bytes.fromhex(host)
            address = ipaddress.ip_address(b"".join(raw[i : i + 4][::-1] for i in range(0, len(raw), 4)))
            assert address.is_loopback, "Disposable identity listener must remain on loopback"
            found.add((table, int(raw_port, 16)))
    return ("tcp", http_port) in found and ("udp", actors_port) in found


async def identity_redirect(page, origin):
    for _ in range(20):
        await page.wait_for_timeout(250)
        if page.url == origin + "/connections/":
            return
        button = page.get_by_role("button", name=re.compile(r"^(Authenticate|Sign in|Allow|Authorize)$"))
        try:
            if await button.count() and await button.first.is_visible():
                await button.first.click()
        except BrowserError as failure:
            if "Execution context was destroyed" not in str(failure):
                raise
    await page.wait_for_url(origin + "/connections/", timeout=20000)


class PocketFixture:
    def __init__(self, root, origin):
        self.root, self.origin = root, origin
        self.port, self.actors = port(), port(socket.SOCK_DGRAM)
        while self.actors == self.port:
            self.actors = port(socket.SOCK_DGRAM)
        self.issuer = f"http://localhost:{self.port}"
        self.process = None

    async def __aenter__(self):
        try:
            return await self._start()
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def _start(self):
        binary = Path(os.environ["POCKET_ID_BIN"])
        assert hashlib.sha256(binary.read_bytes()).hexdigest() == SHA256
        self.root.mkdir(mode=0o700)
        env = {k: os.environ[k] for k in ("PATH", "LANG", "TZ") if k in os.environ}
        key = secrets.token_urlsafe(32)
        env.update(
            APP_URL=self.issuer,
            HOST="127.0.0.1",
            PORT=str(self.port),
            ACTORS_HOST="127.0.0.1",
            ACTORS_PORT=str(self.actors),
            DB_CONNECTION_STRING=str(self.root / "provider.db"),
            ENCRYPTION_KEY=secrets.token_urlsafe(32),
            STATIC_API_KEY=key,
            ANALYTICS_DISABLED="true",
            VERSION_CHECK_DISABLED="true",
            ALLOW_INSECURE_CALLBACK_URLS="true",
            DISABLE_RATE_LIMITING="true",
        )
        self.log = (self.root / "provider.log").open("wb")
        self.process = subprocess.Popen([str(binary)], cwd=self.root, env=env, stdout=self.log, stderr=self.log)
        self.admin = httpx2.Client(base_url=self.issuer, headers={"X-API-Key": key}, timeout=10)
        for _ in range(200):
            try:
                if self.admin.get("/api/application-configuration").status_code == 200:
                    break
            except httpx2.HTTPError:
                pass
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("Disposable identity did not start")
        await wait_for(lambda: provider_listeners(self.process.pid, self.port, self.actors))
        group = self.post("/api/user-groups", {"name": "hktvmall-users", "friendlyName": "Example users"})
        self.users = []
        for number, alias in enumerate(("first", "second"), start=1):
            user = self.post(
                "/api/users",
                {
                    "username": f"example{number}",
                    "firstName": "Example",
                    "lastName": str(number),
                    "email": f"example{number}@example.com",
                    "isAdmin": False,
                },
            )
            assert self.admin.put(
                f"/api/users/{user['id']}/user-groups", json={"userGroupIds": [group["id"]]}
            ).status_code in (200, 204)
            assert (
                self.admin.put(
                    f"/api/custom-claims/user/{user['id']}", json=[{"key": "person_id", "value": alias}]
                ).status_code
                == 200
            )
            self.users.append(user)
        self.post(
            "/api/oidc/clients",
            {
                "id": "hktvmall-ui",
                "name": "Example connections",
                "callbackURLs": [self.origin + "/connections/callback"],
                "isPublic": True,
                "pkceEnabled": True,
                "launchURL": self.origin + "/connections/",
            },
        )
        return self

    def post(self, url, body):
        response = self.admin.post(url, json=body)
        assert response.status_code in (200, 201), "Disposable identity setup failed"
        return response.json()

    async def login(self, browser, user):
        context = await browser.new_context(viewport={"width": 1280, "height": 860})
        page = await context.new_page()
        cdp = await context.new_cdp_session(page)
        await cdp.send("WebAuthn.enable")
        auth = await cdp.send(
            "WebAuthn.addVirtualAuthenticator",
            {
                "options": {
                    "protocol": "ctap2",
                    "transport": "internal",
                    "hasResidentKey": True,
                    "hasUserVerification": True,
                    "isUserVerified": True,
                    "automaticPresenceSimulation": True,
                }
            },
        )
        token = self.post(f"/api/users/{user['id']}/one-time-access-token", {})["token"]
        assert (await context.request.post(self.issuer + "/api/one-time-access-token/" + token)).ok
        await page.goto(self.issuer + "/settings/account")
        await page.get_by_role("button", name=re.compile("add passkey", re.I)).first.click()
        await page.wait_for_timeout(700)
        enrolled = await cdp.send("WebAuthn.getCredentials", {"authenticatorId": auth["authenticatorId"]})
        assert len(enrolled["credentials"]) == 1
        await context.clear_cookies()
        await page.goto(self.origin + "/connections/login?reauth=1")
        await identity_redirect(page, self.origin)
        used = await cdp.send("WebAuthn.getCredentials", {"authenticatorId": auth["authenticatorId"]})
        assert used["credentials"][0]["signCount"] > enrolled["credentials"][0]["signCount"]
        return context, page

    async def __aexit__(self, *_):
        if hasattr(self, "admin"):
            self.admin.close()
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if hasattr(self, "log"):
            self.log.close()

"""Private human recovery for the existing default-account custodian."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from signin_window.automation.profiles import prepare_profile
from signin_window.browser import policy_installed
from signin_window.service import Windows, build_app
from signin_window.state import read_private_file

from hktvmall_login.window import sign_in
from hktvmall_mcp.connections_state import ConnectionState
from hktvmall_mcp.errors import HktvError

from .cli import lease, stores
from .cookies import select_export_cookies
from .human_state import commit, profile_path, recover
from .snapshot import prepare_export_dir, publish, published_generation


class VerifiedSession:
    def __init__(self, result):
        self.result = result

    def user_agent(self):
        return self.result["user_agent"]

    def export_cookies(self):
        return select_export_cookies(
            self.result["cookies"],
            hosts=frozenset({"hktvmall.com", "www.hktvmall.com"}),
            names=frozenset(c["name"] for c in self.result["cookies"]),
        )


def start_params(body):
    if (
        set(body) != {"layout", "key", "epoch"}
        or body["key"] != "default"
        or not isinstance(body["epoch"], str)
        or not re.fullmatch(r"initial|[A-Za-z0-9_-]{32}", str(body["epoch"]))
    ):
        raise ValueError("fixed default recovery parameters required")
    return {"key": "default", "epoch": body["epoch"]}


class HumanRecovery:
    def __init__(self, root, export, expected_email, control, *, login=sign_in, policy=policy_installed):
        self.root, self.export, self.expected_email, self.control = root, export, expected_email, control
        self.login, self.policy = login, policy

    def recover(self):
        with lease(self.root):
            recover(self.root, self.export, self.control)

    async def run(self, window):
        # No caller-selected account, filesystem path, provider or credential reference.
        if (
            set(window.params) != {"key", "epoch"}
            or window.params["key"] != "default"
            or not re.fullmatch(r"initial|[A-Za-z0-9_-]{32}", str(window.params["epoch"]))
        ):
            window.finish("failed")
            return
        epoch = window.params["epoch"]
        candidate = None
        try:
            if not self.policy():
                raise ValueError("browser policy missing")
            with lease(self.root):
                recover(self.root, self.export, self.control)
                self.control.require_active(epoch)
                if self.control.read()["owner"] is None:
                    raise HktvError("not_connected")
                accepted = profile_path(self.root)
                accepted.mkdir(mode=0o700, parents=True, exist_ok=True)
                prepare_profile(accepted)  # same custodian/PID namespace; foreign owners refuse
                candidate = accepted.parent / ("human-" + uuid.uuid4().hex)
                shutil.copytree(
                    accepted,
                    candidate,
                    symlinks=True,
                    ignore=shutil.ignore_patterns(
                        "Singleton*", "BrowserMetrics", "DeferredBrowserMetrics", ".signin-singleton-recovery"
                    ),
                )
                os.chmod(candidate, 0o700)
                result = await self.login(
                    profile=str(candidate),
                    layout=window.layout,
                    env=window.env(),
                    extra_args=("--window-position=0,0",),
                    on_page=window.waiting,
                    locked=True,
                )
                # sign_in closes Chromium before returning independently verified cookies.
                prepare_profile(candidate)
                if result["email"].strip().casefold() != self.expected_email.casefold():
                    window.finish("wrong_account")
                    return
                if window.cancelled.is_set():
                    raise asyncio.CancelledError
                self.control.require_active(epoch)
                state, _ = stores(self.root)
                previous = state.load("hktvmall")
                generation = (
                    max(published_generation(self.export), previous.last_generation, self.control.read()["generation"])
                    + 1
                )
                prepare_export_dir(self.export)
                with tempfile.TemporaryDirectory(prefix="human-snapshot-", dir=self.root) as folder:
                    staging = Path(folder)
                    old = self.export / "snapshot.json"
                    if old.exists():
                        from signin_window.state import atomic_private_write

                        atomic_private_write(staging / "snapshot.json", read_private_file(old, max_bytes=512 * 1024))
                    publish(staging, VerifiedSession(result), generation, self.expected_email, datetime.now(UTC))
                    snapshot = read_private_file(staging / "snapshot.json", max_bytes=512 * 1024)
                    window.saving()
                    commit(
                        self.root,
                        self.export,
                        self.control,
                        epoch,
                        candidate,
                        snapshot,
                        state,
                        generation,
                        previous.hold.hold_id if previous.hold else None,
                    )
                window.finish("connected")
        except asyncio.CancelledError:
            raise
        except Exception:
            window.finish("failed")
        finally:
            # Hard process death is handled on startup and before native keeper warm-up.
            with lease(self.root):
                recover(self.root, self.export, self.control)
                if candidate is not None and candidate.exists():
                    prepare_profile(candidate)
                    shutil.rmtree(candidate)


def configured(env=None):
    env = os.environ if env is None else env
    paths = [
        Path(env[key])
        for key in ("HKTVMALL_MAINTENANCE_ROOT", "HKTVMALL_MAINTENANCE_EXPORT_DIR", "HKTVMALL_CONNECTIONS_CONTROLS_DIR")
    ]
    if any(
        not p.is_absolute()
        or ".." in p.parts
        or p.is_symlink()
        or not p.is_dir()
        or p.stat().st_uid != os.getuid()
        or p.stat().st_mode & 0o077
        for p in paths
    ):
        raise ValueError("existing private custodian mounts required")
    email = env["HKTVMALL_MAINTENANCE_EXPECTED_EMAIL"].strip()
    if "@" not in email or any(c.isspace() for c in email):
        raise ValueError("fixed expected account required")
    return HumanRecovery(paths[0], paths[1], email, ConnectionState(paths[2], "default"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recover-only", action="store_true")
    args = parser.parse_args(argv)
    recovery = configured()
    recovery.recover()
    if args.recover_only:
        return
    token = os.environ.get("HKTVMALL_LOGIN_TOKEN", "").strip()
    if len(token) < 32:
        raise ValueError("private backend token required")
    windows = Windows(
        recovery.run,
        slots=1,
        minutes=int(os.environ.get("HKTVMALL_LOGIN_MINUTES", "15")),
        xvfb=os.environ.get("HKTVMALL_LOGIN_XVFB", "Xvfb"),
        x11vnc=os.environ.get("HKTVMALL_LOGIN_X11VNC", "x11vnc"),
    )
    app = build_app(
        windows,
        token=token,
        novnc_dir=Path(os.environ.get("HKTVMALL_LOGIN_NOVNC", "/usr/share/novnc")),
        direct_view=False,
        start_params=start_params,
    )
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("HKTVMALL_DEFAULT_LOGIN_PORT", "8081")),
        log_level="warning",
        access_log=False,
    )  # noqa: S104


if __name__ == "__main__":
    main()

"""The real default snapshot/profile pair survives interruption and rejected recovery."""

import asyncio
import json
import os
import shutil
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hktvmall_maintenance.cli import lease, stores
from hktvmall_maintenance.human import HumanRecovery, VerifiedSession
from hktvmall_maintenance.human_state import commit, pending_path, profile_path, recover
from hktvmall_maintenance.outcomes import HoldReason
from hktvmall_maintenance.snapshot import publish
from hktvmall_mcp.connections_state import ConnectionState
from hktvmall_mcp.errors import HktvError

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="default custodian uses Linux profile ownership")
EMAIL = "owner@example.com"


def session(email=EMAIL, value="accepted"):
    return {
        "email": email,
        "user_agent": "Synthetic Browser",
        "cookies": [
            {"name": "JSESSIONID", "value": value, "domain": "www.hktvmall.com", "path": "/", "secure": True},
        ],
    }


@pytest.fixture
def custodian(tmp_path):
    root, export, controls = [tmp_path / name for name in ("maintenance", "export", "controls")]
    for folder in (root, export, controls):
        folder.mkdir(mode=0o700)
    profile = profile_path(root)
    profile.mkdir(mode=0o700, parents=True)
    (profile / "accepted-session").write_text("accepted")
    control = ConnectionState(controls, "default")
    control.bind({"issuer": "https://identity.example.com", "sub": "owner-subject"})
    epoch = control.resume()
    state, budget = stores(root)
    assert budget.reserve("hktvmall", limit=1)
    state.record_hold("hktvmall", HoldReason.SIGN_IN_INTERRUPTED)
    publish(export, VerifiedSession(session()), 1, EMAIL, datetime.now(UTC))
    return root, export, control, epoch, state


class Window:
    layout = "desktop"

    def __init__(self, epoch):
        self.params = {"key": "default", "epoch": epoch}
        self.cancelled = threading.Event()
        self.outcome = None

    def env(self):
        return {}

    def waiting(self, host):
        pass

    def saving(self):
        pass

    def finish(self, state):
        self.outcome = state


@pytest.mark.parametrize("outcome", ["connected", "wrong_account", "cancelled", "failed", "old_epoch"])
def test_manual_recovery_preserves_or_replaces_the_existing_default_pair(custodian, outcome):
    root, export, control, epoch, state = custodian
    window = Window(epoch)
    old_snapshot = (export / "snapshot.json").read_bytes()
    old_state = (root / "identity-targets/hktvmall.json").read_bytes()
    ledger = {p: p.read_bytes() for p in (root / "session-keeper").rglob("*.json")}

    async def login(**kwargs):
        Path(kwargs["profile"], "accepted-session").write_text("candidate")
        if outcome == "cancelled":
            raise asyncio.CancelledError
        if outcome == "failed":
            raise ValueError("synthetic provider failure")
        if outcome == "old_epoch":
            barrier = control.suspend()
            control.finish(barrier)
            control.resume()
        return session("other@example.com" if outcome == "wrong_account" else EMAIL, "candidate")

    operation = HumanRecovery(root, export, EMAIL, control, login=login, policy=lambda: True)
    if outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(operation.run(window))
    else:
        asyncio.run(operation.run(window))
    if outcome == "connected":
        assert window.outcome == "connected"
        assert (profile_path(root) / "accepted-session").read_text() == "candidate"
        assert json.loads((export / "snapshot.json").read_bytes())["generation"] == 2
        assert state.load("hktvmall").last_generation == 2
        assert state.load("hktvmall").hold is None
    else:
        assert (profile_path(root) / "accepted-session").read_text() == "accepted"
        assert (export / "snapshot.json").read_bytes() == old_snapshot
        assert (root / "identity-targets/hktvmall.json").read_bytes() == old_state
    assert all(p.read_bytes() == raw for p, raw in ledger.items())  # consumed attempts are never refunded
    assert not pending_path(control).exists()
    assert not list(profile_path(root).parent.glob("human-*"))


@pytest.mark.parametrize("stage", ["prepared", "profile", "snapshot", "state", "committed"])
def test_process_death_at_each_commit_boundary_recovers_a_complete_pair(custodian, stage):
    from hktvmall_mcp.session import SessionStore

    root, export, control, epoch, state = custodian
    reader = SessionStore(export / "snapshot.json", EMAIL)
    reader.read_guard = control.admission
    assert reader.generation == 1
    old_snapshot = (export / "snapshot.json").read_bytes()
    old_state = (root / "identity-targets/hktvmall.json").read_bytes()
    candidate = profile_path(root).parent / ("human-" + "a" * 32)
    shutil.copytree(profile_path(root), candidate)
    (candidate / "accepted-session").write_text("candidate")
    staged_export = root / "staged-export"
    staged_export.mkdir(mode=0o700)
    publish(staged_export, VerifiedSession(session(value="candidate")), 2, EMAIL, datetime.now(UTC))
    snapshot = (staged_export / "snapshot.json").read_bytes()
    pid = os.fork()
    if pid == 0:
        with lease(root):
            commit(
                root,
                export,
                control,
                epoch,
                candidate,
                snapshot,
                state,
                2,
                state.load("hktvmall").hold.hold_id,
                fault=lambda current: os._exit(73) if current == stage else None,
            )
        os._exit(74)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 73
    with pytest.raises(HktvError):
        control.require_active()
    # Even a cached reader's status/generation refresh cannot retain candidate
    # credentials that a PREPARED crash will subsequently roll back.
    with pytest.raises(HktvError):
        reader.status()
    assert reader._held.generation == 1
    # An owner disconnect/resume before recovery must not allow a late publication.
    if stage != "committed":
        barrier = control.suspend()
        control.finish(barrier)
        control.resume()
    else:
        barrier = control.suspend()
        control.finish(barrier)
    with lease(root):
        recover(root, export, control)
    if stage == "committed":
        assert (profile_path(root) / "accepted-session").read_text() == "candidate"
        assert (export / "snapshot.json").read_bytes() == snapshot
        assert state.load("hktvmall").hold is None
    else:
        assert (profile_path(root) / "accepted-session").read_text() == "accepted"
        assert (export / "snapshot.json").read_bytes() == old_snapshot
        assert (root / "identity-targets/hktvmall.json").read_bytes() == old_state
    if stage == "committed":
        assert control.read()["state"] == "disconnected"
        with pytest.raises(HktvError):
            control.require_active()
    else:
        control.require_active()
        assert reader.generation == 1
    assert not candidate.exists() and not pending_path(control).exists()


def test_default_backend_rejects_caller_selected_paths_and_accounts(custodian):
    root, export, control, epoch, _ = custodian
    window = Window(epoch)
    window.params["profile"] = "/unexpected"
    operation = HumanRecovery(root, export, EMAIL, control, login=lambda **_: pytest.fail("no browser"))
    asyncio.run(operation.run(window))
    assert window.outcome == "failed"


def test_default_http_start_parameters_cannot_select_another_target_or_path():
    from hktvmall_maintenance.human import start_params

    valid = {"layout": "desktop", "key": "default", "epoch": "initial"}
    assert start_params(valid) == {"key": "default", "epoch": "initial"}
    for body in (
        {**valid, "profile": "/unexpected"},
        {**valid, "key": "other"},
        {**valid, "epoch": "../../unexpected"},
        {"layout": "desktop"},
    ):
        with pytest.raises(ValueError):
            start_params(body)


def test_stored_hold_needs_attention_without_inventing_signed_out_proof(custodian):
    from hktvmall_maintenance.keeper import IdentityKeeper
    from hktvmall_maintenance.outcomes import IdentityOutcome

    root, export, control, epoch, state = custodian
    _, budget = stores(root)
    before = (export / "snapshot.json").read_bytes()
    keeper = IdentityKeeper(
        target="hktvmall",
        expected_email=EMAIL,
        export_dir=export,
        state=state,
        budget=budget,
        readiness=lambda: pytest.fail("held target cannot request credentials"),
        open_browser=lambda: pytest.fail("stored hold is not a fresh provider observation"),
        lease=lambda: lease(root),
        utc_now=lambda: datetime.now(UTC),
        connection_control=control,
    )
    assert keeper.run(allow_credential_mint=True).outcome == IdentityOutcome.HELD
    assert control.health(1) == "check_needed"
    assert (export / "snapshot.json").read_bytes() == before
    control.note_signed_out(epoch, 1)
    assert keeper.run(allow_credential_mint=True).outcome == IdentityOutcome.HELD
    assert control.health(1) == "sign_in_needed"  # The weaker outcome never hides stronger evidence.


def test_routed_raw_id_namespace_cannot_confuse_default_and_linked_windows():
    from signin_window.client import LoginUnavailable

    from hktvmall_mcp.login_client import RoutedLoginClient

    class Backend:
        def __init__(self):
            self.calls = []

        def start(self, *args, **kwargs):
            return {"id": "m_" + "x" * 22}

        def state(self, window):
            self.calls.append(window)
            return {"state": "waiting"}

        def result(self, window):
            self.calls.append("result")
            return {"synthetic": True}

    normal, managed = Backend(), Backend()
    client = RoutedLoginClient(normal, managed)
    linked = client.start(key="acct_aaaaaaaaaaaa")["id"]
    default = client.start(key="default")["id"]
    assert len(linked) <= 40 and len(default) <= 40
    client.state(linked)
    client.state(default)
    assert normal.calls == managed.calls == ["m_" + "x" * 22]
    assert client.result(default) is None and "result" not in managed.calls
    assert client.result(linked) == {"synthetic": True}
    with pytest.raises(LoginUnavailable):
        client.state("unknown-namespace")

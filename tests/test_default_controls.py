"""The default account's custodian controls (single-account mode): disconnect barriers and signed-out evidence."""

import asyncio
import dataclasses
import sys
import time
from contextlib import nullcontext
from datetime import UTC, datetime

import pytest
from fastmcp import Client
from test_accounts import cookies

from hktvmall_mcp import server
from hktvmall_mcp.accounts import DEFAULT
from hktvmall_mcp.errors import HktvError


@pytest.fixture
def configured(settings, tmp_path):
    controls = tmp_path / "controls"
    controls.mkdir()
    return dataclasses.replace(settings, controls_dir=controls)


def health(accounts):
    """What hktv_status reports about the custodian's evidence for the held generation."""

    async def go():
        async with Client(server.create_server(accounts)) as c:
            return (await c.call_tool("hktv_status", {})).structured_content

    return asyncio.run(go()).get("connection_health")


@pytest.mark.skipif(sys.platform != "linux", reason="optional maintenance requires Linux")
def test_native_positive_signed_out_survives_reader_restart_and_new_generation_supersedes_it(configured, tmp_path):
    from signin_window.sessions import AttemptBudget

    from hktvmall_maintenance.keeper import IdentityKeeper
    from hktvmall_maintenance.models import PersistenceError
    from hktvmall_maintenance.outcomes import IdentityOutcome, SessionDead
    from hktvmall_maintenance.state import TargetStateStore
    from hktvmall_mcp.fsio import atomic_write_json
    from hktvmall_mcp.server import build_accounts
    from hktvmall_mcp.session import session_payload

    settings = configured
    expected = settings.expected_email
    path = settings.snapshot_path
    path.parent.mkdir(parents=True)
    atomic_write_json(path, session_payload(1, expected, "Synthetic Browser", cookies(), time.time()))
    accounts = build_accounts(settings)
    assert health(accounts) is None
    control = accounts.control(DEFAULT)
    original_epoch = control.require_active()

    class DeadBrowser:
        def warm(self):
            raise SessionDead()  # Actual positive native observation, not an outcome-label inference.

    now = datetime.now(UTC)
    keeper = IdentityKeeper(
        target="hktvmall",
        expected_email=expected,
        export_dir=path.parent,
        state=TargetStateStore(tmp_path / "keeper-state", utc_now=lambda: now),
        budget=AttemptBudget(tmp_path / "keeper-budget", utc_now=lambda: now, error_type=PersistenceError),
        readiness=lambda: pytest.fail("no automatic credential sign-in"),
        open_browser=lambda: nullcontext(DeadBrowser()),
        lease=nullcontext,
        utc_now=lambda: now,
        snapshot_generation=lambda _: 1,
        connection_control=control,
    )
    accepted = path.read_bytes()
    assert keeper.run(allow_credential_mint=False).outcome == IdentityOutcome.MINT_NOT_PERMITTED
    assert path.read_bytes() == accepted
    restarted = build_accounts(settings)
    assert health(restarted) == "sign_in_needed"
    # Failed/cancelled reconnect must not erase evidence about the retained generation.
    control.resume()
    assert health(restarted) == "sign_in_needed"
    barrier = control.suspend()
    control.finish(barrier)
    control.resume()
    assert health(restarted) == "sign_in_needed"
    hint = control.path.with_suffix(".health.json").read_bytes()
    with pytest.raises(HktvError):
        control.note_signed_out(original_epoch, 2)
    assert control.path.with_suffix(".health.json").read_bytes() == hint
    # Verified publication of a newer generation makes the old fact inapplicable.
    atomic_write_json(path, session_payload(2, expected, "Synthetic Browser", cookies("new"), time.time()))
    assert health(build_accounts(settings)) is None


def test_signed_out_hint_cannot_be_lost_between_hint_and_control_atomic_renames(configured, monkeypatch):
    import hktvmall_mcp.connections_state as module

    control = server.build_accounts(configured).control(DEFAULT)
    old = control.require_active()
    control.note_signed_out(old, 1)
    write = module.atomic_write_json

    def fail_control_commit(path, data):
        if path == control.path:
            raise OSError("synthetic interrupted control commit")
        write(path, data)

    monkeypatch.setattr(module, "atomic_write_json", fail_control_commit)
    with pytest.raises(OSError):
        control.resume()
    assert control.read()["epoch"] == old
    assert control.health(1) == "sign_in_needed"


@pytest.mark.parametrize("resume_during_warm", [False, True])
@pytest.mark.skipif(sys.platform != "linux", reason="optional maintenance requires Linux")
def test_maintenance_cannot_publish_after_disconnect_or_disconnect_resume(configured, tmp_path, resume_during_warm):
    from signin_window.sessions import AttemptBudget

    from hktvmall_maintenance.keeper import IdentityKeeper
    from hktvmall_maintenance.models import PersistenceError
    from hktvmall_maintenance.outcomes import IdentityOutcome
    from hktvmall_maintenance.state import TargetStateStore

    control = server.build_accounts(configured).control(DEFAULT)
    now = datetime.now(UTC)
    writes = []

    class Browser:
        def warm(self):
            epoch = control.suspend()
            control.finish(epoch)
            if resume_during_warm:
                control.resume()
            return "default@example.com"

    keeper = IdentityKeeper(
        target="hktvmall",
        expected_email="default@example.com",
        export_dir=tmp_path / "exports",
        state=TargetStateStore(tmp_path / "maintenance", utc_now=lambda: now),
        budget=AttemptBudget(tmp_path / "budget", utc_now=lambda: now, error_type=PersistenceError),
        readiness=lambda: pytest.fail("no credential readiness during warm"),
        open_browser=lambda: nullcontext(Browser()),
        lease=nullcontext,
        utc_now=lambda: now,
        snapshot_writer=lambda *args: writes.append(args),
        snapshot_generation=lambda path: 0,
        connection_control=control,
    )
    result = keeper.run(allow_credential_mint=False)
    assert result.outcome == IdentityOutcome.DISCONNECTED
    assert result.exit_code == 0 and result.generation is None
    assert writes == []
    if not resume_during_warm:
        assert keeper.run(allow_credential_mint=True).outcome == IdentityOutcome.DISCONNECTED

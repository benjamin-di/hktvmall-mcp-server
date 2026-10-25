"""The HKTVmall target uses shared durable holds and its own snapshot contract."""

import sys
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

if sys.platform != "linux":
    pytest.skip("optional maintenance requires Linux", allow_module_level=True)

from signin_window.sessions import AttemptBudget

from hktvmall_maintenance.keeper import IdentityKeeper
from hktvmall_maintenance.models import PersistenceError, ProviderReadiness
from hktvmall_maintenance.outcomes import (
    HoldReason,
    IdentityOutcome,
    SessionDead,
    SignInHold,
)
from hktvmall_maintenance.state import TargetStateStore


def CredentialBudget(root, **kwargs):
    return AttemptBudget(root, error_type=PersistenceError, **kwargs)


EMAIL = "account@example.com"
NOW = datetime(2026, 10, 18, 14, tzinfo=UTC)


@pytest.mark.parametrize(
    "reason",
    [
        HoldReason.CAPTCHA_IMAGE_REQUIRED,
        HoldReason.OTP_MAIL_TIMEOUT,
        HoldReason.OTP_MAIL_AMBIGUOUS,
        HoldReason.OTP_REJECTED,
    ],
)
def test_hktvmall_failure_holds_across_runs_and_never_refunds_budget(tmp_path: Path, reason: HoldReason) -> None:
    now = NOW
    calls = []
    store = TargetStateStore(tmp_path / "state", utc_now=lambda: now)
    budget = CredentialBudget(tmp_path / "budget", utc_now=lambda: now)

    class Browser:
        def warm(self):
            raise SessionDead()

        def sign_in(self):
            calls.append("sign_in")
            assert store.load("hktvmall").hold is not None
            raise SignInHold(reason)

    keeper = IdentityKeeper(
        target="hktvmall",
        expected_email=EMAIL,
        export_dir=tmp_path / "export",
        state=store,
        budget=budget,
        readiness=lambda: ProviderReadiness(provider="hktvmall"),
        open_browser=lambda: nullcontext(Browser()),
        lease=nullcontext,
        utc_now=lambda: now,
    )
    result = keeper.run(allow_credential_mint=True)
    assert result.outcome is IdentityOutcome.HELD
    assert result.reason == reason.value
    assert result.needs_attention
    now += timedelta(days=2)
    again = keeper.run(allow_credential_mint=True)
    assert again.hold_id == result.hold_id
    assert calls == ["sign_in"]


def test_hktvmall_publication_increments_its_generation_without_google_files(
    tmp_path: Path,
) -> None:
    from hktvmall_maintenance.cookies import ExportCookie
    from hktvmall_maintenance.snapshot import (
        publish,
        published_generation,
    )

    class Browser:
        def warm(self):
            return EMAIL

        def export_cookies(self):
            return [ExportCookie("www.hktvmall.com", True, "/", True, 0, "JSESSIONID", "test-session")]

        def user_agent(self):
            return "Mozilla/5.0 Chrome/149.0.0.0 Safari/537.36"

    keeper = IdentityKeeper(
        target="hktvmall",
        expected_email=EMAIL,
        export_dir=tmp_path / "export",
        state=TargetStateStore(tmp_path / "state", utc_now=lambda: NOW),
        budget=CredentialBudget(tmp_path / "budget", utc_now=lambda: NOW),
        readiness=lambda: pytest.fail("warm publication needs no credentials"),
        open_browser=lambda: nullcontext(Browser()),
        lease=nullcontext,
        utc_now=lambda: NOW,
        snapshot_writer=publish,
        snapshot_generation=published_generation,
    )
    assert keeper.run(allow_credential_mint=False).generation == 1
    assert keeper.run(allow_credential_mint=False).generation == 2
    assert {p.name for p in (tmp_path / "export").iterdir()} == {"snapshot.json"}


@pytest.mark.parametrize(
    ("session", "outcome", "reason"),
    [
        ("bad;session", IdentityOutcome.VERIFY_FAILED, "export_incomplete"),
        (None, IdentityOutcome.VERIFY_FAILED, "export_incomplete"),
        ("test-session", IdentityOutcome.PUBLISHED, None),
    ],
)
def test_writer_export_failure_is_verify_failed_not_infrastructure(
    tmp_path: Path, session: str | None, outcome: IdentityOutcome, reason: str | None
) -> None:
    from hktvmall_maintenance.cookies import ExportCookie
    from hktvmall_maintenance.snapshot import (
        publish,
        published_generation,
    )

    class Browser:
        def warm(self):
            return EMAIL

        def export_cookies(self):
            odd = ExportCookie("hktvmall.com", False, "/", True, 0, "odd", "a b;c")
            if session is None:
                return [odd]
            return [
                odd,
                ExportCookie("www.hktvmall.com", True, "/", True, 0, "JSESSIONID", session),
            ]

        def user_agent(self):
            return "Mozilla/5.0 Chrome/149.0.0.0 Safari/537.36"

    store = TargetStateStore(tmp_path / "state", utc_now=lambda: NOW)
    keeper = IdentityKeeper(
        target="hktvmall",
        expected_email=EMAIL,
        export_dir=tmp_path / "export",
        state=store,
        budget=CredentialBudget(tmp_path / "budget", utc_now=lambda: NOW),
        readiness=lambda: pytest.fail("warm publication needs no credentials"),
        open_browser=lambda: nullcontext(Browser()),
        lease=nullcontext,
        utc_now=lambda: NOW,
        snapshot_writer=publish,
        snapshot_generation=published_generation,
    )
    result = keeper.run(allow_credential_mint=False)
    assert result.outcome is outcome
    assert result.reason == reason
    assert store.load("hktvmall").hold is None
    assert (store.load("hktvmall").last_generation == 1) is (reason is None)

"""The error taxonomy (README "Troubleshooting"). Tools report ``{code, next_step, retryable}`` and nothing else.

Exception text never reaches a tool result: every message a model sees comes from the fixed
``NEXT_STEP`` table below (plus, for ``invalid_argument``, our own validation text).
"""

from __future__ import annotations

from typing import Any

NEXT_STEP: dict[str, str] = {
    "invalid_argument": "Fix the arguments and call again.",
    "not_signed_in": (
        "No HKTVmall account is signed in yet; search still works. Ask the person to run `hktvmall-mcp login` "
        "on the computer this server runs on and log in to HKTVmall in the window that opens."
    ),
    "session_expired": (
        "HKTVmall ended the sign-in. If this server runs on the person's computer, ask them to run "
        "`hktvmall-mcp login` again; for a connected person, ask them to reconnect the HKTVmall connector; a "
        "server that reads a session snapshot needs a newer one. Do not retry until hktv_status shows a newer "
        "generation."
    ),
    "not_connected": (
        "This connection is not linked to an HKTVmall account. Ask the person to reconnect the HKTVmall "
        "connector and log in to HKTVmall there."
    ),
    "account_mismatch": (
        "HKTVmall shows a different account than the one this connection was made with. Ask the person to "
        "reconnect the HKTVmall connector and log in again; do not use the basket until then."
    ),
    "session_source_missing": (
        "The session snapshot this server reads is missing or unreadable. Tell the operator to check the program "
        "that publishes it and its mount."
    ),
    "session_source_invalid": (
        "The saved HKTVmall session is invalid or belongs to another account. Ask the person to run "
        "`hktvmall-mcp login` again, or the operator to check the session snapshot's configuration."
    ),
    "blocked": (
        "HKTVmall pushed back. No HKTVmall calls until the pause ends (hktv_status shows when). Do not retry now."
    ),
    "budget_exhausted": "Today's HKTVmall request budget is spent. It resets at 00:00 Hong Kong time.",
    "busy": "Another HKTVmall request is still running. Retry in a few seconds.",
    "upstream_error": (
        "HKTVmall or the network failed. Retry once in a minute. HKTVmall answers only connections from Hong Kong."
    ),
    "schema_changed": (
        "HKTVmall's page or API changed and this server cannot read it. Tell the operator (README "
        '"Troubleshooting"); other tools may still work.'
    ),
    "basket_unreadable": (
        "The basket could not be read, even after the repair step. Ask the person to open the basket in the "
        "HKTVmall app; do not change it from here until hktv_get_cart works again."
    ),
    "outcome_unknown": (
        "A change was sent but the basket could not be read back. Call hktv_get_cart before anything else; "
        "never repeat the change blindly."
    ),
    "internal_error": "hktvmall-mcp failed unexpectedly. Retry once; if it repeats, tell the operator.",
}

RETRYABLE = frozenset({"busy", "upstream_error", "internal_error"})
AGENT_INPUT_CODES = frozenset({"invalid_argument"})


class HktvError(Exception):
    """A classified failure. ``log_detail`` is for logs only and must not carry secrets."""

    def __init__(self, code: str, detail: str | None = None, *, log_detail: str | None = None) -> None:
        if code not in NEXT_STEP:
            raise ValueError(f"unknown error code {code}")
        super().__init__(code)
        self.code = code
        self.detail = detail  # only for invalid_argument: our own validation text
        self.log_detail = log_detail

    def payload(self) -> dict[str, Any]:
        next_step = NEXT_STEP[self.code]
        if self.code == "invalid_argument" and self.detail:
            next_step = f"{self.detail}. {next_step}"
        return {"code": self.code, "next_step": next_step, "retryable": self.code in RETRYABLE}


def invalid(detail: str) -> HktvError:
    return HktvError("invalid_argument", detail)

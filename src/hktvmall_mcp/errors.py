"""The error taxonomy (README "Troubleshooting"). Tools report ``{code, next_step, retryable, action}`` and nothing
else.

Exception text never reaches a tool result: every message a model sees comes from the fixed
``NEXT_STEP`` table below (plus, for ``invalid_argument``, our own validation text, and for ``owner_reconnect``, the
owner's person id).
"""

from __future__ import annotations

from typing import Any

NEXT_STEP: dict[str, str] = {
    "account_selection_needed": (
        "No shopping account is selected. Call hktv_connect and give the person its link to choose their own "
        "account or one shared with them. Never choose for them."
    ),
    "shared_access_removed": (
        "Access to the selected shared account was removed or changed. Call hktv_connect and give the person "
        "its link to choose an account. Do not fall back to another account."
    ),
    "account_changed": (
        "The shopping account or its access changed, or this update has no current account_context. Nothing "
        "was changed by this request. Call hktv_get_cart, check whose basket it is, and reassess the requested "
        "changes before sending an update with its account_context."
    ),
    "history_not_shared": (
        "This shared connection permits shopping but not reading order history. Tell the person; only the "
        "connection owner can grant order-history access. Do not switch accounts to obtain it."
    ),
    "connection_disconnected": (
        "Server access to this HKTVmall connection is stopped. Call hktv_connect and give the person its link to "
        "reconnect."
    ),
    "invalid_argument": "Fix the arguments and call again.",
    "not_signed_in": (
        "No HKTVmall account is connected yet; search still works. Call hktv_connect and give the person its link: "
        "they connect their own HKTVmall account there, or ask a household member to share theirs."
    ),
    "session_expired": (
        "HKTVmall ended the sign-in. Call hktv_connect and give the person its link to reconnect. Do not retry "
        "until hktv_status shows signed_in."
    ),
    "not_connected": (
        "This person has no access to HKTVmall here. Tell them; the administrator decides who has access. If you "
        "are an AI agent, do not change the server's settings."
    ),
    "owner_reconnect": (
        "{owner} shares their HKTVmall connection with this person, and it needs {owner} to reconnect it (they open "
        "the link from hktv_connect). Tell the person to ask {owner}; there is nothing they can fix themselves."
    ),
    "account_mismatch": (
        "HKTVmall shows a different account than the one this connection was made with. Call hktv_connect and ask "
        "the person to reconnect; do not use the basket until then."
    ),
    "session_source_missing": (
        "Connection data this server reads (a saved connection, or the session snapshot and its mount) is missing "
        "or unreadable. Tell the operator; there is nothing the person can fix."
    ),
    "session_source_invalid": (
        "The saved HKTVmall session is invalid or belongs to another account. Call hktv_connect and ask the person "
        "to sign in again, or the operator to check the session snapshot's configuration."
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
# Sign-in problems a person must fix; a member of a shared connection gets owner_reconnect instead.
SIGN_IN_CODES = frozenset(
    {"not_signed_in", "session_expired", "connection_disconnected", "account_mismatch", "session_source_invalid"}
)
# What the agent should do (the fleet's closed set): wait, have a person act, or have the operator repair.
ACTION: dict[str, str] = {
    **{code: "human_account_check" for code in SIGN_IN_CODES | {"owner_reconnect", "not_connected"}},
    "basket_unreadable": "human_account_check",
    **{
        code: "human_account_check"
        for code in ("account_selection_needed", "shared_access_removed", "history_not_shared")
    },
    **{code: "retry_later" for code in ("blocked", "budget_exhausted", "busy", "upstream_error", "internal_error")},
    **{code: "repair_local_infrastructure" for code in ("session_source_missing", "schema_changed")},
}


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
        if self.code == "owner_reconnect":
            next_step = next_step.format(owner=self.detail or "The owner")
        return {
            "code": self.code,
            "next_step": next_step,
            "retryable": self.code in RETRYABLE,
            "action": ACTION.get(self.code, "none"),
        }


def invalid(detail: str) -> HktvError:
    return HktvError("invalid_argument", detail)

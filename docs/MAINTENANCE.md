# Optional account maintenance

The reader and human sign-in window work without credential automation.
`hktvmall-mcp-maintenance` is a separate, explicit Linux worker for one operator-owned
HKTVmall account. It starts no scheduler. Run it in the login-browser image
with a private writable profile/state root, the existing snapshot export directory
and narrowly scoped credential access. Do not give these mounts or credentials
to a reader container.

```sh
hktvmall-mcp-maintenance keep \
  --root /maintenance --export-dir /exports/default \
  --expected-email owner@example.com
```

This warms and independently verifies the existing session. Credential sign-in
requires `--allow-credential-mint` and a configured `HKTVMALL_MAINTENANCE_CREDENTIAL_REFERENCE`
(`op://Example/Account`), an installed 1Password CLI and its explicitly supplied
`OP_SERVICE_ACCOUNT_TOKEN`. Keep the account value and reference in private
runtime configuration; pass no password in arguments. The images do not bundle
the proprietary 1Password CLI. Operators supply their separately installed CLI
at runtime. An optional timezone is set with `--timezone` or `HKTVMALL_MAINTENANCE_TIMEZONE`;
UTC is the default.

A failed or inconclusive warm-up never submits credentials. Only positive
signed-out evidence permits one sign-in, then a new independent verification.
The rolling 24-hour attempt is durably reserved before dispatch. Interrupted
sign-ins leave a hold. Site pushback retains escalating cooldowns; account
mismatch requires an operator. A completed sign-in alone never proves access.

`hktvmall-mcp-maintenance status --root /maintenance` reads stored health without
opening a browser or reading credentials. `acknowledge --hold-id HOLD` clears
only the exact current hold while holding the profile lease. It never refunds
an attempt. Commands emit closed JSON outcomes and fail with a nonzero status
when a hold or stale snapshot needs attention.

## Upgrades and ownership

Keep the whole root private. It holds `identity-targets/hktvmall.json`,
`session-keeper/hktvmall.json`, `browser-profiles/hktvmall/default` and `locks/`.
The snapshot remains in the application's documented format. Profiles and
snapshots carry full account authority.

Stop and wait for the previous writer before adopting its current state. Retain
holds, cooldowns, generation watermarks and consumed attempts. Use one writer
per account, and the same absolute profile and lease paths in every process
sharing it. Never copy a profile while Chromium is running. Rehearse adoption
and rollback with synthetic state; rollback must preserve the latest accounting.
The human window's own further-account keeper remains independent and never
submits credentials.

## Verification

Run `make check` for policy, state, account and snapshot regressions. Release
acceptance also exercises the human window against a synthetic site in the
installed browser image. No CI check reaches a real account.

HKTVmall maintenance additionally requires a read-only MCP mailbox endpoint and
bearer through `HKTVMALL_MAINTENANCE_MAIL_URL` and
`HKTVMALL_MAINTENANCE_MAIL_BEARER`. Its only remote call is
`hktvmall_login_mail({"received_after": "<ISO timestamp>"})`. The endpoint must
enforce recipient, sender, subject, time-window and result limits for this bearer
and return only matching login-code messages. It must not grant general inbox
access. The application independently checks the expected recipient and freshness,
and excludes messages
that predate the attempt. HTTPS is required unless an internal hostname is
explicitly listed in `HKTVMALL_MAINTENANCE_MAIL_HTTP_HOSTS`.

The default CAPTCHA policy requires an explicitly supplied
`HKTVMALL_MAINTENANCE_VISION_PLANNER=module:factory`. The factory returns a
`signin_window.automation.plans.VisionPlanner`. No planner identity or fallback
is built in. `HKTVMALL_MAINTENANCE_CAPTCHA_POLICY=hold` instead retains an operator
hold when a challenge appears. Missing sign-in requirements are checked before
consuming a credential attempt; warm session reuse needs no credential access.

With Chromium and its system dependencies installed, run
`uv sync --locked --extra login` and
`uv run --frozen pytest -m browser tests/test_maintenance_sign_in_browser.py`.
The release runs this same acceptance against the installed candidate image,
with a local HTTPS stand-in for password, OTP and CAPTCHA request guards.

The mailbox tool returns a JSON list (or MCP structured/text content containing
it) of messages with `id`, `from.address`, `toRecipients[].emailAddress.address`,
`subject`, `sentDateTime`, `receivedDateTime` and a plain-text `body`. Text is
prefixed with `[UNTRUSTED_EXTERNAL_CONTENT] `; error or truncated responses fail
closed. More than 20 matches, conflicting or ambiguous codes, unexpected subjects
and sender lookalikes never permit a code submission.


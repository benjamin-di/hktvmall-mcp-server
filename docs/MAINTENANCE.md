# Account maintenance and required connection controls

Stored credential automation is optional. Hosted OIDC management and persistent
connection controls apply regardless of whether credential automation is enabled.
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

## Connection-owner suspension

When exposing Connections for the default account, mount the same private writable
`HKTVMALL_CONNECTIONS_CONTROLS_DIR` into reader and maintenance. A configured directory must exist before startup;
a missing mount must not create new authority. `default.json` retains the manager's issuer/subject and durable epoch.
Disconnect blocks new upkeep/minting and final snapshot publication under the shared lock. A disconnect followed by
fresh owner recovery cannot admit an older run's late snapshot. Profiles, snapshots, holds and consumed attempts remain retained.
Only explicit fresh owner connection actions permit access again; deployment never clears the barrier.

`keep` reports the closed `disconnected` outcome when access is stopped. `status` includes `connection_state` and
reports intentional suspension separately from provider health, retaining freshness and hold facts. Monitoring
should recognize deliberate suspension rather than paging for an intentionally inactive account. This controls this
server's access, not provider-wide sessions or agent permissions.

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


## Shared maintenance contracts

The existing state JSON, hold ids, cooldowns, generation watermarks and rolling
attempt ledger remain compatible. Durable state operations now use
signin-window's supported `MaintenanceStateStore`; this application still owns
its reason enums, verification, snapshot schema and cooldown policy. No state
reset, profile copy or budget refund accompanies this upgrade.

The default credential reader remains 1Password. To use another store, set
`HKTVMALL_MAINTENANCE_CREDENTIAL_READER=example_backend:create_reader` to an
explicit trusted factory implementing signin-window's `CredentialReader`.
Configure an opaque reference with `HKTVMALL_MAINTENANCE_CREDENTIAL_REFERENCE`.
The backend's `available` property must not read secrets or contact a provider.
Only the requested username, password or generated TOTP is resolved just in time.
Origin checks, error redaction, durable budgets and holds apply to all backends.
A selected reader's failure never falls back to the ambient 1Password identity.

## Human recovery in the existing custodian

Run `python -m hktvmall_maintenance.human` alongside upkeep in the same container, hostname and PID namespace.
It uses the existing `HKTVMALL_MAINTENANCE_ROOT`, `HKTVMALL_MAINTENANCE_EXPORT_DIR`,
`HKTVMALL_MAINTENANCE_EXPECTED_EMAIL`, `HKTVMALL_CONNECTIONS_CONTROLS_DIR` and private `HKTVMALL_LOGIN_TOKEN`.
It listens privately on port 8081 (`HKTVMALL_DEFAULT_LOGIN_PORT`) and exposes the standard signin-window backend,
with no direct viewer. It needs the login image's Chromium, Xvfb, x11vnc, noVNC and installed browser policy; it
receives no credential reader, mailbox or planner configuration. A supervisor must drain both it and upkeep if either
child stops. Since 0.14 no reader page opens it (My connections serves people mode, which has no snapshot account);
it remains for `--recover-only` and for operators who drive the backend directly.

The manager chooses the account in HKTVmall's own window; only the configured expected account can commit. The
existing durable profile lease spans the window. A cold candidate preserves accepted state during failed, wrong-account
or cancelled attempts. Publication exchanges the closed candidate profile atomically and writes the single-file
snapshot under a private commit journal and shared admission marker. Startup and every upkeep run recover PREPARED
commits to the previous pair; COMMITTED records finish cleanup without republishing an old epoch. No native warm-up
runs while recovery is outstanding. `python -m hktvmall_maintenance.human --recover-only` performs the same recovery
under the profile lease. Preserve the journal, backups, controls and accounting on rollback.

Positive native `SessionDead` observations are projected as a private closed hint in the shared controls directory,
bound to the accepted snapshot generation and captured control epoch. `hktv_status` (`connection_health`) can
therefore report sign-in needed after a reader restart without deleting the accepted snapshot or making provider HTTP requests.
A cancelled recovery retains that fact; a newer verified publication supersedes it. Outcome labels such as a hold,
missing host requirements or exhausted budget alone never manufacture signed-out evidence. An older native run
cannot overwrite a newer owner action's health hint.

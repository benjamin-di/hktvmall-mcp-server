# Changelog

All notable changes are recorded here. This project follows
Semantic Versioning.

## [0.15.4] - 2026-10-31

_No changes to the published package._

### Upgrade

- Action required: none.
- Rollback: compatible with 0.15.3 and 0.15.2; keep current controls and credentials.

## [0.15.3] - 2026-10-31

### Changed

- Reuse the provider-neutral `account-connections` library for explicit account choices, grant checks and native selection controls (#1).

### Fixed

- Reject malformed shared-selection grants with a controlled response while preserving valid retained and migrated grant revisions (#1).

### Upgrade

- Action required: none.
- Rollback: compatible with 0.15.2; keep the current controls and credentials.

## [0.15.2] - 2026-10-31

_No runtime changes from 0.15.1; keep release notes self-contained for public distribution._

### Upgrade

- Action required: none.
- Rollback: compatible with 0.15.0 and 0.15.1.

## 0.15.1 - 2026-10-31

_No runtime changes from 0.15.0; add maintained release notes for publication. Earlier releases were not recorded here; see their tags._

### Upgrade

- Action required: none.
- Rollback: compatible with 0.15.0.

## [0.15.0] - 2026-10-31

### Changed

- **Breaking:** Require `account_context` from the latest `hktv_get_cart` when calling `hktv_update_cart` in people mode; reject stale account choices before changing a basket ([public changes](https://github.com/benjamin-di/hktvmall-mcp-server/compare/v0.14.6...v0.15.2)).
- **Breaking:** Migrate hosted people controls to version 2 while retaining ownership, shares, disconnected choices and accounting; use compatible readers and browser writers together ([public changes](https://github.com/benjamin-di/hktvmall-mcp-server/compare/v0.14.6...v0.15.2)).
- Make owned and shared account selection explicit on My connections and let owners choose whether a share includes order history ([public changes](https://github.com/benjamin-di/hktvmall-mcp-server/compare/v0.14.6...v0.15.2)).

### Fixed

- Preserve healthy account controls during partial sharing failures and keep sign-in recovery on the configured login route ([public changes](https://github.com/benjamin-di/hktvmall-mcp-server/compare/v0.14.6...v0.15.2)).
- Use clearer connection status, recovery and sign-in instructions while preserving unsent choices and keyboard focus ([public changes](https://github.com/benjamin-di/hktvmall-mcp-server/compare/v0.14.6...v0.15.2)).

### Upgrade

- Action required: stop hosted traffic and all people-mode state users before replacing readers and browser writers together; refresh MCP tool catalogues and read the current basket before updates. See [hosted-control upgrades](docs/PERMISSIONS.md#upgrading-hosted-controls).
- Rollback: incompatible with 0.14.x after version-2 controls are written; recover forward with version-2-aware code while preserving current sessions, grants and revocations.

[0.15.2]: https://github.com/benjamin-di/hktvmall-mcp-server/releases/tag/v0.15.2
[0.15.0]: https://github.com/benjamin-di/hktvmall-mcp-server/compare/v0.14.6...v0.15.2

[0.15.3]: https://github.com/benjamin-di/hktvmall-mcp-server/releases/tag/v0.15.3

[0.15.4]: https://github.com/benjamin-di/hktvmall-mcp-server/releases/tag/v0.15.4

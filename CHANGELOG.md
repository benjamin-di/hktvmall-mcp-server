# Changelog

All notable changes are recorded here. This project follows
Semantic Versioning.

## [0.16.1] - 2026-11-12

_Publishes the changes since the last public release, which the public mirror did not receive; the package is unchanged from 0.16.0._

### Upgrade

- Action required: none.
- Rollback: compatible with 0.16.0.

[0.16.1]: https://github.com/benjamin-di/hktvmall-mcp-server/releases/tag/v0.16.1

## [0.16.0] - 2026-11-12

### Added

- Forward the host build identity to the shared Connections, authentication, relay and login-window pages, using signin-window 0.8 (#6)
- Embed the package version and full source revision in release artifacts, with an offline identity check before image promotion or PyPI upload (#6)
- Show the installed version and source revision in product cards, and attach that identity and a process instance to retained server, command, login service and maintenance diagnostics (#6)
- Report the product version through the native MCP handshake in both supported client modes (#6)

### Fixed

- Attribute human maintenance startup failures and refuse source builds with hidden Git-index inputs (#6)
- Refuse source builds whose sparse checkout or index flags hide package inputs from verification (#6)
- Preserve enclosing Rich console capture and recording while attributing diagnostics, and retain native sink-error handling and terminal rendering (#6)

### Upgrade

- Action required: none. Installs from Git and other builds from a clean checkout embed that commit; editable checkouts identify their source revision as `unknown`.
- Rollback: compatible with 0.15.8; no state format changes.

## [0.15.8] - 2026-11-03

### Fixed

- Identify the verified signed-in person on the Connections page.
- Use signin-window 0.7.1 for consistent mobile action widths, readable long labels and accessible checkbox touch targets.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.15.7; preserve current credentials, connection controls and owner bindings.

[0.15.8]: https://github.com/benjamin-di/hktvmall-mcp-server/releases/tag/v0.15.8

## [0.15.7] - 2026-11-02

### Fixed

- Show "Verify and connect" on the Connections page when the sign-in is not recent, and answer a stale Connect or Share with a "Confirm it's you" page, instead of a blocked redirect that ended in an empty 403 (signin-window-private#4).
- Explain refused Connections requests: an ended sign-in gets a 401 page with a sign-in link, and a cross-site or unauthorized request gets a 403 page saying nothing changed (signin-window-private#4).
- Keep the person signed in when a step-up is abandoned or cancelled, by updating signin-window to 0.6.0; a sign-in in progress during the upgrade must be started again (signin-window-private#4).
- Lay out the sharing rows on the Connections page so their controls no longer overlap the person's name (8e28677).

### Upgrade

- Action required: none.
- Rollback: compatible with 0.15.6.

[0.15.7]: https://github.com/benjamin-di/hktvmall-mcp-server/releases/tag/v0.15.7


[0.15.7]: https://github.com/benjamin-di/hktvmall-mcp-server/releases/tag/v0.15.7

## [0.15.6] - 2026-11-01

_Enforce patched authentication and MCP dependencies for pip installations._

### Security

- Enforce the patched dependency minima in published package requirements, including ordinary pip upgrades (#3).
- Prevent unauthorized access to transformed components and potential OAuth client-secret disclosure in affected MCP client wrappers by updating FastMCP to 4.1.2 (GHSA-7xgj-8392-5q4r, GHSA-vqqp-x5qm-5qp4; 6bae6cd).
- Reject oversized signed tokens before allocating their segments to prevent memory exhaustion by updating joserfc to 1.7.6 (GHSA-9pg5-8c7x-5hfx; 6bae6cd).

### Upgrade

- Action required: none.
- Rollback: compatible with 0.15.5. Versions before 0.15.5 retain the security advisories.

[0.15.6]: https://github.com/benjamin-di/hktvmall-mcp-server/releases/tag/v0.15.6

## [0.15.5] - 2026-11-01

_Security: upgrade from 0.15.4 for patched authentication and MCP dependencies._

### Security

- Prevent unauthorized access to transformed components and potential OAuth client-secret disclosure in affected MCP client wrappers by updating FastMCP to 4.1.2 (GHSA-7xgj-8392-5q4r, GHSA-vqqp-x5qm-5qp4), and reject oversized signed tokens before allocating their segments by updating joserfc to 1.7.6 (GHSA-9pg5-8c7x-5hfx; 6bae6cd).

### Upgrade

- Action required: none.
- Rollback: compatible with 0.15.4; older dependencies retain the security advisories.

[0.15.5]: https://github.com/benjamin-di/hktvmall-mcp-server/releases/tag/v0.15.5

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
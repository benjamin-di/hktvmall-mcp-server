# HKTVmall permissions

## Independent permission checks

| Boundary | Authority and purpose |
| --- | --- |
| My connections (browser) | OIDC authorization-code flow with PKCE, exact issuer/client/callback, required signed group and the administrator-controlled person claim. It authenticates a person managing their own connection and whom they share it with. |
| MCP gateway | Independently validates a human or workload token, its issuer, intended audience/resource, required scopes and service grants. Group membership and a workload's configured owner and service/tool ceiling limit routing. A workload cannot choose another person through a request argument. |
| Private backend | An independent backend bearer and the gateway-set person header select the person; the server resolves their saved account selection and verifies its grant. Strip caller-supplied identity headers. Backend credentials are neither browser sessions nor workload tokens; keep them private. |
| Provider | The connected HKTVmall session authorizes actions at HKTVmall. Possession of it does not authorize an MCP caller. |

Browser login, provider sign-in and sharing grant no agent permissions: the gateway still decides who may call.
Conversely, an MCP grant does not let a caller manage anyone's connection. Tool approvals and provider restrictions
still apply after admission. The application does not mint gateway tokens or enlarge gateway grants.

Removing a group, scope, service grant or workload key affects gateway admission independently of connection state.
Token/session lifetimes matter: use the identity provider and gateway's revocation procedure for urgent agent access
removal. Browser logout ends management access; it does not revoke agents or disconnect the provider. Disconnect
stops this application's new access; previously admitted work may finish. Never erase durable owner bindings or
disconnect records to recover access.

## People, connections and sharing

People come from the identity provider. The first verified call or Connections sign-in of a person creates their
record under `people/<person>/`: a control record (owner binding, epoch, disconnect state, provider account, whom
they share with, display name) beside their sealed session, budget and order cache. No configuration lists people or
assigns accounts. A folder without its control record refuses and is never recreated around stored state.

A connection belongs to the person who signed in to HKTVmall. Only they connect, reconnect, disconnect or share it.
The first explicit connection, selection or sharing action pins their issuer and immutable subject; status reads
never do, and a changed claim cannot transfer a record. The first HKTVmall account a person connects stays theirs
(a reconnect must match it), and an HKTVmall account another person already connected is refused. Switching the
selected connection never changes either binding.

Ownership, offered access and selection are independent. Each person owns at most one provider connection and
selects one shopping account, their own or one explicitly shared with them. They can receive several offers. Sharing
starts empty and needs the owner's fresh authentication. It does not change the recipient's selection; only an
explicit recipient action does. Shared members cannot manage the owner's connection, but can independently connect,
reconnect, disconnect and share their own slot.

New shares permit basket access with order history off by default. The owner can enable history explicitly; the
recipient sees the scope before choosing the account. `hktv_recent_orders` checks this permission before reaching
provider or cached order data. `hktv_status(check=true)` also requires it on shared connections because the deep
canary probes order history; ordinary status reads do not. Grant revisions change on permission updates and reissues,
requiring a new choice.
Display names come from the identity provider and are for display only; person IDs distinguish namesakes.

On My connections, **Shared with** always manages the person's own slot, even while they use a shared account.
Recipients first visit My connections or make an admitted MCP call to appear in the roster. They choose an offered
account under **Shared accounts available to me** without provider login. **Use my account** changes only selection;
the other connection's login, basket, members and caches are untouched. **Leave shared account** removes that grant
and clears its selection; leaving an unselected offer preserves the current selection.
This also clears a selection whose history permission revision has changed. An unreadable record does not hide
healthy account choices or ownership and revocation controls; affected shared accounts remain unavailable and the
page asks for administrator attention. It never repairs retained controls or switches accounts automatically.

Disconnect persists its barrier, cancels windows and deletes the owned sealed session; the record, provider binding,
sharing, budgets, caches and generation floor stay. It stops this connection for everyone using it, and the UI
explains the affected members before submission. It does not alter the owner's selection of another person's shared
account. Reconnect advances the connection epoch and preserves selection and grants. The login window's host and its
operator can see what a person types there.

There is no fallback after expiry, disconnect, revocation or leaving. A disconnected or expired selected shared
connection gives `owner_reconnect`; removed or changed access gives `shared_access_removed`. A reissued grant never
reactivates an old selection. An empty selection gives `account_selection_needed`, even if the person retains an
owned login. A new person's initial selection is their own unsigned-in slot, so they can search before connecting.

Each tool call captures its selected account and authorization at admission, before any provider or cache access.
Previously admitted calls may finish on that captured account. A basket result carries an opaque MAC context bound
to the person, selected account, selection revision, connection epoch and sharing grant. Basket writes require a
matching context. A switch away and back still changes the selection revision; stale writes fail before any
provider request. Contexts do not choose an account, carry cookies or authorize bypassing admission. They expire on
selection/grant/epoch changes, not a wall-clock timer. They are not basket-content revisions or approval tokens.

## Shared selection library

`account-connections` owns the pure offer, selection, capability and revocation
rules and escaped native selection controls. This application keeps its existing
version-2 people records, identity/provider bindings, basket contexts, session
generations and lifecycle barriers. Adopting the library requires no record rewrite
or credential migration. Public error codes and history scope remain unchanged.

## Upgrading hosted controls

On first authenticated contact, version-1 people controls migrate under the people lock to strict version 2. Existing
routing, including disconnected owned selections, is preserved; existing shares retain their documented full history
scope. Credentials, identity bindings, epochs, generations, budgets and caches are preserved. Atomic writes and stable
legacy grant revisions let an interrupted migration resume. An unreadable retained control is never recreated.
The default single-account controls stay at version 1.

Quiesce hosted traffic and replace all people-mode readers and browser writers with a version supporting selection
before admitting new traffic. Earlier versions reject the new control schema. **An older image is not a compatible
rollback after migration.** Recover or fix forward with version-2-aware code. Do not erase selection fields, revert
controls independently of their sessions, or restore a backup that revives revoked grants. Account replacement and
control-schema downgrades are not part of this feature.

## Single-account servers

Without a gateway person header, every bearer and the stdio client use the default account: its own `session.json`
or a snapshot that a custodian publishes ([maintenance](MAINTENANCE.md)). The default account's control record lives
in `HKTVMALL_CONNECTIONS_CONTROLS_DIR`, shared with that custodian; `hktv_status.connection_health` reports the
custodian's positive signed-out evidence for the held generation.

## Required controls for hosted adoption

Set `CONNECTIONS_REQUIRE_CONTROLS=1` for hosted readers and credential writers. Missing control records then refuse
access, including after a state directory is replaced. People's records are created only when no state exists for
them; for the default account, initialize once before starting control-aware roles, using the same runtime user and
paths:

```sh
python -m hktvmall_mcp.connections_state --initialize
```

This local operation makes no provider request. It preserves existing valid controls and refuses a missing record
beside retained initialization evidence. It must not run automatically to repair a damaged or missing adopted volume.
Keep markers with current controls, bindings, credentials and accounting; check actual mounted state before startup
and recover on disposable copies. An older backup or an image that ignores controls is not a safe way to clear a
disconnect.

See [configuration](../README.md#hosting-several-people).

# HKTVmall permissions

## Independent permission checks

| Boundary | Authority and purpose |
| --- | --- |
| My connections (browser) | OIDC authorization-code flow with PKCE, exact issuer/client/callback, required signed group and the administrator-controlled person claim. It authenticates a person managing their own connection and whom they share it with. |
| MCP gateway | Independently validates a human or workload token, its issuer, intended audience/resource, required scopes and service grants. Group membership and a workload's configured owner and service/tool ceiling limit routing. A workload cannot choose another person through a request argument. |
| Private backend | An independent backend bearer and the gateway-set person header select the person; the server picks their own connection or the one shared with them. Strip caller-supplied identity headers. Backend credentials are neither browser sessions nor workload tokens; keep them private. |
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
The first explicit Connect, Disconnect or Share pins their issuer and immutable subject; status reads never do, and a
changed claim cannot transfer a record. The first HKTVmall account a person connects stays theirs (a reconnect must
match it), and an HKTVmall account another person already connected is refused, so nobody signs in to someone
else's account.

The owner may share their connection with other people who have records. Members' assistants then use the owner's
basket and order history; members never see the password and cannot reconnect, disconnect or share it. Sharing
starts empty, needs the owner's fresh authentication, and is refused for someone who has their own connection or
already uses another person's. A person uses at most one account: their own, or the one shared with them. Calls
never fall back to another account; a disconnected or expired shared connection gives members `owner_reconnect`
naming the owner. Unsharing, a member's Stop using and the owner's Disconnect apply from the next call; admitted calls
may finish. Display names come from the identity provider at the person's latest Connections sign-in and are for
display only.

On My connections, the owner manages **Shared with**, including during a reconnect. Recipients first open My
connections with their own identity-provider login (or make an admitted MCP call) to appear in the list; they do
not connect an HKTVmall account first. The list shows person IDs beside display names to distinguish namesakes.
Members see whose account they use and **Stop using**, rather than the owner's sharing controls. See the
[sharing steps](../README.md#hosting-several-people).

Disconnect persists its barrier, cancels windows and deletes the sealed session; the record, its sharing, budgets,
caches and generation floor stay. Reconnect advances the epoch and keeps the provider binding. It does not revoke the
provider everywhere. The login window's host and its operator can see what a person types there.

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

# HKTVmall permissions and recovery

## Independent permission checks

| Boundary | Authority and purpose |
| --- | --- |
| Human connections page | OIDC authorization-code flow with PKCE, exact issuer/client/callback, required signed group and reviewed signed account alias. It authenticates a person managing a provider connection. |
| MCP gateway | Independently validates a human or workload token, its issuer, intended audience/resource, required scopes and service grants. Human group membership and a workload's configured owner and service/tool ceiling limit routing. A workload cannot choose another owner through a request argument. |
| Private backend | An independent backend bearer and a gateway-set account alias select a fixed reviewed account. Strip caller-supplied identity headers. Backend credentials are neither browser sessions nor workload tokens; keep them private. |
| Provider | The retained session, profile or linked-device keys authorize actions at the provider. Possession of those credentials does not authorize an MCP caller. |

Browser login, provider consent and successful connection recovery grant no agent permissions. Conversely, an MCP service grant does not make a caller the connection's management owner. Tool approvals and provider restrictions still apply after admission. The application does not mint gateway tokens or enlarge gateway grants.

Removing a group, scope, service grant or workload key affects gateway admission independently of connection state. Token/session lifetimes matter: use the identity provider and gateway's revocation procedure for urgent agent access removal. Browser logout ends management access; it does not revoke agents or disconnect the provider. Disconnect stops this application's new access; previously admitted work may finish. Never erase durable owner bindings or disconnect records to recover access.

## Shared use and management

The reviewed account map selects an existing account ID for each permitted alias. Several aliases may use one shared account; its separate manager map names exactly one management alias. Only that manager can start or complete a window or disconnect. The first explicit management action pins issuer and immutable subject. Neither an email address, changed alias nor a status read transfers management ownership.

Provider identity is independently checked against the configured account. Other accounts use an isolated temporary browser and sealed accepted credentials. The managed default account uses the existing maintenance custodian: its human child gets the fixed account, paths and login token, while stored credential, restricted mailbox and optional planner authority remain confined to upkeep. The reader keeps a read-only snapshot and gets no profile or maintenance credentials.

The manager's Connect/Reconnect opens a cold candidate profile under the existing profile lease. Only a closed, verified candidate can publish the profile and matching snapshot under the captured control epoch. Wrong-account, failed and cancelled attempts preserve accepted data, holds and spent attempts. A private journal and shared pending marker block access until interrupted commits recover. Positive signed-out evidence is bound to the accepted snapshot generation and control epoch, so a recent retained snapshot cannot conceal known rejection or override newer recovery.

Disconnect blocks new reader/upkeep publication. For the managed default it retains the native profile and snapshot; for app-owned accounts it clears the app-owned sealed session through the fenced cleanup path. It does not revoke the provider everywhere. Recovery must retain control state, generations and attempt accounting. See [maintenance](MAINTENANCE.md), [configuration](../README.md) and [remaining rollout work](WORKPLAN.md).

## Required controls for hosted adoption

Set `CONNECTIONS_REQUIRE_CONTROLS=1` for hosted readers and credential writers.
Missing control records then refuse access, including after a state directory
is replaced. Existing standalone behavior remains available for unadopted state;
saved initialization evidence still prevents recreating a lost adopted record.

Before starting matching control-aware roles, explicitly initialize each reviewed
account in its existing private state, using the same runtime user and paths:

```sh
python -m hktvmall_mcp.connections_state --initialize --account <configured-account>
```

This local operation makes no provider request. It preserves existing valid
controls and refuses a missing record beside retained initialization evidence.
It must not run automatically to repair a damaged or missing adopted volume.
Keep markers with current controls, bindings, credentials and accounting; check
actual mounted state before startup and recover on disposable copies. An older
backup or an image that ignores controls is not a safe way to clear a disconnect.

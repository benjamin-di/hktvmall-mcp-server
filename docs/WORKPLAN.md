# Connection rollout workplan

Implemented: mandatory hosted OIDC management, separate shared-use/manager maps, durable owner/epoch controls, isolated linked-account browsers, and real managed-default owner recovery in the existing custodian. Synthetic installed-browser acceptance exercises two owners, wrong account, cancellation, lease exclusion and saved recovery; process-crash regressions cover commit rollback/finalization and cached-reader admission. Positive native signed-out evidence reaches the owner page without a provider call.

Remaining release and deployment acceptance:

- Qualify the final exact source in release CI and both real installed images; retain immutable historical tags and publish only the fixed next release.
- Pin both images through provenance verification and integrate the private custodian supervisor wiring under the full consumer gate.
- Under the deployment lease, retain existing profile/snapshot paths, shared controls, accepted account IDs, holds and attempt ledgers. Verify the reader remains read-only and the human child receives no stored credential, mailbox or planner authority.
- Verify the designated manager can reconnect the actual shared default through its provider window; other mapped users may use it but cannot manage it. Check wrong-account/cancel preservation, provider rejection visibility, disconnect/restart, stale forms and recovery after interruption.
- Independently verify gateway audience/scopes/groups, workload ceilings and account routing. Browser success must not expand agent access.
- Complete deployment acceptance before approved public export/publication. Keep actual identities and operational status in private infrastructure.

See [permissions](PERMISSIONS.md) and [maintenance](MAINTENANCE.md).

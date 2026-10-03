# GameAP enrollment and recovery

Preparation and enrollment are separate operations. Provisioning a node makes
it ready for registration; it does not establish an authenticated connection
to the Panel. Before enrollment, verify network reachability and the dedicated
data volume through the [service roles](../roles/services/).

## Enrollment

Use the [enrollment operation](../playbooks/operations/gameap-enroll-daemon.yml)
through the [normal execution workflow](../README.md#run), selecting one node.
Obtain a fresh connect credential from the Panel and enter it at the hidden
prompt. Treat it as a one-time credential; do not save it in inventory or a
secret provider.

After enrollment, confirm that the Panel recognizes the node as online.
Process liveness and local configuration checks alone do not establish a working
authenticated connection. Check mode does not perform enrollment.

## Recovery

A failed enrollment may already have registered the node on the Panel. Inspect
both ends before attempting recovery; an uncertain outcome is not permission
to consume another credential. Preserve issued identity and certificates rather
than deleting them or registering again as a general connection fix.

Preserve existing Panel data and encryption keys during upgrades. Inspect a
partially initialized database instead of recreating it automatically. Service
convergence does not replace backup and restore verification.

Use [inventory](../inventories/) and the service roles for current deployment
values, credential requirements, and validation behavior. Keep diagnostic logs
local and redact credentials before sharing them.

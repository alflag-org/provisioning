# Shared database operations

## Topology and clients

Host identity and database role are separate. Read current ReplicaSet status to
identify the writable member and its replica; do not infer roles from a host's
name or original placement. Normal convergence maintains the declared topology;
a primary change is a separate operation.

Applications use a local Router, which discovers database roles from ReplicaSet
metadata. Operator-facing DNS aliases are for inspection and are not a source of
routing decisions. Use the write endpoint for migrations and work requiring one
writable backend. Replica reads can be stale, and a read endpoint may fall back
to the primary. Connection endpoints and policies are defined in the Router role.

Provisioning manages database accounts and access; application repositories own
their schema migrations. Runtime and migration accounts should have separate
responsibilities.

## Backup and recovery

A scheduled backup runs on a healthy replica. Taking one on the primary requires
an explicit target and override because it adds load to the writable server.
Backup, restore validation, and topology changes must not overlap on a node.

Only completed, validated backups are eligible for recovery. Restore validation
uses an isolated database instance and must not overwrite the production data.
Successful upload alone does not prove that a backup can be restored.

Replication is asynchronous. Unexpected primary loss can lose transactions, and
the recovery point depends on the latest successfully archived data. Binary-log
archival accompanies successful backup jobs; it is not continuous shipping.
Assess backup freshness and restore evidence before maintenance or recovery.

## Changing the primary

For planned maintenance, verify replication health and backup availability,
switch to a healthy replica, and check client routing before maintaining the
former primary. Rejoin it only after confirming it can catch up safely.

Emergency promotion requires accepting possible data loss and confirming the
same target that will be promoted. Treat the former primary as unavailable;
do not reconnect it as writable until any divergent transactions are resolved.
Neither Router nor DNS provides automatic primary election.

Use the [operation playbooks](../playbooks/operations/) through the execution
workflow in the [README](../README.md). Read each playbook's input assertions for
its current target and confirmation requirements. Check mode and skipped checks
are not evidence of a successful live operation.

## Configuration

The [inventory](../inventories/) defines membership and deployment-specific
values. [Component roles](../roles/components/) define defaults, configuration,
and operation constraints; the [database service role](../roles/services/mysql/)
combines them. Consult these sources for versions, endpoints, secret variables,
account privileges, backup destinations, and schedules.

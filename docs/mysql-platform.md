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

### Configure local backups

The [backup role](../roles/components/mysql_backup/) stores prepared full backups
and closed binary logs in a dedicated local repository, separate from production
data, staging, and restore scratch space. It requires only the existing backup
account secret declared in the
[secret mapping](../inventories/default/secret-requirements/mysql-backup.yml).
Configure that logical name in Atlas and verify it with `atlas secret check`.
The [database service role](../roles/services/mysql/defaults/main.yml) owns the
account and its grants. No object-storage credentials or transfer tools are used.

Enable the backup capability in inventory, choose its local paths, and apply the
[backup-only playbook](../playbooks/components/mysql-backup.yml) through the
execution workflow in the README. It configures backups without converging MySQL
Server, Router, topology, or DNS:

```bash
atlas run provision playbooks/components/mysql-backup.yml \
  --limit '<database-hosts>' \
  --required-secrets inventories/default/secret-requirements/mysql-backup.yml --check
atlas run provision playbooks/components/mysql-backup.yml \
  --limit '<database-hosts>' \
  --required-secrets inventories/default/secret-requirements/mysql-backup.yml
```

The [tool-only playbook](../playbooks/components/mysql-backup-tools.yml) installs
XtraBackup independently when no backup configuration is needed. Both paths
preview APT dependencies and reject database package replacements. Verify the
installed version, unchanged MySQL process start time, and replica health after
applying. Repeat the same check mode; its exit status alone does not prove that
no changes were needed.

Scheduling is controlled separately by `mysql_backup_timer_enabled`. Leaving it
false installs the service and timer for manual execution while keeping the timer
disabled. When enabled, both timers may run; the program skips the current
primary. Schedule, lag tolerance, capacity reserve, and paths are defined in
[inventory](../inventories/) and the [role defaults](../roles/components/mysql_backup/defaults/main.yml).
The programs do not delete retained backups or binary logs. Operators own local
capacity and retention decisions.

### Take a backup and inspect it

Inspect runtime roles first. On the current secondary, start the managed service
and check its result:

```bash
sudo systemctl start mysql-physical-backup.service
sudo systemctl show mysql-physical-backup.service -p Result -p ExecMainStatus
sudo journalctl -u mysql-physical-backup.service --since today
```

The service lowers CPU and I/O priority. Alternatively, run the
[backup operation](../playbooks/operations/mysql-backup.yml) against both database
members through the README workflow, or invoke the installed backup program with
`--config '<backup-config>'`. Protected configuration files already contain the
account connection settings; the operation does not inject secrets again.

The source must be a healthy secondary with bounded lag. Backup, restore, and
topology operations share a nonblocking lock. This implementation leaves
replication threads running and uses `--slave-info`; it does not use
`--safe-slave-backup` or grant replication thread-control privileges. Health is
checked before backup, after the copy, and after binary-log archival. The primary
override is reserved for explicitly authorized exceptional operations.

Read the configured status JSON for the last attempt, success, failure, byte
size, phase durations, lag observations, backup path and binlog manifest. List
completed backups without starting MySQL:

```bash
sudo /usr/local/sbin/mysql-restore-test --config '<backup-config>' --list
```

Backups are grouped by source node, server UUID, and backup ID. Each contains a
manifest with server/tool versions, GTID and binlog position, file sizes and
SHA-256 digests. The completion marker binds the manifest digest and source
identity. Incomplete directories are excluded from selection; every file is
verified before restoration. An existing backup is never overwritten. An
archival failure leaves a failed job even if its full backup was already stored;
verify log coverage separately. Staging is removed after each attempt.

### Restore a local backup into an isolated MySQL instance

Run on the current secondary, using a backup ID from the completed-backup list:

```bash
sudo /usr/local/sbin/mysql-restore-test \
  --config '<backup-config>' --backup-id '<backup-id>'
```

Omitting the ID selects the newest completed backup. The program verifies the
repository copy, creates a disposable copy, checks it again, prepares it, and
copies it back into an empty temporary datadir. Prepare, copy-back and MySQL
startup use transient systemd services with private networking and a read-only
filesystem except for the relevant temporary output directory. Production data
is never a restore target. Startup disables replication autostart, binary
logging, scheduled events, and MySQL X Protocol.

Verification checks `SELECT 1`, MySQL version, expected databases, a representative
system table count, and equality of restored and backup GTIDs. The result and
status report timing and counts without printing row data. Application databases
also need application-specific consistency checks before recovery acceptance.
Cleanup verifies process ownership, stops owned services, and removes temporary
copies only after termination is confirmed. A cleanup failure retains temporary
files for inspection and reports failure. The original completed backup remains.

### Binary logs, PITR, and replica rebuilds

A full backup job rotates and archives closed binary logs under its source node
and UUID. Each run records file names, sizes, and SHA-256 digests. Identical
existing files are reused; conflicting bytes fail rather than overwrite them.
Archival accompanies full backups and is not continuous shipping.

PITR needs the log named in `xtrabackup_binlog_info` at its recorded position,
followed by all logs through the intended recovery time from the same node and
UUID. Verify the archive manifest, digests, and sequence before replay. Never mix
primary and secondary streams. Replay with `mysqlbinlog` into a separate isolated
instance through its explicit local socket, excluding already executed GTIDs and
setting the target time in the same time zone used by `mysqlbinlog`. Use shell
`pipefail`, keep row events out of shared logs, and verify recovered GTIDs and
application data. Add `--pitr` to the restore-test command to replay from the backup's recorded
position through the last closed binlog in its archival manifest. The command
verifies the source identity, file sequence, sizes and digests, pipes SQL directly
into the isolated instance, checks both process exit codes, and reports whether
new GTID transactions were applied. This selects the end of that archived log
stream; use a separately controlled isolated instance for another target time.
A full restore without `--pitr` does not establish PITR success. See the
[Percona PITR procedure](https://docs.percona.com/percona-xtrabackup/8.4/point-in-time-recovery.html).

For a replica rebuild, recover into a new isolated host with a compatible server
series, verify its data and GTIDs, assign a unique server identity, and use the
ReplicaSet administration workflow only after topology-change authorization.
Never copy back into an existing member's production datadir during validation.

Local backups share the storage host's failure domain. Recovery after losing
that host requires a separately preserved copy of the repository. On a new
recovery host, obtain the code and compatible MySQL/XtraBackup binaries, restore
the repository, configure the role's paths and MySQL system account, and grant
AppArmor access to the temporary restore directory. Run the same isolated test
before authorizing a production recovery. Local verification does not establish
offsite or whole-site disaster recovery.

### Failure checks

For a health refusal, inspect runtime role, both replication threads, error
numbers and lag. For lock contention, inspect the backup and topology-lock
services; never remove a lock file to defeat an active owner. For checksum
failures, preserve the completed backup and take a new copy under another ID.
For capacity failures, check repository, staging, and temporary restore usage.
Inspect only the temporary instance's unit, PID, socket and log when diagnosing
restore failures; do not stop the production MySQL service.

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

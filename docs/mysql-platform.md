# Shared MySQL platform

## Architecture

The shared database platform is a two-member MySQL 8.4 InnoDB ReplicaSet. MySQL
Shell owns the current `PRIMARY` and `SECONDARY` roles; hostnames identify nodes,
not database roles.

| Component | Placement | Responsibility |
| --- | --- | --- |
| MySQL Server 8.4 | `mysql-shared01`, `mysql-shared02` | Database engine, GTID, row-based binary logging |
| InnoDB ReplicaSet | both MySQL nodes | Asynchronous primary-to-secondary replication |
| MySQL Router 8.4 | `web01`, `workbench01`, `control01` | Local topology-aware client endpoints |
| Physical backup | both MySQL nodes | Role-aware XtraBackup and binlog archival |
| Zabbix 7.0 LTS | `monitor01` | Server, frontend, and local PostgreSQL |
| NSD role aliases | both authoritative DNS servers | Operator-facing runtime role records |

`monitor01` uses local PostgreSQL so loss of the shared MySQL platform does not
also remove monitoring. That database is a monitoring bootstrap dependency and
is not a shared database tenant.

## Node identities and ReplicaSet

The permanent node identities are:

- `mysql-shared01.srv.alflag.internal` at `10.10.10.221`, `server_id=221`
- `mysql-shared02.srv.alflag.internal` at `10.10.10.222`, `server_id=222`

Both servers enable GTID, `enforce_gtid_consistency`, row-based binary logs,
relay-log recovery, replica binlogging, and seven-day binary log retention.
Remote MySQL traffic requires encrypted transport. Remote root login,
anonymous accounts, and the `test` database are absent.

On a fresh deployment, `mysql-shared01` is only the bootstrap seed. After the
ReplicaSet exists, the current role comes exclusively from MySQL Shell status.
Normal `site.yml` convergence verifies the two-member topology and may rejoin an
unambiguously `OFFLINE` member. It never moves `PRIMARY`.

## Client-local MySQL Router

Each member of `mysql_router_clients` runs its own Router. Applications connect
to loopback or a UNIX socket, never directly to a MySQL node or a role DNS alias.
Prefer the UNIX socket when the application driver supports it; loopback TCP is
the broadly compatible interface.

| Use | TCP | UNIX socket | Backend selection |
| --- | --- | --- | --- |
| Read/write | `127.0.0.1:6446` | `/run/mysqlrouter/mysql-rw.sock` | Current `PRIMARY` |
| Read-only | `127.0.0.1:6447` | `/run/mysqlrouter/mysql-ro.sock` | Current `SECONDARY`, with primary fallback |
| Read/write split | `127.0.0.1:6450` | `/run/mysqlrouter/mysql-rw-split.sock` | Statement-aware primary and secondary routing |

The split endpoint enables connection sharing and `wait_for_my_writes` with a
one-second timeout. This gives session-level read-after-write coordination; it
does not make asynchronous replication synchronous. Router-to-MySQL traffic
uses TLS in `REQUIRED` mode. Bootstrap-generated metadata credentials remain in
the Router keyring rather than configuration or inventory.

A new Router builds its bootstrap candidates from the stable FQDNs of the two
`svc_mysql` inventory members. It tries a TLS MySQL session against each member
in inventory order and fails if neither member accepts the session. The member
can be either `PRIMARY` or `SECONDARY`; Router obtains the current topology from
ReplicaSet metadata and reconnects as needed.

Bootstrap uses the TLS-required `mysql_router_bootstrap` account, restricted to
the inventory addresses of `mysql_router_clients`. Its grant set follows the
[MySQL Router 8.4 bootstrap minimum](https://dev.mysql.com/doc/mysql-router/8.4/en/mysqlrouter.html)
needed to create the generated Router metadata account. The stronger
`mysql_replicaset_admin` account is restricted to the two database-node source
addresses where MySQL Shell runs topology operations; it is not available from
application hosts. After bootstrap, Router uses its generated keyring account,
not the bootstrap account.

The operator-facing role DNS aliases are not bootstrap inputs. Missing or stale
role DNS therefore cannot prevent Router from discovering an available stable
ReplicaSet member.

Use the read/write endpoint for migrations, transactions that require a single
backend, and workloads without split-routing support. Use the read-only endpoint
only when stale reads and primary fallback are acceptable.

## Shared tenants

`mysql_shared_tenants` is the desired-state declaration for application
databases and accounts. Schema migrations remain in each application repository;
provisioning creates only databases, users, source restrictions, TLS
requirements, and grants.

```yaml
mysql_shared_tenants:
  - name: example
    database: example_app
    users:
      - name: example_app
        password_var: example_app_password
        clients:
          - web01
        privileges:
          - application
      - name: example_migrate
        password_var: example_migrate_password
        clients:
          - control01
        privileges:
          - migration
      - name: example_reader
        password_var: example_reader_password
        clients:
          - workbench01
        privileges:
          - read_only
```

Database, account, and secret names accept ASCII letters, digits, and
underscores. Client inventory names resolve to exact IPv4 account sources.
Duplicate database names or account/source pairs fail validation.

The standard privilege profiles separate account responsibilities:

- `application` contains runtime DML and `EXECUTE`, without schema DDL;
- `migration` contains the DML and DDL needed by an explicitly declared schema
  migration account;
- `read_only` contains only `SELECT` and `SHOW VIEW`.

Provisioning does not create a migration account unless the tenant declaration
includes one, and it never runs the application's schema migration.

## Required inputs and secrets

Place secret values in the ignored
`inventories/default/group_vars/all/secrets.yml` file or provide them through an
equivalent operator secret source. A complete deployment requires:

```yaml
mysql_replicaset_admin_password: <secret>
mysql_router_bootstrap_password: <secret>
mysql_backup_password: <secret>
mysql_zabbix_monitor_password: <secret>
zabbix_server_database_password: <secret>
zabbix_server_api_password: <secret-at-least-12-characters>
mysql_backup_repository: /absolute/path/on/an/off-host-mount

# One variable for each tenant password_var declaration:
example_app_password: <secret>
example_migrate_password: <secret>
example_reader_password: <secret>
```

`mysql_backup_repository` must resolve through `findmnt` to an allowed off-host
filesystem (`nfs`, `nfs4`, `cifs`, or `fuse.sshfs`). Enabling backup without that
mounted destination fails before package or schedule configuration. There is no
local-only fallback.

## Backup and restore

Percona XtraBackup 8.4 and the same tooling are installed on both database
nodes. Both timers run, but the program reads the local MySQL runtime state at
job start. A healthy `SECONDARY` proceeds; `PRIMARY` exits without taking a
scheduled full backup. Any ambiguous or degraded state fails closed. A manual
primary backup requires both an explicit host and an override. Backup and
restore validation share a local lock, so they cannot overlap on one node.

Each successful job:

1. takes an online physical backup in local staging;
2. runs `xtrabackup --prepare`;
3. copies the prepared backup to the required off-host repository;
4. flushes and archives closed binary logs with node identity, server UUID, and
   GTID metadata;
5. atomically updates `/var/lib/mysql-backup/status.json`.

Closed binary logs are copied off host only as part of a successful full-backup
job. Transactions after the latest successful archive, including transactions
in the active log, are not yet off host. The recovery point therefore depends
on the interval between successful jobs; this is not short-interval binlog
shipping.

The repository layout keeps stable identities across role changes:

```text
<repository>/mysql-shared/physical/<node>/<server-uuid>/<UTC-run-id>/
<repository>/mysql-shared/binlog/<node>/<server-uuid>/
```

Run a normal explicit backup:

```bash
.venv/bin/ansible-playbook playbooks/operations/mysql-backup.yml
```

Run an emergency backup on a named primary only after accepting its load:

Check current ReplicaSet status first and replace `<current-primary>` below.

```bash
.venv/bin/ansible-playbook playbooks/operations/mysql-backup.yml \
  -e mysql_backup_target=<current-primary> \
  -e mysql_backup_allow_primary=true
```

Restore validation never stops or overwrites the production server. It copies a
prepared backup into `/var/lib/mysql-backup/restore-test`, starts a
network-disabled temporary `mysqld`, runs `SELECT 1`, checks every expected
database, shuts down, and removes the scratch datadir.

```bash
.venv/bin/ansible-playbook playbooks/operations/mysql-restore-test.yml

# Validate a particular prepared backup under the configured repository:
.venv/bin/ansible-playbook playbooks/operations/mysql-restore-test.yml \
  -e mysql_restore_backup_path=/absolute/repository/path/to/run
```

Backup, restore validation, planned switchover, and emergency promotion use the
same `/run/lock/mysql-physical-backup.lock` file. A topology playbook holds that
lock through the ReplicaSet mutation, role-DNS update, and Router validation,
so backup or restore cannot begin during the operation. The lock holder is a
bounded systemd service; `RuntimeMaxSec` releases the lock if the controller
disappears before its normal cleanup runs.

## Zabbix

Foundation provisioning installs Zabbix Agent 2 on every managed host. The
server reconciles all inventory hosts through the local API and links the
packaged `Linux by Zabbix agent` template. MySQL nodes also use the packaged
`MySQL by Zabbix agent 2` template with a named local UNIX-socket session.

Provisioning templates add:

- MySQL GTID, row-binlog, read-only, replica state, and role-DNS consistency;
- physical backup age and failure, plus restore-test age and failure;
- Router service, metadata cache, and synthetic connections through ports 6446,
  6447, and 6450.

Monitor for stale or failed backups and restore validation. Primary nodes retain
backup status for inspection but do not raise responsibility alerts.

The MySQL Agent 2 plugin supplies availability, uptime, connection, thread,
query, transaction, slow-query, InnoDB, buffer-pool, binary-log, and replication
metrics. Linux items supply filesystem usage. Backup status is machine-readable
at `/var/lib/mysql-backup/status.json`.

The frontend is served as `http://zabbix.access.internal`. The initial packaged
API password is accepted only for local bootstrap and is immediately replaced
with `zabbix_server_api_password`.

## Operator-facing role DNS

The short-TTL aliases are:

- `mysql-shared-primary.srv.alflag.internal`
- `mysql-shared-replica.srv.alflag.internal`

Topology operations read fresh ReplicaSet status, atomically update the runtime
zone fragment on both authoritative servers, validate the complete zone,
advance the SOA serial, reload NSD, and require identical serials and record
hashes. Normal DNS convergence preserves the runtime fragment.

These aliases are operator-facing runtime state for inspection,
troubleshooting, backup work, maintenance, and Zabbix consistency checks. They
are not application endpoints, Router bootstrap inputs, application failover
mechanisms, or the routing source of truth. MySQL Router metadata is the
application-routing source of truth. During forced failover, the replica alias
is removed if no healthy online `SECONDARY` exists. Zabbix then reports the
missing or mismatched role record until redundancy returns.

## Planned switchover

The switchover playbook requires the target to be the current online
`SECONDARY`. MySQL Shell performs a dry run before `setPrimaryInstance()`. The
playbook then proves that the new primary is writable, the former primary is
read-only, both members remain online, DNS is synchronized, and every Router
reaches the expected backends.

Before the dry run, both MySQL nodes must report the physical-backup unit as
exactly `LoadState=loaded` and `ActiveState=inactive`. The playbook records each
backup timer's enabled and active state, stops the timer without killing a
running service, and acquires the shared backup/restore lock on both nodes. It
holds both locks through the ReplicaSet change, DNS synchronization, and Router
validation. Its `always` cleanup releases both locks and restores each timer to
the recorded state on success or failure. Unknown, missing, failed, activating,
reloading, or deactivating service states fail before topology mutation.

Check current ReplicaSet status first and replace `<current-secondary>` below.

```bash
.venv/bin/ansible-playbook playbooks/operations/mysql-switchover.yml \
  -e mysql_target_primary=<current-secondary>
```

With `--check`, the playbook reads the backup service and timer state and probes
an existing shared lock without stopping the timer or starting the lock-holder
service. An installed MySQL Shell and ReplicaSet management script then run the
corresponding read-only or dry-run topology check. If either is missing,
provisioning reports that reason and skips the topology check. Check mode does
not change the topology, DNS records, Router state, or timer state, and normal
`site.yml` does not create or join ReplicaSet members.

For planned OS or MySQL maintenance:

1. verify Zabbix, backup freshness, idle backup/restore work, and two online
   members;
2. switch `PRIMARY` to the other member;
3. maintain the now-secondary node;
4. run targeted normal provisioning after it returns so an unambiguous
   `OFFLINE` member can rejoin and catch up;
5. optionally switch `PRIMARY` back.

## Emergency failover

Forced failover is a separate, manual data-loss decision. Use it only after
declaring the former primary unavailable and checking the target's replication
position. The target must still be the online `SECONDARY`, and the confirmation
must name the same host. On the promotion target, the playbook applies the same
fail-closed backup-service check, timer suspension, and shared-lock ownership
used by planned switchover. It does not require the unavailable former primary
to accept a lock. Cleanup releases the target lock and restores its timer after
ReplicaSet, DNS, and Router processing succeeds or fails.

Check current ReplicaSet status first and replace `<current-secondary>` below.

```bash
.venv/bin/ansible-playbook playbooks/operations/mysql-failover.yml \
  -e mysql_target_primary=<current-secondary> \
  -e mysql_failover_confirmation=force:<current-secondary>
```

Do not reconnect the former primary as writable. Inspect it for transactions not
present on the promoted member, resolve any divergence, and deliberately rebuild
or rejoin it before returning to normal convergence.

## Limitations

InnoDB ReplicaSet uses asynchronous replication. Planned maintenance supports a
controlled switchover, but unexpected primary loss requires operator-approved
failover. The platform does not provide automatic election and does not
guarantee `RPO=0`. MySQL Router removes topology details from clients but does
not change those replication guarantees. Closed binary logs move off host only
with a successful physical-backup job, so the physical-backup interval limits
the current off-host recovery-point granularity. This is neither continuous
PITR shipping nor an `RPO=0` design.

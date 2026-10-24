# Private infrastructure monitoring

Provisioning configures a dedicated `monitor01` VM running Zabbix, PostgreSQL,
PHP and nginx. Monitoring is independent of the shared MySQL ReplicaSet.
Inventory selects monitored hosts; removing an assignment retains its Zabbix
definitions and history. Retire resources explicitly after reviewing retention.

## Prepare the VM and access

Create the guest separately in Proxmox. This repository configures existing
guests and does not create the monitoring VM. Use Ubuntu 24.04 amd64, initially
2 vCPU, 4 GiB memory and 40 GiB SSD. Verify the template, MGMT VLAN, guest address,
SSH access and available storage before adding `monitor01` to inventory under
`mgmt`, `provider_proxmox`, `platform_vm` and `svc_zabbix_server`.
The agent group includes the server group automatically. No address is reserved
by the empty monitoring groups.

Use the existing private access path to the inventory address. The frontend
requires an operator-supplied TLS certificate trusted by the controller, with
the inventory IP in its subject alternative names. Set the controller CA path
when the CA is not in the system trust store. The role binds HTTPS to the
inventory IPv4 address and permits the declared private networks. It does not
create a public route, Access application or Tunnel. Verify MGMT reachability
and the upstream network ACL before deployment; leave StatusFrame unchanged.

Mount external NFS or CIFS backup storage before convergence and declare its
exact mount point and source in the server's inventory values. Both convergence
and every backup operation reject a missing or different filesystem. A local
directory, local block device, or same-VM backup is insufficient. Protect the
remote filesystem and its own backups: recovery archives contain credentials
and private keys. Configure retention on the external storage; the recovery
program never expires archives automatically.

## Apply in stages

Use the repository's normal setup and targeted foundation workflow first.
Review the [secret declarations](../inventories/default/secret-requirements/)
and create their logical names in the existing Atlas secret provider. Certificate
and private key values remain PEM text. The agent PSK secret is a JSON object
mapping inventory host names to distinct random hexadecimal PSKs of at least
256 bits. Do not place values in inventory or command arguments.

Monitoring deployment is separate from `site.yml`. For the prepared VM:

```bash
atlas run provision playbooks/components/services/zabbix-server.yml \
  --limit monitor01 \
  --required-secrets inventories/default/secret-requirements/zabbix-server.yml --check
atlas run provision playbooks/components/services/zabbix-server.yml \
  --limit monitor01 \
  --required-secrets inventories/default/secret-requirements/zabbix-server.yml
atlas run provision playbooks/components/services/zabbix-agent.yml \
  --limit monitor01 \
  --required-secrets inventories/default/secret-requirements/zabbix-agent.yml --check
atlas run provision playbooks/components/services/zabbix-agent.yml \
  --limit monitor01 \
  --required-secrets inventories/default/secret-requirements/zabbix-agent.yml
atlas run provision playbooks/operations/zabbix-configure.yml \
  --limit monitor01 \
  --required-secrets inventories/default/secret-requirements/zabbix-api.yml
```

A fresh check defers work that needs installed packages. API checks require a
running frontend and existing template/group prerequisites; they are not an
installation simulation. After initial configuration, repeat all three checks
with the same targets. Identity bootstrap skips the permissions module and its
dependent user/token setup in check mode because the permissions module does not
support it. Atlas suppresses
Ansible output, so verify actual target state separately from exit status.
The collection's item and trigger modules report planned changes during check
mode even for unchanged objects. Use an unchanged real API reapply to establish
convergence; those preview change flags are not a reliable drift count.

The initial schema is imported into an empty database only. Partial unrelated
schemas and release/schema mismatches stop deployment. The initial administrator
password is replaced through a loopback-only API before publishing HTTPS.
If initial import succeeds but administrator setup is interrupted, keep the
frontend private and inspect the database; do not recreate it or leave the default
password active to bypass a failed run.

Add Linux hosts to `svc_zabbix_agent` in small batches. Apply the server playbook
to refresh its enrolled-source ingress, synchronize the API host definitions,
then apply agents with an explicit limit. Agents use encrypted active checks
and expose no passive listener. Normal foundation cleanup preserves resources
on explicitly enrolled monitoring hosts. Removing group membership alone is not
a retirement workflow: review both monitoring history and cleanup declarations.

The default Linux template supplies resource, disk and network checks. The
supplement adds Linux pressure metrics. Service selection follows existing
inventory service assignments and can be overridden per host. Only explicitly
enabled services belong in that selection; do not add a Minecraft unit or an
unregistered Runner based on an assumed service name.

## Synchronize with a restricted token

Initial API configuration uses the administrator because retention, users,
actions and templates require administrative access. It also prepares the
`provisioning-sync` identity and an ungenerated token. Generate that token once
in the private UI and store its value under the declared Atlas provider name.
The identity has access only to the managed host group, selected template groups
and explicitly allowed synchronization methods. It cannot delete hosts or
change users, actions or global settings.

Use the restricted credential for subsequent inventory enrollment:

```bash
atlas run provision playbooks/operations/zabbix-sync-hosts.yml \
  --limit monitor01 \
  --required-secrets inventories/default/secret-requirements/zabbix-sync-hosts.yml --check
atlas run provision playbooks/operations/zabbix-sync-hosts.yml \
  --limit monitor01 \
  --required-secrets inventories/default/secret-requirements/zabbix-sync-hosts.yml
```

PSKs are write-only in the API. A SHA-256 tag of each high-entropy key records
which key was applied, allowing unchanged runs to avoid rewriting it. Rotating a
PSK requires synchronizing both the API host and the corresponding agent.
Secret macros must use `type: secret` and resolve values through additional
Atlas secret declarations. Change their non-secret description when rotating
the value: the collection cannot compare masked API values. Never copy a raw
SNMP passphrase into an interface field; reference a secret macro there.

## Add service and device measurements

DNS success, timing and record checks run from `monitor01`, independently of
the DNS hosts' agents. Separate triggers distinguish one failed peer from a
complete recursive or authoritative service failure. Complete failure suppresses
the corresponding individual notifications.

For DNS counters, explicitly enable the owning DNS role's local control socket
in inventory and converge that DNS host first. Recursive DNS additionally
enables extended cumulative statistics. Enroll its agent afterwards. The agent
gets permission only for `stats_noreset`, not other DNS administration commands.
Unbound counters cover queries, cache hits/misses, SERVFAIL and NXDOMAIN; NSD
counters cover queries and response codes. NSD has no recursive cache metrics.
The implementation follows the [Unbound counters](https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound-control.html)
and [NSD counters](https://nsd.docs.nlnetlabs.nl/en/latest/manpages/nsd-control.html).

For MySQL, enroll both ReplicaSet members as agents, then assign both to
`svc_zabbix_mysql`. Run the dedicated `zabbix-mysql.yml` playbook with that group
as the limit and its secret declaration. It reads live writability and replication
thread states before creating a local monitoring account through the single
writable member. It never promotes a member. The account can inspect global
status and performance schema, but cannot read application table data. The
official active MySQL template uses a local Unix-socket session; the supplemental
query reports runtime read-only flags without treating inventory names as roles.
Synchronize the API after the local session exists.

Declare Proxmox and verified network devices under `zabbix_remote_targets`.
Each target supplies its actual template selection, interfaces, role tag and
macros through inventory. Use the packaged
[Proxmox VE by HTTP template](https://www.zabbix.com/integrations/proxmox)
with a separately verified read-only API token. Bootstrap requires TLS peer and
hostname verification on its HTTP items and nested prototypes. Trust the node's
CA on the monitoring server and use a private endpoint matching its certificate
before transmitting the token. Obtain device identity,
SNMPv3 algorithms, interface data and supported MIBs from each IX2215, AT-x510
and UPS before registering it. The role requires encrypted SNMPv3 and secret
macro references. VRRP, vendor CPU/memory, UPS runtime and Proxmox backup results
must be checked against actual supported items; this repository assigns no
unverified OIDs or device addresses. Add application HTTP checks only for
verified private endpoints and expected responses.

## Alerts and capacity

API configuration uses the packaged Discord media type. The notification action
sends a single problem message and a recovery message and pauses during
maintenance. Its recipient has read-only host access and cannot use the frontend.
Review the standard templates' initial thresholds against collected baselines.

Use the packaged Linux and Zabbix health dashboards to examine usage, saturation,
NVPS, item/host counts, queue, cache and poller utilization, and unsupported items.
Unused VMware, IPMI, Java, trap, connector and report metrics and triggers are
disabled in the health template to match the server's disabled features. Database
size and change per collection interval are measured separately. Investigate
enabled unsupported items rather than increasing resources without measurements.

For independent failure detection, select an off-site heartbeat service and
configure it to alert when three consecutive one-minute heartbeats are missing.
Supply its HTTPS POST endpoint through the separate deadman secret declaration,
then apply `zabbix-deadman.yml` explicitly. A heartbeat is sent only while the
local server and PostgreSQL are ready. The external service must deliver alerts
without relying on the monitored site or Zabbix. No provider is selected
implicitly, and existing StatusFrame configuration is untouched.

## Back up and validate recovery

The server takes an initial backup after bootstrap and schedules subsequent
backups. The recovery program serializes backup and restore validation with the
same lock. Systemd bounds the job and its child processes. Completed backups have
checksums, database/schema versions, matching official RC packages and a private
configuration archive. Incomplete backups are not selectable; completed archives
are never overwritten. Backup failure and stale successful-backup time have an
explicit trigger.

Run the backup operation playbook for an additional recovery point. Select a
specific completed directory for restore validation:

```bash
atlas run provision playbooks/operations/zabbix-backup.yml --limit monitor01
atlas run provision playbooks/operations/zabbix-restore-test.yml --limit monitor01 \
  --extra-vars 'zabbix_restore_backup=<absolute-completed-backup-directory>'
```

The restore test verifies checksums, restores into a new randomly named database,
checks schema and core tables, and drops that database. It does not replace
production data or extract configuration over the live host. Inspect isolated
restore databases after a forced termination before removing them.

For disaster recovery, retain the failed VM and recover onto an isolated guest.
Verify the selected completion record and checksums first. Install the package
versions archived with that backup, restore the configuration for those versions,
and restore `database.dump` into a new empty database owned by `zabbix`, using
`pg_restore --exit-on-error --single-transaction --no-owner --role=zabbix`.
Verify `dbversion` against the selected metadata before enabling the server.
Recheck private addresses, certificate identity and storage source before exposing
the recovered frontend. Do not point old packages at a database already upgraded
by newer packages.

## Update a release explicitly

Normal convergence rejects installed versions different from the pinned release.
APT preferences reject other Zabbix versions, including GA. A release change
requires a reviewed change to exact package versions, checksums and expected
schema, together with validation against a restored backup in an isolated guest.
Use the [official upgrade instructions](https://www.zabbix.com/documentation/8.0/en/manual/installation/upgrade/packages/debian_ubuntu)
for that release as well.

For the maintenance window, stop scheduled backups, the frontend's PHP process
and the Zabbix server, and verify they have stopped. Take an external recovery
backup with the old packages/configuration and complete restore validation before
changing packages. Keep the writers stopped, acquire the maintenance lock, and
use an explicit Ansible apt operation with `policy_rc_d: 101` for the reviewed
package set. Never authorize a version transition by rerunning normal convergence
or by disabling its version guard. Validate configuration before starting services,
then verify binary/API/schema versions, real collection and notifications, and
resume scheduled backups. If the schema changes, recovery uses the complete old
backup and its matching packages/configuration; reinstalling old packages alone
is not a rollback. Keep the recovery point until post-update acceptance completes.

## Accept the deployment

Local checks and an API response do not establish host acceptance. Verify the
selected hosts, services, listeners and exact binary/schema versions after apply,
then repeat the same checks. Confirm enabled items collect real values, discovery
has completed and no unsupported enabled item remains. In a maintenance window,
stop and restore an enrolled agent, then one DNS peer, checking problem and recovery
events and actual Discord deliveries. Validate a backup from external storage
and the independent heartbeat's missing-heartbeat alert. Device collection,
notification delivery, external storage and failure injection require the real
VM, credentials and network paths; do not infer them from CI or a test fixture.

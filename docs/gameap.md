# GameAP

GameAP Panel provides the web UI, API, SQLite database, and gRPC endpoint on
an INFRA VM. GameAP Daemon runs on a separate DMZ VM and connects to the Panel.
The Daemon uses systemd to manage future game server processes. This deployment
does not create game servers.

Host addresses and placement are defined in [inventory](../inventories/default/hosts.yml).
The `svc_gameap_panel` and `svc_gameap_daemon` service groups are separate; both
hosts belong to `provider_proxmox` and `platform_vm`. GameAP remains part of the
existing services lifecycle. The daemon data volume is mounted at `/srv/gameap`. When a data device is
explicitly declared in inventory, provisioning checks its stable device ID,
size, signatures, partitions, and existing mounts before initializing an empty
disk as ext4. It mounts the declared filesystem UUID persistently and refuses
to overwrite a different filesystem or hide existing files. Without a declared
device, an externally prepared mount is required. The OS disk is never used as
a substitute.

## Network

Allow these paths before applying:

| Source | Destination | Purpose |
| --- | --- | --- |
| Operator on the internal management network | Panel TCP 8025 | Web UI and API |
| Daemon | Panel TCP 31718 | Enrollment and ongoing gRPC connection |
| Provisioning controller | Both hosts TCP 22 | SSH configuration |
| Both hosts | Configured resolvers | DNS resolution |
| Both hosts | Internet TCP 443 | Release downloads and upstream metadata |

HTTP and gRPC ports are defined in [Panel defaults](../roles/services/gameap_panel/defaults/main.yml).
Use the Panel inventory address for `GRPC_EXTERNAL_HOST`; it must be reachable
from the Daemon and stable when the Panel issues its gRPC certificate.
The roles do not manage network ACLs. Do not open inbound TCP 31717 on the node
for management, or TCP 25565 for a workload that has not been created.
No reverse proxy, public exposure, or Cloudflare Tunnel is installed.

## Installation and secrets

Both roles install exact upstream release binaries, verifying SHA-256 checksums.
Supported hosts are amd64 Linux VMs with systemd. Version and checksum changes
must be reviewed together in [Panel defaults](../roles/services/gameap_panel/defaults/main.yml)
and [Daemon defaults](../roles/services/gameap_daemon/defaults/main.yml).
There are no development builds, containers, or implicit upgrades.

Panel uses `/usr/bin/gameap`, `/etc/gameap/config.env`, `/var/lib/gameap`, and
`gameap.service`. The SQLite database is `/var/lib/gameap/db.sqlite`. Its DSN
sets WAL through modernc SQLite's `_pragma` parameter. No database backup or
copy mechanism is included.

Declare these variable-to-provider-name mappings for the existing
[Atlas provisioning command](../commands/provision.py), using your configured
provider names in the [names-only declaration](../inventories/default/secret-requirements/gameap-panel.yml):

- `services_gameap_panel_auth_secret`: independent random 32 bytes encoded as 64 lowercase hex characters.
- `services_gameap_panel_encryption_key`: a different independently generated secret with the same encoding.
- `services_gameap_panel_admin_password`: initial administrator password, 16–72 UTF-8 bytes.

Declarations contain names only. Keep secret values out of inventory and Git.
`config.env` is owned by root with mode `0600`. Secret-bearing tasks suppress
Ansible output and diffs. Do not rotate encryption keys casually: existing
Panel data depends on them.

The bootstrap helper starts the vendor binary with the initial password in its
environment, suppresses child output, waits for the seeded database and HTTP
readiness, and stops the process before starting the regular service. The
password is never written to `config.env`. Startup verification refuses a
missing or incomplete administrator or a database without WAL; it prevents an
unseeded service from generating a password in the journal. Existing users and
databases are not replaced. A corrupt or partially seeded database requires
inspection instead of automatic recreation.

Daemon uses `/usr/bin/gameap-daemon`, `/etc/gameap-daemon/gameap-daemon.yaml`,
`/etc/gameap-daemon/certs/`, `/srv/gameap`, `/srv/gameap/steamcmd`, and
`/var/log/gameap-daemon/output.log`. The SteamCMD directory is reserved; no game
runtime or SteamCMD installation is required to register the node. The daemon
runs as root to manage system-scoped units; the `gameap` user is available for
future game processes.

Normal convergence preserves vendor-issued node identity, API key, and
certificates and sets `process_manager.name: systemd`. An unregistered host can
be prepared without a setup credential, but it does not pass enrolled-runtime
acceptance. Partial enrollment fails rather than triggering another enrollment.

## Enroll one node

Enrollment is a separate operation and is never called by `site.yml`. The
[operation](../playbooks/operations/gameap-enroll-daemon.yml) accepts exactly one
node. Both services must have been provisioned, the Panel must be reachable,
and the data volume must be mounted before enrollment.

1. Provision the Panel, log in, and open Administration → Dedicated Servers → Create.
2. Obtain the vendor-generated connect URL. It expires after one hour and is
   consumed by enrollment; do not save it in inventory or the secret provider.
3. Prepare the Daemon through the normal GameAP playbook.
4. Run the explicit operation, entering the URL at its hidden prompt:

   ```sh
   atlas run provision playbooks/operations/gameap-enroll-daemon.yml \
     --limit game-node01 \
     --required-secrets inventories/default/secret-requirements/gameap-daemon.yml \
     --prompt-secret services_gameap_daemon_connect_url
   ```

5. Confirm the Dedicated Servers page shows the node online, then repeat normal
   provisioning and its final check. The authenticated
   `GET /api/nodes/{id}/daemon` endpoint also verifies the live daemon connection.

The helper calls the official `gameap.DaemonGateway/Enroll` gRPC method. The
vendor CLI takes the credential only as a process argument; this helper instead
receives it on stdin through the existing volatile Atlas/Ansible boundary.
The Panel's public root CA is obtained through its inventory SSH connection to
verify TLS before transmitting the credential. Panel alone issues the node ID,
API key, and certificates. The legacy port metadata in the enrollment request
does not create a listener or require inbound node management access.

Existing configuration, certificates, or an unresolved enrollment attempt cause
the operation to fail before another credential is consumed. A non-secret
`.enrollment-attempt` marker is retained if the RPC or identity-file writes
fail: the Panel may already have committed the registration. Inspect both ends
before explicitly recovering the node; the operation never automatically retries
or overwrites an enrolled identity. Check mode never reads a connect credential,
calls the enrollment RPC, or starts the daemon.

## Apply and verify

Use an explicit host limit and a names-only secret declaration file:

```sh
atlas run provision playbooks/components/services/gameap.yml \
  --limit game-control01 --required-secrets inventories/default/secret-requirements/gameap-panel.yml --check
atlas run provision playbooks/components/services/gameap.yml \
  --limit game-control01 --required-secrets inventories/default/secret-requirements/gameap-panel.yml
```

The Daemon preparation needs no Panel secrets; use [the empty declaration](../inventories/default/secret-requirements/gameap-daemon.yml) and limit the same playbook to `game-node01`.
Check mode does not install releases, seed databases, enroll, or start services.
Missing runtime prerequisites are reported or skipped; a first-install check
is not runtime acceptance. After applying, repeat the check and inspect the
recap for `changed=0`, `failed=0`, and `unreachable=0`.

Panel validation checks the binary version, active/enabled service, HTTP and
gRPC ports, HTTP response, initialized SQLite database with WAL, and config
permissions. Daemon validation after enrollment checks the version, service,
configuration, and certificate files. An active daemon process alone does not
prove successful registration; verify that the Dedicated Servers page shows
the node online.

For connection diagnostics on the node:

```sh
sudo systemctl is-enabled gameap-daemon.service
sudo systemctl is-active gameap-daemon.service
sudo journalctl -u gameap-daemon.service --since '-10 minutes' --no-pager
sudo tail -n 100 /var/log/gameap-daemon/output.log
```

Inspect repeated gRPC connection failures, registration failures, and certificate
verification errors. Keep logs local and redact any credentials before sharing.
Do not remove certificates or rerun registration as a general connection fix.

## Upgrades and scope

Review official [Panel releases](https://github.com/gameap/gameap/releases),
[Daemon releases](https://github.com/gameap/daemon/releases), and the
[configuration reference](https://github.com/gameap/gameap/blob/master/.env.example)
for renamed environment variables before changing versions. Bump the exact
version and verified archive checksum, run `mise run check`, run a targeted
check, apply, validate, and check convergence again. No upgrade deletes the
SQLite database or repeats enrollment.

Pterodactyl migration is intentionally out of scope.
Kitsunebi integration is intentionally out of scope.
Minecraft servers, Java, Velocity, Paper, TCPShield, public game DNS, and a
backup platform are separate work. Existing Pterodactyl hosts are untouched.

# Alflag provisioning

This repository is the desired state for Alflag-managed hosts. The service
platform includes authoritative and recursive DNS, a two-node shared MySQL 8.4
ReplicaSet with client-local Routers and physical backups, Zabbix 7.0 LTS, web
origins, and NetBox.

## Set up

```bash
mise install
mise run setup
mise run check
```

`mise run setup` installs the pinned Atlas package and its `secrets` extra.
For an Atlas-managed program venv, install `requirements.txt` in that interpreter.
The Atlas host owns provider configuration and bootstrap credentials; this
repository declares logical names only. Do not put values in inventory files.
Roles receive ordinary Ansible variables and fail when required inputs are absent.

## Run

Register this repository as an Atlas Python program, install its dependencies in
that program's virtual environment, and generate shims. Use `provision` with an
explicit playbook, target limit, and required-secret declaration:

```bash
atlas run provision playbooks/site.yml --limit <target> --required-secrets required-secrets.yml --check
atlas run provision playbooks/site.yml --limit <target> --required-secrets required-secrets.yml
```

Create a declaration for the selected playbook and target. Include every required
variable, including tenant-specific password variables, and no secret values:

```yaml
required_secrets:
  mysql_backup_password: mysql.backup.password
  mysql_replicaset_admin_password: mysql.replication.password
```

All declared values must resolve before Ansible starts. Missing values do not fall
back to inventory credentials. Remove superseded local secret variable files once
external storage and recovery have been verified. LXC provisioning does not copy
an operator's local secrets directory.

Injection uses a random directory on the verified `/dev/shm` tmpfs, with directory
mode `0700` and variable-file mode `0600`. The command removes it after completion,
errors, SIGINT and SIGTERM. SIGKILL and host failure cannot run cleanup; restrict
access to the execution account and clear abandoned volatile files before reusing
a recovered host. Disable swap or use encrypted swap on the control host.

The command reports only the Ansible exit status. It suppresses child output,
file logging and persistent fact caching because error output can contain secret
values. Keep `no_log: true` on tasks handling secrets. Run syntax validation without
secret injection for diagnostics. Do not enable callbacks or tasks that persist
control-host credentials. Target-host credential files required by a service are
part of that service's configuration and must have appropriate permissions.

Use `playbooks/bootstrap.yml` for initial provisioning and `playbooks/cloudflare.yml` for
host-side Cloudflare components.

The [shared MySQL platform](docs/mysql-platform.md) guide documents topology,
Router endpoints, tenant declarations, required secrets, backup and restore,
Zabbix monitoring, role DNS, planned switchovers, emergency failover, and the
platform's asynchronous-replication limits.

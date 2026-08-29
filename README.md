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

Secret values belong in the ignored
`inventories/default/group_vars/all/secrets.yml` file or an equivalent operator
secret source. Roles fail before mutation when a required input is absent.

## Run

Inspect the inventory and use check mode with an explicit target before applying a playbook:

```bash
.venv/bin/ansible-inventory --graph
.venv/bin/ansible-playbook --check --diff playbooks/site.yml --limit <target>
.venv/bin/ansible-playbook playbooks/site.yml --limit <target>
```

Use `playbooks/bootstrap.yml` for initial provisioning and `playbooks/cloudflare.yml` for
host-side Cloudflare components.

The [shared MySQL platform](docs/mysql-platform.md) guide documents topology,
Router endpoints, tenant declarations, required secrets, backup and restore,
Zabbix monitoring, role DNS, planned switchovers, emergency failover, and the
platform's asynchronous-replication limits.

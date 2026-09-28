# Provisioning

Ansible desired state for managed hosts. Host configuration lives in
[inventory](inventories/); reusable behavior lives in [roles](roles/).

## Set up

```bash
mise install
mise run setup
mise run check
```

CI uses the same setup and validation tasks. Tests use synthetic data and local
processes; they do not establish that a change works on deployed hosts.

## Run

Configure SSH and privilege escalation for the selected inventory. Register
this repository as an Atlas Python program using [requirements.txt](requirements.txt)
and generate its command shims. Configure secret providers on the control host.

Select a [playbook](playbooks/) and an explicit target. Declare the secrets
required by its roles as variable-to-provider-name mappings, without values:

```yaml
required_secrets:
  service_password: service.password
```

Replace the placeholders and inspect check-mode results before applying:

```bash
atlas run provision '<playbook>' --limit '<target>' --required-secrets required-secrets.yml --check
atlas run provision '<playbook>' --limit '<target>' --required-secrets required-secrets.yml
```

Missing secrets stop execution. Ansible stdout and stderr remain suppressed.
Provision reports per-host and total execution counts, changed hosts, and changed
task source paths, line numbers, and module names. It never reports task names,
arguments, result messages, or diffs. The versioned summary exists only in the
volatile run directory and is removed with the secret inputs.

Review the changed sources before applying. After applying, repeat the same check
and inspect `changed=0`, `failed=0`, and `unreachable=0`; the exit status alone does
not establish convergence. Check mode may skip operations whose prerequisites are
absent. An otherwise successful run fails if its safe summary is unavailable or
invalid. Use local validation without secret injection for syntax diagnostics.

The [database guide](docs/mysql-platform.md) explains topology and recovery
considerations. The [cleanup playbook](playbooks/operations/resource-cleanup.yml)
applies declared retired-resource removals; inspect the inventory declarations
before running it. Removing a host from inventory does not destroy the machine.

The [authoritative DNS role](roles/dns_authoritative/) and
[recursive DNS role](roles/dns_recursor/) own their complete root configurations;
DNS snippets are not loaded separately. Declare zones and resolver policy in
inventory, including any locally maintained settings that must survive convergence.
Host records come from active inventory membership, and reverse zones come from
network CIDRs. VM address configuration remains owned by Proxmox Cloud-Init;
these roles do not write guest Netplan files.

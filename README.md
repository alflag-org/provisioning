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

Missing secrets stop execution. Secret-bearing child output is suppressed;
use local validation without secret injection for syntax diagnostics.
Check mode may skip operations whose prerequisites are absent.

The [database guide](docs/mysql-platform.md) explains topology and recovery
considerations. The [cleanup playbook](playbooks/operations/resource-cleanup.yml)
applies declared retired-resource removals; inspect the inventory declarations
before running it. Removing a host from inventory does not destroy the machine.

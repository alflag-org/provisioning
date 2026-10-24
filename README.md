# Provisioning

Ansible desired state for managed hosts. Host configuration lives in
[inventory](inventories/); reusable behavior lives in [roles](roles/).

## Set up

```bash
mise install
mise run setup
mise run check
```

CI uses the same setup and validation tasks. Unit tests cover custom program logic
with synthetic data; verify Ansible changes and deployed behavior separately.

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
Provision reports only Ansible's exit status. It does not collect execution
counts or task details.

Review the selected inventory and roles before applying. After applying, repeat
the same check and verify the target's configuration and service behavior directly.
An exit status of zero does not establish that no changes were needed. Check mode
may skip operations whose prerequisites are absent. Use local validation without
secret injection for syntax diagnostics.

The [GameAP guide](docs/gameap.md) explains enrollment and recovery decisions.

The [GitHub Actions Runner guide](docs/github-runner.md) covers isolated CI VMs,
explicit registration and removal, and operational verification.

Simple foreground applications can use the shared
[application service component](roles/components/application_service/). Services
prepare their executable and configuration before that component enables their
systemd instances. Processes requiring special systemd behavior use dedicated units.
Removing an assignment retains deployed resources; retire them explicitly through
resource cleanup. The [yomiage guide](docs/yomiage.md) covers its configuration
and operational prerequisites.

The [database guide](docs/mysql-platform.md) explains topology and recovery
considerations. The [cleanup playbook](playbooks/operations/resource-cleanup.yml)
applies declared retired-resource removals; inspect the inventory declarations
before running it. Removing a host from inventory does not destroy the machine.

The [Talos deployment playbook](playbooks/components/talos.yml) prepares the control
host's pinned Talos checkout, configuration, dedicated Atlas venv, and command shim.
Foundation convergence prepares its sources before Atlas discovers commands.
Deployment never runs registry synchronization. Configure external secrets and
select a verified existing Registry Location before running `registry-sync`.

The [DNS guide](docs/dns.md) covers inventory-generated records, static records
for unmanaged devices and services, optional reverse records, and DNS convergence.

The [monitoring guide](docs/zabbix.md) covers the dedicated private Zabbix VM,
explicit agent enrollment, API configuration, and external recovery backups.

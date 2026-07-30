# Alflag provisioning

This repository owns the Ansible desired state for Alflag-managed hosts. Its root is the Ansible
project root: `ansible.cfg`, inventory, playbooks, roles, and collection requirements are all
resolved from the checkout itself.

The Git history was extracted from the `ansible/` subtree of
`alflag-org/daedalus` at source commit `1917b5a`. Daedalus command wrappers, its Python package,
Atlas release metadata, and generated shims are not part of this repository.

## Responsibility boundary

This repository owns:

- inventory and host/group variables;
- playbooks and roles that describe host desired state;
- Ansible collection requirements;
- local and CI validation commands.

Atlas owns release installation, command shims, correlated execution logs, timeout and process
handling, and the reusable `config-*` commands. Atlas does not clone, pull, reset, or otherwise
manage this checkout.

## Layout

```text
ansible.cfg
inventories/
  default/
    hosts.yml
    group_vars/
    host_vars/
playbooks/
roles/
collections/
  requirements.yml
docs/
mise.toml
requirements-dev.txt
```

## Set up the checkout

```bash
mise install
mise run setup
mise run check
```

`mise run setup` installs the pinned validation tools and the declared Ansible collections.
Configuration commands never run dependency installation implicitly.

`mise run check` runs YAML lint, Ansible lint, and syntax checks for `site.yml`,
`bootstrap.yml`, and `cloudflare.yml`. It does not connect to managed hosts.

## Run through Atlas operations

Install the first-party Atlas operations release and build its runtime before invoking commands
from this repository:

```bash
atlas scripts install /path/to/atlas/operations
atlas runtime install
export PATH="/opt/atlas/shims:$PATH"
```

Then keep this checkout as the current working directory:

```bash
cd /path/to/provisioning

config-validate site
config-check site control01
config-diff site control01
inventory-show
```

`config-apply` is the mutating command and always requires one explicit target:

```bash
config-apply site control01
```

Run the same read-only diff for several targets without bypassing Atlas:

```bash
config-diff-many site control01 web01
printf '%s\n' control01 web01 | config-diff-many site
```

The composition command invokes the public `config-diff` executable once per target. Atlas
therefore records each child run under the same operation ID.

See [docs/operations.md](docs/operations.md) for secret handling, production comparison, and
rollback conditions.

# Alflag provisioning

## Set up

```bash
mise install
mise run setup
mise run check
```

## Run

Inspect the inventory and use check mode with an explicit target before applying a playbook:

```bash
.venv/bin/ansible-inventory --graph
.venv/bin/ansible-playbook --check --diff playbooks/site.yml --limit <target>
.venv/bin/ansible-playbook playbooks/site.yml --limit <target>
```

Use `playbooks/bootstrap.yml` for initial provisioning and `playbooks/cloudflare.yml` for
host-side Cloudflare components.

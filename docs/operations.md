# Operate the provisioning repository through Atlas

## Validate without contacting managed hosts

Run repository checks after setup and before opening a pull request:

```bash
mise run check
config-validate site
config-validate bootstrap
config-validate cloudflare
```

`mise run check` runs YAML lint, Ansible lint, and native Ansible syntax checks.
`config-validate` exercises the installed Atlas operations release from this repository's current
working directory. Both paths must succeed.

## Keep secrets outside Git history

Do not commit passwords, tunnel tokens, private keys, vault passwords, or rendered environment
files. The tracked inventory contains only host addressing, non-secret defaults, and references
to operator-owned files.

An optional playbook that requires a secret may read it from a protected, untracked operator file.
For variables shared by the default inventory, create this ignored path and restrict its mode:

```bash
mkdir -p inventories/default/group_vars/all
install -m 0600 /dev/null inventories/default/group_vars/all/secrets.yml
```

Populate it immediately before the operation from the operator secret store, and remove it after
the operation. Do not pass secret values as command-line arguments. Atlas records command
arguments, with redaction limited to recognized secret-bearing option names.

The `cloudflare` playbook is intentionally outside the normal `site` converge. Invoke it only when
the required tunnel token is available through the protected operator file:

```bash
config-check cloudflare connector01
config-diff cloudflare connector01
config-apply cloudflare connector01
```

## Compare replacement output before changing hosts

Do not retire Daedalus based on syntax checks alone. On each representative target, capture the
old read-only result and the Atlas operations result from the same Git revision and inventory:

```bash
# Existing Daedalus checkout
infra diff --limit control01
infra diff --limit web01

# This provisioning checkout
config-diff site control01
config-diff site web01
```

Compare stdout, stderr, and exit status. Investigate every material difference before running
`config-apply`. Repeat the read-only checks over the agreed observation period.

If replacement output is not equivalent, stop new callers and continue using the pinned
Daedalus revision. Do not remove its registry entry, uninstall its release, delete its `infra`
shim, or archive its repository until the real-host smoke comparison passes.

## Apply to one target

After validation and read-only comparison succeed, apply to exactly one named target:

```bash
config-apply site control01
```

Atlas does not prompt, add `--yes`, install dependencies, or change Git state. The explicit
command name and target are the mutation authorization. Inspect the Atlas run record and native
Ansible output before proceeding to another target.

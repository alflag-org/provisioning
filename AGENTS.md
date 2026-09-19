# Repository guidance

## Code and documentation

- Inventory is the source of truth for host membership and deployment values.
  Role defaults, templates, assertions, and programs define configuration and
  behavior. Express enforceable constraints there, not only in prose.
- Keep managed-host connections tied to inventory addresses. A fixed local
  connection targets the invoking machine, which may be a different host.
- Keep README and operator guides focused on purpose, basic execution, and
  operational decisions. Link to code instead of copying hostnames, addresses,
  versions, port tables, secret-variable inventories, or task sequences.
- Keep implementation guidance here. Explain non-obvious local constraints in
  comments beside the code that enforces them. Do not add history documents,
  temporary reports, test counts, or snapshots of the deployed environment.
- Update prose when the reader's workflow or architectural contract changes;
  ordinary inventory and configuration changes should not require prose edits.

## Validation

- Use `mise run check`; CI calls the same task. `mise run lint` and `mise run test`
  provide focused checks. Keep discovery by directory and test naming convention;
  do not maintain lists of individual playbooks or roles in validation commands.
- Add a test only for a concrete failure in our logic or effects. Prefer small
  synthetic inputs and assertions on decisions, files, or process lifetime.
- Do not duplicate configured values, inspect source text, test dependency
  mechanics, or replace most of a workflow with mocks just to exercise its shape.
  Do not add runtime options solely to support test doubles.
- Remove redundant tests and unused helpers. Keep regression protection for
  secret handling, destructive operations, and backup integrity. A local check
  does not prove remote CI, a real database operation, or a deployed change.

## Implementation constraints

- [Secret execution](commands/provision.py) resolves all inputs before spawning
  Ansible. Preserve literal secret values, verified volatile storage, and cleanup
  after all consumers stop, including signal and timeout paths. Keep secret-bearing
  tasks under `no_log`; do not persist or expose child output or injected values.
- Database roles come from live ReplicaSet state. Preserve the separation between
  stable host identities, runtime roles, and client routing metadata. Normal
  convergence must not promote a primary. Keep emergency promotion explicitly
  authorized for the same target that receives the mutation.
- Keep operation-host selection and check-mode guards consistent across commands
  and consumers of their results. Missing prerequisites must produce an explicit
  skip or failure, not an unguarded mutation or an invented successful result.
- Router bootstrap credentials and topology-administration credentials have
  separate source restrictions. Preserve generated keyring and TLS material.
- Backup, restore, and topology changes share lock ownership. Release locks on
  failure and bound their lifetime if the controller disappears. Do not infer
  inactivity from an unsuccessful service-state query.
- Backup selection requires matching completion metadata and checksums. An
  existing archived object with different content is a conflict, not permission
  to overwrite. Restore validation must remain isolated from production data.
- Add retired-resource declarations to inventory and reusable removal behavior to
  [resource_cleanup](roles/resource_cleanup/). Preserve retained services when
  removing dependencies; handle absent resources and repeated execution. Apply
  replicated database-account removals only through a writable member.

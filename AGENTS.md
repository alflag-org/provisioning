# Repository guidance

## Code and documentation

- Inventory is the source of truth for host membership and deployment values.
  Role defaults, templates, assertions, and programs define configuration and
  behavior. Express enforceable constraints there, not only in prose.
- Keep managed-host connections tied to inventory addresses. A fixed local
  connection targets the invoking machine, which may be a different host.
- Keep README focused on setup and basic execution. Keep other documentation
  limited to concepts and operational decisions that cannot be read directly
  from code. Link to authoritative sources instead of copying configuration,
  variable lists, resource layouts, validation checks, or task sequences.
- Keep implementation guidance here. Explain non-obvious local constraints in
  comments beside the code that enforces them. Do not add history documents,
  temporary reports, test counts, or snapshots of the deployed environment.
- Update prose when the reader's workflow or architectural contract changes;
  ordinary inventory and configuration changes should not require prose edits.
  Remove documents that require routine synchronization with code; retain their
  durable implementation constraints here without duplicating code details.

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

- Host classification, network placement, and service intent are independent.
  Services own their middleware and runtime dependencies; host classification
  must not introduce application runtimes unrelated to the selected services.
- DNS roles own complete configurations; inventory must include locally maintained
  settings that need to survive convergence. Guest network configuration remains
  owned by the provisioning platform rather than DNS roles.
- Each service owns its frontend fragments and application resources. Preserve
  shared configuration and other services' resources during convergence and
  retirement. Validate the complete frontend configuration before any reload,
  including after removing a service fragment.
- Run applications from installed releases with isolated language environments
  where applicable. Prefer dedicated unprivileged identities; use additional
  privileges only when required by the managed process. Select listener exposure
  explicitly; a shared frontend does not imply public ingress or TLS provision.
- [Secret execution](commands/provision.py) resolves all inputs before spawning
  Ansible. Preserve literal secret values, verified volatile storage, and cleanup
  after all consumers stop, including signal and timeout paths. Keep secret-bearing
  tasks under `no_log`; do not persist or expose child output or injected values.
  The execution summary may contain only recap counters and changed host/action/
  source locations. Do not add task names or result payloads to this boundary.
- Database roles come from live ReplicaSet state. Preserve the separation between
  stable host identities, runtime roles, and client routing metadata. Normal
  convergence must not promote a primary. Keep emergency promotion explicitly
  authorized for the same target that receives the mutation.
- Keep operation-host selection and check-mode guards consistent across commands
  and consumers of their results. Missing prerequisites must produce an explicit
  skip or failure, not an unguarded mutation or an invented successful result.
- Rebuild derived state from current inputs on each role invocation. Check mode
  must inspect available state and report drift without performing mutations.
- Separate preparation from one-time enrollment. Preserve vendor-issued identity
  and refuse automatic retries after uncertain registration outcomes. Keep
  enrollment credentials transient and outside process arguments. Verify the
  authenticated remote connection rather than accepting process liveness alone.
- Storage initialization must verify the declared device and existing contents
  before mutation; never substitute the OS disk or hide data under a new mount.
  Preserve existing databases and encryption keys during bootstrap and upgrades;
  partial initialization requires inspection rather than automatic recreation.
- Router bootstrap credentials and topology-administration credentials have
  separate source restrictions. Preserve generated keyring and TLS material.
- Change replication account access through the topology API only after verifying
  a healthy writable topology and consistent account metadata. Refuse partial
  rewrites when manually changed accounts disagree with that metadata.
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

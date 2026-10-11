# GitHub Actions Runners

Provide self-hosted capacity for selected high-volume private CI alongside
GitHub-hosted jobs. The [service inventory](../inventories/default/group_vars/svc_github_runner.yml)
contains the initial repository assignment; no VM is assigned yet. Public CI,
other private CI, Windows/macOS jobs, deployments and release publishing keep
their existing execution paths.

## Configure instances

Declare Runner instances in inventory using the [role inputs](../roles/services/github_runner/defaults/main.yml).
An instance has a unique local ID, a GitHub registration URL, and an optional
Runner name. An organization URL registers an Organization Runner; a repository
URL registers a Repository Runner, including personal-account repositories.
An organization instance may specify an existing Runner group at registration;
move an existing Runner between groups in GitHub's settings. Both kinds can
share a CI VM; each ID has its own OS user, installation, service, HOME and work.
Changing an ID creates a different instance. Removing an entry does not retire
its deployed resources: deregister it before removing the inventory entry.

GitHub owns Runner group, repository and workflow access policies, job routing,
and run history. Configure trusted private repositories and workflows there;
exclude untrusted fork PRs from self-hosted routing in the workflows. Ansible does
not copy those policies or inspect job events. Different trust boundaries belong
on separate VMs; separate users and cleanup do not provide VM-level isolation.

Assign only a dedicated Ubuntu CI VM after verifying its address and
**guest-external** isolation, including IPv6 and same-subnet traffic. Allow
controller SSH and necessary outbound GitHub, registry, DNS and time traffic;
deny unnecessary management, production and Proxmox API access. The inventory
confirmation value records the operator's verification; the role does not create
or prove the network rules. Add the VM to its network, provider, platform and
Runner service groups, then use the [install playbook](../playbooks/components/services/github-runner.yml)
with the [standard check/apply procedure](../README.md).

## Register or remove one instance

Select one configured ID with `services_github_runner_selected_instance` in the
target host's inventory variables. The explicit operation playbooks require a
single host and a matching ID. Normal installation still converges the full list.
Obtain a short-lived token for that exact organization or repository in GitHub's
Runner settings. Do not paste it into chat, inventory, shell arguments or history.

```sh
atlas run provision playbooks/operations/github-runner-register.yml \
  --limit '<runner-host>' \
  --required-secrets inventories/default/secret-requirements/github-runner.yml --check
atlas run provision playbooks/operations/github-runner-register.yml \
  --limit '<runner-host>' \
  --required-secrets inventories/default/secret-requirements/github-runner.yml \
  --prompt-secret services_github_runner_token
```

The hidden prompt is not used in check mode. Tokens reach the official Runner
through its input environment under `no_log`; they are not stored in arguments
or remote token files. Vendor registration credentials remain private to that
instance. Matching registration is retained. Registration failure preserves an
attempt marker: inspect GitHub and local state and resolve any orphan before
manually clearing it and retrying. Identity changes require explicit removal.

Before [removal](../playbooks/operations/github-runner-remove.yml), prevent new
jobs from targeting the selected instance through GitHub/workflow routing and
wait for the current job to finish. Confirm that GitHub reports the instance
Idle and that no job is assigned; if this cannot be confirmed, do not proceed.
Keep routing withdrawn throughout check/apply with GitHub's short-lived removal
token. The operation stops only that service; it does not check job status or wait
for jobs to finish, and stopping a busy service can cancel its job. Failed
deregistration retains local files and credentials;
correct authentication and retry. Successful removal deletes only that instance.
Repeating removal requires no token. Shared prerequisites remain installed.

## Updates and verification

Runner instances update themselves through GitHub's official mechanism.
The pinned archive bootstraps new installations; Ansible does not extract it
over installed or self-updated instances. Normal apply enables and starts validated
registered instances, leaving an already running service running even when its
unit changes. It reloads systemd definitions without restarting the process;
unit changes take effect at its next start. To apply them immediately, withdraw
routing and confirm Idle as described for removal, then explicitly restart only
that instance with `sudo systemctl restart github-runner-<instance-id>.service`
through the existing controller SSH path. Verify the service and GitHub Online
status before restoring routing. Check mode does not register or start services.
Simple bounded hooks clean work and HOME before/after jobs, with current Runner files retained while needed. Process
state and private temporary files may outlive a job; cleanup is not a security
boundary. Use existing system logs, Runner diagnostics and GitHub Runner status
for troubleshooting; service activity alone is not Online acceptance.

Before CI routing changes, verify VM apply/check convergence, Online status,
reboot recovery, dependencies, blocked network paths and repeated Push/PR jobs.
Check that all existing validations run, and compare GitHub usage after real jobs.
Restore the previous workflow `runs-on` value to roll back. Local tests do not
establish real registration, service isolation, updates, network or billing results.

GitHub references: [registration scopes](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners),
[service configuration](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/configure-the-application),
and [job hooks](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/run-scripts).

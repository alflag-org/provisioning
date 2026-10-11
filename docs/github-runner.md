# GitHub Actions Runner

Use one Organization Runner to execute jobs from multiple trusted private
repositories. The [service role](../roles/services/github_runner/) installs the
official Runner, manages an unprivileged account and systemd service, and keeps
automatic updates enabled. Repository names are not fixed in the recipe.

## Prepare and install

Configure the registration Organization and existing Runner group in inventory.
GitHub's [Runner group policy](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/manage-access)
controls which repositories and workflows may use it. Use a group that denies
public repository access, and include only repositories whose code can be
trusted equally. The role validates the group policy and existing Runner
membership without changing GitHub's access policy.

Organization Runners serve repositories within that Organization. Personal
account repositories cannot use them directly; repository transfers require a
separate decision. This recipe does not transfer repositories or select a live
Organization on the operator's behalf.

Create an isolated CI VM through the existing infrastructure management process.
Add its confirmed address to inventory, its network group, `provider_proxmox`,
`platform_vm`, and `svc_github_runner`. Do not assign an existing service or
control host. Verify guest-external Firewall/ACL isolation before enabling the
[isolation confirmation](../inventories/default/group_vars/svc_github_runner.yml).
Cover IPv6 and same-subnet traffic. Permit controller SSH, required DNS/time
synchronization, and outbound GitHub/package-registry traffic; deny unnecessary
management, service, and Proxmox API access. The role does not configure or prove
those network rules.

The controller needs authenticated `gh` with access to organization Runner and
group APIs. Its long-lived authentication stays on the controller. Use the
[install playbook](../playbooks/components/services/github-runner.yml) and
[empty secret declaration](../inventories/default/secret-requirements/github-runner.yml)
following the [standard check/apply procedure](../README.md).
Installation alone does not register or start an unregistered Runner.

## Register and remove

Obtain an Organization registration token from GitHub's Runner settings. Do not
paste it into chat, inventory, command arguments, or shell history. Inspect check
mode first, then apply to one host with the hidden transient-secret prompt:

```sh
atlas run provision playbooks/operations/github-runner-register.yml \
  --limit '<runner-host>' \
  --required-secrets inventories/default/secret-requirements/github-runner.yml --check
atlas run provision playbooks/operations/github-runner-register.yml \
  --limit '<runner-host>' \
  --required-secrets inventories/default/secret-requirements/github-runner.yml \
  --prompt-secret services_github_runner_token
```

Registration uses the configured Organization and Runner group. The token is
passed through the official Runner's input environment under `no_log`; no remote
token file is created. Private registration credentials necessarily remain in
the installation directory. Matching registration is retained, and the explicit
registration workflow requires GitHub Online status and the expected labels.
Check mode does not consume a token or prove connectivity.

For removal, stop routing new jobs to the Runner and wait for active jobs to
finish. Use the [remove playbook](../playbooks/operations/github-runner-remove.yml)
with the same check/apply procedure and GitHub's Organization removal token. It
refuses busy Runners, stops the service, and deregisters before deleting dedicated
files and the account. Shared packages remain installed. Repeated removal of an
already removed Runner does not require a token.

If deregistration fails, local registration remains and the service stays
stopped. Correct authentication and retry. If local and GitHub registration
disagree, inspect both before resolving the stale entry. Ordinary convergence
does not replace registrations or delete uncertain credentials. Complete
removal before changing Organizations or re-registering; changing only the
Runner group in inventory does not move a registered Runner between groups.

## Shared jobs and recovery

A single service account runs jobs from every allowed repository. The hooks
reject public repository events, other Organizations, and fork PRs. They clear
repository workspaces and job HOME before and after jobs, preserving the current
event and Runner-managed folders until the end hook. Tool downloads and caches
in job HOME and work are cleared as well. Cleanup failure or insufficient disk
space fails job preparation. After cancellation, the next start hook cleans up
previous job files.

This cleanup is not a security boundary between repositories. Background
processes, private temporary files, and changes to the Runner installation can
survive a job. Any allowed repository's code can access the common Runner's
registration credentials. Use separate Runner VMs for repositories with different
trust requirements, and deregister/rebuild after suspected compromise.

The service does not grant sudo or container-socket access. Install build
prerequisites through the role's package declaration. Runner diagnostic retention
and its separate journal are bounded without changing other services' logging.
Inspect service state and its journal, then confirm GitHub status on the
controller: service activity alone does not establish job readiness. The explicit
registration workflow performs the Online check; ordinary convergence checks
registration consistency. Keep raw diagnostic logs and credentials private.

Before changing CI routing, verify reboot recovery, blocked internal network
paths, dependency access, and repeated Push/PR jobs from the allowed repositories.
Retain existing checks and keep production deployments and release publishing
on their existing Runners. Restore the previous workflow `runs-on` to roll back
routing. Check GitHub's usage screen after actual jobs for billing acceptance.

GitHub references: [custom services](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/configure-the-application),
[job hooks](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/run-scripts),
and [network and update requirements](https://docs.github.com/en/actions/reference/runners/self-hosted-runners).

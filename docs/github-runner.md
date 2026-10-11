# GitHub Actions Runner

Install a repository-dedicated Runner on an isolated Ubuntu VM. The
[service role](../roles/services/github_runner/) manages the official release,
an unprivileged account, job cleanup, and a custom systemd service using the
official `runsvc.sh` entry point. Automatic Runner updates remain enabled;
reapplying does not extract an older archive over an existing installation.

## Prepare and install

Create the VM through the existing infrastructure management process. Add its
confirmed address to inventory, its network group, `provider_proxmox`,
`platform_vm`, and `svc_github_runner`. No Runner host is currently assigned.
Set repository ownership in host variables if it differs from the
[service inventory](../inventories/default/group_vars/svc_github_runner.yml).
Use one VM and OS account per repository; do not place it on an existing service
or control host.

Before setting the isolation confirmation value in inventory, verify external
Firewall/ACL rules that deny unnecessary access to management networks, existing
services, and the Proxmox API. Account for IPv6 and traffic within the same
subnet. Permit controller SSH, required DNS/time synchronization, and outbound
GitHub/package-registry traffic. The role does not create those rules or prove
their effectiveness. Without confirmed isolation, it fails before mutation.

The controller needs authenticated `gh` with repository administration access.
Only controller-side API reads use that authentication; no controller token is
copied to the VM. The role rejects public repositories and mismatched or partial
registration. Follow the [standard Provisioning execution procedure](../README.md)
with the [install playbook](../playbooks/components/services/github-runner.yml)
and [empty secret declaration](../inventories/default/secret-requirements/github-runner.yml).
Installation alone does not register a Runner or start an unregistered service.

## Register

Obtain a short-lived repository registration token in GitHub's Runner settings.
Do not paste it into chat, inventory, command arguments, or shell history.
First inspect the registration playbook in check mode, then apply to one host:

```sh
atlas run provision playbooks/operations/github-runner-register.yml \
  --limit '<runner-host>' \
  --required-secrets inventories/default/secret-requirements/github-runner.yml --check
atlas run provision playbooks/operations/github-runner-register.yml \
  --limit '<runner-host>' \
  --required-secrets inventories/default/secret-requirements/github-runner.yml \
  --prompt-secret services_github_runner_token
```

The prompt disables terminal echo and is not used in check mode. Registration
passes the token through the official Runner's input environment under `no_log`,
without storing it in a remote token file. The Runner's own registration
credentials necessarily remain in its private installation directory.
Existing matching registration is retained. After startup, the role requires
GitHub to report the expected Runner Online with its required labels.
Check mode does not consume a token or establish Online status.

## Jobs and maintenance

Before CI routing changes, verify Online status, automatic startup after a VM
restart, outbound dependencies, and blocked internal network paths. Run the
existing CI for Push and PR more than once, retaining every existing check.
Keep production deployments and release publishing on their existing Runners.

Root-owned hooks reject public repository events and fork PRs. They remove
repository workspaces and job HOME contents before and after jobs; the start
hook preserves the current event and Runner-managed working folders until the
end hook. Tool downloads and caches in job HOME and work are removed too.
Cleanup failure or insufficient disk space fails job preparation. Cancellation
may prevent the end hook; the next start hook removes previous job files.
Job cleanup is not VM recreation: background processes, files in the service's
private temporary directories, and changes to the persistent Runner installation
can outlive a job. Trusted code can access Runner registration credentials.
Do not use this VM for untrusted code; rebuild and deregister after suspected
compromise.

The service grants no supplementary OS groups, uses `NoNewPrivileges`, hides
operator homes and Atlas configuration, and denies standard container sockets.
Build dependencies requiring root belong in the role's package declaration.
Runner diagnostics have bounded age, and its separate journal has bounded
storage without changing logging for existing services.

Inspect the custom service and its journal:

```sh
systemctl status github-actions-runner.service
sudo journalctl --namespace=github-runner -u github-actions-runner.service
```

Inspect GitHub Runner status on the controller; service activity alone does not
prove it can receive jobs. The explicit registration workflow requires Online
status. Ordinary convergence validates registration identity without a
connectivity test. Runner `_diag` contains update diagnostics; do not
publish raw diagnostic logs or credential files. After recovery, reapply and
repeat the same targeted check to verify convergence.

## Remove and recover

Stop sending new jobs to this Runner and wait for current jobs to finish.
Obtain GitHub's short-lived removal token. Run the
[remove playbook](../playbooks/operations/github-runner-remove.yml) using the same
check/apply commands as registration, substituting that playbook and providing
the removal token at the hidden prompt. It refuses a busy Runner, stops the
service, and deregisters before deleting dedicated files and the account.
Shared packages are retained. A repeated removal of an already removed Runner
does not require a token.

If deregistration fails, local registration is retained and the service stays
stopped. Correct the token or authentication problem and retry. If local and
GitHub registration disagree, inspect both sides before manually resolving the
stale entry; ordinary convergence never uses `--replace` or deletes uncertain
credentials. To change repositories or re-register, complete removal first,
then update inventory and run installation/registration again.

Restore the previous workflow `runs-on` value to route CI back to GitHub-hosted
Runners. Do not switch CI before real Runner acceptance or infer billing changes
from a local check; compare the GitHub usage screen after actual jobs.

GitHub references: [custom services](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/configure-the-application),
[job hooks](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/run-scripts),
and [network and update requirements](https://docs.github.com/en/actions/reference/runners/self-hosted-runners).

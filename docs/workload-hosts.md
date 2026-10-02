# Web and application workload hosts

Use managed workload hosts to accommodate multiple services without tying a
machine's identity to one application. The default Web frontend is nginx;
application processes run under systemd with runtimes selected by their services.

## Separate hosts, services, and runtimes

A **host** is a managed VM, LXC container, or physical machine. Its **host
classification** describes the workloads it can accommodate:

- `web_hosts` provides nginx as the standard HTTP frontend for static content,
  reverse proxying, and FastCGI applications.
- `app_hosts` accommodates applications, bots, workers, schedulers, and daemons.
  Membership introduces no application runtime or HTTP frontend dependency.

A **service** is an application managed by `roles/services/<service>` and selected
by an inventory group named `svc_<service>`. A **component** manages reusable
middleware, such as nginx. A **runtime** executes an application and belongs to
the service that requires it.

Network placement, provider, and platform are independent inventory axes.
Classification expresses placement intent; service roles should check their
required components and inputs rather than require a particular classification.

## Converge a workload host

The [site playbook](../playbooks/site.yml) applies platform and common host state
through [foundation](../playbooks/components/foundation.yml), then applies
[nginx](../playbooks/components/nginx.yml) to `web_hosts` before service roles.
An application host without services receives only shared host configuration.
Shared management prerequisites remain part of foundation; application-specific
Python environments, Node.js, PHP, nginx, and container runtimes come from service
intent, not `app_hosts` membership.

After creating a machine and verifying its final address, add it to its network,
provider, platform, and workload groups in [inventory](../inventories/default/hosts.yml).
Add concrete `svc_<service>` membership only when that service is ready to deploy.
Inspect check mode before applying
the site playbook with an explicit host limit, using the [normal execution
workflow](../README.md#run). A Web host needs no application to converge.

## Configure nginx through service fragments

The [nginx component](../roles/components/nginx/) owns its package, service, root
configuration, fragment directory, logging policy, validation, and reload.
Its root configuration imports `/etc/nginx/conf.d/*.conf`; `sites-enabled` is
not imported. The distribution's default site is therefore inactive. The package
is installed without automatically starting that site. An empty fragment
directory is valid and nginx can start without an HTTP listener.

Each service owns a uniquely named `.conf` file in `nginx_fragment_dir`. It owns
the server names, listen addresses, ports, static paths, and upstream routes in
that file. It must not replace `nginx.conf` or manage other services' fragments.

Include `components/nginx` with `public: true` before rendering a fragment to
declare the middleware dependency, including on hosts outside `web_hosts`.
Notify `nginx configuration changed` after creating, updating, or deleting a
fragment. Flush pending handlers before checking the application's HTTP behavior.
The handler validates the complete configuration with `nginx -t` before reloading;
a validation failure stops the reload. Base configuration replacement also
validates the candidate before installation. Correct an invalid fragment and
reapply before expecting convergence.

For service retirement, declare only its owned fragment and application resources
for removal through [resource cleanup](../roles/resource_cleanup/). Its generic
path removal does not reload nginx. A service-specific retirement task must load
the nginx handlers and notify `nginx configuration changed` when removing the
fragment, or explicitly validate and reload after generic cleanup. The remaining
services keep their fragments. Removing inventory membership alone does not
remove deployed files or stop processes. Do not delete the shared nginx
configuration or purge nginx while another service still depends on it.

nginx presence does not imply Internet exposure. Select the service's listener
and ingress independently from workload classification and network placement.
Cloudflare Tunnel, Access, DNS, and TLS requirements have separate owners. The
Web host baseline installs no ACME client or public-certificate automation.
Add origin TLS explicitly if the connection requires it.

## Add an application service

Implement the application in `roles/services/<service>`, add its concrete
`svc_<service>` inventory group, and select that group in a service playbook
imported by [service convergence](../playbooks/components/services.yml). The
service composes only the middleware and runtime it needs. Validate required
inputs before mutation and keep fresh-host check-mode prerequisites explicit.

Prefer a native package or binary, then an isolated language runtime, then an
OCI container. These are defaults, not prohibitions; use another choice for a
specific technical reason. Container runtime installation must follow explicit
service intent. Do not add Docker or Podman as an application-host baseline.

Python applications use a service-local environment, such as
`/opt/<service>/venv`, instead of installing packages into system Python.
Native applications can use `/opt/<service>/releases/<version>` with a `current`
symlink for release selection. Production processes run from installed releases,
not a repository checkout. PHP applications use PHP-FPM behind nginx; the Web
baseline installs neither PHP-FPM nor a PHP application.

Use systemd for long-running processes. Each service owns its unit, restart
policy, working directory, configuration, and required environment. Run the
application as a dedicated unprivileged system user; package-provided middleware
accounts are acceptable. Send stdout and stderr to journald. nginx uses its
standard access and error logs with distribution-managed rotation.

HTTP application backends normally bind to `127.0.0.1:<port>`. Use a service-owned
Unix socket under `/run/<service>` when natural for the middleware, such as
PHP-FPM. When a frontend on another host must reach a backend, explicitly select
a private bind address and permitted network path; its loopback listener cannot
be reached remotely. Do not default to a wildcard application bind.

Use the following filesystem conventions unless an upstream package provides a
standard layout that should be preserved:

| Path | Ownership and purpose |
| --- | --- |
| `/etc/<service>` | Service configuration |
| `/opt/<service>` | Executables and application releases |
| `/var/lib/<service>` | Persistent mutable data |
| `/var/cache/<service>` | Disposable cache |
| `/run/<service>` | Runtime sockets, PID files, and transient state |
| `/srv/www/<service>` | Public or static Web content when applicable |

Resolve secrets through Provisioning's existing secret provider mechanism and
declare service-owned variable-to-provider-name mappings. Keep secret-bearing
tasks under `no_log`. Do not store credential values in the repository or combine
unrelated application credentials into a host-wide bundle. Follow the existing
execution wrapper's volatile storage and cleanup contract.

## Placement and expansion

Place additional services on an existing suitable host when capacity and isolation
allow it. Add workload hosts for resource pressure, maintenance impact, security
or failure-domain isolation, incompatible runtimes, or special hardware.

The planned initial machines are `web01` in DMZ with provisional address
`10.10.30.21` and `app01` in INFRA with provisional address `10.10.21.21`. These
are planning inputs, not assigned addresses. Create neither active inventory
membership nor DNS records from them; verify addresses when the VMs are created.
The workload groups remain empty until then.

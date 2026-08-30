from ansible.errors import AnsibleFilterError


def mysql_router_bootstrap_candidates(mysql_hosts, hostvars):
    if not isinstance(mysql_hosts, list) or len(mysql_hosts) != 2:
        raise AnsibleFilterError(
            "Router bootstrap requires exactly two svc_mysql inventory hosts"
        )

    candidates = []
    for hostname in mysql_hosts:
        if hostname not in hostvars:
            raise AnsibleFilterError(
                f"Router bootstrap inventory host {hostname!r} does not exist"
            )
        fqdn = hostvars[hostname].get("network_primary_fqdn")
        if not isinstance(fqdn, str) or not fqdn:
            raise AnsibleFilterError(
                f"Router bootstrap inventory host {hostname!r} has no stable FQDN"
            )
        candidates.append(fqdn)

    if len(set(candidates)) != len(candidates):
        raise AnsibleFilterError("Router bootstrap candidate FQDNs must be unique")
    return candidates


def mysql_router_select_bootstrap_candidate(candidates, probe_results):
    if not isinstance(candidates, list) or not candidates:
        raise AnsibleFilterError("Router bootstrap candidates must be a non-empty list")
    if not isinstance(probe_results, list):
        raise AnsibleFilterError("Router bootstrap probe results must be a list")

    successful = {
        result.get("item")
        for result in probe_results
        if isinstance(result, dict) and result.get("rc") == 0
    }
    for candidate in candidates:
        if candidate in successful:
            return candidate

    raise AnsibleFilterError(
        "No stable svc_mysql ReplicaSet member accepted the Router bootstrap session"
    )


class FilterModule:
    def filters(self):
        return {
            "mysql_router_bootstrap_candidates": mysql_router_bootstrap_candidates,
            "mysql_router_select_bootstrap_candidate": (
                mysql_router_select_bootstrap_candidate
            ),
        }

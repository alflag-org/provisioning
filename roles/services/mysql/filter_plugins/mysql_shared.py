import ipaddress

from ansible.errors import AnsibleFilterError


def _required_text(value, source):
    if not isinstance(value, str) or not value:
        raise AnsibleFilterError(f"{source} must be a non-empty string")
    return value


def _host_address(hostname, hostvars):
    _required_text(hostname, "inventory hostname")
    if hostname not in hostvars:
        raise AnsibleFilterError(f"inventory host {hostname!r} does not exist")

    variables = hostvars[hostname]
    address = variables.get("network_ipv4_address") or variables.get("ansible_host")
    try:
        parsed = ipaddress.ip_address(address)
    except (TypeError, ValueError) as error:
        raise AnsibleFilterError(
            f"inventory host {hostname!r} does not define a valid IP address"
        ) from error
    if parsed.version != 4:
        raise AnsibleFilterError(
            f"inventory host {hostname!r} must resolve to an IPv4 address"
        )
    return str(parsed)


def mysql_hosts_to_addresses(hostnames, hostvars):
    if not isinstance(hostnames, list) or not hostnames:
        raise AnsibleFilterError("hostnames must be a non-empty list")
    addresses = [_host_address(hostname, hostvars) for hostname in hostnames]
    return list(dict.fromkeys(addresses))


def mysql_validate_accounts(accounts):
    if not isinstance(accounts, list):
        raise AnsibleFilterError("MySQL accounts must be a list")
    seen = set()
    for index, account in enumerate(accounts):
        source = f"MySQL accounts[{index}]"
        if not isinstance(account, dict):
            raise AnsibleFilterError(f"{source} must be a mapping")
        name = _required_text(account.get("name"), f"{source}.name")
        host = _required_text(account.get("host"), f"{source}.host")
        key = (name, host)
        if key in seen:
            raise AnsibleFilterError(f"duplicate MySQL account {name!r}@{host!r}")
        seen.add(key)
    return accounts


def mysql_validate_databases(databases):
    if not isinstance(databases, list):
        raise AnsibleFilterError("MySQL databases must be a list")
    seen = set()
    for index, database in enumerate(databases):
        source = f"MySQL databases[{index}]"
        if not isinstance(database, dict):
            raise AnsibleFilterError(f"{source} must be a mapping")
        name = _required_text(database.get("name"), f"{source}.name")
        if name in seen:
            raise AnsibleFilterError(f"duplicate MySQL database {name!r}")
        seen.add(name)
    return databases


class FilterModule:
    def filters(self):
        return {
            "mysql_hosts_to_addresses": mysql_hosts_to_addresses,
            "mysql_validate_accounts": mysql_validate_accounts,
            "mysql_validate_databases": mysql_validate_databases,
        }

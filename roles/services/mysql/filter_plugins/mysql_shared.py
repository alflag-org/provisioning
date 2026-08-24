import ipaddress
import re

from ansible.errors import AnsibleFilterError


_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")


def _required_text(value, source):
    if not isinstance(value, str) or not value:
        raise AnsibleFilterError(f"{source} must be a non-empty string")
    return value


def _identifier(value, source):
    value = _required_text(value, source)
    if not _IDENTIFIER.fullmatch(value):
        raise AnsibleFilterError(
            f"{source} may contain only ASCII letters, numbers, and underscores"
        )
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


def mysql_expand_tenants(tenants, hostvars, privilege_profiles):
    if not isinstance(tenants, list):
        raise AnsibleFilterError("mysql_shared_tenants must be a list")
    if not isinstance(privilege_profiles, dict):
        raise AnsibleFilterError("mysql_shared_privilege_profiles must be a mapping")

    databases = []
    users = []
    required_secret_vars = []
    tenant_names = set()
    database_names = set()
    account_keys = set()

    for tenant_index, tenant in enumerate(tenants):
        source = f"mysql_shared_tenants[{tenant_index}]"
        if not isinstance(tenant, dict):
            raise AnsibleFilterError(f"{source} must be a mapping")

        tenant_name = _identifier(tenant.get("name"), f"{source}.name")
        database = _identifier(tenant.get("database"), f"{source}.database")
        if tenant_name in tenant_names:
            raise AnsibleFilterError(f"duplicate tenant name {tenant_name!r}")
        if database in database_names:
            raise AnsibleFilterError(f"duplicate tenant database {database!r}")
        tenant_names.add(tenant_name)
        database_names.add(database)
        databases.append({"name": database})

        tenant_users = tenant.get("users")
        if not isinstance(tenant_users, list) or not tenant_users:
            raise AnsibleFilterError(f"{source}.users must be a non-empty list")

        for user_index, user in enumerate(tenant_users):
            user_source = f"{source}.users[{user_index}]"
            if not isinstance(user, dict):
                raise AnsibleFilterError(f"{user_source} must be a mapping")
            username = _identifier(user.get("name"), f"{user_source}.name")
            password_var = _identifier(
                user.get("password_var"), f"{user_source}.password_var"
            )
            clients = user.get("clients")
            if not isinstance(clients, list) or not clients:
                raise AnsibleFilterError(f"{user_source}.clients must be a non-empty list")

            requested_profiles = user.get("privileges")
            if not isinstance(requested_profiles, list) or not requested_profiles:
                raise AnsibleFilterError(
                    f"{user_source}.privileges must be a non-empty list"
                )
            privileges = []
            for profile_name in requested_profiles:
                if profile_name not in privilege_profiles:
                    raise AnsibleFilterError(
                        f"{user_source}.privileges references unknown profile {profile_name!r}"
                    )
                profile = privilege_profiles[profile_name]
                if not isinstance(profile, list) or not profile:
                    raise AnsibleFilterError(
                        f"mysql_shared_privilege_profiles[{profile_name!r}] must be a non-empty list"
                    )
                privileges.extend(_required_text(value, "MySQL privilege") for value in profile)
            privileges = list(dict.fromkeys(privileges))

            for client in clients:
                address = _host_address(client, hostvars)
                account_key = (username, address)
                if account_key in account_keys:
                    raise AnsibleFilterError(
                        f"duplicate tenant account {username!r}@{address!r}"
                    )
                account_keys.add(account_key)
                users.append(
                    {
                        "name": username,
                        "host": address,
                        "password_var": password_var,
                        "priv": f"{database}.*:{','.join(privileges)}",
                        "tls_requires": {"SSL": True},
                    }
                )
            required_secret_vars.append(password_var)

    return {
        "databases": databases,
        "users": users,
        "required_secret_vars": list(dict.fromkeys(required_secret_vars)),
    }


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
            "mysql_expand_tenants": mysql_expand_tenants,
            "mysql_validate_accounts": mysql_validate_accounts,
            "mysql_validate_databases": mysql_validate_databases,
        }

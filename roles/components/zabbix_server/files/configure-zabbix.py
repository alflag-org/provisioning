#!/usr/bin/python3
import argparse
import json
import os
from pathlib import Path
import sys
import urllib.request


MYSQL_ITEMS = [
    ("MySQL platform status", "mysql.platform.status[json]", 4, "1m"),
    ("MySQL replication state is healthy", "mysql.platform.status[replication_ok]", 3, "1m"),
    ("MySQL node owns scheduled backup", "mysql.platform.status[backup_required]", 3, "1m"),
    ("MySQL role DNS matches local runtime state", "mysql.platform.status[dns_role_match]", 3, "1m"),
    ("MySQL GTID mode is enabled", "mysql.platform.status[gtid_enabled]", 3, "5m"),
    ("MySQL binary log format is ROW", "mysql.platform.status[row_binlog]", 3, "5m"),
    ("MySQL physical backup status", "mysql.backup.status[json]", 4, "5m"),
    ("MySQL physical backup age", "mysql.backup.status[age]", 0, "5m"),
    ("MySQL physical backup failure", "mysql.backup.status[failure]", 3, "5m"),
    ("MySQL restore test age", "mysql.backup.status[restore_age]", 0, "30m"),
    ("MySQL restore test failure", "mysql.backup.status[restore_failure]", 3, "5m"),
]

MYSQL_TRIGGERS = [
    ("MySQL replication or runtime role is unhealthy", "last(/{template}/mysql.platform.status[replication_ok])=0", 4),
    ("MySQL role DNS does not match runtime state", "last(/{template}/mysql.platform.status[dns_role_match])=0", 4),
    ("MySQL GTID mode is disabled", "last(/{template}/mysql.platform.status[gtid_enabled])=0", 4),
    ("MySQL binary log format is not ROW", "last(/{template}/mysql.platform.status[row_binlog])=0", 4),
    (
        "MySQL physical backup failed",
        "last(/{template}/mysql.platform.status[backup_required])=1 and "
        "last(/{template}/mysql.backup.status[failure])=1",
        4,
    ),
    (
        "MySQL physical backup is missing or older than 36 hours",
        "last(/{template}/mysql.platform.status[backup_required])=1 and "
        "(last(/{template}/mysql.backup.status[age])<0 or "
        "last(/{template}/mysql.backup.status[age])>129600)",
        4,
    ),
    (
        "MySQL restore test failed",
        "last(/{template}/mysql.platform.status[backup_required])=1 and "
        "last(/{template}/mysql.backup.status[restore_failure])=1",
        4,
    ),
    (
        "MySQL restore test is missing or older than 8 days",
        "last(/{template}/mysql.platform.status[backup_required])=1 and "
        "(last(/{template}/mysql.backup.status[restore_age])<0 or "
        "last(/{template}/mysql.backup.status[restore_age])>691200)",
        3,
    ),
]

ROUTER_ITEMS = [
    ("MySQL Router status", "mysql.router.status[json]", 4, "1m"),
    ("MySQL Router service is active", "mysql.router.status[service]", 3, "1m"),
    ("MySQL Router metadata cache is healthy", "mysql.router.status[metadata_cache]", 3, "1m"),
    ("MySQL Router read-write endpoint is reachable", "mysql.router.status[rw]", 3, "1m"),
    ("MySQL Router read-only endpoint is reachable", "mysql.router.status[ro]", 3, "1m"),
    ("MySQL Router split endpoint is reachable", "mysql.router.status[split]", 3, "1m"),
]

ROUTER_TRIGGERS = [
    ("MySQL Router service is unavailable", "last(/{template}/mysql.router.status[service])=0", 4),
    ("MySQL Router metadata cache is unhealthy", "last(/{template}/mysql.router.status[metadata_cache])=0", 4),
    ("MySQL Router read-write endpoint is unavailable", "last(/{template}/mysql.router.status[rw])=0", 4),
    ("MySQL Router read-only endpoint is unavailable", "last(/{template}/mysql.router.status[ro])=0", 3),
    ("MySQL Router split endpoint is unavailable", "last(/{template}/mysql.router.status[split])=0", 3),
]


class ZabbixAPIError(RuntimeError):
    pass


class ZabbixAPI:
    def __init__(self, url, host_header):
        self.url = url
        self.host_header = host_header
        self.auth = None
        self.request_id = 0

    def call(self, method, params, *, authenticated=True):
        self.request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": self.request_id,
        }
        if authenticated:
            if not self.auth:
                raise ZabbixAPIError("authenticated API request has no session token")
        headers = {
            "Content-Type": "application/json-rpc",
            "Host": self.host_header,
        }
        if authenticated:
            headers["Authorization"] = f"Bearer {self.auth}"
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode(),
            headers=headers,
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            document = json.loads(response.read())
        if "error" in document:
            error = document["error"]
            raise ZabbixAPIError(
                f"{method} failed: {error.get('message', 'unknown API error')}: "
                f"{error.get('data', '')}"
            )
        return document["result"]

    def login(self, username, password):
        self.auth = self.call(
            "user.login",
            {"username": username, "password": password},
            authenticated=False,
        )

    def logout(self):
        if self.auth:
            try:
                self.call("user.logout", [])
            finally:
                self.auth = None


class Reconciler:
    def __init__(self, api, inventory):
        self.api = api
        self.inventory = inventory
        self.changed = False
        self.host_count = 0
        self.template_count = 0

    def ensure_group(self, method_prefix, name):
        rows = self.api.call(
            f"{method_prefix}.get",
            {"output": ["groupid", "name"], "filter": {"name": [name]}},
        )
        if rows:
            return rows[0]["groupid"]
        result = self.api.call(f"{method_prefix}.create", {"name": name})
        self.changed = True
        return result["groupids"][0]

    def find_template(self, name):
        rows = self.api.call(
            "template.get",
            {"output": ["templateid", "host", "name"], "filter": {"host": [name]}},
        )
        if not rows:
            rows = self.api.call(
                "template.get",
                {"output": ["templateid", "host", "name"], "filter": {"name": [name]}},
            )
        return rows[0] if rows else None

    def ensure_template(self, name, groupid, items, triggers):
        template = self.find_template(name)
        if not template:
            result = self.api.call(
                "template.create",
                {"host": name, "name": name, "groups": [{"groupid": groupid}]},
            )
            templateid = result["templateids"][0]
            self.changed = True
        else:
            templateid = template["templateid"]
        for item_name, key, value_type, delay in items:
            self.ensure_item(templateid, item_name, key, value_type, delay)
        for description, expression, priority in triggers:
            self.ensure_trigger(
                templateid,
                description,
                expression.format(template=name),
                priority,
            )
        self.template_count += 1
        return templateid

    def ensure_item(self, templateid, name, key, value_type, delay):
        rows = self.api.call(
            "item.get",
            {
                "output": ["itemid", "name", "key_", "type", "value_type", "delay"],
                "hostids": [templateid],
                "filter": {"key_": [key]},
            },
        )
        desired = {
            "name": name,
            "key_": key,
            "type": "0",
            "value_type": str(value_type),
            "delay": delay,
        }
        if not rows:
            self.api.call("item.create", {"hostid": templateid, **desired})
            self.changed = True
            return
        current = rows[0]
        if any(current[field] != value for field, value in desired.items()):
            self.api.call("item.update", {"itemid": current["itemid"], **desired})
            self.changed = True

    def ensure_trigger(self, templateid, description, expression, priority):
        rows = self.api.call(
            "trigger.get",
            {
                "output": ["triggerid", "description", "expression", "priority"],
                "templateids": [templateid],
                "filter": {"description": [description]},
            },
        )
        desired = {
            "description": description,
            "expression": expression,
            "priority": str(priority),
        }
        if not rows:
            self.api.call("trigger.create", desired)
            self.changed = True
            return
        current = rows[0]
        if any(current[field] != value for field, value in desired.items()):
            self.api.call("trigger.update", {"triggerid": current["triggerid"], **desired})
            self.changed = True

    def require_template(self, name):
        template = self.find_template(name)
        if not template:
            raise ZabbixAPIError(f"packaged Zabbix template is missing: {name}")
        return template["templateid"]

    def ensure_host(self, host, groupid, templateids):
        rows = self.api.call(
            "host.get",
            {
                "output": ["hostid", "host", "name", "status"],
                "filter": {"host": [host["name"]]},
                "selectHostGroups": ["groupid"],
                "selectParentTemplates": ["templateid"],
                "selectInterfaces": ["interfaceid", "ip", "dns", "useip", "port", "type", "main"],
                "selectMacros": ["hostmacroid", "macro", "value", "type"],
            },
        )
        if not rows:
            result = self.api.call(
                "host.create",
                {
                    "host": host["name"],
                    "name": host["name"],
                    "status": 0,
                    "groups": [{"groupid": groupid}],
                    "templates": [{"templateid": value} for value in templateids],
                    "interfaces": [self.interface(host)],
                    "macros": self.macros(host),
                },
            )
            if not result.get("hostids"):
                raise ZabbixAPIError(f"host.create returned no host id for {host['name']}")
            self.changed = True
            self.host_count += 1
            return

        current = rows[0]
        current_groups = {row["groupid"] for row in current["hostgroups"]}
        current_templates = {row["templateid"] for row in current["parentTemplates"]}
        desired_groups = current_groups | {groupid}
        desired_templates = current_templates | set(templateids)
        update = {
            "hostid": current["hostid"],
            "host": host["name"],
            "name": host["name"],
            "status": 0,
            "groups": [{"groupid": value} for value in sorted(desired_groups)],
            "templates": [{"templateid": value} for value in sorted(desired_templates)],
        }
        comparable = {
            "host": current["host"],
            "name": current["name"],
            "status": int(current["status"]),
            "groups": [{"groupid": value} for value in sorted(current_groups)],
            "templates": [{"templateid": value} for value in sorted(current_templates)],
        }
        if {key: value for key, value in update.items() if key != "hostid"} != comparable:
            self.api.call("host.update", update)
            self.changed = True
        self.ensure_interface(current["hostid"], current["interfaces"], host)
        self.ensure_macros(current["hostid"], current["macros"], self.macros(host))
        self.host_count += 1

    @staticmethod
    def interface(host):
        return {
            "type": 1,
            "main": 1,
            "useip": 1,
            "ip": host["address"],
            "dns": "",
            "port": str(host["port"]),
        }

    @staticmethod
    def macros(host):
        if not host["mysql"]:
            return []
        return [{"macro": "{$MYSQL.DSN}", "value": "mysql-local", "type": 0}]

    def ensure_macros(self, hostid, current, desired):
        by_name = {row["macro"]: row for row in current}
        for macro in desired:
            existing = by_name.get(macro["macro"])
            if not existing:
                self.api.call("usermacro.create", {"hostid": hostid, **macro})
                self.changed = True
            elif (
                existing.get("value") != macro["value"]
                or int(existing.get("type", 0)) != macro["type"]
            ):
                self.api.call(
                    "usermacro.update",
                    {"hostmacroid": existing["hostmacroid"], **macro},
                )
                self.changed = True

    def ensure_interface(self, hostid, interfaces, host):
        desired = self.interface(host)
        agent_interfaces = [row for row in interfaces if row["type"] == "1"]
        if not agent_interfaces:
            self.api.call("hostinterface.create", {"hostid": hostid, **desired})
            self.changed = True
            return
        current = agent_interfaces[0]
        if any(str(current[field]) != str(value) for field, value in desired.items()):
            self.api.call(
                "hostinterface.update",
                {"interfaceid": current["interfaceid"], **desired},
            )
            self.changed = True

    def run(self):
        groups = self.inventory
        host_groupid = self.ensure_group("hostgroup", groups["host_group"])
        template_groupid = self.ensure_group("templategroup", groups["template_group"])
        mysql_platform_id = self.ensure_template(
            groups["templates"]["mysql_platform"],
            template_groupid,
            MYSQL_ITEMS,
            MYSQL_TRIGGERS,
        )
        router_id = self.ensure_template(
            groups["templates"]["mysql_router"],
            template_groupid,
            ROUTER_ITEMS,
            ROUTER_TRIGGERS,
        )
        linux_id = self.require_template(groups["templates"]["linux"])
        mysql_id = self.require_template(groups["templates"]["mysql"])
        for host in groups["hosts"]:
            templateids = [linux_id]
            if host["mysql"]:
                templateids.extend([mysql_id, mysql_platform_id])
            if host["router"]:
                templateids.append(router_id)
            self.ensure_host(host, host_groupid, templateids)


def login_and_rotate(api, username, desired_password, bootstrap_password):
    try:
        api.login(username, desired_password)
        return False
    except ZabbixAPIError as desired_error:
        try:
            api.login(username, bootstrap_password)
        except ZabbixAPIError as bootstrap_error:
            raise ZabbixAPIError(
                "neither the configured nor one-time bootstrap API credential was accepted"
            ) from bootstrap_error
        users = api.call(
            "user.get",
            {"output": ["userid", "username"], "filter": {"username": [username]}},
        )
        if len(users) != 1:
            raise ZabbixAPIError(f"cannot identify API user {username!r}") from desired_error
        api.call(
            "user.update",
            {"userid": users[0]["userid"], "passwd": desired_password},
        )
        api.logout()
        api.login(username, desired_password)
        return True


def logout_preserving_exception(api):
    active_exception = sys.exc_info()[0] is not None
    try:
        api.logout()
    except Exception:
        if not active_exception:
            raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--host-header", required=True)
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--username", required=True)
    args = parser.parse_args()
    desired_password = os.environ.get("ZABBIX_API_PASSWORD", "")
    bootstrap_password = os.environ.get("ZABBIX_API_BOOTSTRAP_PASSWORD", "")
    if len(desired_password) < 12 or not bootstrap_password:
        raise RuntimeError("Zabbix API credentials were not supplied")

    api = ZabbixAPI(args.url, args.host_header)
    try:
        password_changed = login_and_rotate(
            api,
            args.username,
            desired_password,
            bootstrap_password,
        )
        inventory = json.loads(Path(args.inventory).read_text(encoding="utf-8"))
        reconciler = Reconciler(api, inventory)
        reconciler.run()
        print(
            json.dumps(
                {
                    "changed": password_changed or reconciler.changed,
                    "hosts": reconciler.host_count,
                    "templates": reconciler.template_count,
                },
                sort_keys=True,
            )
        )
    finally:
        logout_preserving_exception(api)


if __name__ == "__main__":
    main()

from pathlib import Path
import re
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "inventories/default"


class InventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inventory = yaml.safe_load((INVENTORY / "hosts.yml").read_text())
        cls.groups = cls.inventory["all"]["children"]["default"]["children"]

    def test_database_and_router_groups_have_exact_members(self):
        self.assertEqual(
            set(self.groups["svc_mysql"]["hosts"]),
            {"mysql-shared01", "mysql-shared02"},
        )
        self.assertEqual(
            set(self.groups["mysql_router_clients"]["hosts"]),
            {"web01", "workbench01", "control01"},
        )
        self.assertEqual(set(self.groups["svc_monitoring"]["hosts"]), {"monitor01"})

    def test_host_addresses_are_unique(self):
        addresses = [
            variables["ansible_host"]
            for zone in ("mgmt", "dmz")
            for variables in self.groups[zone]["hosts"].values()
        ]
        self.assertEqual(len(addresses), len(set(addresses)))
        self.assertEqual(addresses.count("10.10.10.222"), 1)

    def test_mysql_node_vars_contain_identity_not_runtime_role(self):
        for hostname, address, server_id in (
            ("mysql-shared01", "10.10.10.221", 221),
            ("mysql-shared02", "10.10.10.222", 222),
        ):
            variables = yaml.safe_load(
                (INVENTORY / "host_vars" / f"{hostname}.yml").read_text()
            )
            self.assertEqual(variables["network_ipv4_address"], address)
            self.assertEqual(variables["mysql_server_id"], server_id)
            for runtime_key in (
                "mysql_primary",
                "mysql_replica",
                "mysql_role",
                "mysql_backup_owner",
            ):
                self.assertNotIn(runtime_key, variables)

    def test_monitoring_alias_is_zabbix_only(self):
        monitor = yaml.safe_load(
            (INVENTORY / "host_vars/monitor01.yml").read_text()
        )
        self.assertEqual(monitor["network_service_aliases"], ["zabbix.access.internal"])

    def test_router_bootstrap_has_no_runtime_role_alias_input(self):
        router_vars = yaml.safe_load(
            (INVENTORY / "group_vars/mysql_router_clients.yml").read_text()
        )
        router_defaults = yaml.safe_load(
            (ROOT / "roles/components/mysql_router/defaults/main.yml").read_text()
        )
        self.assertNotIn("mysql_router_bootstrap_instance", router_vars)
        self.assertNotIn("mysql_router_bootstrap_instance", router_defaults)

    def test_inventory_and_playbooks_do_not_use_legacy_group_prefix(self):
        forbidden_prefix = "cap" + "_"
        for root in (ROOT / "inventories", ROOT / "playbooks"):
            for path in root.rglob("*"):
                if path.is_file():
                    self.assertIsNone(
                        re.search(
                            rf"\b{re.escape(forbidden_prefix)}[A-Za-z0-9_]*",
                            path.read_text(),
                        ),
                        path.as_posix(),
                    )

if __name__ == "__main__":
    unittest.main()

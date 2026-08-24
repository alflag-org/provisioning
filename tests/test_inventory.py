from pathlib import Path
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

if __name__ == "__main__":
    unittest.main()

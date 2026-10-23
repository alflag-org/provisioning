import unittest

from ansible.errors import AnsibleFilterError

from roles.services.mysql.filter_plugins.mysql_shared import (
    mysql_hosts_to_addresses,
    mysql_validate_accounts,
    mysql_validate_databases,
)


HOSTVARS = {
    "web01": {"network_ipv4_address": "10.10.30.21"},
    "control01": {"ansible_host": "10.10.20.62"},
}


class MySQLAccountTests(unittest.TestCase):
    def test_inventory_hosts_expand_to_unique_ipv4_addresses(self):
        self.assertEqual(
            mysql_hosts_to_addresses(["web01", "control01", "web01"], HOSTVARS),
            ["10.10.30.21", "10.10.20.62"],
        )

    def test_missing_inventory_address_fails_cleanly(self):
        with self.assertRaises(AnsibleFilterError):
            mysql_hosts_to_addresses(["web01"], {"web01": {}})

    def test_duplicate_platform_objects_fail_before_mutation(self):
        with self.assertRaises(AnsibleFilterError):
            mysql_validate_accounts(
                [
                    {"name": "app", "host": "10.10.30.21"},
                    {"name": "app", "host": "10.10.30.21"},
                ]
            )
        with self.assertRaises(AnsibleFilterError):
            mysql_validate_databases([{"name": "app"}, {"name": "app"}])


if __name__ == "__main__":
    unittest.main()

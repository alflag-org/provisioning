import unittest


from ansible.errors import AnsibleFilterError

from roles.services.mysql.filter_plugins.mysql_shared import (
    mysql_expand_tenants,
    mysql_hosts_to_addresses,
    mysql_validate_accounts,
    mysql_validate_databases,
)


HOSTVARS = {
    "web01": {"network_ipv4_address": "10.10.30.21"},
    "control01": {"ansible_host": "10.10.10.62"},
}


class MySQLTenantTests(unittest.TestCase):
    def test_inventory_hosts_expand_to_unique_ipv4_addresses(self):
        self.assertEqual(
            mysql_hosts_to_addresses(["web01", "control01", "web01"], HOSTVARS),
            ["10.10.30.21", "10.10.10.62"],
        )

    def test_tenant_expansion_separates_database_accounts_and_secrets(self):
        expanded = mysql_expand_tenants(
            [
                {
                    "name": "example",
                    "database": "example_app",
                    "users": [
                        {
                            "name": "example_writer",
                            "password_var": "example_writer_password",
                            "clients": ["web01"],
                            "privileges": ["application"],
                        }
                    ],
                }
            ],
            HOSTVARS,
            {"application": ["SELECT", "INSERT", "SELECT"]},
        )
        self.assertEqual(expanded["databases"], [{"name": "example_app"}])
        self.assertEqual(
            expanded["required_secret_vars"], ["example_writer_password"]
        )
        self.assertEqual(
            expanded["users"],
            [
                {
                    "name": "example_writer",
                    "host": "10.10.30.21",
                    "password_var": "example_writer_password",
                    "priv": "example_app.*:SELECT,INSERT",
                    "tls_requires": {"SSL": True},
                }
            ],
        )

    def test_unknown_privilege_profile_fails(self):
        with self.assertRaises(AnsibleFilterError):
            mysql_expand_tenants(
                [
                    {
                        "name": "example",
                        "database": "example_app",
                        "users": [
                            {
                                "name": "reader",
                                "password_var": "reader_password",
                                "clients": ["web01"],
                                "privileges": ["not_defined"],
                            }
                        ],
                    }
                ],
                HOSTVARS,
                {},
            )

    def test_tenant_password_variable_is_required(self):
        with self.assertRaises(AnsibleFilterError):
            mysql_expand_tenants(
                [
                    {
                        "name": "example",
                        "database": "example_app",
                        "users": [
                            {
                                "name": "example_app",
                                "clients": ["web01"],
                                "privileges": ["application"],
                            }
                        ],
                    }
                ],
                HOSTVARS,
                {"application": ["SELECT"]},
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

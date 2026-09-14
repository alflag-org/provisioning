import unittest


from ansible.errors import AnsibleFilterError

from roles.components.mysql_router.filter_plugins.mysql_router import (
    mysql_router_bootstrap_candidates,
    mysql_router_select_bootstrap_candidate,
)


class MySQLRouterBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.hosts = ["mysql-shared01", "mysql-shared02"]
        self.hostvars = {
            "mysql-shared01": {
                "network_primary_fqdn": "mysql-shared01.srv.alflag.internal"
            },
            "mysql-shared02": {
                "network_primary_fqdn": "mysql-shared02.srv.alflag.internal"
            },
        }
        self.candidates = mysql_router_bootstrap_candidates(
            self.hosts, self.hostvars
        )

    def test_first_available_candidate_is_selected_in_inventory_order(self):
        scenarios = (
            ([1, 0], "mysql-shared02.srv.alflag.internal"),
            ([0, 1], "mysql-shared01.srv.alflag.internal"),
            ([0, 0], "mysql-shared01.srv.alflag.internal"),
        )
        for return_codes, expected in scenarios:
            with self.subTest(return_codes=return_codes):
                results = [
                    {"item": candidate, "rc": return_code}
                    for candidate, return_code in zip(
                        self.candidates, return_codes, strict=True
                    )
                ]
                self.assertEqual(
                    mysql_router_select_bootstrap_candidate(
                        self.candidates, results
                    ),
                    expected,
                )

    def test_both_unavailable_fails_explicitly(self):
        with self.assertRaisesRegex(AnsibleFilterError, "No stable svc_mysql"):
            mysql_router_select_bootstrap_candidate(
                self.candidates,
                [
                    {"item": candidate, "rc": 1}
                    for candidate in self.candidates
                ],
            )

    def test_candidate_selection_does_not_use_runtime_roles(self):
        for primary in self.hosts:
            hostvars = {
                name: {
                    **variables,
                    "mysql_role": "PRIMARY" if name == primary else "SECONDARY",
                }
                for name, variables in self.hostvars.items()
            }
            self.assertEqual(
                mysql_router_bootstrap_candidates(self.hosts, hostvars),
                self.candidates,
            )

if __name__ == "__main__":
    unittest.main()

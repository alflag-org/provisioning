from pathlib import Path
import unittest

import yaml

from ansible.errors import AnsibleFilterError

from roles.components.mysql_router.filter_plugins.mysql_router import (
    mysql_router_bootstrap_candidates,
    mysql_router_select_bootstrap_candidate,
)


ROOT = Path(__file__).resolve().parents[1]
ROUTER_ROLE = ROOT / "roles/components/mysql_router"


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

    def test_candidates_are_stable_svc_mysql_identities(self):
        self.assertEqual(
            self.candidates,
            [
                "mysql-shared01.srv.alflag.internal",
                "mysql-shared02.srv.alflag.internal",
            ],
        )
        role_text = "\n".join(
            path.read_text()
            for path in (
                ROUTER_ROLE / "defaults/main.yml",
                ROUTER_ROLE / "tasks/main.yml",
                ROOT / "inventories/default/group_vars/mysql_router_clients.yml",
            )
        )
        self.assertNotIn("mysql_router_bootstrap_instance", role_text)
        self.assertNotIn("mysql-shared-primary.srv.alflag.internal", role_text)
        self.assertNotIn("mysql-shared-replica.srv.alflag.internal", role_text)

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

    def test_bootstrap_uses_a_dedicated_tls_account_and_session_probe(self):
        router_defaults = yaml.safe_load(
            (ROUTER_ROLE / "defaults/main.yml").read_text()
        )
        service_defaults = yaml.safe_load(
            (ROOT / "roles/services/mysql/defaults/main.yml").read_text()
        )
        self.assertEqual(
            router_defaults["mysql_router_bootstrap_user"],
            "mysql_router_bootstrap",
        )
        self.assertEqual(
            router_defaults["mysql_router_bootstrap_password_var"],
            "mysql_router_bootstrap_password",
        )
        self.assertEqual(
            service_defaults["mysql_router_bootstrap_password_var"],
            "mysql_router_bootstrap_password",
        )

        grants = "/".join(service_defaults["mysql_router_bootstrap_grants"])
        for forbidden in (
            "SHUTDOWN",
            "SUPER",
            "CLONE_ADMIN",
            "BACKUP_ADMIN",
            "SYSTEM_VARIABLES_ADMIN",
        ):
            self.assertNotIn(forbidden, grants)
        for required in (
            "CREATE USER",
            "mysql_innodb_cluster_metadata.*:SELECT,INSERT,UPDATE,DELETE,EXECUTE",
            "mysql.user:SELECT",
            "performance_schema.global_variables:SELECT",
        ):
            self.assertIn(required, grants)

        service_tasks = (ROOT / "roles/services/mysql/tasks/main.yml").read_text()
        self.assertIn("'tls_requires': {'SSL': true}", service_tasks)
        self.assertIn("groups['mysql_router_clients']", service_tasks)
        self.assertNotIn("mysql_replicaset_admin_allowed_clients", service_tasks)

        router_tasks = (ROUTER_ROLE / "tasks/main.yml").read_text()
        self.assertIn("--ssl-mode=REQUIRED", router_tasks)
        self.assertIn("mysql_innodb_cluster_metadata.instances", router_tasks)
        self.assertIn("mysql_router_selected_bootstrap_candidate", router_tasks)

    def test_replicaset_administrator_sources_are_database_nodes_only(self):
        service_tasks = (ROOT / "roles/services/mysql/tasks/main.yml").read_text()
        task_start = service_tasks.index(
            "Resolve ReplicaSet administrator source addresses from inventory"
        )
        task_end = service_tasks.index(
            "Build local ReplicaSet bootstrap accounts", task_start
        )
        source_task = service_tasks[task_start:task_end]
        self.assertIn("groups['svc_mysql']", source_task)
        self.assertNotIn("mysql_router_clients", source_task)
        self.assertIn(
            "Remove ReplicaSet administrator accounts from Router client sources",
            service_tasks,
        )
        self.assertIn(
            "difference(mysql_replicaset_admin_allowed_addresses)", service_tasks
        )


if __name__ == "__main__":
    unittest.main()

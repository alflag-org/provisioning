from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
OPERATIONS = ROOT / "playbooks/operations"


class MySQLOperationSafetyTests(unittest.TestCase):
    def test_failover_mutation_targets_only_the_confirmed_dynamic_group(self):
        plays = yaml.safe_load((OPERATIONS / "mysql-failover.yml").read_text())
        authorization = next(
            task
            for task in plays[0]["tasks"]
            if "ansible.builtin.add_host" in task
        )
        self.assertEqual(
            authorization["ansible.builtin.add_host"]["groups"],
            "mysql_failover_authorized_target",
        )
        self.assertIn("mysql_failover_authorized_target", plays[1]["hosts"])
        self.assertIn(
            "mysql_failover_authorized_target",
            plays[1]["roles"][0]["when"],
        )

    def test_primary_backup_override_has_a_command_side_target_guard(self):
        plays = yaml.safe_load((OPERATIONS / "mysql-backup.yml").read_text())
        operation = next(
            task
            for task in plays[0]["tasks"]
            if "ansible.builtin.command" in task
        )
        conditions = "\n".join(operation["when"])
        self.assertIn("not mysql_backup_allow_primary", conditions)
        self.assertIn("mysql_backup_target | length > 0", conditions)


if __name__ == "__main__":
    unittest.main()

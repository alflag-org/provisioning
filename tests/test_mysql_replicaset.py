from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "roles/components/mysql_replicaset/tasks/run.yml"
MYSQL_TASKS = ROOT / "roles/services/mysql/tasks/main.yml"


class MySQLReplicaSetTaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tasks = yaml.safe_load(TASKS.read_text())

    def test_shell_command_runs_in_check_mode_for_dry_run(self):
        command = next(task for task in self.tasks if "ansible.builtin.command" in task)
        self.assertFalse(command["check_mode"])

    def test_result_extraction_requires_exactly_one_command_output(self):
        guard = next(
            task
            for task in self.tasks
            if task.get("name") == "Require exactly one ReplicaSet operation result"
        )
        expression = guard["ansible.builtin.assert"]["that"][0]
        self.assertIn("length == 1", expression)

    def test_fresh_install_check_skips_shell_and_result_dependents(self):
        stats = [task for task in self.tasks if "ansible.builtin.stat" in task]
        self.assertEqual(
            {task["ansible.builtin.stat"]["path"] for task in stats},
            {"/usr/bin/mysqlsh", "{{ mysql_replicaset_script_path }}"},
        )
        command = next(task for task in self.tasks if "ansible.builtin.command" in task)
        condition = " ".join(command["when"])
        self.assertIn("not ansible_check_mode", condition)
        self.assertIn("mysql_replicaset_shell.stat.exists", condition)
        self.assertIn("mysql_replicaset_script.stat.exists", condition)
        for name in (
            "Require exactly one ReplicaSet operation result",
            "Extract the ReplicaSet operation result",
            "Require a complete online ReplicaSet for healthy topology actions",
            "Record current ReplicaSet roles",
            "Report the sanitized ReplicaSet state",
        ):
            task = next(task for task in self.tasks if task.get("name") == name)
            self.assertTrue(task.get("when"), name)
            condition = " ".join(task["when"])
            self.assertIn("mysql_replicaset_shell.stat.exists", condition)
            self.assertIn("mysql_replicaset_script.stat.exists", condition)

    def test_shell_probe_and_command_use_the_same_operation_host(self):
        stats = [task for task in self.tasks if "ansible.builtin.stat" in task]
        command = next(task for task in self.tasks if "ansible.builtin.command" in task)
        for stat in stats:
            self.assertTrue(stat["run_once"])
            self.assertEqual(stat["delegate_to"], command["delegate_to"])

    def test_check_mode_primary_delegates_are_lazy(self):
        tasks = yaml.safe_load(MYSQL_TASKS.read_text())
        delegates = [
            task["delegate_to"]
            for task in tasks
            if task.get("name") in (
                "Manage shared tenant databases on the current PRIMARY",
                "Manage shared MySQL accounts on the current PRIMARY",
            )
        ]
        self.assertEqual(len(delegates), 2)
        for delegate in delegates:
            self.assertIn("if ansible_check_mode", delegate)
            self.assertIn("mysql_replicaset_runtime_primary", delegate)
            self.assertNotIn("| ternary", delegate)


if __name__ == "__main__":
    unittest.main()

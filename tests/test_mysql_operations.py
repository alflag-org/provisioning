import fcntl
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
OPERATIONS = ROOT / "playbooks/operations"
BACKUP_IDLE_TASKS = ROOT / "roles/components/mysql_backup/tasks/assert_idle.yml"


class MySQLOperationSafetyTests(unittest.TestCase):
    def test_topology_operations_require_idle_backup_and_restore_work(self):
        for filename, play_index in (
            ("mysql-switchover.yml", 0),
            ("mysql-failover.yml", 1),
        ):
            plays = yaml.safe_load((OPERATIONS / filename).read_text())
            guard = next(
                task
                for task in plays[play_index]["pre_tasks"]
                if task.get("ansible.builtin.include_role", {}).get("tasks_from")
                == "assert_idle.yml"
            )
            self.assertEqual(
                guard["ansible.builtin.include_role"]["name"],
                "components/mysql_backup",
            )
            if filename == "mysql-switchover.yml":
                self.assertNotIn("run_once", guard)
            else:
                self.assertIn(
                    "mysql_failover_authorized_target",
                    guard["when"],
                )

    def test_backup_idle_guard_checks_service_and_shared_lock(self):
        tasks = yaml.safe_load(BACKUP_IDLE_TASKS.read_text())
        arguments = [
            task["ansible.builtin.command"]["argv"]
            for task in tasks
            if "ansible.builtin.command" in task
        ]
        self.assertIn(
            ["/usr/bin/systemctl", "is-active", "{{ mysql_backup_service_name }}"],
            arguments,
        )
        self.assertIn(
            [
                "/usr/bin/flock",
                "--nonblock",
                "{{ mysql_backup_lock_path }}",
                "/usr/bin/true",
            ],
            arguments,
        )
        gate = next(
            task
            for task in tasks
            if task.get("name") == "Require idle MySQL backup and restore operations"
        )
        conditions = "\n".join(gate["ansible.builtin.assert"]["that"])
        self.assertIn("mysql_backup_service_state.rc in [3, 4]", conditions)
        self.assertIn("mysql_backup_operation_lock_probe.rc", conditions)

    def test_backup_idle_probe_conflicts_with_the_python_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "mysql-physical-backup.lock"
            with lock_path.open("w", encoding="utf-8") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = subprocess.run(
                    [
                        "/usr/bin/flock",
                        "--nonblock",
                        str(lock_path),
                        "/usr/bin/true",
                    ],
                    check=False,
                )
                self.assertNotEqual(locked.returncode, 0)
                fcntl.flock(lock, fcntl.LOCK_UN)

            unlocked = subprocess.run(
                [
                    "/usr/bin/flock",
                    "--nonblock",
                    str(lock_path),
                    "/usr/bin/true",
                ],
                check=False,
            )
            self.assertEqual(unlocked.returncode, 0)

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

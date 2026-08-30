import fcntl
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest

import yaml

from roles.components.mysql_backup.filter_plugins.mysql_backup import (
    mysql_backup_service_is_idle,
)


ROOT = Path(__file__).resolve().parents[1]
OPERATIONS = ROOT / "playbooks/operations"
BACKUP_TASKS = ROOT / "roles/components/mysql_backup/tasks"
LOCK_UNIT = (
    ROOT
    / "roles/components/mysql_backup/templates"
    / "mysql-topology-operation-lock.service.j2"
)


def operation_block(filename, play_index):
    plays = yaml.safe_load((OPERATIONS / filename).read_text())
    return plays, plays[play_index]["tasks"][0]


class MySQLOperationSafetyTests(unittest.TestCase):
    def test_topology_operations_hold_and_release_the_shared_lock(self):
        for filename, play_index in (
            ("mysql-switchover.yml", 0),
            ("mysql-failover.yml", 1),
        ):
            with self.subTest(filename=filename):
                _, operation = operation_block(filename, play_index)
                included = [
                    task.get("ansible.builtin.include_role", {}).get("tasks_from")
                    for task in operation["block"]
                ]
                self.assertEqual(included[0], "acquire_topology_lock.yml")
                self.assertIn("validate.yml", included)
                self.assertEqual(
                    operation["always"][0]["ansible.builtin.include_role"][
                        "tasks_from"
                    ],
                    "release_topology_lock.yml",
                )

        switchover, _ = operation_block("mysql-switchover.yml", 0)
        self.assertEqual(switchover[0]["hosts"], "svc_mysql")

    def test_lock_acquisition_is_fail_closed_without_timer_mutation(self):
        tasks = yaml.safe_load(
            (BACKUP_TASKS / "acquire_topology_lock.yml").read_text()
        )
        names = [task["name"] for task in tasks]
        self.assertLess(
            names.index("Require a loaded and inactive physical backup service"),
            names.index("Start the topology operation lock holder"),
        )
        serialized = yaml.safe_dump(tasks)
        self.assertIn("mysql_backup_service_is_idle", serialized)
        self.assertIn("LoadState", serialized)
        self.assertIn("ActiveState", serialized)
        self.assertNotIn("is-active", serialized)
        self.assertNotIn("mysql_backup_timer", serialized)

    def test_lock_holder_has_a_failsafe_expiration(self):
        unit = LOCK_UNIT.read_text()
        self.assertIn(
            "ExecStart=/usr/bin/flock --nonblock --exclusive "
            "{{ mysql_backup_lock_path }} /bin/sh -c "
            "'/usr/bin/systemd-notify --ready; exec /usr/bin/sleep infinity'",
            unit,
        )
        self.assertIn("Type=notify", unit)
        self.assertIn(
            "RuntimeMaxSec={{ mysql_topology_lock_runtime_max_seconds }}", unit
        )
        defaults = yaml.safe_load(
            (ROOT / "roles/components/mysql_backup/defaults/main.yml").read_text()
        )
        self.assertGreaterEqual(defaults["mysql_topology_lock_runtime_max_seconds"], 3600)
        acquire_tasks = (
            BACKUP_TASKS / "acquire_topology_lock.yml"
        ).read_text()
        self.assertIn("mysql_topology_lock_runtime_max_seconds | int >= 3600", acquire_tasks)

    def test_backup_service_state_is_allowed_only_when_loaded_and_inactive(self):
        allowed = "LoadState=loaded\nActiveState=inactive\n"
        self.assertTrue(mysql_backup_service_is_idle(allowed, 0))

        for active_state in (
            "active",
            "activating",
            "reloading",
            "deactivating",
            "failed",
            "unknown",
        ):
            with self.subTest(active_state=active_state):
                output = f"LoadState=loaded\nActiveState={active_state}\n"
                self.assertFalse(mysql_backup_service_is_idle(output, 0))

        self.assertFalse(
            mysql_backup_service_is_idle(
                "LoadState=not-found\nActiveState=inactive\n", 0
            )
        )
        self.assertFalse(mysql_backup_service_is_idle(allowed, 1))

    def test_topology_operations_never_change_the_backup_timer(self):
        topology_tasks = "\n".join(
            (BACKUP_TASKS / filename).read_text()
            for filename in (
                "acquire_topology_lock.yml",
                "release_topology_lock.yml",
            )
        )
        self.assertNotIn("mysql_backup_timer", topology_tasks)
        self.assertNotIn("timer state", topology_tasks)

        scheduled_service = (
            ROOT
            / "roles/components/mysql_backup/templates"
            / "mysql-physical-backup.service.j2"
        ).read_text()
        self.assertIn("--skip-if-lock-busy", scheduled_service)

    def test_backup_or_restore_lock_blocks_topology_acquisition(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "mysql-physical-backup.lock"
            for operation in ("backup", "restore"):
                with self.subTest(operation=operation):
                    with lock_path.open("w", encoding="utf-8") as lock:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        topology = subprocess.run(
                            [
                                "/usr/bin/flock",
                                "--nonblock",
                                str(lock_path),
                                "/usr/bin/true",
                            ],
                            check=False,
                        )
                        self.assertNotEqual(topology.returncode, 0)
                        fcntl.flock(lock, fcntl.LOCK_UN)

    def test_topology_lock_blocks_backup_and_restore_until_release(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "mysql-physical-backup.lock"
            holder = subprocess.Popen(
                [
                    "/usr/bin/flock",
                    "--nonblock",
                    str(lock_path),
                    "/usr/bin/python3",
                    "-c",
                    "import time; print('ready', flush=True); time.sleep(30)",
                ],
                stdout=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                self.assertEqual(holder.stdout.readline().strip(), "ready")
                for operation in ("backup", "restore"):
                    with self.subTest(operation=operation):
                        with lock_path.open("w", encoding="utf-8") as lock:
                            with self.assertRaises(BlockingIOError):
                                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.killpg(holder.pid, signal.SIGTERM)
                holder.wait(timeout=5)
                holder.stdout.close()

            deadline = time.monotonic() + 2
            with lock_path.open("w", encoding="utf-8") as lock:
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.01)
                fcntl.flock(lock, fcntl.LOCK_UN)

    def test_failover_mutation_targets_only_the_confirmed_dynamic_group(self):
        plays, operation = operation_block("mysql-failover.yml", 1)
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
        replicaset = next(
            task
            for task in operation["block"]
            if task.get("ansible.builtin.include_role", {}).get("name")
            == "components/mysql_replicaset"
        )
        self.assertIsNotNone(replicaset)

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

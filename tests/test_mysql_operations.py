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
    mysql_backup_timer_snapshot,
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

    def test_lock_acquisition_is_fail_closed_and_follows_timer_suspension(self):
        tasks = yaml.safe_load(
            (BACKUP_TASKS / "acquire_topology_lock.yml").read_text()
        )
        names = [task["name"] for task in tasks]
        self.assertLess(
            names.index("Suspend the physical backup timer"),
            names.index("Start the topology operation lock holder"),
        )
        self.assertLess(
            names.index("Recheck the physical backup service after timer suspension"),
            names.index("Start the topology operation lock holder"),
        )
        serialized = yaml.safe_dump(tasks)
        self.assertIn("mysql_backup_service_is_idle", serialized)
        self.assertIn("LoadState", serialized)
        self.assertIn("ActiveState", serialized)
        self.assertNotIn("is-active", serialized)

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
        self.assertGreaterEqual(defaults["mysql_topology_lock_runtime_max_seconds"], 600)

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

    def test_timer_snapshot_preserves_enabled_and_active_state(self):
        enabled_active = mysql_backup_timer_snapshot(
            "LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n", 0
        )
        self.assertTrue(enabled_active["valid"])
        self.assertTrue(enabled_active["enabled"])
        self.assertTrue(enabled_active["active"])

        disabled_inactive = mysql_backup_timer_snapshot(
            "LoadState=loaded\nActiveState=inactive\nUnitFileState=disabled\n", 0
        )
        self.assertTrue(disabled_inactive["valid"])
        self.assertFalse(disabled_inactive["enabled"])
        self.assertFalse(disabled_inactive["active"])

        for output in (
            "LoadState=loaded\nActiveState=failed\nUnitFileState=enabled\n",
            "LoadState=not-found\nActiveState=inactive\nUnitFileState=disabled\n",
            "LoadState=loaded\nActiveState=inactive\nUnitFileState=masked\n",
        ):
            self.assertFalse(mysql_backup_timer_snapshot(output, 0)["valid"])

    def test_release_restores_the_saved_timer_state(self):
        tasks = yaml.safe_load(
            (BACKUP_TASKS / "release_topology_lock.yml").read_text()
        )
        restore = next(
            task
            for task in tasks
            if task["name"] == "Restore the physical backup timer state"
        )["ansible.builtin.systemd_service"]
        self.assertEqual(
            restore["enabled"], "{{ mysql_backup_timer_before_topology.enabled }}"
        )
        self.assertIn("mysql_backup_timer_before_topology.active", restore["state"])

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

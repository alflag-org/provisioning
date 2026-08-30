import contextlib
import fcntl
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BACKUP_PATH = ROOT / "roles/components/mysql_backup/files/mysql-physical-backup.py"
BACKUP_SPEC = importlib.util.spec_from_file_location("mysql_physical_backup", BACKUP_PATH)
BACKUP = importlib.util.module_from_spec(BACKUP_SPEC)
BACKUP_SPEC.loader.exec_module(BACKUP)
RESTORE_PATH = ROOT / "roles/components/mysql_backup/files/mysql-restore-test.py"
RESTORE_SPEC = importlib.util.spec_from_file_location("mysql_restore_test", RESTORE_PATH)
RESTORE = importlib.util.module_from_spec(RESTORE_SPEC)
RESTORE_SPEC.loader.exec_module(RESTORE)


class MySQLBackupSafetyTests(unittest.TestCase):
    @staticmethod
    def backup_config(root):
        return {
            "binlog_directory": str(root / "binlog"),
            "replicaset_name": "mysql-shared",
            "source_node": "mysql-shared02",
        }

    def test_binlog_archive_rejects_a_non_file_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "binlog"
            source.mkdir()
            (source / "mysql-bin.000001").write_bytes(b"closed binlog")
            repository = root / "repository"
            archive = (
                repository
                / "mysql-shared/binlog/mysql-shared02/server-uuid"
            )
            (archive / "mysql-bin.000001").mkdir(parents=True)
            responses = [
                [],
                [["mysql-bin.000001", "13"], ["mysql-bin.000002", "4"]],
                [["server-uuid:1"]],
            ]
            with mock.patch.object(BACKUP, "mysql", side_effect=responses):
                with self.assertRaisesRegex(RuntimeError, "not a regular file"):
                    BACKUP.archive_closed_binlogs(
                        self.backup_config(root),
                        repository,
                        "server-uuid",
                        "20260824T010000Z",
                    )

    def test_binlog_archive_rejects_a_symlinked_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "binlog"
            source.mkdir()
            real_binlog = source / "real-binlog"
            real_binlog.write_bytes(b"closed binlog")
            (source / "mysql-bin.000001").symlink_to(real_binlog)
            repository = root / "repository"
            responses = [
                [],
                [["mysql-bin.000001", "13"], ["mysql-bin.000002", "4"]],
                [["server-uuid:1"]],
            ]
            with mock.patch.object(BACKUP, "mysql", side_effect=responses):
                with self.assertRaisesRegex(RuntimeError, "symlinked source binlog"):
                    BACKUP.archive_closed_binlogs(
                        self.backup_config(root),
                        repository,
                        "server-uuid",
                        "20260824T010000Z",
                    )

    def test_restore_cleanup_refuses_a_socket_without_a_live_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory)
            pid_path = scratch / "mysqld.pid"
            socket_path = scratch / "mysqld.sock"
            pid_path.write_text("4294967294\n", encoding="utf-8")
            socket_path.touch()
            with self.assertRaisesRegex(RuntimeError, "without a live ownership pid"):
                RESTORE.stop_server(pid_path, socket_path, scratch)

    def test_scheduled_backup_skips_when_the_shared_lock_is_busy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "mysql-physical-backup.lock"
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps({"lock_file": str(lock_path)}), encoding="utf-8"
            )
            output = io.StringIO()

            with lock_path.open("w", encoding="utf-8") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with mock.patch.object(
                    sys,
                    "argv",
                    [
                        "mysql-physical-backup",
                        "--config",
                        str(config_path),
                        "--skip-if-lock-busy",
                    ],
                ):
                    with contextlib.redirect_stdout(output):
                        BACKUP.main()

            self.assertEqual(
                json.loads(output.getvalue()),
                {"changed": False, "reason": "shared lock busy"},
            )

    def test_explicit_backup_fails_when_the_shared_lock_is_busy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "mysql-physical-backup.lock"
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps({"lock_file": str(lock_path)}), encoding="utf-8"
            )

            with lock_path.open("w", encoding="utf-8") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with mock.patch.object(
                    sys,
                    "argv",
                    ["mysql-physical-backup", "--config", str(config_path)],
                ):
                    with self.assertRaisesRegex(RuntimeError, "topology operation"):
                        BACKUP.main()

    def test_restore_test_fails_when_the_shared_lock_is_busy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "mysql-physical-backup.lock"
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps({"lock_file": str(lock_path)}), encoding="utf-8"
            )

            with lock_path.open("w", encoding="utf-8") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with mock.patch.object(
                    sys,
                    "argv",
                    ["mysql-restore-test", "--config", str(config_path)],
                ):
                    with self.assertRaisesRegex(RuntimeError, "topology operation"):
                        RESTORE.main()


if __name__ == "__main__":
    unittest.main()

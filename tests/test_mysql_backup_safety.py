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

    @staticmethod
    def restore_config(root, transport="filesystem", destination=""):
        return {
            "staging_directory": str(root / "staging"),
            "restore_directory": str(root / "restore"),
            "status_file": str(root / "status.json"),
            "replicaset_name": "mysql-shared",
            "lock_file": str(root / "lock"),
            "mysql_datadir": str(root / "production-datadir"),
            "expected_databases": ["mysql"],
            "backup_destination": {
                "transport": transport,
                "target": destination,
            },
            "filesystem_types": ["nfs", "nfs4", "cifs", "fuse.sshfs"],
            "rclone_config_path": "/tmp/rclone.conf",
        }

    def test_binlog_archive_rejects_a_non_file_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "binlog"
            source.mkdir()
            (source / "mysql-bin.000001").mkdir()
            repository = root / "repository"
            responses = [
                [],
                [["mysql-bin.000001", "13"], ["mysql-bin.000002", "4"]],
                [["server-uuid:1"]],
            ]
            with mock.patch.object(BACKUP, "mysql", side_effect=responses):
                with self.assertRaisesRegex(RuntimeError, "closed source binlog disappeared"):
                    BACKUP.archive_closed_binlogs(
                        self.backup_config(root),
                        "filesystem",
                        repository,
                        "mysql-shared02",
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
                        "filesystem",
                        repository,
                        "mysql-shared02",
                        "server-uuid",
                        "20260824T010000Z",
            )

    def test_restore_backups_listing_ignores_incomplete_and_incoming(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = (
                root
                / "destination"
                / "physical"
                / "mysql-shared"
                / "mysql-shared01"
                / "server-uuid"
            )
            base.mkdir(parents=True)
            complete = base / "20260824T010000Z"
            incomplete = base / "20260824T020000Z"
            incoming = root / "destination" / ".incoming" / "20260824T030000Z"
            complete.mkdir()
            incomplete.mkdir()
            incoming.mkdir(parents=True)
            (complete / "xtrabackup_checkpoints").write_text("", encoding="utf-8")
            (complete / "provisioning-backup.json").write_text("{}", encoding="utf-8")
            (complete / "COMPLETED").write_text("", encoding="utf-8")
            (incomplete / "xtrabackup_checkpoints").write_text("", encoding="utf-8")
            (incoming / "xtrabackup_checkpoints").write_text("", encoding="utf-8")
            (incoming / "provisioning-backup.json").write_text("{}", encoding="utf-8")
            (incoming / "COMPLETED").write_text("", encoding="utf-8")

            candidates = RESTORE.transport_list_backups(
                {"replica": "mysql"},
                "filesystem",
                str(root / "destination"),
                "mysql-shared",
            )
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["run_id"], "20260824T010000Z")

    def test_rclone_transport_copy_directory_uses_configured_rclone(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            (source / "db").mkdir()
            calls = []

            def fake_run(argv, capture=False, check=True):
                calls.append(tuple(argv))
                return mock.Mock(returncode=0)

            with mock.patch.object(BACKUP, "run", side_effect=fake_run):
                BACKUP.transport_copy_directory(
                    {"rclone_config_path": "/tmp/rclone.conf"},
                    "rclone",
                    str(source),
                    "synology:vol/path",
                )
            self.assertIn(
                (
                    "/usr/bin/rclone",
                    "--config",
                    "/tmp/rclone.conf",
                    "copy",
                    str(source),
                    "synology:vol/path",
                    "--copy-links",
                    "--create-empty-src-dirs",
                ),
                calls,
            )

    def test_rsync_transport_copy_directory_command_is_ssh_aware(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            calls = []

            def fake_run(argv, capture=False, check=True):
                calls.append(tuple(argv))
                return mock.Mock(returncode=0)

            with mock.patch.object(BACKUP, "run", side_effect=fake_run):
                BACKUP.transport_copy_directory(
                    {"rclone_config_path": "/tmp/rclone.conf"},
                    "rsync",
                    str(source),
                    "backup@nas:/volume1/mysql",
                )
            self.assertIn(
                (
                    "/usr/bin/rsync",
                    "--archive",
                    "--hard-links",
                    "--numeric-ids",
                    "--sparse",
                    "--mkpath",
                    f"{source}/",
                    "backup@nas:/volume1/mysql/",
                ),
                calls,
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

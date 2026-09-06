import contextlib
import fcntl
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import sys
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
            "b2_bucket": "mysql-backups",
            "b2_prefix": "mysql-shared",
            "rclone_remote": "mysql-backup",
            "rclone_version": "1.75.1",
            "rclone_binary": "/usr/bin/rclone",
            "rclone_config_path": "/etc/mysql-backup/rclone.conf",
        }

    @staticmethod
    def restore_config(root):
        return {
            "staging_directory": str(root / "staging"),
            "restore_directory": str(root / "restore"),
            "status_file": str(root / "status.json"),
            "replicaset_name": "mysql-shared",
            "lock_file": str(root / "lock"),
            "mysql_datadir": str(root / "production-datadir"),
            "expected_databases": ["mysql"],
            "b2_bucket": "mysql-backups",
            "b2_prefix": "mysql-shared",
            "rclone_remote": "mysql-backup",
            "rclone_version": "1.75.1",
            "rclone_binary": "/usr/bin/rclone",
            "rclone_config_path": "/etc/mysql-backup/rclone.conf",
        }

    def test_binlog_archive_rejects_a_non_file_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "binlog"
            source.mkdir()
            (source / "mysql-bin.000001").mkdir()
            responses = [
                [],
                [["mysql-bin.000001", "13"], ["mysql-bin.000002", "4"]],
                [["server-uuid:1"]],
            ]
            with mock.patch.object(BACKUP, "mysql", side_effect=responses), mock.patch.object(
                BACKUP, "rclone_sha1_map", return_value={}
            ):
                with self.assertRaisesRegex(RuntimeError, "closed source binlog disappeared"):
                    BACKUP.archive_closed_binlogs(
                        self.backup_config(root),
                        "mysql-shared02",
                        "server-uuid",
                        "20260824T010000Z",
                    )

    def test_rclone_copy_does_not_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            calls = []

            def fake_run(argv, capture=False, check=True):
                calls.append(tuple(argv))
                return mock.Mock(returncode=0)

            with mock.patch.object(BACKUP, "run", side_effect=fake_run):
                BACKUP.rclone_copy_directory(
                    self.backup_config(Path(directory)), source, "mysql-backup:mysql-backups/mysql-shared"
                )
            self.assertIn("--create-empty-src-dirs", calls[0])
            self.assertNotIn("--copy-links", calls[0])

    def test_rclone_check_uses_checksums(self):
        calls = []

        def fake_run(argv, capture=False, check=True):
            calls.append(tuple(argv))
            return mock.Mock(returncode=0)

        with mock.patch.object(BACKUP, "run", side_effect=fake_run):
            BACKUP.rclone_check(self.backup_config(Path("/tmp")), "/tmp/source", "remote:target")
        self.assertIn("--one-way", calls[0])
        self.assertNotIn("--size-only", calls[0])

    def test_rclone_exists_requires_the_expected_object_name(self):
        config = self.backup_config(Path("/tmp"))
        responses = [
            mock.Mock(returncode=0, stdout="other.json\n"),
            mock.Mock(returncode=0, stdout="complete.json\n"),
        ]
        with mock.patch.object(BACKUP, "run", side_effect=responses):
            self.assertFalse(BACKUP.rclone_exists(config, "remote:bucket/run/complete.json"))
            self.assertTrue(BACKUP.rclone_exists(config, "remote:bucket/run/complete.json"))

    def test_binlog_archive_reuses_matching_objects_and_shared_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "binlog"
            source.mkdir()
            binlog = source / "mysql-bin.000001"
            binlog.write_bytes(b"closed binlog")
            config = self.backup_config(root)
            calls = []

            def capture_copy(config, staging, destination):
                calls.append((sorted(path.name for path in Path(staging).rglob("*") if path.is_file()), destination))

            responses = [
                [],
                [["mysql-bin.000001", "13"], ["mysql-bin.000002", "4"]],
                [["server-uuid:1"]],
            ]
            with mock.patch.object(BACKUP, "mysql", side_effect=responses), mock.patch.object(
                BACKUP, "rclone_sha1_map", return_value={"mysql-bin.000001": BACKUP.file_sha1(binlog)}
            ), mock.patch.object(BACKUP, "rclone_copy_directory", side_effect=capture_copy), mock.patch.object(
                BACKUP, "rclone_check"
            ):
                BACKUP.archive_closed_binlogs(config, "mysql-shared02", "server-uuid", "20260824T010000Z")

            self.assertEqual(calls[0][0], ["20260824T010000Z.json"])
            self.assertEqual(
                calls[0][1],
                "mysql-backup:mysql-backups/mysql-shared/binlog/mysql-shared02/server-uuid",
            )

    def test_primary_scheduled_backup_skips_before_b2_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "lock_file": str(root / "lock"),
                        "status_file": str(root / "status.json"),
                        "source_node": "mysql-shared01",
                        "b2_bucket": "mysql-backups",
                        "b2_prefix": "mysql-shared",
                        "rclone_remote": "mysql-backup",
                        "rclone_version": "1.75.1",
                        "rclone_binary": "/usr/bin/rclone",
                        "rclone_config_path": "/etc/mysql-backup/rclone.conf",
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(BACKUP, "role_state", return_value=("PRIMARY", "uuid", "gtid")), mock.patch.object(
                BACKUP, "rclone_preflight", side_effect=AssertionError("preflight must not run")
            ), mock.patch.object(BACKUP, "write_status"):
                with mock.patch.object(
                    sys,
                    "argv",
                    ["mysql-physical-backup", "--config", str(config_path)],
                ):
                    with mock.patch("builtins.print") as output:
                        BACKUP.main()
            output.assert_called_once()

    def test_completion_marker_is_uploaded_after_checksum_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staging"
            staging.mkdir()
            (staging / "xtrabackup_checkpoints").write_text("", encoding="utf-8")
            manifest = {"backup_run_id": "20260824T010000Z", "server_uuid": "server-uuid"}
            (staging / "provisioning-backup.json").write_text(json.dumps(manifest), encoding="utf-8")
            calls = []
            config = self.backup_config(root)

            with mock.patch.object(BACKUP, "rclone_exists", return_value=False), mock.patch.object(
                BACKUP, "rclone_copy_directory", side_effect=lambda *args: calls.append("copy")
            ), mock.patch.object(BACKUP, "rclone_check", side_effect=lambda *args: calls.append("check")), mock.patch.object(
                BACKUP, "rclone_read_text", return_value=json.dumps(manifest)
            ), mock.patch.object(
                BACKUP, "rclone_copy_text", side_effect=lambda *args: calls.append("complete")
            ), mock.patch.object(
                BACKUP, "is_complete_backup", return_value=True
            ):
                BACKUP.upload_backup(config, staging, "mysql-shared02", "server-uuid", "20260824T010000Z", 10)

            self.assertEqual(calls, ["copy", "check", "complete"])

    def test_preflight_checks_version_and_b2_access(self):
        calls = []

        def fake_run(argv, capture=False, check=True):
            calls.append(tuple(argv))
            return mock.Mock(returncode=0, stdout="rclone v1.75.1\n")

        with mock.patch.object(BACKUP, "run", side_effect=fake_run):
            BACKUP.rclone_preflight(self.backup_config(Path("/tmp")))
        self.assertEqual([call[3] for call in calls], ["version", "lsd", "lsf"])
        self.assertIn("mysql-backup:mysql-backups", calls[1])

    def test_restore_backup_id_ignores_incomplete_candidates(self):
        config = self.restore_config(Path("/tmp"))
        complete = {
            "run_id": "20260824T010000Z",
            "run_at": RESTORE.parse_run_id("20260824T010000Z"),
            "path": "mysql-backup:mysql-backups/mysql-shared/physical/node/uuid/20260824T010000Z",
        }
        with mock.patch.object(RESTORE, "list_backups", return_value=[complete]):
            self.assertEqual(
                RESTORE.resolve_backup(config, "20260824T010000Z"),
                complete["path"],
            )
        with mock.patch.object(RESTORE, "list_backups", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "not uniquely available"):
                RESTORE.resolve_backup(config, "20260824T020000Z")

    def test_scheduled_backup_skips_when_the_shared_lock_is_busy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "mysql-physical-backup.lock"
            config_path = root / "config.json"
            config_path.write_text(json.dumps({"lock_file": str(lock_path)}), encoding="utf-8")
            output = io.StringIO()
            with lock_path.open("w", encoding="utf-8") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with mock.patch.object(
                    sys,
                    "argv",
                    ["mysql-physical-backup", "--config", str(config_path), "--skip-if-lock-busy"],
                ):
                    with contextlib.redirect_stdout(output):
                        BACKUP.main()
            self.assertEqual(json.loads(output.getvalue()), {"changed": False, "reason": "shared lock busy"})

    def test_explicit_backup_fails_when_the_shared_lock_is_busy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "mysql-physical-backup.lock"
            config_path = root / "config.json"
            config_path.write_text(json.dumps({"lock_file": str(lock_path)}), encoding="utf-8")
            with lock_path.open("w", encoding="utf-8") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with mock.patch.object(sys, "argv", ["mysql-physical-backup", "--config", str(config_path)]):
                    with self.assertRaisesRegex(RuntimeError, "topology operation"):
                        BACKUP.main()

    def test_restore_test_fails_when_the_shared_lock_is_busy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "mysql-physical-backup.lock"
            config_path = root / "config.json"
            config_path.write_text(json.dumps({"lock_file": str(lock_path)}), encoding="utf-8")
            with lock_path.open("w", encoding="utf-8") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with mock.patch.object(sys, "argv", ["mysql-restore-test", "--config", str(config_path)]):
                    with self.assertRaisesRegex(RuntimeError, "topology operation"):
                        RESTORE.main()


if __name__ == "__main__":
    unittest.main()

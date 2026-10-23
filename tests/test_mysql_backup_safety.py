import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BACKUP_PATH = ROOT / "roles/components/mysql_backup/files/mysql-physical-backup.py"
sys.path.insert(0, str(BACKUP_PATH.parent))
import mysql_backup_storage as STORAGE

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
            "binlog_directory": str(root / "binlog"), "source_node": "node",
            "mysql_datadir": str(root / "production"), "repository_directory": str(root / "repository"),
            "staging_directory": str(root / "staging"), "restore_directory": str(root / "restore"),
        }

    @staticmethod
    def make_backup(root):
        config = MySQLBackupSafetyTests.backup_config(root)
        staging = root / "staging"
        staging.mkdir()
        for name in ("xtrabackup_info", "xtrabackup_checkpoints", "xtrabackup_binlog_info", "ibdata1"):
            (staging / name).write_text("synthetic backup contents")
        manifest = {"source_node": "node", "server_uuid": "uuid", "backup_run_id": "20261023T010000Z",
                    "files": STORAGE.backup_file_manifest(staging)}
        STORAGE.write_json_atomic(staging / "provisioning-backup.json", manifest)
        backup = STORAGE.store_backup(config, staging, "node", "uuid", "20261023T010000Z",
                                      BACKUP.directory_size(staging))
        return config, backup, manifest

    def test_store_and_select_only_complete_local_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, backup, manifest = self.make_backup(root)
            self.assertEqual(STORAGE.verify_backup(backup), manifest)
            self.assertEqual(STORAGE.resolve_backup(config, backup.name), backup)
            marker = backup / "complete.json"
            marker.unlink()
            self.assertEqual(STORAGE.list_backups(config), [])
            with self.assertRaisesRegex(RuntimeError, "no completed"):
                STORAGE.resolve_backup(config, None)

    def test_storage_refuses_overwriting_an_existing_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, backup, _ = self.make_backup(root)
            digest = STORAGE.file_sha256(backup / "complete.json")
            with self.assertRaises(FileExistsError):
                STORAGE.store_backup(config, root / "staging", "node", "uuid", backup.name,
                                     BACKUP.directory_size(root / "staging"))
            self.assertEqual(STORAGE.file_sha256(backup / "complete.json"), digest)

    def test_incomplete_copy_never_gets_a_completion_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, _, _ = self.make_backup(root)
            (root / "staging" / "ibdata1").write_text("changed after manifest generation")
            with self.assertRaisesRegex(RuntimeError, "checksum or size"):
                STORAGE.store_backup(config, root / "staging", "node", "uuid", "20261023T020000Z", 1)
            self.assertFalse((root / "repository/physical/node/uuid/20261023T020000Z/complete.json").exists())

    def test_corrupt_data_and_metadata_are_refused_before_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            config, backup, _ = self.make_backup(Path(directory))
            marker = json.loads((backup / "complete.json").read_text())
            (backup / "complete.json").write_text(json.dumps(marker | {"source_node": "other"}))
            self.assertEqual(STORAGE.list_backups(config), [])
            with self.assertRaisesRegex(RuntimeError, "identity"):
                STORAGE.verify_backup(backup)
            (backup / "complete.json").write_text(json.dumps(marker))
            (backup / "ibdata1").write_text("corrupt")
            with self.assertRaisesRegex(RuntimeError, "checksum or size"):
                STORAGE.verify_backup(backup)
            (backup / "ibdata1").unlink()
            with self.assertRaisesRegex(RuntimeError, "file set"):
                STORAGE.verify_backup(backup)
            (backup / "ibdata1").symlink_to(backup / "xtrabackup_info")
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                STORAGE.verify_backup(backup)

    def test_storage_paths_refuse_production_overlap_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.backup_config(root)
            with self.assertRaisesRegex(RuntimeError, "must be separate"):
                STORAGE.validate_storage_paths(config | {"repository_directory": str(root / "production/backups")})
            (root / "production").mkdir()
            (root / "repository").symlink_to(root / "production", target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "unsafe storage"):
                STORAGE.validate_storage_paths(config)

    def test_binlog_archive_reuses_identical_files_and_refuses_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.backup_config(root)
            source = root / "binlog"
            source.mkdir()
            (source / "mysql-bin.000001").write_bytes(b"closed binlog")
            responses = [[], [["mysql-bin.000001", "13"], ["mysql-bin.000002", "4"]], [["uuid:1"]]]
            for run_id, expected in (("20261023T010000Z", "stored"), ("20261023T020000Z", "existing")):
                with mock.patch.object(BACKUP, "mysql", side_effect=responses):
                    path = BACKUP.archive_closed_binlogs(config, "node", "uuid", run_id)
                document = json.loads(Path(path).read_text())
                self.assertEqual(document["closed_binlogs"][0]["action"], expected)
                self.assertFalse((root / "repository/binlog/node/uuid/mysql-bin.000002").exists())
            archived = root / "repository/binlog/node/uuid/mysql-bin.000001"
            archived.write_bytes(b"conflicting archive")
            with mock.patch.object(BACKUP, "mysql", side_effect=responses):
                with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                    BACKUP.archive_closed_binlogs(config, "node", "uuid", "20261023T030000Z")
            self.assertEqual(archived.read_bytes(), b"conflicting archive")

    def test_staging_collision_preserves_the_existing_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.backup_config(root) | {
                "status_file": str(root / "status.json"), "lock_file": str(root / "lock"), "minimum_free_bytes": 0,
            }
            for name in ("repository", "staging", "production", "binlog"):
                (root / name).mkdir()
            instant = BACKUP.dt.datetime(2026, 10, 23, 1, tzinfo=BACKUP.UTC)
            class FrozenDateTime(BACKUP.dt.datetime):
                @classmethod
                def now(cls, tz=None):
                    return instant
            existing = root / "staging/20261023T010000Z"
            existing.mkdir()
            sentinel = existing / "must-remain"
            sentinel.write_text("existing staging data")
            path = root / "config.json"
            path.write_text(json.dumps(config))
            with mock.patch.object(BACKUP.dt, "datetime", FrozenDateTime), mock.patch.object(
                BACKUP, "role_state", return_value=("SECONDARY", "uuid", "gtid")
            ), mock.patch.object(BACKUP, "require_healthy_secondary", return_value=0), mock.patch.object(
                BACKUP, "write_status"
            ), mock.patch.object(sys, "argv", ["backup", "--config", str(path)]):
                with self.assertRaises(FileExistsError):
                    BACKUP.main()
            self.assertEqual(sentinel.read_text(), "existing staging data")

    def test_pitr_requires_contiguous_verified_logs_from_the_backup_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.backup_config(root)
            manifest = {"source_node": "node", "server_uuid": "uuid", "backup_run_id": "20261023T010000Z",
                        "xtrabackup_binlog_info": "mysql-bin.000001 4 aaaa:1"}
            archive = root / "repository/binlog/node/uuid"
            (archive / "manifests").mkdir(parents=True)
            entries = []
            for name in ("mysql-bin.000001", "mysql-bin.000002"):
                path = archive / name
                path.write_bytes(b"synthetic closed log")
                entries.append({"name": name, "size": path.stat().st_size, "sha256": STORAGE.file_sha256(path)})
            document = manifest | {"closed_binlogs": entries}
            path = archive / "manifests/20261023T010000Z.json"
            STORAGE.write_json_atomic(path, document)
            position, files = RESTORE.pitr_binlogs(config, manifest)
            self.assertEqual(position, 4)
            self.assertEqual([Path(item).name for item in files], [item["name"] for item in entries])
            (archive / "mysql-bin.000002").write_bytes(b"corrupt")
            with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                RESTORE.pitr_binlogs(config, manifest)
            STORAGE.write_json_atomic(path, document | {"source_node": "other"})
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                RESTORE.pitr_binlogs(config, manifest)
            STORAGE.write_json_atomic(path, document | {"closed_binlogs": [entries[0], entries[1] | {"name": "mysql-bin.000003"}]})
            with self.assertRaisesRegex(RuntimeError, "gap"):
                RESTORE.pitr_binlogs(config, manifest)

    def test_primary_backup_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.backup_config(root) | {
                "lock_file": str(root / "lock"), "status_file": str(root / "status.json"),
            }
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config))
            output = io.StringIO()
            with mock.patch.object(BACKUP, "role_state", return_value=("PRIMARY", "uuid", "gtid")), mock.patch.object(
                BACKUP, "write_status"
            ), mock.patch.object(BACKUP, "run") as run, mock.patch.object(
                sys, "argv", ["backup", "--config", str(config_path)]
            ), contextlib.redirect_stdout(output):
                BACKUP.main()
            self.assertFalse(json.loads(output.getvalue())["changed"])
            run.assert_not_called()

    def test_busy_lock_skips_scheduled_backup_and_rejects_explicit_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock"
            with BACKUP.acquire_operation_lock(path, False):
                self.assertIsNone(BACKUP.acquire_operation_lock(path, True))
                with self.assertRaises(RuntimeError):
                    BACKUP.acquire_operation_lock(path, False)
            with BACKUP.acquire_operation_lock(path, False) as lock:
                self.assertFalse(lock.closed)

    def test_replica_lag_and_thread_failures_refuse_backup(self):
        config = {"maximum_replication_lag_seconds": 30}
        healthy = {
            "Replica_IO_Running": "Yes", "Replica_SQL_Running": "Yes",
            "Last_IO_Errno": "0", "Last_SQL_Errno": "0", "Seconds_Behind_Source": "1",
        }
        for changes in ({}, {"Seconds_Behind_Source": "31"}, {"Seconds_Behind_Source": "NULL"},
                        {"Replica_SQL_Running": "No"}, {"Last_IO_Errno": "1236"}):
            replica = healthy | changes
            with self.subTest(changes=changes), mock.patch.object(
                BACKUP, "role_state", return_value=("SECONDARY", "uuid", "gtid")
            ), mock.patch.object(BACKUP, "mysql", return_value=[list(replica), list(replica.values())]):
                if changes:
                    with self.assertRaisesRegex(RuntimeError, "replication is unhealthy"):
                        BACKUP.require_healthy_secondary(config, "uuid")
                else:
                    self.assertEqual(BACKUP.require_healthy_secondary(config, "uuid"), 1)
        with mock.patch.object(BACKUP, "role_state", return_value=("PRIMARY", "uuid", "gtid")), mock.patch.object(
            BACKUP, "mysql", return_value=[]
        ):
            with self.assertRaisesRegex(RuntimeError, "no longer a healthy"):
                BACKUP.require_healthy_secondary(config, "uuid")

    def test_restore_cleanup_refuses_an_unrelated_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid = root / "mysqld.pid"
            pid.write_text(str(os.getpid()))
            with self.assertRaisesRegex(RuntimeError, "outside the isolated restore"):
                RESTORE.stop_server(pid, root / "mysqld.sock", root)


if __name__ == "__main__":
    unittest.main()

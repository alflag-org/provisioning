import contextlib
import fcntl
import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BACKUP_PROGRAM = ROOT / "roles/components/mysql_backup/files/mysql-physical-backup.py"
BACKUP_SPEC = importlib.util.spec_from_file_location(
    "mysql_physical_backup", BACKUP_PROGRAM
)
BACKUP = importlib.util.module_from_spec(BACKUP_SPEC)
BACKUP_SPEC.loader.exec_module(BACKUP)
RESTORE_PROGRAM = ROOT / "roles/components/mysql_backup/files/mysql-restore-test.py"
RESTORE_SPEC = importlib.util.spec_from_file_location(
    "mysql_restore_test", RESTORE_PROGRAM
)
RESTORE = importlib.util.module_from_spec(RESTORE_SPEC)
RESTORE_SPEC.loader.exec_module(RESTORE)


SERVER_UUID = "11111111-2222-3333-4444-555555555555"


def completed(stdout=""):
    return SimpleNamespace(stdout=stdout, returncode=0)


class MySQLBackupSafetyTests(unittest.TestCase):
    @staticmethod
    def backup_config(root):
        return {
            "b2_bucket": "mysql-backup-bucket",
            "b2_prefix": "mysql-shared",
            "binlog_directory": str(root / "binlog"),
            "rclone_binary": "/usr/local/bin/rclone",
            "rclone_config_file": "/etc/rclone/mysql-backup.conf",
            "rclone_remote": "mysql-backup",
            "source_node": "mysql-shared02",
            "source_nodes": ["mysql-shared01", "mysql-shared02"],
        }

    @staticmethod
    def marker(run_id, source_node="mysql-shared02", server_uuid=SERVER_UUID):
        return {
            "backup_run_id": run_id,
            "source_node": source_node,
            "server_uuid": server_uuid,
            "completed_at": "2026-08-31T01:00:00Z",
            "prepared": True,
            "backup_size": 1024,
        }

    def test_binlog_archive_skips_an_identical_remote_object(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "binlog"
            source.mkdir()
            binlog = source / "mysql-bin.000001"
            binlog.write_bytes(b"closed binlog")
            remote_document = {
                "Name": binlog.name,
                "Size": binlog.stat().st_size,
                "IsDir": False,
                "Hashes": {"sha1": hashlib.sha1(binlog.read_bytes()).hexdigest()},
            }
            responses = [
                [],
                [["mysql-bin.000001", "13"], ["mysql-bin.000002", "4"]],
                [["server-uuid:1"]],
            ]
            rclone = mock.Mock(return_value=completed())

            with mock.patch.object(BACKUP, "mysql", side_effect=responses):
                with mock.patch.object(
                    BACKUP, "remote_files", return_value=[remote_document]
                ):
                    with mock.patch.object(BACKUP, "rclone", rclone):
                        BACKUP.archive_closed_binlogs(
                            self.backup_config(root),
                            SERVER_UUID,
                            "20260831T010000Z",
                            root / "manifest.json",
                        )

            binlog_uploads = [
                call
                for call in rclone.call_args_list
                if "mysql-bin.000001" in " ".join(map(str, call.args))
            ]
            self.assertEqual(binlog_uploads, [])

    def test_binlog_archive_rejects_different_remote_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "binlog"
            source.mkdir()
            binlog = source / "mysql-bin.000001"
            binlog.write_bytes(b"closed binlog")
            remote_document = {
                "Name": binlog.name,
                "Size": binlog.stat().st_size,
                "IsDir": False,
                "Hashes": {"sha1": "0" * 40},
            }
            responses = [
                [],
                [["mysql-bin.000001", "13"], ["mysql-bin.000002", "4"]],
                [["server-uuid:1"]],
            ]

            with mock.patch.object(BACKUP, "mysql", side_effect=responses):
                with mock.patch.object(
                    BACKUP, "remote_files", return_value=[remote_document]
                ):
                    with self.assertRaisesRegex(RuntimeError, "differs"):
                        BACKUP.archive_closed_binlogs(
                            self.backup_config(root),
                            SERVER_UUID,
                            "20260831T010000Z",
                            root / "manifest.json",
                        )

    def test_binlog_archive_rejects_a_symlinked_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "binlog"
            source.mkdir()
            real_binlog = source / "real-binlog"
            real_binlog.write_bytes(b"closed binlog")
            (source / "mysql-bin.000001").symlink_to(real_binlog)
            responses = [
                [],
                [["mysql-bin.000001", "13"], ["mysql-bin.000002", "4"]],
                [["server-uuid:1"]],
            ]
            with mock.patch.object(BACKUP, "mysql", side_effect=responses):
                with mock.patch.object(BACKUP, "remote_files", return_value=[]):
                    with self.assertRaisesRegex(RuntimeError, "symlinked source binlog"):
                        BACKUP.archive_closed_binlogs(
                            self.backup_config(root),
                            SERVER_UUID,
                            "20260831T010000Z",
                            root / "manifest.json",
                        )

    def test_physical_staging_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory) / "staging"
            staging.mkdir()
            outside = Path(directory) / "outside"
            outside.write_bytes(b"outside")
            (staging / "linked-data").symlink_to(outside)
            with self.assertRaisesRegex(RuntimeError, "staging contains a symlink"):
                BACKUP.require_staging_tree_without_symlinks(staging)

    def test_physical_upload_writes_completion_marker_last(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staging"
            staging.mkdir()
            (staging / "xtrabackup_checkpoints").write_text(
                "backup_type = full-prepared\n", encoding="utf-8"
            )
            calls = []

            def fake_rclone(_config, *arguments, capture=False):
                calls.append(arguments)
                if arguments[0] == "lsf":
                    return completed("")
                if arguments[0] == "lsjson":
                    marker = staging / "complete.json"
                    document = {
                        "Name": marker.name,
                        "Size": marker.stat().st_size,
                        "IsDir": False,
                        "Hashes": {
                            "sha1": hashlib.sha1(marker.read_bytes()).hexdigest()
                        },
                    }
                    return completed(json.dumps(document))
                return completed()

            with mock.patch.object(BACKUP, "rclone", side_effect=fake_rclone):
                remote, marker = BACKUP.upload_prepared_backup(
                    self.backup_config(root),
                    staging,
                    SERVER_UUID,
                    "20260831T010000Z",
                    123,
                )

            self.assertTrue(remote.endswith(f"/{SERVER_UUID}/20260831T010000Z"))
            self.assertTrue(marker["prepared"])
            self.assertEqual(
                [call[0] for call in calls],
                ["lsf", "copy", "check", "copyto", "lsjson"],
            )
            self.assertEqual(calls[3][-1], f"{remote}/complete.json")

    def test_preflight_lists_the_bucket_without_requiring_an_existing_prefix(self):
        config = self.backup_config(Path("/tmp"))
        calls = []

        def fake_rclone(_config, *arguments, capture=False):
            calls.append(arguments)
            if arguments[0] == "lsjson":
                return completed(
                    json.dumps(
                        [
                            {
                                "Name": "mysql-backup-bucket",
                                "IsDir": True,
                            }
                        ]
                    )
                )
            return completed("")

        with mock.patch.object(BACKUP, "rclone", side_effect=fake_rclone):
            BACKUP.preflight_b2(config)

        self.assertEqual(calls[1][0], "lsf")
        self.assertIn("mysql-shared/**", calls[1])
        self.assertEqual(calls[1][-1], "mysql-backup:mysql-backup-bucket")

    def test_incomplete_remote_backup_is_not_selectable(self):
        config = self.backup_config(Path("/tmp"))
        listing = (
            "mysql-shared/physical/"
            f"mysql-shared02/{SERVER_UUID}/20260831T010000Z/ibdata1\n"
        )
        with mock.patch.object(RESTORE, "rclone", return_value=completed(listing)):
            with self.assertRaisesRegex(RuntimeError, "no completed B2"):
                RESTORE.select_backup(config)

    def test_latest_and_explicit_completed_backups_are_selectable(self):
        config = self.backup_config(Path("/tmp"))
        older = "20260830T010000Z"
        latest = "20260831T010000Z"
        listing = (
            f"mysql-shared/physical/mysql-shared01/{SERVER_UUID}/{older}/complete.json\n"
            f"mysql-shared/physical/mysql-shared02/{SERVER_UUID}/{latest}/complete.json\n"
        )

        def fake_rclone(_config, *arguments, capture=False):
            if arguments[0] == "lsf":
                return completed(listing)
            run_id = older if older in arguments[-1] else latest
            node = "mysql-shared01" if run_id == older else "mysql-shared02"
            return completed(json.dumps(self.marker(run_id, source_node=node)))

        with mock.patch.object(RESTORE, "rclone", side_effect=fake_rclone):
            self.assertEqual(RESTORE.select_backup(config)["backup_id"], latest)
            self.assertEqual(
                RESTORE.select_backup(config, older)["backup_id"], older
            )

    def test_missing_or_mismatched_completed_backup_fails(self):
        config = self.backup_config(Path("/tmp"))
        run_id = "20260831T010000Z"
        listing = (
            "mysql-shared/physical/"
            f"mysql-shared02/{SERVER_UUID}/{run_id}/complete.json\n"
        )

        with mock.patch.object(RESTORE, "rclone", return_value=completed(listing)):
            with self.assertRaisesRegex(RuntimeError, "no completed B2"):
                RESTORE.select_backup(config, "20260830T010000Z")

        responses = [completed(listing), completed(json.dumps(self.marker(run_id, source_node="mysql-shared01")))]
        with mock.patch.object(RESTORE, "rclone", side_effect=responses):
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                RESTORE.select_backup(config)

    def test_restore_download_uses_the_selected_b2_remote(self):
        config = self.backup_config(Path("/tmp"))
        candidate = {
            "backup_remote": (
                "mysql-backup:mysql-backup-bucket/mysql-shared/physical/"
                f"mysql-shared02/{SERVER_UUID}/20260831T010000Z"
            )
        }
        destination = Path("/var/lib/mysql-backup/restore-test/download")

        def fake_rclone(_config, *arguments, capture=False):
            if arguments[0] == "lsjson":
                return completed(
                    json.dumps(
                        [
                            {
                                "Path": "complete.json",
                                "IsDir": False,
                            }
                        ]
                    )
                )
            return completed()

        rclone = mock.Mock(side_effect=fake_rclone)

        with mock.patch.object(RESTORE, "rclone", rclone):
            RESTORE.download_backup(config, candidate, destination)

        self.assertEqual(rclone.call_args_list[0].args[1], "lsjson")
        self.assertEqual(rclone.call_args_list[1].args[1], "copy")
        self.assertEqual(rclone.call_args_list[1].args[-2], candidate["backup_remote"])
        self.assertEqual(rclone.call_args_list[1].args[-1], str(destination))
        self.assertEqual(rclone.call_args_list[2].args[1], "check")

    def test_restore_rejects_remote_path_traversal_before_download(self):
        config = self.backup_config(Path("/tmp"))
        candidate = {
            "backup_remote": (
                "mysql-backup:mysql-backup-bucket/mysql-shared/physical/"
                f"mysql-shared02/{SERVER_UUID}/20260831T010000Z"
            )
        }
        listing = json.dumps(
            [
                {"Path": "complete.json", "IsDir": False},
                {"Path": "../outside", "IsDir": False},
            ]
        )
        with mock.patch.object(RESTORE, "rclone", return_value=completed(listing)):
            with self.assertRaisesRegex(RuntimeError, "unsafe object path"):
                RESTORE.download_backup(config, candidate, Path("/safe/download"))

    def test_restore_integrity_requires_matching_prepared_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            download = Path(directory)
            run_id = "20260831T010000Z"
            marker = self.marker(run_id)
            candidate = {
                "backup_id": run_id,
                "source_node": "mysql-shared02",
                "server_uuid": SERVER_UUID,
                "completion_marker": marker,
            }
            (download / "complete.json").write_text(
                json.dumps(marker), encoding="utf-8"
            )
            metadata = {
                "backup_run_id": run_id,
                "source_node": "mysql-shared02",
                "server_uuid": SERVER_UUID,
                "prepared": True,
            }
            metadata_path = download / "provisioning-backup.json"
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            (download / "xtrabackup_checkpoints").write_text(
                "backup_type = full-prepared\n", encoding="utf-8"
            )
            (download / "ibdata1").write_bytes(b"physical-data")

            RESTORE.validate_download(download, candidate)
            metadata["server_uuid"] = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                RESTORE.validate_download(download, candidate)

    def test_b2_outage_records_backup_failure_before_heavy_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.backup_config(root)
            config.update(
                {
                    "lock_file": str(root / "backup.lock"),
                    "status_file": str(root / "status.json"),
                }
            )
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            written = []

            with mock.patch.object(
                BACKUP,
                "role_state",
                return_value=("SECONDARY", SERVER_UUID, "server-uuid:1"),
            ):
                with mock.patch.object(
                    BACKUP, "preflight_b2", side_effect=RuntimeError("B2 unavailable")
                ):
                    with mock.patch.object(
                        BACKUP,
                        "write_status",
                        side_effect=lambda _path, status: written.append(status.copy()),
                    ):
                        with mock.patch.object(
                            sys,
                            "argv",
                            ["mysql-physical-backup", "--config", str(config_path)],
                        ):
                            with self.assertRaisesRegex(RuntimeError, "B2 unavailable"):
                                BACKUP.main()

            self.assertIsNotNone(written[-1]["last_failure"])
            self.assertFalse(written[-1]["upload_success"])

    def test_restore_cleanup_refuses_a_socket_without_a_live_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            datadir = Path(directory)
            pid_path = datadir / "mysqld.pid"
            socket_path = datadir / "mysqld.sock"
            pid_path.write_text("4294967294\n", encoding="utf-8")
            socket_path.touch()
            with self.assertRaisesRegex(RuntimeError, "without a live ownership pid"):
                RESTORE.stop_server(pid_path, socket_path, datadir)

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

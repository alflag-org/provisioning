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
        for module in (BACKUP, RESTORE):
            for output, code, expected in (
                ("", 0, False), ("other.json\n", 0, False),
                (" complete.json\n", 0, False), ("complete.json/\n", 0, False),
                ("complete.json\n", 1, False), ("complete.json\n", 0, True),
            ):
                with self.subTest(module=module.__name__, output=output, code=code):
                    with mock.patch.object(module, "run", return_value=mock.Mock(returncode=code, stdout=output)):
                        self.assertEqual(module.rclone_exists(config, "remote:bucket/run/complete.json"), expected)

    def test_backup_selection_validates_completion_metadata(self):
        config = self.backup_config(Path("/tmp"))
        run_id = "20260824T010000Z"
        candidate = f"mysql-shared02/server-uuid/{run_id}"
        manifest = {"backup_run_id": run_id, "source_node": "mysql-shared02", "server_uuid": "server-uuid"}
        manifest_text = json.dumps(manifest)
        completion = manifest | {"manifest_sha256": BACKUP.text_sha256(manifest_text)}
        cases = [
            (manifest_text, json.dumps(completion), True),
            (manifest_text, "[]", False), ("null", json.dumps(completion), False),
            (manifest_text, "{", False), ("[", json.dumps(completion), False),
            (manifest_text, RuntimeError("missing marker"), False),
            (manifest_text, json.dumps(completion | {"manifest_sha256": "0" * 64}), False),
        ]
        for field in ("backup_run_id", "source_node", "server_uuid"):
            cases.append((manifest_text, json.dumps(completion | {field: "wrong"}), False))
            wrong_manifest = json.dumps(manifest | {field: "wrong"})
            cases.append((wrong_manifest, json.dumps(completion | {
                "manifest_sha256": BACKUP.text_sha256(wrong_manifest)
            }), False))
        for module in (BACKUP, RESTORE):
            for manifest_value, completion_value, expected in cases:
                with self.subTest(module=module.__name__, manifest=manifest_value, completion=completion_value):
                    with mock.patch.object(module, "rclone_list_dirs", return_value=[candidate]), mock.patch.object(
                        module, "rclone_exists", return_value=True
                    ), mock.patch.object(module, "rclone_read_text", side_effect=[manifest_value, completion_value]):
                        self.assertEqual(bool(module.list_backups(config)), expected)
            with mock.patch.object(module, "rclone_exists", return_value=False), mock.patch.object(
                module, "rclone_read_text"
            ) as read:
                self.assertFalse(module.is_complete_backup(config, "remote:bucket/" + candidate))
                read.assert_not_called()

    def test_binlog_hash_listing_rejects_unverifiable_objects(self):
        config = self.backup_config(Path("/tmp"))
        for output in (
            "mysql-bin.000001\t\n", "mysql-bin.000001\tERROR\n",
            "mysql-bin.000001\tUNSUPPORTED\n", "mysql-bin.000001\tinvalid\n",
            "mysql-bin.000001\n", ("mysql-bin.000001\t" + "a" * 40 + "\n") * 2,
        ):
            with self.subTest(output=output):
                with mock.patch.object(BACKUP, "run", return_value=mock.Mock(returncode=0, stdout=output)):
                    with self.assertRaises(RuntimeError):
                        BACKUP.rclone_sha1_map(config, "remote:bucket/binlog")

    def test_binlog_archive_uploads_new_objects_and_rejects_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "binlog"
            source.mkdir()
            (source / "mysql-bin.000001").write_bytes(b"closed binlog")
            config = self.backup_config(root)
            for remote_hashes in ({}, {"mysql-bin.000001": "0" * 40}):
                responses = [[], [["mysql-bin.000001", "13"], ["mysql-bin.000002", "4"]], [["uuid:1"]]]

                def capture_copy(config, staging, destination):
                    self.assertEqual((staging / "mysql-bin.000001").read_bytes(), b"closed binlog")
                    self.assertFalse((staging / "mysql-bin.000002").exists())
                    manifest = json.loads((staging / "manifests/20260824T010000Z.json").read_text())
                    self.assertEqual(manifest["closed_binlogs"][0]["action"], "uploaded")
                    self.assertEqual(manifest["gtid_executed"], "uuid:1")

                with self.subTest(remote_hashes=remote_hashes), mock.patch.object(
                    BACKUP, "mysql", side_effect=responses
                ), mock.patch.object(BACKUP, "rclone_sha1_map", return_value=remote_hashes), mock.patch.object(
                    BACKUP, "rclone_copy_directory", side_effect=capture_copy
                ) as copy, mock.patch.object(BACKUP, "rclone_check") as check:
                    if remote_hashes:
                        with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                            BACKUP.archive_closed_binlogs(config, "mysql-shared02", "server-uuid", "20260824T010000Z")
                        copy.assert_not_called()
                        check.assert_not_called()
                    else:
                        BACKUP.archive_closed_binlogs(config, "mysql-shared02", "server-uuid", "20260824T010000Z")
                        copy.assert_called_once()
                        check.assert_called_once()

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
            manifest = {
                "backup_run_id": "20260824T010000Z", "server_uuid": "server-uuid", "source_node": "mysql-shared02"
            }
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
        for module in (BACKUP, RESTORE):
            calls = []

            def fake_run(argv, capture=False, check=True):
                calls.append(tuple(argv))
                if argv[3] == "version":
                    return mock.Mock(returncode=0, stdout="rclone v1.75.1\n")
                if argv[4] == "mysql-backup:":
                    self.assertIn("--dirs-only", argv)
                    return mock.Mock(returncode=0, stdout="mysql-backups/\n")
                if argv[4] == "mysql-backup:mysql-backups/mysql-shared":
                    return mock.Mock(returncode=0, stdout="")
                raise AssertionError("listing files outside the allowed prefix")

            with self.subTest(module=module.__name__), mock.patch.object(module, "run", side_effect=fake_run):
                module.rclone_preflight(self.backup_config(Path("/tmp")))
            self.assertEqual([call[3] for call in calls], ["version", "lsf", "lsf"])

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

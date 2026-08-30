import datetime as dt
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "roles/components/mysql_backup/files/mysql-backup-status.py"
SPEC = importlib.util.spec_from_file_location("mysql_backup_status", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def status_document():
    return {
        "last_attempt": "2026-08-24T01:00:00Z",
        "last_success": "2026-08-24T01:00:00Z",
        "last_failure": "2026-08-23T01:00:00Z",
        "duration": 42.5,
        "backup_size": 1024,
        "backup_id": "20260824T010000Z",
        "backup_remote": "mysql-backup:mysql-backups/mysql-shared/physical/node/uuid/run",
        "source_node": "mysql-shared02",
        "source_role": "SECONDARY",
        "prepare_success": True,
        "upload_success": True,
        "restore_test_success": True,
        "restore_test_timestamp": "2026-08-24T02:00:00Z",
    }


class MySQLBackupStatusTests(unittest.TestCase):
    def test_status_parser_and_age_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            path.write_text(json.dumps(status_document()), encoding="utf-8")
            status = MODULE.read_status(path)
        now = dt.datetime(2026, 8, 24, 3, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(MODULE.metric(status, "age", now), 7200)
        self.assertEqual(MODULE.metric(status, "restore_age", now), 3600)
        self.assertEqual(MODULE.metric(status, "failure", now), 0)
        self.assertEqual(MODULE.metric(status, "restore_failure", now), 0)

    def test_missing_required_field_fails(self):
        document = status_document()
        del document["prepare_success"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(ValueError):
                MODULE.read_status(path)

    def test_new_status_reports_missing_ages_and_restore_failure(self):
        document = status_document()
        document.update(
            {
                "last_success": None,
                "restore_test_success": False,
                "restore_test_timestamp": None,
            }
        )
        self.assertEqual(MODULE.metric(document, "age"), -1)
        self.assertEqual(MODULE.metric(document, "restore_age"), -1)
        self.assertEqual(MODULE.metric(document, "restore_failure"), 1)


if __name__ == "__main__":
    unittest.main()

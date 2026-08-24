import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "roles/components/zabbix_mysql/files/mysql-platform-status.py"
SPEC = importlib.util.spec_from_file_location("mysql_platform_status", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def mysql_document(**overrides):
    document = {
        "hostname": "mysql-shared02",
        "read_only": 1,
        "super_read_only": 1,
        "gtid_executed": "uuid:1-42",
        "gtid_mode": "ON",
        "binlog_format": "ROW",
        "replica_receiver_state": "ON",
        "replica_applier_state": "ON",
    }
    document.update(overrides)
    return document


class MySQLPlatformStatusTests(unittest.TestCase):
    def read(self, document):
        query = SimpleNamespace(stdout=json.dumps(document), stderr="", returncode=0)

        def addresses(name, *_args, **_kwargs):
            address = "10.10.10.221" if "primary" in name else "10.10.10.222"
            return [(None, None, None, None, (address, 0))]

        with mock.patch.object(MODULE.subprocess, "run", return_value=query), mock.patch.object(
            MODULE.socket, "getaddrinfo", side_effect=addresses
        ):
            return MODULE.read_status(
                "/secret.cnf",
                "10.10.10.222",
                "mysql-shared-primary.srv.alflag.internal",
                "mysql-shared-replica.srv.alflag.internal",
            )

    def test_healthy_secondary_owns_backup_and_matches_dns(self):
        status = self.read(mysql_document())
        self.assertEqual(status["runtime_role"], "SECONDARY")
        self.assertTrue(status["replication_ok"])
        self.assertTrue(status["backup_required"])
        self.assertTrue(status["dns_role_match"])
        self.assertEqual(MODULE.metric(status, "backup_required"), 1)

    def test_broken_replica_is_unknown_and_does_not_run_backup(self):
        status = self.read(mysql_document(replica_applier_state="OFF"))
        self.assertEqual(status["runtime_role"], "UNKNOWN")
        self.assertFalse(status["replication_ok"])
        self.assertFalse(status["backup_required"])
        self.assertFalse(status["dns_role_match"])


if __name__ == "__main__":
    unittest.main()

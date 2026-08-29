import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from ansible.errors import AnsibleFilterError


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "roles/dns_authoritative/files/update-mysql-role-dns.py"
SPEC = importlib.util.spec_from_file_location("update_mysql_role_dns", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
SERIAL_MODULE_PATH = (
    ROOT / "roles/components/mysql_role_dns/filter_plugins/serial.py"
)
SERIAL_SPEC = importlib.util.spec_from_file_location(
    "mysql_role_dns_serial", SERIAL_MODULE_PATH
)
SERIAL_MODULE = importlib.util.module_from_spec(SERIAL_SPEC)
SERIAL_SPEC.loader.exec_module(SERIAL_MODULE)


class MySQLRoleDnsTests(unittest.TestCase):
    def test_role_records_use_absolute_targets_and_short_ttl(self):
        self.assertEqual(
            MODULE.role_records(
                "mysql-shared02.srv.alflag.internal",
                "mysql-shared01.srv.alflag.internal.",
                45,
            ),
            "; Managed from MySQL InnoDB ReplicaSet runtime status.\n"
            "mysql-shared-primary 45 IN CNAME mysql-shared02.srv.alflag.internal.\n"
            "mysql-shared-replica 45 IN CNAME mysql-shared01.srv.alflag.internal.\n",
        )

    def test_forced_failover_omits_an_unhealthy_replica_alias(self):
        records = MODULE.role_records("mysql-shared02.srv.alflag.internal", "", 45)
        self.assertIn("mysql-shared-primary", records)
        self.assertNotIn("mysql-shared-replica", records)

    def test_update_advances_serial_once_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = root / "roles.zone"
            zone = root / "srv.zone"
            applied = root / "applied.json"
            records.write_text("; initial\n", encoding="utf-8")
            zone.write_text(
                "@ IN SOA ns.example. hostmaster.example. (\n"
                "  9 ; serial\n"
                ")\n"
                f"$INCLUDE {records}\n",
                encoding="utf-8",
            )
            first = MODULE.update(
                "srv.example",
                zone,
                records,
                "mysql01.srv.example",
                "mysql02.srv.example",
                45,
                "/usr/bin/true",
                applied,
            )
            applied.write_text(
                json.dumps(
                    {
                        "records_sha256": first["records_sha256"],
                        "serial": first["serial"],
                    }
                ),
                encoding="utf-8",
            )
            second = MODULE.update(
                "srv.example",
                zone,
                records,
                "mysql01.srv.example",
                "mysql02.srv.example",
                45,
                "/usr/bin/true",
                applied,
            )
            self.assertTrue(first["changed"])
            self.assertTrue(first["reload_required"])
            self.assertEqual(first["serial"], 10)
            self.assertFalse(second["changed"])
            self.assertEqual(second["serial"], 10)
            self.assertFalse(second["reload_required"])

    def test_missing_applied_marker_requires_reloading_unchanged_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = root / "roles.zone"
            zone = root / "srv.zone"
            desired = MODULE.role_records("mysql01.srv.example", "", 45)
            records.write_text(desired, encoding="utf-8")
            zone.write_text(
                "@ IN SOA ns.example. hostmaster.example. (\n"
                "  12 ; serial\n"
                ")\n"
                f"$INCLUDE {records}\n",
                encoding="utf-8",
            )
            result = MODULE.update(
                "srv.example",
                zone,
                records,
                "mysql01.srv.example",
                "",
                45,
                "/usr/bin/true",
                root / "missing-applied.json",
            )
            self.assertFalse(result["changed"])
            self.assertTrue(result["reload_required"])

    def test_serial_wraps_in_rfc_1982_sequence_space(self):
        serial, updated = MODULE.advance_serial(
            "@ IN SOA ns.example. hostmaster.example. (\n"
            "  4294967295 ; serial\n"
            ")\n"
        )
        self.assertEqual(serial, 0)
        self.assertIn("  0 ; serial", updated)
        self.assertTrue(MODULE.serial_is_newer(4294967295, 0))
        self.assertFalse(MODULE.serial_is_newer(1, 0))

    def test_converged_serial_uses_rfc_1982_order_across_wrap(self):
        self.assertEqual(
            SERIAL_MODULE.mysql_role_dns_converged_serial([4294967295, 1]),
            2,
        )

    def test_converged_serial_only_advances_equal_values_for_new_records(self):
        self.assertEqual(
            SERIAL_MODULE.mysql_role_dns_converged_serial([20, 20], False),
            20,
        )
        self.assertEqual(
            SERIAL_MODULE.mysql_role_dns_converged_serial([20, 20], True),
            21,
        )

    def test_converged_serial_rejects_an_ambiguous_half_range(self):
        with self.assertRaisesRegex(AnsibleFilterError, "ambiguous"):
            SERIAL_MODULE.mysql_role_dns_converged_serial([0, 2147483648])


if __name__ == "__main__":
    unittest.main()

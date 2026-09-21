import unittest

from roles.components.mysql_backup.filter_plugins.mysql_backup import mysql_backup_service_is_idle


class MySQLServicePolicyTests(unittest.TestCase):
    def test_backup_service_state_is_allowed_only_when_loaded_and_inactive(self):
        for loaded, active, code, expected in (
            ("loaded", "inactive", 0, True),
            ("not-found", "inactive", 0, False),
            ("loaded", "inactive", 1, False),
            *(("loaded", state, 0, False) for state in (
                "active", "activating", "reloading", "deactivating", "failed", "unknown",
            )),
        ):
            with self.subTest(loaded=loaded, active=active, code=code):
                self.assertEqual(mysql_backup_service_is_idle(
                    f"LoadState={loaded}\nActiveState={active}\n", code,
                ), expected)

    def test_absent_backup_is_allowed_only_when_explicitly_disabled(self):
        for loaded, active, code, expected in (
            ("not-found", "inactive", 0, True),
            ("not-found", "inactive", 1, False),
            ("loaded", "active", 0, False),
            ("error", "inactive", 0, False),
            ("", "", 0, False),
        ):
            with self.subTest(loaded=loaded, active=active, code=code):
                self.assertEqual(mysql_backup_service_is_idle(
                    f"LoadState={loaded}\nActiveState={active}\n", code, allow_absent=True,
                ), expected)


if __name__ == "__main__":
    unittest.main()

from pathlib import Path
import importlib.util
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "roles/components/zabbix_server/files/configure-zabbix.py"
SPEC = importlib.util.spec_from_file_location("configure_zabbix", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ZabbixServerTests(unittest.TestCase):
    def test_bootstrap_rotation_logs_out_before_desired_login(self):
        class FakeAPI:
            def __init__(self):
                self.events = []
                self.auth = None
                self.desired_attempts = 0

            def login(self, username, password):
                self.events.append(("login", password))
                if password == "desired-password":
                    self.desired_attempts += 1
                    if self.desired_attempts == 1:
                        raise MODULE.ZabbixAPIError("desired credential rejected")
                self.auth = "bootstrap-token"

            def call(self, method, params):
                self.events.append((method, params))
                if method == "user.get":
                    return [{"userid": "1", "username": "zabbix"}]
                return None

            def logout(self):
                self.events.append(("logout",))
                self.auth = None

        api = FakeAPI()
        self.assertTrue(
            MODULE.login_and_rotate(
                api, "zabbix", "desired-password", "bootstrap-password"
            )
        )
        self.assertEqual(
            [event[0] for event in api.events],
            ["login", "login", "user.get", "user.update", "logout", "login"],
        )

    def test_logout_failure_does_not_replace_an_active_exception(self):
        class FailingAPI:
            def logout(self):
                raise ValueError("logout failed")

        with self.assertRaises(RuntimeError):
            try:
                raise RuntimeError("reconciliation failed")
            finally:
                MODULE.logout_preserving_exception(FailingAPI())

    def test_logout_failure_propagates_without_an_active_exception(self):
        class FailingAPI:
            def logout(self):
                raise ValueError("logout failed")

        with self.assertRaisesRegex(ValueError, "logout failed"):
            MODULE.logout_preserving_exception(FailingAPI())

    def test_logout_closes_an_authenticated_session(self):
        api = MODULE.ZabbixAPI("https://example.invalid/api", "zabbix.example.invalid")
        api.auth = "session-token"
        calls = []
        api.call = lambda method, params: calls.append((method, params))

        api.logout()

        self.assertEqual(calls, [("user.logout", [])])
        self.assertIsNone(api.auth)

    def test_schema_import_uses_one_transaction(self):
        task_file = ROOT / "roles/components/zabbix_server/tasks/database.yml"
        text = task_file.read_text()
        self.assertIn(
            "psql --single-transaction --set ON_ERROR_STOP=1 --file=-", text
        )


if __name__ == "__main__":
    unittest.main()

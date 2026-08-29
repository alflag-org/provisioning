import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "roles/components/mysql_router/files/mysql-router-status.py"
SPEC = importlib.util.spec_from_file_location("mysql_router_status", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class MySQLRouterStatusTests(unittest.TestCase):
    def test_router_config_reuses_bootstrap_client_tls_material(self):
        template = (
            ROOT / "roles/components/mysql_router/templates/mysqlrouter.conf.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("client_ssl_cert={{ mysql_router_client_ssl_cert_path }}", template)
        self.assertIn("client_ssl_key={{ mysql_router_client_ssl_key_path }}", template)

    def test_metadata_and_synthetic_endpoints_are_separate_health_signals(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            state.write_text(
                json.dumps(
                    {
                        "metadata-cache": {
                            "cluster-metadata-servers": ["mysql://mysql-shared01:3306"]
                        },
                        "version": "1.0.0",
                    }
                ),
                encoding="utf-8",
            )
            service = SimpleNamespace(stdout="active\n", stderr="", returncode=0)
            endpoint = {"reachable": True, "backend": {"hostname": "db", "read_only": 0}}
            with mock.patch.object(MODULE.subprocess, "run", return_value=service), mock.patch.object(
                MODULE, "query", return_value=endpoint
            ):
                result = MODULE.status(
                    "/secret.cnf",
                    state,
                    {"rw": 6446, "ro": 6447, "split": 6450},
                )
        self.assertTrue(result["service"])
        self.assertTrue(result["metadata_cache"])
        self.assertEqual(MODULE.metric(result, "rw"), 1)
        self.assertEqual(MODULE.metric(result, "ro"), 1)
        self.assertEqual(MODULE.metric(result, "split"), 1)

    def test_nonempty_unrelated_state_is_not_healthy_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            state.write_text('{"version":"1.0.0"}', encoding="utf-8")
            service = SimpleNamespace(stdout="active\n", stderr="", returncode=0)
            endpoint = {"reachable": False, "error": "unavailable"}
            with mock.patch.object(MODULE.subprocess, "run", return_value=service), mock.patch.object(
                MODULE, "query", return_value=endpoint
            ):
                result = MODULE.status(
                    "/secret.cnf",
                    state,
                    {"rw": 6446, "ro": 6447, "split": 6450},
                )
        self.assertFalse(result["metadata_cache"])

    def test_successful_metadata_route_does_not_require_private_state_access(self):
        service = SimpleNamespace(stdout="active\n", stderr="", returncode=0)
        endpoint = {"reachable": True, "backend": {"hostname": "db", "read_only": 0}}
        with mock.patch.object(MODULE.subprocess, "run", return_value=service), mock.patch.object(
            MODULE, "query", return_value=endpoint
        ):
            result = MODULE.status(
                "/secret.cnf",
                Path("/unreadable/bootstrap/state.json"),
                {"rw": 6446, "ro": 6447, "split": 6450},
            )
        self.assertTrue(result["metadata_cache"])


if __name__ == "__main__":
    unittest.main()

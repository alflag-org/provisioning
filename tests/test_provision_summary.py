"""Run real Ansible fixtures across the secret-safe summary boundary."""

import contextlib
import copy
import importlib.util
import io
import os
from pathlib import Path
import sys
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from atlas_core.secrets import SecretResolver

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("provision_summary_command", ROOT / "commands/provision.py")
COMMAND = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COMMAND)
from callback_plugins.provision_summary import CallbackModule
from lib.provision_summary import SummaryUnavailable, validate_summary

CANARY = "SECRET-CANARY-do-not-disclose-9f721"


class ProvisionSummaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.provider = SecretResolver({"fixture.secret": "id"}, lambda ids: {"id": CANARY})

    def execute(self, tasks, *, check=False, host="localhost", extra=""):
        playbook = self.directory / "fixture.yml"
        playbook.write_text(f"---\n- hosts: {host}\n  gather_facts: false\n  become: false\n"
                            f"{extra}  tasks:\n{tasks}")
        inventory = self.directory / "inventory.yml"
        inventory.write_text(f"all:\n  hosts:\n    {host}: {{}}\n")
        documents = []
        original = COMMAND.read_summary

        def inspect(path):
            self.assertEqual(path.parent.parent, Path("/dev/shm"))
            data = path.read_bytes()
            self.assertNotIn(CANARY.encode(), data)
            document = original(path)
            documents.append(document)
            return document

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"ATLAS_VENV": str(Path(sys.executable).parent.parent)}), \
                patch.object(COMMAND, "read_summary", inspect), contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            result = COMMAND.run(playbook, {"password": "fixture.secret"},
                                 ["-i", str(inventory), "-e", f"ansible_python_interpreter={sys.executable}", *(["-c", "local"] if host == "localhost" else []),
                                  *(["--check"] if check else [])],
                                 provider=self.provider)
        output = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(CANARY, output)
        self.assertEqual(len(documents), 1)
        return result, output, documents[0]

    def test_summary_reader_runs_without_ansible_or_site_packages(self):
        code = (
            f"import sys; sys.path.insert(0, {str(ROOT)!r}); "
            "from lib.provision_summary import read_summary, display_summary"
        )
        result = subprocess.run([sys.executable, "-S", "-c", code],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_atlas_discovers_the_provision_command(self):
        etc = self.directory / "etc"
        etc.mkdir()
        (etc / "host.yml").write_text("version: 1\nhost:\n  id: fixture\n")
        (etc / "config.yml").write_text(
            f"programs:\n  provisioning:\n    root: {ROOT}\n"
            "    runtime:\n      type: python\n      venv: provisioning\n")
        environment = {key: value for key, value in os.environ.items() if not key.startswith("ATLAS_")}
        environment.update(ATLAS_ETC_DIR=str(etc), ATLAS_HOME=str(self.directory / "home"),
                           ATLAS_VAR_DIR=str(self.directory / "var"))
        result = subprocess.run([sys.executable, "-m", "atlas.cli", "command", "list"],
                                env=environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("provision", result.stdout)

    def test_no_changes(self):
        code, output, summary = self.execute("    - ansible.builtin.debug:\n        msg: '{{ password }}'\n", check=True)
        self.assertEqual(code, 0)
        self.assertEqual(summary["totals"]["changed"], 0)
        self.assertIn("provision: no changes", output)
        self.assertEqual(summary["changed_events"], [])

    def test_changed_loop_and_no_log_only_report_locations(self):
        code, output, summary = self.execute("""    - name: '{{ password }}'
      ansible.builtin.debug:
        msg: '{{ password }}'
      loop: ['{{ password }}', '{{ password }}']
      changed_when: true
      no_log: true
""", check=True)
        self.assertEqual(code, 0)
        self.assertEqual(summary["totals"]["changed"], 1)
        self.assertIn("changed hosts: localhost", output)
        self.assertIn("fixture.yml:6 ansible.builtin.debug", output)
        self.assertEqual(len(summary["changed_events"]), 1)
        self.assertEqual(set(summary["changed_events"][0]), {"host", "action", "path", "line"})

    def test_module_stdout_stderr_and_failure_message_never_escape(self):
        code, output, summary = self.execute("""    - ansible.builtin.command:
        argv:
          - /bin/sh
          - -c
          - 'echo "$1"; echo "$1" >&2'
          - fixture
          - '{{ password }}'
    - ansible.builtin.fail:
        msg: '{{ password }}'
""")
        self.assertEqual(code, 2)
        self.assertEqual(summary["totals"]["changed"], 1)
        self.assertEqual(summary["totals"]["failed"], 1)
        self.assertIn("failed=1", output)

    def test_no_log_failure_is_counted(self):
        code, output, summary = self.execute("""    - ansible.builtin.fail:
        msg: '{{ password }}'
      no_log: true
""")
        self.assertEqual(code, 2)
        self.assertEqual(summary["hosts"]["localhost"]["failed"], 1)
        self.assertIn("failed=1", output)

    def test_unreachable_is_counted(self):
        with socket.socket() as blocked:
            blocked.bind(("127.0.0.1", 0))
            code, output, summary = self.execute("    - ansible.builtin.ping:\n", host="unreachable_fixture",
                                                 extra="  vars:\n    ansible_connection: ssh\n"
                                                 "    ansible_host: 127.0.0.1\n"
                                                 f"    ansible_port: {blocked.getsockname()[1]}\n")
        self.assertEqual(code, 4)
        self.assertEqual(summary["totals"]["unreachable"], 1)
        self.assertIn("unreachable=1", output)

    def test_environment_callback_is_not_loaded(self):
        plugins = self.directory / "untrusted_callbacks"
        plugins.mkdir()
        marker = self.directory / "loaded"
        adjacent = self.directory / "callback_plugins"
        adjacent.mkdir()
        (adjacent / "adjacent.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        (plugins / "untrusted.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        with patch.dict(os.environ, {"ANSIBLE_CALLBACK_PLUGINS": str(plugins),
                                     "ANSIBLE_CALLBACKS_ENABLED": "untrusted",
                                     "ANSIBLE_STDOUT_CALLBACK": "untrusted",
                                     "PROVISION_SUMMARY_PATH": str(self.directory / "outside.json")}):
            code, _, _ = self.execute("    - ansible.builtin.debug:\n        msg: '{{ password }}'\n")
        self.assertEqual(code, 0)
        self.assertFalse(marker.exists())
        self.assertFalse((self.directory / "outside.json").exists())

    def test_missing_corrupt_and_unexpected_summary_fail_closed(self):
        for content in (None, "{", '{"version":1,"msg":"' + CANARY + '"}'):
            with self.subTest(content=content):
                child = self.directory / "child.py"
                child.write_text("import os\nfrom pathlib import Path\n" + (
                    "" if content is None else
                    f"Path(os.environ['PROVISION_SUMMARY_PATH']).write_text({content!r})\n"
                    "Path(os.environ['PROVISION_SUMMARY_PATH']).chmod(0o600)\n"))
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    result = COMMAND.run(child, {"password": "fixture.secret"}, [],
                                         provider=self.provider, executable=sys.executable)
                self.assertNotEqual(result, 0)
                self.assertIn("safe execution summary is unavailable", stderr.getvalue())
                self.assertNotIn(CANARY, stdout.getvalue() + stderr.getvalue())

    def test_callback_does_not_access_result_payload_or_task_name(self):
        class StaticTask:
            action = "ansible.builtin.command"

            def get_path(self):
                return str(ROOT / "playbooks/site.yml") + ":3"

            def __getattr__(self, name):
                raise AssertionError("forbidden task access")

        class Host:
            def get_name(self):
                return "fixture"

        class Result:
            task = StaticTask()
            host = Host()

            def is_changed(self):
                return True

            def __getattr__(self, name):
                raise AssertionError("forbidden result access")

        callback = CallbackModule()
        callback.v2_runner_on_ok(Result())
        callback.v2_runner_on_failed(Result())
        self.assertFalse(callback._invalid)
        self.assertEqual(callback._events, {("fixture", "playbooks/site.yml", 3, "ansible.builtin.command")})

    def test_nonzero_exit_without_summary_preserves_status(self):
        child = self.directory / "failure.py"
        child.write_text("import sys\nsys.exit(7)\n")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = COMMAND.run(child, {"password": "fixture.secret"}, [],
                                 provider=self.provider, executable=sys.executable)
        self.assertEqual(result, 7)

    def test_summary_file_rejects_symlink_and_duplicate_keys(self):
        summary = self.directory / "summary.json"
        summary.write_text('{"version":1,"version":1}')
        summary.chmod(0o600)
        with self.assertRaises(SummaryUnavailable):
            COMMAND.read_summary(summary)
        link = self.directory / "link.json"
        link.symlink_to(summary)
        with self.assertRaises(SummaryUnavailable):
            COMMAND.read_summary(link)

    def test_schema_rejects_unknown_fields_types_and_inconsistent_totals(self):
        _, _, valid = self.execute("    - ansible.builtin.debug:\n        msg: '{{ password }}'\n", check=True)
        documents = []
        changed = copy.deepcopy(valid)
        changed["hosts"]["localhost"]["msg"] = CANARY
        documents.append(changed)
        changed = copy.deepcopy(valid)
        changed["totals"]["ok"] = True
        documents.append(changed)
        changed = copy.deepcopy(valid)
        changed["totals"]["ok"] += 1
        documents.append(changed)
        changed = copy.deepcopy(valid)
        changed["version"] = True
        documents.append(changed)
        for document in documents:
            with self.assertRaises(SummaryUnavailable):
                validate_summary(document)


if __name__ == "__main__":
    unittest.main()

"""Exercise the actual child boundary with synthetic secrets and no network."""

import importlib.util
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from atlas_core.secrets import SecretResolutionError, SecretResolver

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("provision_command", ROOT / "commands/provision.py")
COMMAND = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COMMAND)


class SecretExecutionTests(unittest.TestCase):
    def test_launches_ansible_from_atlas_program_venv(self):
        with tempfile.TemporaryDirectory() as directory:
            venv = Path(directory)
            (venv / "bin").mkdir()
            child = venv / "bin" / "ansible-playbook"
            marker = venv / "started"
            child.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\n")
            child.chmod(0o700)
            provider = SecretResolver({"mysql.backup.password": "id"}, lambda ids: {"id": "synthetic-secret"})
            with patch.dict(os.environ, {"ATLAS_VENV": str(venv)}):
                self.assertEqual(COMMAND.run(Path("unused"), {"password": "mysql.backup.password"}, [],
                                             provider=provider), 0)
            self.assertTrue(marker.exists())

    def test_real_child_gets_values_without_argv_environment_or_persistent_file(self):
        with tempfile.TemporaryDirectory() as directory:
            child = Path(directory) / "child.py"
            child.write_text('''import os, pathlib, sys, yaml
assert "synthetic-secret" not in repr(sys.argv)
assert "synthetic-secret" not in repr(dict(os.environ))
path = sys.argv[-1][1:]
assert path.startswith("/dev/shm/atlas-run-")
assert pathlib.Path(os.environ["ANSIBLE_LOCAL_TEMP"]).parent == pathlib.Path(path).parent
assert os.stat(pathlib.Path(path).parent).st_mode & 0o777 == 0o700
assert os.stat(path).st_mode & 0o777 == 0o600
assert yaml.load(pathlib.Path(path).read_text(), Loader=yaml.BaseLoader) == {"mysql_password": "synthetic-secret"}
print("synthetic-secret")
sys.stderr.write("synthetic-secret")
''')
            provider = SecretResolver({"mysql.backup.password": "id"}, lambda ids: {"id": "synthetic-secret"})
            self.assertEqual(COMMAND.run(child, {"mysql_password": "mysql.backup.password"}, [],
                                         provider=provider, executable=sys.executable), 0)
            self.assertEqual(list(Path(directory).iterdir()), [child])

    def test_missing_secret_never_starts_child(self):
        provider = SecretResolver({"mysql.backup.password": "id"}, lambda ids: {})
        with self.assertRaises(SecretResolutionError):
            COMMAND.run(Path("unused"), {"password": "mysql.backup.password"}, [],
                        provider=provider, executable="must-not-execute")

    def test_declarations_reject_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "required.yml"
            path.write_text("required_secrets:\n  password: mysql.backup.password\n  password: other.api_token\n")
            with self.assertRaises(ValueError):
                COMMAND.declarations(path)


if __name__ == "__main__":
    unittest.main()


class SecretSignalCleanupTests(unittest.TestCase):
    def test_sigterm_removes_volatile_vars(self):
        import signal
        self._assert_signal_cleanup(signal.SIGTERM)

    def test_sigint_removes_volatile_vars(self):
        import signal
        self._assert_signal_cleanup(signal.SIGINT)

    def _assert_signal_cleanup(self, signum):
        import subprocess
        import time

        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            marker = directory / "ready"
            child = directory / "child.py"
            child.write_text('''import os, pathlib, sys, time
pathlib.Path(os.environ["TEST_READY"]).write_text(sys.argv[-1][1:])
time.sleep(30)
''')
            parent = directory / "parent.py"
            parent.write_text(f'''import importlib.util, sys
from pathlib import Path
from atlas_core.secrets import SecretResolver
spec = importlib.util.spec_from_file_location("command", {str(ROOT / "commands/provision.py")!r})
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
p = SecretResolver({{"mysql.backup.password": "id"}}, lambda ids: {{"id": "synthetic"}})
m.run(Path({str(child)!r}), {{"password": "mysql.backup.password"}}, [], provider=p, executable=sys.executable)
''')
            env = dict(os.environ, TEST_READY=str(marker))
            process = subprocess.Popen([sys.executable, str(parent)], env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 10
                while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(marker.exists())
                secret_path = Path(marker.read_text())
                self.assertTrue(secret_path.exists())
                process.send_signal(signum)
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 128 + signum)
                self.assertFalse(secret_path.parent.exists())
                self.assertNotIn(b"synthetic", stdout + stderr)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()


class SpawnSignalTests(unittest.TestCase):
    def test_signal_during_spawn_is_handled_after_child_is_owned(self):
        import signal
        import subprocess
        from unittest.mock import patch

        original = subprocess.Popen
        children = []
        paths = []

        def spawn(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append(child)
            paths.append(Path(args[0][-1][1:]))
            os.kill(os.getpid(), signal.SIGTERM)
            return child

        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "child.py"
            script.write_text("import time\ntime.sleep(30)\n")
            provider = SecretResolver({"mysql.backup.password": "id"}, lambda ids: {"id": "synthetic"})
            with patch.object(COMMAND.subprocess, "Popen", spawn):
                with self.assertRaises(SystemExit) as error:
                    COMMAND.run(script, {"password": "mysql.backup.password"}, [],
                                provider=provider, executable=sys.executable)
            self.assertEqual(error.exception.code, 143)
            self.assertIsNotNone(children[0].poll())
            self.assertFalse(paths[0].parent.exists())


class LiteralSecretTests(unittest.TestCase):
    def test_ansible_does_not_evaluate_secret_as_jinja(self):
        import shutil

        executable = ROOT / ".venv/bin/ansible-playbook"
        if not executable.exists():
            installed = shutil.which("ansible-playbook")
            if installed is None:
                self.skipTest("Ansible is not installed")
            executable = Path(installed)
        with tempfile.TemporaryDirectory() as directory:
            playbook = Path(directory) / "literal.yml"
            playbook.write_text('''---
- hosts: localhost
  gather_facts: false
  become: false
  tasks:
    - name: Check that the secret remained literal
      ansible.builtin.assert:
        that: password.startswith('{')
      no_log: true
''')
            provider = SecretResolver({"mysql.backup.password": "id"},
                                      lambda ids: {"id": "{{ lookup('pipe', 'false') }}"})
            self.assertEqual(COMMAND.run(playbook, {"password": "mysql.backup.password"},
                                        ["-i", "localhost,", "-c", "local"], provider=provider,
                                        executable=str(executable)), 0)

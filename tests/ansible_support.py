"""Run Ansible with local fixtures and the same interpreter as the test suite."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def ansible_executable(name):
    return str(Path(sys.executable).parent / name)


class AnsibleTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def run_playbook(self, playbook, *, variables=None, check=False):
        inventory = self.directory / "inventory.yml"
        inventory.write_text("all:\n  children:\n    default:\n      hosts:\n        fixture: {}\n")
        variable_file = self.directory / "vars.json"
        variable_file.write_text(json.dumps({
            **(variables or {}),
            "ansible_connection": "local",
            "ansible_become": False,
            "ansible_python_interpreter": sys.executable,
        }))
        environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith("ANSIBLE_")
        }
        environment.update(
            ANSIBLE_CONFIG=str(ROOT / "ansible.cfg"),
            ANSIBLE_ROLES_PATH=str(ROOT / "roles"),
            ANSIBLE_LOCAL_TEMP=str(self.directory / "ansible-tmp"),
            ANSIBLE_NOCOLOR="1",
        )
        return subprocess.run([
            ansible_executable("ansible-playbook"), "-i", str(inventory),
            str(playbook), "--extra-vars", "@" + str(variable_file),
            *(["--check"] if check else []),
        ], cwd=ROOT, env=environment, capture_output=True, text=True, timeout=60)

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

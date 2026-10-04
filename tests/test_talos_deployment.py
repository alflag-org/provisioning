"""Exercise checkout preservation and the non-secret configuration boundary."""

import getpass
import grp
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

from ansible_support import ROOT, AnsibleTestCase


class TalosDeploymentTests(AnsibleTestCase):
    def test_resolved_interpreter_keeps_venv_imports_without_changing_the_base(self):
        base = Path(sys.executable).resolve()
        original_hash = hashlib.sha256(base.read_bytes()).digest()
        original_owner = base.stat().st_uid
        venv = self.directory / "venv"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
        python = venv / "bin/python"
        site_packages = Path(subprocess.check_output([
            str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))",
        ], text=True).strip())
        (site_packages / "talos_import_probe.py").write_text("VALUE = 'venv-only'\n")
        probe = "import talos_import_probe; print(talos_import_probe.VALUE)"
        before = subprocess.run([str(python.resolve()), "-c", probe], capture_output=True)
        self.assertNotEqual(before.returncode, 0)
        runtime = self.directory / "atlas/runtimes/python/test/bin"
        runtime.mkdir(parents=True)
        (runtime / "python").symlink_to(base)
        playbook = self.directory / "interpreter.yml"
        playbook.write_text(yaml.safe_dump([{
            "name": "Keep Atlas execution inside the Talos venv", "hosts": "default", "gather_facts": False,
            "vars": {
                "atlas_home": str(self.directory / "atlas"), "talos_python_version": "test",
                "talos_venv": str(venv), "talos_owner": getpass.getuser(),
                "talos_group": grp.getgrgid(os.getgid()).gr_name,
                "ansible_remote_tmp": str(self.directory / "remote-tmp"),
            },
            "tasks": [{"name": "Prepare interpreter", "ansible.builtin.include_role": {
                "name": str(ROOT / "roles/components/talos"), "tasks_from": "interpreter.yml",
            }}],
        }]))
        self.assert_success(self.run_playbook(playbook))
        self.assertFalse(python.is_symlink())
        self.assertEqual(subprocess.check_output([str(python.resolve()), "-c", probe], text=True).strip(), "venv-only")
        self.assertEqual(hashlib.sha256(base.read_bytes()).digest(), original_hash)
        self.assertEqual(base.stat().st_uid, original_owner)
        repeated = self.run_playbook(playbook)
        self.assert_success(repeated)
        self.assertIn("changed=0", repeated.stdout)

    def run_prepare(self, directory, config, dirty=False):
        source = directory / "source"
        source.mkdir()
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        (source / "owned.txt").write_text("original\n")
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-qm", "fixture"], check=True)
        revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        checkout = directory / "checkout"
        if dirty:
            subprocess.run(["git", "clone", "-q", str(source), str(checkout)], check=True)
            (checkout / "owned.txt").write_text("operator edit\n")
            (source / "owned.txt").write_text("new upstream\n")
            subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-qam", "update"], check=True)
            revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        playbook = directory / "prepare.yml"
        playbook.write_text(yaml.safe_dump([{
            "name": "Test Talos preparation", "hosts": "default", "gather_facts": False,
            "vars": {
                "ansible_become": False,
                "talos_root": str(checkout),
                "talos_repository": str(source),
                "talos_revision": revision,
                "talos_owner": getpass.getuser(),
                "talos_group": grp.getgrgid(os.getgid()).gr_name,
                "talos_config_path": str(directory / "config" / "config.yml"),
                "talos_config": config,
                "ansible_remote_tmp": str(directory / "remote-tmp"),
            },
            "tasks": [{"name": "Prepare Talos", "ansible.builtin.include_role": {
                "name": str(ROOT / "roles/components/talos"), "tasks_from": "prepare.yml",
            }}],
        }]))
        result = self.run_playbook(playbook)
        return result, checkout, directory / "config" / "config.yml"

    def config(self):
        return {"proxmox": {"url": "https://pve.example.internal:8006", "cluster": "demo"}, "registry": {"url": "https://registry.example.org"}}

    def test_preparation_does_not_invent_a_location_or_secret_mapping(self):
        with tempfile.TemporaryDirectory() as temporary:
            result, checkout, config = self.run_prepare(Path(temporary), self.config())
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((checkout / "owned.txt").read_text(), "original\n")
            self.assertEqual(yaml.safe_load(config.read_text()), self.config())

    def test_local_host_edits_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            result, checkout, config = self.run_prepare(Path(temporary), self.config(), dirty=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((checkout / "owned.txt").read_text(), "operator edit\n")
            self.assertFalse(config.exists())

    def test_credential_keys_are_rejected_before_creating_deployment_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = self.config()
            config["proxmox"]["api_token_secret"] = None
            result, checkout, output = self.run_prepare(Path(temporary), config)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(checkout.exists())
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()

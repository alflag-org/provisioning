"""Exercise checkout preservation and the non-secret configuration boundary."""

import getpass
import grp
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

from ansible_support import ROOT, AnsibleTestCase


class TalosDeploymentTests(AnsibleTestCase):
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
            config["proxmox"]["api_token_secret"] = "unprepared"
            result, checkout, output = self.run_prepare(Path(temporary), config)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(checkout.exists())
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()

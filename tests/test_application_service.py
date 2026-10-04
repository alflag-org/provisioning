"""Reject invalid application declarations before touching managed host state."""

import shutil
import pwd
import uuid
from pathlib import Path

import yaml

from ansible_support import AnsibleTestCase, ROOT


class ApplicationServiceTests(AnsibleTestCase):
    def setUp(self):
        super().setUp()
        self.role = self.directory / "application_service"
        shutil.copytree(ROOT / "roles/components/application_service", self.role)
        self.playbook = self.directory / "applications.yml"
        self.playbook.write_text(yaml.safe_dump([{
            "hosts": "default",
            "gather_facts": False,
            "roles": [str(self.role)],
        }]))

    def run_applications(self, applications, *, check=False, facts=None):
        return self.run_playbook(self.playbook, variables={
            "ansible_facts": facts or {"system": "Linux", "service_mgr": "systemd"},
            "application_services": applications,
        }, check=check)

    def assert_rejected_before_mutation(self, applications, message, *, facts=None):
        for check in (False, True):
            with self.subTest(applications=applications, check=check):
                result = self.run_applications(applications, check=check, facts=facts)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(message, result.stdout)
                self.assertRegex(result.stdout, r"changed=0\s")

    def add_run(self, name):
        run = self.role / "files/applications" / name / "run"
        run.parent.mkdir(parents=True, exist_ok=True)
        run.write_text("#!/bin/sh\nexec /bin/sleep infinity\n")
        return run

    def test_rejects_invalid_assignment_types(self):
        for applications in ("worker", {"worker": {}}, None, 1):
            self.assert_rejected_before_mutation(applications, "must be a list")

    def test_rejects_invalid_names(self):
        for name in ("../worker", "Worker", "worker/name", "worker\n", "a" * 29, 1):
            self.assert_rejected_before_mutation([name], "Application names must")

    def test_rejects_duplicates(self):
        self.add_run("duplicate-worker")
        self.assert_rejected_before_mutation(
            ["duplicate-worker", "duplicate-worker"], "must not contain duplicate names",
        )

    def test_rejects_missing_source_after_valid_source_before_mutation(self):
        self.add_run("ready-worker")
        self.assert_rejected_before_mutation(
            ["ready-worker", "missing-worker"], "Missing or non-regular run source",
        )

    def test_rejects_directory_and_symlink_run_sources(self):
        run = self.add_run("invalid-source-worker")
        run.unlink()
        run.mkdir()
        self.assert_rejected_before_mutation(
            ["invalid-source-worker"], "Missing or non-regular run source",
        )
        run.rmdir()
        run.symlink_to(self.add_run("other-worker"))
        self.assert_rejected_before_mutation(
            ["invalid-source-worker"], "Missing or non-regular run source",
        )

    def test_rejects_unsupported_platforms(self):
        for facts in ({"system": "FreeBSD", "service_mgr": "systemd"},
                      {"system": "Linux", "service_mgr": "sysvinit"}):
            self.assert_rejected_before_mutation(
                ["platform-worker"], "require Linux with systemd", facts=facts,
            )

    def test_empty_assignment_changes_nothing(self):
        result = self.run_applications([])
        self.assert_success(result)
        self.assertRegex(result.stdout, r"changed=0\s")

    def test_fresh_host_check_predicts_changes_without_creating_resources(self):
        # Exercise a valid name at the account-length boundary with real modules.
        name = "fixture-" + uuid.uuid4().hex[:20]
        self.add_run(name)
        paths = [Path(prefix) / name for prefix in ("/etc", "/opt", "/var/lib")]
        self.assertTrue(all(not path.exists() for path in paths))
        result = self.run_applications([name], check=True)
        self.assert_success(result)
        self.assertRegex(result.stdout, r"changed=[1-9][0-9]*\s")
        self.assertTrue(all(not path.exists() for path in paths))
        with self.assertRaises(KeyError):
            pwd.getpwnam("app-" + name)

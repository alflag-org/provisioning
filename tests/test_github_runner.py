"""Protect Runner cleanup boundaries and reject untrusted job events."""

import importlib.util
import json
import tempfile
from pathlib import Path
import unittest

from ansible_support import AnsibleTestCase, ROOT

SPEC = importlib.util.spec_from_file_location(
    'runner_cleanup', ROOT / 'roles/services/github_runner/files/cleanup.py',
)
CLEANUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLEANUP)


class RunnerCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.work = self.root / 'work'
        self.home = self.root / 'home'
        self.work.mkdir()
        self.home.mkdir()

    def test_start_preserves_current_event_and_end_removes_job_credentials(self):
        current = self.work / '_temp'
        current.mkdir()
        event = current / 'event.json'
        event.write_text('{}')
        checkout = self.work / 'repository'
        checkout.mkdir()
        (checkout / '.git-credentials').write_text('synthetic secret')
        (self.home / '.netrc').write_text('synthetic secret')
        CLEANUP.cleanup(self.work, self.home, 'start')
        self.assertTrue(event.exists())
        self.assertFalse(checkout.exists())
        self.assertEqual(list(self.home.iterdir()), [])
        CLEANUP.cleanup(self.work, self.home, 'end')
        self.assertEqual(list(self.work.iterdir()), [])
        CLEANUP.cleanup(self.work, self.home, 'end')

    def test_nested_symlinks_do_not_delete_external_data(self):
        external = self.root / 'external'
        external.mkdir()
        retained = external / 'keep'
        retained.write_text('retained')
        (self.work / 'link').symlink_to(external, target_is_directory=True)
        checkout = self.work / 'repository'
        checkout.mkdir()
        (checkout / 'link').symlink_to(external, target_is_directory=True)
        CLEANUP.cleanup(self.work, self.home, 'end')
        self.assertEqual(retained.read_text(), 'retained')
        self.assertEqual(list(self.work.iterdir()), [])

    def test_symlinked_work_root_is_rejected(self):
        self.work.rmdir()
        self.work.symlink_to(self.home, target_is_directory=True)
        retained = self.home / 'keep'
        retained.write_text('retained')
        with self.assertRaises(OSError):
            CLEANUP.cleanup(self.work, self.home, 'end')
        self.assertEqual(retained.read_text(), 'retained')

    def test_accepts_private_repositories_in_the_selected_organization(self):
        for repository in ('owner/first-ci', 'owner/second-ci', 'OWNER/third-ci'):
            with self.subTest(repository=repository):
                CLEANUP.validate_event({
                    'repository': {'private': True, 'full_name': repository},
                }, 'owner')

    def test_rejects_public_other_organization_and_fork_events(self):
        repository = 'owner/private-ci'
        event = {'repository': {'private': True, 'full_name': repository}}
        CLEANUP.validate_event(event, 'owner')
        CLEANUP.validate_event({
            **event, 'pull_request': {'head': {'repo': {'full_name': repository}}},
        }, 'owner')
        rejected = [
            {'repository': {'private': False, 'full_name': repository}},
            {'repository': {'private': True, 'full_name': 'other-owner/private-ci'}},
            {'repository': {'private': True, 'full_name': 'owner'}},
            {'repository': {'private': True, 'full_name': 'owner/invalid/name'}},
            {**event, 'pull_request': {'head': {'repo': {'full_name': 'fork/private-ci'}}}},
            {**event, 'pull_request': {'head': {'repo': {'full_name': 'owner/another-repo'}}}},
            {**event, 'pull_request': {'head': {'repo': None}}},
        ]
        for value in rejected:
            with self.subTest(event=value), self.assertRaises(ValueError):
                CLEANUP.validate_event(value, 'owner')

    def test_invalid_phase_preserves_files(self):
        retained = self.home / 'keep'
        retained.write_text('retained')
        with self.assertRaises(ValueError):
            CLEANUP.cleanup(self.work, self.home, 'invalid')
        self.assertTrue(retained.exists())


class RunnerIsolationTests(AnsibleTestCase):
    def test_unconfirmed_isolation_fails_before_mutation_and_token_exposure(self):
        playbook = self.directory / 'runner.yml'
        playbook.write_text(json.dumps([{
            'hosts': 'default', 'gather_facts': False,
            'roles': ['services/github_runner'],
        }]))
        for check, isolation, groups in (
            (False, False, ['platform_vm', 'svc_github_runner']),
            (True, False, ['platform_vm', 'svc_github_runner']),
            (False, True, ['platform_vm', 'svc_github_runner', 'svc_mysql']),
            (True, True, ['platform_vm', 'svc_github_runner', 'svc_mysql']),
        ):
            with self.subTest(check=check, isolation=isolation, groups=groups):
                result = self.run_playbook(playbook, variables={
                    'ansible_facts': {
                        'distribution': 'Ubuntu', 'distribution_version': '24.04',
                        'architecture': 'x86_64', 'service_mgr': 'systemd',
                    },
                    'group_names': groups,
                    'services_github_runner_isolation_confirmed': isolation,
                    'services_github_runner_organization': 'owner',
                    'services_github_runner_token': 'synthetic-secret-must-not-appear',
                }, check=check)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('confirmed guest-external network isolation', result.stdout)
                self.assertRegex(result.stdout, r'changed=0\s')
                self.assertNotIn('synthetic-secret-must-not-appear', result.stdout + result.stderr)

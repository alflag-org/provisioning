"""Exercise real Ansible operations with a synthetic vendor command boundary."""

import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import subprocess
import tarfile
import uuid

import yaml

from ansible_support import AnsibleTestCase, ROOT

ROLE = 'services/github_runner'
TEMPLATES = ROOT / 'roles/services/github_runner/templates'
TOKEN = 'synthetic-token-must-not-appear'
CONFIG = """#!/usr/bin/python3
import json, os, pathlib, sys
root = pathlib.Path.cwd()
args = sys.argv[1:]
(root / 'argv.json').write_text(json.dumps(args))
token = os.environ.get('ACTIONS_RUNNER_INPUT_TOKEN', '')
if not token or '--token' in args:
    raise SystemExit(3)
if token.startswith('reject-'):
    print(token)
    raise SystemExit(1)
if token == 'incomplete':
    raise SystemExit(0)
if args == ['remove']:
    for name in ['.runner', '.credentials', '.credentials_rsaparams']:
        (root / name).unlink()
else:
    def option(name):
        return args[args.index(name) + 1]
    (root / '.runner').write_text(json.dumps({
        'gitHubUrl': option('--url'), 'agentName': option('--name'),
        'workFolder': option('--work'), 'agentId': 0 if token == 'invalid-metadata' else 1,
    }))
    for name in ['.credentials', '.credentials_rsaparams']:
        (root / name).write_text('synthetic-vendor-credential')
        (root / name).chmod(0o644)
    (root / '.runner').chmod(0o644)
"""


class RunnerTests(AnsibleTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.instance('repo', 'https://github.com/owner/private-ci')
        self.org = self.instance('org', 'https://github.com/example-org', runner_group='Trusted')

    def instance(self, prefix, url, **options):
        identifier = prefix + '-' + uuid.uuid4().hex[:8]
        root = self.directory / identifier
        install, state = root / 'app', root / 'state'
        home, work = state / 'home', state / 'work'
        for path in (install / 'bin', home, work):
            path.mkdir(parents=True, exist_ok=True)
        (install / 'config.sh').write_text(CONFIG)
        (install / 'config.sh').chmod(0o755)
        for path in (install / 'bin/Runner.Listener', install / 'bin/runsvc.sh', install / 'runsvc.sh'):
            path.write_text('synthetic-self-updated-release')
            path.chmod(0o755)
        return {
            'services_github_runner_instance': {'id': identifier, 'url': url, **options},
            'services_github_runner_root': str(install),
            'services_github_runner_state': str(state),
            'services_github_runner_home': str(home),
            'services_github_runner_work': str(work),
            'services_github_runner_name': identifier,
            'services_github_runner_user': pwd.getpwuid(os.getuid()).pw_name,
            'services_github_runner_unit': 'github-runner-' + identifier + '.service',
            'services_github_runner_token': TOKEN,
        }

    def run_tasks(self, stages, context, *, check=False, tasks=None, environment=None):
        playbook = self.directory / 'operation.yml'
        playbook.write_text(yaml.safe_dump([{
            'hosts': 'default', 'gather_facts': False, 'environment': environment or {},
            'tasks': [{'ansible.builtin.include_role': {'name': ROLE, 'tasks_from': stage}}
                      for stage in stages] + (tasks or []),
        }]))
        if 'register' in stages:
            context = {'services_github_runner_operation': 'register', **context}
        result = self.run_playbook(playbook, variables=context, check=check)
        self.assertNotIn(TOKEN, result.stdout + result.stderr)
        return result

    def snapshot(self, context):
        root = Path(context['services_github_runner_root']).parent
        return {str(p.relative_to(root)): p.read_bytes()
                for p in root.rglob('*') if p.is_file() and not p.is_symlink()}

    def test_repository_organization_and_mixed_configuration(self):
        facts = {
            'distribution': 'Ubuntu', 'distribution_version': '24.04',
            'architecture': 'x86_64', 'service_mgr': 'systemd',
        }
        for contexts in ([self.repo], [self.org], [self.repo, self.org]):
            result = self.run_tasks(['validate'], {
                'services_github_runner_instances': [c['services_github_runner_instance'] for c in contexts],
                'services_github_runner_isolation_confirmed': True,
                'ansible_facts': facts, 'group_names': ['platform_vm', 'svc_github_runner'],
            })
            self.assert_success(result)
            self.assertRegex(result.stdout, r'changed=0\s')
        units = []
        for context in (self.repo, self.org):
            unit = Path(context['services_github_runner_root']).parent / 'unit'
            context = {**context, 'services_github_runner_user': 'gha-' + context['services_github_runner_instance']['id']}
            result = self.run_tasks([], context, tasks=[{
                'ansible.builtin.template': {
                    'src': str(TEMPLATES / 'github-runner.service.j2'),
                    'dest': str(unit), 'mode': '0600',
                },
            }])
            self.assert_success(result)
            units.append(unit.read_text())
            for field in ('user', 'root', 'home', 'state'):
                self.assertIn(context['services_github_runner_' + field], units[-1])
        self.assertNotIn(self.repo['services_github_runner_root'], units[1])
        self.assertNotIn(self.org['services_github_runner_root'], units[0])

    def test_invalid_selection_and_duplicate_ids_fail_before_mutation(self):
        instance = self.repo['services_github_runner_instance']
        for settings in (
            {'services_github_runner_instances': [instance, instance]},
            {'services_github_runner_instances': [instance], 'services_github_runner_operation': 'remove'},
        ):
            before = self.snapshot(self.repo)
            result = self.run_tasks(['validate'], settings)
            self.assertNotEqual(result.returncode, 0)
            self.assertRegex(result.stdout, r'changed=0\s')
            self.assertEqual(self.snapshot(self.repo), before)

    def test_ordinary_installation_does_not_register_an_instance(self):
        result = self.run_tasks(['state', 'register'], {
            **self.repo, 'services_github_runner_operation': 'install',
        })
        self.assert_success(result)
        self.assertRegex(result.stdout, r'changed=0\s')
        self.assertFalse((Path(self.repo['services_github_runner_root']) / 'argv.json').exists())

    def test_registration_of_both_scopes_is_independent_and_repeatable(self):
        for context, other in ((self.repo, self.org), (self.org, self.repo)):
            before = self.snapshot(other)
            result = self.run_tasks(['state', 'register'], context)
            self.assert_success(result)
            self.assertEqual(self.snapshot(other), before)
            root = Path(context['services_github_runner_root'])
            args = json.loads((root / 'argv.json').read_text())
            self.assertEqual(args[args.index('--url') + 1], context['services_github_runner_instance']['url'])
            self.assertEqual('--runnergroup' in args, 'runner_group' in context['services_github_runner_instance'])
            self.assertNotIn(TOKEN, ''.join(p.read_text() for p in root.rglob('*') if p.is_file()))
            for name in ('.runner', '.credentials', '.credentials_rsaparams'):
                self.assertEqual((root / name).stat().st_mode & 0o777, 0o600)
            result = self.run_tasks(['state', 'register'], context)
            self.assert_success(result)
            self.assertRegex(result.stdout, r'changed=0\s')

    def test_first_registration_rejects_invalid_metadata_and_preserves_recovery_state(self):
        other = self.snapshot(self.org)
        result = self.run_tasks(['state', 'register'], {
            **self.repo, 'services_github_runner_token': 'invalid-metadata',
        })
        self.assertNotEqual(result.returncode, 0)
        root = Path(self.repo['services_github_runner_root'])
        self.assertTrue((root / '.registration-attempt').exists())
        self.assertTrue((root / '.credentials').exists())
        before = self.snapshot(self.repo)
        retry = self.run_tasks(['state', 'register'], self.repo)
        self.assertNotEqual(retry.returncode, 0)
        self.assertRegex(retry.stdout, r'changed=0\s')
        self.assertEqual(self.snapshot(self.repo), before)
        self.assertEqual(self.snapshot(self.org), other)

    def test_fresh_host_check_mode_needs_no_account_binary_token_or_service(self):
        instance = self.repo['services_github_runner_instance']
        identifier = instance['id']
        account = 'gha-' + identifier
        paths = [
            Path('/opt/github-actions-runner') / identifier,
            Path('/var/lib/github-actions-runner') / identifier,
            Path('/etc/systemd/system') / ('github-runner-' + identifier + '.service'),
            Path('/etc/tmpfiles.d') / ('github-runner-' + identifier + '.conf'),
            *[Path('/usr/local/libexec/github-runner') / (identifier + '-' + phase + '.sh')
              for phase in ('start', 'end')],
        ]
        self.assertTrue(all(not p.exists() for p in paths))
        self.assertFalse(any(p.pw_name == account for p in pwd.getpwall()))
        self.assertFalse(any(g.gr_name == account for g in grp.getgrall()))
        for operation in ('install', 'register'):
            result = self.run_tasks(['main'], {
                'services_github_runner_instances': [instance],
                'services_github_runner_operation': operation,
                'services_github_runner_selected_instance': identifier,
                'services_github_runner_isolation_confirmed': True,
                # Package availability on Ubuntu requires a real target; this
                # check exercises the account/file/registration dependencies.
                'services_github_runner_packages': [],
                'ansible_facts': {'distribution': 'Ubuntu', 'distribution_version': '24.04',
                                  'architecture': 'x86_64', 'service_mgr': 'systemd'},
                'group_names': ['platform_vm', 'svc_github_runner'],
            }, check=True)
            self.assert_success(result)
            self.assertTrue(all(not p.exists() for p in paths))
            self.assertFalse(any(p.pw_name == account for p in pwd.getpwall()))
            self.assertFalse(any(g.gr_name == account for g in grp.getgrall()))

    def test_registration_starts_only_its_service_and_convergence_never_restarts_it(self):
        # A single systemctl boundary exercises the real Ansible systemd module
        # without touching the controller's services.
        binary = self.directory / 'systemctl'
        state = self.directory / 'service-state.json'
        calls = self.directory / 'service-calls.jsonl'
        unit, other = self.repo['services_github_runner_unit'], self.org['services_github_runner_unit']
        state.write_text(json.dumps({unit: {'active': False, 'enabled': False},
                                    other: {'active': True, 'enabled': True}}))
        binary.write_text(r"""#!/usr/bin/python3
import json, pathlib, sys
root = pathlib.Path(__file__).parent
state_file = root / 'service-state.json'
state = json.loads(state_file.read_text())
args = sys.argv[1:]
with (root / 'service-calls.jsonl').open('a') as output:
    output.write(json.dumps(args) + '\n')
if args == ['daemon-reload']:
    raise SystemExit(0)
command, unit = args[:2]
current = state[unit]
if command == 'show':
    print('LoadState=loaded')
    print('ActiveState=' + ('active' if current['active'] else 'inactive'))
elif command == 'is-enabled':
    print('enabled' if current['enabled'] else 'disabled')
    raise SystemExit(0 if current['enabled'] else 1)
elif command in ('enable', 'start'):
    current['enabled' if command == 'enable' else 'active'] = True
    state_file.write_text(json.dumps(state))
else:
    raise SystemExit('Unexpected service mutation: ' + command)
""")
        binary.chmod(0o755)
        environment = {'PATH': str(self.directory) + ':' + os.environ['PATH']}
        self.assert_success(self.run_tasks(['state', 'start'], self.repo, environment=environment))
        self.assertFalse(calls.exists())
        self.assert_success(self.run_tasks(['state', 'register', 'start'], self.repo, environment=environment))
        self.assertEqual(json.loads(state.read_text())[unit], {'active': True, 'enabled': True})
        self.assertEqual(json.loads(state.read_text())[other], {'active': True, 'enabled': True})
        first_calls = [json.loads(line) for line in calls.read_text().splitlines()]
        self.assertIn(['start', unit], first_calls)
        self.assertIn(['enable', unit], first_calls)
        calls.unlink()
        # A changed unit must not cause a running Runner to restart.
        result = self.run_tasks(['state'], self.repo, environment=environment, tasks=[{
            'ansible.builtin.template': {'src': str(TEMPLATES / 'github-runner.service.j2'),
                                        'dest': str(self.directory / 'updated-unit'), 'mode': '0600'},
        }, {'ansible.builtin.include_role': {'name': ROLE, 'tasks_from': 'start'}}])
        self.assert_success(result)
        repeated_calls = [json.loads(line) for line in calls.read_text().splitlines()]
        self.assertFalse(any(args[0] in ('start', 'stop', 'restart', 'enable', 'disable') for args in repeated_calls))
        self.assertFalse(any(other in args for args in first_calls + repeated_calls))
        result = self.run_tasks(['state', 'start'], self.repo, environment=environment)
        self.assert_success(result)
        self.assertRegex(result.stdout, r'changed=0\s')

    def test_failed_or_incomplete_registration_blocks_retry_without_deleting_other_instance(self):
        for context, token in ((self.repo, 'reject-' + TOKEN), (self.org, 'incomplete')):
            before = self.snapshot(self.org if context is self.repo else self.repo)
            result = self.run_tasks(['state', 'register'], {**context, 'services_github_runner_token': token})
            self.assertNotEqual(result.returncode, 0)
            root = Path(context['services_github_runner_root'])
            self.assertTrue((root / '.registration-attempt').exists())
            attempt = (root / 'argv.json').stat().st_mtime_ns
            retry = self.run_tasks(['state', 'register'], context)
            self.assertNotEqual(retry.returncode, 0)
            self.assertRegex(retry.stdout, r'changed=0\s')
            self.assertEqual((root / 'argv.json').stat().st_mtime_ns, attempt)
            self.assertEqual(self.snapshot(self.org if context is self.repo else self.repo), before)

    def test_failed_removal_preserves_credentials_and_success_removes_only_selected_instance(self):
        for context in (self.repo, self.org):
            self.assert_success(self.run_tasks(['state', 'register'], context))
        before = self.snapshot(self.org)
        identifier = self.repo['services_github_runner_instance']['id']
        # No real OS account is created or deleted by this fixture.
        account = 'gha-' + identifier
        self.assertFalse(any(p.pw_name == account for p in pwd.getpwall()))
        self.assertFalse(any(g.gr_name == account for g in grp.getgrall()))
        context = {**self.repo, 'services_github_runner_user': account}
        result = self.run_tasks(['state', 'remove'], {**context, 'services_github_runner_token': 'reject-' + TOKEN})
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((Path(context['services_github_runner_root']) / '.credentials').exists())
        self.assertEqual(self.snapshot(self.org), before)
        self.assert_success(self.run_tasks(['state', 'remove'], context))
        self.assertFalse(Path(context['services_github_runner_root']).exists())
        self.assertFalse(Path(context['services_github_runner_state']).exists())
        self.assertEqual(self.snapshot(self.org), before)
        result = self.run_tasks(['state', 'remove'], {k: v for k, v in context.items() if k != 'services_github_runner_token'})
        self.assert_success(result)
        self.assertRegex(result.stdout, r'changed=0\s')

    def test_bootstrap_version_change_preserves_self_updated_binaries_and_registration(self):
        self.assert_success(self.run_tasks(['state', 'register'], self.repo))
        before, other = self.snapshot(self.repo), self.snapshot(self.org)
        result = self.run_tasks(['state', 'release'], {**self.repo, 'services_github_runner_version': '99.0.0'})
        self.assert_success(result)
        self.assertRegex(result.stdout, r'changed=0\s')
        self.assertEqual(self.snapshot(self.repo), before)
        self.assertEqual(self.snapshot(self.org), other)

    def test_changed_identity_and_invalid_registration_are_preserved_for_recovery(self):
        self.assert_success(self.run_tasks(['state', 'register'], self.repo))
        root = Path(self.repo['services_github_runner_root'])
        before = self.snapshot(self.repo)
        changed = {**self.repo, 'services_github_runner_instance': {
            **self.repo['services_github_runner_instance'], 'url': 'https://github.com/another-org',
        }}
        result = self.run_tasks(['state', 'register'], changed)
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(result.stdout, r'changed=0\s')
        self.assertEqual(self.snapshot(self.repo), before)
        metadata = json.loads((root / '.runner').read_text())
        metadata['agentId'] = 0
        (root / '.runner').write_text(json.dumps(metadata))
        before = self.snapshot(self.repo)
        result = self.run_tasks(['state', 'register'], self.repo)
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(result.stdout, r'changed=0\s')
        self.assertEqual(self.snapshot(self.repo), before)

    def test_initial_archive_is_verified_before_extraction(self):
        root = Path(self.repo['services_github_runner_root'])
        for p in (root / 'bin/Runner.Listener', root / 'runsvc.sh'):
            p.unlink()
        source = self.directory / 'package'
        (source / 'bin').mkdir(parents=True)
        (source / 'bin/Runner.Listener').write_text('verified-initial-release')
        (source / 'bin/runsvc.sh').write_text('entry-point')
        archive = self.directory / 'runner.tar.gz'
        with tarfile.open(archive, 'w:gz') as output:
            output.add(source / 'bin', arcname='bin')
        context = {**self.repo, 'services_github_runner_registered': False,
                   'services_github_runner_release_url': archive.as_uri(),
                   'services_github_runner_checksum': 'sha256:' + '0' * 64}
        other = self.snapshot(self.org)
        result = self.run_tasks(['release'], context)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((root / 'bin/Runner.Listener').exists())
        context['services_github_runner_checksum'] = 'sha256:' + hashlib.sha256(archive.read_bytes()).hexdigest()
        self.assert_success(self.run_tasks(['release'], context))
        self.assertEqual((root / 'bin/Runner.Listener').read_text(), 'verified-initial-release')
        self.assertEqual(self.snapshot(self.org), other)

    def test_cleanup_preserves_other_instance_and_symlink_targets(self):
        work, home = Path(self.repo['services_github_runner_work']), Path(self.repo['services_github_runner_home'])
        external = Path(self.org['services_github_runner_home']) / 'keep'
        external.write_text('retained')
        (work / 'external').symlink_to(external.parent, target_is_directory=True)
        checkout = work / 'repository'
        checkout.mkdir()
        (checkout / 'secret').write_text('temporary')
        current = work / '_temp'
        current.mkdir()
        (current / 'event.json').write_text('{}')
        (home / '.netrc').write_text('temporary')
        scripts = []
        for phase in ('start', 'end'):
            script = self.directory / (phase + '.sh')
            result = self.run_tasks([], {**self.repo, 'item': phase}, tasks=[{
                'ansible.builtin.template': {'src': str(TEMPLATES / 'cleanup.sh.j2'),
                                            'dest': str(script), 'mode': '0700'},
            }])
            self.assert_success(result)
            subprocess.run(['shellcheck', str(script)], check=True, capture_output=True)
            subprocess.run(['sh', str(script)], check=True, capture_output=True)
            if phase == 'start':
                self.assertTrue((current / 'event.json').exists())
            scripts.append(script)
        self.assertFalse(checkout.exists())
        self.assertEqual(list(work.iterdir()), [])
        self.assertEqual(list(home.iterdir()), [])
        self.assertEqual(external.read_text(), 'retained')
        work.rmdir()
        work.symlink_to(external.parent, target_is_directory=True)
        result = subprocess.run(['sh', str(scripts[0])], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(external.read_text(), 'retained')

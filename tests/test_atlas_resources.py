import grp
import os
from pathlib import Path
import pwd
import subprocess
import sys

import yaml

from ansible_support import AnsibleTestCase, ansible_executable


class AtlasResourceTests(AnsibleTestCase):
    def setUp(self):
        super().setUp()
        self.home = self.directory / 'atlas'
        self.etc = self.directory / 'etc'
        self.home.mkdir()
        self.etc.mkdir()
        self.program = self.directory / 'program'
        (self.program / 'bin').mkdir(parents=True)
        self.add_command('hello')
        (self.etc / 'host.yml').write_text(yaml.safe_dump({
            'version': 1, 'host': {'id': 'fixture', 'role': 'host', 'site': 'test'},
        }))
        self.config = self.etc / 'config.yml'
        self.config.write_text(yaml.safe_dump({'programs': {
            'fixture': {'root': str(self.program), 'runtime': {'type': 'native'}},
        }}))
        self.variables = {
            'atlas_home': str(self.home),
            'atlas_etc_dir': str(self.etc),
            'atlas_operator_user': pwd.getpwuid(os.getuid()).pw_name,
            'atlas_operator_group': grp.getgrgid(os.getgid()).gr_name,
            'atlas_become_operator': False,
            'atlas_cli_bin': ansible_executable('atlas'),
            'atlas_runtime_python_version': 'fixture',
            'atlas_runtime_python_executable': sys.executable,
            'atlas': {'programs': {}},
        }

    def add_command(self, name):
        command = self.program / 'bin' / name
        command.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        command.chmod(0o755)

    def converge(self, tasks_from, *, check=False):
        playbook = self.directory / 'resources.yml'
        playbook.write_text(yaml.safe_dump([{
            'hosts': 'fixture', 'gather_facts': False,
            'environment': {'ATLAS_HOME': str(self.home), 'ATLAS_ETC_DIR': str(self.etc),
                            'ATLAS_VAR_DIR': str(self.directory / 'var')},
            'tasks': [{'ansible.builtin.include_role': {
                'name': 'atlas_host', 'tasks_from': tasks_from,
            }}],
        }]))
        return self.run_playbook(playbook, check=check, variables=self.variables)

    def test_runtime_links_converge_and_follow_changed_selection(self):
        self.assert_success(self.converge('runtime.yml', check=True))
        target = self.home / 'runtimes/python/fixture/bin/python'
        self.assertFalse(target.exists())
        self.assert_success(self.converge('runtime.yml'))
        self.assertEqual(target.readlink(), Path(sys.executable))
        repeated = self.converge('runtime.yml')
        self.assert_success(repeated)
        self.assertIn('changed=0', repeated.stdout)
        self.variables['atlas_runtime_python_executable'] = '/usr/bin/python3'
        self.assert_success(self.converge('runtime.yml'))
        self.assertEqual(target.readlink(), Path('/usr/bin/python3'))

    def test_shims_converge_and_only_remove_retired_owned_files(self):
        self.assert_success(self.converge('shims.yml', check=True))
        shim = self.home / 'shims/hello'
        self.assertFalse(shim.exists())
        self.assert_success(self.converge('shims.yml'))
        before = shim.stat().st_mtime_ns
        result = self.converge('shims.yml')
        self.assert_success(result)
        self.assertIn('changed=0', result.stdout)
        self.assertEqual(shim.stat().st_mtime_ns, before)
        executed = subprocess.run([str(shim), 'literal argument'], capture_output=True, text=True,
                                  env={**os.environ, 'ATLAS_HOME': str(self.home), 'ATLAS_ETC_DIR': str(self.etc),
                                       'ATLAS_VAR_DIR': str(self.directory / 'var')})
        self.assertEqual(executed.returncode, 0, executed.stderr)
        self.assertEqual(executed.stdout, 'literal argument\n')
        unrelated = self.home / 'shims/unrelated'
        unrelated.write_text('preserve this file')
        (self.program / 'bin/hello').unlink()
        self.add_command('replacement')
        self.assert_success(self.converge('shims.yml'))
        self.assertFalse(shim.exists())
        self.assertTrue((self.home / 'shims/replacement').exists())
        self.assertEqual(unrelated.read_text(), 'preserve this file')

    def test_shim_collision_refuses_to_overwrite_an_unmanaged_file(self):
        (self.home / 'shims').mkdir()
        shim = self.home / 'shims/hello'
        shim.write_text('preserve this file')
        result = self.converge('shims.yml')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('non-Atlas file or symlink', result.stdout)
        self.assertEqual(shim.read_text(), 'preserve this file')

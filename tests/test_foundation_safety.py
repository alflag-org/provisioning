import grp
import os
from pathlib import Path
import pwd
import shutil
import subprocess

import yaml

from ansible_support import AnsibleTestCase, ROOT


class FoundationSafetyTests(AnsibleTestCase):
    def swap_playbook(self, path, *, tasks_from='swap.yml'):
        playbook = self.directory / 'swap.yml'
        playbook.write_text(yaml.safe_dump([{
            'hosts': 'fixture',
            'gather_facts': False,
            'tasks': [{'ansible.builtin.include_role': {
                'name': 'foundation/platform_vm', 'tasks_from': tasks_from,
            }}],
        }]))
        return self.run_playbook(playbook, check=True, variables={
            'foundation_vm_swap_file_path': str(path),
            'foundation_vm_swap_file_size_mb': 8,
        })

    def test_resize_refuses_a_regular_file_without_swap_signature(self):
        path = self.directory / 'valuable-data'
        content = b'preserve this data\n' + bytes(2 * 1024 * 1024)
        path.write_bytes(content)
        result = self.swap_playbook(path)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('without a swap signature', result.stdout)
        self.assertEqual(path.read_bytes(), content)

    def test_swap_refuses_symlinks_before_reading_or_mutating_the_target(self):
        target = self.directory / 'valuable-data'
        target.write_text('preserve this data')
        path = self.directory / 'swap-link'
        path.symlink_to(target)
        result = self.swap_playbook(path)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('not a regular file', result.stdout)
        self.assertTrue(path.is_symlink())
        self.assertEqual(target.read_text(), 'preserve this data')

    def test_existing_swap_signature_is_accepted_without_modification(self):
        path = self.directory / 'swap'
        with path.open('wb') as stream:
            stream.truncate(2 * 1024 * 1024)
        path.chmod(0o600)
        subprocess.run([shutil.which('mkswap'), str(path)], check=True, capture_output=True)
        before = path.read_bytes()
        result = self.swap_playbook(path, tasks_from='swap_check.yml')
        self.assert_success(result)
        self.assertEqual(path.read_bytes(), before)

    def test_venv_ownership_does_not_follow_system_python(self):
        account = pwd.getpwuid(os.getuid()) if os.getuid() else pwd.getpwnam('nobody')
        group = grp.getgrgid(account.pw_gid).gr_name
        venv = self.directory / 'venv'
        binary = venv / 'bin'
        binary.mkdir(parents=True)
        link = binary / 'python'
        link.symlink_to(Path('/usr/bin/python3').resolve())
        if os.getuid() == 0:
            for path in (venv, binary, link):
                os.chown(path, account.pw_uid, account.pw_gid, follow_symlinks=False)
        interpreter = link.resolve()
        before = interpreter.stat()
        tasks = yaml.safe_load((ROOT / 'roles/atlas_host/tasks/cli.yml').read_text())
        ownership = next(task for task in tasks if task['name'] == 'Ensure Atlas CLI venv ownership')
        ownership = {**ownership, 'register': 'ownership'}
        playbook = self.directory / 'ownership.yml'
        playbook.write_text(yaml.safe_dump([{
            'hosts': 'fixture', 'gather_facts': False,
            'tasks': [ownership, {'ansible.builtin.assert': {'that': ['not ownership.changed']}}],
        }]))
        result = self.run_playbook(playbook, check=True, variables={
            'atlas_cli_venv': str(venv),
            'atlas_operator_user': account.pw_name,
            'atlas_operator_group': group,
            'atlas_become_root': False,
        })
        self.assert_success(result)
        after = interpreter.stat()
        self.assertEqual((before.st_uid, before.st_gid, before.st_mode),
                         (after.st_uid, after.st_gid, after.st_mode))

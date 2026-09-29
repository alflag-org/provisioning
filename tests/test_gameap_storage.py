"""Refuse destructive storage changes when an observed disk is not the declared volume."""
import copy
import uuid
import yaml
from ansible_support import AnsibleTestCase, ROOT


class GameAPStorageTests(AnsibleTestCase):
    def test_storage_guard_accepts_only_empty_or_matching_filesystem(self):
        declared_uuid = str(uuid.uuid4())
        disk = {'path': '/dev/example', 'type': 'disk', 'size': 1024,
                'fstype': None, 'uuid': None, 'mountpoints': []}
        cases = [(disk, [], True)]
        mounted = dict(disk, fstype='ext4', uuid=declared_uuid, mountpoints=['/srv/gameap'])
        cases.append((mounted, [{'type': 'ext4'}], True))
        for changes in [{'type': 'part'}, {'size': 2048}, {'children': [{'path': '/dev/example1'}]},
                        {'mountpoints': ['/']}, {'fstype': 'ext4', 'uuid': str(uuid.uuid4())}]:
            cases.append((dict(disk, **changes), [], False))
        cases.append((disk, [{'type': 'gpt'}], False))
        cases.append((mounted, [{'type': 'ext4'}, {'type': 'gpt'}], False))
        playbook = self.directory / 'storage.yml'
        playbook.write_text(yaml.safe_dump([{
            'hosts': 'fixture', 'gather_facts': False, 'tasks': [{
                'name': 'Validate the storage decision',
                'ansible.builtin.include_tasks': str(ROOT / 'roles/services/gameap_daemon/tasks/validate_storage.yml'),
            }],
        }]))
        for observed, signatures, accepted in cases:
            with self.subTest(disk=observed, signatures=signatures):
                result = self.run_playbook(playbook, check=True, variables={
                    'services_gameap_daemon_disk': copy.deepcopy(observed),
                    'services_gameap_daemon_signatures': signatures,
                    'services_gameap_daemon_data_uuid': declared_uuid,
                    'services_gameap_daemon_data_size_bytes': 1024,
                })
                self.assertEqual(result.returncode == 0, accepted, result.stdout + result.stderr)

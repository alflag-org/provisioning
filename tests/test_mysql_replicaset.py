import importlib.util
from pathlib import Path
import unittest

PATH = Path(__file__).resolve().parents[1] / 'roles/components/mysql_replicaset/files/mysql-replicaset.py'
SPEC = importlib.util.spec_from_file_location('mysql_replicaset', PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ReplicaSetPolicyTests(unittest.TestCase):
    def test_unreachable_primary_is_not_reported_as_writable(self):
        instances = [{'name': 'one', 'host': 'one.test'}, {'name': 'two', 'host': 'two.test'}]
        status = {'replicaSet': {'topology': {
            'one.test:3306': {'instanceRole': 'PRIMARY', 'status': 'UNREACHABLE'},
            'two.test:3306': {'instanceRole': 'SECONDARY', 'status': 'CONNECTING'},
        }}}
        state = MODULE.normalize_status(status, instances)
        self.assertIsNone(state['primary'])
        self.assertIsNone(state['secondary'])
        MODULE.validate_failover_candidate(state, 'two')
        with self.assertRaises(RuntimeError):
            MODULE.validate_online_pair(state, instances)

    def test_failover_refuses_online_primary_and_invalid_targets(self):
        for primary, role, status in (
            ('one', 'SECONDARY', 'ONLINE'),
            (None, 'PRIMARY', 'ONLINE'),
            (None, 'SECONDARY', 'UNREACHABLE'),
            (None, 'SECONDARY', 'INVALIDATED'),
            (None, 'SECONDARY', 'ERROR'),
        ):
            with self.subTest(primary=primary, role=role, status=status):
                state = {'primary': primary, 'members': [{'name': 'two', 'role': role, 'status': status}]}
                with self.assertRaises(RuntimeError):
                    MODULE.validate_failover_candidate(state, 'two')
        with self.assertRaises(RuntimeError):
            MODULE.validate_failover_candidate({'primary': None, 'members': []}, 'missing')

    def test_writable_secondary_is_rejected(self):
        instances = [{'name': 'one'}, {'name': 'two'}]
        state = {'primary': 'one', 'members': [
            {'name': 'one', 'role': 'PRIMARY', 'status': 'ONLINE'},
            {'name': 'two', 'role': 'SECONDARY', 'status': 'ONLINE'},
        ], 'serverVariables': {
            'one': {'reachable': True, 'readOnly': 0, 'superReadOnly': 0},
            'two': {'reachable': True, 'readOnly': 1, 'superReadOnly': 0},
        }}
        with self.assertRaises(RuntimeError):
            MODULE.validate_writable_topology(state, instances)
        state['serverVariables']['two']['superReadOnly'] = 1
        MODULE.validate_writable_topology(state, instances)


if __name__ == '__main__':
    unittest.main()

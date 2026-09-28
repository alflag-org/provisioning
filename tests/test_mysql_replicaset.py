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


class ReplicationAllowedHostTests(unittest.TestCase):
    class ReplicaSet:
        def __init__(self):
            self.host = '192.0.2.0/24'
            self.writes = []

        def options(self):
            return {'replicaSet': {'globalOptions': [
                {'option': 'replicationAllowedHost', 'value': self.host},
            ]}}

        def set_option(self, option, value):
            self.writes.append((option, value))
            self.host = value

    def test_subnet_drift_is_reported_without_mutation_in_check_mode(self):
        replicaset = self.ReplicaSet()
        accounts = [('one', 'replica_one', replicaset.host, True, False)]
        result = MODULE.converge_replication_allowed_host(
            replicaset, '198.51.100.0/24', accounts, dry_run=True,
        )
        self.assertEqual(result, {'changed': True, 'current': '192.0.2.0/24', 'desired': '198.51.100.0/24'})
        self.assertEqual(replicaset.writes, [])
        self.assertEqual(replicaset.host, '192.0.2.0/24')

    def test_subnet_update_converges_once_through_adminapi(self):
        replicaset = self.ReplicaSet()
        accounts = [('one', 'replica_one', replicaset.host, True, False)]
        result = MODULE.converge_replication_allowed_host(
            replicaset, '198.51.100.0/24', accounts, dry_run=False,
        )
        self.assertTrue(result['changed'])
        accounts = [('one', 'replica_one', replicaset.host, True, True)]
        result = MODULE.converge_replication_allowed_host(
            replicaset, '198.51.100.0/24', accounts, dry_run=False,
        )
        self.assertFalse(result['changed'])
        self.assertEqual(replicaset.writes, [('replicationAllowedHost', '198.51.100.0/24')])

    def test_ambiguous_metadata_is_rejected_before_mutation(self):
        replicaset = self.ReplicaSet()
        accounts = [('one', 'replica_one', replicaset.host, True, False)]
        for options in [[], [{'option': 'replicationAllowedHost', 'value': '%'}] * 2]:
            replicaset.options = lambda: {'replicaSet': {'globalOptions': options}}
            with self.assertRaises(RuntimeError):
                MODULE.converge_replication_allowed_host(
                    replicaset, '198.51.100.0/24', accounts, dry_run=False,
                )
        self.assertEqual(replicaset.writes, [])

    def test_partial_manual_account_move_fails_before_any_adminapi_write(self):
        replicaset = self.ReplicaSet()
        accounts = [
            ('one', 'replica_one', '192.0.2.0/24', True, False),
            ('two', 'replica_two', '192.0.2.0/24', False, True),
        ]
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run), self.assertRaisesRegex(RuntimeError, 'metadata disagrees'):
                MODULE.converge_replication_allowed_host(
                    replicaset, '198.51.100.0/24', accounts, dry_run=dry_run,
                )
        self.assertEqual(replicaset.writes, [])


if __name__ == '__main__':
    unittest.main()

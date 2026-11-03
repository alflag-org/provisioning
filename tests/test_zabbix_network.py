"""ICMP adoption must retain behavior and resume safely after partial updates."""

import copy
import importlib.util
from pathlib import Path
import unittest


PATH = Path(__file__).resolve().parents[1] / 'roles/services/zabbix_api/filter_plugins/zabbix_network.py'
SPEC = importlib.util.spec_from_file_location('zabbix_network', PATH)
network = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(network)


def fixture():
    suffixes = ['Unavailable by ICMP ping', 'High ICMP ping loss', 'High ICMP ping response time']
    keys = ['icmpping', 'icmppingloss', 'icmppingsec']
    host = {'hostid': '1', 'parentTemplates': [{'name': 'ICMP Ping'}]}
    items, template_items, triggers, template_triggers = [], [], [], []
    for index, (suffix, key) in enumerate(zip(suffixes, keys), 1):
        item_fields = {'key_': key, 'type': '3', 'value_type': '3', 'delay': '1m', 'status': '0', 'units': '', 'preprocessing': [],
                       'history': '7d', 'trends': '30d', 'timeout': '2s', 'master_itemid': '0'}
        items.append({**item_fields, 'hostid': '1', 'itemid': f'h{index}', 'templateid': f'o{index}'})
        template_items.extend([{**item_fields, 'itemid': f'n{index}'}, {**item_fields, 'itemid': f'o{index}'}])
        common = {'recovery_expression': '', 'recovery_mode': '0', 'priority': '2',
                  'status': '0', 'type': '0', 'manual_close': '0',
                  'correlation_mode': '0', 'correlation_tag': ''}
        triggers.append({**common, 'triggerid': str(index), 'description': 'ICMP Ping: ' + suffix,
                         'templateid': str(10 + index), 'hosts': [{'hostid': '1'}],
                         'expression': '{' + str(100 + index) + '}=0',
                         'functions': [{'functionid': str(100 + index), 'itemid': f'h{index}',
                                        'function': 'max', 'parameter': '$,#3'}],
                         'dependencies': [{'triggerid': str(index - 1)}] if index > 1 else []})
        template_triggers.append({**common, 'triggerid': str(20 + index),
                                  'description': 'Network Generic Device: ' + suffix,
                                  'templateid': '0', 'expression': '{' + str(200 + index) + '}=0',
                                  'functions': [{'functionid': str(200 + index), 'itemid': f'n{index}',
                                                 'function': 'max', 'parameter': '$,#3'}],
                                  'dependencies': [{'triggerid': str(20 + index - 1)}] if index > 1 else []})
        old = copy.deepcopy(template_triggers[-1])
        old.update(triggerid=str(10 + index), description='ICMP Ping: ' + suffix)
        old['functions'][0]['itemid'] = f'o{index}'
        template_triggers.append(old)
    return host, items, triggers, template_items, template_triggers, []


class IcmpMigrationTests(unittest.TestCase):
    def test_equivalent_functions_with_different_ids_keep_host_trigger_ids(self):
        source = fixture()
        before = copy.deepcopy(source)
        plan = network.icmp_migration(*source)
        self.assertTrue(plan['unlink'])
        self.assertEqual([row['triggerid'] for row in plan['renames']], ['1', '2', '3'])
        self.assertTrue(all(row['description'].startswith('Network Generic Device: ') for row in plan['renames']))
        self.assertEqual(source, before)

    def test_resume_after_unlink_and_one_rename(self):
        source = fixture()
        source[0]['parentTemplates'] = []
        for trigger in source[2]:
            trigger['templateid'] = '0'
        source[2][0]['description'] = 'Network Generic Device: Unavailable by ICMP ping'
        plan = network.icmp_migration(*source)
        self.assertFalse(plan['unlink'])
        self.assertEqual([row['triggerid'] for row in plan['renames']], ['2', '3'])

    def test_reject_duplicate_old_and_new_trigger(self):
        source = fixture()
        duplicate = copy.deepcopy(source[2][0])
        duplicate['description'] = 'Network Generic Device: Unavailable by ICMP ping'
        source[2].append(duplicate)
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            network.icmp_migration(*source)

    def test_reject_changed_behavior_before_adoption(self):
        source = fixture()
        source[2][0]['functions'][0]['parameter'] = '$,#7'
        with self.assertRaisesRegex(ValueError, 'Modified'):
            network.icmp_migration(*source)

    def test_reject_unexpected_parent_template(self):
        source = fixture()
        source[0]['parentTemplates'].append({'name': 'Manually managed measurements'})
        with self.assertRaisesRegex(ValueError, 'Unexpected'):
            network.icmp_migration(*source)

    def test_reject_foreign_dependency_even_with_the_same_name_suffix(self):
        source = fixture()
        source[2][1]['dependencies'] = [{'triggerid': '99'}]
        source[2].append({'triggerid': '99', 'hosts': [{'hostid': '1'}],
                          'description': 'Custom suppression: Unavailable by ICMP ping'})
        with self.assertRaisesRegex(ValueError, 'outside'):
            network.icmp_migration(*source)

    def test_reject_modified_item_cadence(self):
        source = fixture()
        source[1][0]['delay'] = '7m'
        with self.assertRaisesRegex(ValueError, 'Modified ICMP item'):
            network.icmp_migration(*source)

    def test_reject_unmanaged_host_threshold_override(self):
        source = fixture()
        source[0]['macros'] = [{'macro': '{$ICMP_LOSS_WARN}'}]
        with self.assertRaisesRegex(ValueError, 'threshold overrides'):
            network.icmp_migration(*source)


if __name__ == '__main__':
    unittest.main()

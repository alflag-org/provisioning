"""Scope preparation must stop optional requests before credentials are enrolled."""

import importlib.util
import unittest
from copy import deepcopy
from pathlib import Path


PATH = Path(__file__).resolve().parents[1] / 'roles/services/zabbix_api/filter_plugins/zabbix_proxmox.py'
SPEC = importlib.util.spec_from_file_location('zabbix_proxmox', PATH)
proxmox = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxmox)


def export(items=None, rules=None):
    return {'zabbix_export': {'version': '8.0', 'templates': [{
        'template': 'Proxmox VE by HTTP', 'items': items or [], 'discovery_rules': rules or []}]}}


class ProxmoxTemplateScopeTests(unittest.TestCase):
    def test_stops_user_collector_and_guest_discovery_before_response_filters(self):
        guest_rule = {
            'key': 'proxmox_ve.node.qemu.network.discovery[{#NODE.ID},{#QEMU.VMID}]',
            'type': 'HTTP_AGENT', 'uuid': 'guest-rule',
            'parent_discovery_rule': {'key': 'proxmox_ve.node.qemu.discovery[{#NODE.ID}]'},
            'item_prototypes': [{'key': 'guest.child', 'type': 'HTTP_AGENT'}]}
        result = proxmox.basic_proxmox_template(export(
            items=[{'key': 'proxmox_ve.get_user_data', 'type': 'HTTP_AGENT'}], rules=[guest_rule]))
        template = result['zabbix_export']['templates'][0]
        self.assertEqual(template['items'][0]['status'], 'DISABLED')
        rule = template['discovery_rules'][0]
        self.assertEqual((rule['status'], rule['discover']), ('DISABLED', 'NO_DISCOVER'))
        self.assertEqual(rule['uuid'], 'guest-rule')
        self.assertEqual(rule['parent_discovery_rule'], guest_rule['parent_discovery_rule'])
        self.assertEqual(rule['item_prototypes'], guest_rule['item_prototypes'])

    def test_preserves_core_identity_and_stops_optional_prototypes_and_alerts(self):
        core = {'key': 'proxmox_ve.node.qemu[{#NODE.ID},{#QEMU.VMID}]',
                'type': 'HTTP_AGENT', 'uuid': 'core', 'verify_peer': 'YES', 'verify_host': 'YES'}
        optional = {'key': 'proxmox_ve.node.qemu.agent.version[{#NODE.ID},{#QEMU.VMID}]',
                    'type': 'HTTP_AGENT', 'uuid': 'optional',
                    'trigger_prototypes': [{'uuid': 'alert', 'expression': 'unchanged'}]}
        source = export(rules=[{'key': 'proxmox_ve.node.qemu.discovery[{#NODE.ID}]',
                                'item_prototypes': [core, optional], 'overrides': [{'step': '1'}]}])
        original = deepcopy(source)
        result = proxmox.basic_proxmox_template(source)
        rule = result['zabbix_export']['templates'][0]['discovery_rules'][0]
        self.assertEqual(rule['item_prototypes'][0], core)
        disabled = rule['item_prototypes'][1]
        self.assertEqual((disabled['status'], disabled['discover']), ('DISABLED', 'NO_DISCOVER'))
        self.assertEqual(disabled['trigger_prototypes'], [
            {'uuid': 'alert', 'expression': 'unchanged', 'status': 'DISABLED', 'discover': 'NO_DISCOVER'}])
        self.assertEqual(rule['overrides'], [{'step': '1'}])
        self.assertEqual(source, original)
        self.assertEqual(proxmox.basic_proxmox_template(result), result)

    def test_unknown_enabled_collector_requires_review(self):
        with self.assertRaisesRegex(ValueError, 'Unexpected Proxmox API collector'):
            proxmox.basic_proxmox_template(export(items=[{'key': 'future.api', 'type': 'HTTP_AGENT'}]))

    def test_stops_local_clock_that_depends_on_excluded_time_collector(self):
        master = 'proxmox_ve.node.time.info[{#NODE.ID}]'
        source = export(rules=[{'key': 'proxmox_ve.nodes.discovery', 'item_prototypes': [
            {'key': master, 'type': 'HTTP_AGENT'},
            {'key': 'proxmox_ve.node.localtime[{#NODE.ID}]', 'type': 'DEPENDENT',
             'master_item': {'key': master}}]}])
        result = proxmox.basic_proxmox_template(source)
        prototypes = result['zabbix_export']['templates'][0]['discovery_rules'][0]['item_prototypes']
        self.assertTrue(all(item['discover'] == 'NO_DISCOVER' for item in prototypes))
        self.assertTrue(all(item['status'] == 'DISABLED' for item in prototypes))
        self.assertEqual(prototypes[1]['master_item'], {'key': master})

    def test_rejects_another_template(self):
        source = export()
        source['zabbix_export']['templates'][0]['template'] = 'Another template'
        with self.assertRaisesRegex(ValueError, 'Expected the packaged Proxmox'):
            proxmox.basic_proxmox_template(source)


if __name__ == '__main__':
    unittest.main()

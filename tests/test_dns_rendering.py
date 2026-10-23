"""Check custom DNS record expansion and validation with synthetic inputs."""

import unittest

from ansible.errors import AnsibleFilterError

from filter_plugins.network import network_reverse_zones, network_stub_zones
from roles.dns_authoritative.filter_plugins.static_records import dns_authoritative_static_zones


class DnsRecordTests(unittest.TestCase):
    def prepare(self, records):
        zones = [
            {'name': 'srv.example.internal', 'managed': True, 'inventory_records': 'server_identity'},
            *network_reverse_zones(['192.0.2.0/24']),
        ]
        hosts = {'managed': {
            'network_primary_fqdn': 'managed.srv.example.internal', 'ansible_host': '192.0.2.10',
        }}
        return dns_authoritative_static_zones(zones, records, list(hosts), hosts, 'srv.example.internal')

    def test_normalizes_forward_records_and_adds_ptr_only_when_requested(self):
        records = [
            {'name': 'Storage', 'type': 'A', 'value': '192.0.2.35', 'ttl': 600, 'ptr': True},
            {'name': 'nas', 'type': 'CNAME', 'value': 'Storage.srv.example.internal.'},
            {'name': 'other', 'type': 'A', 'value': '192.0.2.36'},
        ]
        zones = {zone['name']: zone for zone in self.prepare({'srv.example.internal': records})}
        self.assertEqual(zones['srv.example.internal']['records'], [
            {'name': 'nas', 'type': 'CNAME', 'value': 'storage.srv.example.internal.'},
            {'name': 'other', 'type': 'A', 'value': '192.0.2.36'},
            {'name': 'storage', 'type': 'A', 'value': '192.0.2.35', 'ttl': 600},
        ])
        self.assertEqual(zones['2.0.192.in-addr.arpa']['records'], [
            {'name': '35', 'type': 'PTR', 'value': 'storage.srv.example.internal.', 'ttl': 600},
        ])

    def test_rejects_inventory_runtime_and_duplicate_owner_conflicts(self):
        cases = [
            [{'name': 'managed', 'type': 'A', 'value': '192.0.2.35'}],
            [{'name': 'mysql-shared-primary', 'type': 'A', 'value': '192.0.2.35'}],
            [{'name': '@', 'type': 'CNAME', 'value': 'other.example.internal.'}],
            [{'name': 'same', 'type': 'A', 'value': '192.0.2.35'}] * 2,
            [{'name': name, 'type': 'A', 'value': '192.0.2.35', 'ptr': True} for name in ('one', 'two')],
        ]
        for records in cases:
            with self.subTest(records=records), self.assertRaises(AnsibleFilterError):
                self.prepare({'srv.example.internal': records})

    def test_rejects_invalid_definitions(self):
        good = {'name': 'node', 'type': 'A', 'value': '192.0.2.35'}
        invalid = [
            None, {'unknown.internal': []}, {'srv.example.internal': {}},
            {'srv.example.internal': [{'name': 'node', 'type': 'A'}]},
        ]
        for changes in (
            {'name': 'bad name'}, {'value': '999.0.2.35'}, {'ttl': True},
            {'type': 'CNAME', 'value': 'node.example.internal'},
            {'ptr': 'true'}, {'value': '198.51.100.35', 'ptr': True},
        ):
            invalid.append({'srv.example.internal': [good | changes]})
        for definition in invalid:
            with self.subTest(definition=definition), self.assertRaises(AnsibleFilterError):
                self.prepare(definition)

    def test_input_order_does_not_change_expanded_records(self):
        records = [
            {'name': 'one', 'type': 'A', 'value': '192.0.2.35', 'ptr': True},
            {'name': 'two', 'type': 'A', 'value': '192.0.2.36', 'ptr': True},
        ]
        self.assertEqual(
            self.prepare({'srv.example.internal': records}),
            self.prepare({'srv.example.internal': list(reversed(records))}),
        )


class ReverseZoneTests(unittest.TestCase):
    def test_cidr_expansion_deduplicates_overlapping_networks(self):
        zones = network_reverse_zones(['192.0.4.0/22', '192.0.5.0/24'])
        names = [f'{octet}.0.192.in-addr.arpa' for octet in range(4, 8)]
        self.assertEqual([zone['name'] for zone in zones], names)
        self.assertEqual(network_stub_zones(names, ['192.0.2.53']), [
            {'name': name, 'servers': ['192.0.2.53']} for name in names
        ])

    def test_unaligned_or_unsupported_networks_fail(self):
        for cidr in ['192.0.5.0/22', '192.0.2.0/25', '2001:db8::/32', 'invalid']:
            with self.subTest(cidr=cidr), self.assertRaises(AnsibleFilterError):
                network_reverse_zones([cidr])

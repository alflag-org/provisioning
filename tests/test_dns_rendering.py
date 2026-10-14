from pathlib import Path
import re
from ipaddress import IPv4Address

from ansible.errors import AnsibleFilterError
from filter_plugins.network import network_reverse_zones, network_stub_zones
import unittest

from jinja2 import Environment, StrictUndefined
from ansible_support import AnsibleTestCase
from roles.dns_authoritative.filter_plugins.static_records import dns_authoritative_static_zones
from roles.dns_authoritative.filter_plugins.soa_serial import dns_authoritative_soa_serial


ROOT = Path(__file__).resolve().parents[1]


def regex_replace(value, pattern, replacement):
    return re.sub(pattern, replacement, value)


class DnsRenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        environment = Environment(undefined=StrictUndefined, autoescape=False)
        environment.filters["regex_replace"] = regex_replace
        environment.filters["regex_escape"] = re.escape
        cls.template = environment.from_string(
            (ROOT / "roles/dns_authoritative/templates/zone.j2").read_text()
        )

    def render(self, zone, hosts=None):
        return self.template.render(
            dns_authoritative_zone=zone,
            dns_authoritative_zone_serial=101,
            dns_authoritative_default_ttl=300,
            dns_authoritative_soa={
                "mname": "dns-authoritative01.srv.alflag.internal.",
                "rname": "hostmaster.alflag.internal.",
                "refresh": 3600,
                "retry": 900,
                "expire": 604800,
                "minimum": 300,
            },
            dns_authoritative_nameservers=[
                "dns-authoritative01.srv.alflag.internal.",
                "dns-authoritative02.srv.alflag.internal.",
            ],
            dns_authoritative_record_source_group="default",
            groups={"default": list(hosts) if hosts is not None else ["mysql-shared01", "mysql-shared02"]},
            hostvars=hosts if hosts is not None else {
                "mysql-shared01": {
                    "network_primary_fqdn": "mysql-shared01.srv.alflag.internal",
                    "network_ipv4_address": "10.10.20.221",
                },
                "mysql-shared02": {
                    "network_primary_fqdn": "mysql-shared02.srv.alflag.internal",
                    "network_ipv4_address": "10.10.20.222",
                },
            },
        )

    def test_stable_node_records_and_runtime_fragment_are_both_rendered(self):
        rendered = self.render(
            {
                "name": "srv.alflag.internal",
                "managed": True,
                "inventory_records": "server_identity",
                "runtime_record_files": ["/etc/nsd/runtime/mysql-role-records.zone"],
            }
        )
        self.assertIn("mysql-shared01 IN A 10.10.20.221", rendered)
        self.assertIn("mysql-shared02 IN A 10.10.20.222", rendered)
        self.assertIn("$INCLUDE /etc/nsd/runtime/mysql-role-records.zone", rendered)

    def test_reverse_queries_use_reversed_network_octets(self):
        hosts = {
            name: {"network_primary_fqdn": f"{name}.srv.alflag.internal", "ansible_host": ip}
            for name, ip in {
                "connector01": "10.10.20.41",
                "workbench01": "10.10.20.61",
                "mysql-shared01": "10.10.20.221",
                "dns-recursive01": "10.10.20.240",
                "dns-authoritative01": "10.10.20.242",
                "next": "10.10.21.41",
                "another": "10.10.22.41",
                "last": "10.10.23.41",
                "outside": "10.10.24.41",
                "physical": "10.10.10.11",
            }.items()
        }
        answers = {}
        for zone in network_reverse_zones(["10.10.20.0/22"]):
            rendered = self.render(zone, hosts)
            for owner, target in re.findall(r"^(\d+) IN PTR (\S+)$", rendered, re.M):
                query = owner + "." + zone["name"]
                self.assertNotIn(query, answers)
                answers[query] = target
        for host, values in hosts.items():
            query = IPv4Address(values["ansible_host"]).reverse_pointer
            if host in ("outside", "physical"):
                self.assertNotIn(query, answers)
            else:
                self.assertEqual(answers[query], values["network_primary_fqdn"] + ".")
        mgmt = self.render(network_reverse_zones(["10.10.10.0/24"])[0], hosts)
        self.assertIn("11 IN PTR physical.srv.alflag.internal.", mgmt)
        self.assertNotIn("41 IN PTR", mgmt)

    def test_only_members_of_record_source_group_are_published(self):
        zone = {"name": "srv.alflag.internal", "inventory_records": "server_identity"}
        self.assertNotIn(" IN A ", self.render(zone, {}))

    def prepare(self, static=None, hosts=None, zones=None):
        if hosts is None:
            hosts = {
                "managed": {
                    "network_primary_fqdn": "managed.srv.example.internal",
                    "ansible_host": "192.0.2.10",
                    "network_service_aliases": ["service.access.internal"],
                },
            }
        if zones is None:
            zones = [
                {"name": "example.internal", "managed": True},
                {
                    "name": "srv.example.internal", "managed": True,
                    "inventory_records": "server_identity",
                    "runtime_record_files": ["/etc/nsd/runtime/mysql-role-records.zone"],
                },
                {"name": "access.internal", "managed": True, "inventory_records": "access_aliases"},
                *network_reverse_zones(["192.0.2.0/24"]),
            ]
        prepared = dns_authoritative_static_zones(
            zones, static or {}, list(hosts), hosts, "srv.example.internal",
        )
        return {zone["name"]: zone for zone in prepared}, hosts

    def test_static_forward_records_and_ttl(self):
        zones, hosts = self.prepare({
            "srv.example.internal": [{"name": "storage100", "type": "A", "value": "192.0.2.35", "ttl": 600}],
            "access.internal": [{"name": "nas", "type": "CNAME", "value": "storage100.srv.example.internal."}],
        })
        self.assertIn("storage100 600 IN A 192.0.2.35", self.render(zones["srv.example.internal"], hosts))
        self.assertIn("nas IN CNAME storage100.srv.example.internal.", self.render(zones["access.internal"], hosts))
        self.assertIn("$TTL 300", self.render(zones["access.internal"], hosts))
        self.assertNotIn("35 IN PTR", self.render(zones["2.0.192.in-addr.arpa"], hosts))

    def test_optional_ptr_targets_its_a_record_and_inherits_ttl(self):
        for ptr in (True, False, None):
            record = {"name": "storage100", "type": "A", "value": "192.0.2.35", "ttl": 600}
            if ptr is not None:
                record["ptr"] = ptr
            zones, hosts = self.prepare({"srv.example.internal": [record]})
            self.assertIn("storage100 600 IN A 192.0.2.35", self.render(zones["srv.example.internal"], hosts))
            reverse = self.render(zones["2.0.192.in-addr.arpa"], hosts)
            if ptr is True:
                self.assertIn("35 600 IN PTR storage100.srv.example.internal.", reverse)
            else:
                self.assertNotIn("35 ", reverse)

    def test_ptr_selects_each_reverse_zone_of_a_larger_cidr(self):
        zones = [{"name": "example.internal", "managed": True}, *network_reverse_zones(["192.0.4.0/22"])]
        records = [
            {"name": f"node{octet}", "type": "A", "value": f"192.0.{octet}.35", "ptr": True}
            for octet in range(4, 8)
        ]
        prepared, hosts = self.prepare({"example.internal": records}, hosts={}, zones=zones)
        for octet in range(4, 8):
            self.assertIn(f"35 IN PTR node{octet}.example.internal.", self.render(prepared[f"{octet}.0.192.in-addr.arpa"], hosts))

    def test_conflicting_records_fail(self):
        cases = [
            ("srv.example.internal", [{"name": "managed", "type": "A", "value": "192.0.2.35"}]),
            ("access.internal", [{"name": "service", "type": "CNAME", "value": "other.example.internal."}]),
            ("srv.example.internal", [{"name": "other", "type": "A", "value": "192.0.2.10", "ptr": True}]),
            ("srv.example.internal", [{"name": "same", "type": "A", "value": "192.0.2.35"}] * 2),
            ("srv.example.internal", [
                {"name": "same", "type": "A", "value": "192.0.2.35"},
                {"name": "SAME", "type": "CNAME", "value": "other.example.internal."},
            ]),
            ("srv.example.internal", [
                {"name": "one", "type": "A", "value": "192.0.2.35", "ptr": True},
                {"name": "two", "type": "A", "value": "192.0.2.35", "ptr": True},
            ]),
            ("srv.example.internal", [{"name": "mysql-shared-primary", "type": "A", "value": "192.0.2.35"}]),
            ("srv.example.internal", [{"name": "mysql-shared-replica", "type": "CNAME", "value": "other.example.internal."}]),
            ("example.internal", [{"name": "@", "type": "CNAME", "value": "other.example.internal."}]),
            ("example.internal", [{"name": "srv", "type": "CNAME", "value": "other.example.internal."}]),
        ]
        for name, records in cases:
            with self.subTest(records=records), self.assertRaisesRegex(AnsibleFilterError, "conflict|reserved|coexist"):
                self.prepare({name: records})

    def test_invalid_static_definitions_fail(self):
        good = {"name": "node", "type": "A", "value": "192.0.2.35"}
        invalid = [
            {"unknown.internal": []},
            {"2.0.192.in-addr.arpa": []},
            {"srv.example.internal": {}},
            {"srv.example.internal": [None]},
            {"srv.example.internal": [{"name": "node", "type": "A"}]},
        ]
        overrides = [
            {"type": "AAAA"}, {"type": "PTR"}, {"extra": True},
            {"name": "node."}, {"name": "bad name"}, {"name": "-node"}, {"name": "a" * 64},
            {"name": "a." * 127 + "a"}, {"value": "999.0.2.35"}, {"value": "::1"}, {"value": 123},
            {"ttl": -1}, {"ttl": 1 << 31}, {"ttl": "600"}, {"ttl": 1.5}, {"ttl": True},
            {"ptr": "true"}, {"ptr": 1},
            {"type": "CNAME", "value": "node.example.internal"},
            {"type": "CNAME", "value": "bad..example.internal."},
            {"type": "CNAME", "value": "node.example.internal.", "ptr": True},
            {"value": "198.51.100.35", "ptr": True},
        ]
        invalid += [{"srv.example.internal": [{**good, **override}]} for override in overrides]
        for definition in invalid:
            with self.subTest(definition=definition), self.assertRaises(AnsibleFilterError):
                self.prepare(definition)

    def test_static_record_collection_requires_a_mapping(self):
        for definition in (None, [], "invalid"):
            with self.subTest(definition=definition), self.assertRaisesRegex(AnsibleFilterError, "mapping"):
                dns_authoritative_static_zones([], definition, [], {}, "srv.example.internal")

    def test_empty_static_definitions_preserve_rendered_content(self):
        zones, hosts = self.prepare()
        for zone in zones.values():
            original = {key: value for key, value in zone.items() if key != "records"}
            self.assertEqual(self.render(zone, hosts), self.render(original, hosts))

    def test_existing_explicit_record_migration_preserves_content(self):
        record = {"name": "physical", "type": "A", "value": "192.0.2.11"}
        original = {"name": "access.internal", "managed": True, "inventory_records": "access_aliases", "records": [record]}
        zones, hosts = self.prepare({"access.internal": [record]})
        self.assertEqual(self.render(original, hosts), self.render(zones["access.internal"], hosts))

    def test_order_does_not_change_content_or_serial_but_record_changes_do(self):
        records = [
            {"name": "one", "type": "A", "value": "192.0.2.35", "ptr": True},
            {"name": "two", "type": "A", "value": "192.0.2.36", "ptr": True},
        ]
        first, hosts = self.prepare({"srv.example.internal": records})
        reordered, _ = self.prepare({"srv.example.internal": list(reversed(records))})
        changed, _ = self.prepare({"srv.example.internal": [{**records[0], "value": "192.0.2.37"}, records[1]]})
        removed, _ = self.prepare()
        for name in ("srv.example.internal", "2.0.192.in-addr.arpa"):
            existing = self.render(first[name], hosts)
            identical = self.render(reordered[name], hosts)
            self.assertEqual(existing, identical)
            self.assertEqual(dns_authoritative_soa_serial(existing, identical), 101)
            self.assertEqual(dns_authoritative_soa_serial(existing, self.render(changed[name], hosts)), 102)
            self.assertEqual(dns_authoritative_soa_serial(existing, self.render(removed[name], hosts)), 102)


class DnsPreflightTests(AnsibleTestCase):
    def test_invalid_definition_stops_role_before_remote_changes(self):
        playbook = self.directory / "preflight.yml"
        playbook.write_text("""---
- hosts: default
  gather_facts: false
  roles:
    - dns_authoritative
""")
        config_dir = self.directory / "nsd"
        result = self.run_playbook(playbook, variables={
            "dns_authoritative_enabled": True,
            "dns_authoritative_config_dir": str(config_dir),
            "dns_authoritative_zones": [{"name": "example.internal", "managed": True}],
            "dns_static_records": {"unknown.internal": []},
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown managed forward zone", result.stdout + result.stderr)
        self.assertFalse(config_dir.exists())
        self.assertNotIn("TASK [dns_authoritative : Install authoritative DNS package]", result.stdout)


class ReverseZoneTests(unittest.TestCase):
    def test_cidr_expansion_and_overlap(self):
        zones = network_reverse_zones(["192.0.4.0/22", "192.0.5.0/24"])
        self.assertEqual([zone["name"] for zone in zones], [
            f"{octet}.0.192.in-addr.arpa" for octet in range(4, 8)
        ])
        stubs = network_stub_zones([zone["name"] for zone in zones], ["192.0.2.53"])
        self.assertEqual([stub["name"] for stub in stubs], [zone["name"] for zone in zones])
        self.assertTrue(all(stub["servers"] == ["192.0.2.53"] for stub in stubs))

    def test_unaligned_or_unsupported_networks_fail(self):
        for cidr in ["192.0.5.0/22", "192.0.2.0/25", "2001:db8::/32", "invalid"]:
            with self.subTest(cidr=cidr), self.assertRaises(AnsibleFilterError):
                network_reverse_zones([cidr])


if __name__ == "__main__":
    unittest.main()

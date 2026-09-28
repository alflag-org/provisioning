from pathlib import Path
import re
from ipaddress import IPv4Address

from ansible.errors import AnsibleFilterError
from filter_plugins.network import network_reverse_zones, network_stub_zones
import unittest

from jinja2 import Environment, StrictUndefined


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

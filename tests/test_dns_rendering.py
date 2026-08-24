from pathlib import Path
import re
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

    def render(self, zone):
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
            groups={"default": ["mysql-shared01", "mysql-shared02"]},
            hostvars={
                "mysql-shared01": {
                    "network_primary_fqdn": "mysql-shared01.srv.alflag.internal",
                    "network_ipv4_address": "10.10.10.221",
                },
                "mysql-shared02": {
                    "network_primary_fqdn": "mysql-shared02.srv.alflag.internal",
                    "network_ipv4_address": "10.10.10.222",
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
        self.assertIn("mysql-shared01 IN A 10.10.10.221", rendered)
        self.assertIn("mysql-shared02 IN A 10.10.10.222", rendered)
        self.assertIn("$INCLUDE /etc/nsd/runtime/mysql-role-records.zone", rendered)


if __name__ == "__main__":
    unittest.main()

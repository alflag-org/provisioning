import unittest

from ansible.errors import AnsibleFilterError

from roles.dns_authoritative.filter_plugins.soa_serial import (
    dns_authoritative_soa_serial,
)


def zone(serial, address="192.0.2.1"):
    return (
        "; managed zone\n"
        "@ IN SOA ns.example. hostmaster.example. (\n"
        f"  {serial} ; serial\n"
        ")\n"
        f"host IN A {address}\n"
    )


class DnsAuthoritativeSoaSerialTests(unittest.TestCase):
    def test_new_zone_uses_initial_serial(self):
        self.assertEqual(dns_authoritative_soa_serial(None, zone(1), 42), 42)

    def test_unchanged_content_preserves_existing_serial(self):
        self.assertEqual(dns_authoritative_soa_serial(zone(19), zone(1)), 19)

    def test_changed_content_advances_once(self):
        self.assertEqual(
            dns_authoritative_soa_serial(zone(19), zone(1, "192.0.2.2")),
            20,
        )

    def test_serial_wraps_as_rfc_1982_sequence_space(self):
        previous = (1 << 32) - 1
        current = dns_authoritative_soa_serial(zone(previous), zone(1, "192.0.2.2"))

        self.assertEqual(current, 0)
        self.assertEqual((current - previous) % (1 << 32), 1)

    def test_invalid_existing_serial_fails(self):
        with self.assertRaises(AnsibleFilterError):
            dns_authoritative_soa_serial(zone(1 << 32), zone(1))

    def test_non_integer_initial_serial_fails(self):
        with self.assertRaises(AnsibleFilterError):
            dns_authoritative_soa_serial(None, zone(1), 1.5)

    def test_missing_existing_serial_fails(self):
        with self.assertRaises(AnsibleFilterError):
            dns_authoritative_soa_serial("host IN A 192.0.2.1\n", zone(1))


if __name__ == "__main__":
    unittest.main()

import json
import subprocess
import unittest

from ansible_support import ROOT, ansible_executable


class InventoryTests(unittest.TestCase):
    def test_managed_host_addresses_are_unique(self):
        inventories = sorted((ROOT / "inventories").rglob("hosts.y*ml"))
        self.assertTrue(inventories, "No inventories found")
        for inventory in inventories:
            with self.subTest(inventory=inventory.relative_to(ROOT)):
                result = subprocess.run([
                    ansible_executable("ansible-inventory"), "-i", str(inventory), "--list",
                ], cwd=ROOT, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                hosts = json.loads(result.stdout)["_meta"]["hostvars"]
                self.assertTrue(hosts, "Inventory contains no hosts")
                addresses = {}
                for host, variables in hosts.items():
                    address = variables.get("ansible_host")
                    if address is not None:
                        self.assertNotIn(address, addresses, f"{host} duplicates {addresses.get(address)} at {address}")
                        addresses[address] = host


if __name__ == "__main__":
    unittest.main()

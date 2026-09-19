from pathlib import Path
import unittest

from ansible.inventory.manager import InventoryManager
from ansible.parsing.dataloader import DataLoader
from ansible.vars.manager import VariableManager


ROOT = Path(__file__).resolve().parents[1]


class InventoryTests(unittest.TestCase):
    def test_host_addresses_are_unique(self):
        sources = sorted(
            path for path in (ROOT / "inventories").rglob("hosts.*")
            if path.suffix in {".yml", ".yaml"}
        )
        self.assertTrue(sources, "No inventories found")
        for source in sources:
            with self.subTest(inventory=source.relative_to(ROOT)):
                loader = DataLoader()
                try:
                    inventory = InventoryManager(loader=loader, sources=[str(source)])
                    variables = VariableManager(loader=loader, inventory=inventory)
                    hosts = inventory.get_hosts("all")
                    self.assertTrue(hosts, "No hosts found")
                    owners = {}
                    for host in hosts:
                        address = variables.get_vars(host=host).get("ansible_host", host.name)
                        self.assertNotIn(
                            address, owners,
                            f"{host.name}: {address} already used by {owners.get(address)}",
                        )
                        owners[address] = host.name
                finally:
                    loader.cleanup_all_tmp_files()


if __name__ == "__main__":
    unittest.main()

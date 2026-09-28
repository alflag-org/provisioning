"""Render deployed-service inputs without connecting to any inventory host."""

import ipaddress
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from ansible_support import ROOT, ansible_executable


def render_inventory(directory):
    output = Path(directory)
    tasks = [{
        "name": "Render network inputs on the controller",
        "ansible.builtin.copy": {
            "content": "{{ {'address': network_ipv4_address, 'interface': network_address, "
                       "'cidr': network_cidr, 'gateway': network_gateway, "
                       "'resolvers': network_dns_resolvers, 'fqdn': network_primary_fqdn} | to_json }}",
            "dest": str(output / "{{ inventory_hostname }}.json"), "mode": "0600",
        },
        "delegate_to": "localhost", "become": False,
    }]
    for role, condition, expression in [
        ("dns_authoritative", "'svc_dns_authoritative' in group_names",
         "{'config': lookup('template', role_path ~ '/templates/nsd.conf.j2'), "
         "'zones': dns_authoritative_zones}"),
        ("dns_recursor", "'svc_dns_recursive' in group_names",
         "{'config': lookup('template', role_path ~ '/templates/alflag-recursive.conf.j2')}"),
        ("systemd_resolved", "'platform_vm' in group_names",
         "{'config': lookup('template', role_path ~ '/templates/alflag.conf.j2')}"),
        ("components/mysql_server", "'svc_mysql' in group_names",
         "{'config': lookup('template', role_path ~ '/templates/mysqld.cnf.j2'), "
         "'replication_cidr': mysql_replicaset_replication_allowed_host}"),
    ]:
        name = role.split('/')[-1]
        render_role = output / "roles" / name
        for part in ("defaults", "templates", "filter_plugins"):
            source = ROOT / "roles" / role / part
            if source.exists():
                shutil.copytree(source, render_role / part)
        (render_role / "tasks").mkdir()
        fixture = render_role / "tasks/main.yml"
        render_tasks = [{
            "name": "Render service inputs on the controller",
            "ansible.builtin.copy": {
                "content": "{{ " + expression + " | to_json }}",
                "dest": str(output / ("{{ inventory_hostname }}." + name + ".json")),
                "mode": "0600",
            },
            "delegate_to": "localhost", "become": False,
        }]
        if name == "dns_authoritative":
            render_tasks.append({
                "name": "Render authoritative zones on the controller",
                "ansible.builtin.template": {
                    "src": "zone.j2",
                    "dest": str(output / "{{ inventory_hostname }}.{{ item.name }}.zone"),
                    "mode": "0600",
                },
                "vars": {"dns_authoritative_zone": "{{ item }}", "dns_authoritative_zone_serial": 1},
                "loop": "{{ dns_authoritative_zones }}",
                "delegate_to": "localhost", "become": False,
            })
        fixture.write_text(yaml.safe_dump(render_tasks))
        tasks.append({
            "name": f"Render {name}",
            "ansible.builtin.include_role": {"name": str(render_role)},
            "when": condition,
        })
    playbook = output / "render.yml"
    playbook.write_text(yaml.safe_dump([{
        "name": "Render inventory without remote execution", "hosts": "default",
        "gather_facts": False, "tasks": tasks,
    }]))
    result = subprocess.run([
        ansible_executable("ansible-playbook"), str(playbook),
        "-i", str(ROOT / "inventories/default/hosts.yml"),
        "-e", json.dumps({"ansible_python_interpreter": sys.executable}),
    ], cwd=ROOT, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)


class InventoryRenderingTests(unittest.TestCase):
    def test_service_configuration_uses_inventory_networks(self):
        with tempfile.TemporaryDirectory() as directory:
            render_inventory(directory)
            output = Path(directory)
            files = list(output.glob("*.json"))
            self.assertTrue(files)
            for path in files:
                data = json.loads(path.read_text())
                if path.name.count('.') == 1:
                    interface = ipaddress.ip_interface(data["interface"])
                    self.assertEqual(str(interface.ip), data["address"])
                    self.assertEqual(str(interface.network), data["cidr"])
                    self.assertIn(ipaddress.ip_address(data["gateway"]), interface.network)
                    continue
                host = path.name.split('.')[0]
                network = json.loads((output / f"{host}.json").read_text())
                config = data["config"]
                if "dns_authoritative" in path.name:
                    self.assertIn(f'ip-address: {network["address"]}', config)
                    self.assertEqual(config.count('\nzone:\n'), len(data["zones"]))
                    self.assertNotIn('include:', config)
                elif "dns_recursor" in path.name:
                    self.assertIn(f'interface: {network["address"]}', config)
                    self.assertIn(f'access-control: {network["cidr"]} allow', config)
                    self.assertNotIn('include:', config)
                    authoritative = next(output.glob("*.dns_authoritative.json"))
                    zones = json.loads(authoritative.read_text())["zones"]
                    for zone in zones:
                        if zone.get("inventory_records") == "reverse_ptr":
                            self.assertIn(f'local-zone: "{zone["name"]}" transparent', config)
                            self.assertIn(f'name: "{zone["name"]}"', config)
                    for authoritative in output.glob("*.dns_authoritative.json"):
                        source = json.loads((output / (authoritative.name.split('.')[0] + '.json')).read_text())
                        self.assertIn(f'stub-addr: {source["address"]}', config)
                elif "systemd_resolved" in path.name:
                    self.assertIn('DNS=' + ' '.join(network["resolvers"]), config)
                elif "mysql_server" in path.name:
                    self.assertIn(f'bind-address = {network["address"]}', config)
                    self.assertEqual(data["replication_cidr"], network["cidr"])

            for authoritative in output.glob("*.dns_authoritative.json"):
                server = authoritative.name.split('.')[0]
                forward = (output / f"{server}.srv.alflag.internal.zone").read_text()
                for network_path in files:
                    if network_path.name.count('.') != 1:
                        continue
                    network = json.loads(network_path.read_text())
                    fqdn, address = network["fqdn"], network["address"]
                    self.assertIn(f"{fqdn.removesuffix('.srv.alflag.internal')} IN A {address}", forward)
                    owner, zone = ipaddress.IPv4Address(address).reverse_pointer.split('.', 1)
                    reverse = (output / f"{server}.{zone}.zone").read_text()
                    self.assertIn(f"{owner} IN PTR {fqdn}.", reverse)


if __name__ == "__main__":
    unittest.main()

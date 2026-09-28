"""DNS views of inventory networks."""

from ipaddress import IPv4Network

from ansible.errors import AnsibleFilterError


def network_reverse_zones(cidrs):
    """Expand IPv4 networks into octet-aligned /24 reverse zones."""
    zones = []
    for cidr in cidrs:
        try:
            network = IPv4Network(cidr)
        except ValueError as exc:
            raise AnsibleFilterError(str(exc)) from exc
        if not 16 <= network.prefixlen <= 24:
            raise AnsibleFilterError("Reverse zone generation requires IPv4 /16 through /24")
        for subnet in network.subnets(new_prefix=24):
            prefix = str(subnet.network_address).split('.')[:3]
            name = '.'.join(reversed(prefix)) + '.in-addr.arpa'
            zone = {"name": name, "managed": True, "inventory_records": "reverse_ptr"}
            if zone not in zones:
                zones.append(zone)
    return zones


def network_stub_zones(names, servers):
    return [{"name": name, "servers": list(servers)} for name in names]


class FilterModule:
    def filters(self):
        return {
            "network_reverse_zones": network_reverse_zones,
            "network_stub_zones": network_stub_zones,
        }

"""Validate static DNS records against managed zones and inventory records."""

from collections.abc import Mapping
from ipaddress import IPv4Address
import re

from ansible.errors import AnsibleFilterError


_LABEL = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\Z")


def _dns_name(value, source, absolute=False):
    if not isinstance(value, str) or (absolute and not value.endswith(".")):
        raise AnsibleFilterError(f"{source} must be a DNS name" + (" ending in '.'" if absolute else ""))
    name = value[:-1] if absolute else value
    if len(name) > 253 or not all(_LABEL.fullmatch(label) for label in name.split(".")):
        raise AnsibleFilterError(f"{source} is not a valid DNS name: {value!r}")
    return name.lower()


def _ipv4(value, source):
    if not isinstance(value, str):
        raise AnsibleFilterError(f"{source} must be an IPv4 address")
    try:
        return IPv4Address(value)
    except ValueError as exc:
        raise AnsibleFilterError(f"{source} must be an IPv4 address: {value!r}") from exc


def _ttl(value, source):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < (1 << 31):
        raise AnsibleFilterError(f"{source} must be an integer TTL from 0 through 2147483647")


def _inventory_records(zone, source_hosts, hostvars):
    """Match the existing zone template's inventory selection, without changing its output."""
    mode = zone.get("inventory_records")
    suffix = "." + zone["name"]
    for source_host in sorted(source_hosts):
        host = hostvars[source_host]
        fqdn = host.get("network_primary_fqdn", "").rstrip(".")
        address = host.get("network_ipv4_address", host.get("ansible_host", ""))
        if mode == "server_identity" and fqdn.endswith(suffix) and address:
            yield {"name": fqdn[:-len(suffix)], "type": "A", "value": address}
        elif mode == "access_aliases" and address:
            for alias in sorted(host.get("network_service_aliases", [])):
                alias = alias.rstrip(".")
                if alias.endswith(suffix):
                    yield {"name": alias[:-len(suffix)], "type": "A", "value": address}
        elif mode == "reverse_ptr" and fqdn and address:
            parts = address.split(".")
            if len(parts) == 4 and ".".join(reversed(parts[:3])) + ".in-addr.arpa" == zone["name"]:
                yield {"name": parts[3], "type": "PTR", "value": fqdn + "."}


def dns_authoritative_static_zones(zones, static_records, source_hosts, hostvars, runtime_zone):
    """Merge static forward records and optional PTRs, failing before any remote mutation."""
    if not isinstance(static_records, Mapping):
        raise AnsibleFilterError("dns_static_records must be a mapping of zone names to record lists")
    result = []
    by_name = {}
    owners = {}
    runtime_zone = _dns_name(runtime_zone.rstrip("."), "MySQL runtime zone")
    # These owners are emitted by update-mysql-role-dns.py, even with no static declaration.
    runtime_owners = {f"mysql-shared-{role}.{runtime_zone}" for role in ("primary", "replica")}

    def claim(zone, record, source):
        name = record["name"]
        owner = zone["name"] if name == "@" else _dns_name(name + "." + zone["name"], source)
        if record["type"] == "CNAME" and owner in by_name:
            raise AnsibleFilterError(f"{source}: CNAME cannot coexist with the zone's SOA and NS records")
        if owner in runtime_owners:
            raise AnsibleFilterError(f"{source}: {owner} is reserved for runtime MySQL CNAME records")
        if owner in owners:
            raise AnsibleFilterError(f"{source}: duplicate or conflicting DNS owner {owner}; already used by {owners[owner]}")
        owners[owner] = source

    for original in zones:
        zone = {**original, "records": list(original.get("records", []))}
        name = _dns_name(zone["name"], "zone name")
        if name in by_name:
            raise AnsibleFilterError(f"Duplicate DNS zone: {name}")
        by_name[name] = zone
        result.append(zone)

    for zone in result:
        name = zone["name"]
        if not zone.get("managed", False):
            continue
        for record in zone["records"]:
            claim(zone, record, f"configured record in {name}")
        for record in _inventory_records(zone, source_hosts, hostvars):
            claim(zone, record, f"inventory record in {name}")

    for name, records in static_records.items():
        if name not in by_name or not by_name[name].get("managed", False) or name.endswith(".in-addr.arpa"):
            raise AnsibleFilterError(f"dns_static_records specifies an unknown managed forward zone: {name!r}")
        if not isinstance(records, list):
            raise AnsibleFilterError(f"dns_static_records[{name!r}] must be a list")
        zone = by_name[name]
        for record in records:
            source = f"static record in {name}: {record!r}"
            if not isinstance(record, Mapping):
                raise AnsibleFilterError(f"{source} must be a mapping")
            unknown = set(record) - {"name", "type", "value", "ttl", "ptr"}
            if unknown or not {"name", "type", "value"} <= set(record):
                raise AnsibleFilterError(f"{source} requires name/type/value and accepts only ttl/ptr as optional fields")
            kind = record["type"]
            if kind not in ("A", "CNAME"):
                raise AnsibleFilterError(f"{source}: only A and CNAME static records are supported")
            owner = record["name"]
            if owner != "@":
                owner = _dns_name(owner, source + " relative name")
            if "ttl" in record:
                _ttl(record["ttl"], source + " ttl")
            ptr = record.get("ptr", False)
            if not isinstance(ptr, bool) or (ptr and kind != "A"):
                raise AnsibleFilterError(f"{source}: ptr must be a boolean and ptr: true requires A")
            if kind == "A":
                value = str(_ipv4(record["value"], source))
            else:
                value = _dns_name(record["value"], source + " CNAME target", absolute=True) + "."
            rendered = {"name": owner, "type": kind, "value": value}
            if "ttl" in record:
                rendered["ttl"] = record["ttl"]
            claim(zone, rendered, source)
            zone["records"].append(rendered)
            if ptr:
                pointer = IPv4Address(value).reverse_pointer
                reverse_name = pointer.split(".", 1)[1]
                reverse_zone = by_name.get(reverse_name)
                if (
                    reverse_zone is None
                    or not reverse_zone.get("managed", False)
                    or reverse_zone.get("inventory_records") != "reverse_ptr"
                ):
                    raise AnsibleFilterError(f"{source}: PTR address is outside managed reverse zones")
                target = name if owner == "@" else owner + "." + name
                reverse_record = {"name": pointer.split(".", 1)[0], "type": "PTR", "value": target + "."}
                if "ttl" in rendered:
                    reverse_record["ttl"] = rendered["ttl"]
                claim(reverse_zone, reverse_record, source + " PTR")
                reverse_zone["records"].append(reverse_record)

    for zone in result:
        if zone.get("managed", False):
            zone["records"].sort(key=lambda record: (record["name"].lower(), record["type"], record["value"]))
    return result


class FilterModule:
    def filters(self):
        return {"dns_authoritative_static_zones": dns_authoritative_static_zones}

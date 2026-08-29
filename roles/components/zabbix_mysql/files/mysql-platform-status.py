#!/usr/bin/python3
import argparse
import json
import socket
import subprocess


QUERY = """
SELECT JSON_OBJECT(
  'hostname', @@hostname,
  'read_only', @@GLOBAL.read_only,
  'super_read_only', @@GLOBAL.super_read_only,
  'gtid_executed', @@GLOBAL.gtid_executed,
  'gtid_mode', @@GLOBAL.gtid_mode,
  'binlog_format', @@GLOBAL.binlog_format,
  'replica_receiver_state', COALESCE(
    (SELECT SERVICE_STATE FROM performance_schema.replication_connection_status LIMIT 1),
    'NONE'
  ),
  'replica_applier_state', COALESCE(
    (SELECT SERVICE_STATE FROM performance_schema.replication_applier_status LIMIT 1),
    'NONE'
  )
)
"""


def resolve_addresses(name):
    try:
        return sorted(
            {
                entry[4][0]
                for entry in socket.getaddrinfo(name, None, type=socket.SOCK_STREAM)
            }
        )
    except socket.gaierror:
        return []


def read_status(defaults_file, node_address, primary_alias, replica_alias):
    result = subprocess.run(
        [
            "/usr/bin/mysql",
            f"--defaults-extra-file={defaults_file}",
            "--batch",
            "--raw",
            "--skip-column-names",
            "--execute",
            QUERY,
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    status = json.loads(result.stdout.strip())
    required = {
        "hostname",
        "read_only",
        "super_read_only",
        "gtid_executed",
        "gtid_mode",
        "binlog_format",
        "replica_receiver_state",
        "replica_applier_state",
    }
    if required - status.keys():
        raise RuntimeError("MySQL platform status query returned an incomplete document")
    if status["read_only"] == 0 and status["super_read_only"] == 0:
        role = "PRIMARY"
    elif (
        status["read_only"] == 1
        and status["super_read_only"] == 1
        and status["replica_receiver_state"] == "ON"
        and status["replica_applier_state"] == "ON"
    ):
        role = "SECONDARY"
    else:
        role = "UNKNOWN"
    primary_addresses = resolve_addresses(primary_alias)
    replica_addresses = resolve_addresses(replica_alias)
    expected_addresses = {
        "PRIMARY": primary_addresses,
        "SECONDARY": replica_addresses,
    }.get(role, [])
    other_addresses = {
        "PRIMARY": replica_addresses,
        "SECONDARY": primary_addresses,
    }.get(role, [])
    status.update(
        {
            "runtime_role": role,
            "replication_ok": role in {"PRIMARY", "SECONDARY"},
            "backup_required": role == "SECONDARY",
            "dns_primary_addresses": primary_addresses,
            "dns_replica_addresses": replica_addresses,
            "dns_role_match": (
                len(primary_addresses) == 1
                and len(replica_addresses) == 1
                and primary_addresses != replica_addresses
                and node_address in expected_addresses
                and node_address not in other_addresses
            ),
            "gtid_enabled": status["gtid_mode"] == "ON",
            "row_binlog": status["binlog_format"] == "ROW",
        }
    )
    return status


def metric(status, name):
    if name == "json":
        return json.dumps(status, sort_keys=True, separators=(",", ":"))
    if name not in status:
        raise ValueError(f"unsupported MySQL platform status metric {name!r}")
    value = status[name]
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--defaults-file", required=True)
    parser.add_argument("--node-address", required=True)
    parser.add_argument("--primary-alias", required=True)
    parser.add_argument("--replica-alias", required=True)
    parser.add_argument("metric")
    args = parser.parse_args()
    status = read_status(
        args.defaults_file,
        args.node_address,
        args.primary_alias,
        args.replica_alias,
    )
    print(metric(status, args.metric))


if __name__ == "__main__":
    main()

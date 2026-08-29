#!/usr/bin/python3
import argparse
import json
from pathlib import Path
import subprocess


def query(defaults_file, port):
    result = subprocess.run(
        [
            "/usr/bin/mysql",
            f"--defaults-extra-file={defaults_file}",
            f"--port={port}",
            "--connect-timeout=2",
            "--batch",
            "--raw",
            "--skip-column-names",
            "--execute",
            "SELECT JSON_OBJECT('hostname', @@hostname, 'read_only', @@GLOBAL.read_only)",
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        return {"reachable": False, "error": result.stderr.strip()}
    return {"reachable": True, "backend": json.loads(result.stdout.strip())}


def status(defaults_file, state_file, ports):
    service = subprocess.run(
        ["/usr/bin/systemctl", "is-active", "mysqlrouter.service"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.strip()
    metadata_state = None
    state_path = Path(state_file)
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            metadata = state.get("metadata-cache", {})
            metadata_state = bool(metadata.get("cluster-metadata-servers"))
        except (json.JSONDecodeError, OSError):
            metadata_state = None
    endpoints = {name: query(defaults_file, port) for name, port in ports.items()}
    # A successful query through the read/write metadata route proves that the
    # cache can currently route traffic. Use the state document as an additional
    # negative signal when it is readable, but do not make monitoring depend on
    # bootstrap-private file permissions.
    metadata_cache = endpoints["rw"]["reachable"] and metadata_state is not False
    return {
        "service": service == "active",
        "metadata_cache": metadata_cache,
        "rw": endpoints["rw"],
        "ro": endpoints["ro"],
        "split": endpoints["split"],
    }


def metric(document, name):
    if name == "json":
        return json.dumps(document, sort_keys=True, separators=(",", ":"))
    if name in {"service", "metadata_cache"}:
        return int(document[name])
    if name in {"rw", "ro", "split"}:
        return int(document[name]["reachable"])
    raise ValueError(f"unsupported Router status metric {name!r}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--defaults-file", required=True)
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--rw-port", type=int, required=True)
    parser.add_argument("--ro-port", type=int, required=True)
    parser.add_argument("--split-port", type=int, required=True)
    parser.add_argument("metric")
    args = parser.parse_args()
    document = status(
        args.defaults_file,
        args.state_file,
        {"rw": args.rw_port, "ro": args.ro_port, "split": args.split_port},
    )
    print(metric(document, args.metric))


if __name__ == "__main__":
    main()

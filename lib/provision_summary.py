"""Validate and display execution metadata without importing Ansible."""

import json
import os
from pathlib import Path
import re
import stat

COUNTERS = ("ok", "changed", "failed", "unreachable", "skipped", "rescued", "ignored")
ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 8 * 1024 * 1024


class SummaryUnavailable(ValueError):
    """No complete, validated execution summary is available."""


def _require(condition):
    if not condition:
        raise SummaryUnavailable("safe execution summary is unavailable")


def _text(value, pattern):
    _require(isinstance(value, str) and 0 < len(value) <= 4096 and re.fullmatch(pattern, value) is not None)


def validate_summary(document):
    """Reject unknown fields and inconsistent counts before displaying anything."""
    _require(type(document) is dict and set(document) == {"version", "hosts", "totals", "changed_events"})
    _require(type(document["version"]) is int and document["version"] == 1)
    hosts, totals, events = document["hosts"], document["totals"], document["changed_events"]
    _require(type(hosts) is dict and type(events) is list)
    for host in hosts:
        _text(host, r"[A-Za-z0-9_.:-]+")
    for counts in [totals, *hosts.values()]:
        _require(type(counts) is dict and set(counts) == set(COUNTERS))
        for value in counts.values():
            _require(type(value) is int and 0 <= value <= 2**53 - 1)
    for counter in COUNTERS:
        _require(totals[counter] == sum(counts[counter] for counts in hosts.values()))
    seen = set()
    for event in events:
        _require(type(event) is dict and set(event) == {"host", "action", "path", "line"})
        _require(type(event["host"]) is str and event["host"] in hosts)
        _text(event["action"], r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
        _text(event["path"], r"[A-Za-z0-9_./ -]+")
        _require(".." not in Path(event["path"]).parts)
        _require(type(event["line"]) is int and 0 < event["line"] <= 2**31 - 1)
        key = tuple(event[field] for field in ("host", "path", "line", "action"))
        _require(key not in seen)
        seen.add(key)
    # A changed recap without a source would not be a usable review gate.
    event_hosts = {event["host"] for event in events}
    _require(all(not counts["changed"] or host in event_hosts for host, counts in hosts.items()))
    return document


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def read_summary(path):
    """Read a bounded owner-only regular file without following symlinks."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                     and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1)
            data = handle.read(MAX_BYTES + 1)
        _require(len(data) <= MAX_BYTES)
        return validate_summary(json.loads(data, object_pairs_hook=_unique_object))
    except (OSError, ValueError, TypeError, RecursionError):
        raise SummaryUnavailable("safe execution summary is unavailable") from None


def display_summary(document):
    validate_summary(document)
    print("provision: " + " ".join(f"{key}={document['totals'][key]}" for key in COUNTERS))
    for host, counts in sorted(document["hosts"].items()):
        print(f"provision: host {host}: " + " ".join(f"{key}={counts[key]}" for key in COUNTERS))
    changed_hosts = sorted({host for host, counts in document["hosts"].items() if counts["changed"]}
                           | {event["host"] for event in document["changed_events"]})
    if not changed_hosts:
        print("provision: no changes")
        return
    print("provision: changed hosts: " + ", ".join(changed_hosts))
    print("provision: changed sources:")
    for event in document["changed_events"]:
        print(f"  {event['host']} {event['path']}:{event['line']} {event['action']}")



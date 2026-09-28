"""Collect only execution counts and static task locations, never result payloads."""

import json
import os
from pathlib import Path
import re
import stat

from ansible.plugins.callback import CallbackBase

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


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "aggregate"
    CALLBACK_NAME = "provision_summary"
    CALLBACK_NEEDS_ENABLED = True

    def __init__(self):
        super().__init__()
        self._events = set()
        self._invalid = False

    def _record(self, result):
        try:
            # ansible-core 2.21 exposes the changed flag independently of result.
            # Do not access result.result, task names, fields, arguments or loop items.
            if not result.is_changed():
                return
            location = result.task.get_path()
            source, line = location.rsplit(":", 1)
            source = Path(source)
            if source.is_relative_to(ROOT):
                source = source.relative_to(ROOT)
            self._events.add((result.host.get_name(), str(source), int(line), result.task.action))
        except Exception:
            # Callback exceptions normally only warn; omit the summary to fail closed.
            self._invalid = True

    def v2_runner_on_ok(self, result):
        self._record(result)

    def v2_runner_on_failed(self, result, ignore_errors=False):
        self._record(result)

    def v2_playbook_on_stats(self, stats):
        if self._invalid:
            return
        try:
            hosts = {}
            for host in sorted(stats.processed):
                recap = stats.summarize(host)
                hosts[host] = {key: recap["failures" if key == "failed" else key] for key in COUNTERS}
            document = validate_summary({
                "version": 1,
                "hosts": hosts,
                "totals": {key: sum(counts[key] for counts in hosts.values()) for key in COUNTERS},
                "changed_events": [dict(host=host, path=path, line=line, action=action)
                                   for host, path, line, action in sorted(self._events)],
            })
            data = json.dumps(document, sort_keys=True).encode("utf-8")
            _require(len(data) <= MAX_BYTES)
            path = Path(os.environ["PROVISION_SUMMARY_PATH"])
            # The parent creates a fresh private volatile directory for each run.
            # Publish only a complete document, including on interruption.
            temporary = path.with_suffix(".tmp")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
            os.replace(temporary, path)
        except Exception:
            # Never include exception text: it can contain untrusted metadata.
            return

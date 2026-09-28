"""Collect only execution counts and static task locations, never result payloads."""

import json
import os
from pathlib import Path
import sys

from ansible.plugins.callback import CallbackBase

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.provision_summary import COUNTERS, MAX_BYTES, ROOT, _require, validate_summary


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

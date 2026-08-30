#!/usr/bin/python3
import argparse
import datetime as dt
import json
from pathlib import Path


UTC = dt.timezone.utc


def parse_timestamp(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("backup status timestamps must be strings or null")
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def read_status(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    required = {
        "last_attempt",
        "last_success",
        "last_failure",
        "duration",
        "backup_size",
        "backup_id",
        "backup_remote",
        "source_node",
        "source_role",
        "prepare_success",
        "upload_success",
        "restore_test_success",
        "restore_test_timestamp",
    }
    missing = sorted(required - data.keys())
    if missing:
        raise ValueError(f"backup status is missing fields: {', '.join(missing)}")
    for key in ("last_attempt", "last_success", "last_failure", "restore_test_timestamp"):
        parse_timestamp(data[key])
    for key in ("backup_id", "backup_remote"):
        if data[key] is not None and not isinstance(data[key], str):
            raise ValueError(f"backup status {key} must be a string or null")
    return data


def _age(value, now):
    timestamp = parse_timestamp(value)
    if timestamp is None:
        return -1
    return max(0, int((now - timestamp).total_seconds()))


def metric(status, name, now=None):
    now = now or dt.datetime.now(UTC)
    if name == "json":
        return json.dumps(status, sort_keys=True, separators=(",", ":"))
    if name == "age":
        return _age(status["last_success"], now)
    if name == "failure":
        failure = parse_timestamp(status["last_failure"])
        success = parse_timestamp(status["last_success"])
        return int(failure is not None and (success is None or failure > success))
    if name == "restore_age":
        return _age(status["restore_test_timestamp"], now)
    if name == "restore_failure":
        return int(status["restore_test_success"] is False)
    if name in status:
        value = status[name]
        if isinstance(value, bool):
            return int(value)
        return "" if value is None else value
    raise ValueError(f"unsupported backup status metric {name!r}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", required=True)
    parser.add_argument("metric")
    args = parser.parse_args()
    print(metric(read_status(args.status), args.metric))


if __name__ == "__main__":
    main()

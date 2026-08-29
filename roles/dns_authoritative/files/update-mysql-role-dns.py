#!/usr/bin/python3
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


SERIAL_PATTERN = re.compile(r"(?m)^(?P<indent>[ \t]*)(?P<value>[0-9]+)(?P<suffix>[ \t]*;[ \t]*serial[ \t]*)$")
SERIAL_MODULUS = 1 << 32


def role_records(primary, secondary, ttl):
    records = (
        "; Managed from MySQL InnoDB ReplicaSet runtime status.\n"
        f"mysql-shared-primary {ttl} IN CNAME {primary.rstrip('.')}.\n"
    )
    if secondary:
        records += f"mysql-shared-replica {ttl} IN CNAME {secondary.rstrip('.')}.\n"
    return records


def advance_serial(zone):
    matches = list(SERIAL_PATTERN.finditer(zone))
    if len(matches) != 1:
        raise ValueError("managed zone must contain exactly one SOA serial line")
    current = int(matches[0].group("value"))
    following = (current + 1) % SERIAL_MODULUS
    updated = SERIAL_PATTERN.sub(
        rf"\g<indent>{following}\g<suffix>", zone, count=1
    )
    return following, updated


def replace_serial(zone, serial):
    if not 0 <= serial < SERIAL_MODULUS:
        raise ValueError("SOA serial must be an unsigned 32-bit integer")
    matches = list(SERIAL_PATTERN.finditer(zone))
    if len(matches) != 1:
        raise ValueError("managed zone must contain exactly one SOA serial line")
    return SERIAL_PATTERN.sub(rf"\g<indent>{serial}\g<suffix>", zone, count=1)


def atomic_write(path, content, mode=None):
    current = path.stat() if path.exists() else None
    ownership = current if current is not None else path.parent.stat()
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(
            temporary,
            mode if mode is not None else current.st_mode & 0o777,
        )
        os.chown(temporary, ownership.st_uid, ownership.st_gid)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def reload_was_applied(path, state):
    if path is None or not path.is_file():
        return False
    try:
        return json.loads(path.read_text(encoding="utf-8")) == state
    except (json.JSONDecodeError, OSError):
        return False


def serial_is_newer(current, candidate):
    distance = (candidate - current) % SERIAL_MODULUS
    return 0 < distance < (SERIAL_MODULUS // 2)


def inspect(zone_path, record_path, primary, secondary, ttl, applied_state_path=None):
    desired_records = role_records(primary, secondary, ttl)
    zone = zone_path.read_text(encoding="utf-8")
    matches = list(SERIAL_PATTERN.finditer(zone))
    if len(matches) != 1:
        raise ValueError("managed zone must contain exactly one SOA serial line")
    serial = int(matches[0].group("value"))
    records_sha256 = hashlib.sha256(desired_records.encode()).hexdigest()
    state = {"records_sha256": records_sha256, "serial": serial}
    return {
        **state,
        "records_match": (
            record_path.is_file()
            and record_path.read_text(encoding="utf-8") == desired_records
        ),
        "reload_required": not reload_was_applied(applied_state_path, state),
    }


def update(
    zone_name,
    zone_path,
    record_path,
    primary,
    secondary,
    ttl,
    check_command,
    applied_state_path=None,
    target_serial=None,
):
    desired_records = role_records(primary, secondary, ttl)
    current_records = record_path.read_text(encoding="utf-8") if record_path.exists() else ""
    zone = zone_path.read_text(encoding="utf-8")
    matches = list(SERIAL_PATTERN.finditer(zone))
    if len(matches) != 1:
        raise ValueError("managed zone must contain exactly one SOA serial line")
    current_serial = int(matches[0].group("value"))
    records_changed = current_records != desired_records
    if target_serial is None:
        target_serial = advance_serial(zone)[0] if records_changed else current_serial
    if not 0 <= target_serial < SERIAL_MODULUS:
        raise ValueError("target SOA serial must be an unsigned 32-bit integer")
    if records_changed and target_serial == current_serial:
        raise ValueError("changed runtime records require an advanced SOA serial")
    zone_changed = target_serial != current_serial
    if zone_changed and not serial_is_newer(current_serial, target_serial):
        raise ValueError("target SOA serial does not advance the current serial")
    records_sha256 = hashlib.sha256(desired_records.encode()).hexdigest()
    if not records_changed and not zone_changed:
        state = {
            "changed": False,
            "serial": current_serial,
            "records_sha256": records_sha256,
        }
        state["reload_required"] = not reload_was_applied(
            applied_state_path,
            {
                "records_sha256": state["records_sha256"],
                "serial": state["serial"],
            },
        )
        return state

    updated_zone = replace_serial(zone, target_serial)
    include_line = f"$INCLUDE {record_path}"
    if include_line not in updated_zone:
        raise ValueError(f"managed zone does not include {record_path}")

    with tempfile.TemporaryDirectory(prefix="mysql-role-dns-", dir=zone_path.parent) as temporary_dir:
        temporary_dir = Path(temporary_dir)
        candidate_records = temporary_dir / record_path.name
        candidate_zone = temporary_dir / zone_path.name
        candidate_records.write_text(desired_records, encoding="utf-8")
        candidate_zone.write_text(
            updated_zone.replace(include_line, f"$INCLUDE {candidate_records}"),
            encoding="utf-8",
        )
        subprocess.run(
            [check_command, zone_name, str(candidate_zone)],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    if zone_changed:
        atomic_write(zone_path, updated_zone)
    if records_changed:
        atomic_write(record_path, desired_records, 0o640)
    subprocess.run(
        [check_command, zone_name, str(zone_path)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return {
        "changed": True,
        "reload_required": True,
        "serial": target_serial,
        "records_sha256": records_sha256,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--zone-name", required=True)
    parser.add_argument("--zone-file", required=True)
    parser.add_argument("--record-file", required=True)
    parser.add_argument("--applied-state-file", required=True)
    parser.add_argument("--primary", required=True)
    parser.add_argument("--secondary", default="")
    parser.add_argument("--ttl", type=int, required=True)
    parser.add_argument("--check-command", default="/usr/sbin/nsd-checkzone")
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--target-serial", type=int)
    args = parser.parse_args()
    if not 30 <= args.ttl <= 60:
        raise ValueError("runtime role DNS TTL must be between 30 and 60 seconds")

    lock_path = Path(args.record_file).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.inspect:
            result = inspect(
                Path(args.zone_file),
                Path(args.record_file),
                args.primary,
                args.secondary,
                args.ttl,
                Path(args.applied_state_file),
            )
        else:
            result = update(
                args.zone_name,
                Path(args.zone_file),
                Path(args.record_file),
                args.primary,
                args.secondary,
                args.ttl,
                args.check_command,
                Path(args.applied_state_file),
                args.target_serial,
            )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

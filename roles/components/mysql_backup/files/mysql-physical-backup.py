#!/usr/bin/python3
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time


from mysql_backup_storage import (
    backup_file_manifest, file_sha256, store_backup, timestamp, validate_storage_paths, write_json_atomic,
)

UTC = dt.timezone.utc


def run(argv, *, capture=False, check=True):
    kwargs = {"check": check, "text": True}
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    else:
        kwargs["stdout"] = sys.stderr
        kwargs["stderr"] = sys.stderr
    return subprocess.run(argv, timeout=3600, **kwargs)


def mysql(config, query, *, column_names=False):
    result = run(
        [
            "/usr/bin/mysql",
            f"--defaults-extra-file={config['credentials_file']}",
            "--batch",
            *([] if column_names else ["--raw"]),
            *([] if column_names else ["--skip-column-names"]),
            "--execute",
            query,
        ],
        capture=True,
    )
    return [line.split("\t") for line in result.stdout.splitlines() if line]


def directory_size(path):
    return sum(item.stat().st_size for item in Path(path).rglob("*") if item.is_file())


def read_status(path, source_node):
    path = Path(path)
    current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    defaults = {
        "last_attempt": None,
        "last_success": None,
        "last_failure": None,
        "destination_backend": "local",
        "destination_target": None,
        "destination_available": False,
        "duration": None,
        "backup_size": None,
        "backup_path": None,
        "source_node": source_node,
        "source_role": None,
        "storage_success": False,
        "local_validation_success": False,
        "prepare_success": False,
        "restore_test_success": False,
        "restore_test_timestamp": None,
    }
    return defaults | current


def write_status(path, status):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o640)
    os.chown(temporary, 0, 0)
    os.replace(temporary, path)


def acquire_operation_lock(path, skip_if_busy):
    lock_handle = Path(path).open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        lock_handle.close()
        if skip_if_busy:
            return None
        raise RuntimeError(
            "another backup, restore validation, or topology operation is running"
        ) from error
    return lock_handle


def role_state(config):
    rows = mysql(
        config,
        "SELECT @@GLOBAL.read_only, @@GLOBAL.super_read_only, @@GLOBAL.server_uuid, "
        "REPLACE(@@GLOBAL.gtid_executed, CHAR(10), ''), COALESCE((SELECT SERVICE_STATE FROM "
        "performance_schema.replication_connection_status LIMIT 1), 'NONE'), "
        "COALESCE((SELECT SERVICE_STATE FROM "
        "performance_schema.replication_applier_status LIMIT 1), 'NONE')",
    )
    read_only, super_read_only, server_uuid, gtid_executed, receiver, applier = rows[0]
    if read_only == "0" and super_read_only == "0":
        role = "PRIMARY"
    elif read_only == "1" and super_read_only == "1" and receiver == "ON" and applier == "ON":
        role = "SECONDARY"
    else:
        role = "UNKNOWN"
    return role, server_uuid, gtid_executed


def require_healthy_secondary(config, server_uuid):
    role, current_uuid, _ = role_state(config)
    rows = mysql(config, "SHOW REPLICA STATUS", column_names=True)
    if role != "SECONDARY" or current_uuid != server_uuid or len(rows) != 2:
        raise RuntimeError("backup source is no longer a healthy single-channel SECONDARY")
    replica = dict(zip(rows[0], rows[1]))
    lag = replica.get("Seconds_Behind_Source", "NULL")
    if (
        replica.get("Replica_IO_Running") != "Yes"
        or replica.get("Replica_SQL_Running") != "Yes"
        or replica.get("Last_IO_Errno") != "0"
        or replica.get("Last_SQL_Errno") != "0"
        or not lag.isdigit()
        or int(lag) > config["maximum_replication_lag_seconds"]
    ):
        raise RuntimeError("backup refused because replication is unhealthy or exceeds the lag limit")
    return int(lag)


def archive_closed_binlogs(config, source_node, server_uuid, backup_run_id):
    mysql(config, "FLUSH BINARY LOGS")
    binlogs = mysql(config, "SHOW BINARY LOGS")
    gtid_at_archive = mysql(config, "SELECT REPLACE(@@GLOBAL.gtid_executed, CHAR(10), '')")[0][0]
    archive = validate_storage_paths(config) / "binlog" / source_node / server_uuid
    if not archive.resolve().is_relative_to(Path(config["repository_directory"]).resolve()):
        raise RuntimeError("binlog archive escaped the repository")
    archive.mkdir(parents=True, exist_ok=True, mode=0o700)
    archived = []
    for name, *_ in binlogs[:-1]:
        if Path(name).name != name:
            raise RuntimeError("invalid source binlog name")
        source = Path(config["binlog_directory"]) / name
        if source.is_symlink() or not source.is_file():
            raise RuntimeError("closed source binlog disappeared or is not a regular file")
        digest = file_sha256(source)
        destination = archive / name
        action = "existing"
        if destination.is_symlink():
            raise RuntimeError("refusing a symlinked archived binlog")
        if destination.exists():
            if not destination.is_file() or file_sha256(destination) != digest:
                raise RuntimeError("archived binlog checksum mismatch")
        else:
            with tempfile.NamedTemporaryFile(dir=archive, prefix=".binlog-", delete=False) as stream:
                temporary = Path(stream.name)
            try:
                shutil.copyfile(source, temporary)
                if file_sha256(temporary) != digest:
                    raise RuntimeError("binlog copy checksum mismatch")
                os.link(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            action = "stored"
        archived.append({"name": name, "sha256": digest, "size": destination.stat().st_size, "action": action})
    manifests = archive / "manifests"
    manifests.mkdir(exist_ok=True, mode=0o700)
    manifest_path = manifests / f"{backup_run_id}.json"
    if manifest_path.exists() or manifest_path.is_symlink():
        raise RuntimeError("binlog manifest already exists")
    write_json_atomic(manifest_path, {
        "archived_at": timestamp(), "backup_run_id": backup_run_id,
        "source_node": source_node, "server_uuid": server_uuid, "gtid_executed": gtid_at_archive,
        "closed_binlogs": archived, "active_binlog": binlogs[-1][0] if binlogs else None,
    })
    return str(manifest_path)


def cleanup_local_path(path, expected_root):
    if not path:
        return
    candidate = Path(path)
    if not candidate.exists():
        return
    if not candidate.is_relative_to(expected_root):
        raise RuntimeError("refusing cleanup outside expected root")
    shutil.rmtree(candidate)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--allow-primary", action="store_true")
    parser.add_argument("--skip-if-lock-busy", action="store_true")
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    lock_handle = acquire_operation_lock(config["lock_file"], args.skip_if_lock_busy)
    if lock_handle is None:
        print(json.dumps({"changed": False, "reason": "shared lock busy"}))
        return

    status_path = Path(config["status_file"])
    status = read_status(status_path, config["source_node"])
    started = time.monotonic()
    status.update(
        {
            "last_attempt": timestamp(),
            "destination_backend": "local",
            "destination_target": config["repository_directory"],
            "destination_available": False,
            "storage_success": False,
            "local_validation_success": False,
            "prepare_success": False,
            "source_role": None,
        }
    )
    staging = None
    try:
        role, server_uuid, gtid_executed = role_state(config)
        status["source_role"] = role
        if role == "PRIMARY" and not args.allow_primary:
            write_status(status_path, status)
            print(json.dumps({"changed": False, "role": role, "reason": "current PRIMARY"}, sort_keys=True))
            return
        if role not in {"PRIMARY", "SECONDARY"}:
            raise RuntimeError("backup refused because the local runtime role is not healthy")
        lag_before = require_healthy_secondary(config, server_uuid) if role == "SECONDARY" else None
        repository = validate_storage_paths(config)
        if not repository.is_dir():
            raise RuntimeError("local backup repository is unavailable")
        status["destination_available"] = True

        staging_root = Path(config["staging_directory"]).resolve()
        production_datadir = Path(config["mysql_datadir"]).resolve()
        required_bytes = (2 * directory_size(production_datadir) + directory_size(config["binlog_directory"])
                          + config["minimum_free_bytes"])
        if min(shutil.disk_usage(staging_root).free, shutil.disk_usage(repository).free) < required_bytes:
            raise RuntimeError("insufficient free space for backup staging and the configured reserve")
        run_id = dt.datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        candidate = (staging_root / run_id).resolve()
        if candidate.parent != staging_root:
            raise RuntimeError("backup staging path escaped its configured root")
        candidate.mkdir(parents=True, exist_ok=False)
        staging = candidate

        backup_started = time.monotonic()
        run(
            [
                "/usr/bin/xtrabackup",
                f"--defaults-extra-file={config['credentials_file']}",
                "--backup",
                f"--target-dir={staging}",
                f"--socket={config['mysql_socket']}",
                *(["--slave-info"] if role == "SECONDARY" else []),
            ]
        )
        backup_duration = round(time.monotonic() - backup_started, 3)
        lag_after = require_healthy_secondary(config, server_uuid) if role == "SECONDARY" else None
        prepare_started = time.monotonic()
        run(["/usr/bin/xtrabackup", "--prepare", f"--target-dir={staging}"])
        prepare_duration = round(time.monotonic() - prepare_started, 3)
        status["prepare_success"] = True
        binlog_info = staging / "xtrabackup_binlog_info"
        xtrabackup_version = run(["/usr/bin/xtrabackup", "--version"], capture=True)
        write_json_atomic(
            staging / "provisioning-backup.json",
            {
                "backup_run_id": run_id,
                "started_at": status["last_attempt"],
                "mysql_version": mysql(config, "SELECT VERSION()")[0][0],
                "xtrabackup_version": (xtrabackup_version.stdout + xtrabackup_version.stderr).strip(),
                "gtid_executed_at_start": gtid_executed,
                "prepared_at": timestamp(),
                "source_node": config["source_node"],
                "source_role": role,
                "server_uuid": server_uuid,
                "files": backup_file_manifest(staging),
                "xtrabackup_binlog_info": binlog_info.read_text(encoding="utf-8").strip()
                if binlog_info.is_file()
                else None,
            },
        )
        backup_size = directory_size(staging)
        storage_started = time.monotonic()
        final = store_backup(config, staging, config["source_node"], server_uuid, run_id, backup_size)
        storage_duration = round(time.monotonic() - storage_started, 3)
        status.update(
            {
                "storage_success": True,
                "local_validation_success": True,
                "backup_path": str(final),
                "backup_size": backup_size,
                "backup_duration": backup_duration,
                "prepare_duration": prepare_duration,
                "storage_duration": storage_duration,
                "replication_lag_before": lag_before,
                "replication_lag_after": lag_after,
                "restore_test_success": False,
                "restore_test_timestamp": None,
            }
        )
        status["binlog_manifest"] = archive_closed_binlogs(config, config["source_node"], server_uuid, run_id)
        if role == "SECONDARY":
            require_healthy_secondary(config, server_uuid)
        status.update({"last_success": timestamp(), "duration": round(time.monotonic() - started, 3)})
        write_status(status_path, status)
        print(
            json.dumps(
                {"backup_path": str(final), "changed": True, "prepare_success": True, "role": role},
                sort_keys=True,
            )
        )
    except BaseException:
        status.update({"last_failure": timestamp(), "duration": round(time.monotonic() - started, 3)})
        write_status(status_path, status)
        raise
    finally:
        try:
            if staging:
                cleanup_local_path(staging, Path(config["staging_directory"]).resolve())
        finally:
            lock_handle.close()


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise SystemExit(128 + signum)

    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, interrupted)
    main()

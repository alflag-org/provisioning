#!/usr/bin/python3
import argparse
import datetime as dt
import fcntl
import grp
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


UTC = dt.timezone.utc


def timestamp():
    return dt.datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run(argv, *, capture=False):
    return subprocess.run(
        argv,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else sys.stderr,
        stderr=subprocess.PIPE if capture else sys.stderr,
    )


def mysql(config, query):
    result = run(
        [
            "/usr/bin/mysql",
            f"--defaults-extra-file={config['credentials_file']}",
            "--batch",
            "--raw",
            "--skip-column-names",
            "--execute",
            query,
        ],
        capture=True,
    )
    return [line.split("\t") for line in result.stdout.splitlines() if line]


def role_state(config):
    rows = mysql(
        config,
        "SELECT @@GLOBAL.read_only, @@GLOBAL.super_read_only, @@GLOBAL.server_uuid, "
        "@@GLOBAL.gtid_executed, COALESCE((SELECT SERVICE_STATE FROM "
        "performance_schema.replication_connection_status LIMIT 1), 'NONE'), "
        "COALESCE((SELECT SERVICE_STATE FROM "
        "performance_schema.replication_applier_status LIMIT 1), 'NONE')",
    )
    read_only, super_read_only, server_uuid, gtid_executed, receiver, applier = rows[0]
    if read_only == "0" and super_read_only == "0":
        role = "PRIMARY"
    elif (
        read_only == "1"
        and super_read_only == "1"
        and receiver == "ON"
        and applier == "ON"
    ):
        role = "SECONDARY"
    else:
        role = "UNKNOWN"
    return role, server_uuid, gtid_executed


def repository_is_off_host(config):
    result = run(
        [
            "/usr/bin/findmnt",
            "--noheadings",
            "--output",
            "FSTYPE",
            "--target",
            config["backup_repository"],
        ],
        capture=True,
    )
    filesystem_type = result.stdout.strip().split()[0]
    if filesystem_type not in config["repository_filesystem_types"]:
        raise RuntimeError(f"backup repository filesystem {filesystem_type!r} is not off-host")


def read_status(path, source_node):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "last_attempt": None,
        "last_success": None,
        "last_failure": None,
        "duration": None,
        "backup_size": None,
        "backup_path": None,
        "source_node": source_node,
        "source_role": None,
        "prepare_success": False,
        "restore_test_success": False,
        "restore_test_timestamp": None,
    }


def write_status(path, status):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o640)
    os.chown(temporary, 0, grp.getgrnam("zabbix").gr_gid)
    os.replace(temporary, path)


def directory_size(path):
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_atomic(path, document):
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def acquire_operation_lock(path, skip_if_busy):
    lock_handle = path.open("w", encoding="utf-8")
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


def archive_closed_binlogs(config, destination, server_uuid, backup_run_id):
    mysql(config, "FLUSH BINARY LOGS")
    binlogs = mysql(config, "SHOW BINARY LOGS")
    gtid_at_archive = mysql(config, "SELECT @@GLOBAL.gtid_executed")[0][0]
    archive = (
        destination
        / config["replicaset_name"]
        / "binlog"
        / config["source_node"]
        / server_uuid
    ).resolve()
    if not archive.is_relative_to(destination):
        raise RuntimeError("binlog archive path escaped the configured repository")
    archive.mkdir(parents=True, exist_ok=True)
    for name, *_ in binlogs[:-1]:
        source = Path(config["binlog_directory"]) / name
        target = archive / name
        if source.is_symlink():
            raise RuntimeError(f"refusing a symlinked source binlog: {name}")
        if target.is_symlink():
            raise RuntimeError(f"refusing a symlinked binlog archive target: {name}")
        if not source.is_file():
            raise RuntimeError(f"closed source binlog disappeared before archival: {name}")
        if target.exists() and not target.is_file():
            raise RuntimeError(f"binlog archive target is not a regular file: {name}")
        if not target.exists():
            temporary_target = archive / f".{name}.tmp"
            if temporary_target.exists() or temporary_target.is_symlink():
                raise RuntimeError(f"stale binlog archive staging file exists: {name}")
            shutil.copy2(source, temporary_target)
            os.replace(temporary_target, target)
        if target.is_file():
            source_hash = file_sha256(source)
            target_hash = file_sha256(target)
            if source_hash != target_hash:
                raise RuntimeError(f"archived binlog checksum mismatch for {name}")
    archived = []
    for path in sorted(archive.iterdir()):
        if path.is_symlink():
            raise RuntimeError(f"refusing a symlink in the binlog archive: {path.name}")
        if path.is_file() and not path.name.startswith(".") and path.name != "manifest.json":
            archived.append(
                {
                    "name": path.name,
                    "sha256": file_sha256(path),
                    "size": path.stat().st_size,
                }
            )
    manifest = {
        "archived_at": timestamp(),
        "backup_run_id": backup_run_id,
        "source_node": config["source_node"],
        "server_uuid": server_uuid,
        "gtid_executed": gtid_at_archive,
        "closed_binlogs": archived,
        "active_binlog": binlogs[-1][0] if binlogs else None,
    }
    manifest_directory = archive / "manifests"
    manifest_directory.mkdir(exist_ok=True)
    write_json_atomic(manifest_directory / f"{backup_run_id}.json", manifest)
    write_json_atomic(archive / "manifest.json", manifest)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--allow-primary", action="store_true")
    parser.add_argument("--skip-if-lock-busy", action="store_true")
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    lock_handle = acquire_operation_lock(
        Path(config["lock_file"]), args.skip_if_lock_busy
    )
    if lock_handle is None:
        print(json.dumps({"changed": False, "reason": "shared lock busy"}))
        return
    status_path = Path(config["status_file"])
    status = read_status(status_path, config["source_node"])
    started = time.monotonic()
    attempt = timestamp()
    status.update({"last_attempt": attempt, "source_node": config["source_node"]})
    staging = None
    incoming = None

    try:
        role, server_uuid, gtid_executed = role_state(config)
        status["source_role"] = role
        if role == "PRIMARY" and not args.allow_primary:
            write_status(status_path, status)
            print(json.dumps({"changed": False, "role": role, "reason": "current PRIMARY"}))
            return
        if role not in {"PRIMARY", "SECONDARY"}:
            raise RuntimeError("backup refused because the local runtime role is not healthy")

        repository_is_off_host(config)
        status["prepare_success"] = False
        repository = Path(config["backup_repository"]).resolve()
        run_id = dt.datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        staging_root = Path(config["staging_directory"]).resolve()
        production_datadir = Path(config["mysql_datadir"]).resolve()
        if (
            staging_root == Path("/")
            or len(staging_root.parts) < 4
            or staging_root == production_datadir
            or staging_root.is_relative_to(production_datadir)
            or production_datadir.is_relative_to(staging_root)
        ):
            raise RuntimeError("unsafe backup staging directory")
        staging = (staging_root / run_id).resolve()
        if staging.parent != staging_root:
            raise RuntimeError("backup staging path escaped its configured root")
        staging.mkdir(parents=True, exist_ok=False)

        run(
            [
                "/usr/bin/xtrabackup",
                f"--defaults-extra-file={config['credentials_file']}",
                "--backup",
                f"--target-dir={staging}",
                f"--socket={config['mysql_socket']}",
            ]
        )
        run(["/usr/bin/xtrabackup", "--prepare", f"--target-dir={staging}"])
        status["prepare_success"] = True
        binlog_info_path = staging / "xtrabackup_binlog_info"
        write_json_atomic(
            staging / "provisioning-backup.json",
            {
                "backup_run_id": run_id,
                "gtid_executed_at_start": gtid_executed,
                "prepared_at": timestamp(),
                "source_node": config["source_node"],
                "source_role": role,
                "server_uuid": server_uuid,
                "xtrabackup_binlog_info": (
                    binlog_info_path.read_text(encoding="utf-8").strip()
                    if binlog_info_path.is_file()
                    else None
                ),
            },
        )

        final_path = (
            repository
            / config["replicaset_name"]
            / "physical"
            / config["source_node"]
            / server_uuid
            / run_id
        ).resolve()
        if not final_path.is_relative_to(repository) or final_path.exists():
            raise RuntimeError("unsafe or duplicate final backup path")
        final_path.parent.mkdir(parents=True, exist_ok=True)
        incoming_root = (repository / config["replicaset_name"] / ".incoming").resolve()
        if not incoming_root.is_relative_to(repository):
            raise RuntimeError("backup incoming path escaped the configured repository")
        incoming_root.mkdir(parents=True, exist_ok=True)
        incoming = incoming_root / f"{config['source_node']}-{server_uuid}-{run_id}"
        incoming.mkdir()
        run(
            [
                "/usr/bin/rsync",
                "--archive",
                "--hard-links",
                "--numeric-ids",
                "--sparse",
                f"{staging}/",
                f"{incoming}/",
            ]
        )
        os.replace(incoming, final_path)
        incoming = None
        archive_closed_binlogs(config, repository, server_uuid, run_id)

        status.update(
            {
                "last_success": timestamp(),
                "duration": round(time.monotonic() - started, 3),
                "backup_size": directory_size(final_path),
                "backup_path": str(final_path),
                "last_failure": status.get("last_failure"),
            }
        )
        write_status(status_path, status)
        print(
            json.dumps(
                {
                    "backup_path": str(final_path),
                    "changed": True,
                    "prepare_success": True,
                    "role": role,
                },
                sort_keys=True,
            )
        )
    except Exception:
        status.update(
            {
                "last_failure": timestamp(),
                "duration": round(time.monotonic() - started, 3),
            }
        )
        write_status(status_path, status)
        raise
    finally:
        try:
            if staging and staging.exists():
                staging_root = Path(config["staging_directory"]).resolve()
                if staging.parent != staging_root:
                    raise RuntimeError("refusing to clean a staging path outside its configured root")
                shutil.rmtree(staging)
            if incoming and incoming.exists():
                incoming_root = (
                    Path(config["backup_repository"])
                    / config["replicaset_name"]
                    / ".incoming"
                ).resolve()
                if incoming.resolve().parent != incoming_root:
                    raise RuntimeError("refusing to clean an incoming path outside its configured root")
                shutil.rmtree(incoming)
        except Exception:
            status.update(
                {
                    "last_failure": timestamp(),
                    "duration": round(time.monotonic() - started, 3),
                }
            )
            write_status(status_path, status)
            raise


if __name__ == "__main__":
    main()

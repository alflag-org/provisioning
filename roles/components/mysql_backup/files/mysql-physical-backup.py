#!/usr/bin/python3
import argparse
import datetime as dt
import fcntl
import grp
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time


UTC = dt.timezone.utc
RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._-]+$")
PHYSICAL_ROOT = "physical"
BINLOG_ROOT = "binlog"
COMPLETION_MARKER = "complete.json"


def timestamp():
    return dt.datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run(argv, *, capture=False, check=True):
    kwargs = {"check": check, "text": True}
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    else:
        kwargs["stdout"] = sys.stderr
        kwargs["stderr"] = sys.stderr
    return subprocess.run(argv, **kwargs)


def rclone_argv(config, *args):
    return [config["rclone_binary"], "--config", config["rclone_config_path"], *args]


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


def directory_size(path):
    return sum(item.stat().st_size for item in Path(path).rglob("*") if item.is_file())


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_atomic(path, document):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_status(path, source_node):
    path = Path(path)
    current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    defaults = {
        "last_attempt": None,
        "last_success": None,
        "last_failure": None,
        "destination_backend": "b2",
        "destination_target": None,
        "destination_available": False,
        "duration": None,
        "backup_size": None,
        "backup_path": None,
        "source_node": source_node,
        "source_role": None,
        "transfer_success": False,
        "remote_validation_success": False,
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
    os.chown(temporary, 0, grp.getgrnam("zabbix").gr_gid)
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


def parse_run_id(value):
    if not isinstance(value, str) or not RUN_ID_RE.fullmatch(value):
        raise RuntimeError(f"invalid backup run id: {value!r}")
    return dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def validate_identifier(value, label):
    if not isinstance(value, str) or IDENTIFIER_RE.fullmatch(value) is None:
        raise RuntimeError(f"invalid {label}: {value!r}")


def validate_b2_config(config):
    for key in ("b2_bucket", "b2_prefix", "rclone_remote", "rclone_version"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise RuntimeError(f"{key} is required")
    validate_identifier(config["source_node"], "source_node")
    if config["b2_bucket"].startswith("/") or config["b2_bucket"].endswith("/"):
        raise RuntimeError("b2_bucket must not start or end with '/'")
    if config["b2_prefix"].startswith("/") or config["b2_prefix"].endswith("/"):
        raise RuntimeError("b2_prefix must not start or end with '/'")


def b2_path(config, *parts):
    base = f"{config['rclone_remote']}:{config['b2_bucket']}/{config['b2_prefix']}".rstrip("/")
    return "/".join([base] + [str(part).strip("/") for part in parts])


def rclone_preflight(config):
    version = run(rclone_argv(config, "version"), capture=True, check=False)
    if version.returncode != 0 or f"rclone v{config['rclone_version']}" not in version.stdout:
        raise RuntimeError("rclone version or executable validation failed")
    bucket = f"{config['rclone_remote']}:{config['b2_bucket']}"
    bucket_check = run(rclone_argv(config, "lsd", bucket), capture=True, check=False)
    if bucket_check.returncode != 0:
        raise RuntimeError("B2 bucket authentication or availability check failed")
    prefix_check = run(
        rclone_argv(config, "lsf", b2_path(config), "--max-depth", "1"),
        capture=True,
        check=False,
    )
    if prefix_check.returncode != 0:
        raise RuntimeError("B2 backup prefix availability check failed")


def rclone_exists(config, path):
    result = run(
        rclone_argv(config, "lsf", str(path), "--files-only"),
        capture=True,
        check=False,
    )
    return result.returncode == 0


def rclone_copy_directory(config, source, destination):
    run(rclone_argv(config, "copy", str(source), str(destination), "--create-empty-src-dirs"))


def rclone_copy_text(config, destination, content):
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "complete.json"
        source.write_text(content, encoding="utf-8")
        run(rclone_argv(config, "copyto", str(source), str(destination)))


def rclone_read_text(config, path):
    result = run(rclone_argv(config, "cat", str(path)), capture=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"failed to read B2 object: {path}")
    return result.stdout


def rclone_check(config, source, destination):
    result = run(
        rclone_argv(config, "check", str(source), str(destination), "--one-way"),
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("B2 checksum validation reported a mismatch")


def rclone_list_dirs(config, root):
    result = run(
        rclone_argv(config, "lsf", str(root), "--dirs-only", "--recursive"),
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [line.strip().rstrip("/") for line in result.stdout.splitlines() if line.strip()]


def is_complete_backup(config, candidate):
    return all(
        rclone_exists(config, f"{candidate}/{name}")
        for name in ("xtrabackup_checkpoints", "provisioning-backup.json", COMPLETION_MARKER)
    )


def list_backups(config):
    root = b2_path(config, PHYSICAL_ROOT)
    candidates = []
    for name in rclone_list_dirs(config, root):
        parts = [part for part in name.split("/") if part]
        if len(parts) != 3:
            continue
        source_node, server_uuid, run_id = parts
        if not all(
            (
                IDENTIFIER_RE.fullmatch(source_node),
                IDENTIFIER_RE.fullmatch(server_uuid),
                RUN_ID_RE.fullmatch(run_id),
            )
        ):
            continue
        candidate = b2_path(config, PHYSICAL_ROOT, source_node, server_uuid, run_id)
        if is_complete_backup(config, candidate):
            candidates.append({"run_id": run_id, "run_at": parse_run_id(run_id), "path": candidate})
    return candidates


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
    elif read_only == "1" and super_read_only == "1" and receiver == "ON" and applier == "ON":
        role = "SECONDARY"
    else:
        role = "UNKNOWN"
    return role, server_uuid, gtid_executed


def upload_backup(config, staging, source_node, server_uuid, run_id, backup_size):
    final = b2_path(config, PHYSICAL_ROOT, source_node, server_uuid, run_id)
    complete = f"{final}/{COMPLETION_MARKER}"
    if rclone_exists(config, complete):
        raise RuntimeError(f"backup run already exists: {run_id}")

    rclone_copy_directory(config, staging, final)
    rclone_check(config, staging, final)
    manifest_path = Path(staging) / "provisioning-backup.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("backup_run_id") != run_id or manifest.get("server_uuid") != server_uuid:
        raise RuntimeError("local backup manifest does not match its destination")
    remote_manifest = json.loads(rclone_read_text(config, f"{final}/provisioning-backup.json"))
    if remote_manifest != manifest:
        raise RuntimeError("remote backup manifest does not match local staging")

    completion = {
        "backup_run_id": run_id,
        "completed_at": timestamp(),
        "backup_size": backup_size,
        "manifest_sha256": file_sha256(manifest_path),
    }
    rclone_copy_text(config, complete, json.dumps(completion, indent=2, sort_keys=True) + "\n")
    if not is_complete_backup(config, final):
        raise RuntimeError("remote backup completion marker validation failed")
    return final


def archive_closed_binlogs(config, source_node, server_uuid, backup_run_id):
    mysql(config, "FLUSH BINARY LOGS")
    binlogs = mysql(config, "SHOW BINARY LOGS")
    gtid_at_archive = mysql(config, "SELECT @@GLOBAL.gtid_executed")[0][0]
    remote_root = b2_path(config, BINLOG_ROOT, source_node, server_uuid, backup_run_id)
    with tempfile.TemporaryDirectory() as directory:
        staging = Path(directory) / "binlog"
        staging.mkdir()
        archived = []
        for name, *_ in binlogs[:-1]:
            source = Path(config["binlog_directory"]) / name
            if source.is_symlink():
                raise RuntimeError(f"refusing a symlinked source binlog: {name}")
            if not source.is_file():
                raise RuntimeError(f"closed source binlog disappeared before archival: {name}")
            target = staging / name
            shutil.copy2(source, target)
            archived.append({"name": name, "sha256": file_sha256(target), "size": target.stat().st_size})
        manifest = {
            "archived_at": timestamp(),
            "backup_run_id": backup_run_id,
            "source_node": source_node,
            "server_uuid": server_uuid,
            "gtid_executed": gtid_at_archive,
            "closed_binlogs": archived,
            "active_binlog": binlogs[-1][0] if binlogs else None,
        }
        manifest_root = staging / "manifests"
        manifest_root.mkdir()
        (manifest_root / f"{backup_run_id}.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        rclone_copy_directory(config, staging, remote_root)
        rclone_check(config, staging, remote_root)


def validate_staging_root(staging_root, production_datadir):
    if (
        staging_root == Path("/")
        or len(staging_root.parts) < 4
        or staging_root == production_datadir
        or staging_root.is_relative_to(production_datadir)
        or production_datadir.is_relative_to(staging_root)
    ):
        raise RuntimeError("unsafe backup staging directory")


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

    validate_b2_config(config)
    status_path = Path(config["status_file"])
    status = read_status(status_path, config["source_node"])
    started = time.monotonic()
    status.update(
        {
            "last_attempt": timestamp(),
            "destination_backend": "b2",
            "destination_target": b2_path(config),
            "destination_available": False,
            "transfer_success": False,
            "remote_validation_success": False,
            "prepare_success": False,
            "source_role": None,
        }
    )
    staging = None
    try:
        rclone_preflight(config)
        status["destination_available"] = True
        role, server_uuid, gtid_executed = role_state(config)
        status["source_role"] = role
        if role == "PRIMARY" and not args.allow_primary:
            write_status(status_path, status)
            print(json.dumps({"changed": False, "role": role, "reason": "current PRIMARY"}, sort_keys=True))
            return
        if role not in {"PRIMARY", "SECONDARY"}:
            raise RuntimeError("backup refused because the local runtime role is not healthy")

        staging_root = Path(config["staging_directory"]).resolve()
        production_datadir = Path(config["mysql_datadir"]).resolve()
        validate_staging_root(staging_root, production_datadir)
        run_id = dt.datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
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
        binlog_info = staging / "xtrabackup_binlog_info"
        write_json_atomic(
            staging / "provisioning-backup.json",
            {
                "backup_run_id": run_id,
                "gtid_executed_at_start": gtid_executed,
                "prepared_at": timestamp(),
                "source_node": config["source_node"],
                "source_role": role,
                "server_uuid": server_uuid,
                "xtrabackup_binlog_info": binlog_info.read_text(encoding="utf-8").strip()
                if binlog_info.is_file()
                else None,
            },
        )
        backup_size = directory_size(staging)
        final = upload_backup(config, staging, config["source_node"], server_uuid, run_id, backup_size)
        status.update(
            {
                "transfer_success": True,
                "remote_validation_success": True,
                "backup_path": str(final),
                "backup_size": backup_size,
            }
        )
        archive_closed_binlogs(config, config["source_node"], server_uuid, run_id)
        status.update({"last_success": timestamp(), "duration": round(time.monotonic() - started, 3)})
        write_status(status_path, status)
        print(
            json.dumps(
                {"backup_path": str(final), "changed": True, "prepare_success": True, "role": role},
                sort_keys=True,
            )
        )
    except Exception:
        status.update({"last_failure": timestamp(), "duration": round(time.monotonic() - started, 3)})
        write_status(status_path, status)
        raise
    finally:
        try:
            cleanup_local_path(staging, Path(config["staging_directory"]).resolve())
        finally:
            lock_handle.close()


if __name__ == "__main__":
    main()

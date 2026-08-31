#!/usr/bin/python3
import argparse
import datetime as dt
import fcntl
import grp
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid


UTC = dt.timezone.utc
REMOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
REMOTE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
B2_BUCKET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{4,48}[A-Za-z0-9]$")
STATUS_FIELDS = {
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


def timestamp():
    return dt.datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run(argv, *, capture=False, check=True):
    return subprocess.run(
        argv,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else sys.stderr,
        stderr=subprocess.PIPE if capture else sys.stderr,
    )


def rclone(config, *arguments, capture=False):
    return run(
        [
            config["rclone_binary"],
            "--config",
            config["rclone_config_file"],
            *arguments,
        ],
        capture=capture,
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


def b2_root(config):
    remote = config["rclone_remote"]
    bucket = config["b2_bucket"]
    prefix = config["b2_prefix"]
    if not isinstance(remote, str) or not REMOTE_NAME.fullmatch(remote):
        raise RuntimeError("invalid rclone remote name")
    if not isinstance(bucket, str) or not B2_BUCKET.fullmatch(bucket):
        raise RuntimeError("invalid B2 bucket name")
    if not isinstance(prefix, str) or prefix != prefix.strip("/"):
        raise RuntimeError("invalid B2 backup prefix")
    prefix_parts = prefix.split("/")
    if not prefix_parts or any(
        part in {".", ".."} or not REMOTE_COMPONENT.fullmatch(part)
        for part in prefix_parts
    ):
        raise RuntimeError("invalid B2 backup prefix")
    return f"{remote}:{bucket}/{prefix}"


def b2_path(config, *parts):
    for part in parts:
        if not isinstance(part, str) or not REMOTE_COMPONENT.fullmatch(part):
            raise RuntimeError(f"invalid B2 path component: {part!r}")
    suffix = "/".join(parts)
    return f"{b2_root(config)}/{suffix}" if suffix else b2_root(config)


def b2_bucket_remote(config):
    b2_root(config)
    return f"{config['rclone_remote']}:{config['b2_bucket']}"


def b2_relative_path(config, *parts):
    b2_root(config)
    for part in parts:
        if not isinstance(part, str) or not REMOTE_COMPONENT.fullmatch(part):
            raise RuntimeError(f"invalid B2 path component: {part!r}")
    return "/".join([config["b2_prefix"], *parts])


def require_canonical_server_uuid(value):
    try:
        normalized = str(uuid.UUID(value))
    except (ValueError, AttributeError) as error:
        raise RuntimeError("MySQL returned an invalid server UUID") from error
    if normalized != value:
        raise RuntimeError("MySQL returned a non-canonical server UUID")


def preflight_b2(config):
    bucket_remote = b2_bucket_remote(config)
    remote = config["rclone_remote"]
    bucket = config["b2_bucket"]
    try:
        buckets = json.loads(
            rclone(
                config,
                "lsjson",
                "--dirs-only",
                "--no-mimetype",
                "--no-modtime",
                f"{remote}:",
                capture=True,
            ).stdout
        )
    except (json.JSONDecodeError, TypeError) as error:
        raise RuntimeError("rclone returned an invalid B2 bucket listing") from error
    if not isinstance(buckets, list) or not any(
        item.get("Name") == bucket and item.get("IsDir") is True
        for item in buckets
        if isinstance(item, dict)
    ):
        raise RuntimeError(
            "the configured B2 bucket is not visible; provisioning will not create it"
        )
    rclone(
        config,
        "lsf",
        "--recursive",
        "--files-only",
        "--include",
        f"{config['b2_prefix']}/**",
        bucket_remote,
        capture=True,
    )


def empty_status(source_node):
    return {
        "last_attempt": None,
        "last_success": None,
        "last_failure": None,
        "duration": None,
        "backup_size": None,
        "backup_id": None,
        "backup_remote": None,
        "source_node": source_node,
        "source_role": None,
        "prepare_success": False,
        "upload_success": False,
        "restore_test_success": False,
        "restore_test_timestamp": None,
    }


def read_status(path, source_node):
    if not path.exists():
        return empty_status(source_node)
    status = json.loads(path.read_text(encoding="utf-8"))
    missing = sorted(STATUS_FIELDS - status.keys())
    if missing:
        raise RuntimeError(f"backup status is missing fields: {', '.join(missing)}")
    return status


def write_status(path, status):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.chmod(temporary, 0o640)
    os.chown(temporary, 0, grp.getgrnam("zabbix").gr_gid)
    os.replace(temporary, path)


def directory_size(path):
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def require_staging_tree_without_symlinks(path):
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError("backup staging root is not a regular directory")
    for item in path.rglob("*"):
        if item.is_symlink():
            raise RuntimeError("backup staging contains a symlink")


def file_hash(path, algorithm):
    digest = hashlib.new(algorithm)
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


def remote_files(config, *directory_parts):
    relative_directory = b2_relative_path(config, *directory_parts)
    try:
        document = json.loads(
            rclone(
                config,
                "lsjson",
                "--files-only",
                "--hash",
                "--hash-type",
                "SHA-1",
                "--no-mimetype",
                "--no-modtime",
                "--recursive",
                "--include",
                f"{relative_directory}/*",
                b2_bucket_remote(config),
                capture=True,
            ).stdout
        )
    except (json.JSONDecodeError, TypeError) as error:
        raise RuntimeError("rclone returned an invalid remote object listing") from error
    if not isinstance(document, list):
        raise RuntimeError("rclone remote object listing is not a JSON array")
    return [
        item
        for item in document
        if isinstance(item, dict)
        and PurePosixPath(item.get("Path", "")).parent.as_posix()
        == relative_directory
    ]


def remote_file(config, remote_object):
    try:
        document = json.loads(
            rclone(
                config,
                "lsjson",
                "--stat",
                "--hash",
                "--hash-type",
                "SHA-1",
                "--no-mimetype",
                "--no-modtime",
                remote_object,
                capture=True,
            ).stdout
        )
    except (json.JSONDecodeError, TypeError) as error:
        raise RuntimeError("rclone returned invalid remote object metadata") from error
    if not isinstance(document, dict) or document.get("IsDir") is not False:
        raise RuntimeError("remote backup object is not a regular file")
    return document


def remote_sha1(document):
    hashes = document.get("Hashes", {})
    normalized = {
        str(name).lower().replace("-", ""): value
        for name, value in hashes.items()
    }
    return normalized.get("sha1")


def require_same_remote_file(source, remote_document):
    expected_sha1 = file_hash(source, "sha1")
    actual_sha1 = remote_sha1(remote_document)
    if actual_sha1 is None:
        raise RuntimeError(f"remote SHA-1 is unavailable for {source.name}")
    if (
        int(remote_document.get("Size", -1)) != source.stat().st_size
        or actual_sha1.lower() != expected_sha1
    ):
        raise RuntimeError(f"remote object content differs for {source.name}")


def archive_closed_binlogs(config, server_uuid, backup_run_id, manifest_path):
    mysql(config, "FLUSH BINARY LOGS")
    binlogs = mysql(config, "SHOW BINARY LOGS")
    gtid_at_archive = mysql(config, "SELECT @@GLOBAL.gtid_executed")[0][0]
    existing = {}
    for item in remote_files(
        config,
        "binlog",
        config["source_node"],
        server_uuid,
    ):
        name = item.get("Name")
        if not isinstance(name, str) or not REMOTE_COMPONENT.fullmatch(name):
            raise RuntimeError("B2 binlog archive contains an invalid object name")
        if name in existing:
            raise RuntimeError(f"B2 binlog archive contains duplicate metadata for {name}")
        existing[name] = item

    archived = []
    for name, *_ in binlogs[:-1]:
        if not REMOTE_COMPONENT.fullmatch(name):
            raise RuntimeError(f"invalid MySQL binlog name: {name!r}")
        source = Path(config["binlog_directory"]) / name
        if source.is_symlink():
            raise RuntimeError(f"refusing a symlinked source binlog: {name}")
        if not source.is_file():
            raise RuntimeError(f"closed source binlog disappeared before archival: {name}")
        remote_object = b2_path(
            config,
            "binlog",
            config["source_node"],
            server_uuid,
            name,
        )
        if name in existing:
            require_same_remote_file(source, existing[name])
        else:
            rclone(config, "copyto", "--immutable", str(source), remote_object)
            require_same_remote_file(source, remote_file(config, remote_object))
        archived.append(
            {
                "name": name,
                "sha1": file_hash(source, "sha1"),
                "sha256": file_hash(source, "sha256"),
                "size": source.stat().st_size,
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
    write_json_atomic(manifest_path, manifest)
    manifest_remote = b2_path(
        config,
        "binlog",
        config["source_node"],
        server_uuid,
        "manifests",
        f"{backup_run_id}.json",
    )
    rclone(config, "copyto", "--immutable", str(manifest_path), manifest_remote)
    return manifest


def upload_prepared_backup(config, staging, server_uuid, run_id, backup_size):
    backup_remote = b2_path(
        config,
        "physical",
        config["source_node"],
        server_uuid,
        run_id,
    )
    relative_backup = b2_relative_path(
        config,
        "physical",
        config["source_node"],
        server_uuid,
        run_id,
    )
    existing = rclone(
        config,
        "lsf",
        "--recursive",
        "--files-only",
        "--include",
        f"{relative_backup}/**",
        b2_bucket_remote(config),
        capture=True,
    ).stdout.splitlines()
    if existing:
        raise RuntimeError("refusing to overwrite an existing B2 backup prefix")

    rclone(config, "copy", "--immutable", str(staging), backup_remote)
    rclone(config, "check", str(staging), backup_remote, "--one-way")

    marker = {
        "backup_run_id": run_id,
        "source_node": config["source_node"],
        "server_uuid": server_uuid,
        "completed_at": timestamp(),
        "prepared": True,
        "backup_size": backup_size,
    }
    marker_path = staging / "complete.json"
    if marker_path.exists() or marker_path.is_symlink():
        raise RuntimeError("completion marker path already exists in local staging")
    write_json_atomic(marker_path, marker)
    marker_remote = f"{backup_remote}/complete.json"
    rclone(config, "copyto", "--immutable", str(marker_path), marker_remote)
    require_same_remote_file(marker_path, remote_file(config, marker_remote))
    return backup_remote, marker


def safe_staging_directory(config, run_id):
    configured = Path(config["staging_directory"])
    staging_root = configured.resolve()
    production_datadir = Path(config["mysql_datadir"]).resolve()
    if (
        not configured.is_absolute()
        or configured.is_symlink()
        or configured != staging_root
        or staging_root == Path("/")
        or len(staging_root.parts) < 4
        or staging_root == production_datadir
        or staging_root.is_relative_to(production_datadir)
        or production_datadir.is_relative_to(staging_root)
    ):
        raise RuntimeError("unsafe backup staging directory")
    staging = (staging_root / run_id).resolve()
    if staging.parent != staging_root:
        raise RuntimeError("backup staging path escaped its configured root")
    return staging_root, staging


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
    status.update(
        {
            "last_attempt": timestamp(),
            "source_node": config["source_node"],
            "prepare_success": False,
            "upload_success": False,
        }
    )
    staging = None
    result = None

    try:
        role, server_uuid, gtid_executed = role_state(config)
        require_canonical_server_uuid(server_uuid)
        status["source_role"] = role
        if role == "PRIMARY" and not args.allow_primary:
            write_status(status_path, status)
            print(
                json.dumps(
                    {"changed": False, "role": role, "reason": "current PRIMARY"}
                )
            )
            return
        if role not in {"PRIMARY", "SECONDARY"}:
            raise RuntimeError("backup refused because the local runtime role is not healthy")

        preflight_b2(config)
        run_id = dt.datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        staging_root, staging = safe_staging_directory(config, run_id)
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
        require_staging_tree_without_symlinks(staging)
        status["prepare_success"] = True
        binlog_info_path = staging / "xtrabackup_binlog_info"
        write_json_atomic(
            staging / "provisioning-backup.json",
            {
                "backup_run_id": run_id,
                "gtid_executed_at_start": gtid_executed,
                "prepared": True,
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

        backup_size = directory_size(staging)
        backup_remote, _marker = upload_prepared_backup(
            config, staging, server_uuid, run_id, backup_size
        )
        status.update(
            {
                "backup_id": run_id,
                "backup_remote": backup_remote,
                "backup_size": backup_size,
                "upload_success": True,
            }
        )
        archive_closed_binlogs(
            config,
            server_uuid,
            run_id,
            staging / ".binlog-manifest.json",
        )

        status.update(
            {
                "last_success": timestamp(),
                "duration": round(time.monotonic() - started, 3),
            }
        )
        write_status(status_path, status)
        result = {
            "backup_id": run_id,
            "backup_remote": backup_remote,
            "changed": True,
            "prepare_success": True,
            "upload_success": True,
            "role": role,
        }
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
                    raise RuntimeError(
                        "refusing to clean a staging path outside its configured root"
                    )
                shutil.rmtree(staging)
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
            lock_handle.close()

    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

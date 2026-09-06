#!/usr/bin/python3
import argparse
import datetime as dt
import fcntl
import grp
import hashlib
import json
from pathlib import Path
import os
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
INCOMING_ROOT = ".incoming"
MANIFEST_ROOT = "manifests"
COMPLETION_MARKER = "COMPLETED"


def timestamp():
    return dt.datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run(argv, *, capture=False, check=True):
    kwargs = {
        "check": check,
        "text": True,
    }
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    else:
        kwargs["stdout"] = sys.stderr
        kwargs["stderr"] = sys.stderr
    return subprocess.run(argv, **kwargs)


def rclone_argv(config, *argv):
    command = ["/usr/bin/rclone"]
    rclone_config = config.get("rclone_config_path")
    if rclone_config:
        command.extend(["--config", str(rclone_config)])
    command.extend(argv)
    return command


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
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
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
    if path.exists():
        current = json.loads(path.read_text(encoding="utf-8"))
    else:
        current = {}

    defaults = {
        "last_attempt": None,
        "last_success": None,
        "last_failure": None,
        "destination_transport": None,
        "destination_target": None,
        "destination_available": False,
        "duration": None,
        "backup_size": None,
        "backup_path": None,
        "source_node": source_node,
        "source_role": None,
        "replication_lag_seconds": None,
        "transfer_success": False,
        "remote_validation_success": False,
        "prepare_success": False,
        "restore_test_success": False,
        "restore_test_timestamp": None,
    }
    merged = defaults | current
    if "source_node" not in merged:
        merged["source_node"] = source_node
    return merged


def write_status(path, status):
    temporary = Path(path).with_suffix(".tmp")
    temporary.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o640)
    os.chown(temporary, 0, grp.getgrnam("zabbix").gr_gid)
    os.replace(temporary, path)


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


def parse_run_id(value):
    if not isinstance(value, str) or not RUN_ID_RE.fullmatch(value):
        raise RuntimeError(f"invalid backup run id: {value!r}")
    return dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def validate_identifier(value, label):
    if not isinstance(value, str) or IDENTIFIER_RE.fullmatch(value) is None:
        raise RuntimeError(f"invalid {label}: {value!r}")


def split_rsync_target(target):
    host, sep, remote_path = target.partition(":")
    if not sep:
        raise RuntimeError(f"invalid rsync target: {target!r}")
    return host, remote_path


def parse_destination(config):
    destination = config.get("backup_destination")
    if not isinstance(destination, dict):
        raise RuntimeError("backup_destination must be an object")

    transport = destination.get("transport")
    target = destination.get("target")

    if not isinstance(transport, str):
        raise RuntimeError(f"invalid transport: {transport!r}")

    transport = transport.strip().lower()
    if transport not in {"filesystem", "rsync", "rclone"}:
        raise RuntimeError(f"unsupported transport {transport!r}")

    if not isinstance(target, str) or not target:
        raise RuntimeError("destination target is required")

    if transport == "filesystem" and not target.startswith("/"):
        raise RuntimeError("filesystem transport target must be absolute")

    return transport, target


def transport_path(transport, target, *parts):
    if transport == "filesystem":
        return str((Path(target) / Path(*parts)).resolve())
    if not parts:
        return target.rstrip("/")
    return "/".join([target.rstrip("/")] + [str(part).strip("/") for part in parts])


def transport_child_path(base, *parts):
    if isinstance(base, Path):
        return base.joinpath(*parts)
    return transport_path("rclone", str(base), *parts)


def transport_exists(config, transport, path):
    if transport == "filesystem":
        return Path(path).exists()

    if transport == "rclone":
        result = run(rclone_argv(config, "lsf", str(path)), capture=True, check=False)
        return result.returncode == 0

    remote_host, remote_path = split_rsync_target(str(path))
    if not remote_path:
        return False
    result = run(
        [
            "/usr/bin/rsync",
            "--list-only",
            f"{remote_host}:{remote_path}",
        ],
        capture=True,
        check=False,
    )
    return result.returncode == 0


def transport_copy_directory(config, transport, source, destination):
    source = Path(source)
    if not source.is_dir():
        raise RuntimeError(f"source path is not a directory: {source}")

    if transport == "filesystem":
        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if destination_path.exists():
            raise RuntimeError(f"refusing overwrite to destination directory: {destination_path}")
        run(
            [
                "/usr/bin/cp",
                "-a",
                f"{source}/",
                f"{destination_path}",
            ]
        )
        return

    if transport == "rclone":
        run(
            rclone_argv(
                config,
                "copy",
                str(source),
                str(destination),
                "--copy-links",
                "--create-empty-src-dirs",
            )
        )
        return

    remote_host, remote_path = split_rsync_target(str(destination))
    run(
        [
            "/usr/bin/rsync",
            "--archive",
            "--hard-links",
            "--numeric-ids",
            "--sparse",
            "--mkpath",
            f"{source}/",
            f"{remote_host}:{remote_path}/",
        ]
    )


def transport_copy_text(config, transport, destination, content):
    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory) / "marker.txt"
        temporary.write_text(content, encoding="utf-8")

        if transport == "filesystem":
            destination_path = Path(destination)
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.replace(destination_path)
            return

        if transport == "rclone":
            run(rclone_argv(config, "copyto", str(temporary), str(destination)))
            return

        remote_host, remote_path = split_rsync_target(str(destination))
        run(
            [
                "/usr/bin/rsync",
                "--archive",
                "--mkpath",
                f"{temporary}",
                f"{remote_host}:{remote_path}",
            ]
        )


def transport_move_directory(config, transport, source, destination):
    if transport == "filesystem":
        source_path = Path(source)
        destination_path = Path(destination)
        if not source_path.exists():
            return
        if destination_path.exists():
            raise RuntimeError(f"refusing overwrite destination: {destination_path}")
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.replace(destination_path)
        return

    if transport == "rclone":
        run(rclone_argv(config, "move", str(source), str(destination)))
        return

    source_host, source_path = split_rsync_target(str(source))
    destination_host, destination_path = split_rsync_target(str(destination))
    if source_host != destination_host:
        raise RuntimeError("transport rsync move requires matching host endpoints")

    run(
        [
            "/usr/bin/rsync",
            "--archive",
            "--hard-links",
            "--numeric-ids",
            "--sparse",
            "--mkpath",
            f"{source_host}:{source_path.rstrip('/')}/",
            f"{destination_host}:{destination_path.rstrip('/')}/",
        ]
    )
    transport_remove_directory(config, "rsync", str(source))


def transport_remove_directory(config, transport, path):
    if transport == "filesystem":
        directory = Path(path)
        if directory.exists():
            shutil.rmtree(directory)
        return

    if transport == "rclone":
        run(rclone_argv(config, "purge", str(path)), check=False)
        return

    remote_host, remote_path = split_rsync_target(str(path))
    run(["/usr/bin/ssh", remote_host, "rm", "-rf", remote_path], check=False)


def transport_list_paths(config, transport, root):
    if transport == "filesystem":
        base = Path(root)
        if not base.exists():
            return []
        paths = []
        for item in base.rglob("*"):
            if item.is_dir():
                paths.append(item.relative_to(base).as_posix())
        return sorted(paths)

    if transport == "rclone":
        result = run(
            rclone_argv(config, "lsf", str(root), "--dirs-only", "--recursive"),
            check=False,
            capture=True,
        )
        if result.returncode != 0:
            return []
        return [line.strip().rstrip("/") for line in result.stdout.splitlines() if line.strip()]

    remote_host, remote_path = split_rsync_target(str(root))
    result = run(
        [
            "/usr/bin/rsync",
            "--recursive",
            "--list-only",
            "--out-format=%n",
            f"{remote_host}:{remote_path}",
        ],
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        return []
    return [
        line.strip().rstrip("/")
        for line in result.stdout.splitlines()
        if line.strip()
    ]


def transport_transfer_size(config, transport, path):
    if transport == "filesystem":
        return directory_size(Path(path))

    if transport == "rclone":
        result = run(
            rclone_argv(config, "size", str(path), "--json"),
            check=False,
            capture=True,
        )
        if result.returncode != 0:
            return None
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        size = payload.get("bytes")
        if isinstance(size, int):
            return size

    return None


def transport_read_text(config, transport, path):
    if transport == "filesystem":
        return Path(path).read_text(encoding="utf-8")

    if transport == "rclone":
        result = run(
            rclone_argv(config, "cat", str(path)),
            capture=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"failed to read remote text: {path}")
        return result.stdout

    remote_host, remote_path = split_rsync_target(str(path))
    with tempfile.TemporaryDirectory() as directory:
        local_target = Path(directory) / "manifest.json"
        result = run(
            [
                "/usr/bin/rsync",
                "--archive",
                "--mkpath",
                f"{remote_host}:{remote_path}",
                f"{local_target}",
            ],
            check=False,
            capture=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"failed to read remote text: {path}")
        return local_target.read_text(encoding="utf-8")


def transport_is_complete_backup(config, transport, candidate):
    return (
        transport_exists(config, transport, transport_child_path(candidate, "xtrabackup_checkpoints"))
        and transport_exists(config, transport, transport_child_path(candidate, COMPLETION_MARKER))
        and transport_exists(
            config,
            transport,
            transport_child_path(candidate, "provisioning-backup.json"),
        )
    )


def transport_list_backups(config, transport, target, replicaset):
    base = transport_path(transport, target, PHYSICAL_ROOT, replicaset)
    if transport == "filesystem":
        root = Path(base)
        if not root.exists():
            return []
        candidates = []
        for source_node in sorted(root.iterdir(), key=lambda item: item.name):
            if not source_node.is_dir() or not IDENTIFIER_RE.fullmatch(source_node.name):
                continue
            for server_uuid in sorted(source_node.iterdir(), key=lambda item: item.name):
                if not server_uuid.is_dir() or not IDENTIFIER_RE.fullmatch(server_uuid.name):
                    continue
                for run_id_path in sorted(server_uuid.iterdir(), key=lambda item: item.name):
                    if not run_id_path.is_dir() or not RUN_ID_RE.fullmatch(run_id_path.name):
                        continue
                    if not transport_is_complete_backup(config, transport, run_id_path):
                        continue
                    candidates.append(
                        {
                            "run_id": run_id_path.name,
                            "run_at": parse_run_id(run_id_path.name),
                            "path": run_id_path,
                        }
                    )
        return candidates

    candidates = []
    for name in transport_list_paths(config, transport, base):
        if not name or name.startswith(f"{INCOMING_ROOT}/"):
            continue
        parts = [part for part in name.strip("/").split("/") if part]
        if len(parts) != 3:
            continue
        source_node, server_uuid, run_id = parts
        if not (
            IDENTIFIER_RE.fullmatch(source_node)
            and IDENTIFIER_RE.fullmatch(server_uuid)
            and RUN_ID_RE.fullmatch(run_id)
        ):
            continue
        candidate = transport_path(transport, target, PHYSICAL_ROOT, replicaset, source_node, server_uuid, run_id)
        if transport_is_complete_backup(config, transport, candidate):
            candidates.append(
                {
                    "run_id": run_id,
                    "run_at": parse_run_id(run_id),
                    "path": candidate,
                }
            )
    return candidates


def cleanup_backups(config, transport, target):
    retention_days = int(config.get("retention_days", 14))
    cutoff = dt.datetime.now(UTC) - dt.timedelta(days=retention_days)
    candidates = transport_list_backups(config, transport, target, config["replicaset_name"])
    if not candidates:
        return
    candidates.sort(key=lambda candidate: candidate["run_at"], reverse=True)
    for candidate in candidates[1:]:
        if candidate["run_at"] < cutoff:
            transport_remove_directory(config, transport, str(candidate["path"]))


def cleanup_binlog_backups(config, transport, target):
    retention_days = int(config.get("binlog_retention_days", 21))
    cutoff = dt.datetime.now(UTC) - dt.timedelta(days=retention_days)
    base = transport_path(transport, target, BINLOG_ROOT, config["replicaset_name"])

    latest_by_server = {}
    for name in transport_list_paths(config, transport, base):
        parts = [part for part in name.strip("/").split("/") if part]
        if len(parts) < 4:
            continue
        if parts[-2] != MANIFEST_ROOT:
            continue
        manifest_name = parts[-1]
        if not manifest_name.endswith(".json"):
            continue
        run_id = manifest_name[:-5]
        if not RUN_ID_RE.fullmatch(run_id):
            continue

        server_dir = transport_path(
            transport,
            target,
            BINLOG_ROOT,
            config["replicaset_name"],
            *parts[:-2],
        )
        manifest_path = transport_path(transport, target, BINLOG_ROOT, config["replicaset_name"], *parts)
        try:
            payload = json.loads(transport_read_text(config, transport, manifest_path))
            archived_at = payload.get("archived_at")
            if not archived_at:
                continue
            archived = dt.datetime.fromisoformat(archived_at.replace("Z", "+00:00")).astimezone(UTC)
        except (RuntimeError, ValueError, json.JSONDecodeError):
            continue
        current = latest_by_server.get(server_dir)
        latest_by_server[server_dir] = archived if current is None else max(current, archived)

    for server_dir, latest in latest_by_server.items():
        if latest < cutoff:
            transport_remove_directory(config, transport, server_dir)


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

    lag = None
    if role == "SECONDARY":
        lag = replication_lag_seconds(config)
    return role, server_uuid, gtid_executed, lag


def replication_lag_seconds(config):
    try:
        rows = mysql(
            config,
            "SELECT COALESCE(SECONDS_BEHIND_SOURCE, '0') "
            "FROM performance_schema.replication_connection_status LIMIT 1",
        )
    except Exception:
        return None
    if not rows:
        return None
    raw = rows[0][0]
    if raw in {"", "NULL", None}:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def backup_paths(config, transport, target, source_node, server_uuid, run_id):
    validate_identifier(source_node, "source_node")
    validate_identifier(server_uuid, "server_uuid")
    incoming = transport_path(transport, target, INCOMING_ROOT, run_id)
    final = transport_path(
        transport,
        target,
        PHYSICAL_ROOT,
        config["replicaset_name"],
        source_node,
        server_uuid,
        run_id,
    )
    return incoming, final


def transfer_backup(config, transport, target, source_node, server_uuid, run_id, staging):
    incoming, final = backup_paths(config, transport, target, source_node, server_uuid, run_id)
    transport_copy_directory(config, transport, staging, incoming)
    transport_copy_text(
        config,
        transport,
        transport_child_path(incoming, COMPLETION_MARKER),
        f"completed at {timestamp()}\n",
    )
    transport_move_directory(config, transport, incoming, final)
    if not transport_is_complete_backup(config, transport, final):
        raise RuntimeError("backup transfer did not finalize as completed")
    return final, incoming


def validate_transfer(config, transport, final_path, staging, expected_size):
    if not transport_is_complete_backup(config, transport, final_path):
        raise RuntimeError("prepared backup markers are incomplete after transfer")

    actual = transport_transfer_size(config, transport, final_path)
    if actual is not None and actual != expected_size:
        raise RuntimeError(
            f"backup size mismatch after transfer: expected={expected_size}, actual={actual}"
        )

    if transport == "rclone":
        result = run(
            rclone_argv(
                config,
                "check",
                str(staging),
                str(final_path),
                "--one-way",
                "--size-only",
            ),
            check=False,
            capture=True,
        )
        if result.returncode != 0:
            raise RuntimeError("remote backup validation reported mismatch")


def latest_backup_root(config, transport, target):
    candidates = transport_list_backups(config, transport, target, config["replicaset_name"])
    if not candidates:
        return None
    candidates.sort(key=lambda candidate: candidate["run_at"], reverse=True)
    return candidates[0]["path"]


def transport_fetch_backup(config, transport, backup_path, destination):
    destination = Path(destination)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)

    if transport == "filesystem":
        if not isinstance(backup_path, Path):
            backup_path = Path(backup_path)
        shutil.copytree(backup_path, destination, dirs_exist_ok=False)
        return

    if transport == "rclone":
        run(rclone_argv(config, "copy", str(backup_path), str(destination)))
        return

    remote_host, remote_path = split_rsync_target(str(backup_path))
    run(
        [
            "/usr/bin/rsync",
            "--archive",
            "--mkpath",
            f"{remote_host}:{remote_path}/",
            f"{destination}/",
        ]
    )


def repository_is_off_host(config, transport, target):
    if transport != "filesystem":
        return

    result = run(
        [
            "/usr/bin/findmnt",
            "--noheadings",
            "--output",
            "FSTYPE",
            "--target",
            target,
        ],
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("backup destination validation failed")
    filesystem_type = result.stdout.strip().split()[0] if result.stdout.split() else ""
    if filesystem_type not in config["filesystem_types"]:
        raise RuntimeError("backup destination filesystem is not an accepted off-host filesystem")


def archive_closed_binlogs(config, transport, target, source_node, server_uuid, backup_run_id):
    mysql(config, "FLUSH BINARY LOGS")
    binlogs = mysql(config, "SHOW BINARY LOGS")
    gtid_at_archive = mysql(config, "SELECT @@GLOBAL.gtid_executed")[0][0]

    transport_root = transport_path(
        transport,
        target,
        BINLOG_ROOT,
        config["replicaset_name"],
        source_node,
        server_uuid,
    )
    with tempfile.TemporaryDirectory() as directory:
        staging = Path(directory) / "binlog"
        staging.mkdir()
        archived = []
        for name, *_ in binlogs[:-1]:
            source = Path(config["binlog_directory"]) / name
            target_file = staging / name
            if source.is_symlink():
                raise RuntimeError(f"refusing a symlinked source binlog: {name}")
            if not source.is_file():
                raise RuntimeError(f"closed source binlog disappeared before archival: {name}")
            if target_file.exists() and not target_file.is_file():
                raise RuntimeError(f"binlog archive target is not a regular file: {name}")
            shutil.copy2(source, target_file)
            archived.append(
                {
                    "name": name,
                    "sha256": file_sha256(target_file),
                    "size": target_file.stat().st_size,
                }
            )

        manifest = {
            "archived_at": timestamp(),
            "backup_run_id": backup_run_id,
            "source_node": source_node,
            "server_uuid": server_uuid,
            "gtid_executed": gtid_at_archive,
            "closed_binlogs": archived,
            "active_binlog": binlogs[-1][0] if binlogs else None,
        }
        manifest_root = staging / MANIFEST_ROOT
        manifest_root.mkdir()
        (manifest_root / f"{backup_run_id}.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        transport_copy_directory(config, transport, staging, transport_root)

    if not transport_exists(
        config,
        transport,
        transport_path(transport, transport_root, MANIFEST_ROOT, f"{backup_run_id}.json"),
    ):
        raise RuntimeError("binlog archive manifest missing after transport copy")


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
    lock_handle = acquire_operation_lock(
        Path(config["lock_file"]), args.skip_if_lock_busy
    )
    if lock_handle is None:
        print(json.dumps({"changed": False, "reason": "shared lock busy"}))
        return

    transport, target = parse_destination(config)
    status_path = Path(config["status_file"])
    status = read_status(status_path, config["source_node"])
    started = time.monotonic()
    attempt = timestamp()
    status.update(
        {
            "last_attempt": attempt,
            "source_node": config["source_node"],
            "destination_transport": transport,
            "destination_target": target,
            "destination_available": False,
            "replication_lag_seconds": None,
            "transfer_success": False,
            "remote_validation_success": False,
            "prepare_success": False,
            "source_role": None,
        }
    )

    staging = None
    incoming = None
    final_backup_path = None

    try:
        role, server_uuid, gtid_executed, lag = role_state(config)
        status["source_role"] = role
        status["replication_lag_seconds"] = lag

        if role == "PRIMARY" and not args.allow_primary:
            status["last_failure"] = None
            write_status(status_path, status)
            print(
                json.dumps(
                    {
                        "changed": False,
                        "role": role,
                        "reason": "current PRIMARY",
                    },
                    sort_keys=True,
                )
            )
            return

        if role not in {"PRIMARY", "SECONDARY"}:
            raise RuntimeError("backup refused because the local runtime role is not healthy")

        lag_threshold = config.get("replication_lag_threshold_seconds")
        if role == "SECONDARY" and lag_threshold is not None:
            if lag is None:
                raise RuntimeError(
                    "replication lag is required for secondary backups but unavailable"
                )
            if lag > lag_threshold:
                raise RuntimeError(
                    f"secondary lag {lag} exceeds threshold {lag_threshold}"
                )

        repository_is_off_host(config, transport, target)
        status["destination_available"] = True

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
                "replication_lag_seconds": lag,
                "xtrabackup_binlog_info": (
                    binlog_info_path.read_text(encoding="utf-8").strip()
                    if binlog_info_path.is_file()
                    else None
                ),
            },
        )

        final_backup_path, incoming = transfer_backup(
            config,
            transport,
            target,
            config["source_node"],
            server_uuid,
            run_id,
            staging,
        )
        status["transfer_success"] = True
        status["backup_path"] = str(final_backup_path)

        backup_size = directory_size(staging)
        validate_transfer(config, transport, final_backup_path, staging, backup_size)
        status["remote_validation_success"] = True

        archive_closed_binlogs(
            config,
            transport,
            target,
            config["source_node"],
            server_uuid,
            run_id,
        )

        status.update(
            {
                "last_success": timestamp(),
                "duration": round(time.monotonic() - started, 3),
                "backup_size": backup_size,
                "last_failure": status.get("last_failure"),
            }
        )
        cleanup_backups(config, transport, target)
        cleanup_binlog_backups(config, transport, target)
        write_status(status_path, status)

        print(
            json.dumps(
                {
                    "backup_path": str(final_backup_path),
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
            if staging:
                cleanup_local_path(staging, Path(config["staging_directory"]).resolve())
            if incoming is not None:
                transport_remove_directory(config, transport, incoming)
        finally:
            lock_handle.close()


if __name__ == "__main__":
    main()

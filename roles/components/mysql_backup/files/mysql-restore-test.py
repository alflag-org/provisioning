#!/usr/bin/python3
import argparse
import datetime as dt
import fcntl
import grp
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid


UTC = dt.timezone.utc
MYSQLD_PATH = Path("/usr/sbin/mysqld").resolve()
REMOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
REMOTE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
B2_BUCKET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{4,48}[A-Za-z0-9]$")
RUN_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")


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


def parse_run_id(value):
    if not isinstance(value, str) or not RUN_ID.fullmatch(value):
        raise RuntimeError(f"invalid backup ID: {value!r}")
    try:
        return dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError as error:
        raise RuntimeError(f"invalid backup ID: {value!r}") from error


def parse_completion_path(config, path):
    parts = PurePosixPath(path).parts
    if len(parts) != 4 or parts[-1] != "complete.json":
        raise RuntimeError(f"invalid B2 completion marker path: {path!r}")
    source_node, server_uuid, backup_id, _marker = parts
    source_nodes = config.get("source_nodes")
    if (
        not isinstance(source_nodes, list)
        or source_node not in source_nodes
        or not REMOTE_COMPONENT.fullmatch(source_node)
    ):
        raise RuntimeError(f"completion marker uses an unexpected source node: {source_node}")
    try:
        normalized_uuid = str(uuid.UUID(server_uuid))
    except (ValueError, AttributeError) as error:
        raise RuntimeError("completion marker path has an invalid server UUID") from error
    if normalized_uuid != server_uuid:
        raise RuntimeError("completion marker path has a non-canonical server UUID")
    parsed_id = parse_run_id(backup_id)
    remote = b2_path(config, "physical", source_node, server_uuid, backup_id)
    return {
        "backup_id": backup_id,
        "backup_time": parsed_id,
        "source_node": source_node,
        "server_uuid": server_uuid,
        "backup_remote": remote,
        "marker_remote": f"{remote}/complete.json",
    }


def completed_backup_candidates(config):
    physical_root = b2_relative_path(config, "physical")
    listing = rclone(
        config,
        "lsf",
        "--recursive",
        "--files-only",
        "--include",
        f"{physical_root}/**/complete.json",
        b2_bucket_remote(config),
        capture=True,
    ).stdout.splitlines()
    root_parts = PurePosixPath(physical_root).parts
    marker_paths = []
    for path in listing:
        parts = PurePosixPath(path.strip()).parts
        if (
            parts[: len(root_parts)] == root_parts
            and parts[-1:] == ("complete.json",)
        ):
            marker_paths.append(PurePosixPath(*parts[len(root_parts) :]).as_posix())
    return [parse_completion_path(config, path) for path in marker_paths]


def parse_completed_at(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RuntimeError("completion marker has an invalid completed_at timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError("completion marker has an invalid completed_at timestamp") from error
    if parsed.tzinfo is None:
        raise RuntimeError("completion marker completed_at must include UTC")
    return parsed.astimezone(UTC)


def read_completion_marker(config, candidate):
    try:
        marker = json.loads(
            rclone(config, "cat", candidate["marker_remote"], capture=True).stdout
        )
    except (json.JSONDecodeError, TypeError) as error:
        raise RuntimeError("B2 completion marker is not valid JSON") from error
    required = {
        "backup_run_id",
        "source_node",
        "server_uuid",
        "completed_at",
        "prepared",
    }
    if not isinstance(marker, dict) or required - marker.keys():
        raise RuntimeError("B2 completion marker is missing required fields")
    if (
        marker["backup_run_id"] != candidate["backup_id"]
        or marker["source_node"] != candidate["source_node"]
        or marker["server_uuid"] != candidate["server_uuid"]
        or marker["prepared"] is not True
    ):
        raise RuntimeError("B2 completion marker does not match its remote identity")
    parse_completed_at(marker["completed_at"])
    return marker


def select_backup(config, backup_id=None):
    if backup_id:
        parse_run_id(backup_id)
    candidates = completed_backup_candidates(config)
    if backup_id:
        candidates = [item for item in candidates if item["backup_id"] == backup_id]
    if not candidates:
        description = f" with ID {backup_id}" if backup_id else ""
        raise RuntimeError(f"no completed B2 physical backup is available{description}")
    selected_id = backup_id or max(item["backup_time"] for item in candidates).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    selected = [item for item in candidates if item["backup_id"] == selected_id]
    if len(selected) != 1:
        raise RuntimeError(f"backup ID {selected_id} is ambiguous across B2 identities")
    candidate = selected[0]
    candidate["completion_marker"] = read_completion_marker(config, candidate)
    return candidate


def validate_remote_object_names(config, candidate):
    try:
        document = json.loads(
            rclone(
                config,
                "lsjson",
                "--recursive",
                "--files-only",
                "--no-mimetype",
                "--no-modtime",
                candidate["backup_remote"],
                capture=True,
            ).stdout
        )
    except (json.JSONDecodeError, TypeError) as error:
        raise RuntimeError("rclone returned an invalid physical backup listing") from error
    if not isinstance(document, list):
        raise RuntimeError("rclone physical backup listing is not a JSON array")
    paths = set()
    for item in document:
        path = item.get("Path") if isinstance(item, dict) else None
        pure_path = PurePosixPath(path) if isinstance(path, str) else None
        if (
            pure_path is None
            or not path
            or pure_path.is_absolute()
            or ".." in pure_path.parts
            or pure_path.as_posix() != path
            or item.get("IsDir") is not False
        ):
            raise RuntimeError("B2 physical backup contains an unsafe object path")
        if path in paths:
            raise RuntimeError("B2 physical backup contains a duplicate object path")
        paths.add(path)
    if "complete.json" not in paths:
        raise RuntimeError("B2 physical backup no longer contains its completion marker")


def download_backup(config, candidate, destination):
    validate_remote_object_names(config, candidate)
    rclone(
        config,
        "copy",
        "--immutable",
        candidate["backup_remote"],
        str(destination),
    )
    rclone(
        config,
        "check",
        candidate["backup_remote"],
        str(destination),
        "--one-way",
    )


def read_json_file(path, description):
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"downloaded backup is missing {description}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise RuntimeError(f"downloaded {description} is invalid") from error


def validate_download(destination, candidate):
    for path in destination.rglob("*"):
        if path.is_symlink():
            raise RuntimeError("downloaded backup contains a symlink")

    marker = read_json_file(destination / "complete.json", "completion marker")
    if marker != candidate["completion_marker"]:
        raise RuntimeError("downloaded completion marker differs from the selected marker")

    metadata = read_json_file(
        destination / "provisioning-backup.json", "provisioning metadata"
    )
    if (
        metadata.get("backup_run_id") != candidate["backup_id"]
        or metadata.get("source_node") != candidate["source_node"]
        or metadata.get("server_uuid") != candidate["server_uuid"]
        or metadata.get("prepared") is not True
    ):
        raise RuntimeError("downloaded backup metadata does not match its B2 identity")

    checkpoints = destination / "xtrabackup_checkpoints"
    if checkpoints.is_symlink() or not checkpoints.is_file():
        raise RuntimeError("downloaded backup is missing xtrabackup_checkpoints")
    if not re.search(
        r"^backup_type\s*=\s*full-prepared\s*$",
        checkpoints.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    ):
        raise RuntimeError("downloaded XtraBackup is not prepared")

    metadata_names = {
        "complete.json",
        "provisioning-backup.json",
        "xtrabackup_checkpoints",
    }
    if not any(
        path.is_file() and path.name not in metadata_names
        for path in destination.rglob("*")
    ):
        raise RuntimeError("downloaded backup contains no physical data files")


def update_restore_status(path, success):
    status = json.loads(path.read_text(encoding="utf-8"))
    status["restore_test_success"] = success
    status["restore_test_timestamp"] = timestamp()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.chmod(temporary, 0o640)
    os.chown(temporary, 0, grp.getgrnam("zabbix").gr_gid)
    os.replace(temporary, path)


def change_owner(root, user, group):
    uid = pwd.getpwnam(user).pw_uid
    gid = grp.getgrnam(group).gr_gid
    os.chown(root, uid, gid, follow_symlinks=False)
    for path in root.rglob("*"):
        os.chown(path, uid, gid, follow_symlinks=False)


def restore_server_process(pid, datadir, socket_path):
    process = Path("/proc") / str(pid)
    try:
        executable = (process / "exe").resolve()
        arguments = (process / "cmdline").read_bytes().split(b"\0")
    except (FileNotFoundError, ProcessLookupError):
        return False
    decoded = {value.decode(errors="replace") for value in arguments if value}
    return (
        executable == MYSQLD_PATH
        and f"--datadir={datadir}" in decoded
        and f"--socket={socket_path}" in decoded
    )


def wait_for_exit(pid, timeout=30):
    for _ in range(timeout * 10):
        if not (Path("/proc") / str(pid)).exists():
            return True
        time.sleep(0.1)
    return False


def stop_server(pid_path, socket_path, datadir):
    if pid_path.is_symlink() or socket_path.is_symlink():
        raise RuntimeError("isolated restore ownership files must not be symlinks")
    if not pid_path.exists():
        if socket_path.exists():
            raise RuntimeError("isolated restore socket exists without an ownership pid")
        return
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except ValueError as error:
        raise RuntimeError("isolated restore pid file is invalid") from error
    if not (Path("/proc") / str(pid)).exists():
        if socket_path.exists():
            raise RuntimeError("isolated restore socket exists without a live ownership pid")
        return
    if not restore_server_process(pid, datadir, socket_path):
        raise RuntimeError("refusing to stop a process outside the isolated restore")
    if socket_path.exists():
        run(
            [
                "/usr/bin/mysqladmin",
                "--protocol=socket",
                f"--socket={socket_path}",
                "--user=root",
                "shutdown",
            ],
            check=False,
            capture=True,
        )
        if wait_for_exit(pid):
            return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    if not wait_for_exit(pid):
        raise RuntimeError("isolated restored mysqld did not stop")


def safe_restore_paths(config):
    configured = Path(config["restore_directory"])
    scratch = configured.resolve()
    production_datadir = Path(config["mysql_datadir"]).resolve()
    if (
        not configured.is_absolute()
        or configured.is_symlink()
        or configured != scratch
        or scratch == Path("/")
        or len(scratch.parts) < 4
        or scratch == production_datadir
        or scratch.is_relative_to(production_datadir)
        or production_datadir.is_relative_to(scratch)
    ):
        raise RuntimeError("unsafe restore scratch path")
    return scratch, scratch / "download", scratch / "datadir"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--backup-id")
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    lock_handle = Path(config["lock_file"]).open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        lock_handle.close()
        raise RuntimeError(
            "another backup, restore validation, or topology operation is running"
        ) from error

    status_path = Path(config["status_file"])
    success = False
    scratch = None
    download = None
    datadir = None
    candidate = None

    try:
        candidate = select_backup(config, args.backup_id)
        scratch, download, datadir = safe_restore_paths(config)
        pid_path = scratch / "mysqld.pid"
        socket_path = scratch / "mysqld.sock"
        log_path = scratch / "mysqld.log"

        if scratch.exists():
            stop_server(pid_path, socket_path, datadir)
            shutil.rmtree(scratch)
        scratch.mkdir(parents=True, mode=0o700)
        download.mkdir(mode=0o700)
        download_backup(config, candidate, download)
        validate_download(download, candidate)

        datadir.mkdir(mode=0o700)
        run(
            [
                "/usr/bin/xtrabackup",
                "--copy-back",
                f"--target-dir={download}",
                f"--datadir={datadir}",
            ]
        )
        change_owner(scratch, "mysql", "mysql")
        run(
            [
                "/usr/sbin/mysqld",
                "--no-defaults",
                f"--datadir={datadir}",
                f"--socket={socket_path}",
                f"--pid-file={pid_path}",
                f"--log-error={log_path}",
                "--skip-networking=ON",
                "--mysqlx=OFF",
                "--skip-log-bin",
                "--server-id=4294967000",
                "--user=mysql",
                "--daemonize",
            ]
        )

        for _ in range(60):
            query = run(
                [
                    "/usr/bin/mysql",
                    "--protocol=socket",
                    f"--socket={socket_path}",
                    "--user=root",
                    "--batch",
                    "--skip-column-names",
                    "--execute",
                    "SELECT 1",
                ],
                capture=True,
                check=False,
            )
            if query.returncode == 0 and query.stdout.strip() == "1":
                break
            time.sleep(1)
        else:
            raise RuntimeError("isolated restored mysqld did not accept SELECT 1")

        databases = run(
            [
                "/usr/bin/mysql",
                "--protocol=socket",
                f"--socket={socket_path}",
                "--user=root",
                "--batch",
                "--skip-column-names",
                "--execute",
                "SHOW DATABASES",
            ],
            capture=True,
        ).stdout.splitlines()
        missing = sorted(set(config["expected_databases"]) - set(databases))
        if missing:
            raise RuntimeError(
                f"restored backup is missing expected databases: {', '.join(missing)}"
            )
        success = True
    finally:
        try:
            if scratch is not None:
                pid_path = scratch / "mysqld.pid"
                socket_path = scratch / "mysqld.sock"
                stop_server(pid_path, socket_path, datadir)
                if scratch.exists():
                    shutil.rmtree(scratch)
        except Exception:
            success = False
            raise
        finally:
            try:
                update_restore_status(status_path, success)
            finally:
                lock_handle.close()

    print(
        json.dumps(
            {
                "backup_id": candidate["backup_id"],
                "backup_remote": candidate["backup_remote"],
                "changed": True,
                "restore_test_success": success,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

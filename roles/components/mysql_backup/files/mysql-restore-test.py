#!/usr/bin/python3
import argparse
import datetime as dt
import fcntl
import grp
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import subprocess
import sys
import time


UTC = dt.timezone.utc
MYSQLD_PATH = Path("/usr/sbin/mysqld").resolve()
RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._-]+$")
PHYSICAL_ROOT = "physical"
INCOMING_ROOT = ".incoming"
COMPLETION_MARKER = "COMPLETED"
MANIFEST_ROOT = "manifests"


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
    return transport_path("rclone", str(base), *parts)


def transport_exists(config, transport, path):
    if transport == "filesystem":
        return Path(path).exists()

    if transport == "rclone":
        result = run(["/usr/bin/rclone", "lsf", str(path), "--config", str(config["rclone_config_path"])], capture=True, check=False) if config.get("rclone_config_path") else run(["/usr/bin/rclone", "lsf", str(path)], capture=True, check=False)
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


def parse_run_id(value):
    if not isinstance(value, str) or not RUN_ID_RE.fullmatch(value):
        raise RuntimeError(f"invalid backup run id: {value!r}")
    return dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


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
        command = ["/usr/bin/rclone", "lsf", str(root), "--dirs-only", "--recursive"]
        if config.get("rclone_config_path"):
            command.insert(1, config["rclone_config_path"])
            command.insert(1, "--config")
        result = run(command, check=False, capture=True)
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


def latest_backup(config, transport, target):
    candidates = transport_list_backups(config, transport, target, config["replicaset_name"])
    if not candidates:
        raise RuntimeError("no completed physical backup is available")
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
        command = ["/usr/bin/rclone", "copy", str(backup_path), str(destination)]
        if config.get("rclone_config_path"):
            command[1:1] = ["--config", str(config["rclone_config_path"])]
        run(command)
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


def resolve_latest_backup_path(config, transport, target, explicit_backup_path):
    if explicit_backup_path:
        if transport == "filesystem":
            backup = Path(explicit_backup_path).resolve()
            if not Path(explicit_backup_path).is_absolute():
                raise RuntimeError("restore backup path must be absolute")
        elif transport == "rclone":
            if ":" not in explicit_backup_path:
                raise RuntimeError("restore backup path must use an rclone remote target")
        else:
            backup = explicit_backup_path
        if not transport_is_complete_backup(config, transport, backup):
            raise RuntimeError("restore backup candidate is not completed")
        return backup

    return latest_backup(config, transport, target)


def update_restore_status(path, success):
    status = json.loads(path.read_text(encoding="utf-8"))
    status["restore_test_success"] = success
    status["restore_test_timestamp"] = timestamp()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o640)
    os.chown(temporary, 0, grp.getgrnam("zabbix").gr_gid)
    os.replace(temporary, path)


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
    filesystem_type = result.stdout.strip().split()[0] if result.stdout.split() else ""
    if result.returncode != 0 or filesystem_type not in config["filesystem_types"]:
        raise RuntimeError(f"backup destination filesystem {filesystem_type!r} is not off-host")


def change_owner(root, user, group):
    uid = pwd.getpwnam(user).pw_uid
    gid = grp.getgrnam(group).gr_gid
    os.chown(root, uid, gid, follow_symlinks=False)
    for path in root.rglob("*"):
        os.chown(path, uid, gid, follow_symlinks=False)


def restore_server_process(pid, scratch, socket_path):
    process = Path("/proc") / str(pid)
    try:
        executable = (process / "exe").resolve()
        arguments = (process / "cmdline").read_bytes().split(b"\0")
    except (FileNotFoundError, ProcessLookupError):
        return False
    decoded = {value.decode(errors="replace") for value in arguments if value}
    return (
        executable == MYSQLD_PATH
        and f"--datadir={scratch}" in decoded
        and f"--socket={socket_path}" in decoded
    )


def wait_for_exit(pid, timeout=30):
    for _ in range(timeout * 10):
        if not (Path("/proc") / str(pid)).exists():
            return True
        time.sleep(0.1)
    return False


def stop_server(pid_path, socket_path, scratch):
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
    if not restore_server_process(pid, scratch, socket_path):
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--backup-path")
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
    transport, target = parse_destination(config)
    repository_is_off_host(config, transport, target)

    backup = resolve_latest_backup_path(config, transport, target, args.backup_path)
    if transport == "filesystem":
        repository = Path(target).resolve()
        backup_path = Path(backup).resolve()
        if not backup_path.is_relative_to(repository):
            raise RuntimeError("restore test backup must be from the configured destination repository")
        if not (backup_path / "xtrabackup_checkpoints").is_file():
            raise RuntimeError("restore test backup must be a prepared backup")
        if not (backup_path / COMPLETION_MARKER).is_file():
            raise RuntimeError("restore test backup must be marked completed")
    else:
        backup_path = str(backup)

    scratch = Path(config["restore_directory"]).resolve()
    production_datadir = Path(config["mysql_datadir"]).resolve()
    configured_scratch = Path(config["restore_directory"])
    if (
        not configured_scratch.is_absolute()
        or configured_scratch.is_symlink()
        or scratch == Path("/")
        or len(scratch.parts) < 4
        or scratch == production_datadir
        or scratch.is_relative_to(production_datadir)
        or production_datadir.is_relative_to(scratch)
    ):
        raise RuntimeError("unsafe restore scratch path")
    pid_path = scratch / "mysqld.pid"
    socket_path = scratch / "mysqld.sock"
    log_path = scratch / "mysqld.log"
    status_path = Path(config["status_file"])
    success = False

    try:
        if scratch.exists():
            stop_server(pid_path, socket_path, scratch)
            shutil.rmtree(scratch)
        scratch.mkdir(parents=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix="mysql-backup-restore-", dir=scratch.parent) as source_root:
            transport_fetch_backup(config, transport, backup, Path(source_root))
            source_backup = Path(source_root)
            run(
                [
                    "/usr/bin/xtrabackup",
                    "--copy-back",
                    f"--target-dir={source_backup}",
                    f"--datadir={scratch}",
                ]
            )
            change_owner(scratch, "mysql", "mysql")
            run(
                [
                    "/usr/sbin/mysqld",
                    "--no-defaults",
                    f"--datadir={scratch}",
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
                raise RuntimeError(f"restored backup is missing expected databases: {', '.join(missing)}")
            success = True
    finally:
        try:
            stop_server(pid_path, socket_path, scratch)
            if scratch.exists():
                shutil.rmtree(scratch)
        except Exception:
            success = False
            raise
        finally:
            update_restore_status(status_path, success)
    print(
        json.dumps(
            {
                "backup_path": str(backup),
                "changed": True,
                "restore_test_success": success,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

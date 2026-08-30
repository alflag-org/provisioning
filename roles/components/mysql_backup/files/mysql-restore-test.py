#!/usr/bin/python3
import argparse
import datetime as dt
import fcntl
import grp
import json
import os
from pathlib import Path
import pwd
import shutil
import signal
import subprocess
import sys
import time


UTC = dt.timezone.utc
MYSQLD_PATH = Path("/usr/sbin/mysqld").resolve()


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


def latest_backup(repository, replicaset_name):
    root = repository / replicaset_name / "physical"
    candidates = [path.parent for path in root.glob("*/*/*/xtrabackup_checkpoints")]
    if not candidates:
        raise RuntimeError("no prepared physical backup is available")
    return max(candidates, key=lambda path: path.name)


def update_restore_status(path, success):
    status = json.loads(path.read_text(encoding="utf-8"))
    status["restore_test_success"] = success
    status["restore_test_timestamp"] = timestamp()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o640)
    os.chown(temporary, 0, grp.getgrnam("zabbix").gr_gid)
    os.replace(temporary, path)


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
    repository_is_off_host(config)
    repository = Path(config["backup_repository"]).resolve()
    backup = (
        Path(args.backup_path).resolve()
        if args.backup_path
        else latest_backup(repository, config["replicaset_name"]).resolve()
    )
    if not backup.is_relative_to(repository) or not (backup / "xtrabackup_checkpoints").is_file():
        raise RuntimeError("restore test backup must be a prepared backup under the configured repository")

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
        run(
            [
                "/usr/bin/xtrabackup",
                "--copy-back",
                f"--target-dir={backup}",
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

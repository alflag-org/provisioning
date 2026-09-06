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
import tempfile
import time


UTC = dt.timezone.utc
RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._-]+$")
PHYSICAL_ROOT = "physical"
COMPLETION_MARKER = "complete.json"
MYSQLD_PATH = Path("/usr/sbin/mysqld").resolve()


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


def validate_config(config):
    for key in ("b2_bucket", "b2_prefix", "rclone_remote", "rclone_version"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise RuntimeError(f"{key} is required")
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
    bucket_check = run(
        rclone_argv(config, "lsd", f"{config['rclone_remote']}:{config['b2_bucket']}"),
        capture=True,
        check=False,
    )
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


def parse_run_id(value):
    if not isinstance(value, str) or not RUN_ID_RE.fullmatch(value):
        raise RuntimeError(f"invalid backup id: {value!r}")
    return dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def list_backups(config):
    candidates = []
    for name in rclone_list_dirs(config, b2_path(config, PHYSICAL_ROOT)):
        parts = [part for part in name.split("/") if part]
        if len(parts) != 3:
            continue
        source_node, server_uuid, run_id = parts
        if not (
            IDENTIFIER_RE.fullmatch(source_node)
            and IDENTIFIER_RE.fullmatch(server_uuid)
            and RUN_ID_RE.fullmatch(run_id)
        ):
            continue
        candidate = b2_path(config, PHYSICAL_ROOT, source_node, server_uuid, run_id)
        if is_complete_backup(config, candidate):
            candidates.append({"run_id": run_id, "run_at": parse_run_id(run_id), "path": candidate})
    return candidates


def resolve_backup(config, backup_id):
    candidates = list_backups(config)
    if backup_id:
        parse_run_id(backup_id)
        candidates = [candidate for candidate in candidates if candidate["run_id"] == backup_id]
        if len(candidates) != 1:
            raise RuntimeError(f"backup id is not uniquely available: {backup_id}")
        return candidates[0]["path"]
    if not candidates:
        raise RuntimeError("no completed physical backup is available")
    return max(candidates, key=lambda candidate: candidate["run_at"])["path"]


def transport_fetch_backup(config, backup_path, destination):
    destination = Path(destination)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    run(rclone_argv(config, "copy", str(backup_path), str(destination)))


def update_restore_status(path, success):
    path = Path(path)
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
    return executable == MYSQLD_PATH and f"--datadir={scratch}" in decoded and f"--socket={socket_path}" in decoded


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
    parser.add_argument("--backup-id")
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    lock_handle = Path(config["lock_file"]).open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        lock_handle.close()
        raise RuntimeError("another backup, restore validation, or topology operation is running") from error
    validate_config(config)

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
    backup = None
    try:
        rclone_preflight(config)
        backup = resolve_backup(config, args.backup_id)
        if scratch.exists():
            stop_server(pid_path, socket_path, scratch)
            shutil.rmtree(scratch)
        scratch.mkdir(parents=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix="mysql-backup-restore-", dir=scratch.parent) as source_root:
            transport_fetch_backup(config, backup, Path(source_root))
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
            lock_handle.close()
    print(json.dumps({"backup_id": args.backup_id, "backup_path": backup, "changed": True, "restore_test_success": success}, sort_keys=True))


if __name__ == "__main__":
    main()

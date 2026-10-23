#!/usr/bin/python3
import argparse
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


from mysql_backup_storage import (
    file_sha256, list_backups, read_completion, resolve_backup, timestamp, validate_storage_paths, verify_backup,
)

MYSQLD_PATH = Path("/usr/sbin/mysqld").resolve()


def run(argv, *, capture=False, check=True):
    kwargs = {"check": check, "text": True}
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    else:
        kwargs["stdout"] = sys.stderr
        kwargs["stderr"] = sys.stderr
    return subprocess.run(argv, timeout=3600, **kwargs)


def run_isolated(writable_directory, active_units, *argv):
    unit = f"mysql-backup-restore-{os.getpid()}-{time.monotonic_ns()}.service"
    active_units.add(unit)
    command = [
        "/usr/bin/systemd-run", "--quiet", "--wait", "--pipe", "--collect",
        f"--unit={unit}", "--property=RuntimeMaxSec=3600", "--property=TimeoutStopSec=30",
        "--property=ProtectSystem=strict", f"--property=ReadWritePaths={writable_directory}",
        "--property=PrivateNetwork=yes", *argv,
    ]
    try:
        return run(command)
    finally:
        stop_isolated_unit(unit, active_units)


def stop_isolated_unit(unit, active_units):
    run(["/usr/bin/systemctl", "stop", unit], capture=True, check=False)
    state = run(["/usr/bin/systemctl", "show", unit, "--property=ActiveState", "--value"],
                capture=True, check=False)
    if state.returncode != 0 or state.stdout.strip() not in {"inactive", "failed"}:
        raise RuntimeError("isolated restore command did not stop; temporary files retained")
    active_units.remove(unit)


def update_restore_status(path, success, backup, duration, copy_duration, validation):
    path = Path(path)
    status = json.loads(path.read_text(encoding="utf-8"))
    status["restore_test_success"] = success
    status["restore_test_timestamp"] = timestamp()
    status["restore_test_backup_path"] = backup
    status["restore_test_duration"] = duration
    status["restore_test_copy_duration"] = copy_duration
    status["restore_test_validation"] = validation
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o640)
    os.chown(temporary, 0, 0)
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
                "--no-defaults",
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


def pitr_binlogs(config, manifest):
    archive = (validate_storage_paths(config) / "binlog" / manifest["source_node"] / manifest["server_uuid"])
    if not archive.resolve().is_relative_to(Path(config["repository_directory"]).resolve()):
        raise RuntimeError("PITR archive escaped the repository")
    document = json.loads((archive / "manifests" / f"{manifest['backup_run_id']}.json").read_text())
    if any(document.get(key) != manifest[key] for key in ("source_node", "server_uuid", "backup_run_id")):
        raise RuntimeError("PITR binlog manifest does not match the backup source")
    first, position, *_ = manifest["xtrabackup_binlog_info"].split()
    entries = document["closed_binlogs"]
    starts = [index for index, item in enumerate(entries) if item["name"] == first]
    if len(starts) != 1 or Path(first).name != first or not position.isdigit() or int(position) < 4:
        raise RuntimeError("PITR baseline binlog or position is unavailable")
    sequence = re.fullmatch(r"(.+\.)(\d+)", first)
    if not sequence:
        raise RuntimeError("PITR binlog sequence is invalid")
    paths = []
    for index, entry in enumerate(entries[starts[0]:]):
        expected = sequence[1] + str(int(sequence[2]) + index).zfill(len(sequence[2]))
        path = archive / expected
        if (entry["name"] != expected or path.is_symlink() or not path.is_file()
                or path.stat().st_size != entry["size"] or file_sha256(path) != entry["sha256"]):
            raise RuntimeError("PITR binlogs have a gap, missing file or checksum mismatch")
        paths.append(str(path))
    return int(position), paths


def replay_binlogs(config, manifest, socket_path, restored_gtid):
    position, paths = pitr_binlogs(config, manifest)
    producer = subprocess.Popen([
        "/usr/bin/mysqlbinlog", "--no-defaults", "--verify-binlog-checksum",
        f"--start-position={position}", f"--exclude-gtids={restored_gtid}", *paths,
    ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    consumer = None
    try:
        consumer = subprocess.Popen([
            "/usr/bin/mysql", "--no-defaults", "--protocol=socket", f"--socket={socket_path}", "--user=root",
        ], stdin=producer.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        producer.stdout.close()
        consumer.wait(timeout=3600)
        producer.wait(timeout=30)
        if producer.returncode or consumer.returncode:
            raise RuntimeError("PITR binlog replay failed")
    finally:
        producer.stdout.close()
        for process in (consumer, producer):
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
    return {"binlog_count": len(paths), "last_binlog": Path(paths[-1]).name,
            "end_position": Path(paths[-1]).stat().st_size}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--backup-id")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--pitr", action="store_true", help="replay through the last archived closed binlog")
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    lock_handle = Path(config["lock_file"]).open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        lock_handle.close()
        raise RuntimeError("another backup, restore validation, or topology operation is running") from error
    validate_storage_paths(config)
    if args.list:
        print(json.dumps([{key: item[key] for key in ("run_id", "path")} for item in list_backups(config)],
                         sort_keys=True))
        lock_handle.close()
        return

    scratch = Path(config["restore_directory"]).resolve()
    pid_path = scratch / "mysqld.pid"
    socket_path = scratch / "mysqld.sock"
    log_path = scratch / "mysqld.log"
    status_path = Path(config["status_file"])
    success = False
    backup = None
    started = time.monotonic()
    copy_duration = None
    validation = {}
    active_units = set()
    source_backup = None
    try:
        backup = resolve_backup(config, args.backup_id)
        _, completion = read_completion(backup)
        backup_size = completion.get("backup_size")
        if not isinstance(backup_size, int) or backup_size <= 0:
            raise RuntimeError("completed backup has no valid size for restore space planning")
        required_bytes = 2 * backup_size + config["minimum_free_bytes"]
        if shutil.disk_usage(scratch.parent).free < required_bytes:
            raise RuntimeError("insufficient free space for backup copy, restored data and reserve")
        if scratch.exists():
            stop_server(pid_path, socket_path, scratch)
            shutil.rmtree(scratch)
        scratch.mkdir(parents=True, mode=0o700)
        source_backup = Path(tempfile.mkdtemp(prefix="mysql-backup-restore-", dir=scratch.parent))
        copy_started = time.monotonic()
        manifest = verify_backup(backup)
        shutil.copytree(backup, source_backup, dirs_exist_ok=True)
        verify_backup(source_backup, identity=backup)
        copy_duration = round(time.monotonic() - copy_started, 3)
        run_isolated(source_backup, active_units, "/usr/bin/xtrabackup", "--no-defaults", "--prepare",
                     f"--target-dir={source_backup}", f"--tmpdir={source_backup}")
        run_isolated(
            scratch, active_units, "/usr/bin/xtrabackup", "--no-defaults", "--copy-back",
            f"--target-dir={source_backup}", f"--datadir={scratch}", f"--tmpdir={scratch}",
        )
        change_owner(scratch, "mysql", "mysql")
        mysql_unit = f"mysql-backup-restore-{os.getpid()}-{time.monotonic_ns()}.service"
        active_units.add(mysql_unit)
        run(
            [
                "/usr/bin/systemd-run",
                "--quiet",
                "--collect",
                f"--unit={mysql_unit}",
                "--property=Type=forking",
                f"--property=PIDFile={pid_path}",
                "--property=TimeoutStartSec=60",
                "--property=TimeoutStopSec=30",
                "--property=RuntimeMaxSec=3600",
                "--property=ProtectSystem=strict",
                f"--property=ReadWritePaths={scratch}",
                "--property=PrivateNetwork=yes",
                "/usr/sbin/mysqld",
                "--no-defaults",
                f"--datadir={scratch}",
                f"--tmpdir={scratch}",
                f"--socket={socket_path}",
                f"--pid-file={pid_path}",
                f"--log-error={log_path}",
                "--skip-networking=ON",
                "--skip-replica-start=ON",
                "--persisted-globals-load=OFF",
                "--read-only=OFF",
                "--super-read-only=OFF",
                "--event-scheduler=OFF",
                "--mysqlx=OFF",
                "--skip-log-bin",
                "--gtid-mode=ON",
                "--enforce-gtid-consistency=ON",
                "--server-id=4294967000",
                "--user=mysql",
                "--daemonize",
            ]
        )
        for _ in range(60):
            query = run(
                [
                    "/usr/bin/mysql",
                    "--no-defaults",
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
                "--no-defaults",
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
        result = run(
            [
                "/usr/bin/mysql", "--no-defaults", "--protocol=socket", f"--socket={socket_path}",
                "--user=root", "--batch", "--skip-column-names", "--execute",
                "SELECT VERSION(), REPLACE(@@GLOBAL.gtid_executed, CHAR(10), ''), "
                "(SELECT COUNT(*) FROM mysql.user)",
            ], capture=True,
        )
        version, restored_gtid, account_count = result.stdout.strip().split("\t")
        position = manifest["xtrabackup_binlog_info"].split(None, 2)
        expected_gtid = re.sub(r"\s+", "", position[2]) if len(position) == 3 else ""
        if not expected_gtid or not re.fullmatch(r"[A-Fa-f0-9:,-]+", expected_gtid + restored_gtid):
            raise RuntimeError("backup or restored GTID metadata is missing or invalid")
        gtid_check = run(
            [
                "/usr/bin/mysql", "--no-defaults", "--protocol=socket", f"--socket={socket_path}",
                "--user=root", "--batch", "--skip-column-names", "--execute",
                f"SELECT GTID_SUBSET('{expected_gtid}', '{restored_gtid}'), "
                f"GTID_SUBSET('{restored_gtid}', '{expected_gtid}')",
            ], capture=True,
        )
        if gtid_check.stdout.strip() != "1\t1" or int(account_count) < 1:
            raise RuntimeError("restored GTID or representative table validation failed")
        if version.split(".")[:2] != manifest["mysql_version"].split(".")[:2]:
            raise RuntimeError("restore server series differs from the source server")
        validation = {"mysql_version": version, "database_count": len(databases),
                      "representative_row_count": int(account_count), "gtid_matches_backup": True}
        if args.pitr:
            replay = replay_binlogs(config, manifest, socket_path, restored_gtid)
            after = run([
                "/usr/bin/mysql", "--no-defaults", "--protocol=socket", f"--socket={socket_path}", "--user=root",
                "--batch", "--skip-column-names", "--execute",
                f"SELECT GTID_SUBSET('{restored_gtid}', @@GLOBAL.gtid_executed), "
                f"GTID_SUBTRACT(@@GLOBAL.gtid_executed, '{restored_gtid}') != '', "
                "(SELECT COUNT(*) FROM mysql.user)",
            ], capture=True).stdout.strip().split("\t")
            if len(after) != 3 or after[0] != "1" or int(after[2]) < 1:
                raise RuntimeError("PITR recovered data or GTID validation failed")
            validation["pitr"] = replay | {"success": True, "new_transactions_applied": after[1] == "1"}
        success = True
    finally:
        try:
            stop_server(pid_path, socket_path, scratch)
            for unit in list(active_units):
                stop_isolated_unit(unit, active_units)
            if scratch.exists():
                shutil.rmtree(scratch)
            if source_backup and source_backup.exists():
                shutil.rmtree(source_backup)
        except BaseException:
            success = False
            raise
        finally:
            update_restore_status(status_path, success, str(backup) if backup else None, round(time.monotonic() - started, 3),
                                  copy_duration, validation)
            lock_handle.close()
    print(json.dumps({"backup_id": args.backup_id, "backup_path": str(backup) if backup else None, "changed": True,
                      "restore_test_success": success, "duration": round(time.monotonic() - started, 3),
                      "copy_duration": copy_duration, "validation": validation}, sort_keys=True))


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise SystemExit(128 + signum)

    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, interrupted)
    main()

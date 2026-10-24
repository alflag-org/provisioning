#!/usr/bin/python3
"""Immutable PostgreSQL recovery backups and isolated restore validation."""

import argparse
import fcntl
import grp
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


FILES = ("database.dump", "configuration.tar", "packages.tar", "metadata.json")
PACKAGES = ("zabbix-server-pgsql", "zabbix-frontend-php", "zabbix-sql-scripts", "zabbix-agent2")


def command(argv, **kwargs):
    return subprocess.run(argv, check=True, timeout=1500, stderr=subprocess.DEVNULL, **kwargs)


def postgres(*arguments, **kwargs):
    return command(["runuser", "-u", "postgres", "--", *arguments], **kwargs)


def query(database, sql):
    return postgres("psql", "--no-psqlrc", "-At", "--set", "ON_ERROR_STOP=1",
                    "--dbname", database, "--command", sql,
                    stdout=subprocess.PIPE, text=True).stdout.strip()


def require_storage(config):
    mount = Path(config["mount"])
    if not mount.is_absolute() or mount == Path("/") or mount.is_symlink():
        raise ValueError("invalid external backup mount")
    result = command(["findmnt", "--json", "--mountpoint", str(mount),
                      "--output", "SOURCE,FSTYPE,TARGET"], stdout=subprocess.PIPE, text=True)
    filesystems = json.loads(result.stdout)["filesystems"]
    if len(filesystems) != 1:
        raise ValueError("ambiguous external backup mount")
    fs = filesystems[0]
    if (fs["source"] != config["source"] or fs["target"] != str(mount)
            or fs["fstype"] not in ("nfs", "nfs4", "cifs")):
        raise ValueError("external backup storage identity mismatch")
    repository = mount / "zabbix-recovery"
    if repository.is_symlink():
        raise ValueError("backup repository is a symlink")
    repository.mkdir(mode=0o700, exist_ok=True)
    return repository


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_backup(path):
    if path.is_symlink() or not path.is_dir():
        raise ValueError("invalid backup directory")
    manifest = path / "complete.json"
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError("backup has no completion record")
    hashes = json.loads(manifest.read_text())
    if set(hashes) != set(FILES):
        raise ValueError("invalid backup manifest")
    for name in FILES:
        target = path / name
        if target.is_symlink() or not target.is_file() or checksum(target) != hashes[name]:
            raise ValueError("backup checksum mismatch")
    metadata = json.loads((path / "metadata.json").read_text())
    if not metadata.get("dbversion") or set(metadata.get("packages", {})) != set(PACKAGES):
        raise ValueError("missing recovery metadata")
    return metadata


def backup(config, repository):
    identifier = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex
    staging = repository / (".incomplete-" + identifier)
    destination = repository / identifier
    staging.mkdir(mode=0o700)
    try:
        with (staging / "database.dump").open("xb") as output:
            postgres("pg_dump", "--format=custom", "--no-owner", "--dbname", config["database"], stdout=output)
            output.flush()
            os.fsync(output.fileno())
        packages = {}
        for name in PACKAGES:
            packages[name] = command(["dpkg-query", "-W", "-f=${Version}", name],
                                     stdout=subprocess.PIPE, text=True).stdout
        metadata = {"host": socket.gethostname(), "database": config["database"],
                    "created_at": datetime.now(timezone.utc).isoformat(), "packages": packages,
                    "postgresql": query(config["database"], "SHOW server_version"),
                    "dbversion": query(config["database"], "SELECT mandatory,optional FROM dbversion")}
        (staging / "metadata.json").write_text(json.dumps(metadata, sort_keys=True) + "\n")
        with tarfile.open(staging / "configuration.tar", "w") as archive:
            for name in ("/etc/zabbix", "/etc/nginx", "/etc/php/8.3/fpm",
                         "/etc/postgresql/16/main", "/etc/apt/preferences.d/zabbix-8.0",
                         "/etc/apt/sources.list.d/zabbix-8.0.sources"):
                archive.add(name, arcname=name.lstrip("/"))
            archive.add("/usr/local/sbin/zabbix-recovery", arcname="usr/local/sbin/zabbix-recovery")
            for path in sorted(Path("/etc/systemd/system").glob("zabbix-*")):
                archive.add(path, arcname=str(path).lstrip("/"))
        with tarfile.open(staging / "packages.tar", "w") as archive:
            cached = sorted(Path("/var/cache/zabbix-recovery").glob("*.deb"))
            for package in PACKAGES:
                matches = []
                for candidate in cached:
                    identity = command(["dpkg-deb", "-f", str(candidate), "Package", "Version"],
                                       stdout=subprocess.PIPE, text=True).stdout
                    if identity == f"Package: {package}\nVersion: {packages[package]}\n":
                        matches.append(candidate)
                if len(matches) != 1:
                    raise ValueError("matching recovery package is unavailable or ambiguous")
                archive.add(matches[0], arcname=matches[0].name)
        (staging / "complete.json").write_text(json.dumps({name: checksum(staging / name) for name in FILES}) + "\n")
        validate_backup(staging)
        # Unique names and rename-without-replacement prevent archived-object overwrite.
        if destination.exists():
            raise ValueError("backup destination already exists")
        staging.rename(destination)
        return destination
    except BaseException:
        shutil.rmtree(staging)
        raise


def restore_test(config, path):
    metadata = validate_backup(path)
    if metadata["database"] != config["database"]:
        raise ValueError("backup belongs to another database")
    database = "zabbix_restore_" + uuid.uuid4().hex
    # pg_restore runs as postgres, but the archive lives on root-only remote storage.
    # Feed it over stdin rather than weakening permissions on the recovery backup.
    postgres("createdb", "--owner=zabbix", "--template=template0", database)
    try:
        with (path / "database.dump").open("rb") as source:
            postgres("pg_restore", "--exit-on-error", "--single-transaction", "--no-owner",
                     "--role=zabbix", "--dbname", database, stdin=source)
        version = query(database, "SELECT mandatory,optional FROM dbversion")
        if version != metadata["dbversion"]:
            raise ValueError("restored database version mismatch")
        if int(query(database, "SELECT count(*) FROM users WHERE username='Admin'")) != 1:
            raise ValueError("restored administrator is missing")
        query(database, "SELECT count(*) FROM hosts")
        query(database, "SELECT count(*) FROM items")
    finally:
        postgres("dropdb", "--force", database)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("backup", "verify", "restore-test"))
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    if os.geteuid() != 0:
        raise ValueError("run recovery as root")
    config = json.loads(Path("/etc/zabbix/recovery.json").read_text())
    if not re.fullmatch(r"[a-z][a-z0-9_]*", config["database"]):
        raise ValueError("invalid database name")
    with Path("/run/lock/zabbix-maintenance.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.operation == "backup":
            status_path = Path('/var/lib/zabbix-recovery/status.json')
            status = json.loads(status_path.read_text()) if status_path.exists() else {}
            status.update(last_attempt=int(datetime.now(timezone.utc).timestamp()), success=False)
            try:
                repository = require_storage(config)
                result = backup(config, repository)
                status.update(success=True, last_success=int(datetime.now(timezone.utc).timestamp()))
            finally:
                temporary = status_path.with_name('.status-' + uuid.uuid4().hex)
                temporary.write_text(json.dumps(status) + '\n')
                os.chmod(temporary, 0o640)
                os.chown(temporary, 0, grp.getgrnam('zabbix').gr_gid)
                temporary.replace(status_path)
        else:
            repository = require_storage(config)
            if args.backup is None or args.backup.parent.resolve() != repository.resolve():
                raise ValueError("select a completed backup in the configured external repository")
            result = args.backup
            validate_backup(result)
            if args.operation == "restore-test":
                restore_test(config, result)
        print(json.dumps({"operation": args.operation, "backup": str(result), "verified": True}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # PostgreSQL and configuration errors may include secret-bearing values.
        print("Monitoring recovery failed; inspect storage and PostgreSQL locally.", file=sys.stderr)
        sys.exit(1)

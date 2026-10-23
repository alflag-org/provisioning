"""Integrity checks and immutable local storage for physical MySQL backups."""

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

UTC = dt.timezone.utc
RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._-]+$")
COMPLETION_MARKER = "complete.json"


def timestamp():
    return dt.datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_atomic(path, document):
    path = Path(path)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_storage_paths(config):
    paths = []
    for name in ("mysql_datadir", "repository_directory", "staging_directory", "restore_directory"):
        configured = Path(config[name])
        resolved = configured.resolve()
        if not configured.is_absolute() or configured.is_symlink() or len(resolved.parts) < 4:
            raise RuntimeError(f"unsafe storage path: {name}")
        for other in paths:
            if resolved.is_relative_to(other) or other.is_relative_to(resolved):
                raise RuntimeError("production data, repository, staging and restore paths must be separate")
        paths.append(resolved)
    return Path(config["repository_directory"]).resolve()


def backup_file_manifest(root):
    root = Path(root)
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("backup contains a symlink")
        if path.is_file():
            files[path.relative_to(root).as_posix()] = {"size": path.stat().st_size, "sha256": file_sha256(path)}
    for name in ("xtrabackup_info", "xtrabackup_checkpoints", "xtrabackup_binlog_info"):
        if name not in files or not files[name]["size"]:
            raise RuntimeError(f"backup is missing required metadata: {name}")
    return files


def read_completion(backup, identity=None):
    backup = Path(backup)
    source_node, server_uuid, run_id = Path(identity or backup).parts[-3:]
    if (not IDENTIFIER_RE.fullmatch(source_node) or not IDENTIFIER_RE.fullmatch(server_uuid)
            or not RUN_ID_RE.fullmatch(run_id)):
        raise RuntimeError("invalid local backup identity")
    for name in ("provisioning-backup.json", COMPLETION_MARKER):
        if (backup / name).is_symlink():
            raise RuntimeError("backup metadata must not be symlinks")
    manifest = json.loads((backup / "provisioning-backup.json").read_text(encoding="utf-8"))
    completion = json.loads((backup / COMPLETION_MARKER).read_text(encoding="utf-8"))
    for document in (manifest, completion):
        if not isinstance(document, dict) or any(document.get(key) != value for key, value in (
            ("source_node", source_node), ("server_uuid", server_uuid), ("backup_run_id", run_id),
        )):
            raise RuntimeError("local backup identity does not match its path")
    if completion.get("manifest_sha256") != file_sha256(backup / "provisioning-backup.json"):
        raise RuntimeError("backup manifest checksum mismatch")
    return manifest, completion


def verify_files(root, manifest):
    root = Path(root)
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise RuntimeError("backup has no file checksums")
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise RuntimeError("backup contains a symlink")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual - {COMPLETION_MARKER} != set(files) | {"provisioning-backup.json"}:
        raise RuntimeError("backup file set differs from its manifest")
    total = 0
    for name, expected in files.items():
        path = root / name
        if not path.resolve().is_relative_to(root.resolve()) or not isinstance(expected, dict):
            raise RuntimeError("unsafe file in backup manifest")
        size = path.stat().st_size
        if size != expected.get("size") or file_sha256(path) != expected.get("sha256"):
            raise RuntimeError("backup file checksum or size mismatch")
        total += size
    for name in ("xtrabackup_info", "xtrabackup_checkpoints", "xtrabackup_binlog_info"):
        if name not in files or not files[name]["size"]:
            raise RuntimeError("backup is missing required XtraBackup metadata")
    return total + (root / "provisioning-backup.json").stat().st_size


def verify_backup(backup, identity=None):
    manifest, completion = read_completion(backup, identity)
    if completion.get("backup_size") != verify_files(backup, manifest):
        raise RuntimeError("backup total size differs from its completion marker")
    return manifest


def list_backups(config):
    repository = validate_storage_paths(config)
    candidates = []
    for backup in (repository / "physical").glob("*/*/*"):
        if backup.is_symlink() or not backup.is_dir() or not backup.resolve().is_relative_to(repository):
            continue
        try:
            read_completion(backup)
            run_at = dt.datetime.strptime(backup.name, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except (OSError, ValueError, RuntimeError):
            continue
        candidates.append({"run_id": backup.name, "run_at": run_at, "path": str(backup)})
    return candidates


def resolve_backup(config, backup_id):
    candidates = list_backups(config)
    if backup_id:
        candidates = [item for item in candidates if item["run_id"] == backup_id]
        if len(candidates) != 1:
            raise RuntimeError("backup id is not uniquely available")
        return Path(candidates[0]["path"])
    if not candidates:
        raise RuntimeError("no completed physical backup is available")
    return Path(max(candidates, key=lambda item: item["run_at"])["path"])


def store_backup(config, staging, source_node, server_uuid, run_id, backup_size):
    repository = validate_storage_paths(config)
    for identifier in (source_node, server_uuid):
        if not IDENTIFIER_RE.fullmatch(identifier):
            raise RuntimeError("invalid backup source identity")
    if not RUN_ID_RE.fullmatch(run_id):
        raise RuntimeError("invalid backup run id")
    destination = repository / "physical" / source_node / server_uuid / run_id
    if not destination.resolve().is_relative_to(repository):
        raise RuntimeError("backup destination escaped the repository")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copytree(staging, destination, symlinks=True)
    manifest_path = destination / "provisioning-backup.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (destination / COMPLETION_MARKER).exists() or verify_files(destination, manifest) != backup_size:
        raise RuntimeError("stored backup size or file validation failed")
    write_json_atomic(destination / COMPLETION_MARKER, {
        "backup_run_id": run_id, "source_node": source_node, "server_uuid": server_uuid,
        "completed_at": timestamp(), "backup_size": backup_size,
        "manifest_sha256": file_sha256(manifest_path),
    })
    try:
        verify_backup(destination)
    except BaseException:
        (destination / COMPLETION_MARKER).unlink(missing_ok=True)
        raise
    return destination

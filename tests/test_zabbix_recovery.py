"""Recovery selection must reject incomplete, modified and symlinked backups."""

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PROGRAM = Path(__file__).resolve().parents[1] / "roles/services/zabbix_server/files/zabbix-recovery.py"
SPEC = importlib.util.spec_from_file_location("zabbix_recovery", PROGRAM)
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)


class RecoveryIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.backup = Path(self.temporary.name) / "backup"
        self.backup.mkdir()
        for name in recovery.FILES:
            (self.backup / name).write_bytes(b"synthetic backup data")
        self.metadata = {"dbversion": "1|1", "packages": dict.fromkeys(recovery.PACKAGES, "test")}
        (self.backup / "metadata.json").write_text(json.dumps(self.metadata))
        hashes = {name: hashlib.sha256((self.backup / name).read_bytes()).hexdigest()
                  for name in recovery.FILES}
        (self.backup / "complete.json").write_text(json.dumps(hashes))

    def test_valid_completion_matches_all_payloads(self):
        self.assertEqual(recovery.validate_backup(self.backup), self.metadata)

    def test_incomplete_backup_is_rejected(self):
        (self.backup / "complete.json").unlink()
        with self.assertRaises(ValueError):
            recovery.validate_backup(self.backup)

    def test_changed_dump_is_rejected(self):
        (self.backup / "database.dump").write_bytes(b"modified")
        with self.assertRaises(ValueError):
            recovery.validate_backup(self.backup)

    def test_payload_symlink_is_rejected_even_when_content_matches(self):
        target = self.backup / "database.dump"
        external = self.backup.parent / "external.dump"
        target.rename(external)
        target.symlink_to(external)
        with self.assertRaises(ValueError):
            recovery.validate_backup(self.backup)

    def test_completion_symlink_is_rejected(self):
        manifest = self.backup / "complete.json"
        external = self.backup.parent / "external.json"
        manifest.rename(external)
        manifest.symlink_to(external)
        with self.assertRaises(ValueError):
            recovery.validate_backup(self.backup)

    def test_matching_checksums_do_not_replace_required_package_metadata(self):
        self.metadata['packages'].pop('zabbix-server-pgsql')
        metadata_file = self.backup / 'metadata.json'
        metadata_file.write_text(json.dumps(self.metadata))
        manifest = self.backup / 'complete.json'
        hashes = json.loads(manifest.read_text())
        hashes['metadata.json'] = hashlib.sha256(metadata_file.read_bytes()).hexdigest()
        manifest.write_text(json.dumps(hashes))
        with self.assertRaises(ValueError):
            recovery.validate_backup(self.backup)

    def test_wrong_mounted_source_never_creates_a_backup_repository(self):
        mount = self.backup.parent / 'remote'
        mount.mkdir()
        observed = {'filesystems': [{'source': 'other:/share', 'fstype': 'nfs4', 'target': str(mount)}]}
        with patch.object(recovery, 'command', return_value=SimpleNamespace(stdout=json.dumps(observed))):
            with self.assertRaises(ValueError):
                recovery.require_storage({'mount': str(mount), 'source': 'expected:/share'})
        self.assertFalse((mount / 'zabbix-recovery').exists())


class RecoveryConfigurationTests(unittest.TestCase):
    def test_archive_paths_use_the_selected_runtime_versions(self):
        paths = recovery.configuration_paths({'postgresql_version': '91', 'php_version': '92.3'})
        self.assertIn('/etc/postgresql/91/main', paths)
        self.assertIn('/etc/php/92.3/fpm', paths)

    def test_runtime_versions_cannot_escape_configuration_directories(self):
        for key in ['postgresql_version', 'php_version']:
            with self.subTest(key=key):
                config = {'postgresql_version': '91', 'php_version': '92.3'}
                config[key] = '../../outside'
                with self.assertRaises(ValueError):
                    recovery.configuration_paths(config)


if __name__ == "__main__":
    unittest.main()

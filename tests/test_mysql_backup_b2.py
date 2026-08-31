from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
BACKUP_ROLE = ROOT / "roles/components/mysql_backup"


class MySQLBackupB2ContractTests(unittest.TestCase):
    def test_rclone_release_and_checksums_are_pinned(self):
        defaults = yaml.safe_load((BACKUP_ROLE / "defaults/main.yml").read_text())
        self.assertEqual(defaults["mysql_backup_rclone_version"], "1.75.0")
        architectures = defaults["mysql_backup_rclone_architectures"]
        self.assertEqual(
            architectures["x86_64"]["checksum"],
            "sha256:aa2804e08f48250e71009c727124b6341cd0288465804a9a09d14663cabafbaa",
        )
        self.assertEqual(
            architectures["aarch64"]["checksum"],
            "sha256:d0ad88ba4c8e285b7c9efa591e0ab643280a91741e13c27f3a9c0957ccfa5203",
        )
        self.assertNotIn("rs" + "ync", defaults["mysql_backup_packages"])

    def test_native_b2_config_is_root_only_and_not_logged(self):
        template = (BACKUP_ROLE / "templates/rclone.conf.j2").read_text()
        tasks = (BACKUP_ROLE / "tasks/main.yml").read_text()
        self.assertIn("type = b2", template)
        self.assertIn("account = {{ mysql_backup_b2_application_key_id }}", template)
        self.assertIn("key = {{ mysql_backup_b2_application_key }}", template)
        self.assertIn("mode: \"0600\"", tasks)
        self.assertIn("Render the root-only rclone B2 configuration", tasks)
        self.assertIn("no_log: true", tasks)

    def test_b2_inputs_are_required_without_a_fictional_bucket(self):
        inventory = yaml.safe_load(
            (ROOT / "inventories/default/group_vars/svc_mysql.yml").read_text()
        )
        tasks = (BACKUP_ROLE / "tasks/main.yml").read_text()
        self.assertEqual(inventory["mysql_backup_b2_bucket"], "")
        self.assertEqual(inventory["mysql_backup_b2_prefix"], "mysql-shared")
        self.assertEqual(inventory["mysql_backup_rclone_remote"], "mysql-backup")
        self.assertIn("mysql_backup_b2_bucket is match", tasks)
        self.assertIn("mysql_backup_b2_application_key_id", tasks)
        self.assertIn("mysql_backup_b2_application_key", tasks)

    def test_backup_commands_never_use_destructive_rclone_operations(self):
        programs = "\n".join(
            path.read_text()
            for path in (BACKUP_ROLE / "files").glob("mysql-*.py")
        )
        self.assertIn('"copy"', programs)
        self.assertIn('"copyto"', programs)
        for command in ("sync", "purge", "delete", "deletefile", "cleanup"):
            self.assertNotIn(f'"{command}"', programs)
        self.assertNotIn("--b2-hard-delete", programs)

    def test_scheduled_backup_keeps_shared_lock_skip(self):
        service = (
            BACKUP_ROLE / "templates/mysql-physical-backup.service.j2"
        ).read_text()
        self.assertIn("--skip-if-lock-busy", service)

    def test_restore_contract_uses_backup_id(self):
        playbook = (
            ROOT / "playbooks/operations/mysql-restore-test.yml"
        ).read_text()
        self.assertIn("mysql_restore_backup_id", playbook)
        self.assertIn("--backup-id", playbook)


if __name__ == "__main__":
    unittest.main()

import unittest

from ansible_support import AnsibleTestCase, ROOT


class ResourceCleanupTests(AnsibleTestCase):
    def test_check_mode_preserves_files_and_repeated_cleanup_is_idempotent(self):
        retired = self.directory / "retired"
        retired.mkdir()
        (retired / "credential").write_text("fixture")
        retained = self.directory / "retained"
        retained.write_text("keep")
        variables = {
            "resource_cleanup_paths": [str(retired), str(self.directory / "absent")],
            "resource_cleanup_mysql_users": ["retired_fixture"],
            "resource_cleanup_mysql_socket": str(self.directory / "missing.sock"),
        }
        playbook = ROOT / "playbooks/operations/resource-cleanup.yml"
        result = self.run_playbook(playbook, variables=variables, check=True)
        self.assert_success(result)
        self.assertTrue((retired / "credential").exists())

        result = self.run_playbook(playbook, variables=variables)
        self.assert_success(result)
        self.assertFalse(retired.exists())
        self.assertEqual(retained.read_text(), "keep")

        result = self.run_playbook(playbook, variables=variables)
        self.assert_success(result)
        self.assertRegex(result.stdout, r"changed=0\s")
        self.assertEqual(retained.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()

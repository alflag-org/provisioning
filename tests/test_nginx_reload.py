"""Reject failed configuration validation before invoking the service reload."""

import sys

from ansible_support import AnsibleTestCase


class NginxReloadTests(AnsibleTestCase):
    def test_failed_validation_stops_reload_handler(self):
        config = self.directory / "nginx.conf"
        attempts = self.directory / "validations"
        validator = self.directory / "nginx"
        config.write_text("valid")
        # The executable records validation attempts and rejects the bad input.
        # The real reload module must remain unreachable on validation failure.
        validator.write_text(
            f"#!{sys.executable}\n"
            "from pathlib import Path\n"
            "import sys\n"
            "config = Path(sys.argv[-1]).read_text()\n"
            f"with Path({str(attempts)!r}).open('a') as output:\n"
            "    output.write(config + '\\n')\n"
            "sys.exit(0 if config == 'valid' else 1)\n"
        )
        validator.chmod(0o755)
        playbook = self.directory / "reload.yml"
        playbook.write_text("""---
- name: Reject invalid configuration before reload
  hosts: default
  gather_facts: false
  tasks:
    - name: Load component validation and handlers
      ansible.builtin.include_role:
        name: components/nginx
        tasks_from: validate
        public: true
    - name: Change the configuration to invalid content
      ansible.builtin.copy:
        content: invalid
        dest: '{{ nginx_config_path }}'
        mode: '0600'
      notify: nginx configuration changed
    - name: Apply pending handlers
      ansible.builtin.meta: flush_handlers
""")
        result = self.run_playbook(playbook, variables={
            "nginx_binary": str(validator),
            "nginx_config_path": str(config),
            "nginx_service_name": "provisioning-fixture-must-not-reload",
        })
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(attempts.read_text().splitlines(), ["valid", "invalid"])

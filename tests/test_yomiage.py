"""Fail closed before deployment when yomiage credentials cannot form an environment."""

from contextlib import redirect_stdout
import io
import sys

import yaml

from atlas_core.secrets import SecretResolver
from commands import provision

from ansible_support import AnsibleTestCase, ROOT


class YomiagePreflightTests(AnsibleTestCase):
    def test_declared_secrets_reach_ansible_without_exposing_values(self):
        required = provision.declarations(ROOT / 'inventories/default/secret-requirements/yomiage.yml')
        sentinel = 'synthetic-resolved-credential-must-not-appear'
        provider = SecretResolver(
            {name: str(index) for index, name in enumerate(required.values())},
            lambda identifiers: {identifier: sentinel for identifier in identifiers},
        )
        inventory = self.directory / 'inventory.yml'
        inventory.write_text('all:\n  hosts:\n    fixture: {}\n')
        playbook = self.directory / 'resolved.yml'
        playbook.write_text(yaml.safe_dump([{
            'hosts': 'fixture', 'gather_facts': False,
            'vars': {
                'ansible_connection': 'local', 'ansible_become': False,
                'ansible_python_interpreter': sys.executable,
                'application_services': ['yomiage'],
                'services_yomiage_billing_api_url': 'https://billing.example.invalid',
                'services_yomiage_hmac_key_id': 'fixture-key',
                # Fail at the real release platform guard after credential resolution.
                'ansible_facts': {'system': 'FreeBSD'},
            },
            'roles': ['services/yomiage'],
        }]))
        output = io.StringIO()
        with redirect_stdout(output):
            result = provision.run(playbook, required, ['-i', str(inventory)], provider=provider)
        self.assertNotEqual(result, 0)
        self.assertIn('changed=0 failed=1', output.getvalue())
        self.assertNotIn(sentinel, output.getvalue())

    def test_missing_and_nul_credentials_fail_without_exposure_or_mutation(self):
        playbook = self.directory / "yomiage.yml"
        playbook.write_text(yaml.safe_dump([{
            "hosts": "default",
            "gather_facts": False,
            "roles": ["services/yomiage"],
        }]))
        sentinel = "synthetic-credential-must-not-appear"
        variables = {
            "application_services": ["yomiage"],
            "services_yomiage_billing_api_url": "https://billing.example.invalid",
            "services_yomiage_hmac_key_id": "fixture-key",
            "services_yomiage_discord_token": sentinel,
            "services_yomiage_google_tts_api_key": sentinel,
            "services_yomiage_cf_access_client_id": sentinel,
            "services_yomiage_cf_access_client_secret": sentinel,
        }
        for value in (None, sentinel + "\0suffix"):
            for check in (False, True):
                with self.subTest(value_present=value is not None, check=check):
                    supplied = dict(variables)
                    if value is not None:
                        supplied["services_yomiage_hmac_key"] = value
                    result = self.run_playbook(playbook, variables=supplied, check=check)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertRegex(result.stdout, r"changed=0\s")
                    self.assertNotIn(sentinel, result.stdout + result.stderr)

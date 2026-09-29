"""Exercise credential parsing and preservation of vendor enrollment identity."""

import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'roles/services/gameap_daemon/files/gameap-enroll.py'
SPEC = importlib.util.spec_from_file_location('gameap_enroll', HELPER)
ENROLL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ENROLL)


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def test_rejects_credential_for_different_panel_before_rpc(self):
        key = secrets.token_urlsafe(24)
        self.assertEqual(ENROLL.parse_connect_url('grpc://192.0.2.10:31718/' + key, '192.0.2.10:31718'), key)
        for url in ['grpc://192.0.2.11:31718/' + key, 'http://192.0.2.10:31718/' + key,
                    'grpc://192.0.2.10:31718/' + key + '?extra=1', 'grpc://192.0.2.10:31718/a/b']:
            with self.subTest(url=url.split('/')[2]):
                with self.assertRaises(ValueError):
                    ENROLL.parse_connect_url(url, '192.0.2.10:31718')

    def test_existing_or_partial_identity_prevents_reenrollment(self):
        for name in ('certs', 'gameap-daemon.yaml', '.enrollment-attempt'):
            path = self.directory / name
            path.touch()
            with self.assertRaises(ValueError):
                ENROLL.require_unenrolled(self.directory)
            path.unlink()
        os.symlink(self.directory / 'absent', self.directory / 'certs')
        with self.assertRaises(ValueError):
            ENROLL.require_unenrolled(self.directory)

    def test_vendor_values_survive_serialization_without_yaml_interpretation(self):
        _, response_class = ENROLL.messages()
        credential = secrets.token_urlsafe(24) + '\nvalue: {{ literal }}'
        response = response_class(success=True, node_id=83, api_key=credential,
                                  root_certificate='-----BEGIN CERTIFICATE-----\nfixture-ca',
                                  server_certificate='-----BEGIN CERTIFICATE-----\nfixture-node',
                                  server_private_key='-----BEGIN PRIVATE KEY-----\nfixture-key')
        ENROLL.save_identity(self.directory, response, '192.0.2.10:31718')
        config = self.directory / 'gameap-daemon.yaml'
        identity = json.loads(config.read_text())
        self.assertEqual(identity['api_key'], credential)
        self.assertEqual(identity['ds_id'], 83)
        self.assertEqual(identity['process_manager'], {'name': 'systemd'})
        self.assertEqual((config.stat().st_mode & 0o777), 0o600)
        self.assertEqual((self.directory / 'certs/server.key').stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            ENROLL.save_identity(self.directory, response, '192.0.2.10:31718')
        self.assertEqual(json.loads(config.read_text())['api_key'], credential)

    def test_rejected_or_incomplete_response_writes_no_identity(self):
        _, response_class = ENROLL.messages()
        for response in [response_class(success=False), response_class(success=True, node_id=3, api_key='incomplete')]:
            with self.assertRaises(ValueError):
                ENROLL.save_identity(self.directory, response, '192.0.2.10:31718')
            self.assertEqual(list(self.directory.iterdir()), [])

    def test_malformed_stdin_never_leaks_into_output(self):
        credential = secrets.token_urlsafe(32)
        result = subprocess.run([sys.executable, str(HELPER)], input=credential,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(credential, result.stdout + result.stderr)
        self.assertNotIn('Traceback', result.stderr)

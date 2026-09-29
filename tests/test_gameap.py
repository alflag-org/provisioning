"""Guard GameAP bootstrap decisions and prevent secret exposure through config."""

import importlib.util
from contextlib import closing
from pathlib import Path
import secrets
import sqlite3
import tempfile
import unittest

from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'gameap_bootstrap', ROOT / 'roles/services/gameap_panel/files/gameap-bootstrap.py')
BOOTSTRAP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BOOTSTRAP)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / 'db.sqlite'

    def test_inspection_does_not_create_database(self):
        self.assertEqual(BOOTSTRAP.database_state(self.database), 'uninitialized')
        self.assertFalse(self.database.exists())

    def test_corrupt_database_does_not_request_reinitialization(self):
        self.database.write_bytes(b'not a database')
        with self.assertRaises(sqlite3.DatabaseError):
            BOOTSTRAP.database_state(self.database)
        self.assertEqual(self.database.read_bytes(), b'not a database')

    def test_partial_seed_requires_admin_role_and_wal(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executescript('''
                CREATE TABLE users (id INTEGER PRIMARY KEY);
                CREATE TABLE roles (id INTEGER PRIMARY KEY, name TEXT);
                CREATE TABLE assigned_roles (entity_id INTEGER, role_id INTEGER, entity_type TEXT);
                INSERT INTO users VALUES (1);
                INSERT INTO roles VALUES (1, 'admin');
            ''')
        with self.assertRaises(ValueError):
            BOOTSTRAP.database_state(self.database)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("INSERT INTO assigned_roles VALUES (1, 1, 'Gameap\\Models\\User')")
        with self.assertRaises(ValueError):
            BOOTSTRAP.database_state(self.database)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('PRAGMA journal_mode=WAL')
        self.assertEqual(BOOTSTRAP.database_state(self.database), 'initialized')
        self.assertEqual(BOOTSTRAP.database_state(self.database), 'initialized')

    def test_empty_users_allows_retry_after_interrupted_seed(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('CREATE TABLE users (id INTEGER PRIMARY KEY)')
        self.assertEqual(BOOTSTRAP.database_state(self.database), 'uninitialized')


class ConfigTests(unittest.TestCase):
    def test_panel_env_keeps_secrets_distinct_and_omits_seed_password(self):
        values = {
            'services_gameap_panel_auth_secret': secrets.token_hex(32),
            'services_gameap_panel_encryption_key': secrets.token_hex(32),
            'services_gameap_panel_admin_password': secrets.token_urlsafe(32),
            'services_gameap_panel_http_port': 18025,
            'services_gameap_panel_grpc_port': 41718,
            'services_gameap_panel_grpc_external_host': '192.0.2.7',
        }
        template = ROOT / 'roles/services/gameap_panel/templates/config.env.j2'
        rendered = Environment(undefined=StrictUndefined).from_string(template.read_text()).render(values)
        env = dict(line.split('=', 1) for line in rendered.splitlines())
        self.assertEqual(env['AUTH_SECRET'], values['services_gameap_panel_auth_secret'])
        self.assertEqual(env['ENCRYPTION_KEY'], values['services_gameap_panel_encryption_key'])
        self.assertNotIn(values['services_gameap_panel_admin_password'], rendered)
        self.assertNotIn('ADMIN_PASSWORD', env)
        self.assertEqual(env['GRPC_EXTERNAL_HOST'], '192.0.2.7')

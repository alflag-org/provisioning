#!/usr/bin/python3
"""Seed through the vendor binary without retaining or logging the admin password."""

import ctypes
from contextlib import closing
import json
import os
from pathlib import Path
import pwd
import resource
import shlex
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request

DATABASE = Path('/var/lib/gameap/db.sqlite')


def database_state(path):
    if not path.exists():
        return 'uninitialized'
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'users' not in tables or db.execute('SELECT count(*) FROM users').fetchone()[0] == 0:
            return 'uninitialized'
        if not {'roles', 'assigned_roles'} <= tables:
            raise ValueError('Incomplete administrator initialization')
        admin = db.execute("SELECT 1 FROM users u JOIN assigned_roles a ON a.entity_id=u.id "
                           "JOIN roles r ON r.id=a.role_id WHERE r.name='admin' AND a.entity_type=? LIMIT 1",
                           ('Gameap\\Models\\User',)).fetchone()
        if not admin or db.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'wal':
            raise ValueError('Administrator or WAL verification failed')
    return 'initialized'


def seed():
    credentials = json.load(sys.stdin)
    password = credentials['password']
    if not isinstance(password, str) or not 16 <= len(password.encode()) <= 72 or '\x00' in password:
        raise ValueError('Invalid initial administrator password')
    if database_state(DATABASE) == 'initialized':
        return
    service = subprocess.run(['systemctl', 'is-active', 'gameap.service'],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    if service.stdout.strip() not in (b'inactive', b'failed', b'unknown'):
        raise ValueError('Panel must be stopped for initialization')
    environment = {'PATH': '/usr/bin:/bin', 'HOME': '/var/lib/gameap'}
    for line in Path('/etc/gameap/config.env').read_text().splitlines():
        if not line or line.startswith('#'):
            continue
        key, value = line.split('=', 1)
        parsed = shlex.split(value)
        if len(parsed) != 1:
            raise ValueError('Invalid configuration')
        environment[key] = parsed[0]
    environment.update(ADMIN_PASSWORD=password, ADMIN_LOGIN=credentials['login'], ADMIN_EMAIL=credentials['email'])
    account = pwd.getpwnam('gameap')
    parent = os.getpid()

    def child_setup():
        os.umask(0o077)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        os.setgroups([])
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
        # Bound the child even if the controller kills this helper without cleanup.
        if ctypes.CDLL(None).prctl(1, signal.SIGKILL) != 0 or os.getppid() != parent:
            os._exit(1)

    def interrupted(number, frame):
        raise InterruptedError('Initialization interrupted')

    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, interrupted)
    process = subprocess.Popen(['/usr/bin/gameap'], cwd='/var/lib/gameap', env=environment,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, preexec_fn=child_setup, start_new_session=True)
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('Panel initialization exited')
            try:
                with urllib.request.urlopen('http://127.0.0.1:' + environment['HTTP_PORT'] + '/', timeout=2) as response:
                    if response.status == 200 and database_state(DATABASE) == 'initialized':
                        return
            except (OSError, sqlite3.Error):
                pass
            time.sleep(0.5)
        raise TimeoutError('Panel initialization timed out')
    finally:
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(number, signal.SIG_IGN)
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        except ProcessLookupError:
            process.wait()


def main():
    try:
        action = sys.argv[1]
        if action == 'seed':
            seed()
        elif action == 'inspect':
            print(database_state(DATABASE))
        elif action == 'verify':
            if database_state(DATABASE) != 'initialized':
                raise ValueError('Database requires initialization')
        else:
            raise ValueError('Invalid action')
        return 0
    except Exception:
        # Exceptions can contain SQL data, environment contents, or credentials.
        print('GameAP database initialization or verification failed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())

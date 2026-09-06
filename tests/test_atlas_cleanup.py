"""Exercise the real Atlas supervisor with synthetic secret consumers."""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def running(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


class AtlasCleanupTests(unittest.TestCase):
    def test_timeout(self):
        self.check_cleanup("timeout", 124)

    def test_sigterm(self):
        self.check_cleanup("signal", 143)

    def test_normal_exit_with_surviving_descendant(self):
        self.check_cleanup("normal", 0)

    def check_cleanup(self, mode, expected):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            etc = root / "etc"
            etc.mkdir()
            (etc / "host.yml").write_text("version: 1\nhost:\n  id: synthetic-test\n")
            (etc / "config.yml").write_text(
                f"runtime:\n  python:\n    version: '{sys.version_info.major}.{sys.version_info.minor}'\n"
                f"programs:\n  probe:\n    root: {root}\n    runtime:\n      type: python\n      venv: probe\n")
            python = root / "venvs/probe/bin/python"
            python.parent.mkdir(parents=True)
            python.symlink_to(sys.executable)
            marker = root / "ready.json"
            child = root / "consumer.py"
            child.write_text('''import json, os, pathlib, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
r, w = os.pipe()
pid = os.fork()
if pid == 0:
    os.close(r)
    os.write(w, b"ready")
    os.close(w)
    time.sleep(60)
    os._exit(0)
os.close(w)
os.read(r, 5)
os.close(r)
pathlib.Path(os.environ["TEST_READY"]).write_text(json.dumps({"pids": [os.getpid(), pid], "path": sys.argv[-1][1:]}))
if os.environ["TEST_MODE"] != "normal":
    time.sleep(60)
''')
            command = root / "commands/probe.py"
            command.parent.mkdir()
            command.write_text(f'''import importlib.util, sys
from pathlib import Path
from atlas_core.secrets import SecretResolver
spec = importlib.util.spec_from_file_location("provision", {str(ROOT / 'commands/provision.py')!r})
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
p = SecretResolver({{"mysql.backup.password": "id"}}, lambda ids: {{"id": "synthetic-secret"}})
raise SystemExit(m.run(Path({str(child)!r}), {{"password": "mysql.backup.password"}}, [], provider=p, executable=sys.executable))
''')
            env = {key: value for key, value in os.environ.items() if not key.startswith("ATLAS_")}
            env.update(ATLAS_HOME=str(root / "home"), ATLAS_ETC_DIR=str(etc),
                       ATLAS_VAR_DIR=str(root / "var"), ATLAS_VENVS_DIR=str(root / "venvs"),
                       TEST_READY=str(marker), TEST_MODE=mode)
            argv = [sys.executable, "-m", "atlas.cli", "run"]
            if mode == "timeout":
                argv += ["--timeout", "2"]
            process = subprocess.Popen([*argv, "probe"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            data = None
            try:
                deadline = time.monotonic() + 10
                while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                if not marker.exists():
                    stdout, stderr = process.communicate(timeout=5)
                    self.fail(f"consumer did not start: {stdout!r} {stderr!r}")
                data = json.loads(marker.read_text())
                if mode == "signal":
                    process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, expected, stderr)
                deadline = time.monotonic() + 2
                while any(running(pid) for pid in data["pids"]) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(any(running(pid) for pid in data["pids"]))
                self.assertFalse(Path(data["path"]).parent.exists())
                self.assertNotIn(b"synthetic-secret", stdout + stderr)
                record = json.loads((root / "var/logs/runs.jsonl").read_text().splitlines()[-1])
                self.assertEqual(record["timed_out"], mode == "timeout")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
                if data:
                    for pid in data["pids"]:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    path = Path(data["path"]).parent
                    if path.parent == Path("/dev/shm") and path.name.startswith("atlas-"):
                        shutil.rmtree(path, ignore_errors=True)

"""Check the runner's custom cleanup script without applying its Ansible role."""

from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

from jinja2 import Environment, StrictUndefined


ROOT = Path(__file__).resolve().parents[1]


class RunnerCleanupTests(unittest.TestCase):
    def test_cleanup_preserves_other_instances_and_rejects_symlinked_roots(self):
        environment = Environment(undefined=StrictUndefined)
        environment.filters['quote'] = shlex.quote
        template = environment.from_string(
            (ROOT / 'roles/services/github_runner/templates/cleanup.sh.j2').read_text()
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work, home, other = (root / name for name in ('work', 'home', 'other'))
            for path in (work, home, other):
                path.mkdir()
            retained = other / 'credential'
            retained.write_text('retained')
            (work / 'external').symlink_to(other, target_is_directory=True)
            (work / 'checkout').mkdir()
            (work / 'checkout/secret').write_text('temporary')
            (home / '.netrc').write_text('temporary')
            current = work / '_temp'
            current.mkdir()
            (current / 'event.json').write_text('{}')
            scripts = []
            for phase in ('start', 'end'):
                script = root / (phase + '.sh')
                script.write_text(template.render(
                    services_github_runner_work=str(work),
                    services_github_runner_home=str(home), item=phase,
                ))
                subprocess.run(['sh', str(script)], check=True, capture_output=True, timeout=5)
                if phase == 'start':
                    self.assertTrue((current / 'event.json').exists())
                    self.assertFalse((work / 'checkout').exists())
                scripts.append(script)
            self.assertEqual(list(work.iterdir()), [])
            self.assertEqual(list(home.iterdir()), [])
            self.assertEqual(retained.read_text(), 'retained')
            work.rmdir()
            work.symlink_to(other, target_is_directory=True)
            result = subprocess.run(['sh', str(scripts[0])], capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(retained.read_text(), 'retained')

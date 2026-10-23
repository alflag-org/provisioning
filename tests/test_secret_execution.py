"""Check secret serialization and child ownership without starting Ansible or Atlas."""

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, call, patch

import yaml

from atlas_core.secrets import SecretConfigurationError, SecretResolutionError
from commands import provision


class SecretExecutionTests(unittest.TestCase):
    def test_declarations_accept_mapping_and_reject_duplicate_or_reserved_variables(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'required.yml'
            path.write_text('required_secrets:\n  password: fixture.secret\n')
            self.assertEqual(provision.declarations(path), {'password': 'fixture.secret'})
            for content in (
                'required_secrets:\n  password: fixture.secret\n  password: other.secret\n',
                'required_secrets:\n  ansible_connection: fixture.secret\n',
            ):
                with self.subTest(content=content):
                    path.write_text(content)
                    with self.assertRaises(SecretConfigurationError):
                        provision.declarations(path)

    def test_run_serializes_literal_secrets_privately_and_preserves_child_exit_status(self):
        literal = "{{ lookup('pipe', 'false') }}\nsynthetic-secret"
        provider = Mock(get_many=Mock(return_value={'fixture.secret': literal}))
        child = Mock()
        child.wait.return_value = 2
        with tempfile.TemporaryDirectory() as directory:
            paths = []

            def spawn(argv, **options):
                path = Path(argv[-1][1:])
                paths.append(path)
                document = yaml.compose(path.read_text())
                self.assertEqual(
                    {key.value: (value.tag, value.value) for key, value in document.value},
                    {'password': ('!unsafe', literal), 'setup_url': ('!unsafe', 'one-shot {{ literal }}')},
                )
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(argv[0], '/synthetic-venv/bin/python')
                self.assertIn('/synthetic-venv/bin/ansible-playbook', argv)
                self.assertNotIn(literal, repr(argv) + repr(options['env']))
                self.assertNotIn('untrusted', repr(options['env']))
                self.assertEqual(options['stdout'], subprocess.DEVNULL)
                self.assertEqual(options['stderr'], subprocess.DEVNULL)
                return child

            def stop(process):
                self.assertIs(process, child)
                self.assertTrue(paths[0].exists())

            output = io.StringIO()
            with patch.object(provision, 'get_run_directory', return_value=None), patch.object(
                provision, 'temporary_run_directory', side_effect=lambda: tempfile.TemporaryDirectory(dir=directory)
            ), patch.object(provision.subprocess, 'Popen', side_effect=spawn), patch.object(
                provision, '_stop', side_effect=stop
            ) as stopped, patch.dict(os.environ, {
                'ATLAS_VENV': '/synthetic-venv', 'ANSIBLE_CALLBACK_PLUGINS': 'untrusted',
            }), redirect_stdout(output), redirect_stderr(output):
                result = provision.run(
                    Path('unused'), {'password': 'fixture.secret'}, [], provider=provider,
                    transient={'setup_url': 'one-shot {{ literal }}'},
                )
            self.assertEqual(result, 2)
            self.assertNotIn(literal, output.getvalue())
            self.assertIn('status 2', output.getvalue())
            stopped.assert_called_once()
            self.assertFalse(paths[0].parent.exists())

    def test_unresolved_secrets_and_invalid_transient_inputs_never_start_a_child(self):
        with patch.object(provision.subprocess, 'Popen') as spawn:
            with self.assertRaises(SecretResolutionError):
                provision.run(Path('unused'), {'password': 'fixture.secret'}, [],
                              provider=Mock(get_many=Mock(return_value={})))
            for transient in ({'password': 'one-shot'}, {'ansible_connection': 'local'}, {'setup_url': ''}):
                with self.subTest(transient=transient), self.assertRaises(SecretConfigurationError):
                    provision.run(Path('unused'), {'password': 'fixture.secret'}, [], transient=transient)
            spawn.assert_not_called()

    def test_signal_during_spawn_stops_owned_child_before_removing_secret_file(self):
        child = Mock()
        paths = []

        def spawn(argv, **options):
            paths.append(Path(argv[-1][1:]))
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return child

        def stop(process):
            self.assertIs(process, child)
            self.assertTrue(paths[0].exists())

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(provision, 'get_run_directory', return_value=None), patch.object(
                provision, 'temporary_run_directory', side_effect=lambda: tempfile.TemporaryDirectory(dir=directory)
            ), patch.object(provision.subprocess, 'Popen', side_effect=spawn), patch.object(
                provision, '_stop', side_effect=stop
            ) as stopped:
                with self.assertRaises(SystemExit) as error:
                    provision.run(Path('unused'), {}, [])
            self.assertEqual(error.exception.code, 143)
            stopped.assert_called_once_with(child)
            self.assertFalse(paths[0].parent.exists())

    def test_stop_escalates_for_surviving_descendants_after_leader_exit(self):
        child = Mock(pid=12345)
        with patch.object(provision.os, 'killpg') as kill:
            provision._stop(child)
        self.assertEqual(kill.call_args_list, [
            call(child.pid, signal.SIGTERM), call(child.pid, signal.SIGKILL),
        ])

    def test_check_mode_does_not_prompt_for_one_shot_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'required.yml'
            path.write_text('required_secrets: {}\n')
            with patch.object(provision.getpass, 'getpass') as prompt, patch.object(
                provision, 'run', return_value=0,
            ) as run:
                self.assertEqual(provision.main([
                    'unused', '--limit', 'fixture', '--required-secrets', str(path),
                    '--check', '--prompt-secret', 'setup_url',
                ]), 0)
            prompt.assert_not_called()
            self.assertEqual(run.call_args.kwargs['transient'], {})

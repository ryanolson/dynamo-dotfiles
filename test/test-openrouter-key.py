"""Run openrouter-key with stub fleet and op commands."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'dot_local/bin/executable_openrouter-key'
REF = 'op://Fleet/OpenRouter-WCIE-Limited/credential'
PYTHON = os.environ.get('OPENROUTER_KEY_PYTHON', 'python3')


class OpenRouterKey(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.bin = self.home / 'bin'
        self.bin.mkdir()
        self.stub('fleet', 'printf "%s|" fleet "$@" >> "$HOME/calls"; echo >> "$HOME/calls"\n'
                           '[ -n "${FLEET_FAIL:-}" ] && exit 1\nprintf "fleet-key"')
        self.stub('op', 'printf "%s|" op "$@" >> "$HOME/calls"; echo >> "$HOME/calls"\n'
                        '[ -n "${OP_FAIL:-}" ] && exit 1\nprintf "op-key"')
        self.stub('show', 'printf "%s\\n" "$OPENROUTER_API_KEY" "${LINEAR_API_KEY:-none}" "$@"')

    def stub(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o755)

    def enroll(self):
        state = self.home / '.local/state/fleet'
        state.mkdir(parents=True)
        (state / 'node.crt').write_text('cert')

    def run_key(self, *args, **env):
        full = {'HOME': str(self.home), 'PATH': f'{self.bin}:/usr/bin:/bin', **env}
        return subprocess.run([PYTHON, str(SCRIPT), *args], env=full, capture_output=True, text=True, timeout=30)

    def calls(self):
        calls = self.home / 'calls'
        return calls.read_text().splitlines() if calls.exists() else []

    def test_an_enrolled_machine_prints_the_key_from_fleet_only(self):
        self.enroll()
        result = self.run_key()
        self.assertEqual((result.returncode, result.stdout), (0, 'fleet-key\n'))
        self.assertEqual(self.calls(), [f'fleet|secret|get|{REF}|'])

    def test_a_broker_failure_falls_back_to_op(self):
        self.enroll()
        result = self.run_key(FLEET_FAIL='1')
        self.assertEqual(result.stdout, 'op-key\n')
        self.assertEqual(self.calls(), [f'fleet|secret|get|{REF}|', f'op|read|{REF}|'])

    def test_a_machine_that_is_not_enrolled_uses_op(self):
        result = self.run_key()
        self.assertEqual(result.stdout, 'op-key\n')
        self.assertEqual(self.calls(), [f'op|read|{REF}|'])

    def test_no_key_from_either_source_fails(self):
        self.enroll()
        result = self.run_key(FLEET_FAIL='1', OP_FAIL='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertIn(REF, result.stderr)

    def test_a_command_gets_only_the_openrouter_key_and_its_arguments(self):
        self.enroll()
        result = self.run_key('show', 'a b', '--flag')
        self.assertEqual(result.stdout.splitlines(), ['fleet-key', 'none', 'a b', '--flag'])
        self.assertEqual(self.calls(), [f'fleet|secret|get|{REF}|'])

    def test_a_missing_command_is_one_clear_error(self):
        self.enroll()
        result = self.run_key('nope-not-here')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('no nope-not-here on PATH', result.stderr)

    def test_a_failed_key_read_does_not_run_the_command(self):
        result = self.run_key('show', FLEET_FAIL='1', OP_FAIL='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()

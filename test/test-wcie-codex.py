"""Run wcie-codex with stub fleet, op, and codex commands."""
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
WCIE = ROOT / 'dot_local/bin/executable_wcie-codex'
CONFIG = ROOT / 'dot_codex/wcie.config.toml'
REF = 'op://Fleet/OpenRouter-WCIE-Limited/credential'


class WcieCodex(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.bin = self.home / 'bin'
        self.bin.mkdir()
        self.stub('codex', 'printf "%s|" "$OPENROUTER_API_KEY" "$@" > "$HOME/got"')
        self.stub('fleet', 'printf "%s|" fleet "$@" >> "$HOME/calls"; echo >> "$HOME/calls"\n'
                           '[ -n "${FLEET_FAIL:-}" ] && exit 1\nprintf "fleet-key"')
        self.stub('op', 'printf "%s|" op "$@" >> "$HOME/calls"; echo >> "$HOME/calls"\nprintf "op-key"')

    def stub(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o755)

    def enroll(self):
        state = self.home / '.local/state/fleet'
        state.mkdir(parents=True)
        (state / 'node.crt').write_text('cert')

    def run_wcie(self, *args, **env):
        full = {'HOME': str(self.home), 'PATH': f'{self.bin}:/usr/bin:/bin', **env}
        return subprocess.run(['bash', str(WCIE), *args], env=full, capture_output=True, text=True, timeout=30)

    def calls(self):
        calls = self.home / 'calls'
        return calls.read_text().splitlines() if calls.exists() else []

    def test_an_enrolled_machine_gets_the_key_through_fleet(self):
        self.enroll()
        result = self.run_wcie('exec', 'hi')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / 'got').read_text(), 'fleet-key|-p|wcie|exec|hi|')
        self.assertEqual(self.calls(), [f'fleet|secret|get|{REF}|'])
        self.assertNotIn('fleet-key', result.stdout + result.stderr)

    def test_a_broker_failure_falls_back_to_op(self):
        self.enroll()
        result = self.run_wcie(FLEET_FAIL='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / 'got').read_text(), 'op-key|-p|wcie|')
        self.assertEqual(self.calls()[-1], f'op|read|{REF}|')

    def test_a_machine_that_is_not_enrolled_uses_op(self):
        result = self.run_wcie()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([c.split('|')[0] for c in self.calls()], ['op'])

    def test_no_key_from_either_source_fails_without_running_codex(self):
        self.stub('op', 'exit 1')
        result = self.run_wcie()
        self.assertEqual(result.returncode, 1)
        self.assertIn('could not read', result.stderr)
        self.assertFalse((self.home / 'got').exists())

    def test_the_profile_file_is_valid_toml_for_the_provider_it_names(self):
        config = tomllib.loads(CONFIG.read_text())
        provider = config['model_providers'][config['model_provider']]
        self.assertEqual(provider['env_key'], 'OPENROUTER_API_KEY')


if __name__ == '__main__':
    unittest.main()

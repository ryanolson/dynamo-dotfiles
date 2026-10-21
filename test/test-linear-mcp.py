"""Run linear-mcp with stub fleet, op, and npx commands."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
LINEAR = ROOT / 'dot_local/bin/executable_linear-mcp'
REF = 'op://Fleet/Linear Stargate Command/credential'
KEY = 'lin_api_test-key'
BASH = shutil.which('bash')


class LinearMcp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.bin = self.home / 'bin'
        self.bin.mkdir()
        # npx records what the MCP server would see: its arguments and the one variable that carries the key.
        self.stub('npx', 'printf "%s\\n" "$@" > "$HOME/argv"; printf "%s" "${LINEAR_AUTH:-}" > "$HOME/auth"\n'
                         'echo protocol-output')
        self.stub('fleet', 'printf "%s|" fleet "$@" >> "$HOME/calls"; echo >> "$HOME/calls"\n'
                           '[ -n "${FLEET_FAIL:-}" ] && exit 1\nprintf "%s\\n" "' + KEY + '"')
        self.stub('op', 'printf "%s|" op "$@" >> "$HOME/calls"; echo >> "$HOME/calls"\nprintf "%s" "op-' + KEY + '"')

    def stub(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o755)

    def enroll(self):
        state = self.home / '.local/state/fleet'
        state.mkdir(parents=True)
        (state / 'node.crt').write_text('cert')

    def run_linear(self, **env):
        full = {'HOME': str(self.home), 'PATH': f'{self.bin}:/usr/bin:/bin', **env}
        # An absolute bash: a test may leave PATH without it to hide npx.
        return subprocess.run([BASH, str(LINEAR)], env=full, capture_output=True, text=True, timeout=30)

    def calls(self):
        calls = self.home / 'calls'
        return calls.read_text().splitlines() if calls.exists() else []

    def test_an_enrolled_machine_reads_the_key_from_fleet(self):
        self.enroll()
        result = self.run_linear()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [f'fleet|secret|get|{REF}|'])
        self.assertEqual((self.home / 'auth').read_text(), f'Bearer {KEY}')

    def test_the_key_reaches_npx_only_through_the_environment(self):
        self.enroll()
        result = self.run_linear()
        argv = (self.home / 'argv').read_text()
        self.assertNotIn(KEY, argv)
        self.assertNotIn(KEY, result.stderr)
        # mcp-remote expands ${LINEAR_AUTH} itself; the shell must not.
        self.assertEqual(argv.splitlines()[:4],
                         ['-y', 'mcp-remote@0.15.2', 'https://mcp.linear.app/mcp', '--header'])
        self.assertEqual(argv.splitlines()[4], 'Authorization:${LINEAR_AUTH}')

    def test_stdout_is_only_the_server_output(self):
        # stdout is the MCP protocol channel: a stray line from the wrapper would corrupt it.
        self.enroll()
        self.assertEqual(self.run_linear().stdout, 'protocol-output\n')

    def test_a_broker_failure_falls_back_to_op(self):
        self.enroll()
        result = self.run_linear(FLEET_FAIL='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([c.split('|')[0] for c in self.calls()], ['fleet', 'op'])
        self.assertEqual(self.calls()[-1], f'op|read|{REF}|')
        self.assertEqual((self.home / 'auth').read_text(), f'Bearer op-{KEY}')

    def test_a_machine_that_is_not_enrolled_uses_op(self):
        result = self.run_linear()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([c.split('|')[0] for c in self.calls()], ['op'])

    def test_no_key_source_is_an_error_and_starts_nothing(self):
        (self.bin / 'op').unlink()
        result = self.run_linear()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('linear-mcp:', result.stderr)
        self.assertFalse((self.home / 'argv').exists())

    def test_an_empty_key_is_an_error(self):
        self.enroll()
        self.stub('fleet', 'echo')
        (self.bin / 'op').unlink()
        result = self.run_linear()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / 'argv').exists())

    def test_missing_npx_is_an_error_that_names_it(self):
        self.enroll()
        (self.bin / 'npx').unlink()
        result = self.run_linear(PATH=str(self.bin))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('npx', result.stderr)


if __name__ == '__main__':
    unittest.main()

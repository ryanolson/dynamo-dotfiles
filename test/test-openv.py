"""Run openv with stub fleet and op commands, and render the reference file."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
OPENV = ROOT / 'dot_local/bin/executable_openv'
REFS = ('# Secret references\nHF_TOKEN=op://Fleet/HuggingFace/credential\n'
        'NGC_API_KEY=op://Fleet/NGC NIM Operations PAT/credential\n')
# The command prints what it got into a file, so the test sees the values and openv's own output does not.
SHOW = ['sh', '-c', 'printf "%s|%s|%s" "$HF_TOKEN" "$NGC_API_KEY" "$1" > "$HOME/got"', 'sh', 'a b']


class Openv(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.bin = self.home / 'bin'
        self.bin.mkdir()
        self.dynamo = self.home / '.config/dynamo'
        self.dynamo.mkdir(parents=True)
        self.stub('fleet', 'printf "%s|" fleet "$@" >> "$HOME/calls"; echo >> "$HOME/calls"\n'
                           '[ -n "${FLEET_FAIL:-}" ] && case "$3" in *NGC*) exit 1 ;; esac\n'
                           'printf "val-%s" "${3##*/Fleet/}"')
        self.stub('op', 'printf "%s|" op "$@" >> "$HOME/calls"; echo >> "$HOME/calls"')

    def stub(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o755)

    def enroll(self):
        state = self.home / '.local/state/fleet'
        state.mkdir(parents=True)
        (state / 'node.crt').write_text('cert')

    def openv(self, refs=REFS, args=SHOW, **env):
        if refs is not None:
            (self.dynamo / 'dev.env.fleet').write_text(refs)
        full = {'HOME': str(self.home), 'PATH': f'{self.bin}:/usr/bin:/bin', **env}
        return subprocess.run(['bash', str(OPENV), *args], env=full, capture_output=True, text=True, timeout=30)

    def calls(self):
        calls = self.home / 'calls'
        return calls.read_text().splitlines() if calls.exists() else []

    def test_an_enrolled_machine_gets_the_values_through_fleet(self):
        self.enroll()
        result = self.openv()
        self.assertEqual(result.returncode, 0, result.stderr)
        got = (self.home / 'got').read_text()
        self.assertEqual(got, 'val-HuggingFace/credential|val-NGC NIM Operations PAT/credential|a b')
        self.assertNotIn('val-', result.stdout + result.stderr)
        self.assertEqual(self.calls(), ['fleet|secret|get|op://Fleet/HuggingFace/credential|',
                                        'fleet|secret|get|op://Fleet/NGC NIM Operations PAT/credential|'])

    def test_a_broker_failure_falls_back_to_op_for_every_variable(self):
        # One source for the whole command: no mix of broker values and op values.
        self.enroll()
        result = self.openv(FLEET_FAIL='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[-1], f'op|run|--env-file|{self.dynamo}/dev.env.fleet|--|{"|".join(SHOW)}|')

    def test_a_machine_that_is_not_enrolled_uses_op(self):
        result = self.openv()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([c.split('|')[0] for c in self.calls()], ['op'])

    def test_no_broker_and_no_op_is_an_error(self):
        (self.bin / 'op').unlink()
        result = self.openv()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('openv:', result.stderr)

    def test_a_bad_line_is_an_error(self):
        self.enroll()
        for line in ('HF_TOKEN=hf-token', 'hf=op://Fleet/a/b', 'HF_TOKEN=op://Fleet/a', 'HF_TOKEN=op://Fleet/a/b;x',
                     'HF_TOKEN'):
            result = self.openv(refs=line + '\n')
            self.assertNotEqual(result.returncode, 0, line)
        self.assertEqual(self.calls(), [])

    def test_no_references_or_no_command_is_an_error(self):
        self.assertNotEqual(self.openv(refs='# none\n').returncode, 0)
        self.assertNotEqual(self.openv(refs=None).returncode, 0)
        self.assertNotEqual(self.openv(args=()).returncode, 0)
        self.assertEqual(self.calls(), [])


class ReferenceFile(unittest.TestCase):
    def render(self, template, enabled):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'chezmoi.yaml'
            config.write_text(f'data:\n  machine_class: "sudo"\n  onepassword:\n    enabled: {str(enabled).lower()}\n')
            return subprocess.run(['chezmoi', 'execute-template', '--config', str(config), '--source', str(ROOT)],
                                  input=template.read_text(), capture_output=True, text=True, check=True).stdout

    def test_every_machine_gets_the_same_references(self):
        for enabled in (True, False):
            out = self.render(ROOT / 'dot_config/dynamo/dev.env.fleet.tmpl', enabled)
            self.assertIn('HF_TOKEN=op://Fleet/HuggingFace/credential\n', out)
            self.assertIn('NGC_API_KEY=op://Fleet/NGC NIM Operations PAT/credential\n', out)
            self.assertNotIn('.config/dynamo/dev.env.fleet', self.render(ROOT / '.chezmoiignore', enabled).split())


if __name__ == '__main__':
    unittest.main()

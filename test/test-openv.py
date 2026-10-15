"""Run openv with stub op and fleet commands, and render the broker map for each 1Password setting."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
OPENV = ROOT / 'dot_local/bin/executable_openv'
OP_FILE = '# Secret references\nHF_TOKEN=op://Development/HuggingFace/credential\n'
OFF_FILE = '# 1Password integration is disabled for this machine.\n'
FLEET_FILE = '# Broker secret names\nHF_TOKEN=hf-token\nNGC_API_KEY=ngc-api-key\n'


class Openv(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.bin = self.home / 'bin'
        self.bin.mkdir()
        self.dynamo = self.home / '.config/dynamo'
        self.dynamo.mkdir(parents=True)
        for name in ('op', 'fleet'):
            stub = self.bin / name
            stub.write_text(f'#!/bin/sh\nprintf "%s|" {name} "$@" >> "$HOME/calls"; echo >> "$HOME/calls"\n')
            stub.chmod(0o755)

    def openv(self, op=None, fleet=None, args=('cmd', 'a b')):
        for name, text in (('dev.env.op', op), ('dev.env.fleet', fleet)):
            if text is not None:
                (self.dynamo / name).write_text(text)
        env = {'HOME': str(self.home), 'PATH': f'{self.bin}:/usr/bin:/bin'}
        return subprocess.run(['bash', str(OPENV), *args], env=env, capture_output=True, text=True, timeout=30)

    def calls(self):
        calls = self.home / 'calls'
        return calls.read_text().splitlines() if calls.exists() else []

    def test_references_use_op(self):
        result = self.openv(op=OP_FILE, fleet=FLEET_FILE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [f'op|run|--env-file|{self.dynamo}/dev.env.op|--|cmd|a b|'])

    def test_without_references_the_broker_is_used(self):
        result = self.openv(op=OFF_FILE, fleet=FLEET_FILE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ['fleet|secret|env|HF_TOKEN=hf-token|NGC_API_KEY=ngc-api-key|--|cmd|a b|'])

    def test_no_op_file_uses_the_broker(self):
        self.openv(fleet=FLEET_FILE)
        self.assertTrue(self.calls()[0].startswith('fleet|secret|env|'))

    def test_nothing_to_use_is_an_error(self):
        for files in ({'op': OFF_FILE}, {'op': OFF_FILE, 'fleet': '# none\n'}, {}):
            result = self.openv(**files)
            self.assertNotEqual(result.returncode, 0, files)
            self.assertIn('openv:', result.stderr)
        self.assertEqual(self.calls(), [])

    def test_a_bad_broker_line_is_an_error(self):
        # A line that is not VAR=name could put an option or a command into the fleet arguments.
        for line in ('HF_TOKEN=--x', 'hf=hf-token', 'HF_TOKEN=hf token', 'HF_TOKEN'):
            result = self.openv(fleet=line + '\n')
            self.assertNotEqual(result.returncode, 0, line)
        self.assertEqual(self.calls(), [])

    def test_missing_command_is_an_error(self):
        result = self.openv(fleet=FLEET_FILE, args=())
        self.assertNotEqual(result.returncode, 0)


class FleetMap(unittest.TestCase):
    def render(self, template, enabled):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'chezmoi.yaml'
            config.write_text(f'data:\n  machine_class: "sudo"\n  onepassword:\n    enabled: {str(enabled).lower()}\n')
            return subprocess.run(['chezmoi', 'execute-template', '--config', str(config), '--source', str(ROOT)],
                                  input=template.read_text(), capture_output=True, text=True, check=True).stdout

    def test_the_map_names_each_variable(self):
        out = self.render(ROOT / 'dot_config/dynamo/dev.env.fleet.tmpl', False)
        self.assertIn('HF_TOKEN=hf-token\n', out)
        self.assertIn('NGC_API_KEY=ngc-api-key\n', out)

    def test_only_a_machine_without_1password_gets_the_map(self):
        self.assertIn('.config/dynamo/dev.env.fleet', self.render(ROOT / '.chezmoiignore', True).split())
        self.assertNotIn('.config/dynamo/dev.env.fleet', self.render(ROOT / '.chezmoiignore', False).split())


if __name__ == '__main__':
    unittest.main()

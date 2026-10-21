"""Run wcie-codex with stub fleet, op, and codex commands."""
import json
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
K3 = 'moonshotai/kimi-k3'


def live_entry(slug, priority):
    return {'slug': slug, 'display_name': slug.upper(), 'priority': priority, 'availability_nux': {'message': 'try'},
            'model_messages': {'instructions_template': f'{slug} instructions'}}


LIVE_CATALOG = {'models': [live_entry('gpt-6.1-sol', 0), live_entry('gpt-6-astra', 2), live_entry('gpt-6-luna', 4)]}


class WcieCodex(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.bin = self.home / 'bin'
        self.bin.mkdir()
        codex_home = self.home / '.codex'
        codex_home.mkdir()
        # The deployed profile also holds TUI state that codex writes back.
        (codex_home / 'wcie.config.toml').write_text(
            CONFIG.read_text() + '\n[tui]\nscreen_reader_detection_done = true\n')
        (self.home / 'live.json').write_text(json.dumps(LIVE_CATALOG))
        self.stub('codex', 'if [ "$1" = debug ]; then\n'
                           '  [ -n "${DEBUG_FAIL:-}" ] && exit 1\n  cat "$HOME/live.json"; exit 0\nfi\n'
                           'printf "%s\\n" "$OPENROUTER_API_KEY" "$@" > "$HOME/got"')
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
        return subprocess.run(['python3', str(WCIE), *args], env=full, capture_output=True, text=True, timeout=30)

    def calls(self):
        calls = self.home / 'calls'
        return calls.read_text().splitlines() if calls.exists() else []

    def got(self):
        key, *argv = (self.home / 'got').read_text().splitlines()
        return key, argv

    def cache(self):
        return self.home / '.cache/wcie-codex/models.json'

    def catalog_arg(self):
        return f'model_catalog_json={json.dumps(str(self.cache()))}'

    def test_an_enrolled_machine_gets_the_key_through_fleet(self):
        self.enroll()
        result = self.run_wcie('exec', 'hi')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.got(), ('fleet-key', ['-p', 'wcie', '-c', self.catalog_arg(), 'exec', 'hi']))
        self.assertEqual(self.calls(), [f'fleet|secret|get|{REF}|'])
        self.assertNotIn('fleet-key', result.stdout + result.stderr)

    def test_a_broker_failure_falls_back_to_op(self):
        self.enroll()
        result = self.run_wcie(FLEET_FAIL='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.got()[0], 'op-key')
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

    def test_the_catalog_lists_k3_first_then_sol_and_astra_on_openrouter(self):
        self.assertEqual(self.run_wcie().returncode, 0)
        models = json.loads(self.cache().read_text())['models']
        self.assertEqual([m['slug'] for m in sorted(models, key=lambda m: m['priority'])],
                         [K3, 'openai/gpt-6.1-sol', 'openai/gpt-6-astra'])
        self.assertTrue(all(m['auto_review_model_override'] == K3 for m in models))
        k3 = models[0]
        self.assertEqual(k3['model_messages'], {'instructions_template': 'gpt-6.1-sol instructions'})
        self.assertIsNone(models[1]['availability_nux'])

    def test_a_failed_catalog_read_reuses_the_cached_catalog(self):
        self.assertEqual(self.run_wcie().returncode, 0)
        result = self.run_wcie('exec', DEBUG_FAIL='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('using the cached catalog', result.stderr)
        self.assertEqual(self.got()[1], ['-p', 'wcie', '-c', self.catalog_arg(), 'exec'])

    def test_no_catalog_at_all_still_runs_codex_without_one(self):
        result = self.run_wcie('exec', DEBUG_FAIL='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('without the K3/Sol/Astra catalog', result.stderr)
        self.assertEqual(self.got()[1], ['-p', 'wcie', 'exec'])

    def test_app_server_gets_the_profile_as_overrides_and_keeps_trailing_args(self):
        # ACP adapters append their own -c flags, for example hooks.
        result = self.run_wcie('app-server', '-c', 'hooks={}')
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.got()[1]
        self.assertNotIn('-p', argv)
        start = argv.index('app-server')
        self.assertEqual(argv[start:], ['app-server', '-c', 'hooks={}'])
        flags = argv[:start]
        self.assertEqual(flags[-2:], ['-c', self.catalog_arg()])
        pairs = [flags[i + 1] for i in range(0, len(flags) - 2, 2)]
        self.assertTrue(all(flags[i] == '-c' for i in range(0, len(flags), 2)))
        rebuilt = {}
        for pair in pairs:
            key, _, value = pair.partition('=')
            node = rebuilt
            *parents, leaf = self.key_path(key)
            for parent in parents:
                node = node.setdefault(parent, {})
            node[leaf] = tomllib.loads(f'v = {value}')['v']
        self.assertEqual(rebuilt, tomllib.loads(CONFIG.read_text()))

    @staticmethod
    def key_path(dotted):
        # Resolve a dotted TOML key, quoted parts included, to its parts.
        doc = tomllib.loads(f'{dotted} = 0')
        parts = []
        while isinstance(doc, dict):
            (key, doc), = doc.items()
            parts.append(key)
        return parts

    def test_codex_from_an_npx_package_bin_is_skipped(self):
        # npx puts the adapter's node_modules/.bin, with its own codex package, first on PATH.
        shim = self.home / 'npx/node_modules/.bin'
        shim.mkdir(parents=True)
        (shim / 'codex').write_text('#!/bin/sh\ntouch "$HOME/shim-ran"; exit 1\n')
        (shim / 'codex').chmod(0o755)
        result = self.run_wcie('app-server', PATH=f'{shim}:{self.bin}:/usr/bin:/bin')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / 'shim-ran').exists())
        self.assertIn('app-server', self.got()[1])

    def test_the_profile_file_is_valid_toml_for_the_provider_it_names(self):
        config = tomllib.loads(CONFIG.read_text())
        provider = config['model_providers'][config['model_provider']]
        self.assertEqual(provider['env_key'], 'OPENROUTER_API_KEY')

    def test_the_profile_sends_luna_and_terra_work_to_its_model(self):
        config = tomllib.loads(CONFIG.read_text())
        model = config['model']
        self.assertEqual(model, K3)
        self.assertEqual(config['review_model'], model)
        self.assertEqual(config['agents']['default_subagent_model'], model)
        self.assertEqual(config['memories'], {'extract_model': model, 'consolidation_model': model})


if __name__ == '__main__':
    unittest.main()

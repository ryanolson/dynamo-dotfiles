"""Run wcie-codex with stub fleet, op, and codex commands.

WCIE_PYTHON selects the interpreter that runs the launcher (default python3), so the suite can
check an older python3, such as the 3.9 that macOS ships.
"""
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
PYTHON = os.environ.get('WCIE_PYTHON', 'python3')


def live_entry(slug, priority):
    return {'slug': slug, 'display_name': slug.upper(), 'priority': priority, 'availability_nux': {'message': 'try'},
            'model_messages': {'instructions_template': f'{slug} instructions'}}


LIVE_CATALOG = {'models': [live_entry('gpt-6.1-sol', 0), live_entry('gpt-6-astra', 2), live_entry('gpt-6-luna', 4)]}

# The metadata codex gives a model it does not know (models-manager model_info_from_slug), which
# is what K3 ran on before it had a catalog entry.
FALLBACK_FIELDS = {
    'description': None, 'default_reasoning_level': None, 'supported_reasoning_levels': [],
    'shell_type': 'unified_exec', 'supported_in_api': True, 'additional_speed_tiers': [], 'service_tiers': [],
    'default_service_tier': None, 'available_access_programs': None, 'availability_nux': None, 'upgrade': None,
    'include_skills_usage_instructions': False, 'include_plugin_usage_instructions': False,
    'include_apps_usage_instructions': False, 'supports_reasoning_summary_parameter': True,
    'default_reasoning_summary': 'auto', 'support_verbosity': False, 'default_verbosity': None,
    'apply_patch_tool_type': None, 'web_search_tool_type': 'text',
    'truncation_policy': {'mode': 'bytes', 'limit': 10000}, 'supports_image_detail_original': False,
    'context_window': 272000, 'max_context_window': 272000, 'auto_compact_token_limit': None, 'comp_hash': None,
    'effective_context_window_percent': 95, 'experimental_supported_tools': [], 'input_modalities': ['text', 'image'],
    'supports_search_tool': False, 'supports_experimental_context': False, 'use_responses_lite': False,
    'supports_reasoning_effort_updates': False, 'guardian': None, 'node_repl_auto_review_required': False,
    'node_repl_disabled': False, 'model_specialty': None, 'tool_mode': None,
}


class WcieCodex(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.bin = self.home / 'bin'
        self.bin.mkdir()
        self.codex_home = self.home / '.codex'
        self.codex_home.mkdir()
        # The deployed profile also holds TUI state that codex writes back. App-server mode must not
        # see it: it reads the JSON that chezmoi renders from the source profile.
        (self.codex_home / 'wcie.config.toml').write_text(
            CONFIG.read_text() + '\n[tui]\nscreen_reader_detection_done = true\n')
        self.write_profile_json(tomllib.loads(CONFIG.read_text()))
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

    def write_profile_json(self, profile):
        (self.codex_home / 'wcie.profile.json').write_text(json.dumps(profile))

    def enroll(self):
        state = self.home / '.local/state/fleet'
        state.mkdir(parents=True)
        (state / 'node.crt').write_text('cert')

    def run_wcie(self, *args, **env):
        full = {'HOME': str(self.home), 'PATH': f'{self.bin}:/usr/bin:/bin', **env}
        return subprocess.run([PYTHON, str(WCIE), *args], env=full, capture_output=True, text=True, timeout=30)

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

    def test_the_catalog_lists_k3_first_then_astra_on_openrouter(self):
        self.assertEqual(self.run_wcie().returncode, 0)
        models = json.loads(self.cache().read_text())['models']
        self.assertEqual([m['slug'] for m in sorted(models, key=lambda m: m['priority'])],
                         [K3, 'openai/gpt-6-astra'])
        self.assertTrue(all(m['auto_review_model_override'] == K3 for m in models))
        self.assertIsNone(models[1]['availability_nux'])

    def test_the_k3_entry_keeps_the_metadata_codex_gives_an_unknown_model(self):
        self.assertEqual(self.run_wcie().returncode, 0)
        k3 = json.loads(self.cache().read_text())['models'][0]
        self.assertEqual({k: k3.get(k, 'MISSING') for k in FALLBACK_FIELDS}, FALLBACK_FIELDS)
        self.assertEqual(k3['model_messages'], {'instructions_template': 'gpt-6-astra instructions'})

    def test_a_failed_catalog_read_reuses_the_cached_catalog(self):
        self.assertEqual(self.run_wcie().returncode, 0)
        result = self.run_wcie('exec', DEBUG_FAIL='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('using the cached catalog', result.stderr)
        self.assertEqual(self.got()[1], ['-p', 'wcie', '-c', self.catalog_arg(), 'exec'])

    def test_a_cached_catalog_for_another_model_is_not_used(self):
        self.assertEqual(self.run_wcie().returncode, 0)
        profile = tomllib.loads(CONFIG.read_text())
        profile['model'] = 'moonshotai/kimi-k4'
        self.write_profile_json(profile)
        result = self.run_wcie('exec', DEBUG_FAIL='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.got()[1], ['-p', 'wcie', 'exec'])

    def test_a_corrupt_cached_catalog_is_not_used(self):
        self.cache().parent.mkdir(parents=True)
        self.cache().write_text('{"models": [')
        result = self.run_wcie('exec', DEBUG_FAIL='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.got()[1], ['-p', 'wcie', 'exec'])

    def test_an_unwritable_cache_still_runs_codex(self):
        (self.home / '.cache').mkdir()
        (self.home / '.cache/wcie-codex').write_text('a file where the cache directory goes')
        result = self.run_wcie('exec')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.got()[1], ['-p', 'wcie', 'exec'])

    def test_no_catalog_at_all_still_runs_codex_without_one(self):
        result = self.run_wcie('exec', DEBUG_FAIL='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('without the K3/Astra catalog', result.stderr)
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
        self.assertTrue(all(flags[i] == '-c' for i in range(0, len(flags), 2)))
        rebuilt = {}
        for pair in (flags[i + 1] for i in range(0, len(flags) - 2, 2)):
            key, _, value = pair.partition('=')
            node = rebuilt
            *parents, leaf = key.split('.')  # codex splits -c keys on dots
            for parent in parents:
                node = node.setdefault(parent, {})
            node[leaf] = tomllib.loads(f'v = {value}')['v']
        self.assertEqual(rebuilt, tomllib.loads(CONFIG.read_text()))

    def test_app_server_refuses_a_key_that_codex_cannot_take_as_an_override(self):
        profile = tomllib.loads(CONFIG.read_text())
        profile['projects'] = {'/home/me/repo.v2': {'trust_level': 'trusted'}}
        self.write_profile_json(profile)
        result = self.run_wcie('app-server')
        self.assertEqual(result.returncode, 1)
        self.assertIn('/home/me/repo.v2', result.stderr)
        self.assertFalse((self.home / 'got').exists())

    def test_app_server_without_the_rendered_profile_asks_for_chezmoi_apply(self):
        (self.codex_home / 'wcie.profile.json').unlink()
        result = self.run_wcie('app-server')
        self.assertEqual(result.returncode, 1)
        self.assertIn('chezmoi apply', result.stderr)
        self.assertFalse((self.home / 'got').exists())

    def test_the_tui_without_the_rendered_profile_runs_without_the_catalog(self):
        (self.codex_home / 'wcie.profile.json').unlink()
        result = self.run_wcie('exec')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('without the K3/Astra catalog', result.stderr)
        self.assertEqual(self.got()[1], ['-p', 'wcie', 'exec'])

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

    def test_chezmoi_renders_the_profile_json_from_the_source_profile(self):
        template = ROOT / 'dot_codex/wcie.profile.json.tmpl'
        rendered = subprocess.run(['chezmoi', '--source', str(ROOT), 'execute-template'], input=template.read_text(),
                                  capture_output=True, text=True, timeout=30)
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertEqual(json.loads(rendered.stdout), tomllib.loads(CONFIG.read_text()))


if __name__ == '__main__':
    unittest.main()

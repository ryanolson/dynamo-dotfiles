"""Drive codex-review-gate against the installed Codex plugin, with a scratch home and scratch repositories."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'dot_local/bin/executable_codex-review-gate'
REAL_CLAUDE = Path(os.environ.get('CLAUDE_CONFIG_DIR', Path.home() / '.claude'))


def installed_plugin():
    try:
        record = json.loads((REAL_CLAUDE / 'plugins/installed_plugins.json').read_text())
        return Path(record['plugins']['codex@openai-codex'][0]['installPath'])
    except (OSError, KeyError, IndexError, ValueError):
        return None


PLUGIN = installed_plugin()
NODE = shutil.which('node')


@unittest.skipUnless(PLUGIN and NODE, 'needs the installed codex@openai-codex plugin and node')
class ReviewGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        plugins = self.home / '.claude/plugins'
        plugins.mkdir(parents=True)
        # Point at the real plugin code. Its state goes to the scratch data folder, never to the real one.
        record = {'version': 2, 'plugins': {'codex@openai-codex': [{'scope': 'user', 'installPath': str(PLUGIN)}]}}
        (plugins / 'installed_plugins.json').write_text(json.dumps(record))
        self.env = {'HOME': str(self.home), 'PATH': os.path.dirname(NODE) + ':/usr/bin:/bin', 'LANG': 'C.UTF-8',
                    'GIT_CONFIG_NOSYSTEM': '1'}
        self.repo_a = self.home / 'repos/a'
        self.repo_b = self.home / 'repos/b'
        self.worktree = self.home / 'elsewhere/a-wt'
        for repo in (self.repo_a, self.repo_b):
            self.git('init', '-q', '-b', 'main', str(repo))
            self.git('-c', 'user.name=t', '-c', 'user.email=t@example.com', 'commit', '-q', '--allow-empty', '-m', 'init',
                     cwd=repo)
        # The worktree sits outside the scanned depth, so only `git worktree list` can find it.
        self.git('worktree', 'add', '-q', '-b', 'wt', str(self.worktree), cwd=self.repo_a)

    def git(self, *args, cwd=None):
        subprocess.run(['git', *args], cwd=cwd, env=self.env, check=True, capture_output=True, timeout=30)

    def gate(self, *args):
        result = subprocess.run([str(SCRIPT), *args], env=self.env, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=120)
        return result.returncode, result.stdout, result.stderr

    def listed(self, *flags):
        code, out, err = self.gate('list', '--json', *flags)
        self.assertEqual(code, 0, err)
        return {(row['path'], row['enabled']) for row in json.loads(out)}

    def test_toggle_and_filter(self):
        code, _, err = self.gate('enable', str(self.repo_a), str(self.worktree), str(self.repo_b))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.listed('--enabled'),
                         {(str(self.repo_a), True), (str(self.worktree), True), (str(self.repo_b), True)})
        self.assertEqual(self.listed('--disabled'), set())

        code, _, err = self.gate('disable', str(self.repo_a))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.listed('--enabled'), {(str(self.worktree), True), (str(self.repo_b), True)})
        self.assertEqual(self.listed('--disabled'), {(str(self.repo_a), False)})
        self.assertEqual(len(self.listed()), 3)

    def test_subfolder_toggles_its_repository(self):
        sub = self.repo_a / 'deep/er'
        sub.mkdir(parents=True)
        code, _, err = self.gate('enable', str(sub))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.listed('--enabled'), {(str(self.repo_a), True)})

    def test_removed_workspace_is_unresolved(self):
        self.gate('enable', str(self.repo_b))
        shutil.rmtree(str(self.repo_b))
        code, out, err = self.gate('list', '--json')
        self.assertEqual(code, 0, err)
        rows = json.loads(out)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]['path'])
        self.assertTrue(rows[0]['state'].startswith('b-'))
        self.assertTrue(rows[0]['enabled'])

    def test_root_option_limits_the_scan(self):
        self.gate('enable', str(self.repo_a), str(self.repo_b))
        code, out, err = self.gate('list', '--json', '--root', str(self.repo_b))
        self.assertEqual(code, 0, err)
        paths = {row['path'] for row in json.loads(out)}
        self.assertEqual(paths, {str(self.repo_b), None})

    def test_text_output_marks_state(self):
        self.gate('enable', str(self.repo_a))
        code, out, err = self.gate('list')
        self.assertEqual(code, 0, err)
        self.assertIn('on', out.split())
        self.assertIn(str(self.repo_a), out)

    def test_missing_path_fails(self):
        code, _, err = self.gate('enable', str(self.home / 'nope'))
        self.assertEqual(code, 1)
        self.assertIn('nope', err)
        self.assertEqual(self.listed(), set())

    def test_missing_plugin_fails(self):
        (self.home / '.claude/plugins/installed_plugins.json').write_text('{"version": 2, "plugins": {}}')
        for args in (['list'], ['disable', str(self.repo_a)]):
            code, _, err = self.gate(*args)
            self.assertEqual(code, 1)
            self.assertIn('codex@openai-codex', err)


if __name__ == '__main__':
    unittest.main()

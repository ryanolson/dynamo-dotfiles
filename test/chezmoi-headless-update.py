"""Run chezmoi-headless-update against a scratch home, source clone, and bare remote, with no terminal."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'dot_local/bin/executable_chezmoi-headless-update'
CHEZMOI = shutil.which('chezmoi')


class HeadlessUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / 'home'
        self.remote = root / 'remote.git'
        self.upstream = root / 'upstream'
        self.source = self.home / '.local/share/chezmoi'
        # Only ~/.local/bin holds chezmoi, so every run also proves that the script finds it without a login PATH.
        (self.home / '.local/bin').mkdir(parents=True)
        (self.home / '.local/bin/chezmoi').symlink_to(CHEZMOI)
        (self.home / '.config/chezmoi').mkdir(parents=True)
        (self.home / '.config/chezmoi/chezmoi.json').write_text('{}')
        self.env = {
            'HOME': str(self.home), 'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.com',
            'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.com',
        }
        self.git('init', '-q', '--bare', '-b', 'main', str(self.remote))
        self.git('clone', '-q', str(self.remote), str(self.upstream))
        self.push({'dot_a': 'a1\n'})
        self.git('clone', '-q', str(self.remote), str(self.source))
        self.run_cmd([str(self.home / '.local/bin/chezmoi'), 'apply', '--no-tty'])
        self.assertEqual(self.target('.a'), 'a1\n')

    def run_cmd(self, args, **kwargs):
        return subprocess.run(args, env=self.env, check=True, capture_output=True, text=True, timeout=60, **kwargs)

    def git(self, *args, cwd=None):
        return self.run_cmd(['git', *args], cwd=cwd).stdout.strip()

    def push(self, files):
        for name, text in files.items():
            (self.upstream / name).write_text(text)
        self.git('add', '-A', cwd=self.upstream)
        self.git('commit', '-q', '-m', 'change', cwd=self.upstream)
        self.git('push', '-q', 'origin', 'HEAD:main', cwd=self.upstream)

    def target(self, name):
        path = self.home / name
        return path.read_text() if path.exists() else None

    def update(self):
        # A new session has no controlling terminal, as under a bb server. A prompt fails or hangs here.
        result = subprocess.run([str(SCRIPT)], env=self.env, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=60, start_new_session=True)
        return result.returncode, json.loads(result.stdout), result.stderr

    def test_clean_update(self):
        before = self.git('rev-parse', 'HEAD', cwd=self.source)
        self.push({'dot_a': 'a2\n', 'dot_b': 'b1\n'})
        code, report, stderr = self.update()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(report['result'], 'updated')
        self.assertEqual(report['reasons'], [])
        self.assertEqual(report['before'], before)
        self.assertEqual(report['after'], self.git('rev-parse', 'HEAD', cwd=self.upstream))
        self.assertEqual((self.target('.a'), self.target('.b')), ('a2\n', 'b1\n'))

    def test_local_edit_is_kept_and_reported(self):
        (self.home / '.a').write_text('local\n')
        self.push({'dot_a': 'a2\n', 'dot_b': 'b1\n'})
        code, report, _ = self.update()
        self.assertEqual(code, 2)
        self.assertEqual(report['result'], 'needs_attention')
        self.assertIn('apply_failed', report['reasons'])
        self.assertIn('pending', report['reasons'])
        self.assertIn('.a', [entry['path'] for entry in report['pending']])
        self.assertEqual((self.target('.a'), self.target('.b')), ('local\n', 'b1\n'))

    def test_fetch_failure_applies_nothing(self):
        self.push({'dot_b': 'b1\n'})
        self.remote.rename(self.remote.with_name('gone.git'))
        code, report, _ = self.update()
        self.assertEqual(code, 1)
        self.assertEqual(report['result'], 'pull_failed')
        self.assertEqual(report['reasons'], ['fetch_failed'])
        self.assertIsNone(self.target('.b'))

    def test_dirty_source_conflict_leaves_repository_untouched(self):
        before = self.git('rev-parse', 'HEAD', cwd=self.source)
        (self.source / 'dot_a').write_text('dirty\n')
        self.push({'dot_a': 'a2\n', 'dot_b': 'b1\n'})
        code, report, _ = self.update()
        self.assertEqual(code, 1)
        self.assertEqual(report['reasons'], ['merge_failed'])
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=self.source), before)
        self.assertEqual((self.source / 'dot_a').read_text(), 'dirty\n')
        self.assertFalse((self.source / '.git/rebase-merge').exists())
        self.assertFalse((self.source / '.git/MERGE_HEAD').exists())
        self.assertEqual(self.git('stash', 'list', cwd=self.source), '')
        self.assertIsNone(self.target('.b'))

    def test_unrelated_dirty_source_file_survives(self):
        (self.source / 'dot_a').write_text('dirty\n')
        self.push({'dot_b': 'b1\n'})
        code, report, stderr = self.update()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(report['result'], 'updated')
        self.assertEqual(self.target('.b'), 'b1\n')
        self.assertEqual((self.source / 'dot_a').read_text(), 'dirty\n')
        # chezmoi applies the working tree, not HEAD, as an interactive `chezmoi apply` does.
        self.assertEqual(self.target('.a'), 'dirty\n')

    def test_changed_config_template_is_reported(self):
        self.push({'.chezmoi.json.tmpl': '{"data": {"changed": true}}\n', 'dot_b': 'b1\n'})
        code, report, _ = self.update()
        self.assertEqual(code, 2)
        self.assertIn('config_template_changed', report['reasons'])
        self.assertEqual(self.target('.b'), 'b1\n')
        self.assertEqual((self.home / '.config/chezmoi/chezmoi.json').read_text(), '{}')


if __name__ == '__main__':
    unittest.main()

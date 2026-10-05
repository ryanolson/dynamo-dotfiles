"""Apply only .chezmoiremove to a scratch home and check which npm installs it deletes."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PREFIXES = ['.npm-global', '.local/aarch64/npm', '.local/x86_64/npm']
REMOVED = {'ccmanager': 'ccmanager', 'gemini': '@google/gemini-cli', 'ruler': '@intellectronica/ruler'}
KEPT = {'codex': '@openai/codex', 'pi': '@earendil-works/pi-coding-agent'}


class ChezmoiRemove(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'
        for prefix in PREFIXES:
            for name, package in {**REMOVED, **KEPT}.items():
                pkg = self.home / prefix / 'lib/node_modules' / package
                pkg.mkdir(parents=True)
                (pkg / 'package.json').write_text('{}')
                link = self.home / prefix / 'bin' / name
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(os.path.relpath(pkg / 'cli.js', link.parent))
        config = self.root / 'chezmoi.json'
        config.write_text('{}')
        subprocess.run(
            ['chezmoi', '--config', str(config), '--config-format', 'json', '--source', str(ROOT),
             '--destination', str(self.home), '--cache', str(self.root / 'cache'),
             '--persistent-state', str(self.root / 'state.boltdb'), 'apply', '--force', '--include=remove'],
            check=True, capture_output=True, text=True, timeout=60)

    def test_removes_retired_npm_tools(self):
        for prefix in PREFIXES:
            for name, package in REMOVED.items():
                with self.subTest(prefix=prefix, package=package):
                    self.assertFalse((self.home / prefix / 'bin' / name).is_symlink())
                    self.assertFalse((self.home / prefix / 'lib/node_modules' / package).exists())

    def test_keeps_current_npm_tools(self):
        for prefix in PREFIXES:
            for name, package in KEPT.items():
                with self.subTest(prefix=prefix, package=package):
                    self.assertTrue((self.home / prefix / 'bin' / name).is_symlink())
                    self.assertTrue((self.home / prefix / 'lib/node_modules' / package / 'package.json').exists())


if __name__ == '__main__':
    unittest.main()

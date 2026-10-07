"""Source dot_profile.tmpl in a POSIX shell and check the PATH it builds for user tool prefixes."""
from pathlib import Path
import platform
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASE_PATH = '/usr/bin:/bin'


class ProfilePath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        # The profile sources ~/.cargo/env unconditionally, and dash exits when `.` cannot open a file.
        (self.home / '.cargo').mkdir()
        (self.home / '.cargo/env').write_text('')

    def path_entries(self):
        result = subprocess.run(
            ['env', '-i', 'HOME=' + str(self.home), 'PATH=' + BASE_PATH, 'sh', '-c',
             '. "$1" && printf %s "$PATH"', 'sh', str(ROOT / 'dot_profile.tmpl')],
            check=True, capture_output=True, text=True, timeout=30)
        return result.stdout.split(':')

    def test_adds_pixi_before_npm_global(self):
        for rel in ('.pixi/bin', '.npm-global/bin'):
            (self.home / rel).mkdir(parents=True)
        entries = self.path_entries()
        pixi, npm = str(self.home / '.pixi/bin'), str(self.home / '.npm-global/bin')
        self.assertIn(pixi, entries)
        self.assertIn(npm, entries)
        self.assertLess(entries.index(pixi), entries.index(npm))
        self.assertLess(entries.index(npm), entries.index('/usr/bin'))

    def test_skips_missing_prefixes(self):
        entries = self.path_entries()
        self.assertNotIn(str(self.home / '.pixi/bin'), entries)
        self.assertNotIn(str(self.home / '.npm-global/bin'), entries)

    def test_arch_layout_wins(self):
        arch = self.home / '.local' / platform.machine()
        for rel in ('.pixi/bin', '.npm-global/bin'):
            (self.home / rel).mkdir(parents=True)
        (arch / 'npm/bin').mkdir(parents=True)
        entries = self.path_entries()
        self.assertLess(entries.index(str(arch / 'npm/bin')), entries.index(str(self.home / '.pixi/bin')))


if __name__ == '__main__':
    unittest.main()

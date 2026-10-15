"""Render ~/.config/bb/server-url and check that fish and POSIX logins export it as BB_SERVER_URL."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / 'dot_config/bb/server-url.tmpl'
FISH_CONF = ROOT / 'dot_config/fish/conf.d/bb-server-url.fish'
PROFILE = ROOT / 'dot_profile.tmpl'
URL = 'https://bb.tail1.ts.net'


class Render(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bin = Path(self.tmp.name) / 'bin'
        self.bin.mkdir()
        # Only chezmoi and a fake tailscale on PATH, so the real tailscale on this machine is not used.
        os.symlink(shutil.which('chezmoi'), self.bin / 'chezmoi')

    def fake_tailscale(self, body):
        script = self.bin / 'tailscale'
        script.write_text('#!/bin/sh\n' + body)
        script.chmod(0o755)

    def render(self):
        result = subprocess.run(
            ['chezmoi', 'execute-template', '--no-tty', '--config', str(Path(self.tmp.name) / 'none.toml')],
            input=TEMPLATE.read_text(), env={'PATH': f'{self.bin}:/usr/bin:/bin', 'HOME': self.tmp.name},
            check=True, capture_output=True, text=True, timeout=30)
        return result.stdout

    def test_uses_the_tailnet_suffix(self):
        self.fake_tailscale('printf \'{"MagicDNSSuffix": "tail1.ts.net", "Self": {}}\'\n')
        self.assertEqual(self.render(), URL + '\n')

    def test_empty_without_tailscale(self):
        self.assertEqual(self.render().strip(), '')

    def test_empty_when_tailscale_fails(self):
        self.fake_tailscale('exit 1\n')
        self.assertEqual(self.render().strip(), '')

    def test_empty_without_a_suffix(self):
        self.fake_tailscale('printf \'{"Self": {}}\'\n')
        self.assertEqual(self.render().strip(), '')


class Shells(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        # The profile sources ~/.cargo/env unconditionally, and dash exits when `.` cannot open a file.
        (self.home / '.cargo').mkdir()
        (self.home / '.cargo/env').write_text('')

    def write_url(self):
        (self.home / '.config/bb').mkdir(parents=True)
        (self.home / '.config/bb/server-url').write_text(URL + '\n')

    def run_shell(self, argv, preset=None):
        env = {'HOME': str(self.home), 'PATH': '/usr/bin:/bin'}
        if preset is not None:
            env['BB_SERVER_URL'] = preset
        return subprocess.run(['env', '-i', *[f'{k}={v}' for k, v in env.items()], *argv],
                              check=True, capture_output=True, text=True, timeout=30).stdout

    def profile(self, preset=None):
        return self.run_shell(['sh', '-c', '. "$1" && printf %s "${BB_SERVER_URL-unset}"', 'sh', str(PROFILE)], preset)

    def fish(self, preset=None):
        fish = shutil.which('fish')
        if fish is None:
            self.skipTest('fish is not installed')
        return self.run_shell([fish, '--no-config', '-c',
                               f'source {FISH_CONF}; set -q BB_SERVER_URL; and printf %s $BB_SERVER_URL; or printf unset'],
                              preset)

    def test_exports_the_rendered_url(self):
        self.write_url()
        self.assertEqual(self.profile(), URL)
        self.assertEqual(self.fish(), URL)

    def test_keeps_a_url_already_set(self):
        self.write_url()
        self.assertEqual(self.profile('http://127.0.0.1:41000'), 'http://127.0.0.1:41000')
        self.assertEqual(self.fish('http://127.0.0.1:41000'), 'http://127.0.0.1:41000')

    def test_unset_without_the_file(self):
        self.assertEqual(self.profile(), 'unset')
        self.assertEqual(self.fish(), 'unset')


if __name__ == '__main__':
    unittest.main()

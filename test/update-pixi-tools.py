"""Run the rendered weekly pixi update script with a stub pixi and a scratch home."""
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / 'run_after_update-pixi-tools.sh.tmpl'
ARCH = os.uname().machine


class WeeklyUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.log = self.root / 'log'

    def pixi(self, folder, exit_code=0):
        path = self.home / folder / 'pixi'
        path.parent.mkdir(parents=True)
        path.write_text('#!/bin/sh\necho "$0 $*" >> "{}"\nexit {}\n'.format(self.log, exit_code))
        path.chmod(0o755)
        return str(path)

    def stamp(self):
        return self.home / '.local/state/dotfiles' / 'pixi-update-{}'.format(ARCH)

    def run_script(self, machine_class='headless-sudo'):
        text = TEMPLATE.read_text().replace('{{ if eq .chezmoi.os "linux" -}}', '').replace('{{ end -}}', '')
        script = self.root / 'update.sh'
        script.write_text(text.replace('{{ .machine_class }}', machine_class))
        result = subprocess.run(['bash', str(script)], env={'HOME': str(self.home), 'PATH': '/usr/bin:/bin'},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_first_run_updates_and_writes_the_stamp(self):
        pixi = self.pixi('.pixi/bin')
        self.assertEqual(self.run_script(), ['{} global update'.format(pixi)])
        self.assertTrue(self.stamp().exists())

    def test_fresh_stamp_skips_the_update(self):
        self.pixi('.pixi/bin')
        self.run_script()
        self.assertEqual(len(self.run_script()), 1)

    def test_stamp_older_than_a_week_updates_again(self):
        self.pixi('.pixi/bin')
        self.run_script()
        old = time.time() - 8 * 24 * 3600
        os.utime(str(self.stamp()), (old, old))
        self.assertEqual(len(self.run_script()), 2)

    def test_failed_update_warns_and_tries_again_next_apply(self):
        self.pixi('.pixi/bin', exit_code=1)
        self.run_script()
        self.assertFalse(self.stamp().exists())
        self.assertEqual(len(self.run_script()), 2)

    def test_no_pixi_does_nothing(self):
        self.assertEqual(self.run_script(), [])

    def test_nosudo_uses_the_arch_pixi(self):
        # On a shared $HOME, ~/.pixi/bin can hold another architecture's pixi.
        self.pixi('.pixi/bin')
        arch_pixi = self.pixi('.local/{}/pixi/bin'.format(ARCH))
        self.assertEqual(self.run_script('headless-nosudo'), ['{} global update'.format(arch_pixi)])


if __name__ == '__main__':
    unittest.main()

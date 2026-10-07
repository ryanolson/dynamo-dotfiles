"""Check which zellij dynamo-remote-session runs, with a scratch home and a non-login PATH."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'dot_local/bin/executable_dynamo-remote-session'


class RemoteSessionZellij(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def zellij(self, folder):
        path = self.home / folder / 'zellij'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#!/bin/sh\nexit 0\n')
        path.chmod(0o755)
        return str(path)

    def check(self):
        # A non-login SSH command: no profile, so PATH is the system default.
        env = {'HOME': str(self.home), 'PATH': '/usr/bin:/bin'}
        return subprocess.run(['bash', str(SCRIPT), 'check'], env=env, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=30)

    def test_arch_root_wins_over_a_shared_pixi_folder(self):
        # headless-nosudo shares $HOME across architectures. ~/.pixi/bin can hold another arch's binaries.
        self.zellij('.pixi/bin')
        right = self.zellij('.local/{}/pixi/bin'.format(os.uname().machine))
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), right)

    def test_pixi_wins_over_cargo(self):
        # Interactive fish puts ~/.pixi/bin before ~/.cargo/bin; the session must run the same zellij.
        self.zellij('.cargo/bin')
        right = self.zellij('.pixi/bin')
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), right)

    def test_missing_zellij_fails(self):
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn('zellij', result.stderr)


if __name__ == '__main__':
    unittest.main()

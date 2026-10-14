import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'join.sh'
REF = 'a' * 40
TOKEN = 'eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.c2ln'
FP = 'ab' * 32

CURL_STUB = r'''#!/usr/bin/env python3
import os, shutil, sys
args = sys.argv[1:]
out = args[args.index('-o') + 1]
url = [a for a in args if a.startswith('https://')][0]
with open(os.environ['CURL_LOG'], 'a') as f:
    f.write(url + '\n')
prefix = 'https://raw.githubusercontent.com/owner/dots/'
if not url.startswith(prefix):
    sys.exit(22)
path = os.path.join(os.environ['TREE'], url[len(prefix):])
if not os.path.exists(path):
    sys.exit(22)
shutil.copy(path, out)
'''

RECORDER = r'''#!/bin/sh
printf '%s\n' "$0 $*" >> "$JOIN_LOG"
if [ "$1" = enroll ] || [ "$1" = ephemeral ]; then
    printf 'stdin=%s\n' "$(cat)" >> "$JOIN_LOG"
fi
'''


class Join(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        bin_dir = self.home / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'curl').write_text(CURL_STUB)
        (bin_dir / 'curl').chmod(0o700)
        self.tree = self.home / 'tree'
        fleet = self.tree / REF / 'dot_local/bin/executable_fleet'
        fleet.parent.mkdir(parents=True)
        fleet.write_text(RECORDER)
        (self.tree / REF / 'bootstrap.sh').write_text(
            '#!/bin/sh\nprintf "bootstrap\\n" >> "$JOIN_LOG"\nmkdir -p "$HOME/.local/bin"\n'
            f'cp "{fleet}" "$HOME/.local/bin/fleet"\nchmod 755 "$HOME/.local/bin/fleet"\n')
        self.log = self.home / 'join.log'
        self.curl_log = self.home / 'curl.log'
        self.env = dict(os.environ, HOME=str(self.home), PATH=f'{bin_dir}:{os.environ["PATH"]}', TREE=str(self.tree),
                        JOIN_LOG=str(self.log), CURL_LOG=str(self.curl_log), TMPDIR=str(self.home))

    def join(self, *extra, ref=REF, token=TOKEN):
        return subprocess.run(['bash', str(SCRIPT), '--repo', 'owner/dots', '--ref', ref, '--name', 'box',
                               '--ca-url', 'https://ca.example', '--fingerprint', FP, '--token', token, *extra],
                              capture_output=True, text=True, env=self.env)

    def lines(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def urls(self):
        return self.curl_log.read_text().splitlines() if self.curl_log.exists() else []

    def test_installs_fleet_from_the_pinned_commit_and_enrolls(self):
        result = self.join('--no-trust')
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = self.home / '.local/bin/fleet'
        self.assertEqual(stat.S_IMODE(installed.stat().st_mode), 0o755)
        self.assertEqual(self.urls(), [f'https://raw.githubusercontent.com/owner/dots/{REF}/dot_local/bin/executable_fleet'])
        lines = self.lines()
        self.assertEqual(lines[0], f'{installed} enroll box --ca-url https://ca.example --fingerprint {FP} --no-trust')
        self.assertEqual(lines[1], f'stdin={TOKEN}')
        self.assertEqual(lines[2], f'{installed} timer install')
        self.assertNotIn(TOKEN, lines[0])

    def test_uses_an_installed_fleet(self):
        installed = self.home / '.local/bin/fleet'
        installed.parent.mkdir(parents=True)
        installed.write_text(RECORDER)
        installed.chmod(0o755)
        before = installed.read_text()
        self.assertEqual(self.join().returncode, 0)
        self.assertEqual(self.urls(), [])
        self.assertEqual(installed.read_text(), before)
        self.assertTrue(self.lines()[0].startswith(f'{installed} enroll box'))

    def test_dotfiles_runs_bootstrap_first(self):
        result = self.join('--dotfiles')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.urls(), [f'https://raw.githubusercontent.com/owner/dots/{REF}/bootstrap.sh'])
        lines = self.lines()
        self.assertEqual(lines[0], 'bootstrap')
        self.assertTrue(lines[1].startswith(f'{self.home}/.local/bin/fleet enroll box'))

    def test_ephemeral_runs_a_pinned_copy_in_the_foreground(self):
        installed = self.home / '.local/bin/fleet'
        result = self.join('--ephemeral')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(installed.exists())
        lines = self.lines()
        program, command = lines[0].split(' ', 2)[:2]
        self.assertEqual(command, 'ephemeral')
        self.assertNotEqual(program, str(installed))
        self.assertFalse(Path(program).exists())
        self.assertEqual(lines[1], f'stdin={TOKEN}')
        self.assertEqual(len(lines), 2)

    def test_invalid_inputs_are_rejected_before_any_download(self):
        for label, kwargs in {'ref': {'ref': 'main'}, 'token': {'token': 'not-a-token'}}.items():
            with self.subTest(label):
                self.assertNotEqual(self.join(**kwargs).returncode, 0)
        self.assertEqual(self.urls(), [])
        self.assertEqual(self.lines(), [])


if __name__ == '__main__':
    unittest.main()

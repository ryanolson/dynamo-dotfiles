"""Exercise installer routing and failure handling without network access."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class Installers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.bin = self.home / 'bin'
        self.bin.mkdir()
        for name in ('bash', 'mktemp', 'rm', 'mkdir', 'mv'):
            (self.bin / name).symlink_to('/usr/bin/' + name)
        self.env = dict(os.environ, HOME=str(self.home), PATH=str(self.bin))
        self.env.pop('NPM_CONFIG_PREFIX', None)

    def stub(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/bash\nset -eu\n' + body + '\n')
        path.chmod(0o755)

    def run_helper(self, name):
        return subprocess.run(['/bin/bash', str(ROOT / 'dot_local/bin' / ('executable_iou_' + name))], env=self.env, capture_output=True, text=True)

    def test_npm_prefix_and_errors(self):
        self.stub('npm', 'printf "%s|%s\\n" "$NPM_CONFIG_PREFIX" "$*"; exit "${FAIL:-0}"')
        for name, package in [('codex', '@openai/codex'), ('pi', '@earendil-works/pi-coding-agent')]:
            with self.subTest(name=name):
                self.env.pop('NPM_CONFIG_PREFIX', None)
                result = self.run_helper(name)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(str(self.home / '.npm-global') + '|install -g', result.stdout)
                self.assertIn(package + '@latest', result.stdout)
                self.env['NPM_CONFIG_PREFIX'] = '/custom/npm'
                self.assertIn('/custom/npm|', self.run_helper(name).stdout)
                self.env['FAIL'] = '17'
                self.assertEqual(self.run_helper(name).returncode, 17)
                del self.env['FAIL']

    def test_native_install_download_failure_and_update(self):
        for name, command in [('claude', 'claude'), ('cursor', 'cursor-agent')]:
            with self.subTest(name=name):
                self.stub('curl', 'printf \'echo installed\\n\' > "$4"; exit "${FAIL:-0}"')
                result = self.run_helper(name)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), 'installed')
                self.env['FAIL'] = '22'
                result = self.run_helper(name)
                self.assertEqual(result.returncode, 22)
                self.assertEqual(result.stdout, '')
                del self.env['FAIL']
                self.stub(command, 'echo "$*"; exit 13')
                result = self.run_helper(name)
                self.assertEqual(result.stdout.strip(), 'update')
                self.assertEqual(result.returncode, 13)
                (self.bin / command).unlink()

    def test_antigravity_preserves_binary_on_download_failure(self):
        target = self.home / '.local/bin/agy'
        target.parent.mkdir(parents=True)
        target.write_text('existing binary')
        self.stub('curl', 'exit 22')
        self.assertEqual(self.run_helper('antigravity').returncode, 22)
        self.assertEqual(target.read_text(), 'existing binary')
        self.assertEqual(list(target.parent.glob('.iou-antigravity.*')), [])

    def test_antigravity_install_and_upgrade(self):
        self.stub('curl', '''cat_script='#!/bin/bash
set -eu
test "$1" = --dir
printf "#!/bin/bash\\necho updated\\n" > "$2/agy"
/usr/bin/chmod +x "$2/agy"
'
printf '%s' "$cat_script" > "$4"''')
        target = self.home / '.local/bin/agy'
        for _ in range(2):
            result = self.run_helper('antigravity')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'updated')
            self.assertTrue(os.access(target, os.X_OK))
            self.assertEqual(list(target.parent.glob('.iou-antigravity.*')), [])


if __name__ == '__main__':
    unittest.main()

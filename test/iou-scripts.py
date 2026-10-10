"""Exercise installer routing and failure handling without network access."""
import os
import shutil
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
        for name in ('bash', 'mktemp', 'rm', 'mkdir', 'mv', 'chmod', 'install', 'shasum'):
            (self.bin / name).symlink_to(shutil.which(name))
        self.env = dict(os.environ, HOME=str(self.home), PATH=str(self.bin))
        self.env.pop('NPM_CONFIG_PREFIX', None)

    def stub(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/bash\nset -eu\n' + body + '\n')
        path.chmod(0o755)

    def run_helper(self, name):
        return subprocess.run(['/bin/bash', str(ROOT / 'dot_local/bin' / ('executable_iou_' + name))], env=self.env, capture_output=True, text=True)

    def test_q_builds_all_binaries_before_installing(self):
        repo = self.home / 'repos/q'
        repo.mkdir(parents=True)
        (repo / 'Cargo.toml').write_text('[package]')
        (repo / 'Cargo.lock').write_text('')
        self.stub('iou_relay', 'echo relay >> "$HOME/relay-calls"; exit "${RELAY_FAIL:-0}"')
        for name in ('sed',):
            (self.bin / name).symlink_to(shutil.which(name))
        self.stub('rustc', 'echo "host: test-platform"')
        self.stub('cargo', 'test "$PWD" = "$HOME/repos/q"\nprintf \'%s\\n\' "$*" > "$HOME/build-args"\nif [ "${FAIL:-0}" != 0 ]; then exit "$FAIL"; fi\nmkdir -p target/iou-q/test-platform/release\nfor name in q q-proxy q-launch; do\n  printf \'#!/bin/bash\\necho native\\n\' > "target/iou-q/test-platform/release/$name"\ndone')
        target = self.home / '.local/bin/q'
        target.parent.mkdir(parents=True)
        target.write_text('previous')
        self.env['FAIL'] = '17'
        self.assertEqual(self.run_helper('q').returncode, 17)
        self.assertEqual(target.read_text(), 'previous')
        del self.env['FAIL']
        self.env['RELAY_FAIL'] = '19'
        self.assertEqual(self.run_helper('q').returncode, 19)
        self.assertEqual(target.read_text(), 'previous')
        del self.env['RELAY_FAIL']
        result = self.run_helper('q')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / 'relay-calls').exists())
        self.assertIn('--release --locked --bins', (self.home / 'build-args').read_text())
        self.assertIn('--target test-platform', (self.home / 'build-args').read_text())
        for name in ('q', 'q-proxy', 'q-launch'):
            binary = target.parent / name
            self.assertIn('native', binary.read_text())
            self.assertTrue(os.access(binary, os.X_OK))

    def test_relay_latest_platform_checksum_and_failure_preservation(self):
        self.stub('uname', 'if [ "$1" = -s ]; then echo "${TEST_OS:-Darwin}"; else echo "${TEST_ARCH:-arm64}"; fi')
        self.stub('gh', r"""if [ "$1" = api ]; then
  test "$2" = repos/NVIDIA/NeMo-Relay/releases/latest
  echo 0.9.4
elif [ "$1" = release ]; then
  test "$2" = download
  test "$3" = 0.9.4
  shift 3
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pattern) asset="$2"; shift 2 ;;
      --dir) directory="$2"; shift 2 ;;
      --repo) test "$2" = NVIDIA/NeMo-Relay; shift 2 ;;
      *) exit 90 ;;
    esac
  done
  case "$asset" in
    *.sha256)
      binary="${asset%.sha256}"
      (cd "$directory"; shasum -a 256 "$binary") > "$directory/$asset"
      if [ "${CORRUPT:-0}" = 1 ]; then echo tampered >> "$directory/$binary"; fi ;;
    *) printf '#!/bin/bash\necho "nemo-relay 0.9.4"\n' > "$directory/$asset"
       echo "$asset" > "$HOME/selected-asset" ;;
  esac
else exit 91
fi""")
        target = self.home / '.local/bin/nemo-relay'
        for system, arch, triple in [('Darwin', 'arm64', 'aarch64-apple-darwin'), ('Linux', 'aarch64', 'aarch64-unknown-linux-musl'), ('Linux', 'x86_64', 'x86_64-unknown-linux-musl')]:
            self.env.update(TEST_OS=system, TEST_ARCH=arch)
            result = self.run_helper('relay')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((self.home / 'selected-asset').read_text().strip(), f'nemo-relay-cli-{triple}-0.9.4')
            self.assertTrue(os.access(target, os.X_OK))
        before = target.read_bytes()
        self.env['CORRUPT'] = '1'
        self.assertNotEqual(self.run_helper('relay').returncode, 0)
        self.assertEqual(target.read_bytes(), before)
        del self.env['CORRUPT']
        self.stub('gh', 'exit 22')
        self.assertEqual(self.run_helper('relay').returncode, 22)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(list(target.parent.glob('.iou-relay.*')), [])

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

    def test_devin_installs_then_updates(self):
        self.stub('curl', 'printf \'printf "#!/bin/bash\\\\necho \\\\"\\\\$*\\\\"\\\\n" > "$HOME/.local/bin/devin"; chmod 755 "$HOME/.local/bin/devin"\\n\' > "$4"; exit "${FAIL:-0}"')
        (self.home / '.local/bin').mkdir(parents=True)
        result = self.run_helper('devin')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'update')
        self.env['FAIL'] = '22'
        (self.home / '.local/bin/devin').unlink()
        result = self.run_helper('devin')
        self.assertEqual(result.returncode, 22)
        self.assertEqual(result.stdout, '')

    def test_devin_updates_in_place_when_installed(self):
        self.stub('devin', 'echo "$*"; exit 13')
        result = self.run_helper('devin')
        self.assertEqual(result.stdout.strip(), 'update')
        self.assertEqual(result.returncode, 13)

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
chmod +x "$2/agy"
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

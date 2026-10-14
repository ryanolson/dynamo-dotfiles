import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'dot_local/bin/executable_bb-server-service'

NPM = r'''#!/bin/sh
echo "npm $*" >> "$LOG"
case "$1" in
    view) echo "0.46.0" ;;
    install)
        cp .npmrc "$HOME/npmrc-seen" 2>/dev/null
        mkdir -p "$HOME/.local/share/bb-server/node_modules/.bin"
        printf '#!/bin/sh\necho "bb-app $*" >> "$LOG"\n' > "$HOME/.local/share/bb-server/node_modules/.bin/bb-app"
        chmod 755 "$HOME/.local/share/bb-server/node_modules/.bin/bb-app" ;;
esac
'''
RECORD = '#!/bin/sh\necho "$(basename "$0") $*" >> "$LOG"\n'


class BbServerService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.bin = self.home / 'bin'
        self.bin.mkdir()
        self.log = self.home / 'log'
        self.stub('npm', NPM)
        self.stub('systemctl', RECORD)
        self.stub('loginctl', RECORD)
        self.node('24.3.0')
        self.env = dict(os.environ, HOME=str(self.home), PATH=f'{self.bin}:/usr/bin:/bin', LOG=str(self.log),
                        XDG_CONFIG_HOME=str(self.home / '.config'), USER='t')

    def stub(self, name, body):
        (self.bin / name).write_text(body)
        (self.bin / name).chmod(0o755)

    def node(self, version):
        self.stub('node', f'#!/bin/sh\necho {version}\n')

    def run_script(self, *args):
        return subprocess.run(['bash', str(SCRIPT), *args], capture_output=True, text=True, env=self.env)

    def test_install_pins_the_exact_version_and_starts_the_unit(self):
        result = self.run_script('install')
        self.assertEqual(result.returncode, 0, result.stderr)
        unit = (self.home / '.config/systemd/user/bb-server.service').read_text()
        self.assertIn(f'ExecStart={self.home}/.local/share/bb-server/node_modules/.bin/bb-app start '
                      f'--data-dir {self.home}/.bb --server-port 38886\n', unit)
        log = self.log.read_text()
        self.assertIn('npm view bb-app@latest version', log)
        self.assertIn('bb-app@0.46.0', log)
        self.assertIn('systemctl --user enable --now bb-server.service', log)
        self.assertIn('allow-scripts=better-sqlite3,node-pty,@parcel/watcher',
                      (self.home / 'npmrc-seen').read_text())

    def test_install_refuses_an_unsupported_node(self):
        self.node('22.10.0')
        result = self.run_script('install')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('22.19, 24, or 26', result.stderr)
        self.assertFalse(self.log.exists() and 'npm install' in self.log.read_text())

    def test_remove_deletes_only_the_unit(self):
        self.assertEqual(self.run_script('install').returncode, 0)
        result = self.run_script('remove')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / '.config/systemd/user/bb-server.service').exists())
        self.assertTrue((self.home / '.local/share/bb-server/node_modules/.bin/bb-app').exists())


if __name__ == '__main__':
    unittest.main()

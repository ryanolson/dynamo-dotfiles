"""Run iou_antigravity with a stub curl and a stub agy.

The stub curl serves a fake upstream installer. That installer writes a stub agy. The stub agy
records its arguments and, like the real `agy install`, appends a PATH line to ~/.profile unless
it gets --skip-path. Those profile files belong to chezmoi, so the real home must stay untouched.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
IOU = ROOT / 'dot_local/bin/executable_iou_antigravity'

CURL = '''#!/bin/bash
out=
while [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done
cat > "$out" <<'INSTALLER'
#!/bin/bash
dir=$2
cat > "$dir/agy" <<'AGY'
#!/bin/bash
echo "$@" >> "$AGY_LOG"
for a in "$@"; do [ "$a" = --skip-path ] && skip=1; done
[ -n "$skip" ] || echo 'export PATH=changed' >> "$HOME/.profile"
AGY
chmod +x "$dir/agy"
INSTALLER
'''


class IouAntigravity(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / 'home'
        self.home.mkdir()
        bin_dir = Path(tmp.name) / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'curl').write_text(CURL)
        (bin_dir / 'curl').chmod(0o755)
        self.log = Path(tmp.name) / 'agy.log'
        self.env = dict(os.environ, HOME=str(self.home), AGY_LOG=str(self.log),
                        PATH=f'{bin_dir}:{os.environ["PATH"]}')

    def test_installs_agy_without_touching_the_shell_profiles(self):
        result = subprocess.run(['bash', str(IOU)], capture_output=True, text=True, timeout=60, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / '.local/bin/agy').exists())
        self.assertEqual(self.log.read_text().split(), ['install', '--dir', str(self.home / '.local/bin'),
                                                        '--skip-path', '--skip-aliases'])
        self.assertFalse((self.home / '.profile').exists())


if __name__ == '__main__':
    unittest.main()

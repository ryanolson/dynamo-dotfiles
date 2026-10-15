"""Run the ~/.npmrc modify script on sample files and render the ignore rules per class."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'modify_dot_npmrc'


class Npmrc(unittest.TestCase):
    def modify(self, current):
        return subprocess.run(['bash', str(SCRIPT)], input=current, capture_output=True, text=True, check=True,
                              env=dict(os.environ, HOME='/home/u')).stdout

    def test_new_file_gets_the_prefix(self):
        self.assertEqual(self.modify(''), 'prefix=/home/u/.npm-global\n')

    def test_other_lines_stay_and_the_old_prefix_goes(self):
        current = '//registry.example/:_authToken=X\nprefix = /opt/other\nfund=false'
        self.assertEqual(self.modify(current),
                         '//registry.example/:_authToken=X\nfund=false\nprefix=/home/u/.npm-global\n')

    def test_a_second_run_changes_nothing(self):
        once = self.modify('fund=false\n')
        self.assertEqual(self.modify(once), once)

    def ignored(self, machine_class):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'chezmoi.yaml'
            config.write_text(f'data:\n  machine_class: "{machine_class}"\n')
            out = subprocess.run(['chezmoi', 'execute-template', '--config', str(config), '--source', str(ROOT)],
                                 input=(ROOT / '.chezmoiignore').read_text(), capture_output=True, text=True,
                                 check=True).stdout
        return '.npmrc' in out.split()

    def test_only_nosudo_skips_the_file(self):
        # A no-sudo home can be shared across architectures, and the prefix there is per architecture.
        self.assertTrue(self.ignored('nosudo'))
        self.assertFalse(self.ignored('sudo'))


if __name__ == '__main__':
    unittest.main()

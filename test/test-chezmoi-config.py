from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASE = ('data:\n  name: "N"\n  email: "e@example.com"\n  machine_class: "headless-sudo"\n'
        '  machine_seed: "blank"\n  cursor_ssh_host: "h"\n')


class ChezmoiConfig(unittest.TestCase):
    def init(self, existing):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'chezmoi.yaml'
            config.write_text(existing)
            return subprocess.run(['chezmoi', 'execute-template', '--init', '--no-tty', '--config', str(config),
                                   '--source', str(ROOT)], input=(ROOT / '.chezmoi.yaml.tmpl').read_text(),
                                  capture_output=True, text=True, check=True).stdout

    def test_init_keeps_an_existing_vault(self):
        # setup-secrets reads this vault; init must not reset a machine's override.
        self.assertIn('    vault: "Corp"\n', self.init(BASE + '  onepassword:\n    vault: "Corp"\n'))

    def test_init_defaults_the_vault(self):
        self.assertIn('    vault: "Development"\n', self.init(BASE))


if __name__ == '__main__':
    unittest.main()

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLASS_PROMPT = 'Machine class'
OP_PROMPT = 'Does this machine have the 1Password CLI and administer the fleet'
SIGNING_PROMPT = 'SSH signing public key (from 1Password item public key field; empty to skip signing)'
PEOPLE = 'data:\n  name: "N"\n  email: "e@example.com"\n  machine_seed: "blank"\n  cursor_ssh_host: "h"\n'
BASE = PEOPLE + '  machine_class: "sudo"\n  onepassword:\n    enabled: false\n'


class ChezmoiConfig(unittest.TestCase):
    def init(self, existing, *prompts, env=None, check=True):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'chezmoi.yaml'
            config.write_text(existing)
            return subprocess.run(['chezmoi', 'execute-template', '--init', '--no-tty', '--config', str(config),
                                   '--source', str(ROOT), *prompts], input=(ROOT / '.chezmoi.yaml.tmpl').read_text(),
                                  capture_output=True, text=True, check=check,
                                  env={**os.environ, 'DOTFILES_MACHINE_CLASS': '', **(env or {})})

    def test_init_keeps_an_existing_vault(self):
        # setup-secrets reads this vault; init must not reset a machine's override.
        self.assertIn('    vault: "Corp"\n', self.init(BASE + '    vault: "Corp"\n').stdout)

    def test_init_defaults_the_vault(self):
        self.assertIn('    vault: "Development"\n', self.init(BASE).stdout)

    def test_a_value_that_is_not_a_class_is_asked_again(self):
        out = self.init(PEOPLE + '  machine_class: "primary"\n  onepassword:\n    enabled: false\n',
                        '--promptChoice', f'{CLASS_PROMPT}=nosudo').stdout
        self.assertIn('  machine_class: "nosudo"\n', out)

    def test_an_existing_class_is_kept(self):
        for value in ('sudo', 'nosudo'):
            out = self.init(PEOPLE + f'  machine_class: "{value}"\n  onepassword:\n    enabled: false\n',
                            '--promptChoice', f'{CLASS_PROMPT}=sudo' if value == 'nosudo' else f'{CLASS_PROMPT}=nosudo')
            self.assertIn(f'  machine_class: "{value}"\n', out.stdout)

    def test_bootstrap_forces_nosudo(self):
        out = self.init(BASE, env={'DOTFILES_MACHINE_CLASS': 'nosudo'}).stdout
        self.assertIn('  machine_class: "nosudo"\n', out)

    def test_a_new_machine_chooses_sudo_or_nosudo(self):
        no_class = PEOPLE + '  onepassword:\n    enabled: false\n'
        self.assertIn('  machine_class: "nosudo"\n',
                      self.init(no_class, '--promptChoice', f'{CLASS_PROMPT}=nosudo').stdout)
        refused = self.init(no_class, '--promptChoice', f'{CLASS_PROMPT}=primary', check=False)
        self.assertNotEqual(refused.returncode, 0)

    def test_init_keeps_the_1password_setting_on_any_class(self):
        # The class no longer decides 1Password: a sudo machine keeps its own setting.
        out = self.init(PEOPLE + '  machine_class: "sudo"\n  onepassword:\n    enabled: true\n'
                        '    ssh_agent: true\n    signing_public_key: "ssh-ed25519 AAAAKEY"\n').stdout
        self.assertIn('    enabled: true\n', out)
        self.assertIn('    ssh_agent: true\n', out)
        self.assertIn('    signing_public_key: "ssh-ed25519 AAAAKEY"\n', out)
        out = self.init(PEOPLE + '  machine_class: "nosudo"\n  onepassword:\n    enabled: false\n').stdout
        self.assertIn('    enabled: false\n', out)

    def test_a_new_machine_answers_the_1password_question(self):
        out = self.init(PEOPLE + '  machine_class: "sudo"\n', '--promptBool', f'{OP_PROMPT}=true',
                        '--promptString', f'{SIGNING_PROMPT}=ssh-ed25519 AAAANEW').stdout
        self.assertIn('    enabled: true\n', out)
        self.assertIn('    ssh_agent: true\n', out)
        self.assertIn('    signing_public_key: "ssh-ed25519 AAAANEW"\n', out)
        out = self.init(PEOPLE + '  machine_class: "sudo"\n', '--promptBool', f'{OP_PROMPT}=false').stdout
        self.assertIn('    enabled: false\n', out)
        self.assertIn('    ssh_agent: false\n', out)
        self.assertIn('    signing_public_key: ""\n', out)


if __name__ == '__main__':
    unittest.main()

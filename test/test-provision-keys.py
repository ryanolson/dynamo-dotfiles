import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'dot_local/bin/executable_provision-keys'


class ProvisionKeys(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = dict(os.environ, HOME=str(self.home), SSH_AUTH_SOCK='',
                        GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=str(self.home / '.gitconfig'))
        self.key = self.home / 'fixture'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(self.key)], check=True)
        bin_dir = self.home / 'bin'
        bin_dir.mkdir()
        op = bin_dir / 'op'
        op.write_text('#!/bin/sh\n[ "$1" = read ] || exit 1\n[ "$2" = "op://Test/Key/private key?ssh-format=openssh" ] || exit 2\ncat "$HOME/fixture"\n')
        op.chmod(0o700)
        self.env['PATH'] = str(bin_dir) + ':' + self.env['PATH']
        self.git('config', '--global', 'user.name', 'Test')
        self.git('config', '--global', 'user.email', 'test@example.com')
        self.git('config', '--global', 'include.path', '~/.ssh/provisioned/gitconfig')

    def git(self, *args):
        return subprocess.run(['git', *args], env=self.env, cwd=self.home,
                              capture_output=True, text=True, check=True)

    def run_script(self, ref='op://Test/Key/private key'):
        return subprocess.run(['bash', str(SCRIPT), ref], env=self.env, capture_output=True, text=True)

    def test_provision_sign_and_repeat(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.home / '.ssh/provisioned'
        self.assertEqual((target / 'authentication').stat().st_mode & 0o777, 0o600)
        self.assertEqual(target.stat().st_mode & 0o777, 0o700)
        self.assertNotIn('PRIVATE KEY', result.stdout + result.stderr)
        self.git('init', '-q')
        self.git('commit', '--allow-empty', '-m', 'test')
        self.git('verify-commit', 'HEAD')
        before = (target / 'authentication').read_bytes()
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual((target / 'authentication').read_bytes(), before)
        status = subprocess.run(['bash', str(ROOT / 'dot_local/bin/executable_git-signing-status')],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertIn('local SSH signing key', status.stdout)

    def test_failure_leaves_no_installation(self):
        self.assertNotEqual(self.run_script('op://Test/Missing/private key').returncode, 0)
        self.assertFalse((self.home / '.ssh/provisioned').exists())
        self.assertEqual(list((self.home / '.ssh').glob('.provision-*')), [])

    def test_invalid_private_key_leaves_no_installation(self):
        self.key.write_text('not a private key\n')
        self.assertNotEqual(self.run_script().returncode, 0)
        self.assertFalse((self.home / '.ssh/provisioned').exists())

    def test_active_provisioning_is_not_disturbed(self):
        lock = self.home / '.ssh/.provision-lock'
        lock.mkdir(parents=True)
        self.assertNotEqual(self.run_script().returncode, 0)
        self.assertTrue(lock.exists())
        self.assertFalse((self.home / '.ssh/provisioned').exists())

    def test_symlink_target_is_not_followed(self):
        ssh = self.home / '.ssh'
        ssh.mkdir()
        destination = self.home / 'untouched'
        destination.mkdir()
        (ssh / 'provisioned').symlink_to(destination)
        self.assertNotEqual(self.run_script().returncode, 0)
        self.assertEqual(list(destination.iterdir()), [])

    def test_separate_signing_key(self):
        other = self.home / 'other'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(other)], check=True)
        (self.home / 'bin/op').write_text('#!/bin/sh\ncase "$2" in\n  *Other*) cat "$HOME/other" ;;\n  *) cat "$HOME/fixture" ;;\nesac\n')
        result = subprocess.run(['bash', str(SCRIPT), 'op://Test/Key/private key', 'op://Test/Other/private key'],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.home / '.ssh/provisioned'
        self.assertNotEqual((target / 'authentication.pub').read_text(), (target / 'signing.pub').read_text())
        self.git('init', '-q')
        self.git('commit', '--allow-empty', '-m', 'separate signing key')
        self.git('verify-commit', 'HEAD')

    def test_templates_preserve_local_configuration(self):
        config = self.home / 'chezmoi.yaml'
        config.write_text('data:\n  name: Test\n  email: test@example.com\n  onepassword:\n    enabled: true\n    ssh_agent: true\n    signing_public_key: ""\n')
        def render(path):
            return subprocess.run(['chezmoi', '--config', str(config), '--source', str(ROOT), 'execute-template'],
                                  input=(ROOT / path).read_text(), capture_output=True, text=True, check=True,
                                  env=self.env).stdout
        (self.home / '.gitconfig').write_text(render('dot_gitconfig.tmpl'))
        self.assertEqual(self.run_script().returncode, 0)
        self.git('init', '-q')
        self.git('commit', '--allow-empty', '-m', 'template test')
        self.git('verify-commit', 'HEAD')
        modifier = render('private_dot_ssh/modify_private_config.tmpl')
        ssh_config = 'Host *\n    IdentityAgent /missing-agent\n'
        for _ in range(2):
            ssh_config = subprocess.run(['bash', '-c', modifier], input=ssh_config, capture_output=True,
                                        text=True, check=True, env=self.env).stdout
        self.assertEqual(ssh_config.count('Include ~/.ssh/provisioned/config'), 1)
        # OpenSSH expands ~ from the account database rather than HOME.
        target = self.home / '.ssh/provisioned'
        (target / 'config').write_text((target / 'config').read_text().replace('~/', str(self.home) + '/'))
        ssh_file = self.home / 'ssh-config'
        ssh_file.write_text(ssh_config.replace('~/', str(self.home) + '/'))
        result = subprocess.run(['ssh', '-G', '-F', str(ssh_file), 'example.com'], capture_output=True,
                                text=True, check=True, env=self.env)
        self.assertIn('identityagent none\n', result.stdout)
        self.assertIn('identitiesonly yes\n', result.stdout)

    def test_different_key_is_not_overwritten(self):
        self.assertEqual(self.run_script().returncode, 0)
        target = self.home / '.ssh/provisioned/authentication'
        before = target.read_bytes()
        self.key.unlink()
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(self.key)], check=True)
        self.assertNotEqual(self.run_script().returncode, 0)
        self.assertEqual(target.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()

import os
from pathlib import Path
import shutil
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

    @staticmethod
    def key_material(path):
        return path.read_text().split()[:2]

    def run_script(self, ref='op://Test/Key/private key'):
        return subprocess.run(['bash', str(SCRIPT), '--ssh-key-ref', ref], env=self.env, capture_output=True, text=True)

    def render(self, path):
        config = self.home / 'chezmoi.yaml'
        config.write_text('data:\n  name: Test\n  email: test@example.com\n  onepassword:\n    enabled: true\n    ssh_agent: true\n    signing_public_key: ""\n')
        return subprocess.run(['chezmoi', '--config', str(config), '--source', str(ROOT), 'execute-template'],
                              input=(ROOT / path).read_text(), capture_output=True, text=True, check=True,
                              env=self.env).stdout

    def test_provision_sign_and_repeat(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.home / '.ssh/provisioned'
        self.assertEqual((target / 'key').stat().st_mode & 0o777, 0o600)
        self.assertEqual(target.stat().st_mode & 0o777, 0o700)
        self.assertNotIn('PRIVATE KEY', result.stdout + result.stderr)
        self.git('init', '-q')
        self.git('commit', '--allow-empty', '-m', 'test')
        self.git('verify-commit', 'HEAD')
        before = (target / 'key').read_bytes()
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual((target / 'key').read_bytes(), before)
        self.assertEqual(sorted(p.name for p in target.iterdir()),
                         ['allowed_signers', 'config', 'gitconfig', 'key', 'key.pub'])
        self.assertEqual(self.git('config', '--global', '--includes', 'user.signingkey').stdout.strip(),
                         '~/.ssh/provisioned/key')
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
        result = subprocess.run(['bash', str(SCRIPT), '--ssh-key-ref', 'op://Test/Key/private key', '--signing-key-ref', 'op://Test/Other/private key'],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.home / '.ssh/provisioned'
        self.assertNotEqual(self.key_material(target / 'key.pub'), self.key_material(target / 'other.pub'))
        self.assertIn('signingkey = ~/.ssh/provisioned/other\n', (target / 'gitconfig').read_text())
        self.git('init', '-q')
        self.git('commit', '--allow-empty', '-m', 'separate signing key')
        self.git('verify-commit', 'HEAD')

    def test_signing_key_shared_with_a_later_login_key(self):
        other = self.home / 'other'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(other)], check=True)
        (self.home / 'bin/op').write_text('#!/bin/sh\ncase "$2" in\n  *Other*) cat "$HOME/other" ;;\n  *) cat "$HOME/fixture" ;;\nesac\n')
        result = subprocess.run(['bash', str(SCRIPT), '--ssh-key-ref', 'op://Test/Key/private key',
                                 '--ssh-key-ref', 'op://Test/Other/private key',
                                 '--signing-key-ref', 'op://Test/Other/private key'],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.home / '.ssh/provisioned'
        self.assertEqual(sorted(p.name for p in target.iterdir()),
                         ['allowed_signers', 'config', 'gitconfig', 'key', 'key.pub', 'other', 'other.pub'])
        self.assertIn('signingkey = ~/.ssh/provisioned/other\n', (target / 'gitconfig').read_text())
        self.git('init', '-q')
        self.git('commit', '--allow-empty', '-m', 'shared signing key')
        self.git('verify-commit', 'HEAD')

    def test_unusable_file_names_are_rejected(self):
        (self.home / 'bin/op').write_text('#!/bin/sh\ncat "$HOME/fixture"\n')
        cases = {
            'same name': ['--ssh-key-ref', 'op://Test/My Key/private key', '--ssh-key-ref', 'op://Other/my-key/private key'],
            'same name as signing': ['--ssh-key-ref', 'op://Test/My Key/private key', '--signing-key-ref', 'op://Other/my_key/private key'],
            'repeated': ['--ssh-key-ref', 'op://Test/Key/private key', '--ssh-key-ref', 'op://Test/Key/private key'],
            'reserved': ['--ssh-key-ref', 'op://Test/Config/private key'],
            'reserved git': ['--ssh-key-ref', 'op://Test/gitconfig/private key'],
            'empty': ['--ssh-key-ref', 'op://Test/!!!/private key'],
        }
        for name, args in cases.items():
            with self.subTest(name):
                result = subprocess.run(['bash', str(SCRIPT), *args], env=self.env, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('file name', result.stderr)
                self.assertFalse((self.home / '.ssh/provisioned').exists())

    def test_signing_status_needs_the_named_key_file(self):
        self.assertEqual(self.run_script().returncode, 0)
        (self.home / '.ssh/provisioned/key').rename(self.home / 'moved')
        status = subprocess.run(['bash', str(ROOT / 'dot_local/bin/executable_git-signing-status')],
                                env=self.env, capture_output=True, text=True)
        self.assertNotEqual(status.returncode, 0)
        self.assertIn('degraded: local signing key', status.stdout)

    def test_two_login_keys_and_separate_signing_key(self):
        for name in ('brev', 'mbp-nv16', 'signer'):
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(self.home / name)], check=True)
        (self.home / 'bin/op').write_text('#!/bin/sh\ncase "$2" in\n  *brev*) cat "$HOME/brev" ;;\n  *mbp-nv16*) cat "$HOME/mbp-nv16" ;;\n  *"Git Signing Key"*) cat "$HOME/signer" ;;\n  *) exit 1 ;;\nesac\n')
        args = ['bash', str(SCRIPT),
                '--ssh-key-ref', 'op://Development/brev/private key',
                '--ssh-key-ref', 'op://Development/mbp-nv16/private key',
                '--signing-key-ref', 'op://Development/Git Signing Key/private key']
        result = subprocess.run(args, env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.home / '.ssh/provisioned'
        for installed, source in (('brev', 'brev'), ('mbp-nv16', 'mbp-nv16'), ('git-signing-key', 'signer')):
            self.assertEqual((target / installed).read_bytes(), (self.home / source).read_bytes())
            self.assertEqual(self.key_material(target / (installed + '.pub')), self.key_material(self.home / (source + '.pub')))
        # The public key comment carries the 1Password item name as written.
        self.assertEqual((target / 'git-signing-key.pub').read_text().split(None, 2)[2], 'Git Signing Key\n')
        self.assertEqual((target / 'mbp-nv16.pub').read_text().split(None, 2)[2], 'mbp-nv16\n')
        ssh_config = (target / 'config').read_text()
        self.assertEqual([line.strip() for line in ssh_config.splitlines() if 'IdentityFile' in line],
                         ['IdentityFile ~/.ssh/provisioned/brev', 'IdentityFile ~/.ssh/provisioned/mbp-nv16'])
        self.assertIn('signingkey = ~/.ssh/provisioned/git-signing-key\n', (target / 'gitconfig').read_text())
        self.git('init', '-q')
        self.git('commit', '--allow-empty', '-m', 'three keys')
        self.git('verify-commit', 'HEAD')
        self.assertEqual(subprocess.run(args, env=self.env, capture_output=True).returncode, 0)

    def test_templates_preserve_local_configuration(self):
        render = self.render
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
        target = self.home / '.ssh/provisioned/key'
        before = target.read_bytes()
        self.key.unlink()
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(self.key)], check=True)
        self.assertNotEqual(self.run_script().returncode, 0)
        self.assertEqual(target.read_bytes(), before)


    def test_signature_failure_is_reported(self):
        real = shutil.which('ssh-keygen')
        fake = self.home / 'bin/ssh-keygen'
        fake.write_text(f'#!/bin/sh\nfor arg in "$@"; do [ "$arg" = sign ] && exit 1; done\nexec {real} "$@"\n')
        fake.chmod(0o700)
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('sign', result.stderr)
        self.assertFalse((self.home / '.ssh/provisioned').exists())

    def test_bootstrap_rejects_repeated_signing_key(self):
        # No --ssh-key-ref: bootstrap stops at argument checks and installs nothing.
        result = subprocess.run(['bash', str(ROOT / 'bootstrap.sh'),
                                 '--signing-key-ref', 'op://Test/One/private key',
                                 '--signing-key-ref', 'op://Test/Two/private key'],
                                env=self.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('only once', result.stdout + result.stderr)

    def test_setup_secrets_upload_hint(self):
        for name in ('openv', 'dev-remote', 'git-signing-status', 'gh'):
            stub = self.home / 'bin' / name
            stub.write_text('#!/bin/sh\nexit 0\n')
            stub.chmod(0o700)
        (self.home / 'bin/op').write_text('#!/bin/sh\nexit 0\n')
        env_file = self.home / 'dev.env.op'
        env_file.write_text('NGC_API_KEY=op://Test/NGC/credential\n')
        env = {k: v for k, v in self.env.items() if 'TOKEN' not in k and 'PAT' not in k.split('_')}
        env['DYNAMO_OP_ENV_FILE'] = str(env_file)
        script = self.render('dot_local/bin/executable_setup-secrets.tmpl')
        web = 'GitHub.com > Settings > SSH and GPG keys'

        def check():
            result = subprocess.run(['bash', '-c', script], env=env, cwd=self.home, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result.stdout

        output = check()
        self.assertIn(web, output)
        self.assertNotIn('Or: ' + web, output)
        self.assertNotIn('gh ssh-key add', output)
        provisioned = self.home / '.ssh/provisioned'
        provisioned.mkdir(parents=True)
        (provisioned / 'git-signing-key.pub').write_text('ssh-ed25519 AAAA\n')
        self.git('config', '--global', 'user.signingkey', '~/.ssh/provisioned/git-signing-key')
        output = check()
        self.assertIn('gh ssh-key add ~/.ssh/provisioned/git-signing-key.pub', output)
        self.assertIn('Or: ' + web, output)


if __name__ == '__main__':
    unittest.main()

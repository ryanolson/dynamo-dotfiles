import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'dot_local/bin/executable_fleet'
USER_CA = 'ecdsa-sha2-nistp256 AAAAUSERCA'
HOST_CA = 'ecdsa-sha2-nistp256 AAAAHOSTCA'
TOKEN = 'eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.c2ln'

STEP_STUB = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
a = sys.argv[1:]
with open(os.environ['STEP_LOG'], 'a') as f:
    f.write(json.dumps(a) + '\n')
def opt(name):
    return a[a.index(name) + 1] if name in a else None
if a[:2] == ['ca', 'root']:
    Path(a[2]).write_text('ROOT\n')
elif a[:2] == ['ca', 'certificate']:
    if opt('--token') != os.environ['EXPECT_TOKEN']:
        sys.exit('bad token')
    Path(a[3]).write_text('CERT ' + a[2] + '\n')
    Path(a[4]).write_text('KEY\n')
elif a[:2] == ['certificate', 'needs-renewal']:
    sys.exit(int(os.environ.get('NEEDS_X509', '1')) if Path(a[2]).exists() else 2)
elif a[:2] in (['ca', 'renew'], ['ca', 'revoke']):
    pass
elif a[:2] == ['ssh', 'needs-renewal']:
    sys.exit(int(os.environ.get('NEEDS_SSH', '1')))
elif a[:2] == ['ssh', 'certificate']:
    pub = Path(a[3])
    pub.with_name(pub.name[:-4] + '-cert.pub').write_text('CERT\n')
elif a[:2] == ['ssh', 'config']:
    print(os.environ['HOST_CA'] if '--host' in a else os.environ['USER_CA'])
elif a[:2] == ['ca', 'token']:
    seen = {'key': Path(opt('--key')).read_text(), 'password': Path(opt('--password-file')).read_text(),
            'key_path': opt('--key'), 'sans': [a[i + 1] for i, x in enumerate(a) if x == '--san']}
    Path(os.environ['STEP_LOG']).with_name('token-seen.json').write_text(json.dumps(seen))
    print(os.environ['EXPECT_TOKEN'])
else:
    sys.exit('unexpected ' + ' '.join(a))
'''

OP_STUB = r'''#!/bin/sh
[ "$1" = read ] || exit 1
case "$2" in
  "op://Vault/Enroll/jwk_encrypted") printf 'JWE-KEY' ;;
  "op://Vault/Enroll/password") printf 'PASSWORD' ;;
  *) exit 2 ;;
esac
'''


class Fleet(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        bin_dir = self.home / 'bin'
        bin_dir.mkdir()
        for name, body in [('step', STEP_STUB), ('op', OP_STUB)]:
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o700)
        self.log = self.home / 'step.log'
        self.env = dict(os.environ, HOME=str(self.home), PATH=f'{bin_dir}:{os.environ["PATH"]}',
                        STEP_LOG=str(self.log), EXPECT_TOKEN=TOKEN, USER_CA=USER_CA, HOST_CA=HOST_CA,
                        XDG_CONFIG_HOME=str(self.home / '.config'), XDG_STATE_HOME=str(self.home / '.local/state'))
        self.state = self.home / '.local/state/fleet'

    def fleet(self, *args, stdin=None, **env):
        return subprocess.run(['bash', str(SCRIPT), *args], input=stdin, capture_output=True, text=True,
                              env=dict(self.env, **env))

    def enroll(self, *extra):
        result = self.fleet('enroll', 'node-1.fleet', '--ca-url', 'https://ca.example', '--fingerprint', 'ab' * 32,
                            *extra, stdin=TOKEN + '\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def mode(self, path):
        return stat.S_IMODE(path.stat().st_mode)

    def test_enroll_writes_identity_with_private_modes(self):
        self.enroll()
        self.assertEqual(self.mode(self.state), 0o700)
        for name in ['node.key', 'id_fleet']:
            self.assertEqual(self.mode(self.state / name), 0o600)
        self.assertTrue((self.state / 'id_fleet-cert.pub').exists())
        self.assertIn('CERT node-1.fleet', (self.state / 'node.crt').read_text())
        ssh_sign = [c for c in self.calls() if c[:2] == ['ssh', 'certificate']]
        self.assertEqual(len(ssh_sign), 1)
        self.assertIn('--sign', ssh_sign[0])
        self.assertEqual(ssh_sign[0][ssh_sign[0].index('--provisioner') + 1], 'fleet')
        self.assertNotIn(TOKEN, json.dumps([c for c in self.calls() if c[:2] != ['ca', 'certificate']]))

    def test_enroll_ssh_config_is_usable(self):
        self.enroll()
        fragment = self.home / '.ssh/fleet/config'
        result = subprocess.run(['ssh', '-G', '-F', str(fragment), 'example.com'], capture_output=True, text=True,
                                check=True, env=self.env)
        self.assertIn(f'certificatefile {self.state}/id_fleet-cert.pub\n', result.stdout)
        self.assertIn(f'identityfile {self.state}/id_fleet\n', result.stdout)
        self.assertIn(f'identityfile {self.home}/.ssh/id_ed25519\n', result.stdout)
        self.assertIn(f'{self.home}/.ssh/fleet/known_hosts', result.stdout)
        self.assertEqual((self.home / '.ssh/fleet/known_hosts').read_text(), f'@cert-authority * {HOST_CA}\n')

    def test_trust_preserves_existing_keys_and_is_idempotent(self):
        ssh = self.home / '.ssh'
        ssh.mkdir(mode=0o700)
        keys = ssh / 'authorized_keys'
        keys.write_text('ssh-ed25519 AAAAEXISTING laptop\n')
        keys.chmod(0o600)
        self.enroll()
        self.enroll()
        text = keys.read_text()
        self.assertTrue(text.startswith('ssh-ed25519 AAAAEXISTING laptop\n'))
        self.assertEqual(text.count('# BEGIN fleet'), 1)
        self.assertIn(f'cert-authority,principals="ryan" {USER_CA}\n', text)
        self.assertEqual(self.mode(keys), 0o600)
        self.assertEqual(self.fleet('untrust').returncode, 0)
        self.assertEqual(keys.read_text(), 'ssh-ed25519 AAAAEXISTING laptop\n')

    def test_no_trust_leaves_authorized_keys_alone(self):
        self.enroll('--no-trust')
        self.assertFalse((self.home / '.ssh/authorized_keys').exists())

    def test_principal_is_configurable(self):
        self.enroll('--principal', 'rolson')
        self.assertIn('cert-authority,principals="rolson"', (self.home / '.ssh/authorized_keys').read_text())

    def test_renew_does_nothing_while_certificates_are_fresh(self):
        self.enroll()
        self.log.unlink()
        result = self.fleet('renew')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse([c for c in self.calls() if c[:2] in (['ca', 'renew'], ['ssh', 'certificate'])])

    def test_renew_refreshes_expiring_certificates(self):
        self.enroll()
        self.log.unlink()
        result = self.fleet('renew', NEEDS_X509='0', NEEDS_SSH='0')
        self.assertEqual(result.returncode, 0, result.stderr)
        kinds = [c[:2] for c in self.calls()]
        self.assertIn(['ca', 'renew'], kinds)
        self.assertIn(['ssh', 'certificate'], kinds)

    def test_renew_reissues_missing_ssh_certificate(self):
        self.enroll()
        (self.state / 'id_fleet-cert.pub').unlink()
        self.log.unlink()
        self.assertEqual(self.fleet('renew').returncode, 0)
        self.assertIn(['ssh', 'certificate'], [c[:2] for c in self.calls()])

    def test_renew_without_enrollment_fails(self):
        result = self.fleet('renew')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not enrolled', result.stderr)

    def test_bad_token_leaves_no_identity(self):
        result = self.fleet('enroll', 'node-1.fleet', '--ca-url', 'https://ca.example', '--fingerprint', 'ab' * 32,
                            stdin='wrong.token.value\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.state / 'node.crt').exists())
        self.assertFalse((self.state / 'node.key').exists())

    def test_invalid_inputs_are_rejected(self):
        cases = {
            'name': ['enroll', 'bad name', '--ca-url', 'https://ca.example', '--fingerprint', 'ab' * 32],
            'url': ['enroll', 'node', '--ca-url', 'http://ca.example', '--fingerprint', 'ab' * 32],
            'fingerprint': ['enroll', 'node', '--ca-url', 'https://ca.example', '--fingerprint', 'xyz'],
            'principal': ['enroll', 'node', '--ca-url', 'https://ca.example', '--fingerprint', 'ab' * 32,
                          '--principal', 'a"b'],
        }
        for label, args in cases.items():
            with self.subTest(label):
                self.assertNotEqual(self.fleet(*args, stdin=TOKEN + '\n').returncode, 0)
        self.assertFalse(self.log.exists())

    def test_config_values_are_not_executed(self):
        cfg = self.home / '.config/fleet'
        cfg.mkdir(parents=True)
        marker = self.home / 'pwned'
        (cfg / 'config').write_text(f'ca_url=$(touch {marker})\n')
        self.fleet('status')
        self.assertFalse(marker.exists())

    def test_token_uses_1password_and_removes_secrets(self):
        init = self.fleet('init', '--ca-url', 'https://ca.example', '--fingerprint', 'ab' * 32,
                          '--enroll-item', 'op://Vault/Enroll')
        self.assertEqual(init.returncode, 0, init.stderr)
        result = self.fleet('token', 'node-2.fleet', '--san', 'node-2', '--san', 'node-2.example')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), TOKEN)
        seen = json.loads((self.home / 'token-seen.json').read_text())
        self.assertEqual((seen['key'], seen['password']), ('JWE-KEY', 'PASSWORD'))
        self.assertEqual(seen['sans'], ['node-2.fleet', 'node-2', 'node-2.example'])
        self.assertFalse(Path(seen['key_path']).exists())

    def test_enroll_remote_sends_token_on_stdin_and_removes_secrets(self):
        ssh = self.home / 'bin/ssh'
        ssh.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$HOME/ssh-args"\ncat > "$HOME/ssh-stdin"\n')
        ssh.chmod(0o700)
        self.fleet('init', '--ca-url', 'https://ca.example', '--fingerprint', 'ab' * 32,
                   '--enroll-item', 'op://Vault/Enroll')
        result = self.fleet('enroll-remote', 'user@box', 'box.fleet', '--no-trust')
        self.assertEqual(result.returncode, 0, result.stderr)
        args = (self.home / 'ssh-args').read_text().splitlines()
        self.assertEqual(args[0], 'user@box')
        self.assertIn('fleet enroll box.fleet --ca-url https://ca.example', args[1])
        self.assertIn('--no-trust', args[1])
        self.assertNotIn(TOKEN, args[1])
        self.assertEqual((self.home / 'ssh-stdin').read_text().strip(), TOKEN)
        seen = json.loads((self.home / 'token-seen.json').read_text())
        self.assertFalse(Path(seen['key_path']).exists())

    def test_revoke_uses_a_revoke_token(self):
        self.fleet('init', '--ca-url', 'https://ca.example', '--fingerprint', 'ab' * 32,
                   '--enroll-item', 'op://Vault/Enroll')
        result = self.fleet('revoke', '1234567890')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        token = [c for c in calls if c[:2] == ['ca', 'token']][0]
        self.assertEqual(token[2], '1234567890')
        self.assertIn('--revoke', token)
        revoke = [c for c in calls if c[:2] == ['ca', 'revoke']][0]
        self.assertEqual(revoke[2:4], ['1234567890', '--token'])
        self.assertEqual(revoke[4], TOKEN)
        seen = json.loads((self.home / 'token-seen.json').read_text())
        self.assertFalse(Path(seen['key_path']).exists())
        self.assertNotEqual(self.fleet('revoke', '12ab').returncode, 0)

    def test_status_reports_missing_enrollment(self):
        result = self.fleet('status')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not enrolled', result.stdout + result.stderr)


class SshConfigInclude(unittest.TestCase):
    def test_modify_script_includes_fleet_config_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = home / 'chezmoi.yaml'
            config.write_text('data:\n  name: Test\n  email: test@example.com\n')
            env = dict(os.environ, HOME=str(home))
            modifier = subprocess.run(['chezmoi', '--config', str(config), '--source', str(ROOT), 'execute-template'],
                                      input=(ROOT / 'private_dot_ssh/modify_private_config.tmpl').read_text(),
                                      capture_output=True, text=True, check=True, env=env).stdout
            text = 'Host example\n    User me\n'
            for _ in range(2):
                text = subprocess.run(['bash', '-c', modifier], input=text, capture_output=True, text=True,
                                      check=True, env=env).stdout
            lines = text.splitlines()
            self.assertEqual(lines[:2], ['Include ~/.ssh/provisioned/config', 'Include ~/.ssh/fleet/config'])
            self.assertEqual(text.count('Include ~/.ssh/fleet/config'), 1)
            self.assertIn('Host example\n    User me\n', text)


if __name__ == '__main__':
    unittest.main()

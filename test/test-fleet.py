import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'dot_local/bin/executable_fleet'
USER_CA = 'ecdsa-sha2-nistp256 AAAAUSERCA'
HOST_CA = 'ecdsa-sha2-nistp256 AAAAHOSTCA'
TOKEN = 'eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.c2ln'

def clean_env(**values):
    # Never all of os.environ: an inherited FLEET_AGENT_SOCK, FLEET_SECRETS_SOCK, SSH_AUTH_SOCK, or
    # XDG_RUNTIME_DIR points fleet at the host's live sockets, and some tests remove "their" socket.
    env = {k: v for k, v in os.environ.items() if k in ('PATH', 'LANG', 'TMPDIR', 'USER', 'LOGNAME') or k.startswith('LC_')}
    env.update(values)
    return env


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
  "op://Vault/Admin/jwk_encrypted") printf 'ADMIN-KEY' ;;
  "op://Vault/Admin/password") printf 'ADMIN-PASSWORD' ;;
  "op://Personal/Fleet config/ca_url") printf 'https://ca.example' ;;
  "op://Personal/Fleet config/fingerprint") printf 'abababababababababababababababababababababababababababababababab' ;;
  "op://Personal/Fleet config/broker_url") printf 'https://ca.example:8443' ;;
  "op://Personal/Fleet config/enroll_item") printf 'op://Vault/Enroll' ;;
  "op://Personal/Fleet config/admin_item") printf 'op://Vault/Admin' ;;
  *) exit 2 ;;
esac
'''


class FleetBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        bin_dir = self.home / 'bin'
        bin_dir.mkdir()
        recorder = '#!/bin/sh\nprintf "%s %s\\n" "$(basename "$0")" "$*" >> "$HOME/scheduler.log"\n'
        for name, body in [('step', STEP_STUB), ('op', OP_STUB), ('launchctl', recorder), ('systemctl', recorder),
                           ('loginctl', recorder), ('crontab', recorder), ('journalctl', recorder)]:
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o700)
        self.log = self.home / 'step.log'
        # On Linux, fleet puts the agent and secrets sockets under XDG_RUNTIME_DIR, and falls back to
        # FLEET_RUNTIME_BASE/UID (/run/user/UID) without it. Both are the test's, or a test that removes
        # its socket would remove the host's live one.
        runtime = self.home / 'run'
        runtime.mkdir(mode=0o700)
        (self.home / 'run-user').mkdir(mode=0o755)
        self.env = clean_env(HOME=str(self.home), PATH=f'{bin_dir}:{os.environ["PATH"]}',
                        XDG_RUNTIME_DIR=str(runtime), FLEET_RUNTIME_BASE=str(self.home / 'run-user'),
                        STEP_LOG=str(self.log), EXPECT_TOKEN=TOKEN, USER_CA=USER_CA, HOST_CA=HOST_CA,
                        XDG_CONFIG_HOME=str(self.home / '.config'), XDG_STATE_HOME=str(self.home / '.local/state'),
                        FLEET_PYLON=str(self.home / 'no-pylon'), FLEET_TAILSCALE=str(self.home / 'no-tailscale'))
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


class Fleet(FleetBase):
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
        self.assertNotIn('/.ssh/id_', result.stdout)
        self.assertIn(f'{self.home}/.ssh/fleet/known_hosts', result.stdout)
        self.assertEqual((self.home / '.ssh/fleet/known_hosts').read_text(), f'@cert-authority * {HOST_CA}\n')

    def test_existing_default_keys_stay_usable(self):
        ssh = self.home / '.ssh'
        ssh.mkdir(mode=0o700)
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(ssh / 'id_ed25519')], check=True)
        self.enroll()
        fragment = (ssh / 'fleet/config').read_text()
        self.assertIn(f'IdentityFile {ssh}/id_ed25519\n', fragment)
        self.assertNotIn('id_rsa', fragment)

    def test_renew_picks_up_a_new_default_key(self):
        self.enroll()
        ssh = self.home / '.ssh'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(ssh / 'id_ed25519')], check=True)
        self.assertEqual(self.fleet('renew').returncode, 0)
        self.assertIn(f'IdentityFile {ssh}/id_ed25519\n', (ssh / 'fleet/config').read_text())

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

    def pylon_stub(self, supports_renew=True):
        pylon = self.home / 'bin/pylon'
        renew = 'printf "%s\\n" "$@" > "$HOME/pylon-args"' if supports_renew else 'exit 2'
        pylon.write_text(f'#!/bin/sh\nif [ "$1" = renew ]; then\n  [ "$2" = --help ] && {{ {"exit 0" if supports_renew else "exit 2"}; }}\n'
                         f'  {renew}\nfi\n')
        pylon.chmod(0o700)
        return pylon

    def test_renew_uses_pylon_when_it_can_renew(self):
        self.enroll()
        self.log.unlink()
        pylon = self.pylon_stub()
        result = self.fleet('renew', FLEET_PYLON=str(pylon))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / 'pylon-args').read_text().splitlines(),
                         ['renew', '--ca-url', 'https://ca.example', '--state', str(self.state),
                          '--name', 'node-1.fleet'])
        self.assertFalse([c for c in self.calls() if c[:2] in (['ca', 'renew'], ['ssh', 'certificate'])])

    def test_renew_falls_back_to_step_for_an_older_pylon(self):
        self.enroll()
        self.log.unlink()
        pylon = self.pylon_stub(supports_renew=False)
        result = self.fleet('renew', FLEET_PYLON=str(pylon), NEEDS_X509='0', NEEDS_SSH='0')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(['ca', 'renew'], [c[:2] for c in self.calls()])
        self.assertFalse((self.home / 'pylon-args').exists())

    def test_renew_writes_ssh_aliases_from_the_broker(self):
        self.enroll()
        curl = self.home / 'bin/curl'
        curl.write_text('#!/bin/sh\ncase "$*" in *v1/ssh-hosts*) [ -f "$HOME/broker-down" ] && exit 7\n'
                        '  printf "bb brev.example.ts.net ubuntu\\nbad ProxyCommand=x u\\ngpu gpu.corp.example ryan bb\\nevil h u -oProxyCommand=x\\nip 100.64.0.7 u - ip-host.example.ts.net\\n" ;; esac\n')
        curl.chmod(0o700)
        self.assertEqual(self.fleet('renew').returncode, 0)
        config = self.home / '.ssh/fleet/config'
        text = config.read_text()
        self.assertIn('Host bb\n    HostName brev.example.ts.net\n    User ubuntu\n    Port 22\n    StrictHostKeyChecking accept-new\n', text)
        self.assertNotIn('ProxyCommand', text)
        self.assertIn('Host gpu\n    HostName gpu.corp.example\n    User ryan\n    Port 22\n    StrictHostKeyChecking accept-new\n    ProxyJump bb\n', text)
        self.assertNotIn('Host evil', text)
        self.assertIn('Host ip\n    HostName 100.64.0.7\n    User u\n    Port 22\n    StrictHostKeyChecking accept-new\n    HostKeyAlias ip-host.example.ts.net\n', text)
        self.assertNotIn('ProxyJump -', text)
        self.assertLess(text.index('Host bb'), text.index('Host *'))
        resolved = subprocess.run(['ssh', '-G', '-F', str(config), 'bb'], capture_output=True, text=True, check=True).stdout
        self.assertIn('hostname brev.example.ts.net\n', resolved)
        self.assertIn('user ubuntu\n', resolved)
        self.assertIn(f'certificatefile {self.state}/id_fleet-cert.pub', resolved)
        # The last alias has no jump and no host key alias: renew must still succeed.
        curl.write_text('#!/bin/sh\ncase "$*" in *v1/ssh-hosts*) printf "ip 100.64.0.7 u - k.example\\nbb h ubuntu\\n" ;; esac\n')
        result = self.fleet('renew')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Host bb\n    HostName h\n    User ubuntu\n    Port 22\n    StrictHostKeyChecking accept-new\nHost *.ts.net ', config.read_text())
        curl.write_text('#!/bin/sh\ncase "$*" in *v1/ssh-hosts*) [ -f "$HOME/broker-down" ] && exit 7\n'
                        '  printf "bb brev.example.ts.net ubuntu\\n" ;; esac\n')
        self.assertEqual(self.fleet('renew').returncode, 0)
        # A broker outage keeps the last list instead of dropping the aliases.
        (self.home / 'broker-down').write_text('')
        self.assertEqual(self.fleet('renew').returncode, 0)
        self.assertIn('Host bb\n', config.read_text())

    def test_a_fleet_alias_keeps_port_22_when_a_later_config_names_the_same_host(self):
        # ~/.brev/ssh_config is included after the fleet config and sets Port 11009 for spark-p.
        # SSH takes the first value of each option, so without a port of its own the alias got 11009.
        self.enroll()
        curl = self.home / 'bin/curl'
        curl.write_text('#!/bin/sh\ncase "$*" in *v1/ssh-hosts*) printf "spark-p 100.64.0.9 ryan - spark-p\\n" ;; esac\n')
        curl.chmod(0o700)
        self.assertEqual(self.fleet('renew').returncode, 0)
        main = self.home / '.ssh/config'
        brev = self.home / 'brev_ssh_config'
        brev.write_text('Host spark-p\n  HostName 127.0.0.1\n  Port 11009\n  User other\n')
        main.write_text(f'Include {self.home}/.ssh/fleet/config\nInclude {brev}\n')
        resolved = subprocess.run(['ssh', '-G', '-F', str(main), 'spark-p'], capture_output=True, text=True,
                                  check=True).stdout
        self.assertIn('port 22\n', resolved)
        self.assertIn('hostname 100.64.0.9\n', resolved)
        self.assertIn('user ryan\n', resolved)

    def test_ssh_config_accepts_new_host_keys_only_for_fleet_hosts(self):
        self.enroll()
        curl = self.home / 'bin/curl'
        curl.write_text('#!/bin/sh\ncase "$*" in *v1/ssh-hosts*) printf "bb 100.112.0.5 ubuntu - brev.example.ts.net\\n" ;; esac\n')
        curl.chmod(0o700)
        tailscale = self.home / 'bin/tailscale'
        tailscale.write_text('#!/bin/sh\n[ "$1 $2" = "status --json" ] || exit 1\n'
                             'printf \'{\\n  "MagicDNSSuffix": "tail1.ts.net",\\n  "Self": {}\\n}\\n\'\n')
        tailscale.chmod(0o700)
        self.assertEqual(self.fleet('renew', FLEET_TAILSCALE=str(tailscale)).returncode, 0)
        config = self.home / '.ssh/fleet/config'
        text = config.read_text()
        self.assertIn('    CanonicalDomains tail1.ts.net\n', text)
        self.assertIn('    CanonicalizeMaxDots 0\n', text)

        def checking(host):
            out = subprocess.run(['ssh', '-G', '-F', str(config), host], capture_output=True, text=True,
                                 check=True).stdout
            return next(line.split()[1] for line in out.splitlines() if line.startswith('stricthostkeychecking '))

        for host in ['bb', '100.64.0.1', '100.127.255.254', 'node.tail1.ts.net', 'other.ts.net']:
            self.assertEqual(checking(host), 'accept-new', host)
        for host in ['github.com', 'example.com', '100.63.1.1', '100.128.0.1', '10.0.0.1']:
            self.assertEqual(checking(host), 'ask', host)

        # Without tailscale, bare names stay as typed, and addresses and ts.net names still accept new keys.
        self.assertEqual(self.fleet('renew', FLEET_TAILSCALE=str(self.home / 'no-tailscale')).returncode, 0)
        text = config.read_text()
        self.assertNotIn('Canonical', text)
        self.assertEqual(checking('100.100.1.2'), 'accept-new')
        self.assertEqual(checking('example.com'), 'ask')

        # A malformed suffix is ignored rather than written into the config.
        tailscale.write_text('#!/bin/sh\nprintf \'{"MagicDNSSuffix": "x y\\\\nHost *"}\'\n')
        self.assertEqual(self.fleet('renew', FLEET_TAILSCALE=str(tailscale)).returncode, 0)
        self.assertNotIn('Canonical', config.read_text())

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
        self.assertEqual(args[:2], ['--', 'user@box'])
        self.assertIn('fleet enroll box.fleet --ca-url https://ca.example', args[2])
        self.assertIn('--no-trust', args[2])
        self.assertNotIn(TOKEN, args[2])
        self.assertEqual((self.home / 'ssh-stdin').read_text().strip(), TOKEN)
        seen = json.loads((self.home / 'token-seen.json').read_text())
        self.assertFalse(Path(seen['key_path']).exists())
        (self.home / 'ssh-args').unlink()
        result = self.fleet('enroll-remote', '-oProxyCommand', 'box.fleet', '--no-trust')
        self.assertIn('invalid HOST', result.stderr)
        self.assertFalse((self.home / 'ssh-args').exists())

    def test_revoke_uses_a_revoke_token(self):
        self.fleet('init', '--ca-url', 'https://ca.example', '--fingerprint', 'ab' * 32,
                   '--enroll-item', 'op://Vault/Enroll')
        result = self.fleet('revoke', '7mrlHuilZFCgm5n26BWUDU')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        token = [c for c in calls if c[:2] == ['ca', 'token']][0]
        self.assertEqual(token[2], '340142925354073613744636653243438135724')
        self.assertIn('--revoke', token)
        revoke = [c for c in calls if c[:2] == ['ca', 'revoke']][0]
        self.assertEqual(revoke[2:4], ['340142925354073613744636653243438135724', '--token'])
        self.assertEqual(revoke[4], TOKEN)
        seen = json.loads((self.home / 'token-seen.json').read_text())
        self.assertFalse(Path(seen['key_path']).exists())
        self.assertNotEqual(self.fleet('revoke', '12a-b').returncode, 0)

    def test_status_reports_the_agent_and_the_broker(self):
        self.enroll()
        curl = self.home / 'bin/curl'
        curl.write_text('#!/bin/sh\nprintf ok\n')
        curl.chmod(0o700)
        result = self.fleet('status')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('agent: off\n', result.stdout)
        self.assertIn('broker: ok\n', result.stdout)
        sock, _ = self.start_real_agent()
        (self.home / '.ssh/fleet').mkdir(parents=True, exist_ok=True)
        (self.home / '.ssh/fleet/agent').write_text(f'{sock}\n')
        pylon = self.home / 'bin/pylon'
        pylon.write_text('#!/bin/sh\nexit 0\n')
        pylon.chmod(0o700)
        curl.write_text('#!/bin/sh\nexit 7\n')
        result = self.fleet('status', FLEET_PYLON=str(pylon), FLEET_AGENT_SOCK=str(sock))
        self.assertIn(f'agent: pylon on {sock}, 2 keys\n', result.stdout)
        self.assertIn('broker: no answer\n', result.stdout)

    def test_status_shows_the_serial_in_base62(self):
        self.enroll()
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes',
                        '-keyout', '/dev/null', '-subj', '/CN=t', '-days', '1',
                        '-set_serial', '340142925354073613744636653243438135724',
                        '-out', str(self.state / 'node.crt')], check=True, capture_output=True)
        result = self.fleet('status')
        self.assertIn('serial: 7mrlHuilZFCgm5n26BWUDU\n', result.stdout)

    def socat_stub(self):
        socat = self.home / 'bin/socat'
        socat.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$HOME/socat-args"\n')
        socat.chmod(0o700)

    def test_agent_bridges_a_local_socket_to_the_broker(self):
        self.enroll()
        self.socat_stub()
        result = self.fleet('agent', XDG_RUNTIME_DIR=str(self.home / 'run'))
        self.assertEqual(result.returncode, 0, result.stderr)
        args = (self.home / 'socat-args').read_text().splitlines()
        sock = self.home / 'run/fleet-agent.sock' if os.uname().sysname == 'Linux' else self.home / '.ssh/fleet/agent.sock'
        self.assertEqual(args[-2], f'UNIX-LISTEN:{sock},fork,unlink-early,mode=600')
        self.assertEqual(args[-1], f'OPENSSL:ca.example:8444,cert={self.state}/node.crt,key={self.state}/node.key,'
                                   f'cafile={self.state}/root_ca.crt,verify=1')

    def test_agent_prefers_pylon(self):
        self.enroll()
        self.socat_stub()
        pylon = self.home / 'bin/pylon'
        pylon.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$HOME/pylon-args"\n')
        pylon.chmod(0o700)
        sock = self.home / 'a.sock'
        result = self.fleet('agent', FLEET_PYLON=str(pylon), FLEET_AGENT_SOCK=str(sock))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / 'pylon-args').read_text().splitlines(),
                         ['agent', '--socket', str(sock), '--broker', 'ca.example:8444',
                          '--cert', f'{self.state}/node.crt', '--key', f'{self.state}/node.key',
                          '--ca', f'{self.state}/root_ca.crt'])
        self.assertFalse((self.home / 'socat-args').exists())

    def tunnel(self, *args, **env):
        return self.fleet('tunnel', *args, FLEET_TUNNEL_NO_SERVICE='1', **env)

    def test_tunnel_client_listens_on_loopback_and_dials_mtls(self):
        self.enroll()
        self.socat_stub()
        r = self.tunnel('add', 'bb', '--mode', 'client', '--listen', '127.0.0.1:38900',
                        '--to', 'relay.example:8461', '--peer', 'bb-server')
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.tunnel('run', 'bb')
        self.assertEqual(r.returncode, 0, r.stderr)
        args = (self.home / 'socat-args').read_text().splitlines()
        self.assertEqual(args[-2], 'TCP-LISTEN:38900,bind=127.0.0.1,reuseaddr,fork')
        self.assertEqual(args[-1], f'OPENSSL:relay.example:8461,cert={self.state}/node.crt,key={self.state}/node.key,'
                                   f'cafile={self.state}/root_ca.crt,verify=1,commonname=bb-server')

    def test_tunnel_server_accepts_only_fleet_certificates(self):
        self.enroll()
        self.socat_stub()
        self.assertEqual(self.tunnel('add', 'door', '--mode', 'server', '--listen', '100.64.0.9:8461',
                                     '--to', '127.0.0.1:38886').returncode, 0)
        self.assertEqual(self.tunnel('run', 'door').returncode, 0)
        args = (self.home / 'socat-args').read_text().splitlines()
        self.assertEqual(args[-2], f'OPENSSL-LISTEN:8461,bind=100.64.0.9,reuseaddr,fork,cert={self.state}/node.crt,'
                                   f'key={self.state}/node.key,cafile={self.state}/root_ca.crt,verify=1')
        self.assertEqual(args[-1], 'TCP:127.0.0.1:38886')

    def test_tunnel_gate_admits_fleet_certificates_and_forwards_ciphertext(self):
        self.enroll()
        self.socat_stub()
        r = self.tunnel('add', 'hop', '--mode', 'gate', '--listen', '10.0.0.5:8461',
                        '--to', 'bb-server.example:8461', '--allow', '10.0.0.9/32')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.tunnel('run', 'hop').returncode, 0)
        args = (self.home / 'socat-args').read_text().splitlines()
        tls = f'cert={self.state}/node.crt,key={self.state}/node.key,cafile={self.state}/root_ca.crt,verify=1'
        self.assertEqual(args[-2], f'OPENSSL-LISTEN:8461,bind=10.0.0.5,reuseaddr,fork,{tls},range=10.0.0.9/32')
        # The inner TLS between client and server passes through unread.
        self.assertEqual(args[-1], 'TCP:bb-server.example:8461')

    def test_tunnel_client_via_a_gate_nests_tls(self):
        self.enroll()
        calls = self.home / 'socat-calls'
        socat = self.home / 'bin/socat'
        # The two socat processes run at once, so each call is one append, not one per argument.
        socat.write_text('#!/bin/sh\nline=$(printf "%s\\t" "$@")\nprintf "%s\\n" "$line" >> "$HOME/socat-calls"\n')
        socat.chmod(0o700)
        r = self.tunnel('add', 'bb', '--mode', 'client', '--listen', '127.0.0.1:38900', '--to', 'door.example:8461',
                        '--peer', 'bb-server', '--via', 'gate.example:8461', '--via-peer', 'gate')
        self.assertEqual(r.returncode, 0, r.stderr)
        # Both socat processes are stubs that exit at once. run exits non-zero when either one
        # ends, so the service manager restarts the pair.
        self.assertEqual(self.tunnel('run', 'bb').returncode, 1)
        tls = f'cert={self.state}/node.crt,key={self.state}/node.key,cafile={self.state}/root_ca.crt,verify=1'
        lines = sorted(l.rstrip('\t') for l in calls.read_text().splitlines())
        self.assertEqual(lines, sorted([
            # Outer layer: a loopback hop that wraps the inner stream in mTLS to the gate.
            f'TCP-LISTEN:38901,bind=127.0.0.1,reuseaddr,fork\tOPENSSL:gate.example:8461,{tls},commonname=gate',
            # Inner layer: mTLS to the server, checked against its fleet name, through the hop.
            f'TCP-LISTEN:38900,bind=127.0.0.1,reuseaddr,fork\tOPENSSL:127.0.0.1:38901,{tls},commonname=bb-server',
        ]))
        self.assertNotEqual(self.tunnel('add', 'bb', '--mode', 'client', '--listen', '127.0.0.1:38900',
                                        '--to', 'd:1', '--peer', 'p', '--via', 'g:1').returncode, 0)

    def test_tunnel_has_no_unauthenticated_relay(self):
        self.enroll()
        self.assertNotEqual(self.tunnel('add', 'hop', '--mode', 'relay', '--listen', '10.0.0.5:8461',
                                        '--to', 'h:8461').returncode, 0)
        for allow in ['10.0.0.9/32,exec=sh', 'any', '10.0.0.0/99']:
            self.assertNotEqual(self.tunnel('add', 'hop', '--mode', 'gate', '--listen', '10.0.0.5:8461',
                                            '--to', 'h:8461', '--allow', allow).returncode, 0, allow)

    def test_tunnel_rejects_bad_arguments(self):
        self.enroll()
        for args in [('add', 'x', '--mode', 'client', '--listen', '127.0.0.1:1', '--to', 'h:2'),
                     ('add', 'x', '--mode', 'bogus', '--listen', '127.0.0.1:1', '--to', 'h:2'),
                     ('add', 'x', '--mode', 'server', '--listen', '127.0.0.1:1', '--to', 'h:2,exec=sh'),
                     ('add', '../x', '--mode', 'server', '--listen', '127.0.0.1:1', '--to', 'h:2'),
                     ('run', 'missing')]:
            self.assertNotEqual(self.tunnel(*args).returncode, 0, args)

    def test_linux_sockets_stay_under_the_test_home(self):
        # The host's XDG_RUNTIME_DIR must not reach fleet, whatever the parent environment holds.
        runtime = Path(self.env['XDG_RUNTIME_DIR'])
        self.assertTrue(runtime.is_relative_to(self.home), runtime)
        self.enroll()
        (self.home / 'bin/uname').write_text('#!/bin/sh\n[ "$1" = -m ] && echo x86_64 || echo Linux\n')
        (self.home / 'bin/uname').chmod(0o700)
        sentinel = runtime / 'fleet-agent.sock'
        sentinel.write_text('')
        result = self.fleet('agent', 'remove')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(sentinel.exists())
        for line in (self.home / 'scheduler.log').read_text().splitlines():
            self.assertTrue(line.split()[0] in ('systemctl', 'launchctl', 'loginctl', 'crontab', 'journalctl'), line)

    def test_agent_refuses_a_socket_path_that_is_too_long(self):
        self.enroll()
        self.socat_stub()
        result = self.fleet('agent', FLEET_AGENT_SOCK='/tmp/' + 'x' * 120)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('too long', result.stderr)
        self.assertFalse((self.home / 'socat-args').exists())

    def test_agent_install_schedules_a_service(self):
        sock, _ = self.start_real_agent()
        self.enroll()
        self.socat_stub()
        result = self.fleet('agent', 'install', FLEET_AGENT_SOCK=str(sock))
        self.assertEqual(result.returncode, 0, result.stderr)
        scheduled = (self.home / 'scheduler.log').read_text()
        self.assertTrue('launchctl bootstrap' in scheduled or 'systemctl --user enable --now fleet-agent.service' in scheduled,
                        scheduled)
        self.assertIn('SSH_AUTH_SOCK=', result.stdout)

    def start_real_agent(self):
        sock = Path(tempfile.mkdtemp(dir='/tmp')) / 'a.sock'
        self.addCleanup(lambda: __import__('shutil').rmtree(sock.parent, ignore_errors=True))
        out = subprocess.run(['ssh-agent', '-a', str(sock)], capture_output=True, text=True, check=True).stdout
        pid = int(out.split('SSH_AGENT_PID=')[1].split(';')[0])
        self.addCleanup(lambda: subprocess.run(['kill', str(pid)]))
        for name in ['agent-signing', 'agent-auth']:
            key = self.home / name
            subprocess.run(['ssh-keygen', '-q', '-t', 'ecdsa', '-b', '256', '-N', '', '-C', f'fleet {name}',
                            '-f', str(key)], check=True)
            subprocess.run(['ssh-add', '-q', str(key)], env=clean_env(SSH_AUTH_SOCK=str(sock)), check=True)
        return sock, (self.home / 'agent-signing.pub').read_text().split()[:2]

    def test_agent_install_wires_git_signing_and_github(self):
        sock, pub = self.start_real_agent()
        ssh = self.home / '.ssh'
        ssh.mkdir(mode=0o700)
        (ssh / 'allowed_signers').write_text('me@example.com ssh-ed25519 AAAAOLDKEY\n')
        self.enroll()
        self.socat_stub()
        env = dict(FLEET_AGENT_SOCK=str(sock))
        result = self.fleet('agent', 'install', **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        fragment = (ssh / 'fleet/gitconfig').read_text()
        # A file path, not key::: git before 2.35 (Ubuntu 22.04 has 2.34) reads key:: as a path.
        self.assertIn(f'signingkey = {ssh}/fleet/agent-signing.pub\n', fragment)
        self.assertEqual((ssh / 'fleet/agent-signing.pub').read_text().split()[:2], pub)
        self.assertIn(f'program = {self.home}/.local/bin/fleet-ssh-keygen\n', fragment)
        self.assertIn('gpgsign = true\n', fragment)
        signers = (ssh / 'fleet/allowed_signers').read_text()
        self.assertIn('me@example.com ssh-ed25519 AAAAOLDKEY\n', signers)
        self.assertIn(f'* {" ".join(pub)}\n', signers)
        config = ssh / 'fleet/config'
        github = subprocess.run(['ssh', '-G', '-F', str(config), 'github.com'], capture_output=True, text=True,
                                check=True).stdout
        self.assertIn(f'identityagent {sock}\n', github)
        self.assertIn('identitiesonly yes\n', github)
        self.assertIn(f'identityfile {ssh}/fleet/agent-auth.pub\n', github)
        other = subprocess.run(['ssh', '-G', '-F', str(config), 'example.com'], capture_output=True, text=True,
                               check=True).stdout
        self.assertNotIn(str(sock), other)
        self.assertEqual(self.fleet('renew', **env).returncode, 0)
        self.assertIn('Host github.com', config.read_text())

        wrapper = self.home / '.local/bin/fleet-ssh-keygen'
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        wrapper.write_text((ROOT / 'dot_local/bin/executable_fleet-ssh-keygen').read_text())
        wrapper.chmod(0o755)
        repo = self.home / 'repo'
        repo.mkdir()
        genv = clean_env(HOME=str(self.home), SSH_AUTH_SOCK='/nonexistent', FLEET_AGENT_SOCK=str(sock),
                    GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=str(self.home / '.gitconfig'))
        (self.home / '.gitconfig').write_text(f'[user]\n    name = T\n    email = t@example.com\n'
                                              f'[include]\n    path = {ssh}/fleet/gitconfig\n')
        git = lambda *a: subprocess.run(['git', '-C', str(repo), *a], env=genv, capture_output=True, text=True)
        git('init', '-q')
        commit = git('commit', '--allow-empty', '-m', 'signed')
        self.assertEqual(commit.returncode, 0, commit.stderr)
        verify = git('verify-commit', 'HEAD')
        self.assertEqual(verify.returncode, 0, verify.stderr)
        self.assertIn('Good "git" signature', verify.stderr)
        status = lambda: subprocess.run(['bash', str(ROOT / 'dot_local/bin/executable_git-signing-status')],
                                        env=genv, capture_output=True, text=True)
        self.assertEqual(status().stdout.strip(), 'ready: the fleet agent holds the signing key')
        genv['FLEET_AGENT_SOCK'] = str(self.home / 'gone.sock')
        self.assertTrue(status().stdout.startswith('degraded: the fleet agent'))
        genv['FLEET_AGENT_SOCK'] = str(sock)

        self.assertEqual(self.fleet('agent', 'remove', **env).returncode, 0)
        self.assertFalse((ssh / 'fleet/gitconfig').exists())
        self.assertFalse((ssh / 'fleet/allowed_signers').exists())
        self.assertFalse((ssh / 'fleet/agent-auth.pub').exists())
        self.assertFalse((ssh / 'fleet/agent-signing.pub').exists())
        self.assertNotIn('Host github.com', config.read_text())

    def test_agent_install_fails_without_a_signing_key(self):
        self.enroll()
        self.socat_stub()
        result = self.fleet('agent', 'install', FLEET_AGENT_SOCK=str(self.home / 'missing.sock'),
                            FLEET_AGENT_WAIT='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('agent-signing', result.stderr)
        self.assertFalse((self.home / '.ssh/fleet/gitconfig').exists())

    def test_agent_needs_socat(self):
        self.enroll()
        result = self.fleet('agent', FLEET_SOCAT=str(self.home / 'no-socat'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('pylon or socat', result.stderr)

    def test_uninstall_removes_every_fleet_piece(self):
        ssh = self.home / '.ssh'
        ssh.mkdir(mode=0o700)
        (ssh / 'authorized_keys').write_text('ssh-ed25519 AAAAOTHERKEY me@laptop\n')
        sock, _ = self.start_real_agent()
        self.enroll()
        self.socat_stub()
        self.assertEqual(self.fleet('timer', 'install').returncode, 0)
        self.assertEqual(self.fleet('agent', 'install', FLEET_AGENT_SOCK=str(sock)).returncode, 0)
        self.assertEqual(self.fleet('tunnel', 'add', 'bb', '--mode', 'client', '--listen', '127.0.0.1:38900',
                                    '--to', 'h:8461', '--peer', 'p').returncode, 0)
        self.assertIn('BEGIN fleet', (ssh / 'authorized_keys').read_text())
        self.log.unlink()
        result = self.fleet('uninstall', '--yes', FLEET_AGENT_SOCK=str(sock))
        self.assertEqual(result.returncode, 0, result.stderr)
        revoke = [c for c in self.calls() if c[:2] == ['ca', 'revoke']]
        self.assertEqual(len(revoke), 1)
        self.assertIn('--cert', revoke[0])
        for gone in [self.state, self.home / '.config/fleet', ssh / 'fleet']:
            self.assertFalse(gone.exists(), gone)
        self.assertEqual((ssh / 'authorized_keys').read_text(), 'ssh-ed25519 AAAAOTHERKEY me@laptop\n')
        scheduler = (self.home / 'scheduler.log').read_text()
        labels = (['dev.fleet.renew', 'dev.fleet.agent', 'dev.fleet.tunnel.bb']
                  if os.uname().sysname == 'Darwin' else
                  ['disable --now fleet-renew.timer', 'disable --now fleet-agent.service',
                   'disable --now fleet-tunnel-bb.service'])
        for label in labels:
            self.assertIn(label, scheduler)
        for plist in ['dev.fleet.renew', 'dev.fleet.agent', 'dev.fleet.tunnel.bb']:
            self.assertFalse((self.home / f'Library/LaunchAgents/{plist}.plist').exists(), plist)
        self.assertIn('removed', result.stdout)

    def test_uninstall_needs_confirmation_and_can_keep_the_certificate(self):
        self.enroll()
        result = self.fleet('uninstall', stdin='')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.state / 'node.crt').exists())
        self.log.unlink()
        result = self.fleet('uninstall', '--yes', '--keep-cert')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse([c for c in self.calls() if c[:2] == ['ca', 'revoke']])
        self.assertFalse(self.state.exists())

    def test_uninstall_cleans_leftovers_without_an_enrollment(self):
        (self.home / '.ssh/fleet').mkdir(parents=True)
        (self.home / '.ssh/fleet/config').write_text('Host *\n')
        result = self.fleet('uninstall', '--yes')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / '.ssh/fleet').exists())
        self.assertNotIn('remove', self.fleet('uninstall', '--bogus').stdout)
        self.assertNotEqual(self.fleet('uninstall', '--bogus').returncode, 0)

    def test_status_reports_missing_enrollment(self):
        result = self.fleet('status')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not enrolled', result.stdout + result.stderr)


class FleetLaptop(FleetBase):
    def init(self, *extra):
        result = self.fleet('init', '--ca-url', 'https://ca.example', '--fingerprint', 'ab' * 32,
                            '--enroll-item', 'op://Vault/Enroll', *extra)
        self.assertEqual(result.returncode, 0, result.stderr)

    def source_repo(self, pushed=True):
        repo = self.home / 'src'
        repo.mkdir()
        git = lambda *a: subprocess.run(['git', '-C', str(repo), *a], check=True, capture_output=True, text=True).stdout
        git('init', '-q')
        git('-c', 'user.name=T', '-c', 'user.email=t@example.com', 'commit', '-q', '--allow-empty', '-m', 'x')
        git('remote', 'add', 'origin', 'git@github.com:owner/dots.git')
        if pushed:
            git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        return repo, git('rev-parse', 'HEAD').strip()

    def test_invite_prints_a_pinned_join_command(self):
        self.init()
        repo, sha = self.source_repo()
        result = self.fleet('invite', 'box', '--san', 'box.example', '--ephemeral', '--no-trust', FLEET_SOURCE=str(repo))
        self.assertEqual(result.returncode, 0, result.stderr)
        line = result.stdout.strip().splitlines()[-1]
        self.assertTrue(line.startswith("bash -c 'exec bash <(curl -fsSL \"$0\") \"$@\"' "))
        self.assertIn(f'https://raw.githubusercontent.com/owner/dots/{sha}/join.sh', line)
        for part in ['--repo owner/dots', f'--ref {sha}', '--name box', '--ca-url https://ca.example',
                     f'--fingerprint {"ab" * 32}', f'--token {TOKEN}', '--ephemeral', '--no-trust']:
            self.assertIn(part, line)
        token = [c for c in self.calls() if c[:2] == ['ca', 'token']][0]
        self.assertEqual(token[token.index('--not-after') + 1], '15m')
        seen = json.loads((self.home / 'token-seen.json').read_text())
        self.assertEqual(seen['sans'], ['box', 'box.example'])
        self.assertFalse(Path(seen['key_path']).exists())

    def test_invite_refuses_an_unpushed_commit(self):
        self.init()
        repo, _ = self.source_repo(pushed=False)
        result = self.fleet('invite', 'box', FLEET_SOURCE=str(repo))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('push', result.stderr)
        self.assertFalse([c for c in self.calls() if c[:2] == ['ca', 'token']])

    def test_login_uses_the_admin_provisioner_without_trust(self):
        self.init('--admin-item', 'op://Vault/Admin')
        result = self.fleet('login', 'laptop-1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('CERT laptop-1', (self.state / 'node.crt').read_text())
        self.assertFalse((self.home / '.ssh/authorized_keys').exists())
        seen = json.loads((self.home / 'token-seen.json').read_text())
        self.assertEqual((seen['key'], seen['password']), ('ADMIN-KEY', 'ADMIN-PASSWORD'))
        self.assertFalse(Path(seen['key_path']).exists())
        token = [c for c in self.calls() if c[:2] == ['ca', 'token']][0]
        self.assertEqual(token[token.index('--provisioner') + 1], 'admin')
        self.assertEqual(self.fleet('login').returncode, 0)

    def test_login_needs_an_admin_item(self):
        self.init()
        result = self.fleet('login', 'laptop-1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--admin-item', result.stderr)

    def test_login_on_a_new_laptop_reads_the_config_from_1password_and_schedules_renewal(self):
        result = self.fleet('login', 'laptop-2')
        self.assertEqual(result.returncode, 0, result.stderr)
        config = (self.home / '.config/fleet/config').read_text()
        for line in ['ca_url=https://ca.example', 'broker_url=https://ca.example:8443', 'admin_item=op://Vault/Admin',
                     'enroll_item=op://Vault/Enroll', 'name=laptop-2']:
            self.assertIn(line + '\n', config)
        token = [c for c in self.calls() if c[:2] == ['ca', 'token']][0]
        self.assertEqual(token[token.index('--provisioner') + 1], 'admin')
        scheduled = (self.home / 'scheduler.log').read_text()
        self.assertTrue('launchctl bootstrap' in scheduled or 'systemctl --user enable --now fleet-renew.timer' in scheduled,
                        scheduled)

    def test_login_keeps_an_existing_config(self):
        self.init('--admin-item', 'op://Vault/Admin', '--broker-url', 'https://other.example:8443')
        self.assertEqual(self.fleet('login', 'laptop-3').returncode, 0)
        self.assertIn('broker_url=https://other.example:8443\n', (self.home / '.config/fleet/config').read_text())

    def test_list_asks_the_broker_with_this_machines_certificate(self):
        self.enroll()
        self.init('--broker-url', 'https://ca.example:8443')
        curl = self.home / 'bin/curl'
        curl.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$HOME/curl-args"\nprintf "NAME STATE\\nspark-d active\\n"\n')
        curl.chmod(0o700)
        result = self.fleet('list')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('spark-d active', result.stdout)
        args = (self.home / 'curl-args').read_text().splitlines()
        for flag, value in [('--cert', f'{self.state}/node.crt'), ('--key', f'{self.state}/node.key'),
                            ('--cacert', f'{self.state}/root_ca.crt'), ('-H', 'Accept: text/plain')]:
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertEqual(args[-1], 'https://ca.example:8443/v1/machines')
        self.fleet('list', '--json')
        self.assertIn('Accept: application/json', (self.home / 'curl-args').read_text())
        for flag in ['-a', '--all']:
            self.assertEqual(self.fleet('list', flag).returncode, 0)
            self.assertEqual((self.home / 'curl-args').read_text().splitlines()[-1],
                             'https://ca.example:8443/v1/machines?all=1')
        self.fleet('list', '-a', '--json')
        args = (self.home / 'curl-args').read_text()
        self.assertIn('Accept: application/json', args)
        self.assertIn('/v1/machines?all=1', args)
        self.assertNotEqual(self.fleet('list', '--bogus').returncode, 0)


    def secret_curl(self):
        curl = self.home / 'bin/curl'
        curl.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$HOME/curl-args"\n'
                        'case "$*" in *v1/secrets/missing*) exit 22 ;; esac\nprintf "tok-123"\n')
        curl.chmod(0o700)
        self.enroll()
        self.init('--broker-url', 'https://ca.example:8443')

    def test_secret_get_asks_the_broker_for_the_raw_value(self):
        self.secret_curl()
        result = self.fleet('secret', 'get', 'claude-oauth-token')
        self.assertEqual((result.returncode, result.stdout), (0, 'tok-123'), result.stderr)
        args = (self.home / 'curl-args').read_text().splitlines()
        self.assertEqual(args[-1], 'https://ca.example:8443/v1/secrets/claude-oauth-token')
        self.assertIn('Accept: application/octet-stream', args)
        self.assertEqual(args[args.index('--cert') + 1], f'{self.state}/node.crt')
        self.assertNotEqual(self.fleet('secret', 'get', 'missing').returncode, 0)
        for bad in ['../x', 'A', '']:
            (self.home / 'curl-args').unlink(missing_ok=True)
            self.assertNotEqual(self.fleet('secret', 'get', bad).returncode, 0)
            self.assertFalse((self.home / 'curl-args').exists())

    def test_broker_url_defaults_to_the_ca_host(self):
        self.enroll()
        curl = self.home / 'bin/curl'
        curl.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$HOME/curl-args"\nprintf ok\n')
        curl.chmod(0o700)
        result = self.fleet('secret', 'get', 'fleet-selftest')
        self.assertEqual((result.returncode, result.stdout), (0, 'ok'), result.stderr)
        self.assertEqual((self.home / 'curl-args').read_text().splitlines()[-1],
                         'https://ca.example:8443/v1/secrets/fleet-selftest')

    def test_secret_env_runs_a_command_without_printing_the_value(self):
        self.secret_curl()
        out = self.home / 'out'
        result = self.fleet('secret', 'env', 'TOKEN=claude-oauth-token', '--', 'sh', '-c', f'printf %s "$TOKEN" > {out}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(out.read_text(), 'tok-123')
        self.assertNotIn('tok-123', result.stdout + result.stderr)
        self.assertNotEqual(self.fleet('secret', 'env', 'TOKEN=missing', '--', 'true').returncode, 0)
        self.assertNotEqual(self.fleet('secret', 'env', 'bad-var=x', '--', 'true').returncode, 0)

    REF = 'op://Fleet/NGC NIM Operations PAT/credential'
    ENCODED = 'op%3A%2F%2FFleet%2FNGC%20NIM%20Operations%20PAT%2Fcredential'

    def secrets_socket(self):
        # A bound socket with no server: the curl stub plays pylon, so only the path must be a socket.
        import socket as socketlib
        path = self.home / '.ssh/fleet/secrets.sock'
        path.parent.mkdir(parents=True, exist_ok=True)
        sock = socketlib.socket(socketlib.AF_UNIX)
        sock.bind(str(path))
        self.addCleanup(sock.close)
        return path

    def ref_curl(self):
        # The curl stub answers as pylon for --unix-socket when $PYLON_UP is set, and as the broker otherwise.
        self.secret_curl()
        curl = self.home / 'bin/curl'
        curl.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$HOME/curl-args"; echo == >> "$HOME/curl-args"\n'
                        'case "$*" in *--unix-socket*) [ -n "$PYLON_UP" ] || exit 7; printf pylon-val; exit 0 ;; esac\n'
                        'printf broker-val\n')
        (self.home / 'curl-args').unlink(missing_ok=True)

    def curl_calls(self):
        return [c.strip().splitlines() for c in (self.home / 'curl-args').read_text().split('==') if c.strip()]

    def test_secret_get_by_reference_asks_pylon_first(self):
        self.ref_curl()
        sock = self.secrets_socket()
        result = self.fleet('secret', 'get', self.REF, PYLON_UP='1', FLEET_SECRETS_SOCK=str(sock))
        self.assertEqual((result.returncode, result.stdout), (0, 'pylon-val'), result.stderr)
        (call,) = self.curl_calls()
        self.assertEqual(call[call.index('--unix-socket') + 1], str(sock))
        self.assertEqual(call[-1], f'http://pylon/v1/secrets?ref={self.ENCODED}')

    def test_secret_get_by_reference_falls_back_to_the_broker(self):
        self.ref_curl()
        sock = self.secrets_socket()
        result = self.fleet('secret', 'get', self.REF, FLEET_SECRETS_SOCK=str(sock))
        self.assertEqual((result.returncode, result.stdout), (0, 'broker-val'), result.stderr)
        pylon, broker = self.curl_calls()
        self.assertEqual(broker[-1], f'https://ca.example:8443/v1/secrets?ref={self.ENCODED}')
        self.assertEqual(broker[broker.index('--cert') + 1], f'{self.state}/node.crt')
        (self.home / 'curl-args').unlink()
        result = self.fleet('secret', 'get', self.REF, FLEET_SECRETS_SOCK=str(self.home / 'none.sock'))
        self.assertEqual(result.stdout, 'broker-val')
        self.assertEqual(len(self.curl_calls()), 1)

    def test_secret_env_takes_references(self):
        self.ref_curl()
        out = self.home / 'out'
        result = self.fleet('secret', 'env', f'NGC_API_KEY={self.REF}', 'TOKEN=claude-oauth-token', '--',
                            'sh', '-c', f'printf "%s|%s" "$NGC_API_KEY" "$TOKEN" > {out}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(out.read_text(), 'broker-val|broker-val')

    def test_invalid_references_are_refused_before_any_request(self):
        self.ref_curl()
        for bad in ['op://Fleet/x', 'op://Fleet/a/b/c/d', 'op://Fleet/a?b/c', 'op://Fleet/a/b;rm', 'op:/Fleet/a/b']:
            self.assertNotEqual(self.fleet('secret', 'get', bad).returncode, 0, bad)
        self.assertFalse((self.home / 'curl-args').exists())

    def test_secret_flush_asks_pylon(self):
        self.ref_curl()
        sock = self.secrets_socket()
        result = self.fleet('secret', 'flush', PYLON_UP='1', FLEET_SECRETS_SOCK=str(sock))
        self.assertEqual(result.returncode, 0, result.stderr)
        (call,) = self.curl_calls()
        self.assertIn('POST', call)
        self.assertEqual(call[-1], 'http://pylon/v1/flush')
        self.assertNotEqual(self.fleet('secret', 'flush', FLEET_SECRETS_SOCK=str(self.home / 'none.sock')).returncode, 0)

    def test_agent_turns_on_the_secrets_socket_when_pylon_has_it(self):
        self.enroll()
        pylon = self.home / 'bin/pylon'
        pylon.write_text('#!/bin/sh\nif [ "$2" = --help ]; then echo "      --secrets-socket <PATH>"; exit 0; fi\n'
                         'printf "%s\\n" "$@" > "$HOME/pylon-args"\n')
        pylon.chmod(0o700)
        result = self.fleet('agent', FLEET_PYLON=str(pylon), FLEET_AGENT_SOCK=str(self.home / 'a.sock'),
                            FLEET_SECRETS_SOCK=str(self.home / 's.sock'))
        self.assertEqual(result.returncode, 0, result.stderr)
        args = (self.home / 'pylon-args').read_text().splitlines()
        self.assertEqual(args[args.index('--secrets-socket') + 1], str(self.home / 's.sock'))
        self.assertEqual(args[args.index('--broker-url') + 1], 'https://ca.example:8443')

    def test_claude_launcher_supplies_the_oauth_token(self):
        self.secret_curl()
        claude = self.home / 'bin/claude'
        claude.write_text('#!/bin/sh\nprintf "%s %s" "$CLAUDE_CODE_OAUTH_TOKEN" "$*" > "$HOME/claude-run"\n')
        claude.chmod(0o700)
        result = self.fleet('claude', '--version')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / 'claude-run').read_text(), 'tok-123 --version')
        self.assertTrue((self.home / 'curl-args').read_text().splitlines()[-1].endswith('/v1/secrets/claude-oauth-token'))


    def test_codex_launcher_supplies_the_access_token(self):
        self.secret_curl()
        codex = self.home / 'bin/codex'
        codex.write_text('#!/bin/sh\nprintf "%s %s" "$CODEX_ACCESS_TOKEN" "$*" > "$HOME/codex-run"\n')
        codex.chmod(0o700)
        result = self.fleet('codex', 'exec', 'hi')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / 'codex-run').read_text(), 'tok-123 exec hi')
        self.assertTrue((self.home / 'curl-args').read_text().splitlines()[-1].endswith('/v1/secrets/codex-access-token'))


class FleetEphemeral(FleetBase):
    def start(self, *extra):
        runtime = self.home / 'run'
        self.stderr_path = self.home / 'ephemeral.err'
        stderr = self.stderr_path.open('w')
        self.addCleanup(stderr.close)
        proc = subprocess.Popen(['bash', str(SCRIPT), 'ephemeral', 'pod-1', '--ca-url', 'https://ca.example',
                                 '--fingerprint', 'ab' * 32, *extra], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=stderr, text=True, env=dict(self.env, XDG_RUNTIME_DIR=str(runtime)))
        self.addCleanup(proc.stdout.close)
        proc.stdin.write(TOKEN + '\n')
        proc.stdin.close()
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        line = proc.stdout.readline()
        self.assertIn('ephemeral identity pod-1 is active', line, self.stderr_path.read_text())
        return proc, runtime

    def test_ephemeral_identity_lives_in_memory_and_is_removed_on_exit(self):
        keys = self.home / '.ssh/authorized_keys'
        keys.parent.mkdir(mode=0o700)
        keys.write_text('ssh-ed25519 AAAAEXISTING laptop\n')
        proc, runtime = self.start()
        state = [p for p in runtime.iterdir() if p.name.startswith('fleet-ephemeral.')]
        self.assertEqual(len(state), 1)
        self.assertTrue((state[0] / 'state/fleet/node.key').exists())
        self.assertFalse(self.state.exists())
        fragment = (self.home / '.ssh/fleet/config').read_text()
        self.assertIn(str(state[0]), fragment)
        self.assertIn('# BEGIN fleet', keys.read_text())
        proc.terminate()
        self.assertEqual(proc.wait(timeout=20), 0, self.stderr_path.read_text())
        self.assertNotIn('Terminated', self.stderr_path.read_text())
        self.assertFalse(state[0].exists())
        self.assertFalse((self.home / '.ssh/fleet').exists())
        self.assertEqual(keys.read_text(), 'ssh-ed25519 AAAAEXISTING laptop\n')
        revoke = [c for c in self.calls() if c[:2] == ['ca', 'revoke']]
        self.assertEqual(len(revoke), 1)
        self.assertIn('--cert', revoke[0])

    def test_ephemeral_refuses_a_persistent_enrollment(self):
        self.enroll()
        result = self.fleet('ephemeral', 'pod-1', '--ca-url', 'https://ca.example', '--fingerprint', 'ab' * 32,
                            stdin=TOKEN + '\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('persistent', result.stderr)


class FleetStateDir(FleetBase):
    # Task 032, part 2: a Slurm home is shared, so the identity goes to a node-local FLEET_STATE_DIR.
    NETWORK = ['nfs', 'nfs4', 'lustre', 'gpfs', 'cifs', 'smb2', 'smbfs', 'beegfs', 'fuse.sshfs']
    LOCAL = ['ext4', 'ext2/ext3', 'xfs', 'btrfs', 'apfs', 'tmpfs', 'overlay']

    def setUp(self):
        super().setUp()
        self.local = self.home / 'node-local/fleet'

    def linux(self):
        (self.home / 'bin/uname').write_text('#!/bin/sh\n[ "$1" = -m ] && echo x86_64 || echo Linux\n')
        (self.home / 'bin/uname').chmod(0o700)

    def test_state_goes_to_fleet_state_dir_when_set(self):
        result = self.fleet('enroll', 'node-1.fleet', '--ca-url', 'https://ca.example', '--fingerprint', 'ab' * 32,
                            stdin=TOKEN + '\n', FLEET_STATE_DIR=str(self.local))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('CERT node-1.fleet', (self.local / 'node.crt').read_text())
        self.assertEqual(self.mode(self.local), 0o700)
        self.assertFalse(self.state.exists())
        self.assertIn(str(self.local / 'id_fleet'), (self.home / '.ssh/fleet/config').read_text())

    def test_state_goes_to_the_default_when_unset(self):
        self.enroll()
        self.assertTrue((self.state / 'node.crt').exists())
        self.assertFalse(self.local.exists())

    def test_a_relative_state_dir_is_refused(self):
        for bad in ['node-local/fleet', '/tmp/a b', '/tmp/a%b']:
            result = self.fleet('status', FLEET_STATE_DIR=bad)
            self.assertNotEqual(result.returncode, 0, bad)
            self.assertIn('FLEET_STATE_DIR', result.stderr)

    def test_services_receive_the_state_dir(self):
        self.linux()
        env = {'FLEET_STATE_DIR': str(self.local)}
        units = self.home / '.config/systemd/user'
        self.assertEqual(self.fleet('timer', 'install', **env).returncode, 0)
        self.assertIn(f'Environment=FLEET_STATE_DIR={self.local}\n', (units / 'fleet-renew.service').read_text())
        enrolled = self.fleet('enroll', 'node-1.fleet', '--ca-url', 'https://ca.example', '--fingerprint', 'ab' * 32,
                              stdin=TOKEN + '\n', **env)
        self.assertEqual(enrolled.returncode, 0, enrolled.stderr)
        (self.home / 'bin/socat').write_text('#!/bin/sh\n')
        (self.home / 'bin/socat').chmod(0o700)
        self.fleet('agent', 'install', FLEET_AGENT_WAIT='0', FLEET_SOCAT=str(self.home / 'bin/socat'), **env)
        self.assertIn(f'Environment=FLEET_STATE_DIR={self.local}\n', (units / 'fleet-agent.service').read_text())
        self.assertEqual(self.fleet('timer', 'install').returncode, 0)
        self.assertNotIn('FLEET_STATE_DIR', (units / 'fleet-renew.service').read_text())

    def test_launchd_agents_receive_the_state_dir(self):
        (self.home / 'bin/uname').write_text('#!/bin/sh\n[ "$1" = -m ] && echo arm64 || echo Darwin\n')
        (self.home / 'bin/uname').chmod(0o700)
        self.assertEqual(self.fleet('timer', 'install', FLEET_STATE_DIR=str(self.local)).returncode, 0)
        plist = (self.home / 'Library/LaunchAgents/dev.fleet.renew.plist').read_text()
        self.assertIn('<key>EnvironmentVariables</key><dict><key>FLEET_STATE_DIR</key>'
                      f'<string>{self.local}</string></dict>', plist)

    def doctor(self, fstype, **env):
        self.linux()
        (self.home / 'bin/findmnt').write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" > "$HOME/findmnt-args"\necho {fstype}\n')
        (self.home / 'bin/findmnt').chmod(0o700)
        return self.fleet('doctor', **env)

    def test_doctor_warns_for_each_network_file_system(self):
        for fstype in self.NETWORK:
            with self.subTest(fstype=fstype):
                result = self.doctor(fstype)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'network file system ({fstype})', result.stderr)
                self.assertIn('FLEET_STATE_DIR', result.stderr)

    def test_doctor_is_quiet_for_local_file_systems(self):
        for fstype in self.LOCAL:
            with self.subTest(fstype=fstype):
                result = self.doctor(fstype)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn('warning', result.stderr)
                self.assertIn(f'({fstype})', result.stdout)

    def test_doctor_checks_the_state_dir_or_its_nearest_parent(self):
        result = self.doctor('nfs4', FLEET_STATE_DIR=str(self.local))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(self.local), result.stdout)
        self.assertEqual((self.home / 'findmnt-args').read_text().split()[-1], str(self.home))

    def test_doctor_reads_the_macos_mount_table(self):
        (self.home / 'bin/uname').write_text('#!/bin/sh\n[ "$1" = -m ] && echo arm64 || echo Darwin\n')
        (self.home / 'bin/uname').chmod(0o700)
        (self.home / 'bin/df').write_text('#!/bin/sh\necho "Filesystem 512-blocks Used Available Capacity Mounted on"\n'
                                          'echo "srv:/home 100 1 99 1% /Volumes/home dir"\n')
        (self.home / 'bin/mount').write_text('#!/bin/sh\necho "/dev/disk3s1 on / (apfs, local, journaled)"\n'
                                             'echo "srv:/home on /Volumes/home dir (nfs, nodev, nosuid)"\n')
        for name in ('df', 'mount'):
            (self.home / f'bin/{name}').chmod(0o700)
        result = self.fleet('doctor')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('network file system (nfs)', result.stderr)

    def test_doctor_reports_an_unknown_type_without_failing(self):
        result = self.doctor('')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('unknown', result.stdout)
        self.assertNotIn('warning', result.stderr)


class InstallStep(FleetBase):
    # A Linux host with fake download tools. The step binary is a script that prints a version.
    def setUp(self):
        super().setUp()
        bin_dir = self.home / 'bin'
        stubs = {
            'uname': '#!/bin/sh\n[ "$1" = -m ] && echo aarch64 || echo Linux\n',
            'curl': '#!/bin/sh\nwhile [ $# -gt 0 ] && [ "$1" != -o ]; do shift; done; [ $# -ge 2 ] || exit 22; : > "$2"\n',
            'sha256sum': '#!/bin/sh\ncat >/dev/null\n',
            'tar': '#!/bin/sh\nd="$4/step_linux_arm64/bin"; mkdir -p "$d"\n'
                   'printf "#!/bin/sh\\necho Smallstep CLI\\n" > "$d/step"\n',
        }
        for name, body in stubs.items():
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o700)
        (bin_dir / 'step').unlink()

    def test_sudo_host_gets_step_in_local_bin(self):
        # The shell turns on the no-sudo layout when ~/.local/<arch> exists, so fleet must not make it.
        result = self.fleet('install-step')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / '.local/bin/step').exists())
        self.assertFalse((self.home / '.local/aarch64').exists())

    def test_nosudo_host_keeps_step_in_the_arch_folder(self):
        (self.home / '.local/aarch64').mkdir(parents=True)
        result = self.fleet('install-step')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / '.local/aarch64/bin/step').exists())
        self.assertFalse((self.home / '.local/bin/step').exists())


class InstallPylon(FleetBase):
    # A Linux aarch64 host. The curl stub writes $CURL_BODY to the -o file and logs the URL.
    BODY = b'\x7fELF fake pylon'

    def setUp(self):
        super().setUp()
        bin_dir = self.home / 'bin'
        stubs = {
            'uname': '#!/bin/sh\n[ "$1" = -m ] && echo aarch64 || echo Linux\n',
            'curl': '#!/bin/sh\nprintf "%s\\n" "$*" >> "$HOME/curl.log"\n'
                    'while [ $# -gt 0 ] && [ "$1" != -o ]; do shift; done; [ $# -ge 2 ] || exit 22\n'
                    'cp "$CURL_BODY" "$2"\n',
        }
        for name, body in stubs.items():
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o700)
        self.body = self.home / 'body'
        self.body.write_bytes(self.BODY)
        # The pin is part of the script, so the test runs a copy with the pin of the fake binary.
        text = SCRIPT.read_text()
        self.version = re.search(r'^PYLON_VERSION=(\S+)$', text, re.M).group(1)
        good = hashlib.sha256(self.BODY).hexdigest()
        self.script = self.home / 'fleet'
        self.script.write_text(re.sub(r'^(PYLON_SHA256_linux_aarch64=)\S+$', r'\g<1>' + good, text, flags=re.M))
        self.enroll()

    def install(self):
        return subprocess.run(['bash', str(self.script), 'install-pylon'], capture_output=True, text=True,
                              env=dict(self.env, CURL_BODY=str(self.body)))

    def curls(self):
        log = self.home / 'curl.log'
        return [line for line in log.read_text().splitlines() if '/v1/artifacts/' in line] if log.exists() else []

    def test_installs_the_pinned_binary_from_the_broker(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        pylon = self.home / '.local/bin/pylon'
        self.assertEqual(pylon.read_bytes(), self.BODY)
        self.assertEqual(self.mode(pylon), 0o755)
        (curl,) = self.curls()
        self.assertIn(f'https://ca.example:8443/v1/artifacts/pylon/{self.version}/linux-aarch64', curl)
        self.assertIn(f'--cert {self.state}/node.crt', curl)
        self.assertIn('systemctl --user restart fleet-agent.service', self.restarts())

    def restarts(self):
        log = self.home / 'scheduler.log'
        return [l for l in log.read_text().splitlines() if 'fleet-agent' in l or 'dev.fleet.agent' in l] \
            if log.exists() else []

    def test_a_current_binary_is_not_downloaded_again(self):
        self.install()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.curls()), 1)

    def test_the_agent_restarts_also_when_the_binary_was_current(self):
        # A pylon-install task proves the daemon that runs, so a current file is no reason to skip.
        self.assertEqual(self.install().returncode, 0)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.restarts(), ['systemctl --user restart fleet-agent.service'] * 2)

    def test_a_failed_restart_fails_install(self):
        (self.home / 'bin/systemctl').write_text('#!/bin/sh\nprintf "systemctl %s\\n" "$*" >> "$HOME/scheduler.log"\n'
                                                 'case "$*" in *restart*) exit 5 ;; esac\n')
        for label in ['new binary', 'current binary']:
            result = self.install()
            self.assertNotEqual(result.returncode, 0, label)
            self.assertIn('could not restart the fleet agent', result.stderr, label)
        self.assertEqual((self.home / '.local/bin/pylon').read_bytes(), self.BODY)

    def test_macos_restarts_the_launchd_agent_and_fails_when_it_cannot(self):
        (self.home / 'bin/uname').write_text('#!/bin/sh\n[ "$1" = -m ] && echo arm64 || echo Darwin\n')
        text = self.script.read_text()
        good = hashlib.sha256(self.BODY).hexdigest()
        self.script.write_text(re.sub(r'^(PYLON_SHA256_darwin_arm64=)\S+$', r'\g<1>' + good, text, flags=re.M))
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.restarts(), [f'launchctl kickstart -k gui/{os.getuid()}/dev.fleet.agent'])
        (self.home / 'bin/launchctl').write_text('#!/bin/sh\nexit 113\n')
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('could not restart the fleet agent', result.stderr)

    def test_a_wrong_checksum_installs_nothing(self):
        pylon = self.home / '.local/bin/pylon'
        pylon.parent.mkdir(parents=True, exist_ok=True)
        pylon.write_text('old')
        self.body.write_bytes(b'tampered')
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('checksum', result.stderr)
        self.assertEqual(pylon.read_text(), 'old')
        self.assertEqual(list(pylon.parent.glob('.fleet.*')), [])

    def test_nosudo_host_installs_into_the_arch_folder(self):
        (self.home / '.local/aarch64').mkdir(parents=True)
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual((self.home / '.local/aarch64/bin/pylon').read_bytes(), self.BODY)
        self.assertFalse((self.home / '.local/bin/pylon').exists())

def find_openssl3():
    # The test certificates need OpenSSL 3.4 or later, for -not_before and -not_after.
    for path in [os.environ.get('FLEET_TEST_OPENSSL'), '/opt/homebrew/opt/openssl@3/bin/openssl',
                 str(Path.home() / '.pixi/envs/socat/bin/openssl'), shutil.which('openssl')]:
        if path and os.path.exists(path):
            usage = subprocess.run([path, 'x509', '-help'], capture_output=True, text=True)
            if '-not_before' in usage.stdout + usage.stderr:
                return path
    return None


OPENSSL3 = find_openssl3()


class Pki:
    # The fleet's shape: a root, an intermediate, and leaves; node.crt holds the leaf and the intermediate.
    def __init__(self, folder):
        self.dir = Path(folder)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.serial = 1000

    def ssl(self, *args):
        subprocess.run([OPENSSL3, *args], check=True, capture_output=True, cwd=self.dir)

    def key(self, name):
        self.ssl('genpkey', '-algorithm', 'EC', '-pkeyopt', 'ec_paramgen_curve:P-256', '-out', f'{name}.key')
        return self.dir / f'{name}.key'

    def sign(self, name, issuer, ext, validity):
        self.serial += 1
        self.ssl('req', '-new', '-key', f'{name}.key', '-subj', f'/CN={name}', '-out', f'{name}.csr')
        (self.dir / f'{name}.ext').write_text(ext)
        self.ssl('x509', '-req', '-in', f'{name}.csr', '-CA', f'{issuer}.crt', '-CAkey', f'{issuer}.key',
                 '-set_serial', str(self.serial), '-extfile', f'{name}.ext', '-out', f'{name}.crt', *validity)

    def ca(self, root, intermediate):
        self.key(root)
        self.ssl('req', '-x509', '-new', '-key', f'{root}.key', '-subj', f'/CN={root}', '-days', '2',
                 '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign',
                 '-out', f'{root}.crt')
        self.key(intermediate)
        self.sign(intermediate, root, 'basicConstraints=critical,CA:TRUE,pathlen:0\n'
                                      'keyUsage=critical,keyCertSign,cRLSign\n', ['-days', '2'])
        return self.dir / f'{root}.crt'

    def fingerprint(self, crt):
        out = subprocess.run([OPENSSL3, 'x509', '-noout', '-fingerprint', '-sha256', '-in', str(crt)],
                             check=True, capture_output=True, text=True).stdout
        return out.strip().split('=', 1)[1].replace(':', '').lower()

    def leaf(self, file, name, intermediate, eku='serverAuth,clientAuth', validity=('-days', '1')):
        # file names the output; name is the certificate's CN and DNS name.
        self.key(file)
        self.serial += 1
        self.ssl('req', '-new', '-key', f'{file}.key', '-subj', f'/CN={name}', '-out', f'{file}.csr')
        (self.dir / f'{file}.ext').write_text('basicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature\n'
                                              f'extendedKeyUsage={eku}\nsubjectAltName=DNS:{name}\n')
        self.ssl('x509', '-req', '-in', f'{file}.csr', '-CA', f'{intermediate}.crt', '-CAkey', f'{intermediate}.key',
                 '-set_serial', str(self.serial), '-extfile', f'{file}.ext', '-out', f'{file}.leaf', *validity)
        chain = self.dir / f'{file}.crt'
        chain.write_text((self.dir / f'{file}.leaf').read_text() + (self.dir / f'{intermediate}.crt').read_text())
        serial = f'{self.serial:X}'
        # OpenSSL and Python print a serial as whole bytes.
        return chain, self.dir / f'{file}.key', serial.zfill(len(serial) + len(serial) % 2)


class Door(FleetBase):
    def setUp(self):
        super().setUp()
        self.prefix = self.home / 'root'
        self.env['FLEET_DOOR_PREFIX'] = str(self.prefix)

    def pin(self, fingerprint):
        config = self.home / '.config/fleet/config'
        config.write_text(re.sub(r'^fingerprint=.*$', f'fingerprint={fingerprint}', config.read_text(), flags=re.M))

    def render(self, *args):
        if not (self.home / '.config/fleet/config').exists():
            self.enroll()
        out = self.home / 'out'
        result = self.fleet('door', 'render', str(out), *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        files = {str(p.relative_to(out)): p.read_text() for p in out.rglob('*') if p.is_file()}
        return out, files

    def lines(self, text):
        return [l.strip() for l in text.splitlines()]

    def test_render_defaults_to_a_public_door_with_client_certificates_and_limits(self):
        out, files = self.render()
        p = self.prefix
        self.assertEqual(sorted(files), ['etc/fleet-door/nginx.conf', 'etc/systemd/system/fleet-door-sync.path',
                                         'etc/systemd/system/fleet-door-sync.service',
                                         'etc/systemd/system/fleet-door-sync.timer',
                                         'etc/systemd/system/fleet-door.service', 'usr/local/libexec/fleet-door-sync'])
        conf = self.lines(files['etc/fleet-door/nginx.conf'])
        for line in ['load_module /usr/lib/nginx/modules/ngx_stream_module.so;', f'pid {p}/run/fleet-door.pid;', 'worker_shutdown_timeout 1h;',
                     'listen 0.0.0.0:8461 ssl;', 'limit_conn door_source 64;', 'limit_conn door_all 1024;',
                     f'ssl_certificate {p}/etc/fleet-door/tls/current/node.crt;',
                     f'ssl_certificate_key {p}/etc/fleet-door/tls/current/node.key;',
                     f'ssl_client_certificate {p}/etc/fleet-door/tls/current/root_ca.crt;', 'ssl_verify_client on;',
                     'ssl_verify_depth 1;', 'ssl_protocols TLSv1.2 TLSv1.3;', 'ssl_ciphers HIGH:!aNULL:!eNULL:!PSK;',
                     'ssl_handshake_timeout 10s;', 'proxy_pass 127.0.0.1:38886;']:
            self.assertIn(line, conf)
        # The total limit leaves out 127.0.0.1 only, where the sync checks the served certificate.
        loopback = ['map $remote_addr $door_all_key {', '127.0.0.1 "";', 'default $server_port;', '}',
                    'limit_conn_zone $door_all_key zone=door_all:1m;']
        start = conf.index(loopback[0])
        self.assertEqual(conf[start:start + len(loopback)], loopback)
        self.assertEqual([l for l in conf if l.startswith('limit_conn_zone')],
                         ['limit_conn_zone $binary_remote_addr zone=door_source:10m;', loopback[-1]])
        unit = self.lines(files['etc/systemd/system/fleet-door.service'])
        for line in [f'ExecStartPre={p}/usr/local/libexec/fleet-door-sync copy',
                     f"ExecStart=/usr/sbin/nginx -c {p}/etc/fleet-door/nginx.conf -g 'daemon off;'",
                     'ExecReload=/bin/kill -HUP $MAINPID', 'Restart=always', 'RestartSec=5', 'TasksMax=32',
                     'MemoryMax=256M', 'LimitNOFILE=16384', 'ProtectSystem=full',
                     f'ReadWritePaths={p}/etc/fleet-door/tls']:
            self.assertIn(line, unit)
        path = self.lines(files['etc/systemd/system/fleet-door-sync.path'])
        self.assertIn(f'PathChanged={self.state}/node.crt', path)
        self.assertIn(f'PathChanged={self.state}/node.key', path)
        self.assertIn('OnUnitActiveSec=15min', self.lines(files['etc/systemd/system/fleet-door-sync.timer']))
        self.assertIn(f'ExecStart={p}/usr/local/libexec/fleet-door-sync',
                      self.lines(files['etc/systemd/system/fleet-door-sync.service']))
        self.assertTrue(os.access(out / 'usr/local/libexec/fleet-door-sync', os.X_OK))

    def test_render_takes_the_listener_target_limits_and_deadline(self):
        _, files = self.render('--listen', '127.0.0.1:9461', '--to', '127.0.0.1:9000', '--per-source', '3',
                               '--max', '8', '--handshake-timeout', '2')
        conf = self.lines(files['etc/fleet-door/nginx.conf'])
        for line in ['listen 127.0.0.1:9461 ssl;', 'proxy_pass 127.0.0.1:9000;', 'limit_conn door_source 3;',
                     'limit_conn door_all 8;', 'ssl_handshake_timeout 2s;']:
            self.assertIn(line, conf)

    def test_render_needs_the_fleet_ca_fingerprint_from_enrollment(self):
        # The root-run sync trusts only this CA, not whatever root_ca.crt the user's state holds.
        r = self.fleet('door', 'render', str(self.home / 'out'))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('fingerprint', r.stderr)
        self.enroll()
        _, files = self.render()
        self.assertIn(f"fingerprint={'ab' * 32}", files['usr/local/libexec/fleet-door-sync'])

    def test_render_rejects_bad_options(self):
        self.enroll()
        out = str(self.home / 'out')
        for args in [('--listen', '0.0.0.0:8461;'), ('--to', '127.0.0.1:1 ssl'), ('--per-source', '0'),
                     ('--per-source', 'x'), ('--max', '8;'), ('--handshake-timeout', '0'),
                     ('--handshake-timeout', '61'), ('--handshake-timeout', '5s'), ('--bogus', '1')]:
            self.assertNotEqual(self.fleet('door', 'render', out, *args).returncode, 0, args)
        # The served-certificate check runs from loopback, which the total limit does not count.
        for listen in ['10.0.0.5:8461', '100.112.182.96:8461', 'door.example:8461']:
            r = self.fleet('door', 'render', out, '--listen', listen)
            self.assertNotEqual(r.returncode, 0, listen)
            self.assertIn('0.0.0.0 or a 127.', r.stderr)
        self.assertNotEqual(self.fleet('door', 'bogus').returncode, 0)
        self.assertNotEqual(self.fleet('door', 'render').returncode, 0)

    def install_stubs(self):
        bin_dir = self.home / 'bin'
        (bin_dir / 'sudo').write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$HOME/sudo.log"\n'
                                      '[ "$1" = -v ] && exit 0\nexec "$@"\n')
        (bin_dir / 'sudo').chmod(0o700)
        nginx = self.home / 'nginx'
        nginx.write_text('#!/bin/sh\nexit 0\n')
        nginx.chmod(0o700)
        module = self.home / 'ngx_stream_module.so'
        module.write_text('')
        return dict(FLEET_DOOR_NGINX=str(nginx), FLEET_DOOR_MODULE=str(module))

    def test_install_works_where_sudo_v_asks_for_a_password(self):
        # A sudoers file with both a password rule and a NOPASSWD rule makes sudo -v ask for a
        # password (verifypw=all), while sudo -n runs commands. brev is set up this way.
        self.enroll()
        env = self.install_stubs()
        (self.home / 'bin/sudo').write_text(
            '#!/bin/sh\nprintf "%s\\n" "$*" >> "$HOME/sudo.log"\n'
            '[ "$1" = -v ] && { echo "sudo: a password is required" >&2; exit 1; }\n'
            '[ "$1" = -n ] && shift\nexec "$@"\n')
        result = self.fleet('door', 'install', **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.prefix / 'etc/fleet-door/nginx.conf').exists())

    def test_install_writes_root_files_and_starts_the_units(self):
        self.enroll()
        env = self.install_stubs()
        result = self.fleet('door', 'install', **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.prefix / 'etc/fleet-door/nginx.conf').exists())
        self.assertTrue((self.prefix / 'etc/systemd/system/fleet-door.service').exists())
        self.assertEqual(self.mode(self.prefix / 'usr/local/libexec/fleet-door-sync'), 0o755)
        self.assertEqual(self.mode(self.prefix / 'etc/fleet-door/nginx.conf'), 0o644)
        self.assertEqual(self.mode(self.prefix / 'etc/fleet-door/tls'), 0o700)
        log = (self.home / 'scheduler.log').read_text().splitlines()
        self.assertEqual(log, ['systemctl daemon-reload',
                               'systemctl enable fleet-door.service fleet-door-sync.path fleet-door-sync.timer',
                               'systemctl restart fleet-door.service',
                               'systemctl restart fleet-door-sync.path fleet-door-sync.timer',
                               'systemctl start fleet-door-sync.service'])

    def test_install_never_gives_root_a_file_the_user_can_write(self):
        # A process running as the user could swap such a file during the sudo prompt; the sync
        # script then runs as root. Content goes to root on stdin, and sudo names only destinations.
        self.enroll()
        env = self.install_stubs()
        result = self.fleet('door', 'install', **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = (self.home / 'sudo.log').read_text().splitlines()
        # Credentials come first: sudo -n true where no password is needed, else sudo -v.
        self.assertIn(calls[0], ('-n true', '-v'))
        for call in calls:
            for word in call.split():
                if word.startswith('/'):
                    self.assertTrue(word.startswith(str(self.prefix) + '/'), call)
        out = self.home / 'out'
        self.assertEqual(self.fleet('door', 'render', str(out), **env).returncode, 0)
        for path in [p for p in out.rglob('*') if p.is_file()]:
            rel = path.relative_to(out)
            self.assertEqual((self.prefix / rel).read_text(), path.read_text(), rel)
            self.assertEqual(self.mode(self.prefix / rel), self.mode(path), rel)

    def test_install_needs_enrollment_and_the_nginx_stream_module(self):
        env = self.install_stubs()
        self.assertIn('not enrolled', self.fleet('door', 'install', **env).stderr)
        self.enroll()
        missing = dict(env, FLEET_DOOR_MODULE=str(self.home / 'none.so'))
        self.assertIn('libnginx-mod-stream', self.fleet('door', 'install', **missing).stderr)
        self.assertFalse(self.prefix.exists())

    def test_remove_and_uninstall_stop_the_door(self):
        self.enroll()
        env = self.install_stubs()
        self.assertEqual(self.fleet('door', 'install', **env).returncode, 0)
        (self.home / 'scheduler.log').unlink()
        result = self.fleet('uninstall', '--yes', '--keep-cert', **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('systemctl disable --now fleet-door.service fleet-door-sync.path fleet-door-sync.timer',
                      (self.home / 'scheduler.log').read_text())
        self.assertFalse((self.prefix / 'etc/fleet-door').exists())
        self.assertFalse((self.prefix / 'etc/systemd/system/fleet-door.service').exists())
        self.assertFalse((self.prefix / 'usr/local/libexec/fleet-door-sync').exists())

    @unittest.skipUnless(OPENSSL3, 'needs OpenSSL 3.4 or later for test certificates')
    def test_sync_copy_takes_only_a_matching_current_fleet_certificate(self):
        self.enroll()
        pki = Pki(self.home / 'pki')
        root = pki.ca('root', 'inter')
        good, good_key, serial = pki.leaf('door', 'door', 'inter')
        pki.ca('evil', 'evilinter')
        evil, evil_key, _ = pki.leaf('evildoor', 'door', 'evilinter')
        evil_root = pki.dir / 'evil.crt'
        expired, expired_key, _ = pki.leaf('old', 'door', 'inter',
                                            validity=('-not_before', '20200101000000Z', '-not_after', '20200102000000Z'))
        self.pin(pki.fingerprint(root))
        out, _ = self.render()
        sync = out / 'usr/local/libexec/fleet-door-sync'
        env = dict(PATH=f'{Path(OPENSSL3).parent}:{self.env["PATH"]}')
        tls = self.prefix / 'etc/fleet-door/tls'

        def use(crt, key, ca=root):
            shutil.copy(crt, self.state / 'node.crt')
            shutil.copy(key, self.state / 'node.key')
            shutil.copy(ca, self.state / 'root_ca.crt')
            return subprocess.run([str(sync), 'copy'], capture_output=True, text=True, env=dict(self.env, **env))

        # A door from before the version folders kept its files flat in tls/.
        tls.mkdir(parents=True)
        for f in ('node.crt', 'node.key', 'root_ca.crt'):
            (tls / f).write_text('old')
        r = use(good, good_key)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f'serial {serial}', r.stderr)
        for f in ('node.crt', 'node.key', 'root_ca.crt'):
            self.assertFalse((tls / f).exists(), f)
        self.assertEqual((tls / 'current/node.crt').read_text(), good.read_text())
        self.assertEqual(self.mode(tls), 0o700)
        self.assertEqual(self.mode(tls / 'current/node.key'), 0o600)
        r = use(good, good_key)
        self.assertEqual((r.returncode, r.stderr), (0, ''))
        # The last case is a user-writable state with its own CA: a consistent chain under another root.
        for crt, key, ca, why in [(good, evil_key, root, 'do not match'),
                                  (evil, evil_key, root, 'not issued by the fleet CA'),
                                  (expired, expired_key, root, 'expired'),
                                  (evil, evil_key, evil_root, 'is not the fleet CA')]:
            r = use(crt, key, ca)
            self.assertNotEqual(r.returncode, 0, why)
            self.assertIn(why, r.stderr)
            self.assertEqual((tls / 'current/node.crt').read_text(), good.read_text(), why)
            self.assertEqual((tls / 'current/root_ca.crt').read_text(), root.read_text(), why)
        self.assertEqual(sorted(p.name for p in tls.iterdir() if p.name != '.lock' and p.name != 'current'),
                         [os.readlink(tls / 'current')])
        (self.state / 'node.key').unlink()
        r = subprocess.run([str(sync), 'copy'], capture_output=True, text=True, env=dict(self.env, **env))
        self.assertIn('cannot read', r.stderr)

    @unittest.skipUnless(OPENSSL3, 'needs OpenSSL 3.4 or later for test certificates')
    def test_sync_copy_runs_one_at_a_time_and_swaps_all_files_at_once(self):
        self.enroll()
        pki = Pki(self.home / 'pki')
        root = pki.ca('root', 'inter')
        self.pin(pki.fingerprint(root))
        pairs = [pki.leaf(f'door{n}', 'door', 'inter')[:2] for n in range(2)]
        known = {(c.read_text(), k.read_text()) for c, k in pairs}
        out, _ = self.render()
        sync = out / 'usr/local/libexec/fleet-door-sync'
        env = dict(self.env, PATH=f'{Path(OPENSSL3).parent}:{self.env["PATH"]}')
        tls = self.prefix / 'etc/fleet-door/tls'
        # Each run reads its own state, so the runs disagree about what the door should hold.
        states = []
        for n in range(8):
            state = self.home / f'state{n}/fleet'
            state.mkdir(parents=True)
            shutil.copy(pairs[n % 2][0], state / 'node.crt')
            shutil.copy(pairs[n % 2][1], state / 'node.key')
            shutil.copy(root, state / 'root_ca.crt')
            states.append(state)
        seen, stop = [], threading.Event()

        def read():
            # nginx reads the files through current at reload; every read must see one matching pair.
            while not stop.is_set():
                try:
                    d = tls / os.readlink(tls / 'current')
                    seen.append(((d / 'node.crt').read_text(), (d / 'node.key').read_text()))
                except OSError:
                    pass

        reader = threading.Thread(target=read)
        reader.start()
        procs = [subprocess.Popen(['bash', '-c', f'sed "s#^src=.*#src={state}#" "$1" > "$1.$2" && '
                                  f'chmod +x "$1.$2" && exec "$1.$2" copy', '-', str(sync), str(n)],
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for n, state in enumerate(states * 3)]
        results = [p.communicate(timeout=120) + (p.returncode,) for p in procs]
        stop.set()
        reader.join()
        for out_, err, rc in results:
            self.assertEqual(rc, 0, err)
        self.assertTrue(seen)
        self.assertEqual(set(seen) - known, set())
        self.assertEqual(sorted(p.name for p in tls.iterdir() if p.name not in ('.lock', 'current')),
                         [os.readlink(tls / 'current')])

    @unittest.skipUnless(OPENSSL3 and shutil.which('timeout'), 'needs OpenSSL 3.4 or later and timeout')
    def test_served_check_ends_when_the_door_does_not_answer(self):
        self.enroll()
        pki = Pki(self.home / 'pki')
        root = pki.ca('root', 'inter')
        self.pin(pki.fingerprint(root))
        crt, key, _ = pki.leaf('door', 'door', 'inter')
        shutil.copy(crt, self.state / 'node.crt')
        shutil.copy(key, self.state / 'node.key')
        shutil.copy(root, self.state / 'root_ca.crt')
        # A listener that accepts TCP and never speaks TLS.
        mute = socket.socket()
        mute.bind(('127.0.0.1', 0))
        mute.listen(16)
        self.addCleanup(mute.close)
        out, _ = self.render('--listen', f'127.0.0.1:{mute.getsockname()[1]}')
        env = dict(self.env, PATH=f'{Path(OPENSSL3).parent}:{self.env["PATH"]}')
        start = time.time()
        r = subprocess.run([str(out / 'usr/local/libexec/fleet-door-sync')], capture_output=True, text=True,
                           env=env, timeout=120)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('serves serial none', r.stderr)
        self.assertLess(time.time() - start, 40)

    @unittest.skipUnless(OPENSSL3 and shutil.which('timeout'), 'needs OpenSSL 3.4 or later and timeout')
    def test_sync_releases_the_lock_before_it_reloads_the_door(self):
        # systemctl reload waits for a start in progress, whose ExecStartPre (sync copy) takes the lock.
        # systemd starts ExecStartPre with its own environment, so the stand-in drops FLEET_DOOR_LOCKED.
        self.enroll()
        pki = Pki(self.home / 'pki')
        root = pki.ca('root', 'inter')
        self.pin(pki.fingerprint(root))
        first, renewed = pki.leaf('door', 'door', 'inter'), pki.leaf('door2', 'door', 'inter')
        env = dict(self.env, PATH=f'{Path(OPENSSL3).parent}:{self.env["PATH"]}')

        def use(identity):
            shutil.copy(identity[0], self.state / 'node.crt')
            shutil.copy(identity[1], self.state / 'node.key')
            shutil.copy(root, self.state / 'root_ca.crt')

        use(first)
        port = free_port()
        out, _ = self.render('--listen', f'127.0.0.1:{port}')
        sync = out / 'usr/local/libexec/fleet-door-sync'
        self.assertEqual(subprocess.run([str(sync), 'copy'], capture_output=True, env=env).returncode, 0)
        # The door stand-in serves tls/current; a reload restarts it, as nginx rereads the files.
        current = self.prefix / 'etc/fleet-door/tls/current'
        door = (f'{OPENSSL3} s_server -accept 127.0.0.1:{port} -www -cert {current}/node.crt '
                f'-key {current}/node.key </dev/null >/dev/null 2>&1 & echo $! > "$HOME/door.pid"')
        subprocess.run(['sh', '-c', door], env=env, check=True)
        self.addCleanup(lambda: subprocess.run(['sh', '-c', 'kill $(cat "$HOME/door.pid")'], env=env))
        (self.home / 'bin/systemctl').write_text(
            f'#!/bin/sh\n[ "$1" = reload ] || exit 0\nenv -u FLEET_DOOR_LOCKED timeout 20 {sync} copy\nrc=$?\n'
            'echo "reload copy rc=$rc" >> "$HOME/scheduler.log"\n'
            f'kill $(cat "$HOME/door.pid"); sleep 0.5; {door}\nexit $rc\n')
        (self.home / 'bin/systemctl').chmod(0o700)
        use(renewed)
        r = subprocess.run([str(sync)], capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr + (self.home / 'scheduler.log').read_text())
        self.assertIn('reload copy rc=0', (self.home / 'scheduler.log').read_text())

    @unittest.skipUnless(OPENSSL3 and shutil.which('timeout'), 'needs OpenSSL 3.4 or later and timeout')
    def test_a_failed_reload_is_retried_by_the_next_sync(self):
        # After a failed reload, or a sync killed between swap and reload, the copy is already current.
        # The next run must compare what the door serves, not only the files.
        self.enroll()
        pki = Pki(self.home / 'pki')
        root = pki.ca('root', 'inter')
        self.pin(pki.fingerprint(root))
        first, renewed = pki.leaf('door', 'door', 'inter'), pki.leaf('door2', 'door', 'inter')
        env = dict(self.env, PATH=f'{Path(OPENSSL3).parent}:{self.env["PATH"]}')

        def use(identity):
            shutil.copy(identity[0], self.state / 'node.crt')
            shutil.copy(identity[1], self.state / 'node.key')
            shutil.copy(root, self.state / 'root_ca.crt')

        use(first)
        port = free_port()
        out, _ = self.render('--listen', f'127.0.0.1:{port}')
        sync = out / 'usr/local/libexec/fleet-door-sync'
        self.assertEqual(subprocess.run([str(sync), 'copy'], capture_output=True, env=env).returncode, 0)
        current = self.prefix / 'etc/fleet-door/tls/current'
        door = (f'{OPENSSL3} s_server -accept 127.0.0.1:{port} -www -cert {current}/node.crt '
                f'-key {current}/node.key </dev/null >/dev/null 2>&1 & echo $! > "$HOME/door.pid"')
        subprocess.run(['sh', '-c', door], env=env, check=True)
        self.addCleanup(lambda: subprocess.run(['sh', '-c', 'kill $(cat "$HOME/door.pid")'], env=env))
        (self.home / 'bin/systemctl').write_text(
            '#!/bin/sh\n[ "$1" = reload ] || exit 0\n[ -e "$HOME/reload-fails" ] && exit 1\n'
            'echo reloaded >> "$HOME/scheduler.log"\n'
            f'kill $(cat "$HOME/door.pid"); sleep 0.5; {door}\n')
        (self.home / 'bin/systemctl').chmod(0o700)
        log = self.home / 'scheduler.log'
        use(renewed)
        (self.home / 'reload-fails').touch()
        r = subprocess.run([str(sync)], capture_output=True, text=True, env=env, timeout=120)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('could not reload', r.stderr)
        (self.home / 'reload-fails').unlink()
        r = subprocess.run([str(sync)], capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(log.read_text().splitlines().count('reloaded'), 1)
        # A door that already serves the current certificate is not reloaded.
        r = subprocess.run([str(sync)], capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(log.read_text().splitlines().count('reloaded'), 1)
        # A door that does not answer is reported, not reloaded: a failed probe is not another serial.
        subprocess.run(['sh', '-c', 'kill $(cat "$HOME/door.pid")'], env=env)
        r = subprocess.run([str(sync)], capture_output=True, text=True, env=env, timeout=120)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('serves serial none', r.stderr)
        self.assertEqual(log.read_text().splitlines().count('reloaded'), 1)

    def test_install_writes_nothing_when_a_file_fails_to_render(self):
        # A cat that stops partway when it writes to a pipe stands in for a render that dies midway.
        self.enroll()
        env = self.install_stubs()
        real = shutil.which('cat')
        # Only the nginx.conf render fails; fleet uses cat elsewhere too.
        (self.home / 'bin/cat').write_text(
            f'#!/bin/sh\n[ $# -eq 0 ] || exec {real} "$@"\nt=$(mktemp)\n{real} > "$t"\n'
            'if [ -p /dev/stdout ] && grep -q worker_connections "$t"; then head -n 3 "$t"; rm -f "$t"; exit 1; fi\n'
            f'{real} "$t"\nrm -f "$t"\n')
        (self.home / 'bin/cat').chmod(0o700)
        result = self.fleet('door', 'install', **env)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.prefix / 'etc/fleet-door').is_dir(), result.stderr)
        self.assertFalse((self.prefix / 'etc/fleet-door/nginx.conf').exists())
        self.assertFalse((self.prefix / 'usr/local/libexec/fleet-door-sync').exists())

    def test_renew_warns_when_the_fleet_ca_differs_from_the_door(self):
        self.enroll()
        env = self.install_stubs()
        self.assertEqual(self.fleet('door', 'install', **env).returncode, 0)
        r = self.fleet('renew', **env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('fleet door', r.stderr)
        self.pin('cd' * 32)
        r = self.fleet('renew', **env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('fleet door install', r.stderr)
        self.assertIn('ab' * 32, r.stderr)


DOOR_IMAGE = os.environ.get('FLEET_DOOR_IMAGE')


@unittest.skipUnless(DOOR_IMAGE and OPENSSL3 and shutil.which('docker') and shutil.which('socat')
                     and sys.platform == 'linux', 'set FLEET_DOOR_IMAGE to an nginx image; needs docker, socat, '
                                                  'OpenSSL 3.4 or later, and Linux (127.0.0.0/8 sources)')
class DoorNginx(FleetBase):
    # The rendered door, run by the deployed nginx (an image of the target's nginx package), against
    # real fleet-shaped certificates. Sources 127.0.0.2 and up stand for distinct client addresses.
    DEADLINE = 2

    def setUp(self):
        super().setUp()
        self.enroll()
        pki = self.pki = Pki(self.home / 'pki')
        self.root = pki.ca('root', 'inter')
        self.door = pki.leaf('door', 'door', 'inter')
        self.client = pki.leaf('client', 'client', 'inter')
        self.install_identity(*self.door)
        self.backend = socket.socket()
        self.backend.bind(('127.0.0.1', 0))
        self.backend.listen(64)
        self.addCleanup(self.backend.close)
        threading.Thread(target=self.echo, daemon=True).start()
        self.port = free_port()
        self.prefix = self.home / 'root'
        self.env.update(FLEET_DOOR_PREFIX=str(self.prefix), PATH=f'{Path(OPENSSL3).parent}:{self.env["PATH"]}')
        config = self.home / '.config/fleet/config'
        config.write_text(re.sub(r'^fingerprint=.*$', f'fingerprint={pki.fingerprint(self.root)}',
                                 config.read_text(), flags=re.M))
        out = self.prefix
        r = self.fleet('door', 'render', str(out), '--listen', f'127.0.0.1:{self.port}',
                       '--to', f'127.0.0.1:{self.backend.getsockname()[1]}', '--per-source', '3', '--max', '6',
                       '--handshake-timeout', str(self.DEADLINE))
        self.assertEqual(r.returncode, 0, r.stderr)
        (self.prefix / 'run').mkdir()
        self.sync = self.prefix / 'usr/local/libexec/fleet-door-sync'
        r = subprocess.run([str(self.sync), 'copy'], capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.conf = self.prefix / 'etc/fleet-door/nginx.conf'
        self.name = f'fleet-door-test-{os.getpid()}-{self.port}'
        subprocess.run(['docker', 'run', '-d', '--rm', '--name', self.name, '--network', 'host',
                        '-v', f'{self.home}:{self.home}', DOOR_IMAGE, '/usr/sbin/nginx', '-c', str(self.conf),
                        '-g', 'daemon off;'], check=True, capture_output=True, timeout=60)
        self.addCleanup(subprocess.run, ['docker', 'rm', '-f', self.name], capture_output=True, timeout=60)
        # nginx runs as root in the container and writes the pid file; let the test remove it.
        self.addCleanup(subprocess.run, ['docker', 'exec', self.name, 'chmod', '-R', 'a+rwX', str(self.prefix)],
                        capture_output=True, timeout=60)
        (self.home / 'bin/systemctl').write_text(
            f'#!/bin/sh\n[ "$1" = reload ] && exec docker exec {self.name} /usr/sbin/nginx -c {self.conf} -s reload\n'
            'printf "systemctl %s\\n" "$*" >> "$HOME/scheduler.log"\n')
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                socket.create_connection(('127.0.0.1', self.port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.2)
        else:
            logs = subprocess.run(['docker', 'logs', self.name], capture_output=True, text=True).stderr
            self.fail(f'nginx did not listen: {logs}')

    def install_identity(self, chain, key, _serial):
        shutil.copy(chain, self.state / 'node.crt')
        shutil.copy(key, self.state / 'node.key')
        shutil.copy(self.root, self.state / 'root_ca.crt')

    def echo(self):
        while True:
            try:
                conn, _ = self.backend.accept()
            except OSError:
                return
            threading.Thread(target=self.echo_one, args=(conn,), daemon=True).start()

    @staticmethod
    def echo_one(conn):
        with conn:
            while data := conn.recv(4096):
                conn.sendall(data)

    def tls(self, identity=None, source='127.0.0.1', root=None, name='door'):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(root or self.root)
        if identity:
            ctx.load_cert_chain(identity[0], identity[1])
        raw = socket.create_connection(('127.0.0.1', self.port), timeout=5, source_address=(source, 0))
        return ctx.wrap_socket(raw, server_hostname=name)

    def echoes(self, identity=None, source='127.0.0.1'):
        # In TLS 1.3 the server checks the client certificate after the client's handshake ends, so
        # a rejection shows as an alert or a close on the first read.
        try:
            with self.tls(identity, source) as s:
                s.sendall(b'ping')
                return s.recv(4) == b'ping'
        except (OSError, ssl.SSLError):
            return False

    def slow(self, source, drip):
        # A client that never finishes the handshake: silent, or one ClientHello byte every 0.3 s.
        s = socket.create_connection(('127.0.0.1', self.port), timeout=10, source_address=(source, 0))
        result = {'start': time.time(), 'closed': None}

        def run():
            hello = bytes([0x16, 0x03, 0x01, 0x02, 0x00, 0x01, 0x00, 0x01, 0xfc, 0x03, 0x03]) + bytes(600)
            s.settimeout(0.3)
            for i in range(len(hello)):
                try:
                    if drip:
                        s.send(hello[i:i + 1])
                    if s.recv(100) == b'':
                        break
                except socket.timeout:
                    continue
                except OSError:
                    break
            result['closed'] = time.time() - result['start']
            s.close()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        result['thread'] = thread
        return result

    def served_serial(self):
        with self.tls(self.client) as s:
            return s.getpeercert()['serialNumber']

    def test_only_current_fleet_client_certificates_reach_the_service(self):
        pki = self.pki
        pki.ca('evil', 'evilinter')
        evil = pki.leaf('evilclient', 'client', 'evilinter')
        expired = pki.leaf('expired', 'client', 'inter',
                           validity=('-not_before', '20200101000000Z', '-not_after', '20200102000000Z'))
        server_only = pki.leaf('serveronly', 'client', 'inter', eku='serverAuth')
        self.assertTrue(self.echoes(self.client))
        for label, identity in [('no certificate', None), ('another CA', evil), ('expired', expired),
                                ('server use only', server_only)]:
            self.assertFalse(self.echoes(identity), label)
        old = subprocess.run([OPENSSL3, 's_client', '-connect', f'127.0.0.1:{self.port}', '-tls1_1', '-cipher',
                              'DEFAULT:@SECLEVEL=0', '-cert', str(self.client[0]), '-key', str(self.client[1])],
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(old.returncode, 0)
        self.assertIn('alert protocol version', old.stdout + old.stderr)

    def tunnel_client(self, peer):
        port = free_port()
        r = self.fleet('tunnel', 'add', 'bb', '--mode', 'client', '--listen', f'127.0.0.1:{port}',
                       '--to', f'127.0.0.1:{self.port}', '--peer', peer, FLEET_TUNNEL_NO_SERVICE='1')
        self.assertEqual(r.returncode, 0, r.stderr)
        proc = subprocess.Popen(['bash', str(SCRIPT), 'tunnel', 'run', 'bb'], env=self.env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait, 10)
        self.addCleanup(proc.terminate)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                s = socket.create_connection(('127.0.0.1', port), timeout=5)
                break
            except OSError:
                time.sleep(0.1)
        with s:
            s.sendall(b'ping')
            try:
                return s.recv(4) == b'ping'
            except OSError:
                return False

    def test_the_fleet_tunnel_client_checks_the_server_name_and_ca(self):
        self.assertTrue(self.tunnel_client('door'))
        self.assertFalse(self.tunnel_client('brev-x86-cpu'))
        # A door with a certificate from another CA, under the expected name.
        self.pki.ca('evil', 'evilinter')
        chain, key, _ = self.pki.leaf('evildoor', 'door', 'evilinter')
        tls = self.prefix / 'etc/fleet-door/tls/current'
        shutil.copy(chain, tls / 'node.crt')
        shutil.copy(key, tls / 'node.key')
        subprocess.run(['docker', 'exec', self.name, '/usr/sbin/nginx', '-c', str(self.conf), '-s', 'reload'],
                       check=True, capture_output=True, timeout=30)
        time.sleep(1)
        self.assertFalse(self.tunnel_client('door'))

    def test_slow_handshakes_end_at_the_deadline_and_capacity_returns(self):
        # Fill the per-source limit (3) from one address with clients that never finish a handshake.
        slow = [self.slow('127.0.0.2', drip) for drip in (False, True, True)]
        time.sleep(0.3)
        self.assertFalse(self.echoes(self.client, '127.0.0.2'))
        self.assertTrue(self.echoes(self.client, '127.0.0.3'))
        for s in slow:
            s['thread'].join(10)
            self.assertIsNotNone(s['closed'])
            self.assertGreater(s['closed'], self.DEADLINE - 0.5)
            self.assertLess(s['closed'], self.DEADLINE + 1.5)
        self.assertTrue(self.echoes(self.client, '127.0.0.2'))

    def test_the_global_limit_holds_across_sources_and_frees_at_the_deadline(self):
        slow = [self.slow(f'127.0.0.{n}', True) for n in (2, 2, 3, 3, 4, 4)]
        time.sleep(0.3)
        self.assertFalse(self.echoes(self.client, '127.0.0.5'))
        for s in slow:
            s['thread'].join(10)
            self.assertLess(s['closed'], self.DEADLINE + 1.5)
        self.assertTrue(self.echoes(self.client, '127.0.0.5'))

    def test_sync_checks_the_served_certificate_while_the_global_limit_is_full(self):
        # Established fleet connections, not slow ones: they hold the global limit (6) past any deadline.
        held = []
        for source in ('127.0.0.2', '127.0.0.2', '127.0.0.3', '127.0.0.3', '127.0.0.4', '127.0.0.4'):
            s = self.tls(self.client, source)
            s.sendall(b'ping')
            self.assertEqual(s.recv(4), b'ping')
            held.append(s)
        self.addCleanup(lambda: [s.close() for s in held])
        self.assertFalse(self.echoes(self.client, '127.0.0.5'))
        renewed = self.pki.leaf('door2', 'door', 'inter')
        self.install_identity(*renewed)
        r = subprocess.run([str(self.sync)], capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.served_serial(), renewed[2])

    def test_sync_reloads_the_served_certificate_and_fails_when_it_is_stale(self):
        self.assertEqual(self.served_serial(), self.door[2])
        renewed = self.pki.leaf('door2', 'door', 'inter')
        self.install_identity(*renewed)
        r = subprocess.run([str(self.sync)], capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.served_serial(), renewed[2])
        # Unchanged: no reload, and the served certificate still checks out.
        r = subprocess.run([str(self.sync)], capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual((r.returncode, r.stderr), (0, ''))
        soon = time.strftime('%Y%m%d%H%M%SZ', time.gmtime(time.time() + 7200))
        short = self.pki.leaf('door3', 'door', 'inter', validity=('-not_after', soon))
        self.install_identity(*short)
        r = subprocess.run([str(self.sync)], capture_output=True, text=True, env=self.env, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('expires within 8 hours', r.stderr)
        self.assertEqual(self.served_serial(), short[2])


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class TestEnvironment(unittest.TestCase):
    def test_inherited_sockets_never_reach_fleet(self):
        # A developer's shell holds the live agent, secrets, and SSH sockets. A test that removes "its"
        # socket must not find one of these through an inherited variable. Short paths: socket limits.
        with tempfile.TemporaryDirectory(dir='/tmp') as tmp:
            live = {name: Path(tmp) / name for name in ('FLEET_AGENT_SOCK', 'FLEET_SECRETS_SOCK', 'SSH_AUTH_SOCK')}
            for path in live.values():
                path.write_text('')
            env = dict(os.environ, **{name: str(path) for name, path in live.items()})
            result = subprocess.run([sys.executable, __file__, 'Fleet.test_linux_sockets_stay_under_the_test_home',
                                     'FleetEphemeral'], capture_output=True, text=True, env=env, timeout=240)
            for path in live.values():
                self.assertTrue(path.exists(), path)
            self.assertEqual(result.returncode, 0, result.stderr)


class GitconfigInclude(unittest.TestCase):
    def test_gitconfig_includes_the_fleet_fragment_last(self):
        for data in ['data:\n  name: T\n  email: t@example.com\n',
                     'data:\n  name: T\n  email: t@example.com\n  onepassword:\n    enabled: true\n'
                     '    ssh_agent: true\n    signing_public_key: "ssh-ed25519 AAAA"\n']:
            with tempfile.TemporaryDirectory() as tmp, self.subTest(data=data):
                config = Path(tmp) / 'chezmoi.yaml'
                config.write_text(data)
                text = subprocess.run(['chezmoi', '--config', str(config), '--source', str(ROOT), 'execute-template'],
                                      input=(ROOT / 'dot_gitconfig.tmpl').read_text(), capture_output=True, text=True,
                                      check=True, env=clean_env(HOME=tmp)).stdout
                includes = [l.strip() for l in text.splitlines() if l.strip().startswith('path =')]
                self.assertEqual(includes[-1], 'path = ~/.ssh/fleet/gitconfig')


class SshConfigInclude(unittest.TestCase):
    def test_modify_script_includes_fleet_config_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = home / 'chezmoi.yaml'
            config.write_text('data:\n  name: Test\n  email: test@example.com\n')
            env = clean_env(HOME=str(home))
            modifier = subprocess.run(['chezmoi', '--config', str(config), '--source', str(ROOT), 'execute-template'],
                                      input=(ROOT / 'private_dot_ssh/modify_private_config.tmpl').read_text(),
                                      capture_output=True, text=True, check=True, env=env).stdout
            # A config from before 2026-10-15 also includes ~/.ssh/provisioned/config. The modifier removes it.
            text = 'Include ~/.ssh/provisioned/config\nHost example\n    User me\n'
            for _ in range(2):
                text = subprocess.run(['bash', '-c', modifier], input=text, capture_output=True, text=True,
                                      check=True, env=env).stdout
            lines = text.splitlines()
            self.assertEqual(lines[0], 'Include ~/.ssh/fleet/config')
            self.assertEqual(text.count('Include ~/.ssh/fleet/config'), 1)
            self.assertNotIn('provisioned', text)
            self.assertIn('Host example\n    User me\n', text)

if __name__ == '__main__':
    unittest.main()

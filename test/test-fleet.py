import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
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
        # /run/user/UID without it. A test that inherited either would remove the host's live sockets.
        runtime = self.home / 'run'
        runtime.mkdir(mode=0o700)
        self.env = dict(os.environ, HOME=str(self.home), PATH=f'{bin_dir}:{os.environ["PATH"]}',
                        XDG_RUNTIME_DIR=str(runtime),
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
        self.assertIn('Host bb\n    HostName brev.example.ts.net\n    User ubuntu\n    StrictHostKeyChecking accept-new\n', text)
        self.assertNotIn('ProxyCommand', text)
        self.assertIn('Host gpu\n    HostName gpu.corp.example\n    User ryan\n    StrictHostKeyChecking accept-new\n    ProxyJump bb\n', text)
        self.assertNotIn('Host evil', text)
        self.assertIn('Host ip\n    HostName 100.64.0.7\n    User u\n    StrictHostKeyChecking accept-new\n    HostKeyAlias ip-host.example.ts.net\n', text)
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
        self.assertIn('Host bb\n    HostName h\n    User ubuntu\n    StrictHostKeyChecking accept-new\nHost *.ts.net ', config.read_text())
        curl.write_text('#!/bin/sh\ncase "$*" in *v1/ssh-hosts*) [ -f "$HOME/broker-down" ] && exit 7\n'
                        '  printf "bb brev.example.ts.net ubuntu\\n" ;; esac\n')
        self.assertEqual(self.fleet('renew').returncode, 0)
        # A broker outage keeps the last list instead of dropping the aliases.
        (self.home / 'broker-down').write_text('')
        self.assertEqual(self.fleet('renew').returncode, 0)
        self.assertIn('Host bb\n', config.read_text())

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
        self.addCleanup(lambda: sock.parent.exists() and __import__('shutil').rmtree(sock.parent))
        out = subprocess.run(['ssh-agent', '-a', str(sock)], capture_output=True, text=True, check=True).stdout
        pid = int(out.split('SSH_AGENT_PID=')[1].split(';')[0])
        self.addCleanup(lambda: subprocess.run(['kill', str(pid)]))
        for name in ['agent-signing', 'agent-auth']:
            key = self.home / name
            subprocess.run(['ssh-keygen', '-q', '-t', 'ecdsa', '-b', '256', '-N', '', '-C', f'fleet {name}',
                            '-f', str(key)], check=True)
            subprocess.run(['ssh-add', '-q', str(key)], env=dict(os.environ, SSH_AUTH_SOCK=str(sock)), check=True)
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
        genv = dict(os.environ, HOME=str(self.home), SSH_AUTH_SOCK='/nonexistent', FLEET_AGENT_SOCK=str(sock),
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
        labels = (['dev.fleet.renew', 'dev.fleet.agent', 'dev.fleet.tunnel.bb', 'dev.fleet.worker']
                  if os.uname().sysname == 'Darwin' else
                  ['disable --now fleet-renew.timer', 'disable --now fleet-agent.service',
                   'disable --now fleet-tunnel-bb.service', 'disable --now fleet-worker.service'])
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


# A stub of the actl CLI. keygen writes a private seed file the way actl does and prints a public
# key. submit, status, and workers play a controller whose state is $HOME/controller.json:
#   tasks          task ID -> {"task": document, "polls": n}
#   offline        worker IDs whose tasks stay queued
#   fail           worker IDs whose tasks fail
#   lose_admitted  worker IDs whose next submit is admitted but its reply is lost
#   lose_dropped   worker IDs whose next submit is neither admitted nor answered
#   expired        true: a resend of an unknown task is refused, as for an expired request
#   down           true: every call fails as if NATS were unreachable
#   workers        the worker IDs that `actl workers` lists
ACTL_STUB = r'''#!/usr/bin/env python3
import json, os, sys, time, uuid
from pathlib import Path
home = Path(os.environ['HOME'])
a = sys.argv[1:]
with open(home / 'actl.log', 'a') as f:
    f.write(json.dumps(a) + '\n')
def opt(name):
    return a[a.index(name) + 1] if name in a else None
if a == ['--version']:
    print('actl ' + os.environ.get('ACTL_STUB_VERSION', '0.2.0'))
    sys.exit(0)
words = [w for i, w in enumerate(a) if not w.startswith('--') and (i == 0 or a[i - 1] not in
         ('--config', '--seed-file', '--repo', '--commit', '--type', '--requires', '--save-task', '--task-file',
          '--runtime', '--attempts'))]
cmd = words[0] if words else ''
if cmd == 'keygen':
    path = Path(opt('--seed-file'))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, b'SUAPRIVATESEEDSENTINEL\n')
    os.close(fd)
    n = len(list(home.glob('.keygen-*')))
    (home / f'.keygen-{n}').write_text('')
    print(os.environ.get('ACTL_PUBKEY', 'U' + 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'[n] * 55))
    sys.exit(0)
if cmd == 'doctor':
    print('configuration and credential OK')
    sys.exit(0)
state_path = home / 'controller.json'
state = json.loads(state_path.read_text()) if state_path.exists() else {}
state.setdefault('tasks', {})
def save():
    state_path.write_text(json.dumps(state))
def host(doc):
    return doc['required_capabilities'][0][len('host-'):]
def rejected(code):
    sys.stderr.write(f'Error: controller rejected request\n\nCaused by:\n    {code}\n')
    sys.exit(1)
if state.get('down'):
    sys.stderr.write('Error: NATS connection failed\n')
    sys.exit(1)
if cmd == 'submit':
    if opt('--task-file'):
        doc = json.loads(Path(opt('--task-file')).read_text())
    else:
        doc = {'protocol_version': 1, 'id': str(uuid.uuid4()), 'correlation_id': str(uuid.uuid4()),
               'parent_task_id': None, 'repository': opt('--repo'), 'commit': opt('--commit'),
               'task_type': opt('--type'), 'context_refs': [], 'required_capabilities': [opt('--requires')],
               'policy': {'runtime_seconds': 300, 'max_attempts': 2}, 'created_at': int(time.time())}
    if opt('--save-task'):
        fd = os.open(opt('--save-task'), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        os.write(fd, json.dumps(doc, indent=2).encode())
        os.close(fd)
    h = host(doc)
    known = doc['id'] in state['tasks']
    if not known and h in state.get('lose_dropped', []):
        state['lose_dropped'].remove(h)
        save()
        sys.stderr.write('Error: request timed out\n')
        sys.exit(1)
    if not known and opt('--task-file') and state.get('expired'):
        rejected('expired')
    if not known:
        state['tasks'][doc['id']] = {'task': doc, 'polls': 0}
    if h in state.get('lose_admitted', []):
        state['lose_admitted'].remove(h)
        save()
        sys.stderr.write('Error: request timed out\n')
        sys.exit(1)
    save()
    print(json.dumps({'response': 'task', 'record': {'task': doc, 'state': {'status': 'queued'}, 'attempts': 0,
                                                     'updated_at': 0}}))
    sys.exit(0)
if cmd == 'status':
    entry = state['tasks'].get(words[1])
    if entry is None:
        rejected('not_found')
    entry['polls'] += 1
    h = host(entry['task'])
    if h in state.get('offline', []):
        st = {'status': 'queued'}
    elif entry['polls'] < 2:
        st = {'status': 'running', 'lease': {}}
    elif h in state.get('fail', []):
        st = {'status': 'failed', 'reason': 'execution', 'result': {'exit_code': 1, 'stdout': {}, 'stderr': {},
                                                                    'truncated': False, 'commit': None}}
    else:
        st = {'status': 'completed', 'result': {'exit_code': 0, 'stdout': {}, 'stderr': {}, 'truncated': False,
                                                'commit': entry['task']['commit']}}
    save()
    print(json.dumps({'response': 'inspection', 'record': {'task': entry['task'], 'state': st, 'attempts': 1,
                                                           'updated_at': 0},
                      'events': [], 'next': None, 'eligible_workers': 0 if h in state.get('offline', []) else 1}))
    sys.exit(0)
if cmd == 'workers':
    print(json.dumps({'response': 'workers', 'next': None, 'workers': [
        {'worker': {'worker_id': w, 'platform': 'linux', 'architecture': 'aarch64', 'version': '0.2.0',
                    'capabilities': [], 'repositories': [], 'task_types': []},
         'enrollment': {}, 'last_seen': 0, 'roots': []} for w in state.get('workers', [])]}))
    sys.exit(0)
sys.exit('unexpected actl ' + ' '.join(a))
'''

# A stub of systemctl --user for the worker service. $HOME/svc.json holds whether the service is
# active and its main PID. When the drain file exists, a read of MainPID plays the worker:
# DRAIN_MODE=ack writes the acknowledgement and exits, exit exits without one, fail exits and
# marks the unit failed, other acknowledges with another PID, hang keeps running.
SYSTEMCTL_STUB = r'''#!/usr/bin/env python3
import json, os, sys, time, hashlib
from pathlib import Path
home = Path(os.environ['HOME'])
a = sys.argv[1:]
root = home / '.local/state/fleet/actl-worker'
state_path = home / 'svc.json'
state = json.loads(state_path.read_text()) if state_path.exists() else {'active': False, 'pid': 0, 'next': 4000,
                                                                          'failed': False}
def save():
    state_path.write_text(json.dumps(state))
def binary_sum():
    for d in (home / '.local/bin', home / '.local/aarch64/bin'):
        if (d / 'actl').exists():
            return hashlib.sha256((d / 'actl').read_bytes()).hexdigest()[:12]
    return 'none'
with open(home / 'scheduler.log', 'a') as f:
    f.write('systemctl ' + ' '.join(a) + (f' actl={binary_sum()}' if 'stop' in a else '') + '\n')
args = [x for x in a if x not in ('--user', '--quiet', '--value', '--no-pager')]
def start():
    state.update(active=True, pid=state['next'], failed=False)
    state['next'] += 1
if args[:1] == ['show-environment']:
    sys.exit(0)
if args[:2] == ['show', '-p'] and args[2] == 'MainPID':
    mode = os.environ.get('DRAIN_MODE', 'ack')
    if state['active'] and (root / 'drain').exists() and mode != 'hang':
        if mode in ('ack', 'other'):
            pid = state['pid'] + (1 if mode == 'other' else 0)
            (root / 'drained').write_text(json.dumps({'pid': pid, 'drained_at': int(time.time())}))
        state.update(active=False, pid=0, failed=(mode == 'fail'))
    save()
    print(state['pid'] if state['active'] else 0)
    sys.exit(0)
if args[:1] == ['is-active']:
    sys.exit(0 if state['active'] else 3)
if args[:1] == ['is-failed']:
    sys.exit(0 if state['failed'] else 1)
if args[:1] == ['start'] or args[:2] == ['enable', '--now'] or args[:1] == ['restart']:
    if not state['active'] or args[:1] == ['restart']:
        start()
elif args[:1] == ['stop']:
    state.update(active=False, pid=0)
save()
sys.exit(0)
'''

# A stub of launchctl for the macOS worker agent, with the same state file and drain behavior.
LAUNCHCTL_STUB = r'''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
home = Path(os.environ['HOME'])
a = sys.argv[1:]
root = home / '.local/state/fleet/actl-worker'
state_path = home / 'svc.json'
state = json.loads(state_path.read_text()) if state_path.exists() else {'active': False, 'pid': 0, 'next': 4000,
                                                                          'loaded': False}
def save():
    state_path.write_text(json.dumps(state))
with open(home / 'scheduler.log', 'a') as f:
    f.write('launchctl ' + ' '.join(a) + '\n')
worker = a[1:2] == [f'gui/{os.getuid()}/dev.fleet.worker'] or (len(a) > 2 and a[2].endswith('dev.fleet.worker.plist'))
if not worker:
    sys.exit(0)
if a[0] == 'print':
    if not state.get('loaded'):
        sys.exit(113)
    if state['active'] and (root / 'drain').exists() and os.environ.get('DRAIN_MODE', 'ack') == 'ack':
        (root / 'drained').write_text(json.dumps({'pid': state['pid'], 'drained_at': int(time.time())}))
        state.update(active=False, pid=0)
    save()
    print('gui/501/dev.fleet.worker = {\n\tstate = ' + ('running' if state['active'] else 'not running'))
    if state['active']:
        print(f"\tpid = {state['pid']}")
    print('}')
    sys.exit(0)
if a[0] == 'bootstrap':
    state.update(loaded=True, active=True, pid=state['next'])
    state['next'] += 1
elif a[0] == 'bootout':
    state.update(loaded=False, active=False, pid=0)
elif a[0] == 'kickstart':
    state.update(active=True, pid=state['next'])
    state['next'] += 1
save()
'''

# The pinned actl is the stub itself, so install can run keygen and status can run doctor with it.
ACTL_BODY = ACTL_STUB.encode()
NEW_ACTL_BODY = ACTL_STUB.encode() + b'# 0.3.0\n'
SUPERVISOR_BODY = b'\x7fELF fake actl-supervisor 0.2.0'


def pinned_script(home, actl=ACTL_BODY, supervisor=SUPERVISOR_BODY, version='0.2.0'):
    """A copy of fleet with the actl pins that artifacts/release would add after the last checksum line."""
    text = SCRIPT.read_text()
    lines = [f'ACTL_VERSION={version}']
    for target in ('darwin_arm64', 'linux_aarch64', 'linux_x86_64'):
        lines.append(f'ACTL_SHA256_{target}={hashlib.sha256(actl).hexdigest()}')
        lines.append(f'ACTL_SUPERVISOR_SHA256_{target}={hashlib.sha256(supervisor).hexdigest()}')
    last = [m for m in re.finditer(r'^[A-Z_]+_SHA256_[a-z0-9_]+=\S+\n', text, re.M)][-1]
    text = text[:last.end()] + '\n'.join(lines) + '\n' + text[last.end():]
    script = home / 'fleet-pinned'
    script.write_text(text)
    return script


class ActlBase(FleetBase):
    SERVER = 'tls://ctl.example.ts.net:4222'
    CA = '-----BEGIN CERTIFICATE-----\nMIIBfakeCA==\n-----END CERTIFICATE-----\n'

    def setUp(self):
        super().setUp()
        self.bin = self.home / 'bin'
        for name, body in [('actl', ACTL_STUB), ('systemctl', SYSTEMCTL_STUB), ('launchctl', LAUNCHCTL_STUB),
                           ('journalctl', '#!/bin/sh\necho "worker log: teardown was not confirmed"\n'),
                           ('uname', '#!/bin/sh\ncase "$1" in -m) echo "${FAKE_ARCH:-aarch64}" ;; '
                                     '*) echo "${FAKE_OS:-Linux}" ;; esac\n'),
                           ('curl', '#!/bin/sh\nprintf "%s\\n" "$*" >> "$HOME/curl.log"\nout=""; url=""\n'
                                    'while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift 2 ;; '
                                    'https://*) url="$1"; shift ;; *) shift ;; esac; done\n'
                                    'case "$url" in\n'
                                    '  */v1/actl) [ -f "$HOME/actl.json" ] || exit 22; cat "$HOME/actl.json" ;;\n'
                                    '  */v1/artifacts/actl-supervisor/*) cp "$HOME/supervisor-body" "$out" ;;\n'
                                    '  */v1/artifacts/actl/*) cp "$HOME/actl-body" "$out" ;;\n'
                                    '  *) exit 22 ;;\nesac\n')]:
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o700)
        (self.home / 'actl-body').write_bytes(ACTL_BODY)
        (self.home / 'supervisor-body').write_bytes(SUPERVISOR_BODY)
        self.publish()
        self.script = pinned_script(self.home)
        self.user = __import__('pwd').getpwuid(os.getuid()).pw_name
        self.config_dir = self.home / '.config/fleet/actl'
        self.root = self.state / 'actl-worker'
        self.seed = self.state / 'actl/worker.seed'

    def publish(self, workers=()):
        (self.home / 'actl.json').write_text(json.dumps({
            'server': self.SERVER, 'ca_pem': self.CA, 'workers': [{'id': i, 'os': o} for i, o in workers]}))

    def enroll_as(self, name):
        result = self.fleet('enroll', name, '--ca-url', 'https://ca.example', '--fingerprint', 'ab' * 32,
                            '--no-trust', stdin=TOKEN + '\n')
        self.assertEqual(result.returncode, 0, result.stderr)

    def run_script(self, *args, script=None, **env):
        return subprocess.run(['bash', str(script or self.script), *args], capture_output=True, text=True,
                              env=dict(self.env, **env))

    def svc(self):
        path = self.home / 'svc.json'
        return json.loads(path.read_text()) if path.exists() else {'active': False, 'pid': 0}

    def set_svc(self, **values):
        state = dict({'active': False, 'pid': 0, 'next': 4000, 'failed': False, 'loaded': False}, **self.svc())
        state.update(values)
        (self.home / 'svc.json').write_text(json.dumps(state))

    def scheduler(self):
        path = self.home / 'scheduler.log'
        return path.read_text().splitlines() if path.exists() else []

    def downloads(self):
        log = self.home / 'curl.log'
        return [l for l in log.read_text().splitlines() if '/v1/artifacts/' in l] if log.exists() else []


class WorkerInstall(ActlBase):
    def setUp(self):
        super().setUp()
        self.enroll_as('node-1')

    def install(self, *args, **env):
        return self.run_script('worker', 'install', *args, **env)

    def executor_args(self, toml, name):
        block = toml.split(f'[worker.executors.{name}]\n')[1].split('\n\n')[0]
        line = next(l for l in block.splitlines() if l.startswith('args = '))
        return json.loads(line[len('args = '):])

    def test_linux_install_renders_the_worker_and_prints_only_the_public_key(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        key = 'U' + 'A' * 55
        self.assertEqual(result.stdout, f'node-1=linux:{key}\n')
        self.assertNotIn('SUAPRIVATESEEDSENTINEL', result.stdout + result.stderr)
        bindir = self.home / '.local/bin'
        self.assertEqual((bindir / 'actl').read_bytes(), ACTL_BODY)
        self.assertEqual((bindir / 'actl-supervisor').read_bytes(), SUPERVISOR_BODY)
        self.assertEqual(self.mode(bindir / 'actl'), 0o755)
        self.assertEqual(self.mode(self.seed), 0o600)
        self.assertEqual(self.mode(self.seed.parent), 0o700)
        self.assertEqual((self.config_dir / 'ca.pem').read_text(), self.CA)
        toml = (self.config_dir / 'worker.toml').read_text()
        h = str(self.home)
        for line in ['[connection]', f'servers = ["{self.SERVER}"]', f'ca_certificate = "{h}/.config/fleet/actl/ca.pem"',
                     f'seed_file = "{h}/.local/state/fleet/actl/worker.seed"', 'principal = "worker.node-1"',
                     '[worker]', f'root = "{h}/.local/state/fleet/actl-worker"', 'git_binary = "/usr/bin/git"',
                     f'supervisor_binary = "{h}/.local/bin/actl-supervisor"', 'max_disk_bytes = 10737418240',
                     'retain_attempts = 100', 'capabilities = ["host-node-1", "os-linux"]',
                     '[worker.repositories]', 'dotfiles = "https://github.com/ryanolson/dynamo-dotfiles.git"']:
            self.assertIn(line + '\n', toml)
        commands = {'fleet-apply': ['executor', 'apply'], 'pylon-install': ['executor', 'pylon-install'],
                    'secrets-flush': ['secret', 'flush'], 'fleet-probe': ['executor', 'probe']}
        uid = os.getuid()
        for name, command in commands.items():
            block = toml.split(f'[worker.executors.{name}]\n')[1]
            self.assertTrue(block.startswith('kind = "command"\nprogram = "/usr/bin/env"\n'), block)
            args = self.executor_args(toml, name)
            self.assertEqual(args[:8], ['-u', 'GIT_CONFIG_GLOBAL', '-u', 'GIT_CONFIG_NOSYSTEM', '-u',
                                        'GIT_TERMINAL_PROMPT', '-u', 'GIT_LFS_SKIP_SMUDGE'])
            env = dict(x.split('=', 1) for x in args[8:-2 - len(command)])
            self.assertEqual(env['HOME'], h)
            self.assertEqual((env['USER'], env['LOGNAME']), (self.user, self.user))
            self.assertEqual(env['XDG_CONFIG_HOME'], f'{h}/.config')
            self.assertEqual(env['XDG_STATE_HOME'], f'{h}/.local/state')
            # The runtime folder of the shell that ran install: /run/user/UID on a real machine, the
            # isolated folder here, so a test worker's executors never reach the host's sockets.
            self.assertEqual(env['XDG_RUNTIME_DIR'], self.env['XDG_RUNTIME_DIR'])
            self.assertEqual(env['LANG'], 'C.UTF-8')
            path = env['PATH'].split(':')
            self.assertEqual(path[0], f'{h}/.local/bin')
            self.assertTrue(all(p.startswith('/') for p in path), path)
            self.assertIn('/usr/bin', path)
            self.assertEqual(args[-2 - len(command):], ['/bin/bash', 'dot_local/bin/executable_fleet', *command])
            # systemctl --user finds the user manager through XDG_RUNTIME_DIR alone (systemd 255,
            # proved on spark-d with a real worker), so no D-Bus address is rendered.
            self.assertNotIn('DBUS_SESSION_BUS_ADDRESS', ' '.join(args))
        unit = (self.home / '.config/systemd/user/fleet-worker.service').read_text()
        self.assertIn(f'ExecStart={h}/.local/bin/actl --config {h}/.config/fleet/actl/worker.toml worker\n', unit)
        for line in ['Restart=always', 'RestartSec=30', 'TimeoutStopSec=90', 'WantedBy=default.target']:
            self.assertIn(line + '\n', unit)
        log = self.scheduler()
        self.assertIn('systemctl --user enable fleet-worker.service', log)
        self.assertTrue(self.svc()['active'])
        self.assertTrue(any(l.startswith('loginctl ') for l in log), log)
        (dl_actl, dl_sup) = sorted(self.downloads(), key=lambda l: 'supervisor' in l)
        self.assertIn('https://ca.example:8443/v1/artifacts/actl/0.2.0/linux-aarch64', dl_actl)
        self.assertIn('https://ca.example:8443/v1/artifacts/actl-supervisor/0.2.0/linux-aarch64', dl_sup)
        self.assertIn(f'--cert {self.state}/node.crt', dl_actl)

    def test_install_without_a_runtime_folder_renders_the_user_runtime_folder(self):
        # A Tailscale SSH session has no XDG_RUNTIME_DIR. Every command here is a stub.
        env = dict(self.env)
        del env['XDG_RUNTIME_DIR']
        result = subprocess.run(['bash', str(self.script), 'worker', 'install'], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.executor_args((self.config_dir / 'worker.toml').read_text(), 'pylon-install')
        self.assertIn(f'XDG_RUNTIME_DIR=/run/user/{os.getuid()}', args)

    def test_macos_install_renders_a_launchd_agent(self):
        result = self.install(FAKE_OS='Darwin', FAKE_ARCH='arm64')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith('node-1=macos:U'), result.stdout)
        toml = (self.config_dir / 'worker.toml').read_text()
        self.assertIn('capabilities = ["host-node-1", "os-macos"]\n', toml)
        args = self.executor_args(toml, 'secrets-flush')
        self.assertFalse([x for x in args if x.startswith('XDG_RUNTIME_DIR=')])
        self.assertIn('/opt/homebrew/bin', dict(x.split('=', 1) for x in args if '=' in x)['PATH'].split(':'))
        plist = (self.home / 'Library/LaunchAgents/dev.fleet.worker.plist').read_text()
        h = str(self.home)
        for part in ['<string>dev.fleet.worker</string>', f'<string>{h}/.local/bin/actl</string><string>--config</string>'
                     f'<string>{h}/.config/fleet/actl/worker.toml</string><string>worker</string>',
                     '<key>ExitTimeOut</key><integer>60</integer>', '<key>RunAtLoad</key><true/>',
                     '<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>',
                     f'{h}/Library/Logs/fleet-worker.log']:
            self.assertIn(part, plist)
        self.assertIn(f'launchctl bootstrap gui/{os.getuid()} {h}/Library/LaunchAgents/dev.fleet.worker.plist',
                      self.scheduler())
        self.assertTrue(self.svc()['active'])

    def test_unchanged_install_changes_nothing_and_starts_a_stopped_service(self):
        self.assertEqual(self.install().returncode, 0)
        files = [self.home / '.local/bin/actl', self.config_dir / 'worker.toml', self.config_dir / 'ca.pem',
                 self.home / '.config/systemd/user/fleet-worker.service', self.seed]
        before = {f: (f.stat().st_mtime_ns, f.read_bytes()) for f in files}
        log_len, pid = len(self.scheduler()), self.svc()['pid']
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f'node-1=linux:U{"A" * 55}\n')
        self.assertEqual({f: (f.stat().st_mtime_ns, f.read_bytes()) for f in files}, before)
        self.assertEqual(len(self.downloads()), 2)
        self.assertEqual(self.svc()['pid'], pid)
        self.assertFalse([l for l in self.scheduler()[log_len:] if ' stop' in l or ' start' in l or 'restart' in l])
        self.assertFalse((self.root / 'drain').exists())
        self.set_svc(active=False, pid=0)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.svc()['active'])
        self.assertEqual({f: (f.stat().st_mtime_ns, f.read_bytes()) for f in files}, before)

    def test_an_unsafe_existing_seed_stops_install(self):
        def reset():
            if self.seed.parent.exists():
                __import__('shutil').rmtree(self.seed.parent)
            self.seed.parent.mkdir(mode=0o700, parents=True)
        cases = {}
        reset()
        self.seed.write_text('SUAOTHER\n'); self.seed.chmod(0o644)
        cases['group-readable seed'] = self.install()
        reset()
        target = self.home / 'elsewhere.seed'; target.write_text('SUAOTHER\n'); target.chmod(0o600)
        self.seed.symlink_to(target)
        cases['symlinked seed'] = self.install()
        reset()
        self.seed.write_text('SUAOTHER\n'); self.seed.chmod(0o600); self.seed.parent.chmod(0o755)
        cases['shared directory'] = self.install()
        reset()
        self.seed.write_text('SUAOTHER\n'); self.seed.chmod(0o600)
        os.link(self.seed, self.home / 'second-link')
        cases['second hard link'] = self.install()
        for label, result in cases.items():
            self.assertNotEqual(result.returncode, 0, label)
            self.assertIn('seed', result.stderr, label)
            self.assertNotIn('SUAOTHER', result.stdout + result.stderr, label)
        self.assertFalse(list(self.home.glob('.keygen-*')))
        self.assertFalse((self.config_dir / 'worker.toml').exists())

    def test_update_drains_the_worker_before_it_replaces_anything(self):
        self.assertEqual(self.install().returncode, 0)
        old = hashlib.sha256(ACTL_BODY).hexdigest()[:12]
        new_body = NEW_ACTL_BODY
        (self.home / 'actl-body').write_bytes(new_body)
        script = pinned_script(self.home, actl=new_body, version='0.3.0')
        log_len, pid = len(self.scheduler()), self.svc()['pid']
        result = self.run_script('worker', 'install', script=script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / '.local/bin/actl').read_bytes(), new_body)
        log = self.scheduler()[log_len:]
        stop = next(i for i, l in enumerate(log) if l.startswith('systemctl --user stop fleet-worker.service'))
        self.assertIn(f'actl={old}', log[stop])
        self.assertTrue(any('MainPID' in l for l in log[:stop]))
        self.assertTrue(any('start fleet-worker.service' in l for l in log[stop:]))
        self.assertFalse((self.root / 'drain').exists())
        self.assertFalse((self.root / 'drained').exists())
        self.assertNotEqual(self.svc()['pid'], pid)
        self.assertTrue(self.svc()['active'])

    def test_update_stops_when_the_worker_exits_without_an_acknowledgement(self):
        self.assertEqual(self.install().returncode, 0)
        new_body = NEW_ACTL_BODY
        (self.home / 'actl-body').write_bytes(new_body)
        script = pinned_script(self.home, actl=new_body, version='0.3.0')
        for mode in ['exit', 'fail', 'other']:
            with self.subTest(mode):
                self.set_svc(active=True, pid=4100, failed=False)
                (self.root / 'drain').unlink(missing_ok=True)
                log_len = len(self.scheduler())
                result = self.run_script('worker', 'install', script=script, DRAIN_MODE=mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('without a drain acknowledgement', result.stderr)
                self.assertIn('worker log: teardown was not confirmed', result.stderr)
                self.assertEqual((self.home / '.local/bin/actl').read_bytes(), ACTL_BODY)
                self.assertTrue((self.root / 'drain').exists())
                self.assertFalse([l for l in self.scheduler()[log_len:] if ' start' in l])

    def test_update_that_times_out_removes_the_drain_file(self):
        self.assertEqual(self.install().returncode, 0)
        new_body = NEW_ACTL_BODY
        (self.home / 'actl-body').write_bytes(new_body)
        script = pinned_script(self.home, actl=new_body, version='0.3.0')
        result = self.run_script('worker', 'install', '--timeout', '2', script=script, DRAIN_MODE='hang')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('did not drain within 2 seconds', result.stderr)
        self.assertFalse((self.root / 'drain').exists())
        self.assertEqual((self.home / '.local/bin/actl').read_bytes(), ACTL_BODY)
        self.assertTrue(self.svc()['active'])

    def test_both_checksums_are_checked_before_anything_is_replaced(self):
        self.assertEqual(self.install().returncode, 0)
        new_actl, new_sup = NEW_ACTL_BODY, b'\x7fELF fake actl-supervisor 0.3.0'
        (self.home / 'actl-body').write_bytes(new_actl)
        (self.home / 'supervisor-body').write_bytes(b'tampered')
        script = pinned_script(self.home, actl=new_actl, supervisor=new_sup, version='0.3.0')
        log_len = len(self.scheduler())
        result = self.run_script('worker', 'install', script=script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('checksum', result.stderr)
        self.assertEqual((self.home / '.local/bin/actl').read_bytes(), ACTL_BODY)
        self.assertEqual((self.home / '.local/bin/actl-supervisor').read_bytes(), SUPERVISOR_BODY)
        self.assertFalse((self.root / 'drain').exists())
        self.assertFalse([l for l in self.scheduler()[log_len:] if 'stop' in l or 'MainPID' in l])
        self.assertEqual(list((self.home / '.local/bin').glob('.fleet.*')), [])

    def test_install_fails_clearly_without_the_actl_pins(self):
        result = self.fleet('worker', 'install')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('actl is not pinned', result.stderr)
        self.assertFalse(self.downloads())
        self.assertFalse((self.home / '.local/bin/actl').exists())

    def test_install_refuses_an_unsafe_exit_marker(self):
        self.assertEqual(self.install().returncode, 0)
        (self.root).mkdir(parents=True, exist_ok=True)
        (self.root / 'unsafe-exit').write_text('{}')
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unsafe-exit', result.stderr)

    def test_install_needs_published_connection_data_and_a_valid_worker_id(self):
        (self.home / 'actl.json').unlink()
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('/v1/actl', result.stderr)
        self.publish()
        self.enroll_as('node-1.fleet')
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('worker ID', result.stderr)
        self.assertFalse(list(self.home.glob('.keygen-*')))

    def test_nosudo_host_installs_the_pair_into_the_arch_folder(self):
        (self.home / '.local/aarch64').mkdir(parents=True)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        arch_bin = self.home / '.local/aarch64/bin'
        self.assertEqual((arch_bin / 'actl').read_bytes(), ACTL_BODY)
        self.assertIn(f'supervisor_binary = "{arch_bin}/actl-supervisor"\n',
                      (self.config_dir / 'worker.toml').read_text())
        self.assertFalse((self.home / '.local/bin/actl').exists())

    def test_worker_status_reports_the_service_versions_and_connection(self):
        self.assertEqual(self.install().returncode, 0)
        result = self.run_script('worker', 'status')
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn('worker: node-1 (linux)\n', out)
        self.assertIn(f'service: active, pid {self.svc()["pid"]}\n', out)
        self.assertIn('pinned: actl 0.2.0\n', out)
        self.assertIn('installed: actl 0.2.0, matches the pin\n', out)
        self.assertIn(f'connection: {self.SERVER}', out)
        self.assertIn('active attempts: 0\n', out)
        self.assertIn('drain: none\n', out)

    def test_executor_commands(self):
        result = self.fleet('executor', 'probe', ACTL_TASK_ID='0190-test')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('fleet-probe: node-1 task 0190-test', result.stdout)
        outside = self.home / 'not-a-checkout'
        outside.mkdir()
        for command in ['apply', 'pylon-install']:
            result = subprocess.run(['bash', str(SCRIPT), 'executor', command], capture_output=True, text=True,
                                    env=self.env, cwd=outside)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('checkout of the dotfiles', result.stderr)
            self.assertFalse((self.state / 'applied').exists())


# Stubs for the executors (task 029 part 5). curl plays the broker and, for --unix-socket requests,
# a pylon that answers only after a restart ($HOME/pylon-running), and only from the STATUS_AFTER-th
# poll on. systemctl and launchctl restart that pylon. ssh-add lists $HOME/agent-keys.
EXECUTOR_CURL = r'''#!/usr/bin/env python3
import json, os, shutil, sys
from pathlib import Path
home = Path(os.environ['HOME'])
a = sys.argv[1:]
with open(home / 'curl.log', 'a') as f:
    f.write(json.dumps(a) + '\n')
def opt(name):
    return a[a.index(name) + 1] if name in a else None
url = next(x for x in a if x.startswith('http'))
if opt('--unix-socket'):
    with open(home / 'sockets.log', 'a') as f:
        f.write(opt('--unix-socket') + ' ' + url + '\n')
    if not (home / 'pylon-running').exists():
        sys.exit(7)
    if url.endswith('/v1/status'):
        polls = home / 'status-polls'
        n = int(polls.read_text()) + 1 if polls.exists() else 1
        polls.write_text(str(n))
        if n < int(os.environ.get('STATUS_AFTER', '1')):
            sys.exit(7)
        print((home / 'pylon-status.json').read_text())
        sys.exit(0)
    if url.endswith('/v1/flush'):
        print('{"flushed": 2}')
        sys.exit(0)
    if '/v1/secrets?ref=' in url:
        value = (home / 'selftest').read_text() if (home / 'selftest').exists() else 'hf_value'
        if value == 'refused':
            sys.exit(22)
        sys.stdout.write(value)
        sys.exit(0)
    sys.exit(22)
if '/v1/artifacts/pylon/' in url:
    shutil.copy(home / 'body', opt('-o'))
    sys.exit(0)
sys.exit(22)
'''
EXECUTOR_RESTART = r'''#!/bin/sh
printf '%s %s\n' "$(basename "$0")" "$*" >> "$HOME/scheduler.log"
case "$*" in
  *"restart fleet-agent.service"*|*"kickstart -k gui/"*/dev.fleet.agent)
    [ -e "$HOME/restart-fails" ] && exit 5
    rm -f "$HOME/status-polls"; : > "$HOME/pylon-running" ;;
esac
exit 0
'''
SSH_ADD_STUB = r'''#!/bin/sh
printf '%s %s\n' "$SSH_AUTH_SOCK" "$*" >> "$HOME/ssh-add.log"
if [ -f "$HOME/agent-hangs" ]; then echo $$ > "$HOME/ssh-add.pid"; exec sleep 60; fi
[ -f "$HOME/agent-keys" ] || { echo 'The agent has no identities.'; exit 1; }
cat "$HOME/agent-keys"
'''


class Executors(FleetBase):
    """fleet executor apply and pylon-install, run as the worker runs them: from a depth-1 checkout of
    a dotfiles commit, with that commit's fleet script."""
    BODY = b'\x7fELF fake pylon 0.2.0'

    def setUp(self):
        super().setUp()
        for name, body in [('curl', EXECUTOR_CURL), ('systemctl', EXECUTOR_RESTART), ('launchctl', EXECUTOR_RESTART),
                           ('ssh-add', SSH_ADD_STUB),
                           ('uname', '#!/bin/sh\ncase "$1" in -m) echo "${FAKE_ARCH:-aarch64}" ;; '
                                     '*) echo "${FAKE_OS:-Linux}" ;; esac\n')]:
            (self.home / 'bin' / name).write_text(body)
            (self.home / 'bin' / name).chmod(0o700)
        (self.home / 'body').write_bytes(self.BODY)
        self.enroll()
        self.origin = self.home / 'dotfiles'
        self.git(self.origin.parent, 'init', '-q', '-b', 'main', str(self.origin))
        self.checkouts = 0
        self.applied = self.state / 'applied'
        self.set_status('0.2.0')
        (self.home / 'agent-keys').write_text('ssh-ed25519 AAAAC3Nza fleet agent-signing\n'
                                              'ssh-ed25519 AAAAC3Nzb fleet agent-auth\n')

    def git(self, cwd, *args, **env):
        return subprocess.run(['git', '-c', 'user.name=T', '-c', 'user.email=t@example.com', *args], cwd=cwd,
                              check=True, capture_output=True, text=True, env=dict(self.env, **env)).stdout.strip()

    def set_status(self, version):
        status = {'entries': 0, 'ttl': 300}
        if version is not None:
            status['version'] = version
        (self.home / 'pylon-status.json').write_text(json.dumps(status))

    def commit(self, when, version='0.2.0', marker=''):
        """A dotfiles commit whose fleet script pins pylon VERSION with the fake binary's checksum.
        WHEN is the committer time in Unix seconds. Returns the commit."""
        text = SCRIPT.read_text()
        text = re.sub(r'^PYLON_VERSION=\S+$', f'PYLON_VERSION={version}', text, flags=re.M)
        good = hashlib.sha256(self.BODY).hexdigest()
        for target in ('linux_aarch64', 'darwin_arm64'):
            text = re.sub(rf'^(PYLON_SHA256_{target}=)\S+$', r'\g<1>' + good, text, flags=re.M)
        files = {'dot_local/bin/executable_fleet': text + f'# {marker}\n',
                 'dot_local/bin/executable_fleet-ssh-keygen': f'#!/bin/sh\n# keygen {marker}\n'}
        for rel, body in files.items():
            (self.origin / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.origin / rel).write_text(body)
        stamp = f'@{when} +0000'
        self.git(self.origin, 'add', '-A')
        self.git(self.origin, 'commit', '-q', '--allow-empty', '-m', f'commit {marker}',
                 GIT_COMMITTER_DATE=stamp, GIT_AUTHOR_DATE=stamp)
        sha = self.git(self.origin, 'rev-parse', 'HEAD')
        self.git(self.origin, 'tag', f't{sha[:12]}')
        return sha

    def checkout(self, sha):
        """A depth-1 checkout of SHA, as the worker fetches a task's commit."""
        self.checkouts += 1
        wt = self.home / f'attempt-{self.checkouts}'
        self.git(self.home, 'init', '-q', str(wt))
        self.git(wt, 'fetch', '-q', '--depth', '1', f'file://{self.origin}', f'refs/tags/t{sha[:12]}')
        self.git(wt, 'checkout', '-q', 'FETCH_HEAD')
        self.assertEqual(self.git(wt, 'rev-list', '--count', 'HEAD'), '1')
        return wt

    def executor(self, command, sha, **env):
        return subprocess.run(['bash', 'dot_local/bin/executable_fleet', 'executor', command], cwd=self.checkout(sha),
                              capture_output=True, text=True, timeout=120, env=dict(self.env, **dict({'FLEET_PYLON_WAIT': '3'}, **env)))

    def record(self):
        return dict((l.split()[0], l.split()[1:]) for l in self.applied.read_text().splitlines()) \
            if self.applied.exists() else {}

    def targets(self):
        return [self.home / '.local/bin/fleet', self.home / '.local/bin/fleet-ssh-keygen']

    def snapshot(self):
        return {p: (p.stat().st_mtime_ns, p.read_bytes()) if p.exists() else None
                for p in self.targets() + [self.applied, self.home / '.local/bin/pylon']}

    def scheduler(self):
        log = self.home / 'scheduler.log'
        return log.read_text().splitlines() if log.exists() else []

    def downloads(self):
        log = self.home / 'curl.log'
        return [l for l in log.read_text().splitlines() if '/v1/artifacts/' in l] if log.exists() else []


class ExecutorApply(Executors):
    def test_apply_installs_the_fleet_files_of_the_commit_and_records_it(self):
        sha = self.commit(1_700_000_000, marker='one')
        result = self.executor('apply', sha)
        self.assertEqual(result.returncode, 0, result.stderr)
        fleet, keygen = self.targets()
        self.assertTrue(fleet.read_text().endswith('# one\n'))
        self.assertEqual(keygen.read_text(), '#!/bin/sh\n# keygen one\n')
        self.assertEqual((self.mode(fleet), self.mode(keygen)), (0o755, 0o755))
        self.assertEqual(self.record(), {'fleet-apply': [sha, '1700000000']})
        self.assertEqual(self.mode(self.applied), 0o600)
        self.assertEqual([p.name for p in self.state.iterdir() if p.name.startswith('.applied')], [])
        self.assertEqual([p.name for p in (self.home / '.local/bin').iterdir() if p.name.startswith('.')], [])
        self.assertIn(sha[:12], result.stdout)

    def test_the_same_commit_again_is_a_no_op(self):
        sha = self.commit(1_700_000_000, marker='one')
        self.assertEqual(self.executor('apply', sha).returncode, 0)
        before = self.snapshot()
        result = self.executor('apply', sha)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('already', result.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_the_same_commit_again_repairs_drift(self):
        sha = self.commit(1_700_000_000, marker='one')
        self.assertEqual(self.executor('apply', sha).returncode, 0)
        fleet, keygen = self.targets()
        good = fleet.read_bytes()
        fleet.write_text('#!/bin/sh\n# edited by hand\n')
        keygen.chmod(0o644)
        record = self.applied.read_bytes()
        result = self.executor('apply', sha)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fleet.read_bytes(), good)
        self.assertEqual((self.mode(fleet), self.mode(keygen)), (0o755, 0o755))
        self.assertEqual(self.applied.read_bytes(), record)
        self.assertIn(f'installed {fleet}', result.stdout)

    def test_a_script_that_does_not_parse_changes_nothing(self):
        good = self.commit(1_700_000_000, marker='one')
        self.assertEqual(self.executor('apply', good).returncode, 0)
        before = self.snapshot()
        bad = self.commit(1_700_000_100, marker='two\nif then (')
        result = self.executor('apply', bad)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('does not parse', result.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual([p.name for p in (self.home / '.local/bin').iterdir() if p.name.startswith('.')], [])

    def test_a_newer_commit_applies(self):
        old = self.commit(1_700_000_000, marker='one')
        new = self.commit(1_700_000_100, marker='two')
        self.assertEqual(self.executor('apply', old).returncode, 0)
        result = self.executor('apply', new)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.targets()[0].read_text().endswith('# two\n'))
        self.assertEqual(self.record(), {'fleet-apply': [new, '1700000100']})

    def test_an_older_commit_is_refused_without_changing_a_file(self):
        old = self.commit(1_700_000_000, marker='one')
        new = self.commit(1_700_000_100, marker='two')
        self.assertEqual(self.executor('apply', new).returncode, 0)
        before = self.snapshot()
        result = self.executor('apply', old)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('older than', result.stderr)
        self.assertIn(new[:12], result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_unchanged_files_keep_their_time(self):
        one = self.commit(1_700_000_000, marker='same')
        self.assertEqual(self.executor('apply', one).returncode, 0)
        before = {p: p.stat().st_mtime_ns for p in self.targets()}
        two = self.commit(1_700_000_100, marker='same')
        self.assertEqual(self.executor('apply', two).returncode, 0)
        self.assertEqual({p: p.stat().st_mtime_ns for p in self.targets()}, before)
        self.assertEqual(self.record()['fleet-apply'][0], two)

    def test_a_malformed_record_stops_the_executor(self):
        sha = self.commit(1_700_000_000)
        self.state.mkdir(parents=True, exist_ok=True)
        self.applied.write_text('fleet-apply not-a-commit\n')
        result = self.executor('apply', sha)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(self.applied), result.stderr)
        self.assertFalse(self.targets()[0].exists())

    def test_each_executor_has_its_own_fence(self):
        # pylon-install at a commit does not apply the fleet files, so it must not turn a later
        # fleet-apply at the same commit into a repeat; and a fleet-apply of a newer commit must not
        # make a release's pylon-install of an older pin commit stale.
        old = self.commit(1_700_000_000, marker='one')
        new = self.commit(1_700_000_100, marker='two')
        self.assertEqual(self.executor('pylon-install', old).returncode, 0)
        result = self.executor('apply', old)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.targets()[0].exists())
        self.assertEqual(self.executor('apply', new).returncode, 0)
        self.assertEqual(self.record(), {'fleet-apply': [new, '1700000100'], 'pylon-install': [old, '1700000000']})


class ExecutorPylonInstall(Executors):
    def test_install_restarts_and_proves_the_running_daemon(self):
        sha = self.commit(1_700_000_000)
        result = self.executor('pylon-install', sha)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / '.local/bin/pylon').read_bytes(), self.BODY)
        (download,) = self.downloads()
        self.assertIn('/v1/artifacts/pylon/0.2.0/linux-aarch64', download)
        self.assertIn('systemctl --user restart fleet-agent.service', self.scheduler())
        runtime = self.env['XDG_RUNTIME_DIR']
        self.assertIn(f'{runtime}/fleet-secrets.sock http://pylon/v1/status',
                      (self.home / 'sockets.log').read_text().splitlines())
        sockets = (self.home / 'sockets.log').read_text().splitlines()
        self.assertIn(f'{runtime}/fleet-secrets.sock http://pylon/v1/secrets?ref=op%3A%2F%2FFleet%2FHuggingFace%2Fcredential',
                      sockets)
        secret_calls = [json.loads(l) for l in (self.home / 'curl.log').read_text().splitlines() if '/v1/secrets' in l]
        self.assertEqual(len(secret_calls), 1)
        self.assertIn('--max-time', secret_calls[0])
        self.assertIn('--unix-socket', secret_calls[0])
        self.assertNotIn('hf_value', result.stdout + result.stderr)
        self.assertEqual((self.home / 'ssh-add.log').read_text().splitlines()[-1], f'{runtime}/fleet-agent.sock -L')
        self.assertIn('pylon 0.2.0', result.stdout)
        self.assertIn('2 agent keys', result.stdout)
        self.assertEqual(self.record(), {'pylon-install': [sha, '1700000000']})

    def test_the_pins_come_from_the_task_commit(self):
        sha = self.commit(1_700_000_000, version='0.3.0')
        self.set_status('0.3.0')
        result = self.executor('pylon-install', sha)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('/v1/artifacts/pylon/0.3.0/linux-aarch64', self.downloads()[0])

    def test_a_current_binary_still_restarts_and_is_checked(self):
        one = self.commit(1_700_000_000, marker='one')
        self.assertEqual(self.executor('pylon-install', one).returncode, 0)
        two = self.commit(1_700_000_100, marker='two')
        result = self.executor('pylon-install', two)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.downloads()), 1)
        self.assertEqual(self.scheduler().count('systemctl --user restart fleet-agent.service'), 2)

    def test_the_status_check_waits_for_startup(self):
        sha = self.commit(1_700_000_000)
        result = self.executor('pylon-install', sha, STATUS_AFTER='3')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / 'status-polls').read_text(), '3')

    def failed(self, result, message):
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(message, result.stderr)
        self.assertNotIn('pylon-install', self.record())

    def test_a_daemon_with_another_version_fails(self):
        self.set_status('0.1.9')
        self.failed(self.executor('pylon-install', self.commit(1_700_000_000)), "reports version '0.1.9'")

    def test_a_status_without_a_version_fails(self):
        self.set_status(None)
        self.failed(self.executor('pylon-install', self.commit(1_700_000_000)), 'no version')

    def test_a_daemon_that_does_not_answer_fails(self):
        sha = self.commit(1_700_000_000)
        self.failed(self.executor('pylon-install', sha, STATUS_AFTER='99', FLEET_PYLON_WAIT='2'), 'does not answer')

    def test_a_failed_restart_fails(self):
        (self.home / 'restart-fails').write_text('')
        self.failed(self.executor('pylon-install', self.commit(1_700_000_000)), 'could not restart the fleet agent')

    def test_a_failed_secret_check_fails(self):
        (self.home / 'selftest').write_text('refused')
        self.failed(self.executor('pylon-install', self.commit(1_700_000_000)), 'op://Fleet/HuggingFace/credential')

    def test_an_empty_secret_fails(self):
        (self.home / 'selftest').write_text('')
        self.failed(self.executor('pylon-install', self.commit(1_700_000_000)), 'empty')

    def test_the_secret_check_does_not_fall_back_to_the_broker(self):
        (self.home / 'selftest').write_text('refused')
        self.failed(self.executor('pylon-install', self.commit(1_700_000_000)), 'secrets socket')
        broker = [l for l in (self.home / 'curl.log').read_text().splitlines() if '/v1/secrets' in l and 'unix-socket' not in l]
        self.assertEqual(broker, [])

    def test_the_secret_reference_can_be_overridden(self):
        sha = self.commit(1_700_000_000)
        result = self.executor('pylon-install', sha, FLEET_SELFTEST_REF='op://Fleet/Other item/password')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('ref=op%3A%2F%2FFleet%2FOther%20item%2Fpassword', (self.home / 'sockets.log').read_text())

    def test_an_agent_without_keys_fails(self):
        (self.home / 'agent-keys').unlink()
        self.failed(self.executor('pylon-install', self.commit(1_700_000_000)), 'the fleet agent lists no keys')

    def test_a_repeat_is_harmless_and_an_older_commit_changes_nothing(self):
        old = self.commit(1_700_000_000, version='0.1.0', marker='old')
        new = self.commit(1_700_000_100, marker='new')
        self.assertEqual(self.executor('pylon-install', new).returncode, 0)
        before, restarts, downloads = self.snapshot(), len(self.scheduler()), len(self.downloads())
        result = self.executor('pylon-install', new)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('already', result.stdout)
        result = self.executor('pylon-install', old)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('older than', result.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((len(self.scheduler()), len(self.downloads())), (restarts, downloads))

    def test_the_download_has_a_time_limit(self):
        self.assertEqual(self.executor('pylon-install', self.commit(1_700_000_000)).returncode, 0)
        (download,) = self.downloads()
        self.assertIn('--connect-timeout', json.loads(download))
        self.assertIn('--max-time', json.loads(download))

    def without_timeout(self):
        """A PATH with every command of this one except timeout, as on a Mac without coreutils."""
        tools = self.home / 'no-timeout-bin'
        tools.mkdir()
        for d in self.env['PATH'].split(':'):
            if not os.path.isdir(d) or Path(d) == self.home / 'bin':
                continue
            for entry in os.scandir(d):
                if entry.name not in ('timeout', 'gtimeout') and not (tools / entry.name).exists():
                    (tools / entry.name).symlink_to(entry.path)
        return f"{self.home / 'bin'}:{tools}"

    def check_a_hung_agent(self, **env):
        (self.home / 'agent-hangs').write_text('')
        started = time.monotonic()
        result = self.executor('pylon-install', self.commit(1_700_000_000), FLEET_PYLON_WAIT='2', **env)
        self.assertLess(time.monotonic() - started, 20)
        self.failed(result, 'the fleet agent lists no keys')
        pid = int((self.home / 'ssh-add.pid').read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_a_hung_agent_fails_in_time_and_leaves_no_process(self):
        self.check_a_hung_agent()

    def test_a_hung_agent_fails_in_time_without_timeout(self):
        self.check_a_hung_agent(PATH=self.without_timeout())

    def test_macos_restarts_the_launchd_agent(self):
        sha = self.commit(1_700_000_000)
        result = self.executor('pylon-install', sha, FAKE_OS='Darwin', FAKE_ARCH='arm64')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'launchctl kickstart -k gui/{os.getuid()}/dev.fleet.agent', self.scheduler())
        self.assertIn(f'{self.home}/.ssh/fleet/secrets.sock http://pylon/v1/status',
                      (self.home / 'sockets.log').read_text().splitlines())


class ActlClient(ActlBase):
    def setUp(self):
        super().setUp()
        self.enroll_as('laptop-1')
        self.env['FLEET_ACTL'] = str(self.bin / 'actl')

    def test_client_init_creates_the_seed_once_and_prints_only_the_public_key(self):
        result = self.fleet('actl', 'client-init')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f'U{"A" * 55}\n')
        seed = self.state / 'actl/client.seed'
        self.assertEqual(self.mode(seed), 0o600)
        self.assertEqual(self.mode(seed.parent), 0o700)
        again = self.fleet('actl', 'client-init')
        self.assertEqual((again.returncode, again.stdout), (0, result.stdout), again.stderr)
        self.assertEqual(len(list(self.home.glob('.keygen-*'))), 1)
        self.assertNotIn('SUAPRIVATESEEDSENTINEL', result.stdout + result.stderr + again.stdout + again.stderr)
        seed.chmod(0o640)
        self.assertNotEqual(self.fleet('actl', 'client-init').returncode, 0)

    def test_client_config_renders_from_the_broker(self):
        self.assertNotEqual(self.fleet('actl', 'client-config').returncode, 0)
        self.assertEqual(self.fleet('actl', 'client-init').returncode, 0)
        result = self.fleet('actl', 'client-config')
        self.assertEqual(result.returncode, 0, result.stderr)
        h = str(self.home)
        self.assertEqual((self.config_dir / 'client.toml').read_text(),
                         f'[connection]\nservers = ["{self.SERVER}"]\nca_certificate = "{h}/.config/fleet/actl/ca.pem"\n'
                         f'seed_file = "{h}/.local/state/fleet/actl/client.seed"\nprincipal = "client.cli"\n')
        self.assertEqual((self.config_dir / 'ca.pem').read_text(), self.CA)

    def test_client_commands_fail_clearly_without_the_actl_pins(self):
        del self.env['FLEET_ACTL']
        result = self.fleet('actl', 'client-init')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('actl is not pinned', result.stderr)


# An ssh stub. The worker host answers `fleet worker install` with $INSTALL_LINE. The controller
# records --add-worker and adds the worker to the controller state that the actl stub lists.
# $HOME/fail-install and $HOME/fail-add-worker make that step fail.
SSH_STUB = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
home = Path(os.environ['HOME'])
a = sys.argv[1:]
with open(home / 'ssh.log', 'a') as f:
    f.write(json.dumps(a) + '\n')
host, command = a[-2], a[-1]
if 'fleet worker install' in command:
    if (home / 'fail-install').exists():
        sys.exit('install failed on ' + host)
    print('installed actl 0.2.0', file=sys.stderr)
    print(os.environ['INSTALL_LINE'])
    sys.exit(0)
if '--add-worker' in command:
    if (home / 'fail-add-worker').exists():
        sys.exit('provision.py: reload failed')
    spec = command.split('--add-worker ')[1].split()[0]
    state_path = home / 'controller.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    workers = state.setdefault('workers', [])
    if spec.split('=')[0] not in workers and not (home / 'worker-offline').exists():
        workers.append(spec.split('=')[0])
    state_path.write_text(json.dumps(state))
    sys.exit(0)
sys.exit('unexpected ssh ' + ' '.join(a))
'''


class WorkerEnroll(ActlBase):
    KEY = 'U' + 'K' * 55

    def setUp(self):
        super().setUp()
        self.enroll_as('laptop-1')
        self.env['FLEET_ACTL'] = str(self.bin / 'actl')
        (self.bin / 'ssh').write_text(SSH_STUB)
        (self.bin / 'ssh').chmod(0o700)
        self.repo = self.home / 'fleet-config'
        (self.repo / 'broker').mkdir(parents=True)
        self.roster = self.repo / 'broker/actl.json'
        self.roster.write_text(json.dumps({'server': self.SERVER, 'ca_pem': self.CA, 'workers': []}, indent=2) + '\n')
        self.assertEqual(self.fleet('actl', 'client-init').returncode, 0)
        self.env['INSTALL_LINE'] = f'brev-x86-cpu=linux:{self.KEY}'

    def enroll_worker(self, *extra, **env):
        return self.fleet('worker', 'enroll', 'bb', '--fleet-config', str(self.repo), '--timeout', '3', *extra, **env)

    def ssh_calls(self):
        return [json.loads(l) for l in (self.home / 'ssh.log').read_text().splitlines()]

    def test_enroll_uses_the_worker_id_from_the_install_line_not_the_alias(self):
        result = self.enroll_worker()
        self.assertEqual(result.returncode, 0, result.stderr)
        install, add = self.ssh_calls()
        self.assertEqual(install[-2:], ['bb', '~/.local/bin/fleet worker install'])
        self.assertEqual(add[-2], 'bb')
        self.assertIn('sudo -n ~/repos/agentic-control/scripts/provision.py --config /etc/actl-fleet/deployment.json '
                      f'--environment prod --add-worker brev-x86-cpu=linux:{self.KEY}', add[-1])
        self.assertEqual(json.loads(self.roster.read_text())['workers'], [{'id': 'brev-x86-cpu', 'os': 'linux'}])
        self.assertIn('bb is the worker brev-x86-cpu', result.stdout)
        self.assertIn('commit broker/actl.json', result.stdout)
        self.assertIn('brev-x86-cpu is connected', result.stdout)

    def test_a_host_or_controller_that_looks_like_an_option_is_refused(self):
        for args in (['-oProxyCommand'], ['bb', '--controller', '-oProxyCommand'], ['.bb'], ['bb', '--controller', '_x']):
            with self.subTest(args):
                result = self.fleet('worker', 'enroll', *args, '--fleet-config', str(self.repo), '--timeout', '3')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('invalid', result.stderr)
        self.assertFalse((self.home / 'ssh.log').exists())

    def test_ssh_gets_the_host_after_the_end_of_options(self):
        self.assertEqual(self.enroll_worker().returncode, 0)
        for call in self.ssh_calls():
            self.assertEqual(call[-3], '--', call)

    def test_controller_and_provisioner_are_configurable(self):
        result = self.enroll_worker('--controller', 'ctl-host', '--provisioner', '/srv/actl')
        self.assertEqual(result.returncode, 0, result.stderr)
        add = self.ssh_calls()[1]
        self.assertEqual(add[-2], 'ctl-host')
        self.assertIn('sudo -n /srv/actl/scripts/provision.py ', add[-1])
        self.assertNotEqual(self.enroll_worker('--provisioner', '/srv/$(x)').returncode, 0)

    def test_each_step_can_be_interrupted_and_run_again(self):
        steps = [('install', self.home / 'fail-install', 'install failed'),
                 ('add-worker', self.home / 'fail-add-worker', '--add-worker'),
                 ('roster', self.roster, 'actl.json'),
                 ('wait', self.home / 'worker-offline', 'not connected')]
        for step, marker, message in steps:
            with self.subTest(step):
                (self.home / 'controller.json').unlink(missing_ok=True)
                if step == 'roster':
                    saved = self.roster.read_text()
                    self.roster.unlink()
                else:
                    marker.write_text('')
                result = self.enroll_worker()
                self.assertNotEqual(result.returncode, 0, step)
                self.assertIn(message, result.stderr, step)
                if step == 'roster':
                    self.roster.write_text(saved)
                else:
                    marker.unlink()
                result = self.enroll_worker()
                self.assertEqual(result.returncode, 0, f'{step}: {result.stderr}')
                self.assertEqual(json.loads(self.roster.read_text())['workers'],
                                 [{'id': 'brev-x86-cpu', 'os': 'linux'}], step)

    def test_roster_refuses_an_id_enrolled_with_another_os(self):
        self.roster.write_text(json.dumps({'server': self.SERVER, 'ca_pem': self.CA,
                                           'workers': [{'id': 'Brev-X86-CPU', 'os': 'macos'}]}))
        result = self.enroll_worker()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('already in the roster', result.stderr)
        self.assertEqual(json.loads(self.roster.read_text())['workers'], [{'id': 'Brev-X86-CPU', 'os': 'macos'}])

    def test_a_malformed_install_line_stops_before_the_controller(self):
        for line in ['nonsense', 'brev-x86-cpu=linux:SUASEED', f'bad.id=linux:{self.KEY}']:
            (self.home / 'ssh.log').unlink(missing_ok=True)
            result = self.enroll_worker(INSTALL_LINE=line)
            self.assertNotEqual(result.returncode, 0, line)
            self.assertIn('ID=OS:PUBLIC_KEY', result.stderr)
            self.assertEqual(len(self.ssh_calls()), 1, line)


class FleetTask(ActlBase):
    COMMIT = 'c' * 40

    def setUp(self):
        super().setUp()
        self.enroll_as('laptop-1')
        self.env['FLEET_ACTL'] = str(self.bin / 'actl')
        self.env['FLEET_TASK_POLL'] = '0.05'
        self.publish([('brev-x86-cpu', 'linux'), ('spark-d', 'linux'), ('mba-15', 'macos')])
        self.assertEqual(self.fleet('actl', 'client-init').returncode, 0)
        self.tasks = self.state / 'tasks'

    def controller(self, **values):
        path = self.home / 'controller.json'
        state = json.loads(path.read_text()) if path.exists() else {}
        state.update(values)
        path.write_text(json.dumps(state))

    def run_dir(self, result):
        m = re.search(r'fleet task --resume (\S+)', result.stdout + result.stderr)
        self.assertTrue(m, result.stdout + result.stderr)
        return self.tasks / m.group(1)

    def docs(self, run, worker):
        return sorted((run / worker).glob('task-*.json'), key=lambda p: int(p.stem.split('-')[1]))

    def report(self, result):
        return {l.split()[0]: l.split()[1] for l in result.stdout.splitlines() if l and l.split()[0] in
                ('brev-x86-cpu', 'spark-d', 'mba-15')}

    def submits(self):
        return [json.loads(l) for l in (self.home / 'actl.log').read_text().splitlines() if 'submit' in l]

    def test_all_sends_one_task_per_machine_and_reports_an_offline_machine(self):
        self.controller(offline=['mba-15'])
        result = self.fleet('task', 'secrets-flush', '--all', '--commit', self.COMMIT, '--timeout', '2')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.report(result), {'brev-x86-cpu': 'completed', 'spark-d': 'completed', 'mba-15': 'queued'})
        self.assertIn('1 machine unfinished', result.stdout)
        run = self.run_dir(result)
        spec = json.loads((run / 'spec.json').read_text())
        self.assertEqual((spec['task'], spec['commit'], spec['hosts']),
                         ('secrets-flush', self.COMMIT, ['brev-x86-cpu', 'spark-d', 'mba-15']))
        self.assertEqual(spec['roster'], [{'id': 'brev-x86-cpu', 'os': 'linux'}, {'id': 'spark-d', 'os': 'linux'},
                                          {'id': 'mba-15', 'os': 'macos'}])
        for worker in spec['hosts']:
            (doc,) = self.docs(run, worker)
            task = json.loads(doc.read_text())
            self.assertEqual((task['task_type'], task['commit'], task['required_capabilities']),
                             ('secrets-flush', self.COMMIT, [f'host-{worker}']))
        for call in self.submits():
            self.assertIn('--save-task', call)
        self.assertTrue((self.config_dir / 'client.toml').exists())
        self.controller(offline=[])
        resumed = self.fleet('task', '--resume', run.name, '--timeout', '2')
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(set(self.report(resumed).values()), {'completed'})
        self.assertEqual(len(self.submits()), 3)

    def test_host_selects_one_machine_from_the_roster(self):
        result = self.fleet('task', 'fleet-probe', '--host', 'spark-d', '--commit', self.COMMIT, '--timeout', '2')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.report(result), {'spark-d': 'completed'})
        result = self.fleet('task', 'fleet-probe', '--host', 'bb', '--commit', self.COMMIT)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not in the roster', result.stderr)
        for bad in [('task', 'rm-rf', '--all', '--commit', self.COMMIT), ('task', 'fleet-probe', '--commit', self.COMMIT),
                    ('task', 'fleet-probe', '--all', '--commit', 'main')]:
            self.assertNotEqual(self.fleet(*bad).returncode, 0, bad)

    def test_default_commit_is_main_of_the_public_dotfiles(self):
        (self.bin / 'git').write_text('#!/bin/sh\nprintf "%s\\n" "$*" > "$HOME/git-args"\n'
                                      f'printf "{self.COMMIT}\\trefs/heads/main\\n"\n')
        (self.bin / 'git').chmod(0o700)
        result = self.fleet('task', 'fleet-probe', '--host', 'spark-d', '--timeout', '2')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.home / 'git-args').read_text().strip(),
                         'ls-remote https://github.com/ryanolson/dynamo-dotfiles.git refs/heads/main')
        self.assertEqual(json.loads((self.run_dir(result) / 'spec.json').read_text())['commit'], self.COMMIT)

    def test_resume_keeps_a_task_whose_reply_was_lost(self):
        self.controller(lose_admitted=['spark-d'], lose_dropped=['mba-15'])
        result = self.fleet('task', 'secrets-flush', '--all', '--commit', self.COMMIT, '--timeout', '1')
        self.assertNotEqual(result.returncode, 0)
        run = self.run_dir(result)
        admitted = json.loads(self.docs(run, 'spark-d')[0].read_text())['id']
        dropped = json.loads(self.docs(run, 'mba-15')[0].read_text())['id']
        resumed = self.fleet('task', '--resume', run.name, '--timeout', '2')
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(set(self.report(resumed).values()), {'completed'})
        self.assertEqual(len(self.docs(run, 'spark-d')), 1)
        self.assertEqual(len(self.docs(run, 'mba-15')), 1)
        tasks = json.loads((self.home / 'controller.json').read_text())['tasks']
        self.assertIn(admitted, tasks)
        self.assertIn(dropped, tasks)
        resends = [c for c in self.submits() if '--task-file' in c]
        self.assertEqual([c[c.index('--task-file') + 1] for c in resends], [str(self.docs(run, 'mba-15')[0])])

    def test_a_refused_resend_or_a_missing_document_gets_a_new_task(self):
        self.controller(lose_dropped=['spark-d', 'mba-15'], down=False)
        result = self.fleet('task', 'secrets-flush', '--all', '--commit', self.COMMIT, '--timeout', '1')
        run = self.run_dir(result)
        self.docs(run, 'mba-15')[0].write_text('{"id": "trunc')
        self.controller(expired=True)
        resumed = self.fleet('task', '--resume', run.name, '--timeout', '2')
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(self.report(resumed), {'brev-x86-cpu': 'completed', 'spark-d': 'completed',
                                                'mba-15': 'completed'})
        for worker in ['spark-d', 'mba-15']:
            docs = self.docs(run, worker)
            self.assertEqual([d.name for d in docs], ['task-1.json', 'task-2.json'], worker)
            new = json.loads(docs[1].read_text())
            self.assertIn(new['id'], json.loads((self.home / 'controller.json').read_text())['tasks'])
            self.assertIn(new['id'], resumed.stdout)

    def test_an_unreachable_controller_creates_no_new_documents(self):
        result = self.fleet('task', 'secrets-flush', '--all', '--commit', self.COMMIT, '--timeout', '1',
                            )
        run = self.run_dir(result)
        self.controller(down=True)
        resumed = self.fleet('task', '--resume', run.name, '--timeout', '1')
        self.assertNotEqual(resumed.returncode, 0)
        for worker in ['brev-x86-cpu', 'spark-d', 'mba-15']:
            self.assertEqual(len(self.docs(run, worker)), 1)
        self.assertIn('fleet task --resume', resumed.stdout)

    def test_one_resume_runs_at_a_time(self):
        self.controller(offline=['mba-15'])
        result = self.fleet('task', 'secrets-flush', '--host', 'mba-15', '--commit', self.COMMIT, '--timeout', '1')
        run = self.run_dir(result)
        holder = subprocess.Popen(['python3', '-c', 'import fcntl, sys, time\n'
                                   'f = open(sys.argv[1], "a"); fcntl.flock(f, fcntl.LOCK_EX); print("held", flush=True)\n'
                                   'time.sleep(30)', str(run / 'lock')], stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), 'held')
        resumed = self.fleet('task', '--resume', run.name, '--timeout', '1')
        self.assertNotEqual(resumed.returncode, 0)
        self.assertIn('another fleet task', resumed.stderr)

    def test_secret_flush_all_sends_secrets_flush_to_every_machine(self):
        result = self.fleet('secret', 'flush', '--all', '--commit', self.COMMIT, '--timeout', '2')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.report(result), {'brev-x86-cpu': 'completed', 'spark-d': 'completed', 'mba-15': 'completed'})
        types = [c[c.index('--type') + 1] for c in self.submits()]
        self.assertEqual(types, ['secrets-flush'] * 3)
        self.assertEqual(sorted(c[c.index('--requires') + 1] for c in self.submits()),
                         ['host-brev-x86-cpu', 'host-mba-15', 'host-spark-d'])
        self.assertFalse((self.home / 'curl.log').exists() and '--unix-socket' in (self.home / 'curl.log').read_text())
        self.controller(fail=['spark-d'])
        failed = self.fleet('secret', 'flush', '--all', '--commit', self.COMMIT, '--timeout', '2')
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('1 machine failed', failed.stdout)
        self.assertNotEqual(self.fleet('secret', 'flush', 'extra').returncode, 0)

    def test_run_specification_is_written_before_any_submission(self):
        self.controller(down=True)
        result = self.fleet('task', 'fleet-apply', '--all', '--commit', self.COMMIT, '--timeout', '1')
        self.assertNotEqual(result.returncode, 0)
        run = self.run_dir(result)
        self.assertEqual(json.loads((run / 'spec.json').read_text())['task'], 'fleet-apply')
        self.assertEqual(list(run.glob('.spec*')), [])
        self.assertIn('unsent', result.stdout)


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
                                      check=True, env=dict(os.environ, HOME=tmp)).stdout
                includes = [l.strip() for l in text.splitlines() if l.strip().startswith('path =')]
                self.assertEqual(includes[-1], 'path = ~/.ssh/fleet/gitconfig')


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

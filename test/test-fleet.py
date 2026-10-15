import hashlib
import json
import os
from pathlib import Path
import re
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
        labels = (['dev.fleet.renew', 'dev.fleet.agent', 'dev.fleet.tunnel.bb'] if os.uname().sysname == 'Darwin' else
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
        self.assertIn('systemctl --user try-restart fleet-agent.service', (self.home / 'scheduler.log').read_text())

    def test_a_current_binary_is_not_downloaded_again(self):
        self.install()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.curls()), 1)

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

"""Contract tests of fleet worker install and fleet task with the real actl CLI, controller, and worker
on a real NATS broker (agentic-control task 029, part 4). Linux only. They skip unless these are set:

  FLEET_CONTRACT_ACTL_DIR     a folder with actl and actl-supervisor built from agentic-control
  FLEET_CONTRACT_NATS_SERVER  a nats-server 2.12 binary
  FLEET_CONTRACT_SOURCE       an agentic-control checkout with the fleet deployment (task 029 part 3):
                              its scripts/provision.py renders the broker configuration
  FLEET_CONTRACT_COMMIT       optional: a pushed dotfiles commit that has `fleet executor probe`;
                              default: main of the public dotfiles

The worker fetches the public dotfiles from GitHub. Only the CA host is simulated: stubs stand in for
step, the broker (curl), and systemctl, as in test-fleet.py. Run it with a time limit:

  timeout 900 python3 test/test-fleet-actl-contract.py
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('test_fleet', ROOT / 'test/test-fleet.py')
tf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tf)

ACTL_DIR = os.environ.get('FLEET_CONTRACT_ACTL_DIR')
NATS = os.environ.get('FLEET_CONTRACT_NATS_SERVER')
SOURCE = os.environ.get('FLEET_CONTRACT_SOURCE')
WORKER = 'contract-1'

CURL_STUB = '''#!/bin/sh
url=""; out=""
while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift 2 ;; https://*) url="$1"; shift ;; *) shift ;; esac; done
case "$url" in
  */v1/actl) cat "$HOME/actl.json" ;;
  */v1/artifacts/actl-supervisor/*) cp "$HOME/supervisor-body" "$out" ;;
  */v1/artifacts/actl/*) cp "$HOME/actl-body" "$out" ;;
  *) exit 22 ;;
esac
'''


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def uuid7():
    ms = int(time.time() * 1000)
    r = os.urandom(10)
    return str(uuid.UUID(bytes=ms.to_bytes(6, 'big') + bytes([0x70 | (r[0] & 0x0f), r[1], 0x80 | (r[2] & 0x3f)]) + r[3:]))


@unittest.skipUnless(ACTL_DIR and NATS and SOURCE and sys.platform.startswith('linux'),
                     'set FLEET_CONTRACT_ACTL_DIR, FLEET_CONTRACT_NATS_SERVER, and FLEET_CONTRACT_SOURCE on Linux')
class ActlContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.procs = {}
        cls.tmp = Path(tempfile.mkdtemp(prefix='fleet-contract-', dir=os.environ.get('FLEET_CONTRACT_TMP')))
        home = cls.home = cls.tmp / 'home'
        home.mkdir(mode=0o700)
        bindir = home / 'bin'
        bindir.mkdir()
        recorder = '#!/bin/sh\nprintf "%s %s\\n" "$(basename "$0")" "$*" >> "$HOME/scheduler.log"\n'
        for name, body in [('step', tf.STEP_STUB), ('systemctl', tf.SYSTEMCTL_STUB), ('loginctl', recorder),
                           ('journalctl', '#!/bin/sh\ntail -n 40 "$HOME/worker.log"\n'), ('curl', CURL_STUB)]:
            (bindir / name).write_text(body)
            (bindir / name).chmod(0o700)
        cls.actl = Path(ACTL_DIR) / 'actl'
        cls.env = dict(os.environ, HOME=str(home), PATH=f'{bindir}:{os.environ["PATH"]}', STEP_LOG=str(home / 'step.log'),
                       EXPECT_TOKEN=tf.TOKEN, USER_CA=tf.USER_CA, HOST_CA=tf.HOST_CA,
                       XDG_CONFIG_HOME=str(home / '.config'), XDG_STATE_HOME=str(home / '.local/state'),
                       FLEET_PYLON=str(home / 'no-pylon'), FLEET_TAILSCALE=str(home / 'no-tailscale'),
                       FLEET_ACTL=str(cls.actl), FLEET_TASK_POLL='0.5', RUST_LOG='warn')
        # Never the host's runtime folder: fleet keeps its agent and secrets sockets there on Linux.
        (cls.tmp / 'run').mkdir(mode=0o700)
        cls.env['XDG_RUNTIME_DIR'] = str(cls.tmp / 'run')
        cls.commit = os.environ.get('FLEET_CONTRACT_COMMIT') or subprocess.run(
            ['git', 'ls-remote', 'https://github.com/ryanolson/dynamo-dotfiles.git', 'refs/heads/main'],
            capture_output=True, text=True, check=True).stdout.split()[0]
        cls.fleet('enroll', WORKER, '--ca-url', 'https://ca.example', '--fingerprint', 'ab' * 32, '--no-trust',
                  stdin=tf.TOKEN + '\n')
        cls.tls()
        keys = cls.tmp / 'keys'
        keys.mkdir(mode=0o700)
        cls.public = {}
        for role in ('admin', 'controller', 'operator'):
            cls.public[role] = subprocess.run([str(cls.actl), 'keygen', '--seed-file', str(keys / f'{role}.seed')],
                                              capture_output=True, text=True, check=True).stdout.strip()
        cls.public['client'] = cls.fleet('actl', 'client-init').stdout.strip()
        cls.port = free_port()
        (home / 'actl.json').write_text(json.dumps({'server': f'tls://localhost:{cls.port}',
                                                   'ca_pem': (cls.tmp / 'ca.pem').read_text(),
                                                   'workers': [{'id': WORKER, 'os': 'linux'}]}))
        shutil.copy(cls.actl, home / 'actl-body')
        shutil.copy(Path(ACTL_DIR) / 'actl-supervisor', home / 'supervisor-body')
        version = subprocess.run([str(cls.actl), '--version'], capture_output=True, text=True,
                                 check=True).stdout.split()[1]
        cls.script = tf.pinned_script(home, actl=cls.actl.read_bytes(),
                                      supervisor=(Path(ACTL_DIR) / 'actl-supervisor').read_bytes(), version=version)
        line = cls.fleet('worker', 'install', script=cls.script).stdout.strip()
        assert line.startswith(f'{WORKER}=linux:U'), line
        cls.worker_key = line.split(':', 1)[1]
        cls.start_nats(keys)
        cls.connection('admin', 'admin.main', keys / 'admin.seed')
        subprocess.run([str(cls.actl), '--config', str(cls.tmp / 'admin.toml'), 'bootstrap'], env=cls.env,
                       check=True, capture_output=True, timeout=60)
        cls.write_controller(keys)
        cls.start_controller()
        cls.start_worker()
        cls.wait_for_worker()

    @classmethod
    def tearDownClass(cls):
        for proc in cls.procs.values():
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    proc.kill()
        if not os.environ.get('FLEET_CONTRACT_KEEP'):
            shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def fleet(cls, *args, stdin=None, script=None, check=True, timeout=300):
        result = subprocess.run(['bash', str(script or tf.SCRIPT), *args], input=stdin, capture_output=True,
                                text=True, env=cls.env, timeout=timeout)
        if check and result.returncode != 0:
            raise AssertionError(f'fleet {" ".join(args)}: {result.returncode}\n{result.stdout}\n{result.stderr}')
        return result

    @classmethod
    def tls(cls):
        t = cls.tmp
        run = lambda *a: subprocess.run(['openssl', *a], check=True, capture_output=True, cwd=t)
        run('req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes', '-keyout', 'ca.key',
            '-out', 'ca.pem', '-subj', '/CN=contract CA', '-days', '2', '-addext', 'basicConstraints=critical,CA:TRUE')
        run('req', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes', '-keyout', 'server.key',
            '-out', 'server.csr', '-subj', '/CN=localhost')
        (t / 'san.ext').write_text('subjectAltName=DNS:localhost,IP:127.0.0.1\n')
        run('x509', '-req', '-in', 'server.csr', '-CA', 'ca.pem', '-CAkey', 'ca.key', '-CAcreateserial',
            '-out', 'server.pem', '-days', '2', '-extfile', 'san.ext')

    @classmethod
    def start_nats(cls, keys):
        spec = importlib.util.spec_from_file_location('provision', Path(SOURCE) / 'scripts/provision.py')
        provision = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(provision)
        config = {'accounts': {'prod': {role: cls.public[role] for role in ('admin', 'controller', 'client', 'operator')}},
                  'workers': [{'id': WORKER, 'os': 'linux', 'key': cls.worker_key}]}
        text = provision.render_fleet_nats(config)
        store = cls.tmp / 'jetstream'
        replacements = {'$ACTL_NATS_LISTEN': f'127.0.0.1:{cls.port}', 'http: 127.0.0.1:8222': f'http: 127.0.0.1:{free_port()}',
                        'store_dir: /data': f'store_dir: {store}',
                        '/run/actl-tls/server.pem': str(cls.tmp / 'server.pem'),
                        '/run/actl-tls/server.key': str(cls.tmp / 'server.key')}
        for old, new in replacements.items():
            assert text.count(old) == 1, old
            text = text.replace(old, new)
        (cls.tmp / 'nats.conf').write_text(text)
        cls.spawn('nats', [NATS, '-c', str(cls.tmp / 'nats.conf')])
        for _ in range(100):
            try:
                socket.create_connection(('127.0.0.1', cls.port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError('nats-server did not start: ' + (cls.tmp / 'nats.log').read_text())

    @classmethod
    def connection(cls, name, principal, seed):
        text = (f'[connection]\nservers = ["tls://localhost:{cls.port}"]\nca_certificate = "{cls.tmp}/ca.pem"\n'
                f'seed_file = "{seed}"\nprincipal = "{principal}"\n')
        (cls.tmp / f'{name}.toml').write_text(text)
        return text

    @classmethod
    def write_controller(cls, keys):
        types = '["fleet-apply", "pylon-install", "secrets-flush", "fleet-probe"]'
        text = cls.connection('controller', 'controller.main', keys / 'controller.seed')
        text += (f'\n[controller]\ndatabase = "{cls.tmp}/controller/state.sqlite"\nrepositories = ["dotfiles"]\n'
                 f'task_types = {types}\n\n[controller.workers.{WORKER}]\nrepositories = ["dotfiles"]\n'
                 f'task_types = {types}\ncapabilities = ["host-{WORKER}", "os-linux"]\n')
        (cls.tmp / 'controller.toml').write_text(text)

    @classmethod
    def spawn(cls, name, argv):
        log = open(cls.home / f'{name}.log', 'a')
        cls.procs[name] = subprocess.Popen(argv, stdout=log, stderr=log, env=cls.env, stdin=subprocess.DEVNULL)
        log.close()

    @classmethod
    def start_controller(cls):
        cls.spawn('controller', [str(cls.actl), '--config', str(cls.tmp / 'controller.toml'), 'controller'])

    @classmethod
    def stop_controller(cls):
        proc = cls.procs.pop('controller')
        proc.terminate()
        proc.wait(timeout=60)

    @classmethod
    def start_worker(cls):
        cls.spawn('worker', [str(cls.home / '.local/bin/actl'), '--config',
                             str(cls.home / '.config/fleet/actl/worker.toml'), 'worker'])

    @classmethod
    def wait_for_worker(cls):
        for _ in range(120):
            result = subprocess.run([str(cls.actl), '--config', str(cls.home / '.config/fleet/actl/client.toml'), '--json',
                                     'workers'], capture_output=True, text=True, env=cls.env) \
                if (cls.home / '.config/fleet/actl/client.toml').exists() else None
            if result is None:
                cls.fleet('actl', 'client-config')
                continue
            if result.returncode == 0 and any(w['worker']['worker_id'] == WORKER
                                              for w in json.loads(result.stdout)['workers']):
                return
            time.sleep(1)
        raise RuntimeError('the worker did not check in: ' + (cls.home / 'worker.log').read_text()[-4000:])

    def run_dir(self, result):
        return self.home / '.local/state/fleet/tasks' / result.stdout.split()[1].rstrip(':')

    def docs(self, run):
        return sorted((run / WORKER).glob('task-*.json'), key=lambda p: int(p.stem.split('-')[1]))

    def report(self, result):
        line = next(l for l in result.stdout.splitlines() if l.startswith(WORKER + ' '))
        return line.split()

    def status(self, task_id):
        out = subprocess.run([str(self.actl), '--config', str(self.home / '.config/fleet/actl/client.toml'), '--json',
                              'status', task_id], capture_output=True, text=True, env=self.env, check=True).stdout
        return json.loads(out)['record']

    def test_a_task_completes_and_its_document_is_the_admitted_task(self):
        result = self.fleet('task', 'fleet-probe', '--host', WORKER, '--commit', self.commit, '--timeout', '180')
        name, state, detail, task_id = self.report(result)
        self.assertEqual((state, detail), ('completed', 'exit=0'), result.stdout + result.stderr)
        (doc,) = self.docs(self.run_dir(result))
        saved = json.loads(doc.read_text())
        self.assertEqual(saved['id'], task_id)
        self.assertEqual(self.status(task_id)['task'], saved)

    def test_a_lost_reply_is_resumed_with_the_admitted_task(self):
        # Without a controller the submission is stored in the durable command stream and no reply
        # comes. The document exists anyway: actl saves it before it sends.
        self.stop_controller()
        try:
            result = self.fleet('task', 'fleet-probe', '--host', WORKER, '--commit', self.commit, '--timeout', '1',
                                check=False)
            self.assertEqual(self.report(result)[1], 'unsent', result.stdout + result.stderr)
            run = self.run_dir(result)
            (doc,) = self.docs(run)
            saved = json.loads(doc.read_text())
        finally:
            self.start_controller()
        resumed = self.fleet('task', '--resume', run.name, '--timeout', '180')
        name, state, detail, task_id = self.report(resumed)
        self.assertEqual((state, detail, task_id), ('completed', 'exit=0', saved['id']), resumed.stdout)
        self.assertEqual(len(self.docs(run)), 1)

    def test_resume_without_a_complete_document_creates_one(self):
        self.stop_controller()
        try:
            result = self.fleet('task', 'fleet-probe', '--host', WORKER, '--commit', self.commit, '--timeout', '1',
                                check=False)
            run = self.run_dir(result)
            (doc,) = self.docs(run)
            doc.write_text(doc.read_text()[:40])
        finally:
            self.start_controller()
        resumed = self.fleet('task', '--resume', run.name, '--timeout', '180')
        name, state, detail, task_id = self.report(resumed)
        self.assertEqual((state, detail), ('completed', 'exit=0'), resumed.stdout + resumed.stderr)
        docs = self.docs(run)
        self.assertEqual([d.name for d in docs], ['task-1.json', 'task-2.json'])
        self.assertEqual(json.loads(docs[1].read_text())['id'], task_id)

    def test_a_refused_resend_leads_to_a_new_task_with_a_terminal_result(self):
        self.stop_controller()
        try:
            result = self.fleet('task', 'fleet-probe', '--host', WORKER, '--commit', self.commit, '--timeout', '1',
                                check=False)
            run = self.run_dir(result)
            (doc,) = self.docs(run)
            # A document that never reached the controller and is now older than the admission window
            # of 300 seconds: the controller refuses it as expired.
            saved = json.loads(doc.read_text())
            saved.update(id=uuid7(), correlation_id=uuid7(), created_at=int(time.time()) - 400)
            doc.write_text(json.dumps(saved, indent=2))
        finally:
            self.start_controller()
        resumed = self.fleet('task', '--resume', run.name, '--timeout', '180')
        self.assertIn('refused task', resumed.stderr)
        name, state, detail, task_id = self.report(resumed)
        self.assertEqual((state, detail), ('completed', 'exit=0'), resumed.stdout + resumed.stderr)
        docs = self.docs(run)
        self.assertEqual(len(docs), 2)
        self.assertEqual(json.loads(docs[1].read_text())['id'], task_id)
        self.assertNotEqual(task_id, saved['id'])

    def test_the_rendered_worker_configuration_loads_in_actl(self):
        config = self.home / '.config/fleet/actl/worker.toml'
        result = subprocess.run([str(self.home / '.local/bin/actl'), '--config', str(config), 'doctor'],
                                capture_output=True, text=True, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        status = self.fleet('worker', 'status', script=self.script).stdout
        self.assertIn(f'connection: tls://localhost:{self.port} (configuration and credential OK)', status)
        self.assertIn('installed: actl', status)


if __name__ == '__main__':
    unittest.main()

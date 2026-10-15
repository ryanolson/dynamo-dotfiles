"""Contract tests of fleet worker install and fleet task with the real actl CLI, controller, and worker
on a real NATS broker (agentic-control task 029, part 4). Linux only. They skip unless these are set:

  FLEET_CONTRACT_ACTL_DIR     a folder with actl and actl-supervisor built from agentic-control
  FLEET_CONTRACT_NATS_SERVER  a nats-server 2.12 binary
  FLEET_CONTRACT_SOURCE       an agentic-control checkout with the fleet deployment (task 029 part 3):
                              its scripts/provision.py renders the broker configuration
  FLEET_CONTRACT_COMMIT       optional: a pushed dotfiles commit with the part 5 executors;
                              default: main of the public dotfiles
  FLEET_CONTRACT_OLD_COMMIT   optional: an older pushed commit with the part 5 executors, for the
                              refusal of a stale commit
  FLEET_CONTRACT_PYLON        optional: a pylon binary whose SHA-256 is the commit's
                              PYLON_SHA256_linux_aarch64 (or _x86_64), for the pylon-install test
  FLEET_CONTRACT_USER_UNITS   optional, 1: also prove that an executor restarts a user unit. This one
                              test uses the host's systemd user manager: it creates two transient units
                              named fleet-contract-*, a target and a worker, and removes them.

The worker fetches the public dotfiles from GitHub. Only the CA host is simulated: stubs stand in for
step, the broker (curl), and systemctl, as in test-fleet.py. The executors run the fleet script of the
commit with the rendered environment, whose PATH starts with the test home's ~/.local/bin. Stubs
there play the broker and restart a fake pylon, which serves the secrets and agent sockets in the
test's own XDG_RUNTIME_DIR. Run it with a time limit:

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


# On the executors' PATH. The broker: the pylon download only. Requests to a Unix socket go to the
# real curl, so the executor talks to the fake pylon below.
EXECUTOR_CURL = '''#!/bin/sh
case " $* " in *" --unix-socket "*) exec /usr/bin/curl "$@" ;; esac
url=""; out=""
while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift 2 ;; https://*) url="$1"; shift ;; *) shift ;; esac; done
case "$url" in
  */v1/artifacts/pylon/*) cp "$HOME/pylon-body" "$out" ;;
  *) exit 22 ;;
esac
'''
# systemctl --user restart fleet-agent.service asks the fake pylon to restart with the version that
# the task's fleet script pins (the executor runs in the checkout). The fake runs outside the worker,
# as systemd runs pylon: actl owns every process that an attempt starts, and a daemon left behind by
# an executor makes the worker stop with an unconfirmed teardown.
EXECUTOR_SYSTEMCTL = '''#!/bin/sh
printf '%s\\n' "$*" >> "$HOME/executor-systemctl.log"
[ "$*" = "--user restart fleet-agent.service" ] || exit 1
version=$(sed -n 's/^PYLON_VERSION=//p' dot_local/bin/executable_fleet 2>/dev/null | head -1)
exec python3 -c 'import socket, sys
s = socket.socket(socket.AF_UNIX)
s.connect(sys.argv[1])
s.sendall(sys.argv[2].encode() + b"\\n")
sys.exit(0 if s.recv(64) == b"restarted\\n" else 1)' "$HOME/fake-pylon.ctl" "${version:-unknown}"
'''
# The secrets socket (status with a version, flush, secrets by reference, each reference logged) and
# an ssh-agent that lists one key. A line on the control socket is a restart: the new version, a full
# cache again, and one more restart counted.
FAKE_PYLON = r'''import http.server, json, os, socketserver, struct, sys, threading
runtime, control, pidfile = sys.argv[1:]
state = {'entries': 3, 'version': 'not-restarted', 'restarts': 0}
class Http(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def answer(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        if self.path == '/v1/status':
            self.answer({'entries': state['entries'], 'ttl': 300, 'version': state['version'],
                         'restarts': state['restarts']})
        elif self.path.startswith('/v1/secrets?ref='):
            with open(control + '.secrets.log', 'a') as f:
                f.write(self.path + '\n')
            self.send_response(200)
            self.send_header('Content-Length', '8')
            self.end_headers()
            self.wfile.write(b'hf_value')
        else:
            self.send_error(404)
    def do_POST(self):
        if self.path == '/v1/flush':
            flushed, state['entries'] = state['entries'], 0
            self.answer({'flushed': flushed})
        else:
            self.send_error(404)
def s(b):
    return struct.pack('>I', len(b)) + b
blob = s(b'ssh-ed25519') + s(bytes(range(32)))
class Agent(socketserver.BaseRequestHandler):
    def handle(self):
        while True:
            head = self.request.recv(4)
            if len(head) < 4:
                return
            body = self.request.recv(struct.unpack('>I', head)[0])
            if body[:1] == bytes([11]):
                reply = bytes([12]) + struct.pack('>I', 1) + s(blob) + s(b'fleet agent-signing')
            else:
                reply = bytes([5])
            self.request.sendall(s(reply))
class Control(socketserver.StreamRequestHandler):
    def handle(self):
        state.update(version=self.rfile.readline().decode().strip(), entries=3, restarts=state['restarts'] + 1)
        self.wfile.write(b'restarted\n')
class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
servers = []
for path, handler in ((os.path.join(runtime, 'fleet-secrets.sock'), Http),
                      (os.path.join(runtime, 'fleet-agent.sock'), Agent), (control, Control)):
    if os.path.exists(path):
        os.unlink(path)
    servers.append(Server(path, handler))
with open(pidfile, 'w') as f:
    f.write(str(os.getpid()))
for srv in servers[1:]:
    threading.Thread(target=srv.serve_forever, daemon=True).start()
servers[0].serve_forever()
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
        executor_bin = home / '.local/bin'
        for name, body in [('curl', EXECUTOR_CURL), ('systemctl', EXECUTOR_SYSTEMCTL), ('fake-pylon', FAKE_PYLON)]:
            (executor_bin / name).write_text(body)
            (executor_bin / name).chmod(0o700)
        if os.environ.get('FLEET_CONTRACT_PYLON'):
            shutil.copy(os.environ['FLEET_CONTRACT_PYLON'], home / 'pylon-body')
        cls.spawn('fake-pylon', ['python3', str(executor_bin / 'fake-pylon'), str(cls.tmp / 'run'),
                                 str(home / 'fake-pylon.ctl'), str(home / 'fake-pylon.pid')])
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
        types = '["fleet-apply", "pylon-install", "secrets-flush", "fleet-probe", "unit-restart"]'
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

    # ---- part 5: the executors through the real worker ----

    def task(self, name, commit):
        result = self.fleet('task', name, '--host', WORKER, '--commit', commit, '--timeout', '180', check=False)
        return self.report(result)

    def at(self, commit, path):
        """The file PATH of the public dotfiles at COMMIT, fetched one commit deep as the worker does."""
        repo = self.tmp / f'dotfiles-{commit[:12]}'
        if not repo.exists():
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            subprocess.run(['git', '-C', str(repo), 'fetch', '-q', '--depth', '1',
                            'https://github.com/ryanolson/dynamo-dotfiles.git', commit], check=True, timeout=120)
        return subprocess.run(['git', '-C', str(repo), 'show', f'FETCH_HEAD:{path}'], capture_output=True,
                              check=True).stdout

    def applied(self):
        path = self.home / '.local/state/fleet/applied'
        return {l.split()[0]: l.split()[1] for l in path.read_text().splitlines()} if path.exists() else {}

    def snapshot(self, *paths):
        return {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in paths}

    def test_fleet_apply_applies_once_and_refuses_an_older_commit(self):
        fleet = self.home / '.local/bin/fleet'
        name, state, detail, _ = self.task('fleet-apply', self.commit)
        self.assertEqual((state, detail), ('completed', 'exit=0'))
        self.assertEqual(fleet.read_bytes(), self.at(self.commit, 'dot_local/bin/executable_fleet'))
        self.assertTrue(os.access(fleet, os.X_OK))
        self.assertEqual(self.applied()['fleet-apply'], self.commit)
        record = self.home / '.local/state/fleet/applied'
        before = self.snapshot(fleet, self.home / '.local/bin/fleet-ssh-keygen', record)
        name, state, detail, _ = self.task('fleet-apply', self.commit)
        self.assertEqual((state, detail), ('completed', 'exit=0'))
        self.assertEqual(self.snapshot(*before), before)
        old = os.environ.get('FLEET_CONTRACT_OLD_COMMIT')
        if not old:
            self.skipTest('set FLEET_CONTRACT_OLD_COMMIT for the stale commit')
        name, state, detail, _ = self.task('fleet-apply', old)
        self.assertEqual(state, 'failed')
        self.assertTrue(detail.endswith(',exit=1'), detail)
        self.assertEqual(self.snapshot(*before), before)

    def pylon_status(self):
        out = subprocess.run(['/usr/bin/curl', '-fsS', '--unix-socket', str(self.tmp / 'run/fleet-secrets.sock'),
                              'http://pylon/v1/status'], capture_output=True, text=True, check=True).stdout
        return json.loads(out)

    @unittest.skipUnless(os.environ.get('FLEET_CONTRACT_PYLON'), 'set FLEET_CONTRACT_PYLON')
    def test_pylon_install_proves_the_daemon_and_a_repeat_is_harmless(self):
        name, state, detail, _ = self.task('pylon-install', self.commit)
        self.assertEqual((state, detail), ('completed', 'exit=0'), (self.home / 'fake-pylon.log').read_text())
        self.assertEqual((self.home / '.local/bin/pylon').read_bytes(), (self.home / 'pylon-body').read_bytes())
        pinned = [l for l in self.at(self.commit, 'dot_local/bin/executable_fleet').decode().splitlines()
                  if l.startswith('PYLON_VERSION=')][0].split('=', 1)[1]
        self.assertEqual(self.pylon_status()['version'], pinned)
        self.assertEqual(self.applied()['pylon-install'], self.commit)
        self.assertIn('/v1/secrets?ref=op%3A%2F%2FFleet%2FHuggingFace%2Fcredential',
                      (self.home / 'fake-pylon.ctl.secrets.log').read_text().splitlines())
        restarts = self.pylon_status()['restarts']
        self.assertGreater(restarts, 0)
        name, state, detail, _ = self.task('pylon-install', self.commit)
        self.assertEqual((state, detail), ('completed', 'exit=0'))
        self.assertEqual(self.pylon_status()['restarts'], restarts)

    def test_secrets_flush_all_empties_the_cache(self):
        self.assertGreater(self.pylon_status()['entries'], 0)
        result = self.fleet('secret', 'flush', '--all', '--commit', self.commit, '--timeout', '180', check=False)
        name, state, detail, _ = self.report(result)
        self.assertEqual((state, detail), ('completed', 'exit=0'), result.stdout + result.stderr)
        self.assertEqual(self.pylon_status()['entries'], 0)

    @unittest.skipUnless(os.environ.get('FLEET_CONTRACT_USER_UNITS') == '1', 'set FLEET_CONTRACT_USER_UNITS=1')
    def test_an_executor_restarts_a_user_unit(self):
        # The worker runs as a systemd user service and its executor gets the rendered environment:
        # no DBUS_SESSION_BUS_ADDRESS, XDG_RUNTIME_DIR the user's. The rendered value here is the
        # test's own folder, so this executor takes the rendered arguments with only that value
        # replaced by the user's, as it is on a real machine. Everything else is the host's user
        # manager, reached by two throwaway transient units.
        runtime = f'/run/user/{os.getuid()}'
        host = dict(os.environ, XDG_RUNTIME_DIR=runtime)
        tag = uuid.uuid4().hex[:8]
        target, worker = f'fleet-contract-target-{tag}.service', f'fleet-contract-worker-{tag}.service'
        toml = (self.home / '.config/fleet/actl/worker.toml').read_text()
        block = toml.split('[worker.executors.fleet-probe]\n')[1].split('\n\n')[0]
        args = json.loads(next(l for l in block.splitlines() if l.startswith('args = '))[len('args = '):])
        self.assertEqual(args[-4:], ['/bin/bash', 'dot_local/bin/executable_fleet', 'executor', 'probe'])
        self.assertNotIn('DBUS_SESSION_BUS_ADDRESS', ' '.join(args))
        env = [f'XDG_RUNTIME_DIR={runtime}' if a.startswith('XDG_RUNTIME_DIR=') else a for a in args[:-4]]
        # /usr/bin/systemctl: the systemctl on the rendered PATH is the test's stub that restarts the fake pylon.
        script = '/usr/bin/systemctl --user restart "$0" && /usr/bin/systemctl --user show -p MainPID --value "$0"'
        unit_args = env + ['/bin/sh', '-c', script, target]
        config = self.tmp / 'worker-unit.toml'
        config.write_text(toml + '\n[worker.executors.unit-restart]\nkind = "command"\nprogram = "/usr/bin/env"\n'
                          f'args = {json.dumps(unit_args)}\n')
        show = lambda prop: subprocess.run(['systemctl', '--user', 'show', '-p', prop, '--value', target],
                                           capture_output=True, text=True, env=host, check=True).stdout.strip()
        proc = self.procs.pop('worker')
        proc.terminate()
        proc.wait(timeout=90)
        try:
            subprocess.run(['systemd-run', '--user', '--quiet', f'--unit={target}', '/bin/sleep', '600'],
                           env=host, check=True)
            before = (show('MainPID'), show('InvocationID'))
            subprocess.run(['systemd-run', '--user', '--quiet', f'--unit={worker}', '-E', f'HOME={self.home}',
                            '-E', 'RUST_LOG=warn', str(self.home / '.local/bin/actl'), '--config', str(config), 'worker'],
                           env=host, check=True)
            client = str(self.home / '.config/fleet/actl/client.toml')
            out = subprocess.run([str(self.actl), '--config', client, '--json', 'submit', '--repo', 'dotfiles', '--commit',
                                  self.commit, '--type', 'unit-restart', '--requires', f'host-{WORKER}'],
                                 capture_output=True, text=True, env=self.env, check=True).stdout
            task_id = json.loads(out)['record']['task']['id']
            for _ in range(180):
                record = self.status(task_id)
                if record['state']['status'] in ('completed', 'failed', 'cancelled'):
                    break
                time.sleep(1)
            logs = ''.join(p.read_text(errors='replace')[-2000:] for p in
                           (self.home / '.local/state/fleet/actl-worker').rglob('*.log') if task_id in str(p) or
                           record['state'].get('result', {}).get('stderr', {}).get('attempt_id', '-') in str(p))
            self.assertEqual((record['state']['status'], record['state'].get('result', {}).get('exit_code')),
                             ('completed', 0), logs or json.dumps(record['state'])[:2000])
            after = (show('MainPID'), show('InvocationID'))
            self.assertNotEqual(after[0], before[0])
            self.assertNotEqual(after[1], before[1])
            self.assertEqual(show('ActiveState'), 'active')
        finally:
            for unit in (worker, target):
                subprocess.run(['systemctl', '--user', 'stop', unit], env=host, capture_output=True)
                subprocess.run(['systemctl', '--user', 'reset-failed', unit], env=host, capture_output=True)
            self.start_worker()
            self.wait_for_worker()


if __name__ == '__main__':
    unittest.main()

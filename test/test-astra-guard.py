"""astra-guard: the only tool that calls the Astra GitOps project's GitLab API.

Every case runs the real script against fakes, and never a real GitLab:

- a fake GitLab, an HTTP server in this process, backed by real bare git
  repositories. It serves only the API calls the guard is meant to make; any
  other method or path is answered 418 and fails the test. Every request must
  carry the sentinel token. Merges honour `sha` (409 when the head moved) and
  squash the branch into one new commit on `main`, as the real project does.
  Pipelines are made up per commit: green by default, with a `helm:plan` trace
  in helm-diff form; tests turn jobs red or remove them.
- a fake `fleet` that implements `fleet secret env VAR=REF -- CMD` with a
  sentinel token;
- a fake `bb` that implements only `bb sgc ask` and `bb sgc answer-of`, as
  the answer-of contract describes them. Tests set the answers in its state.

PATH holds only the fakes and /usr/bin:/bin. HOME, XDG_STATE_HOME and the
config point into a temporary directory. Every name, host and number here is
made up.
"""
from datetime import datetime, timedelta, timezone
import http.server
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'dot_agents/skills/k8s-clusters'
GUARD = SKILL / 'scripts/executable_astra-guard'
# No bytecode next to the skill's sources: chezmoi would deploy it.
sys.dont_write_bytecode = True
sys.path.insert(0, str(SKILL / 'scripts'))
import astra_guard_rules as rules  # noqa: E402

PID = 4242              # the deploy project
PLATFORM_PID = 77       # holds the ArgoCD app files
OTHER_PID = 99          # a project the guard must never write to
ME = 'thr_alpha1'
OTHER = 'thr_bravo2'
SECRET = 'SENTINEL-not-a-real-token-8c1e27'
TOKEN_REF = 'op://Example/Fake GitLab/credential'
NS = 'ns-wcep-example'
SHARED_NS = 'ns-shared-example'
DEV = 'dynamo-workloads/values/pdx04-dev/values.yaml'
STG = 'dynamo-workloads/values/pdx04-stg/values.yaml'
PRD = 'dynamo-workloads/values/pdx04-prd/values.yaml'
TEMPLATE = 'dynamo-workloads/stg/templates/dgd.yaml'
ISSUE = 'TEAM-910'
BRANCH = 'wcep/TEAM-910-smoke'

DEV_VALUES = '''\
# Made-up dev values for the astra-guard tests.
podSecurityContext:
  runAsUser: 1000

imagePullSecrets:
- example-pullsecret

dynamoDeployVault:
  externalSecrets:
  - name: example-hf-token
    data:
    - secretKey: HF_TOKEN
      property: EXAMPLE_HF_TOKEN

vllmModels: []
mockerRepros: []
'''

DEV_ENTRY = '''\
mockerRepros:
  - enabled: true
    name: wcep-smoke
    image: nvcr.io/example/dynamo:1.0
    worker:
      replicas: 1
      resources:
        requests: { cpu: "1", memory: "2Gi" }
'''

STG_VALUES = '''\
# Made-up staging values for the astra-guard tests.
nimModels:
  - name: it-canary
    gpuType: B200
    resources:
      limits: { nvidia.com/gpu: "1" }

mockerRepros:
  - enabled: true
    name: it-repro
    worker:
      replicas: 2
'''

STG_ENTRY = '''\
  - enabled: true
    name: wcep-shadow
    gpuCount: "2"
    worker:
      replicas: 1
'''

ARGO_APP = '''\
spec:
  destination:
    server: https://kubernetes.default.svc
    namespace: %s
'''


def dev_deploy(values=DEV_VALUES, entry=DEV_ENTRY):
    return values.replace('mockerRepros: []\n', entry)


def stg_deploy(values=STG_VALUES):
    return values + STG_ENTRY


def git(repo, *args, input=None, env=None):
    p = subprocess.run(['git', '-C', str(repo), *args], input=input, capture_output=True, text=True,
                       env={**os.environ, 'GIT_AUTHOR_NAME': 'Fake', 'GIT_AUTHOR_EMAIL': 'fake@example.test',
                            'GIT_COMMITTER_NAME': 'Fake', 'GIT_COMMITTER_EMAIL': 'fake@example.test',
                            'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1', **(env or {})})
    if p.returncode:
        raise RuntimeError(f'git {args}: {p.stderr}')
    return p.stdout


class FakeGitLab:
    """A strict fake of the GitLab API v4 calls the guard makes."""

    TREE_PAGE_CAP = 3  # smaller than the guard asks for, so pagination is exercised

    def __init__(self, root):
        self.root = Path(root)
        self.repos = {}
        self.mrs = {}
        self.next_iid = 7
        self.jobs = {}        # sha -> {name: status}; absent means the default green pair
        self.traces = {}      # sha -> helm:plan trace override
        self.no_pipeline = set()
        self.render_ns = NS
        self.requests = []
        self.unmodelled = []
        self.bad_auth = []
        self.lock = threading.Lock()
        self.pipelines = {}   # pipeline id -> sha
        self.job_index = {}   # job id -> (sha, name)
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _serve(self, method):
                length = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(length).decode() if length else ''
                with fake.lock:
                    status, headers, payload = fake.handle(method, self.path, body, self.headers)
                data = payload if isinstance(payload, bytes) else (
                    payload.encode() if isinstance(payload, str) else json.dumps(payload).encode())
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._serve('GET')

            def do_POST(self):
                self._serve('POST')

            def do_PUT(self):
                self._serve('PUT')

            def do_DELETE(self):
                self._serve('DELETE')

            def do_PATCH(self):
                self._serve('PATCH')

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    # -- repositories --------------------------------------------------------

    def add_repo(self, pid, files):
        repo = self.root / f'repo-{pid}.git'
        git(self.root, 'init', '-q', '--bare', '-b', 'main', str(repo))
        self.repos[pid] = repo
        self.commit(pid, 'main', files, parent=None)
        return repo

    def sha(self, pid, ref):
        return git(self.repos[pid], 'rev-parse', '--verify', '-q', f'{ref}^{{commit}}').strip()

    def branch_sha(self, branch, pid=PID):
        try:
            return self.sha(pid, f'refs/heads/{branch}')
        except RuntimeError:
            return None

    def file_at(self, ref, path, pid=PID):
        return git(self.repos[pid], 'show', f'{ref}:{path}')

    def commit(self, pid, branch, files, parent='branch', message='fake commit'):
        """Commit `files` ({path: text or None to delete}) onto `branch` (or onto `parent`)."""
        repo = self.repos[pid]
        if parent == 'branch':
            parent = self.branch_sha(branch, pid)
        with tempfile.TemporaryDirectory() as d:
            env = {'GIT_INDEX_FILE': str(Path(d) / 'index'), 'GIT_WORK_TREE': d}
            if parent:
                git(repo, 'read-tree', parent, env=env)
            for path, text in files.items():
                if text is None:
                    git(repo, 'update-index', '--force-remove', path, env=env)
                else:
                    blob = git(repo, 'hash-object', '-w', '--stdin', input=text).strip()
                    git(repo, 'update-index', '--add', '--cacheinfo', f'100644,{blob},{path}', env=env)
            tree = git(repo, 'write-tree', env=env).strip()
        args = ['commit-tree', tree, '-m', message] + (['-p', parent] if parent else [])
        sha = git(repo, *args).strip()
        git(repo, 'update-ref', f'refs/heads/{branch}', sha)
        return sha

    def chmod_file(self, branch, path, mode='100755'):
        repo = self.repos[PID]
        parent = self.branch_sha(branch)
        with tempfile.TemporaryDirectory() as d:
            env = {'GIT_INDEX_FILE': str(Path(d) / 'index')}
            git(repo, 'read-tree', parent, env=env)
            blob = git(repo, 'rev-parse', f'{parent}:{path}').strip()
            git(repo, 'update-index', '--cacheinfo', f'{mode},{blob},{path}', env=env)
            tree = git(repo, 'write-tree', env=env).strip()
        sha = git(repo, 'commit-tree', tree, '-p', parent, '-m', 'mode').strip()
        git(repo, 'update-ref', f'refs/heads/{branch}', sha)
        return sha

    def new_mr(self, branch, description='', pid=PID, target='main'):
        iid = self.next_iid
        self.next_iid += 1
        self.mrs[(pid, iid)] = {
            'iid': iid, 'project_id': pid, 'source_project_id': pid, 'target_project_id': pid,
            'source_branch': branch, 'target_branch': target, 'title': 't', 'description': description,
            'state': 'opened', 'draft': False, 'squash': True,
            'web_url': f'https://gitlab.example.test/example/deploy/-/merge_requests/{iid}',
            'merge_commit_sha': None, 'squash_commit_sha': None, 'merge_body': None}
        return iid

    def mr_view(self, mr):
        view = {k: v for k, v in mr.items() if k != 'merge_body'}
        if mr['state'] == 'opened':
            view['sha'] = self.branch_sha(mr['source_branch'], mr['source_project_id'])
        else:
            view['sha'] = mr.get('final_sha')
        return view

    # -- pipelines -----------------------------------------------------------

    def job_states(self, sha):
        return self.jobs.get(sha, {'helm:plan': 'success', 'validate:tests': 'success'})

    def set_job(self, sha, name, status):
        self.jobs.setdefault(sha, dict(self.job_states(sha)))[name] = status

    def drop_job(self, sha, name):
        self.jobs.setdefault(sha, dict(self.job_states(sha))).pop(name, None)

    def plan_trace(self, sha):
        if sha in self.traces:
            return self.traces[sha]
        main = self.sha(PID, 'refs/heads/main')
        lines = ['\x1b[0Ksection_start:1700000000:step_script\r\x1b[0KRunning helm plan',
                 '$ python3 scripts/helm-plan.py --base ' + main]
        for path in (DEV, STG):
            try:
                old = rules.entries(rules.load_values(self.file_at(main, path)))
                new = rules.entries(rules.load_values(self.file_at(sha, path)))
            except Exception:
                continue
            for key in sorted(set(old) | set(new)):
                name = key[1]
                if key not in old:
                    action = 'has been added'
                elif key not in new:
                    action = 'has been removed'
                elif old[key] != new[key]:
                    action = 'has changed'
                else:
                    continue
                lines.append(f'\x1b[33m{self.render_ns}, {name}, DynamoGraphDeployment (nvidia.com) {action}:\x1b[0m')
                lines.append('+ apiVersion: nvidia.com/v1alpha1')
        lines.append('\x1b[0Ksection_end:1700000001:step_script\r\x1b[0K')
        return '\n'.join(lines) + '\n'

    def pipeline_for(self, sha):
        for pid_, s in self.pipelines.items():
            if s == sha:
                return pid_
        pid_ = 500 + len(self.pipelines)
        self.pipelines[pid_] = sha
        return pid_

    # -- the API ------------------------------------------------------------

    def handle(self, method, raw_path, body, headers):
        url = urllib.parse.urlsplit(raw_path)
        path = url.path
        query = urllib.parse.parse_qs(url.query)
        self.requests.append({'method': method, 'path': raw_path, 'body': body,
                              'token': headers.get('PRIVATE-TOKEN') == SECRET})
        if headers.get('PRIVATE-TOKEN') != SECRET or headers.get('Authorization'):
            self.bad_auth.append(raw_path)
            return 401, {}, {'message': '401 Unauthorized'}
        if not path.startswith('/api/v4/'):
            return self.unmodelled_call(method, raw_path)
        parts = [urllib.parse.unquote(p) for p in path[len('/api/v4/'):].split('/')]
        try:
            return self.route(method, parts, query, json.loads(body) if body else None, raw_path)
        except RuntimeError as e:
            return 404, {}, {'message': f'404 Not found ({e})'}

    def unmodelled_call(self, method, raw_path):
        self.unmodelled.append(f'{method} {raw_path}')
        return 418, {}, {'message': 'not modelled by the fake'}

    def route(self, method, parts, query, body, raw_path):
        q1 = {k: v[0] for k, v in query.items()}
        if len(parts) < 2 or parts[0] != 'projects':
            return self.unmodelled_call(method, raw_path)
        pid = int(parts[1])
        if pid not in self.repos:
            return 404, {}, {'message': '404 Project Not Found'}
        rest = parts[2:]
        if method == 'GET' and rest == []:
            return 200, {}, {'id': pid, 'default_branch': 'main', 'merge_method': 'merge'}
        if method == 'GET' and rest[:2] == ['repository', 'branches'] and len(rest) == 3:
            sha = self.branch_sha(rest[2], pid)
            if not sha:
                return 404, {}, {'message': '404 Branch Not Found'}
            return 200, {}, {'name': rest[2], 'commit': {'id': sha}}
        if method == 'GET' and rest[:2] == ['repository', 'files'] and len(rest) == 4 and rest[3] == 'raw':
            ref = q1['ref']
            return 200, {'Content-Type': 'text/plain'}, self.file_at(ref, rest[2], pid)
        if method == 'GET' and rest == ['repository', 'tree']:
            ref, sub = q1['ref'], q1.get('path', '')
            per_page = min(int(q1.get('per_page', 20)), self.TREE_PAGE_CAP)
            page = int(q1.get('page', 1))
            out = git(self.repos[pid], 'ls-tree', f'{ref}:{sub}' if sub else ref)
            items = []
            for line in out.splitlines():
                meta, name = line.split('\t', 1)
                mode, typ, oid = meta.split()
                items.append({'id': oid, 'name': name, 'type': typ, 'mode': mode,
                              'path': f'{sub}/{name}' if sub else name})
            chunk = items[(page - 1) * per_page: page * per_page]
            more = page * per_page < len(items)
            return 200, {'X-Next-Page': str(page + 1) if more else ''}, chunk
        if method == 'GET' and rest == ['repository', 'merge_base']:
            refs = query.get('refs[]', [])
            if len(refs) != 2:
                return 400, {}, {'message': 'need two refs'}
            return 200, {}, {'id': git(self.repos[pid], 'merge-base', *refs).strip()}
        if method == 'GET' and rest == ['pipelines']:
            sha = q1.get('sha')
            if not sha or sha in self.no_pipeline:
                return 200, {}, []
            return 200, {}, [{'id': self.pipeline_for(sha), 'sha': sha, 'status': 'success',
                              'source': 'merge_request_event'}]
        if method == 'GET' and len(rest) == 3 and rest[0] == 'pipelines' and rest[2] == 'jobs':
            sha = self.pipelines[int(rest[1])]
            jobs = []
            for name, status in self.job_states(sha).items():
                jid = 9000 + len(self.job_index)
                self.job_index[jid] = (sha, name)
                jobs.append({'id': jid, 'name': name, 'status': status, 'allow_failure': True,
                             'commit': {'id': sha}, 'pipeline': {'id': int(rest[1]), 'sha': sha}})
            return 200, {}, jobs
        if method == 'GET' and len(rest) == 3 and rest[0] == 'jobs' and rest[2] == 'trace':
            sha, name = self.job_index[int(rest[1])]
            if name != 'helm:plan':
                return 200, {}, 'tests passed\n'
            return 200, {'Content-Type': 'text/plain'}, self.plan_trace(sha)
        if method == 'GET' and len(rest) == 2 and rest[0] == 'merge_requests':
            mr = self.mrs.get((pid, int(rest[1])))
            if not mr:
                return 404, {}, {'message': '404 Not found'}
            return 200, {}, self.mr_view(mr)
        if method == 'POST' and rest == ['repository', 'commits']:
            branch = body['branch']
            exists = self.branch_sha(branch, pid) is not None
            if exists and not body.get('force'):
                return 400, {}, {'message': 'branch exists'}
            files = {a['file_path']: a['content'] for a in body['actions'] if a['action'] == 'update'}
            sha = self.commit(pid, branch, files, parent=body['start_sha'],
                              message=body.get('commit_message', 'x'))
            return 201, {}, {'id': sha}
        if method == 'POST' and rest == ['merge_requests']:
            if self.branch_sha(body['source_branch'], pid) is None:
                return 400, {}, {'message': 'source branch missing'}
            iid = self.new_mr(body['source_branch'], body.get('description', ''), pid, body['target_branch'])
            self.mrs[(pid, iid)]['title'] = body.get('title', '')
            return 201, {}, self.mr_view(self.mrs[(pid, iid)])
        if method == 'PUT' and len(rest) == 2 and rest[0] == 'merge_requests':
            mr = self.mrs[(pid, int(rest[1]))]
            if body.get('state_event') == 'close':
                mr['final_sha'] = self.branch_sha(mr['source_branch'], pid)
                mr['state'] = 'closed'
                return 200, {}, self.mr_view(mr)
            return 400, {}, {'message': 'unsupported'}
        if method == 'PUT' and len(rest) == 3 and rest[0] == 'merge_requests' and rest[2] == 'merge':
            mr = self.mrs[(pid, int(rest[1]))]
            mr['merge_body'] = body
            head = self.branch_sha(mr['source_branch'], pid)
            if mr['state'] != 'opened':
                return 405, {}, {'message': '405 Method Not Allowed'}
            if body.get('sha') and body['sha'] != head:
                return 409, {}, {'message': 'SHA does not match HEAD of source branch'}
            main = self.branch_sha('main', pid)
            out = subprocess.run(['git', '-C', str(self.repos[pid]), 'merge-tree', '--write-tree', main, head],
                                 capture_output=True, text=True)
            if out.returncode:
                return 406, {}, {'message': 'conflict'}
            tree = out.stdout.split()[0]
            new = git(self.repos[pid], 'commit-tree', tree, '-p', main, '-m', f'squash !{mr["iid"]}').strip()
            git(self.repos[pid], 'update-ref', 'refs/heads/main', new)
            git(self.repos[pid], 'update-ref', '-d', f'refs/heads/{mr["source_branch"]}')
            mr.update(state='merged', final_sha=head, merge_commit_sha=new, squash_commit_sha=new)
            return 200, {}, self.mr_view(mr)
        return self.unmodelled_call(method, raw_path)

    def writes(self):
        return [r for r in self.requests if r['method'] != 'GET']


FAKE_FLEET = r'''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
with open(os.environ['FAKE_FLEET_LOG'], 'a') as f:
    f.write(json.dumps(a) + '\n')
if a[:2] != ['secret', 'env'] or '--' not in a:
    print('fake fleet: unsupported', file=sys.stderr); sys.exit(2)
i = a.index('--')
env = dict(os.environ)
for kv in a[2:i]:
    k, ref = kv.split('=', 1)
    if ref != os.environ['FAKE_FLEET_REF']:
        print('fake fleet: unknown secret', file=sys.stderr); sys.exit(3)
    env[k] = os.environ['FAKE_FLEET_SECRET']
for k in ('FAKE_FLEET_SECRET',):
    env.pop(k, None)
os.execvpe(a[i + 1], a[i + 1:], env)
'''

FAKE_BB = r'''#!/usr/bin/env python3
"""Fake bb: only `bb sgc ask` and `bb sgc answer-of`. State: FAKE_BB_STATE. Log: FAKE_BB_LOG."""
import json, os, sys
a = sys.argv[1:]
with open(os.environ['FAKE_BB_LOG'], 'a') as f:
    f.write(json.dumps({'argv': a, 'env_has_token': any(os.environ.get(k) == os.environ.get('FAKE_SENTINEL')
                                                         for k in os.environ if not k.startswith('FAKE_'))}) + '\n')
path = os.environ['FAKE_BB_STATE']
st = json.load(open(path)) if os.path.exists(path) else {'questions': {}, 'n': 0}
if a[:2] == ['sgc', 'ask']:
    opts, q, issue, i = [], None, None, 2
    while i < len(a):
        if a[i] in ('--question', '--option', '--recommend', '--label', '--issue'):
            if a[i] == '--question': q = a[i + 1]
            if a[i] == '--option': opts.append(a[i + 1])
            if a[i] == '--issue': issue = a[i + 1]
            i += 2
        else:
            i += 1
    st['n'] += 1
    qid = 'bbq_fake%d' % st['n']
    st['questions'][qid] = {'id': qid, 'threadId': os.environ.get('BB_THREAD_ID'), 'issue': issue,
                            'question': q, 'options': [{'letter': chr(65 + n), 'text': o} for n, o in enumerate(opts)],
                            'status': 'open', 'answeredAt': None, 'channel': None, 'option': None, 'text': None}
    json.dump(st, open(path, 'w'))
    print(json.dumps({'id': qid, 'status': 'open'}))
    sys.exit(0)
if a[:2] == ['sgc', 'answer-of'] and len(a) == 4 and a[3] == '--json':
    rec = st['questions'].get(a[2])
    if rec is None:
        print('unknown question ' + a[2], file=sys.stderr); sys.exit(1)
    print(json.dumps(rec))
    sys.exit(0)
print('fake bb: unexpected ' + ' '.join(a), file=sys.stderr)
sys.exit(2)
'''


def today():
    return datetime.now(timezone.utc).date()


class GuardCase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.gl = FakeGitLab(self.dir / 'gitlab')
        (self.dir / 'gitlab').mkdir(exist_ok=True)
        self.gl.add_repo(PID, {
            'README.md': 'made up\n',
            DEV: DEV_VALUES,
            STG: STG_VALUES,
            PRD: '# prod values, never touched\nmockerRepros: []\n',
            TEMPLATE: 'kind: DynamoGraphDeployment\n',
            'dynamo-workloads/prd/templates/dgd.yaml': 'kind: DynamoGraphDeployment\n',
        })
        self.gl.add_repo(PLATFORM_PID, {'apps/dev.yaml': ARGO_APP % NS, 'apps/stg.yaml': ARGO_APP % NS})
        self.gl.add_repo(OTHER_PID, {'README.md': 'another project\n'})
        self.bin = self.dir / 'bin'
        self.bin.mkdir()
        for name, text in (('fleet', FAKE_FLEET), ('bb', FAKE_BB)):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        self.home = self.dir / 'home'
        self.home.mkdir()
        self.state_home = self.dir / 'state'
        self.config = self.dir / 'astra.toml'
        self.write_config()
        self.outputs = []
        self.work = self.dir / 'work'
        self.work.mkdir()

    def tearDown(self):
        self.gl.close()
        self.assertEqual(self.gl.unmodelled, [], 'the guard made a call the fake does not model')
        self.assertEqual(self.gl.bad_auth, [], 'a request reached GitLab without the token header')
        self.tmp.cleanup()

    def write_config(self, extra='', argocd=True):
        def argo(site):
            if not argocd:
                return ''
            return (f'argocd = {{ project = {PLATFORM_PID}, file = "apps/{site}.yaml", '
                    'key = "spec.destination.namespace" }')
        self.config.write_text(f'''
gitlab_url = "{self.gl.url}"
project_id = {PID}
token_ref = "{TOKEN_REF}"
shared_namespaces = ["{SHARED_NS}"]
{extra}

[sites.pdx04-dev]
cluster = "dev-cluster.example.test"
{argo('dev')}

[sites.pdx04-stg]
cluster = "stg-cluster.example.test"
{argo('stg')}
''')

    def env(self, thread=ME):
        env = {
            'PATH': f'{self.bin}:/usr/bin:/bin',
            'HOME': str(self.home),
            'XDG_STATE_HOME': str(self.state_home),
            'ASTRA_GUARD_CONFIG': str(self.config),
            'FAKE_FLEET_LOG': str(self.dir / 'fleet.log'),
            'FAKE_FLEET_REF': TOKEN_REF,
            'FAKE_FLEET_SECRET': SECRET,
            'FAKE_SENTINEL': SECRET,
            'FAKE_BB_LOG': str(self.dir / 'bb.log'),
            'FAKE_BB_STATE': str(self.dir / 'bb-state.json'),
            'LANG': 'C.UTF-8',
            'CLAUDECODE': '1',
        }
        if thread:
            env['BB_THREAD_ID'] = thread
        return env

    def guard(self, *args, thread=ME):
        p = subprocess.run([sys.executable, '-I', str(GUARD), *args], capture_output=True, text=True,
                           env=self.env(thread), stdin=subprocess.DEVNULL, timeout=60)
        self.outputs.append(p.stdout + p.stderr)
        try:
            body = json.loads(p.stdout) if p.stdout.strip() else {}
        except json.JSONDecodeError:
            body = {'raw': p.stdout}
        return p.returncode, body, p.stderr

    def ok(self, *args, thread=ME):
        rc, body, err = self.guard(*args, thread=thread)
        self.assertEqual(rc, 0, f'{args}: {body} {err}')
        return body

    def refused(self, code, *args, thread=ME):
        rc, body, err = self.guard(*args, thread=thread)
        self.assertNotEqual(rc, 0, f'{args} was allowed: {body}')
        self.assertEqual(body.get('code'), code, f'{args}: {body} {err}')
        return body

    # -- flows ---------------------------------------------------------------

    def local(self, text, name='values.yaml'):
        p = self.work / name
        p.write_text(text)
        return str(p)

    def main(self):
        return self.gl.branch_sha('main')

    def push(self, text=None, path=DEV, branch=BRANCH, base=None):
        text = dev_deploy() if text is None else text
        return self.ok('push', '--branch', branch, '--base', base or self.main(),
                       '--file', f'{path}={self.local(text)}')

    def open(self, branch=BRANCH, kind='deploy', expires='future'):
        args = ['open', '--branch', branch, '--issue', ISSUE, '--kind', kind, '--title', 'Add wcep-smoke']
        if expires == 'future':
            args += ['--expires', str(today() + timedelta(days=3))]
        elif expires:
            args += ['--expires', expires]
        return self.ok(*args)['iid']

    def deployed_mr(self, text=None, path=DEV):
        self.push(text, path)
        return self.open()

    def check(self, iid, ns=NS):
        return self.ok('check', '--mr', str(iid), '--namespace', ns)

    def ask(self, iid):
        return self.ok('ask', '--mr', str(iid))['ask_id']

    def bb_state(self):
        return json.loads((self.dir / 'bb-state.json').read_text())

    def answer(self, qid, letter='A', channel='bb', text=None, thread=None, status='answered'):
        st = self.bb_state()
        rec = st['questions'][qid]
        rec['status'] = status
        rec['channel'] = channel
        rec['answeredAt'] = datetime.now(timezone.utc).isoformat()
        if letter:
            rec['option'] = next(o for o in rec['options'] if o['letter'] == letter)
            rec['text'] = None
        else:
            rec['option'] = None
            rec['text'] = text
        if thread:
            rec['threadId'] = thread
        (self.dir / 'bb-state.json').write_text(json.dumps(st))

    def add_question(self, question, letter_text='Approve', channel='bb', thread=ME):
        st = self.bb_state() if (self.dir / 'bb-state.json').exists() else {'questions': {}, 'n': 0}
        st['n'] += 1
        qid = f'bbq_hand{st["n"]}'
        st['questions'][qid] = {
            'id': qid, 'threadId': thread, 'issue': ISSUE, 'question': question,
            'options': [{'letter': 'A', 'text': 'Approve'}, {'letter': 'B', 'text': 'Reject'}],
            'status': 'answered', 'answeredAt': datetime.now(timezone.utc).isoformat(), 'channel': channel,
            'option': {'letter': 'A', 'text': letter_text}, 'text': None}
        (self.dir / 'bb-state.json').write_text(json.dumps(st))
        return qid

    def approved(self, text=None, path=DEV):
        iid = self.deployed_mr(text, path)
        self.check(iid)
        qid = self.ask(iid)
        self.answer(qid)
        return iid, qid

    def merge(self, iid, qid):
        return self.ok('merge', '--mr', str(iid), '--answer', qid)

    def log_lines(self):
        p = self.state_home / 'astra-guard' / 'approvals.jsonl'
        return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []

    def bb_calls(self):
        p = self.dir / 'bb.log'
        return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []

    def merges(self):
        return [r for r in self.gl.requests if r['method'] == 'PUT' and r['path'].endswith('/merge')]


class WritesOutsideTheList(GuardCase):
    """Every write outside the four allowed ones is refused, and nothing reaches GitLab."""

    def test_push_to_a_branch_outside_wcep_refused(self):
        for branch in ('main', 'feature/x', 'wcep/smoke', 'wcep/TEAM-910', 'wcep/TEAM-910-Smoke',
                       'wcep/TEAM-910-smoke/../main', 'refs/heads/wcep/TEAM-910-smoke', 'xwcep/TEAM-910-smoke'):
            with self.subTest(branch=branch):
                self.refused('branch_not_allowed', 'push', '--branch', branch, '--base', self.main(),
                             '--file', f'{DEV}={self.local(dev_deploy())}')
        self.assertEqual(self.gl.writes(), [])

    def test_push_of_a_file_outside_the_two_values_files_refused(self):
        for path in (TEMPLATE, PRD, 'dynamo-workloads/prd/templates/dgd.yaml', 'README.md',
                     'dynamo-workloads/values/pdx04-dev/../pdx04-prd/values.yaml', '/' + DEV):
            with self.subTest(path=path):
                self.refused('path_not_allowed', 'push', '--branch', BRANCH, '--base', self.main(),
                             '--file', f'{path}={self.local("x: 1\n")}')
        self.assertEqual(self.gl.writes(), [])

    def test_push_that_changes_an_it_entry_refused_before_it_is_sent(self):
        bad = DEV_VALUES.replace('runAsUser: 1000', 'runAsUser: 0')
        self.refused('outside_entries', 'push', '--branch', BRANCH, '--base', self.main(),
                     '--file', f'{DEV}={self.local(bad)}')
        self.assertEqual(self.gl.writes(), [])

    def test_push_on_a_stale_base_refused(self):
        old = self.main()
        self.gl.commit(PID, 'main', {'README.md': 'IT changed this\n'})
        self.refused('main_moved', 'push', '--branch', BRANCH, '--base', old,
                     '--file', f'{DEV}={self.local(dev_deploy())}')
        self.assertEqual(self.gl.writes(), [])

    def test_open_refuses_a_branch_outside_wcep_and_a_deploy_without_expiry(self):
        self.refused('branch_not_allowed', 'open', '--branch', 'feature/x', '--issue', ISSUE, '--kind', 'deploy',
                     '--title', 't', '--expires', str(today() + timedelta(days=1)))
        self.refused('branch_issue_mismatch', 'open', '--branch', 'wcep/TEAM-1-smoke', '--issue', ISSUE,
                     '--kind', 'deploy', '--title', 't', '--expires', str(today() + timedelta(days=1)))
        self.refused('expiry_missing', 'open', '--branch', BRANCH, '--issue', ISSUE, '--kind', 'deploy',
                     '--title', 't')
        self.refused('expiry_past', 'open', '--branch', BRANCH, '--issue', ISSUE, '--kind', 'deploy',
                     '--title', 't', '--expires', str(today() - timedelta(days=1)))
        self.assertEqual(self.gl.writes(), [])

    def test_close_refuses_a_merge_request_the_guard_did_not_open(self):
        self.gl.commit(PID, 'wcep/TEAM-910-other', {DEV: dev_deploy()}, parent=self.main())
        iid = self.gl.new_mr('wcep/TEAM-910-other')
        self.refused('not_guard_mr', 'close', '--mr', str(iid))
        self.assertEqual(self.gl.writes(), [])

    def test_close_of_a_guard_opened_merge_request_sends_only_state_event_close(self):
        iid = self.deployed_mr()
        self.ok('close', '--mr', str(iid))
        last = self.gl.writes()[-1]
        self.assertEqual((last['method'], json.loads(last['body'])), ('PUT', {'state_event': 'close'}))
        self.assertEqual(self.gl.mrs[(PID, iid)]['state'], 'closed')

    def test_merge_of_a_merge_request_the_guard_did_not_open_refused(self):
        self.gl.commit(PID, 'wcep/TEAM-910-other', {DEV: dev_deploy()}, parent=self.main())
        iid = self.gl.new_mr('wcep/TEAM-910-other')
        qid = self.add_question(f'!{iid}')
        body = self.refused('not_guard_mr', 'merge', '--mr', str(iid), '--answer', qid)
        self.assertNotIn('None', body['comment'])
        self.assertIn('an issue this guard did not record', body['comment'])
        self.refused('not_guard_mr', 'check', '--mr', str(iid), '--namespace', NS)
        self.assertEqual(self.merges(), [])

    def test_get_refuses_anything_but_a_relative_api_path(self):
        for path in ('https://evil.example.test/api/v4/projects', '//evil.example.test/x', 'projects/1',
                     '/projects/../../admin', '/projects/1/../2', '/projects/1\n/x', '/projects/1#x'):
            with self.subTest(path=path):
                self.refused('path_not_allowed', 'get', path)
        self.assertEqual(self.gl.requests, [])

    def test_get_reads_any_project(self):
        body = self.ok('get', f'/projects/{OTHER_PID}')
        self.assertEqual(body['body']['id'], OTHER_PID)
        self.assertEqual([r['method'] for r in self.gl.requests], ['GET'])

    def test_no_generic_write_command(self):
        for args in (['post', '/projects/1/merge_requests'], ['put', '/projects/1'], ['delete', '/projects/1'],
                     ['get', '--method', 'POST', '/projects/1'], ['rebase', '--mr', '7'], ['approve', '--mr', '7']):
            with self.subTest(args=args):
                rc, _, _ = self.guard(*args)
                self.assertNotEqual(rc, 0)
        self.assertEqual(self.gl.requests, [])

    def test_the_write_gate_allows_only_the_four_writes(self):
        loader = importlib.machinery.SourceFileLoader('astra_guard_cli', str(GUARD))
        spec = importlib.util.spec_from_loader('astra_guard_cli', loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        sha = 'a' * 40
        ok = [
            ('POST', f'/projects/{PID}/repository/commits',
             {'branch': BRANCH, 'start_sha': sha, 'force': True, 'commit_message': 'm',
              'actions': [{'action': 'update', 'file_path': DEV, 'content': 'x', 'encoding': 'text'}]}),
            ('POST', f'/projects/{PID}/merge_requests',
             {'source_branch': BRANCH, 'target_branch': 'main', 'title': 't', 'description': 'd'}),
            ('PUT', f'/projects/{PID}/merge_requests/7', {'state_event': 'close'}),
            ('PUT', f'/projects/{PID}/merge_requests/7/merge', {'sha': sha}),
        ]
        for method, path, body in ok:
            mod.gate(method, path, body, PID)
        bad = [
            ('DELETE', f'/projects/{PID}/repository/branches/main', None),
            ('POST', f'/projects/{OTHER_PID}/merge_requests',
             {'source_branch': BRANCH, 'target_branch': 'main', 'title': 't'}),
            ('POST', f'/projects/{PID}/repository/branches', {'branch': BRANCH, 'ref': 'main'}),
            ('POST', f'/projects/{PID}/repository/commits',
             {'branch': 'main', 'start_sha': sha, 'actions': [
                 {'action': 'update', 'file_path': DEV, 'content': 'x', 'encoding': 'text'}]}),
            ('POST', f'/projects/{PID}/repository/commits',
             {'branch': BRANCH, 'start_sha': sha, 'actions': [
                 {'action': 'update', 'file_path': TEMPLATE, 'content': 'x', 'encoding': 'text'}]}),
            ('POST', f'/projects/{PID}/repository/commits',
             {'branch': BRANCH, 'start_sha': sha, 'actions': [
                 {'action': 'create', 'file_path': DEV, 'content': 'x', 'encoding': 'text'}]}),
            ('POST', f'/projects/{PID}/repository/commits',
             {'branch': BRANCH, 'start_sha': sha, 'actions': [
                 {'action': 'update', 'file_path': DEV, 'content': 'x', 'encoding': 'base64'}]}),
            ('POST', f'/projects/{PID}/repository/commits',
             {'branch': BRANCH, 'start_sha': sha, 'actions': [
                 {'action': 'update', 'file_path': DEV, 'content': 'x', 'encoding': 'text',
                  'execute_filemode': True}]}),
            ('POST', f'/projects/{PID}/merge_requests',
             {'source_branch': BRANCH, 'target_branch': 'release', 'title': 't'}),
            ('POST', f'/projects/{PID}/merge_requests',
             {'source_branch': BRANCH, 'target_branch': 'main', 'title': 't', 'target_project_id': OTHER_PID}),
            ('POST', f'/projects/{PID}/merge_requests',
             {'source_branch': 'feature', 'target_branch': 'main', 'title': 't'}),
            ('PUT', f'/projects/{PID}/merge_requests/7', {'state_event': 'reopen'}),
            ('PUT', f'/projects/{PID}/merge_requests/7', {'state_event': 'close', 'target_branch': 'x'}),
            ('PUT', f'/projects/{PID}/merge_requests/7/merge', {}),
            ('PUT', f'/projects/{PID}/merge_requests/7/merge', {'sha': sha, 'merge_when_pipeline_succeeds': True}),
            ('PUT', f'/projects/{PID}/merge_requests/7/merge', {'sha': sha, 'auto_merge': True}),
            ('PUT', f'/projects/{PID}/merge_requests/7/rebase', {}),
            ('POST', f'/projects/{PID}/merge_requests/7/approve', {}),
            ('POST', f'/projects/{PID}/merge_requests/7/notes', {'body': 'hi'}),
            ('PATCH', f'/projects/{PID}', {}),
        ]
        for method, path, body in bad:
            with self.subTest(method=method, path=path, body=body):
                with self.assertRaises(rules.Refusal) as cm:
                    mod.gate(method, path, body, PID)
                self.assertEqual(cm.exception.code, 'write_not_allowed')


class DiffRules(GuardCase):
    """Each diff rule refuses its bad case at check, on a branch pushed around the guard."""

    def mr_with(self, files, branch=BRANCH, kind='deploy', base=None):
        # The guard's own push would refuse most of these, so build the branch directly
        # and open the merge request through the guard.
        self.gl.commit(PID, branch, files, parent=base or self.main())
        return self.open(branch, kind=kind, expires='future' if kind == 'deploy' else None)

    def test_a_good_deploy_passes_and_prints_the_ask(self):
        iid = self.deployed_mr()
        body = self.check(iid)
        head, main = self.gl.branch_sha(BRANCH), self.main()
        self.assertEqual((body['head'], body['target']), (head, main))
        q = body['question']
        for part in (f'!{iid}', head, main, 'dev-cluster.example.test', NS, 'wcep-smoke', 'GPUs: 0',
                     str(today() + timedelta(days=3)), 'fast-forward', '-  - enabled: true', '+mockerRepros: []'):
            self.assertIn(part, q)
        self.assertEqual(body['options'], ['Approve', 'Reject'])
        self.assertLessEqual(len(q), 4000)

    def test_template_and_prd_paths_refused(self):
        for files in ({TEMPLATE: 'kind: Changed\n'}, {PRD: 'mockerRepros: [x]\n'},
                      {'dynamo-workloads/prd/templates/dgd.yaml': 'x\n'}, {'new-file.yaml': 'x: 1\n'},
                      {'README.md': None}, {DEV: dev_deploy(), TEMPLATE: 'kind: Changed\n'}):
            with self.subTest(files=list(files)):
                branch = f'wcep/TEAM-910-p{len(self.gl.mrs)}'
                iid = self.mr_with(files, branch=branch)
                self.refused('path_not_allowed', 'check', '--mr', str(iid), '--namespace', NS)

    def test_a_mode_change_on_a_values_file_refused(self):
        self.gl.commit(PID, BRANCH, {DEV: dev_deploy()}, parent=self.main())
        self.gl.chmod_file(BRANCH, DEV)
        iid = self.open()
        self.refused('path_not_allowed', 'check', '--mr', str(iid), '--namespace', NS)

    def test_both_values_files_in_one_merge_request_refused(self):
        iid = self.mr_with({DEV: dev_deploy(), STG: stg_deploy()})
        self.refused('one_site', 'check', '--mr', str(iid), '--namespace', NS)

    def test_a_change_to_an_it_entry_refused(self):
        iid = self.mr_with({STG: stg_deploy().replace('replicas: 2', 'replicas: 3')})
        self.refused('outside_entries', 'check', '--mr', str(iid), '--namespace', NS)

    def test_renaming_an_it_entry_to_wcep_refused(self):
        iid = self.mr_with({STG: STG_VALUES.replace('name: it-repro', 'name: wcep-repro')})
        self.refused('outside_entries', 'check', '--mr', str(iid), '--namespace', NS)

    def test_a_wcep_item_outside_the_workload_lists_refused(self):
        bad = DEV_VALUES.replace('  - name: example-hf-token',
                                 '  - name: wcep-secret\n    data: []\n  - name: example-hf-token')
        iid = self.mr_with({DEV: bad})
        self.refused('outside_entries', 'check', '--mr', str(iid), '--namespace', NS)

    def test_a_top_level_change_outside_entries_refused(self):
        iid = self.mr_with({DEV: dev_deploy() + 'wcep-extra:\n  enabled: true\n'})
        self.refused('outside_entries', 'check', '--mr', str(iid), '--namespace', NS)

    def test_an_anchor_that_changes_an_it_entry_refused(self):
        # The anchor sits in our entry (in an earlier list); IT's entry takes its value by alias.
        values = STG_VALUES.replace('    worker:\n      replicas: 2', '    worker: *wcepworker')
        values = values.replace('nimModels:\n', 'nimModels:\n  - name: wcep-anchor\n    worker: &wcepworker\n'
                                                '      replicas: 9\n', 1)
        iid = self.mr_with({STG: values})
        self.refused('outside_entries', 'check', '--mr', str(iid), '--namespace', NS)

    def test_duplicate_keys_refused(self):
        iid = self.mr_with({DEV: dev_deploy() + 'vllmModels: []\n'})
        self.refused('yaml_invalid', 'check', '--mr', str(iid), '--namespace', NS)

    def test_entry_names_must_be_wcep_and_never_prd(self):
        for name in ('wcep-smoke-prd', 'wcep-', 'wcep-Smoke', 'wcep_smoke'):
            with self.subTest(name=name):
                branch = f'wcep/TEAM-910-n{len(self.gl.mrs)}'
                iid = self.mr_with({DEV: dev_deploy(entry=DEV_ENTRY.replace('wcep-smoke', name))}, branch=branch)
                self.refused('outside_entries' if not name.startswith('wcep-') else 'entry_name',
                             'check', '--mr', str(iid), '--namespace', NS)

    def test_a_dev_entry_that_requests_a_gpu_refused(self):
        for extra in ('    gpuCount: "1"\n', '    gpuType: B200\n',
                      '    worker2:\n      resources:\n        limits: { nvidia.com/gpu: "1" }\n'):
            with self.subTest(extra=extra):
                branch = f'wcep/TEAM-910-g{len(self.gl.mrs)}'
                iid = self.mr_with({DEV: dev_deploy(entry=DEV_ENTRY + extra)}, branch=branch)
                self.refused('dev_gpu', 'check', '--mr', str(iid), '--namespace', NS)

    def test_a_staging_entry_reports_its_gpus(self):
        iid = self.mr_with({STG: stg_deploy()})
        body = self.check(iid)
        self.assertIn('stg-cluster.example.test', body['question'])
        self.assertIn('gpuCount=2', body['question'])

    def test_a_deploy_needs_an_expiry_in_the_merge_request(self):
        iid = self.deployed_mr()
        self.gl.mrs[(PID, iid)]['description'] = 'no expiry here'
        self.refused('expiry_missing', 'check', '--mr', str(iid), '--namespace', NS)
        self.gl.mrs[(PID, iid)]['description'] = f'Expires: {today() - timedelta(days=1)}'
        self.refused('expiry_past', 'check', '--mr', str(iid), '--namespace', NS)

    def test_a_teardown_that_adds_or_changes_refused(self):
        # Land an entry on main first, so the teardown has something to remove.
        self.gl.commit(PID, 'main', {DEV: dev_deploy()})
        adds = dev_deploy(entry=DEV_ENTRY + '  - name: wcep-more\n    enabled: true\n')
        iid = self.mr_with({DEV: adds}, branch='wcep/TEAM-910-teardown', kind='teardown')
        self.refused('teardown_not_removal', 'check', '--mr', str(iid), '--namespace', NS)
        changes = dev_deploy().replace('replicas: 1', 'replicas: 2')
        iid = self.mr_with({DEV: changes}, branch='wcep/TEAM-910-teardown2', kind='teardown')
        self.refused('teardown_not_removal', 'check', '--mr', str(iid), '--namespace', NS)

    def test_a_branch_behind_main_refused_before_the_ask(self):
        iid = self.deployed_mr()
        self.gl.commit(PID, 'main', {'README.md': 'IT moved main\n'})
        self.refused('behind_main', 'check', '--mr', str(iid), '--namespace', NS)
        self.refused('not_checked', 'ask', '--mr', str(iid))
        self.assertEqual(self.bb_calls(), [])

    def test_a_merge_request_that_changes_nothing_refused(self):
        iid = self.mr_with({DEV: DEV_VALUES + '# only a comment\n'})
        self.refused('no_entry_change', 'check', '--mr', str(iid), '--namespace', NS)


class RenderedNamespaceAndJobs(GuardCase):

    def test_the_rendered_namespace_must_match_the_ask(self):
        iid = self.deployed_mr()
        self.gl.render_ns = 'ns-someone-else'
        self.refused('rendered_namespace_mismatch', 'check', '--mr', str(iid), '--namespace', NS)

    def test_a_plan_without_our_resources_is_unverified(self):
        iid = self.deployed_mr()
        self.gl.traces[self.gl.branch_sha(BRANCH)] = 'No changes.\n'
        self.refused('rendered_namespace_unverified', 'check', '--mr', str(iid), '--namespace', NS)

    def test_a_plan_that_misses_a_changed_entry_is_unverified(self):
        iid = self.deployed_mr()
        self.gl.traces[self.gl.branch_sha(BRANCH)] = (
            f'{NS}, wcep-other, DynamoGraphDeployment (nvidia.com) has been added:\n')
        self.refused('rendered_namespace_unverified', 'check', '--mr', str(iid), '--namespace', NS)

    def test_the_argocd_destination_must_match_the_ask(self):
        self.gl.commit(PLATFORM_PID, 'main', {'apps/dev.yaml': ARGO_APP % 'ns-elsewhere'})
        iid = self.deployed_mr()
        self.refused('argocd_destination_mismatch', 'check', '--mr', str(iid), '--namespace', NS)

    def test_no_argocd_source_is_unverified(self):
        self.write_config(argocd=False)
        iid = self.deployed_mr()
        self.refused('argocd_destination_unverified', 'check', '--mr', str(iid), '--namespace', NS)

    def test_the_shared_namespace_is_named_in_the_ask(self):
        self.gl.render_ns = SHARED_NS
        self.gl.commit(PLATFORM_PID, 'main', {'apps/dev.yaml': ARGO_APP % SHARED_NS})
        iid = self.deployed_mr()
        q = self.check(iid, ns=SHARED_NS)['question']
        self.assertIn("shared namespace", q)

    def test_red_or_missing_jobs_refused(self):
        cases = [('helm:plan', 'failed', 'job_not_passed'), ('validate:tests', 'failed', 'job_not_passed'),
                 ('validate:tests', 'running', 'job_not_passed'), ('helm:plan', None, 'job_missing'),
                 ('validate:tests', None, 'job_missing')]
        for name, status, code in cases:
            with self.subTest(name=name, status=status):
                branch = f'wcep/TEAM-910-j{len(self.gl.mrs)}'
                self.push(branch=branch)
                iid = self.open(branch)
                sha = self.gl.branch_sha(branch)
                if status:
                    self.gl.set_job(sha, name, status)
                else:
                    self.gl.drop_job(sha, name)
                self.refused(code, 'check', '--mr', str(iid), '--namespace', NS)

    def test_no_pipeline_for_the_head_refused(self):
        iid = self.deployed_mr()
        self.gl.no_pipeline.add(self.gl.branch_sha(BRANCH))
        self.refused('pipeline_missing', 'check', '--mr', str(iid), '--namespace', NS)


class TheMerge(GuardCase):

    def test_an_approved_merge_sends_the_head_as_sha_and_lands(self):
        iid, qid = self.approved()
        head = self.gl.branch_sha(BRANCH)
        body = self.merge(iid, qid)
        self.assertEqual(json.loads(self.merges()[-1]['body']), {'sha': head})
        self.assertEqual(self.gl.mrs[(PID, iid)]['state'], 'merged')
        self.assertEqual(body['result'], 'merged')
        self.assertIn('wcep-smoke', self.gl.file_at('main', DEV))

    def test_the_ask_command_asks_with_exactly_approve_and_reject(self):
        iid = self.deployed_mr()
        check = self.check(iid)
        self.ask(iid)
        argv = self.bb_calls()[-1]['argv']
        self.assertEqual(argv[:2], ['sgc', 'ask'])
        opts = [argv[i + 1] for i, a in enumerate(argv) if a == '--option']
        self.assertEqual(opts, ['Approve', 'Reject'])
        self.assertEqual(argv[argv.index('--recommend') + 1], 'none')
        self.assertEqual(argv[argv.index('--issue') + 1], ISSUE)
        self.assertEqual(argv[argv.index('--question') + 1], check['question'])

    def refused_merge(self, code, iid, qid):
        body = self.refused(code, 'merge', '--mr', str(iid), f'--answer={qid}')
        self.assertEqual(self.merges(), [], 'a refused merge reached GitLab')
        self.assertEqual(self.gl.mrs[(PID, iid)]['state'], 'opened')
        return body

    def test_no_answer_record_refused(self):
        # A forged Linear comment leaves no record in the plugin: answer-of fails.
        iid = self.deployed_mr()
        self.check(iid)
        self.refused_merge('answer_unknown', iid, 'bbq_forged1')

    def answer_of_replaced(self, body):
        # answer-of prints `body` (stdout, exit 0) or fails as the plugin does for a corrupt record.
        fake = FAKE_BB.replace("if a[:2] == ['sgc', 'answer-of'] and len(a) == 4 and a[3] == '--json':",
                               "if a[:2] == ['sgc', 'answer-of']:\n" + body + "\nif False:", 1)
        self.assertNotEqual(fake, FAKE_BB)
        (self.bin / 'bb').write_text(fake)

    def test_a_corrupt_record_refused_and_logged(self):
        iid, qid = self.approved()
        self.answer_of_replaced("    print(json.dumps({'code': 'corrupt'}), file=sys.stderr); sys.exit(1)")
        self.refused_merge('answer_unknown', iid, qid)
        line = self.log_lines()[-1]
        self.assertEqual((line['result'], line['code'], line['answer']), ('refused', 'answer_unknown', None))

    def test_output_that_does_not_parse_refused_and_logged(self):
        iid, qid = self.approved()
        for out in ("print('Approve')", "print(json.dumps(['Approve']))", "print('')"):
            with self.subTest(out=out):
                self.answer_of_replaced(f'    {out}; sys.exit(0)')
                self.refused_merge('answer_invalid', iid, qid)
                self.assertEqual(self.log_lines()[-1]['code'], 'answer_invalid')

    def test_an_open_question_refused(self):
        iid = self.deployed_mr()
        self.check(iid)
        qid = self.ask(iid)
        self.refused_merge('answer_not_answered', iid, qid)

    def test_reject_refused(self):
        iid = self.deployed_mr()
        self.check(iid)
        qid = self.ask(iid)
        self.answer(qid, letter='B')
        self.refused_merge('answer_not_approve', iid, qid)

    def test_free_text_refused_even_when_it_says_approve(self):
        iid = self.deployed_mr()
        self.check(iid)
        qid = self.ask(iid)
        self.answer(qid, letter=None, text='Approve')
        self.refused_merge('answer_not_approve', iid, qid)

    def test_an_option_that_only_looks_like_approve_refused(self):
        iid = self.deployed_mr()
        check = self.check(iid)
        for text in ('approve', 'Approve ', 'APPROVE', 'Approved'):
            with self.subTest(text=text):
                qid = self.add_question(check['question'], letter_text=text)
                self.refused_merge('answer_not_approve', iid, qid)

    def test_an_answer_from_linear_refused(self):
        iid = self.deployed_mr()
        self.check(iid)
        qid = self.ask(iid)
        self.answer(qid, channel='linear')
        self.refused_merge('answer_channel', iid, qid)

    def test_an_answer_to_another_thread_refused(self):
        iid = self.deployed_mr()
        self.check(iid)
        qid = self.ask(iid)
        self.answer(qid, thread=OTHER)
        self.refused_merge('answer_thread', iid, qid)

    def test_an_approve_for_another_merge_request_refused(self):
        other = 'wcep/TEAM-910-other'
        self.push(branch=other)
        other_iid = self.open(other)
        self.check(other_iid)
        other_qid = self.ask(other_iid)
        self.answer(other_qid)
        iid = self.deployed_mr(dev_deploy(entry=DEV_ENTRY.replace('wcep-smoke', 'wcep-smoke2')))
        self.check(iid)
        self.refused_merge('answer_mismatch', iid, other_qid)

    def test_an_iid_that_is_only_a_prefix_refused(self):
        iid = self.deployed_mr()
        check = self.check(iid)
        q = check['question'].replace(f'!{iid}', f'!{iid}0')
        self.refused_merge('answer_mismatch', iid, self.add_question(q))

    def test_short_commits_in_the_question_refused(self):
        iid = self.deployed_mr()
        check = self.check(iid)
        q = check['question'].replace(check['head'], check['head'][:12])
        self.refused_merge('answer_mismatch', iid, self.add_question(q))
        q = check['question'].replace(check['target'], check['target'][:12])
        self.refused_merge('answer_mismatch', iid, self.add_question(q))

    def test_an_approve_for_an_older_commit_refused(self):
        iid = self.deployed_mr()
        self.check(iid)
        old_q = self.ask(iid)
        self.answer(old_q)
        self.push(dev_deploy(entry=DEV_ENTRY.replace('replicas: 1', 'replicas: 2')))
        self.check(iid)
        self.refused_merge('answer_mismatch', iid, old_q)

    def test_without_a_check_refused(self):
        iid = self.deployed_mr()
        self.refused_merge('not_checked', iid, self.add_question(f'!{iid}'))

    def test_a_job_that_turned_red_after_the_ask_refused(self):
        iid, qid = self.approved()
        self.gl.set_job(self.gl.branch_sha(BRANCH), 'helm:plan', 'failed')
        self.refused_merge('job_not_passed', iid, qid)

    def test_outside_a_bb_thread_refused(self):
        iid, qid = self.approved()
        body = self.refused('thread_required', 'merge', '--mr', str(iid), '--answer', qid, thread=None)
        self.assertEqual(self.merges(), [])
        self.assertEqual(body['code'], 'thread_required')

    def test_a_malformed_answer_id_refused(self):
        iid, _ = self.approved()
        self.refused_merge('answer_id', iid, '=--json')
        self.refused_merge('answer_id', iid, 'bbq_x;rm')


class Races(GuardCase):

    def test_a_push_between_the_check_and_the_ask(self):
        iid = self.deployed_mr()
        self.check(iid)
        self.gl.commit(PID, BRANCH, {DEV: dev_deploy(entry=DEV_ENTRY.replace('replicas: 1', 'replicas: 5'))})
        self.refused('head_moved', 'ask', '--mr', str(iid))
        qid = self.add_question('stale')
        self.refused('head_moved', 'merge', '--mr', str(iid), '--answer', qid)
        self.assertEqual(self.merges(), [])

    def test_a_push_between_the_ask_and_the_merge(self):
        iid, qid = self.approved()
        self.gl.commit(PID, BRANCH, {DEV: dev_deploy(entry=DEV_ENTRY.replace('replicas: 1', 'replicas: 5'))})
        self.refused('head_moved', 'merge', '--mr', str(iid), '--answer', qid)
        self.assertEqual(self.merges(), [])
        self.assertEqual(self.gl.mrs[(PID, iid)]['state'], 'opened')

    def test_a_new_commit_on_main_after_the_approval(self):
        iid, qid = self.approved()
        self.gl.commit(PID, 'main', {'README.md': 'IT changed a shared template\n'})
        self.refused('main_moved', 'merge', '--mr', str(iid), '--answer', qid)
        self.assertEqual(self.merges(), [])

    def test_the_server_refuses_a_head_that_moves_inside_the_merge_call(self):
        # The guard re-reads the head, then calls merge with it as `sha`. If the branch
        # moves in between, GitLab answers 409; the fake does the same.
        iid, qid = self.approved()
        head = self.gl.branch_sha(BRANCH)
        original = self.gl.route

        def moving(method, parts, query, body, raw):
            if method == 'PUT' and parts[-1] == 'merge':
                self.gl.commit(PID, BRANCH, {DEV: dev_deploy(entry=DEV_ENTRY.replace('replicas: 1', 'replicas: 7'))})
            return original(method, parts, query, body, raw)
        self.gl.route = moving
        self.refused('merge_failed', 'merge', '--mr', str(iid), '--answer', qid)
        self.assertEqual(json.loads(self.merges()[-1]['body']), {'sha': head})
        self.assertEqual(self.gl.mrs[(PID, iid)]['state'], 'opened')


class SquashWorkflow(GuardCase):

    def test_deploy_then_teardown_leaves_main_without_the_entry(self):
        iid, qid = self.approved()
        self.merge(iid, qid)
        landed = self.main()
        self.assertIn('wcep-smoke', self.gl.file_at(landed, DEV))
        # IT works on main in the meantime, in the same file and elsewhere.
        self.gl.commit(PID, 'main', {'README.md': 'IT edit\n',
                                     DEV: self.gl.file_at(landed, DEV).replace('runAsUser: 1000',
                                                                                'runAsUser: 1001')})
        teardown = 'wcep/TEAM-910-teardown'
        body = self.ok('teardown', '--deploy-mr', str(iid), '--branch', teardown)
        self.assertEqual(body['base'], self.main())
        t_iid = self.open(teardown, kind='teardown', expires=None)
        check = self.check(t_iid)
        self.assertIn('wcep-smoke (removed)', check['question'])
        t_qid = self.ask(t_iid)
        self.answer(t_qid)
        self.merge(t_iid, t_qid)
        final = self.gl.file_at('main', DEV)
        self.assertNotIn('wcep', final)
        self.assertEqual(final, DEV_VALUES.replace('runAsUser: 1000', 'runAsUser: 1001'))
        self.assertEqual(rules.entries(rules.load_values(final)), {})

    def test_teardown_needs_a_landed_guard_merge(self):
        iid = self.deployed_mr()
        self.refused('not_merged', 'teardown', '--deploy-mr', str(iid), '--branch', 'wcep/TEAM-910-teardown')
        self.assertEqual(self.gl.writes()[-1]['path'].endswith('/merge_requests'), True)

    def test_a_teardown_kind_merge_request_needs_no_expiry(self):
        iid, qid = self.approved()
        self.merge(iid, qid)
        self.ok('teardown', '--deploy-mr', str(iid), '--branch', 'wcep/TEAM-910-teardown')
        t_iid = self.open('wcep/TEAM-910-teardown', kind='teardown', expires=None)
        q = self.check(t_iid)['question']
        self.assertIn('Kind: teardown', q)


class TokenAndLog(GuardCase):

    def full_cycle(self):
        iid, qid = self.approved()
        body = self.merge(iid, qid)
        return iid, qid, body

    def test_the_token_never_leaves_the_header(self):
        iid, qid = self.approved()
        self.refused('answer_unknown', 'merge', '--mr', str(iid), '--answer', 'bbq_nothing')
        self.merge(iid, qid)
        self.assertTrue(all(r['token'] for r in self.gl.requests))
        for r in self.gl.requests:
            self.assertNotIn(SECRET, r['path'] + r['body'])
        for out in self.outputs:
            self.assertNotIn(SECRET, out)
        for log in ('fleet.log', 'bb.log'):
            self.assertNotIn(SECRET, (self.dir / log).read_text())
        self.assertFalse(any(c['env_has_token'] for c in self.bb_calls()), 'bb inherited the token')
        for f in self.state_home.rglob('*'):
            if f.is_file():
                self.assertNotIn(SECRET, f.read_text(errors='replace'))
        fleet = [json.loads(line) for line in (self.dir / 'fleet.log').read_text().splitlines()]
        self.assertTrue(fleet)
        for argv in fleet:
            self.assertEqual(argv[:3], ['secret', 'env', f'ASTRA_GUARD_TOKEN={TOKEN_REF}'])

    def test_a_token_in_an_error_body_is_not_printed(self):
        iid = self.deployed_mr()
        original = self.gl.route

        def echo(method, parts, query, body, raw):
            if parts[-1] == 'merge_base':
                return 500, {}, {'message': f'boom {SECRET}'}
            return original(method, parts, query, body, raw)
        self.gl.route = echo
        rc, body, err = self.guard('check', '--mr', str(iid), '--namespace', NS)
        self.assertNotEqual(rc, 0)
        self.assertNotIn(SECRET, json.dumps(body) + err)

    def test_the_guard_only_calls_sgc_ask_and_answer_of(self):
        iid, qid = self.approved()
        self.refused('answer_unknown', 'merge', '--mr', str(iid), '--answer', 'bbq_nothing')
        self.merge(iid, qid)
        calls = [c['argv'] for c in self.bb_calls()]
        self.assertTrue(calls)
        for argv in calls:
            self.assertIn(argv[:2], (['sgc', 'ask'], ['sgc', 'answer-of']), argv)
        source = GUARD.read_text() + (SKILL / 'scripts/astra_guard_rules.py').read_text()
        for forbidden in ('sqlite', 'interactions', 'respond', "'sgc', 'answer'", '"sgc", "answer"'):
            self.assertNotIn(forbidden, source)

    def test_a_merge_writes_an_attempt_line_then_a_result_line(self):
        # Write-ahead log (steward ruling): an attempt line before the merge call, the result after.
        iid, qid, body = self.full_cycle()
        lines = self.log_lines()
        self.assertEqual([l['result'] for l in lines], ['attempt', 'merged'])
        self.assertEqual((lines[0]['ask_id'], lines[0]['mr'], lines[0]['head'], lines[0]['target']),
                         (qid, iid, lines[1]['head'], lines[1]['target']))
        line = lines[1]
        for key in ('time', 'ask_id', 'answer', 'mr', 'head', 'target', 'result'):
            self.assertIn(key, line)
        self.assertEqual(line['ask_id'], qid)
        self.assertEqual(line['mr'], iid)
        self.assertEqual(line['result'], 'merged')
        self.assertEqual(set(line['answer']), {'status', 'channel', 'option', 'answeredAt'})
        self.assertEqual(line['answer']['option'], {'letter': 'A', 'text': 'Approve'})
        self.assertEqual(line['head'], self.gl.mrs[(PID, iid)]['final_sha'])

    def test_a_refused_merge_writes_one_line_with_the_reason(self):
        iid = self.deployed_mr()
        self.check(iid)
        qid = self.ask(iid)
        self.answer(qid, letter='B')
        self.refused('answer_not_approve', 'merge', '--mr', str(iid), '--answer', qid)
        lines = self.log_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]['result'], 'refused')
        self.assertEqual(lines[0]['code'], 'answer_not_approve')
        self.assertTrue(lines[0]['reason'])
        self.assertEqual(lines[0]['answer']['option'], {'letter': 'B', 'text': 'Reject'})

    def test_the_comment_names_the_ask_the_mr_both_commits_and_the_result(self):
        iid, qid, body = self.full_cycle()
        c = body['comment']
        line = self.log_lines()[0]
        for part in (qid, f'!{iid}', line['head'], line['target'], 'merged', ISSUE):
            self.assertIn(part, c)
        self.assertTrue(c.startswith(f'[agent:claude-code {ME}]'))
        self.assertNotIn(SECRET, c)
        iid2 = self.deployed_mr(self.gl.file_at('main', DEV) + '  - name: wcep-other\n    enabled: true\n')
        refused = self.refused('not_checked', 'merge', '--mr', str(iid2), '--answer', qid)
        self.assertIn('refused', refused['comment'])
        self.assertIn('not_checked', refused['comment'])
        self.assertNotIn(SECRET, refused['comment'])


class StructuralRules(GuardCase):
    """One test per structural rule of the redesign after review pass 3. Each fails on the
    pre-redesign head and passes after it. The rules and why point patches were not enough
    are in references/astra-guard.md, "Five kinds, five rules"."""

    def test_values_are_json_shaped(self):
        for value in ('!!set {a: null}', '2026-11-05', '2026-11-05T00:00:00Z', '!!binary aGk=', '{1: x}',
                      '{true: x}', '!!omap [{a: 1}]'):
            with self.subTest(value=value):
                with self.assertRaises(rules.Refusal) as cm:
                    rules.load_values(f'itSetting: {value}\n')
                self.assertEqual(cm.exception.code, 'yaml_invalid')
        entry = DEV_ENTRY + '    started: 2026-11-05\n'
        self.gl.commit(PID, BRANCH, {DEV: dev_deploy(entry=entry)}, parent=self.main())
        iid = self.open()
        self.refused('yaml_invalid', 'check', '--mr', str(iid), '--namespace', NS)

    def guard_module(self):
        loader = importlib.machinery.SourceFileLoader('astra_guard_sinks', str(GUARD))
        spec = importlib.util.spec_from_loader('astra_guard_sinks', loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        mod._TOKEN = SECRET

        # This runs in the test process, whose PATH holds the real bb. A guard that reaches
        # a subprocess here would ask the owner a real question, so no subprocess may run.
        class NoSubprocess:
            TimeoutExpired = subprocess.TimeoutExpired
            DEVNULL = subprocess.DEVNULL

            @staticmethod
            def run(argv, **kwargs):
                raise AssertionError(f'the sink test reached a subprocess: {argv[:3]}')
        mod.subprocess = NoSubprocess
        return mod

    def test_every_sink_refuses_the_token_raw_or_url_encoded(self):
        mod = self.guard_module()
        encoded = ''.join(f'%{ord(c):02X}' for c in SECRET)
        old = os.environ.get('XDG_STATE_HOME')
        os.environ['XDG_STATE_HOME'] = str(self.state_home)
        try:
            for form in (SECRET, encoded):
                with self.subTest(sink='state.json', form=form[:6]):
                    with mod.State() as st:
                        st.data['mrs']['x'] = {'note': f'nested {form}'}
                        with self.assertRaises(rules.Refusal) as cm:
                            st.save()
                    self.assertEqual(cm.exception.code, 'secret_at_sink')
                with self.subTest(sink='approvals.jsonl', form=form[:6]):
                    with mod.open_log() as log:
                        mod.write_log(log, {'time': 't', 'ask_id': 'bbq_x1', 'answer': {'option': {'text': form}},
                                            'mr': 7, 'head': 'a' * 40, 'target': 'b' * 40, 'result': 'refused',
                                            'code': 'answer_not_approve', 'reason': 'r', 'merge_commit': None})
                    last = json.loads((self.state_home / 'astra-guard/approvals.jsonl').read_text().splitlines()[-1])
                    self.assertEqual((last['mr'], last['code'], last['answer']), (7, 'answer_not_approve', None))
                with self.subTest(sink='bb argv', form=form[:6]):
                    with self.assertRaises(rules.Refusal) as cm:
                        mod.bb_ask(f'question {form}', ISSUE)
                    self.assertEqual(cm.exception.code, 'secret_at_sink')
                with self.subTest(sink='stdout', form=form[:6]):
                    import contextlib
                    import io
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        mod.emit({'body': {'deep': [form]}})
                    self.assertNotIn(SECRET, urllib.parse.unquote(buf.getvalue()))
                    self.assertEqual(json.loads(buf.getvalue())['code'], 'secret_at_sink')
            for f in (self.state_home / 'astra-guard').iterdir():
                if f.is_file():
                    self.assertNotIn(SECRET, urllib.parse.unquote(f.read_text()))
        finally:
            if old is None:
                os.environ.pop('XDG_STATE_HOME', None)
            else:
                os.environ['XDG_STATE_HOME'] = old

    def test_a_header_shaped_line_parses_completely_or_refuses(self):
        for line in ('ns-a, wcep-x, DynamoGraphDeployment (nvidia.com) has been add',
                     'ns-a, wcep-x, DynamoGraphDeployment (',
                     'ns-a, wcep x, DynamoGraphDeployment (nvidia.com) has been added:',
                     'ns-a, wcep-x, DynamoGraphDeployment (nvidia.com) has been added: trailing',
                     'ns a, wcep-x, Service (v1) has changed:'):
            with self.subTest(line=line):
                with self.assertRaises(rules.Refusal) as cm:
                    rules.parse_plan(f'{NS}, wcep-ok, Service (v1) has been added:\n{line}\n')
                self.assertEqual(cm.exception.code, 'rendered_namespace_unverified')

    def test_the_merge_opens_the_log_before_any_gitlab_call(self):
        iid, qid = self.approved()
        log = self.state_home / 'astra-guard' / 'approvals.jsonl'
        log.write_text('')
        log.chmod(0o444)
        before = len(self.gl.requests)
        body = self.refused('log_unusable', 'merge', '--mr', str(iid), '--answer', qid)
        self.assertEqual(self.gl.requests[before:], [], 'the merge reached GitLab before its log was open')
        self.assertIn('comment', body)
        self.assertEqual(self.gl.mrs[(PID, iid)]['state'], 'opened')

    def test_a_deploy_only_adds_entries(self):
        self.gl.commit(PID, 'main', {DEV: dev_deploy()})
        changed = dev_deploy().replace('replicas: 1', 'replicas: 3')
        swapped = dev_deploy(entry=DEV_ENTRY.replace('wcep-smoke', 'wcep-other'))
        for n, text in enumerate((changed, swapped)):
            with self.subTest(case=n):
                branch = f'wcep/TEAM-910-add{n}'
                self.gl.commit(PID, branch, {DEV: text}, parent=self.main())
                iid = self.open(branch)
                self.refused('deploy_not_addition', 'check', '--mr', str(iid), '--namespace', NS)
        hunks = rules.inverse_patch(DEV_VALUES, dev_deploy())
        undone = rules.entries(rules.load_values(rules.apply_patch(dev_deploy(), hunks)))
        self.assertEqual(undone, {}, 'the inverse of an add-only deploy is a pure removal')


class Invariants(GuardCase):
    """One test per invariant from the redesign after review R1. Each fails on 42796a6 and
    passes after. The reasons are in references/astra-guard.md, "Five kinds, five rules"."""

    def test_every_plan_resource_is_in_the_asked_namespace(self):
        ok = (NS, 'wcep-x', 'DynamoGraphDeployment', 'added')
        rules.check_plan([ok, (NS, 'chart-helper', 'ConfigMap', 'added')], ['wcep-x'], NS)
        for other in ((SHARED_NS, 'chart-helper', 'Deployment', 'added'),
                      ('', 'chart-helper', 'ClusterRole', 'added'),
                      ('', 'wcep-x-admin', 'ClusterRoleBinding', 'removed')):
            with self.subTest(other=other):
                with self.assertRaises(rules.Refusal) as cm:
                    rules.check_plan([ok, other], ['wcep-x'], NS)
                self.assertEqual(cm.exception.code, 'rendered_namespace_mismatch')

    def test_the_attempt_line_is_on_disk_before_the_merge_call(self):
        iid, qid = self.approved()
        log = self.state_home / 'astra-guard' / 'approvals.jsonl'
        seen = []
        original = self.gl.route

        def look(method, parts, query, body, raw):
            if method == 'PUT' and parts[-1] == 'merge':
                seen.append([json.loads(l) for l in log.read_text().splitlines()] if log.exists() else [])
            return original(method, parts, query, body, raw)
        self.gl.route = look
        self.merge(iid, qid)
        self.assertEqual(len(seen), 1)
        self.assertEqual([(l['result'], l['ask_id'], l['mr'], l['head']) for l in seen[0]],
                         [('attempt', qid, iid, self.gl.mrs[(PID, iid)]['final_sha'])])

    def test_fleet_runs_as_a_child_and_never_runs_the_guard(self):
        self.ok('get', f'/projects/{PID}')
        calls = [json.loads(line) for line in (self.dir / 'fleet.log').read_text().splitlines()]
        self.assertEqual(len(calls), 1)
        argv = calls[0]
        self.assertEqual(argv[:4], ['secret', 'env', f'ASTRA_GUARD_TOKEN={TOKEN_REF}', '--'])
        self.assertNotIn(str(GUARD), argv, 'the guard re-ran itself under fleet')
        self.assertNotIn(SECRET, json.dumps(argv))
        self.assertTrue(self.gl.requests and all(r['token'] for r in self.gl.requests))

    def test_teardown_proves_the_merge_from_gitlab(self):
        iid, qid = self.approved()
        self.merge(iid, qid)
        state = self.state_home / 'astra-guard' / 'state.json'
        st = json.loads(state.read_text())
        for rec in st['mrs'].values():
            rec.pop('merged', None)  # the local record is a convenience only
        state.write_text(json.dumps(st))
        self.ok('teardown', '--deploy-mr', str(iid), '--branch', 'wcep/TEAM-910-teardown')
        self.assertEqual(self.gl.file_at('wcep/TEAM-910-teardown', DEV), DEV_VALUES)
        # GitLab must show the checked head as the merged one.
        self.gl.mrs[(PID, iid)]['final_sha'] = 'c' * 40
        self.refused('landed_head_mismatch', 'teardown', '--deploy-mr', str(iid), '--branch', 'wcep/TEAM-910-t2')


class PureRules(unittest.TestCase):

    def test_duplicate_keys_and_multiple_documents_refused(self):
        for text in ('a: 1\na: 2\n', 'a: 1\n---\nb: 2\n', 'a: !!python/object:os.system x\n'):
            with self.subTest(text=text):
                with self.assertRaises(rules.Refusal) as cm:
                    rules.load_values(text)
                self.assertEqual(cm.exception.code, 'yaml_invalid')

    def test_inverse_patch_applies_after_unrelated_changes(self):
        base, head = DEV_VALUES, dev_deploy()
        hunks = rules.inverse_patch(base, head)
        moved = head.replace('runAsUser: 1000', 'runAsUser: 1001') + '# IT appended this\n'
        self.assertEqual(rules.apply_patch(moved, hunks),
                         base.replace('runAsUser: 1000', 'runAsUser: 1001') + '# IT appended this\n')

    def test_inverse_patch_refuses_an_ambiguous_or_missing_match(self):
        hunks = rules.inverse_patch(DEV_VALUES, dev_deploy())
        with self.assertRaises(rules.Refusal) as cm:
            rules.apply_patch(DEV_VALUES, hunks)
        self.assertEqual(cm.exception.code, 'inverse_patch_conflict')

    def test_plan_parser_reads_helm_diff_headers(self):
        trace = ('\x1b[0Ksection_start:1:x\r\x1b[0K\n'
                 '\x1b[33mns-a, wcep-x, DynamoGraphDeployment (nvidia.com) has been added:\x1b[0m\n'
                 'ns-b, it-thing, Service (v1) has changed:\n')
        self.assertEqual(rules.parse_plan(trace),
                         [('ns-a', 'wcep-x', 'DynamoGraphDeployment', 'added'),
                          ('ns-b', 'it-thing', 'Service', 'changed')])


class AdversarialReview(GuardCase):

    def test_a_token_echoed_by_gitlab_does_not_reach_state_or_bb_arguments(self):
        iid = self.deployed_mr()
        self.gl.mrs[(PID, iid)]['web_url'] += f'?diagnostic={SECRET}'
        # The guard refuses a response that echoes the token rather than rewriting it.
        self.refused('secret_in_response', 'check', '--mr', str(iid), '--namespace', NS)
        self.refused('not_checked', 'ask', '--mr', str(iid))
        exposed = [path.name for path in (self.state_home / 'astra-guard').iterdir()
                   if path.is_file() and SECRET in path.read_text()]
        exposed += ['bb argv' for call in self.bb_calls() if SECRET in json.dumps(call['argv'])]
        self.assertEqual(exposed, [], 'A reflected token must be redacted before saving or spawning bb')

    def test_a_cluster_scoped_resource_is_not_hidden_by_a_valid_plan_line(self):
        iid = self.deployed_mr()
        head = self.gl.branch_sha(BRANCH)
        self.gl.traces[head] = (
            f'{NS}, wcep-smoke, DynamoGraphDeployment (nvidia.com) has been added:\n'
            ', wcep-smoke-admin, ClusterRole (rbac.authorization.k8s.io/v1) has been added:\n')
        rc, body, err = self.guard('check', '--mr', str(iid), '--namespace', NS)
        self.assertNotEqual(rc, 0, f'A resource outside the namespace was allowed: {body} {err}')
        self.assertIn(body.get('code'), ('rendered_namespace_mismatch', 'rendered_namespace_unverified'))

    def test_successful_required_jobs_on_page_two_are_found(self):
        iid = self.deployed_mr()
        head = self.gl.branch_sha(BRANCH)
        self.gl.jobs[head] = {'helm:plan': 'success', 'validate:tests': 'success',
                              **{f'other:{n}': 'success' for n in range(100)}}
        original = self.gl.route
        snapshots = {}

        def paginated_jobs(method, parts, query, body, raw):
            if method == 'GET' and parts[2:3] == ['pipelines'] and parts[-1] == 'jobs':
                pipeline = parts[3]
                if pipeline not in snapshots:
                    status, headers, jobs = original(method, parts, query, body, raw)
                    snapshots[pipeline] = sorted(jobs, key=lambda job: job['id'], reverse=True)
                jobs = snapshots[pipeline]
                size = int(query.get('per_page', ['20'])[0])
                page = int(query.get('page', ['1'])[0])
                headers = {'X-Next-Page': str(page + 1) if page * size < len(jobs) else ''}
                return 200, headers, jobs[(page - 1) * size:page * size]
            return original(method, parts, query, body, raw)

        self.gl.route = paginated_jobs
        self.check(iid)

    def test_a_truncated_merge_response_still_logs_the_attempt(self):
        iid, qid = self.approved()
        handler_class = self.gl.server.RequestHandlerClass
        original = handler_class.do_PUT
        fake = self.gl

        def truncated_response(handler):
            if not handler.path.endswith('/merge'):
                return original(handler)
            length = int(handler.headers.get('Content-Length') or 0)
            body = handler.rfile.read(length).decode()
            with fake.lock:
                status, headers, payload = fake.handle('PUT', handler.path, body, handler.headers)
            data = json.dumps(payload).encode()
            handler.send_response(status)
            for key, value in headers.items():
                handler.send_header(key, value)
            handler.send_header('Content-Length', str(len(data)))
            handler.end_headers()
            handler.wfile.write(data[:-1])
            handler.close_connection = True

        handler_class.do_PUT = truncated_response
        rc, body, err = self.guard('merge', '--mr', str(iid), '--answer', qid)
        self.assertNotEqual(rc, 0, (body, err))
        self.assertEqual(self.gl.mrs[(PID, iid)]['state'], 'merged')
        lines = self.log_lines()
        # Write-ahead log: the attempt line, then the result (unknown outcome).
        self.assertEqual([l['result'] for l in lines], ['attempt', 'refused'],
                         f'The merge reached GitLab but has no audit line: {body}')
        self.assertEqual(lines[1]['code'], 'merge_unknown')
        self.assertEqual(lines[0]['ask_id'], qid)
        self.assertIn('comment', body)

    def test_it_values_cannot_change_from_a_boolean_to_a_number(self):
        base = DEV_VALUES + 'itFeature: true\n'
        head = dev_deploy(base).replace('itFeature: true', 'itFeature: 1')
        self.assertIs(rules.load_values(base)['itFeature'], True)
        self.assertIs(type(rules.load_values(head)['itFeature']), int)
        self.gl.commit(PID, 'main', {DEV: base})
        self.gl.commit(PID, BRANCH, {DEV: head}, parent=self.main())
        iid = self.open()
        self.refused('outside_entries', 'check', '--mr', str(iid), '--namespace', NS)

    def test_dev_cannot_request_a_mig_gpu(self):
        entry = DEV_ENTRY.replace('requests: { cpu: "1", memory: "2Gi" }',
                                  'requests: { cpu: "1", memory: "2Gi" }\n'
                                  '        limits: { nvidia.com/mig-1g.5gb: "1" }')
        self.gl.commit(PID, BRANCH, {DEV: dev_deploy(entry=entry)}, parent=self.main())
        iid = self.open()
        self.refused('dev_gpu', 'check', '--mr', str(iid), '--namespace', NS)

    def test_teardown_cannot_remove_an_entry_named_prd(self):
        values = dev_deploy(entry=DEV_ENTRY.replace('wcep-smoke', 'wcep-smoke-prd'))
        self.gl.commit(PID, 'main', {DEV: values})
        self.gl.commit(PID, BRANCH, {DEV: DEV_VALUES}, parent=self.main())
        iid = self.open(kind='teardown', expires=None)
        self.refused('entry_name', 'check', '--mr', str(iid), '--namespace', NS)


class PassTwoReview(GuardCase):

    def test_failed_check_after_merge_preserves_the_teardown_patch(self):
        iid, qid = self.approved()
        self.merge(iid, qid)
        self.refused('mr_not_open', 'check', '--mr', str(iid), '--namespace', NS)
        teardown = 'wcep/TEAM-910-teardown'
        body = self.ok('teardown', '--deploy-mr', str(iid), '--branch', teardown)
        self.assertEqual(body['base'], self.main())
        self.assertEqual(self.gl.file_at(teardown, DEV), DEV_VALUES)

    def test_merge_config_refusal_is_logged_and_signed(self):
        iid, qid = self.approved()
        self.config.write_text('project_id = 0\n')
        body = self.refused('config_invalid', 'merge', '--mr', str(iid), '--answer', qid)
        self.assertEqual(self.merges(), [])
        lines = self.log_lines()
        self.assertEqual(len(lines), 1, 'A merge refusal before cmd_merge still needs an audit line')
        self.assertEqual((lines[0]['mr'], lines[0]['ask_id'], lines[0]['result']), (iid, qid, 'refused'))
        self.assertIn('comment', body)

    def test_empty_ask_namespace_cannot_authorize_cluster_scoped_resources(self):
        self.gl.commit(PLATFORM_PID, 'main', {'apps/dev.yaml': ARGO_APP % '""'})
        iid = self.deployed_mr()
        self.gl.traces[self.gl.branch_sha(BRANCH)] = (
            ', wcep-smoke, ClusterRole (rbac.authorization.k8s.io/v1) has been added:\n')
        rc, body, err = self.guard('check', '--mr', str(iid), '--namespace', '')
        self.assertNotEqual(rc, 0, 'An empty ask namespace authorized a cluster-scoped resource')

    def test_response_redaction_cannot_hide_an_it_value_change(self):
        base = DEV_VALUES + f'itSetting: "{SECRET}"\n'
        head = dev_deploy(base).replace(SECRET, '[redacted]')
        self.gl.commit(PID, 'main', {DEV: base})
        self.gl.commit(PID, BRANCH, {DEV: head}, parent=self.main())
        iid = self.open()
        rc, body, err = self.guard('check', '--mr', str(iid), '--namespace', NS)
        self.assertNotEqual(rc, 0, 'Redaction hid a change outside the wcep entries')

    def test_json_escaped_token_echo_is_redacted_before_state_and_bb(self):
        iid = self.deployed_mr()
        self.gl.mrs[(PID, iid)]['web_url'] += f'?diagnostic={SECRET}'
        original = self.gl.route

        def escaped_echo(method, parts, query, body, raw):
            status, headers, payload = original(method, parts, query, body, raw)
            if method == 'GET' and parts == ['projects', str(PID), 'merge_requests', str(iid)]:
                wire = json.dumps(payload).replace(SECRET, f'\\u{ord(SECRET[0]):04x}{SECRET[1:]}')
                self.assertFalse(SECRET in wire)
                self.assertTrue(SECRET in json.loads(wire)['web_url'])
                return status, headers, wire
            return status, headers, payload

        self.gl.route = escaped_echo
        # The guard refuses a response that echoes the token rather than rewriting it.
        self.refused('secret_in_response', 'check', '--mr', str(iid), '--namespace', NS)
        self.refused('not_checked', 'ask', '--mr', str(iid))
        exposed = [path.name for path in (self.state_home / 'astra-guard').iterdir()
                   if path.is_file() and SECRET in path.read_text()]
        exposed += ['bb argv' for call in self.bb_calls() if SECRET in json.dumps(call['argv'])]
        self.assertEqual(exposed, [], 'JSON escapes must not bypass response redaction')


class PassThreeReview(GuardCase):

    def test_it_set_members_cannot_change_from_boolean_to_integer(self):
        base = DEV_VALUES + 'itSetting: !!set {true: null}\n'
        head = dev_deploy(base).replace('!!set {true: null}', '!!set {1: null}')
        # Values are JSON-shaped since the redesign: a set is refused at load, so no comparison
        # ever sees one.
        for text in (base, head):
            with self.assertRaises(rules.Refusal) as cm:
                rules.load_values(text)
            self.assertEqual(cm.exception.code, 'yaml_invalid')
        self.gl.commit(PID, 'main', {DEV: base})
        self.gl.commit(PID, BRANCH, {DEV: head}, parent=self.main())
        iid = self.open()
        self.refused('yaml_invalid', 'check', '--mr', str(iid), '--namespace', NS)

    def test_merge_refuses_before_writing_when_approval_log_is_unusable(self):
        iid, qid = self.approved()
        before = self.main()
        log = self.state_home / 'astra-guard' / 'approvals.jsonl'
        log.mkdir()
        rc, body, err = self.guard('merge', '--mr', str(iid), '--answer', qid)
        self.assertNotEqual(rc, 0, (body, err))
        self.assertEqual(self.merges(), [], 'The guard merged before discovering that it cannot audit the merge')
        self.assertEqual(self.main(), before)
        self.assertEqual(self.gl.mrs[(PID, iid)]['state'], 'opened')

    def test_truncated_resource_header_cannot_hide_a_cluster_scoped_resource(self):
        iid = self.deployed_mr()
        head = self.gl.branch_sha(BRANCH)
        self.gl.traces[head] = (
            f'{NS}, wcep-smoke, DynamoGraphDeployment (nvidia.com) has been added:\n'
            ', wcep-smoke-admin, ClusterRole (rbac.authorization.k8s.io/v1) has been added')
        rc, body, err = self.guard('check', '--mr', str(iid), '--namespace', NS)
        self.assertNotEqual(rc, 0, 'A clipped resource header hid a resource outside the namespace')
        self.assertIn(body.get('code'), ('rendered_namespace_unverified', 'rendered_namespace_mismatch'),
                      (body, err))

    def test_changed_deploy_can_apply_its_recorded_teardown(self):
        # Ruling A: a deploy only adds entries. A change is a teardown and then a new deploy,
        # so a deploy that changes an entry is refused at check and never needs a restoring teardown.
        self.gl.commit(PID, 'main', {DEV: dev_deploy()})
        changed = dev_deploy().replace('replicas: 1', 'replicas: 2')
        self.refused('deploy_not_addition', 'push', '--branch', BRANCH, '--base', self.main(),
                     '--file', f'{DEV}={self.local(changed)}')
        self.gl.commit(PID, BRANCH, {DEV: changed}, parent=self.main())
        iid = self.open()
        self.refused('deploy_not_addition', 'check', '--mr', str(iid), '--namespace', NS)

    def test_url_escaped_token_in_http_error_never_reaches_output_or_log(self):
        iid, qid = self.approved()
        escaped = ''.join(f'%{ord(c):02X}' for c in SECRET)
        self.assertNotIn(SECRET, escaped)
        self.assertEqual(urllib.parse.unquote(escaped), SECRET)
        original = self.gl.route

        def echo_error(method, parts, query, body, raw):
            if method == 'GET' and parts[-1] == 'merge_base':
                return 500, {}, {'message': f'diagnostic={escaped}'}
            return original(method, parts, query, body, raw)

        self.gl.route = echo_error
        rc, body, err = self.guard('merge', '--mr', str(iid), '--answer', qid)
        self.assertNotEqual(rc, 0, (body, err))
        self.assertEqual(self.merges(), [])
        self.assertEqual(len(self.log_lines()), 1)
        exposed = ['output' for out in self.outputs if SECRET in urllib.parse.unquote(out)]
        exposed += [path.name for path in (self.state_home / 'astra-guard').iterdir()
                    if path.is_file() and SECRET in urllib.parse.unquote(path.read_text())]
        self.assertEqual(exposed, [], 'HTTP errors must not retain an encoded token')

    def test_yaml_escaped_response_token_never_reaches_state_or_bb_argv(self):
        escaped = ''.join(f'\\x{ord(c):02x}' for c in SECRET)
        values = stg_deploy().replace('gpuCount: "2"', f'gpuType: "{escaped}"')
        self.assertNotIn(SECRET, values)
        self.assertEqual(rules.load_values(values)['mockerRepros'][-1]['gpuType'], SECRET)
        self.gl.commit(PID, BRANCH, {STG: values}, parent=self.main())
        iid = self.open()
        rc, body, err = self.guard('check', '--mr', str(iid), '--namespace', NS)
        if rc == 0:
            self.ask(iid)
        else:
            self.assertIn(body.get('code'), ('secret_in_response', 'secret_at_sink'), (body, err))
        exposed = [path.name for path in (self.state_home / 'astra-guard').iterdir()
                   if path.is_file() and SECRET in path.read_text()]
        exposed += ['bb argv' for call in self.bb_calls() if SECRET in json.dumps(call['argv'])]
        self.assertEqual(exposed, [], 'YAML decoding must not put the token in state or bb arguments')


class RedesignReview(GuardCase):

    def test_a_landed_merge_with_a_lost_response_can_still_be_torn_down(self):
        iid, qid = self.approved()
        original = self.gl.route

        def lose_merge_response(method, parts, query, body, raw):
            status, headers, payload = original(method, parts, query, body, raw)
            if method == 'PUT' and parts == ['projects', str(PID), 'merge_requests', str(iid), 'merge']:
                return status, headers, json.dumps(payload)[:-1]
            return status, headers, payload

        self.gl.route = lose_merge_response
        self.refused('merge_unknown', 'merge', '--mr', str(iid), '--answer', qid)
        self.assertEqual([l['result'] for l in self.log_lines()], ['attempt', 'refused'])
        mr = self.ok('get', f'/projects/{PID}/merge_requests/{iid}')['body']
        self.assertEqual(mr['state'], 'merged')
        self.assertIn('wcep-smoke', self.gl.file_at('main', DEV))
        branch = 'wcep/TEAM-910-teardown'

        teardown = self.ok('teardown', '--deploy-mr', str(iid), '--branch', branch)

        self.assertEqual(teardown['base'], self.main())
        self.assertEqual(self.gl.file_at(branch, DEV), DEV_VALUES)

    def test_plan_resources_without_wcep_names_still_need_the_asked_namespace(self):
        iid = self.deployed_mr()
        head = self.gl.branch_sha(BRANCH)
        self.check(iid)
        for namespace, kind in (('ns-someone-else', 'Deployment'), ('', 'ClusterRole')):
            with self.subTest(namespace=namespace, kind=kind):
                trace = (f'{NS}, wcep-smoke, DynamoGraphDeployment (nvidia.com) has been added:\n'
                         f'{namespace}, chart-helper, {kind} (example.test/v1) has been added:\n')
                self.assertEqual(rules.parse_plan(trace)[-1],
                                 (namespace, 'chart-helper', kind, 'added'))
                self.gl.traces[head] = trace

                rc, body, err = self.guard('check', '--mr', str(iid), '--namespace', NS)

                self.assertNotEqual(rc, 0,
                                    f'A fully parsed resource outside the namespace was ignored: {body} {err}')

    def test_a_fleet_failure_still_logs_and_signs_the_merge_refusal(self):
        iid, qid = self.approved()
        before = self.main()
        (self.bin / 'fleet').write_text(
            '#!/usr/bin/env python3\n'
            'import sys\n'
            'print("fake fleet: secret broker unavailable", file=sys.stderr)\n'
            'sys.exit(23)\n')

        rc, body, err = self.guard('merge', '--mr', str(iid), '--answer', qid)

        self.assertNotEqual(rc, 0, (body, err))
        self.assertEqual(self.merges(), [])
        self.assertEqual(self.main(), before)
        lines = self.log_lines()
        self.assertEqual(len(lines), 1, 'A failed fleet exec bypassed the merge refusal log')
        self.assertEqual((lines[0]['ask_id'], lines[0]['mr'], lines[0]['result']),
                         (qid, iid, 'refused'))
        self.assertIn('comment', body)

    def test_merge_refuses_a_log_that_opens_but_cannot_write(self):
        iid, qid = self.approved()
        before = self.main()
        log = self.state_home / 'astra-guard' / 'approvals.jsonl'
        self.assertTrue(Path('/dev/full').is_char_device())
        log.symlink_to('/dev/full')

        rc, body, err = self.guard('merge', '--mr', str(iid), '--answer', qid)

        self.assertNotEqual(rc, 0, (body, err))
        self.assertEqual(self.merges(), [],
                         'Opening the log succeeded, but the guard merged before discovering ENOSPC')
        self.assertEqual(self.main(), before)
        self.assertEqual(self.gl.mrs[(PID, iid)]['state'], 'opened')


class RoundTwoReview(GuardCase):

    def test_gpu_summary_keeps_same_named_entries_in_different_lists(self):
        values = rules.load_values(stg_deploy())
        values['nimModels'].append({'name': 'wcep-shadow', 'gpuCount': '8'})
        iid = self.deployed_mr(json.dumps(values), path=STG)

        question = self.check(iid)['question']

        gpu_line = next(line for line in question.splitlines() if line.startswith('GPUs '))
        self.assertIn('gpuCount=2', gpu_line)
        self.assertIn('gpuCount=8', gpu_line)

    def test_push_cannot_commit_a_yaml_escaped_token(self):
        escaped = ''.join(f'\\x{ord(c):02x}' for c in SECRET)
        values = stg_deploy().replace('gpuCount: "2"', f'gpuType: "{escaped}"')
        self.assertNotIn(SECRET, values)
        self.assertEqual(rules.load_values(values)['mockerRepros'][-1]['gpuType'], SECRET)

        rc, body, err = self.guard('push', '--branch', BRANCH, '--base', self.main(),
                                   '--file', f'{STG}={self.local(values)}')

        if rc == 0:
            committed = rules.load_values(self.gl.file_at(body['commit'], STG))
            self.assertEqual(committed['mockerRepros'][-1]['gpuType'], SECRET)
        self.assertNotEqual(rc, 0, f'The commits API stored a YAML value containing the token: {body} {err}')
        self.assertEqual(self.gl.writes(), [])

    def test_json_exponent_cannot_change_to_a_string_outside_our_entries(self):
        base = rules.load_values(DEV_VALUES)
        base['itSetting'] = 1000.0
        base_text = json.dumps(base).replace('1000.0', '1e3')
        head = json.loads(base_text)
        head['itSetting'] = '1e3'
        head['mockerRepros'] = rules.load_values(dev_deploy())['mockerRepros']
        head_text = json.dumps(head)
        self.assertIsInstance(json.loads(base_text)['itSetting'], float)
        self.assertIsInstance(json.loads(head_text)['itSetting'], str)
        self.gl.commit(PID, 'main', {DEV: base_text})
        self.gl.commit(PID, BRANCH, {DEV: head_text}, parent=self.main())
        iid = self.open()

        self.refused('outside_entries', 'check', '--mr', str(iid), '--namespace', NS)


if __name__ == '__main__':
    unittest.main()

"""k8s-guard: the only write path agents have to a shared Kubernetes cluster.

Every case runs the real script against fakes on PATH, and never a real cluster:

- a stateful fake `kubectl` that holds namespaces, objects and pods in a JSON
  file, answers server-side dry runs, and refuses any write to a Namespace
  after creation (the bot's rights end at its RoleBindings inside the namespace);
- a fake `bb` that holds leases and the one operation slot of each, as the
  k8s-leases contract describes them;
- a fake `fleet` that prints a sentinel secret;
- a fake `tsh` that fails every test that reaches it.

PATH holds only the fakes and /usr/bin:/bin, so the real kubectl and tsh in
/usr/local/bin can never run. HOME, the profiles and KUBECONFIG all point into
a temporary directory. Every name here is made up.
"""
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'dot_agents/skills/k8s-clusters'
GUARD = SKILL / 'scripts/executable_k8s-guard'
EXAMPLE = SKILL / 'references/profiles.example.toml'

CTX = 'proxy.example.test-lab-arm-a'      # profile lab-arm-a: gb300, 4 per node, writable
PROFILE = 'lab-arm-a'
CTX_QA = 'proxy.example.test-lab-x86-qa'  # bot_can_write = false
CTX_CI = 'proxy.example.test-lab-ci'      # allowed = never
CTX_B200 = 'proxy.example.test-lab-b200'  # added below: b200, 8 per node
ME = 'thr_alpha1'
OTHER = 'thr_bravo2'
PREFIX = 'dyntest-alice-'
NS = PREFIX + '12-smoke'
SECRET = 'SENTINEL-not-a-real-token-5f3a9c'
DGD_API = 'nvidia.com/v1beta1'
DGD_LIST = 'dynamographdeployments.nvidia.com'
DGD_NAME_LABEL = 'nvidia.com/dynamo-graph-deployment-name'
LEASE_CM = 'wcep-agent-lease'
NS_UID = 'wcep.agent/namespace-uid'

EXTRA_PROFILE = '''
[clusters.lab-b200]
context = "proxy.example.test-lab-b200"
allowed = "first-choice"
source = ["made up for the k8s-guard tests"]
bot_can_write = true
arch = "amd64"
gpu_product = "NVIDIA-B200"
gpu_type = "b200"
gpus_per_node = 8
storage_class = "example-rwo"
model_cache = "shared-model-cache"
pull_secret = ""
reaper = true
operator_namespace = "dynamo-system"
'''

FAKE_KUBECTL = r'''#!/usr/bin/env python3
"""Stateful fake kubectl. State: FAKE_K8S_STATE. Log: FAKE_KUBECTL_LOG (argv only, never stdin)."""
import copy, json, os, sys, time

STATE = os.environ['FAKE_K8S_STATE']
argv = sys.argv[1:]
VALUED = {'--kubeconfig', '--context', '-n', '--namespace', '-o', '--output', '-f', '--filename', '--type',
          '-p', '--patch', '--raw', '--request-timeout', '--from-file', '--dry-run', '--field-selector', '-l',
          '--selector', '--wait'}
flags, pos, i = {}, [], 0
while i < len(argv):
    a = argv[i]
    if a.startswith('--') and '=' in a and a.split('=', 1)[0] in VALUED:
        k, v = a.split('=', 1); flags[k] = v; i += 1
    elif a in VALUED:
        flags[a] = argv[i + 1]; i += 2
    else:
        pos.append(a); i += 1
stdin = sys.stdin.read() if flags.get('-f', flags.get('--filename')) == '-' or \
    '/dev/stdin' in flags.get('--from-file', '') else ''
with open(os.environ['FAKE_KUBECTL_LOG'], 'a') as log:
    log.write(json.dumps({'argv': argv, 'pos': pos, 'flags': flags,
                          'env': {k: os.environ.get(k) for k in ('KUBECONFIG', 'KUBECTL_KUBERC')}}) + '\n')
st = json.load(open(STATE))
ns_flag = flags.get('-n') or flags.get('--namespace')
out_json = flags.get('-o', flags.get('--output')) == 'json'


def save():
    with open(STATE, 'w') as f:
        json.dump(st, f)


def fail(msg, code=1):
    sys.stderr.write(msg + '\n'); sys.exit(code)


def emit(obj):
    sys.stdout.write(json.dumps(obj) + '\n'); sys.exit(0)


def now_iso():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def next_uid(name):
    st['counter'] = st.get('counter', 0) + 1
    return f'uid-{name}-{st["counter"]}'


def rv():
    st['rv'] = st.get('rv', 100) + 1
    return str(st['rv'])


def deep_merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = copy.deepcopy(v)
    return base


def docs(text):
    text = text.strip(); dec = json.JSONDecoder(); out = []; i = 0
    while i < len(text):
        obj, j = dec.raw_decode(text, i); i = j
        while i < len(text) and text[i].isspace():
            i += 1
        out.extend(obj['items'] if obj.get('kind') == 'List' else [obj])
    return out


def key(obj):
    return obj['kind'] + '/' + obj['metadata']['name']


def ptr(path):
    return [p.replace('~1', '/').replace('~0', '~') for p in path.lstrip('/').split('/')]


def json_patch(obj, ops):
    for op in ops:
        parts = ptr(op['path']); parent = obj
        for p in parts[:-1]:
            parent = parent[int(p)] if isinstance(parent, list) else parent[p]
        last = parts[-1]
        if op['op'] == 'test':
            cur = parent[int(last)] if isinstance(parent, list) else parent.get(last)
            if cur != op['value']:
                fail(f'The request is invalid: the server rejected our request due to an error in our request: '
                     f'testing value {op["path"]} failed')
        elif op['op'] in ('add', 'replace'):
            if isinstance(parent, list):
                parent[int(last)] = op['value']
            else:
                parent[last] = op['value']
        else:
            fail('fake kubectl: unsupported patch op ' + op['op'], 97)


cmd = pos[0] if pos else ''
if pos[:1] == ['label'] and '--local' in pos:
    # Local: parse the manifest, set the label on every object, print the objects. No API call.
    k, v = next(p for p in pos[1:] if '=' in p).split('=', 1)
    for obj in docs(stdin):
        obj.setdefault('metadata', {}).setdefault('labels', {})[k] = v
        sys.stdout.write(json.dumps(obj) + '\n')
    sys.exit(0)
if pos[:2] == ['config', 'view']:
    # Local: kubectl reads the file it was given (here /proc/self/fd/<n>, inherited from the guard).
    emit(json.load(open(flags['--kubeconfig'])))
for needle in st.get('forbid', []):
    if needle in ' '.join(argv):
        fail(f'Error from server (Forbidden): fake forbids "{needle}"')
if st.get('unreachable'):
    fail('Unable to connect to the server: dial tcp 192.0.2.1:443: i/o timeout')

if cmd == 'get':
    what = pos[1]
    if what == 'namespace':
        ns = st['namespaces'].get(pos[2])
        if ns is None:
            fail(f'Error from server (NotFound): namespaces "{pos[2]}" not found')
        emit(ns)
    if what == DGD_LIST_NAME:
        if st.get('recreate_ns_on_list') and ns_flag in st['namespaces']:
            # The namespace is deleted and made again under the same name between two of the guard's reads.
            st['namespaces'][ns_flag]['metadata']['uid'] = 'uid-recreated'
            save()
        items = [o for o in st['objects'].get(ns_flag, {}).values() if o['kind'] == 'DynamoGraphDeployment']
        if flags.get('-l'):
            lk, lv = flags['-l'].split('=', 1)
            items = [o for o in items if (o['metadata'].get('labels') or {}).get(lk) == lv]
        emit({'kind': 'List', 'apiVersion': 'v1', 'items': items})
    if what == 'pods':
        emit({'kind': 'List', 'apiVersion': 'v1', 'items': st['pods'].get(ns_flag, [])})
    if what == 'configmap':
        o = st['objects'].get(ns_flag, {}).get('ConfigMap/' + pos[2])
        if o is None:
            fail(f'Error from server (NotFound): configmaps "{pos[2]}" not found')
        emit(o)
    if '/' in what:
        res, name = what.split('/', 1)
        kind = {'dynamographdeployments.nvidia.com': 'DynamoGraphDeployment'}.get(res)
        o = st['objects'].get(ns_flag, {}).get(f'{kind}/{name}')
        if o is None:
            fail(f'Error from server (NotFound): {res} "{name}" not found')
        emit(o)
    fail('fake kubectl: no handler for get ' + ' '.join(pos[1:]), 97)

if cmd == 'create' and len(pos) == 1:
    obj = json.loads(stdin)
    if obj['kind'] == 'Namespace':
        name = obj['metadata']['name']
        if name in st['namespaces']:
            fail(f'Error from server (AlreadyExists): namespaces "{name}" already exists')
        obj['metadata'].update(uid=next_uid(name), resourceVersion=rv(), creationTimestamp=now_iso())
        obj['status'] = {'phase': 'Active'}
        st['namespaces'][name] = obj; st['objects'].setdefault(name, {}); st['pods'].setdefault(name, [])
        save(); emit(obj)
    ns = obj['metadata'].get('namespace') or ns_flag
    if ns not in st['namespaces']:
        fail(f'Error from server (NotFound): namespaces "{ns}" not found')
    if key(obj) in st['objects'][ns]:
        fail(f'Error from server (AlreadyExists): {obj["kind"].lower()}s "{obj["metadata"]["name"]}" already exists')
    obj['metadata'].update(namespace=ns, uid=next_uid(obj['metadata']['name']), resourceVersion=rv())
    st['objects'][ns][key(obj)] = obj; save(); emit(obj)

if cmd == 'create' and pos[1:2] == ['secret']:
    stype, name = pos[2], pos[3]
    if ns_flag not in st['namespaces']:
        fail(f'Error from server (NotFound): namespaces "{ns_flag}" not found')
    if f'Secret/{name}' in st['objects'][ns_flag]:
        fail(f'Error from server (AlreadyExists): secrets "{name}" already exists')
    k = flags['--from-file'].split('=', 1)[0]
    st['objects'][ns_flag][f'Secret/{name}'] = {
        'kind': 'Secret', 'apiVersion': 'v1', 'type': flags.get('--type', 'Opaque'),
        'metadata': {'name': name, 'namespace': ns_flag, 'uid': next_uid(name)}, 'stringData': {k: stdin}}
    save(); sys.stdout.write(f'secret/{name} created\n'); sys.exit(0)

if cmd == 'replace':
    obj = json.loads(stdin)
    if obj['kind'] == 'Namespace':
        fail('Error from server (Forbidden): namespaces is forbidden: cannot update resource "namespaces"')
    ns = obj['metadata'].get('namespace') or ns_flag
    cur = st['objects'].get(ns, {}).get(key(obj))
    if cur is None:
        fail(f'Error from server (NotFound): {obj["kind"].lower()}s "{obj["metadata"]["name"]}" not found')
    if obj['metadata'].get('resourceVersion') != cur['metadata']['resourceVersion']:
        fail('Error from server (Conflict): Operation cannot be fulfilled: the object has been modified')
    obj['metadata'].update(uid=cur['metadata']['uid'], resourceVersion=rv(), namespace=ns)
    st['objects'][ns][key(obj)] = obj; save(); emit(obj)

if cmd == 'apply':
    dry = flags.get('--dry-run')
    if dry not in (None, 'server'):
        fail('fake kubectl: only server dry runs', 97)
    if not dry and st.get('apply_fail'):
        fail('Error from server (InternalError): fake apply failure')
    if ns_flag not in st['namespaces']:
        fail(f'Error from server (NotFound): namespaces "{ns_flag}" not found')
    out = []
    for obj in docs(stdin):
        meta = obj.setdefault('metadata', {})
        if obj.get('kind') in st.get('cluster_scoped', ['ClusterRole', 'Namespace', 'PriorityClass']):
            meta.pop('namespace', None)
        else:
            if meta.get('namespace', ns_flag) != ns_flag:
                fail('error: the namespace from the provided object "%s" does not match the namespace "%s". '
                     'You must pass \'--namespace=%s\' to perform this operation.' % (meta['namespace'], ns_flag,
                                                                                     meta['namespace']))
            meta['namespace'] = ns_flag
        cur = st['objects'][ns_flag].get(key(obj))
        merged = deep_merge(copy.deepcopy(cur), obj) if cur else obj
        merged['metadata']['uid'] = cur['metadata']['uid'] if cur else next_uid(meta['name'])
        merged['metadata']['resourceVersion'] = rv()
        if obj.get('kind') == 'Service':
            merged.setdefault('spec', {}).setdefault('type', 'ClusterIP')
        for kind, extra in st.get('mutate', {}).items():
            if kind == merged.get('kind'):
                deep_merge(merged, extra)
        out.append(merged)
        if not dry:
            st['objects'][ns_flag][key(merged)] = merged
    if not dry:
        save()
    emit(out[0] if len(out) == 1 else {'kind': 'List', 'apiVersion': 'v1', 'items': out})

if cmd == 'patch':
    target = pos[1]
    if target.startswith('namespace'):
        fail('Error from server (Forbidden): namespaces is forbidden: cannot patch resource "namespaces"')
    res, name = target.split('/', 1)
    if not res.startswith('dynamographdeployments.') or flags.get('--type') != 'json':
        fail('fake kubectl: no handler for patch ' + target, 97)
    o = st['objects'].get(ns_flag, {}).get('DynamoGraphDeployment/' + name)
    if o is None:
        fail(f'Error from server (NotFound): dynamographdeployments "{name}" not found')
    o = copy.deepcopy(o); json_patch(o, json.loads(flags.get('-p') or flags.get('--patch')))
    o['metadata']['resourceVersion'] = rv()
    st['objects'][ns_flag]['DynamoGraphDeployment/' + name] = o; save(); emit(o)

if cmd == 'delete' and '--raw' in flags:
    body = json.loads(stdin)
    want = (body.get('preconditions') or {}).get('uid')
    parts = flags['--raw'].strip('/').split('/')
    if parts[:3] == ['api', 'v1', 'namespaces'] and len(parts) == 4:
        ns = st['namespaces'].get(parts[3])
        if ns is None:
            fail(f'Error from server (NotFound): namespaces "{parts[3]}" not found')
        if want is None:
            fail('fake kubectl: namespace delete without a UID precondition', 96)
        if ns['metadata']['uid'] != want:
            fail('Error from server (Conflict): Precondition failed: UID in precondition: %s, UID in object meta: %s'
                 % (want, ns['metadata']['uid']))
        del st['namespaces'][parts[3]]; st['objects'].pop(parts[3], None); st['pods'].pop(parts[3], None)
        st.setdefault('deleted_namespaces', []).append(parts[3]); save()
        emit({'kind': 'Status', 'status': 'Success'})
    if parts[0] == 'apis' and parts[3] == 'namespaces' and parts[5] == 'dynamographdeployments':
        ns, name = parts[4], parts[6]
        o = st['objects'].get(ns, {}).get('DynamoGraphDeployment/' + name)
        if o is None:
            fail(f'Error from server (NotFound): dynamographdeployments "{name}" not found')
        if want is None:
            fail('fake kubectl: DGD delete without a UID precondition', 96)
        if o['metadata']['uid'] != want:
            fail('Error from server (Conflict): Precondition failed')
        del st['objects'][ns]['DynamoGraphDeployment/' + name]
        pods = st['pods'].get(ns, [])
        if st.get('terminate_pods_on_delete', True):
            st['pods'][ns] = [p for p in pods if p['metadata'].get('labels', {}).get(DGD_LABEL) != name]
        else:
            for p in pods:
                if p['metadata'].get('labels', {}).get(DGD_LABEL) == name:
                    p['metadata']['deletionTimestamp'] = now_iso()
        save(); emit({'kind': 'Status', 'status': 'Success'})
    fail('fake kubectl: no handler for raw delete ' + flags['--raw'], 97)

if cmd == 'delete':
    res, name = pos[1].split('/', 1)
    kinds = {'dynamographdeployments.nvidia.com': 'DynamoGraphDeployment', 'configmaps': 'ConfigMap',
             'services': 'Service', 'secrets': 'Secret', 'persistentvolumeclaims': 'PersistentVolumeClaim',
             'podcliquesets.grove.io': 'PodCliqueSet'}
    k = f'{kinds.get(res, res)}/{name}'
    if k not in st['objects'].get(ns_flag, {}):
        fail(f'Error from server (NotFound): {res} "{name}" not found')
    del st['objects'][ns_flag][k]; save()
    sys.stdout.write(f'{res}/{name} deleted\n'); sys.exit(0)

fail('fake kubectl: no handler for: ' + ' '.join(pos), 97)
'''.replace('DGD_LIST_NAME', repr(DGD_LIST)).replace('DGD_LABEL', repr(DGD_NAME_LABEL))

FAKE_BB = r'''#!/usr/bin/env python3
"""Fake bb: the k8s-leases commands the guard may call. State: FAKE_BB_STATE. Log: FAKE_BB_LOG."""
import json, os, sys
from datetime import datetime, timedelta, timezone

argv = sys.argv[1:]
st = json.load(open(os.environ['FAKE_BB_STATE']))
with open(os.environ['FAKE_BB_LOG'], 'a') as log:
    log.write(json.dumps(argv) + '\n')
if st.get('unreachable'):
    sys.stderr.write('Cannot connect to BB server. Ensure it is running and BB_SERVER_URL is correct.\n')
    sys.exit(1)


def save():
    json.dump(st, open(os.environ['FAKE_BB_STATE'], 'w'))


def iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%S.') + f'{dt.microsecond // 1000:03d}Z'


def out(obj):
    print(json.dumps(obj)); sys.exit(0)


def err(code, msg):
    print(json.dumps({'error': msg, 'code': code})); sys.exit(1)


def lease(lid):
    l = st['leases'].get(lid)
    if l is None:
        err('lease_not_found', f'No lease {lid}.')
    return l


if argv[:3] == ['k8s', 'lease', 'acquire']:
    o = {argv[i][2:]: argv[i + 1] for i in range(3, len(argv) - 1, 2)}
    for l in st['leases'].values():
        if l['cluster'] == o['cluster'] and l['namespace'] == o['namespace'] and \
                l['state'] in ('active', 'revoking', 'releasing'):
            err('lease_exists', 'The namespace already has a live lease.')
    if st.get('acquire_refuse'):
        err(st['acquire_refuse'], 'refused by the fake')
    lid = st.get('next_id', 'lse_fake0001')
    now = datetime.now(timezone.utc)
    st['leases'][lid] = {'lease_id': lid, 'state': 'active', 'cluster': o['cluster'], 'namespace': o['namespace'],
                         'uid': o['uid'], 'thread': o['thread'], 'gpu_type': o['gpu-type'], 'gpus': int(o['gpus']),
                         'expires_at': iso(now + timedelta(hours=float(o['hours']))), 'multi_node': False}
    save(); out({'lease_id': lid, 'expires_at': st['leases'][lid]['expires_at']})
if argv[:3] == ['k8s', 'lease', 'show']:
    out(lease(argv[3]))
if argv[:4] == ['k8s', 'lease', 'op', 'begin']:
    l = lease(argv[4])
    if l['state'] != 'active':
        err('not_active', f'Lease {argv[4]} is {l["state"]}.')
    if l.get('slot'):
        err('slot_busy', 'The slot is held.')
    l['slot'] = 'tok-' + argv[4]
    dl = datetime.now(timezone.utc) + timedelta(seconds=st.get('slot_seconds', 120))
    save(); out({'token': l['slot'], 'deadline': iso(dl)})
if argv[:4] == ['k8s', 'lease', 'op', 'end']:
    l = lease(argv[4])
    if l.get('slot') != argv[5]:
        err('bad_token', 'Not your slot.')
    l['slot'] = None
    if st.get('op_end_state'):
        l['state'] = st['op_end_state']
    save(); out({'state': l['state']})
if argv[:3] == ['k8s', 'lease', 'release']:
    l = lease(argv[3])
    if l['state'] == 'active':
        l['state'] = 'releasing'
    save(); out({'state': l['state']})
print('fake bb: no handler for ' + ' '.join(argv), file=sys.stderr)
sys.exit(97)
'''

FAKE_FLEET = '''#!/bin/sh
echo "$@" >> "$FAKE_FLEET_LOG"
[ "$1 $2" = "secret get" ] || exit 97
printf '%s\\n' "$FAKE_SECRET"
'''

FAKE_TSH = '#!/bin/sh\necho tsh >> "$FAKE_TSH_LOG"\necho "fake tsh: agents never use tsh" >&2\nexit 98\n'

# The profile parser takes the account's home from the password database, not $HOME. The tests run
# the guard through this runner (as test-k8s-check.py does), which answers pwd.getpwuid with the
# temporary home.
RUNNER = r'''
import os, pwd, runpy, sys
real = pwd.getpwuid
class Entry:
    def __init__(self, entry):
        self.pw_name, self.pw_uid, self.pw_dir = entry.pw_name, entry.pw_uid, os.environ['FAKE_PW_DIR']
pwd.getpwuid = lambda uid: Entry(real(uid))
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
'''


def kubeconfig(tbot, user=None, contexts=(CTX, CTX_QA, CTX_B200), destination=None):
    """A bot kubeconfig as tbot writes it with its exec plugin on, in JSON (also valid YAML)."""
    user = user if user is not None else {'exec': {
        'apiVersion': 'client.authentication.k8s.io/v1beta1', 'command': tbot,
        'args': ['kube', 'credentials', f'--destination-dir={destination}']}}
    return json.dumps({
        'apiVersion': 'v1', 'kind': 'Config',
        'clusters': [{'name': 'example', 'cluster': {'server': 'https://proxy.example.test'}}],
        'contexts': [{'name': c, 'context': {'cluster': 'example', 'user': 'bot'}} for c in contexts],
        'users': [{'name': 'bot', 'user': user}]})


def iso_ms(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%S.') + f'{dt.microsecond // 1000:03d}Z'


def hours_from_now(h):
    return datetime.now(timezone.utc) + timedelta(hours=h)


def container(gpus=0, name='main', resources=True):
    c = {'name': name, 'image': 'registry.example.test/dynamo:abc1234'}
    if resources:
        req = {'cpu': '2', 'memory': '8Gi'}
        lim = {'cpu': '2', 'memory': '8Gi'}
        if gpus:
            req['nvidia.com/gpu'] = str(gpus)
            lim['nvidia.com/gpu'] = str(gpus)
        c['resources'] = {'requests': req, 'limits': lim}
    return c


def one_node(lease_id):
    return {'podAffinity': {'requiredDuringSchedulingIgnoredDuringExecution': [{
        'labelSelector': {'matchLabels': {'wcep.agent/lease': lease_id}},
        'topologyKey': 'kubernetes.io/hostname'}]}}


def component(name, gpus=0, replicas=1, lease_id=None, ctype='worker', **extra):
    spec = {'containers': [container(gpus)]}
    meta = {}
    if gpus and lease_id:
        spec['affinity'] = one_node(lease_id)
        spec['nodeSelector'] = {'nvidia.com/gpu.product': 'NVIDIA-GB300'}
        meta['labels'] = {'wcep.agent/lease': lease_id}
    c = {'name': name, 'type': ctype, 'replicas': replicas, 'podTemplate': {'metadata': meta, 'spec': spec}}
    c.update(extra)
    return c


def dgd(name, *components, **spec_extra):
    spec = {'components': list(components) or [component('Frontend', ctype='frontend')]}
    spec.update(spec_extra)
    return {'apiVersion': DGD_API, 'kind': 'DynamoGraphDeployment', 'metadata': {'name': name}, 'spec': spec}


def gpu_dgd(name, gpus, lease_id, replicas=1):
    return dgd(name, component('Frontend', ctype='frontend'),
               component('decode', gpus=gpus, replicas=replicas, lease_id=lease_id))


def pod(dgd_name, gpus, terminating=False, phase='Running', name=None):
    meta = {'name': name or f'{dgd_name}-decode-{gpus}', 'labels': {DGD_NAME_LABEL: dgd_name}}
    if terminating:
        meta['deletionTimestamp'] = '2026-01-01T00:00:00Z'
    return {'metadata': meta, 'spec': {'containers': [container(gpus)]}, 'status': {'phase': phase}}


def namespace(name, thread, uid, expires_at=None):
    ann = {'wcep.agent/expires-at': expires_at or iso_ms(hours_from_now(2))}
    return {'apiVersion': 'v1', 'kind': 'Namespace',
            'metadata': {'name': name, 'uid': uid, 'resourceVersion': '10',
                         'labels': {'owner': 'alice', 'dynamo.nvidia.com/developer': 'alice',
                                    'dynamo.nvidia.com/developer-agent-rbac': 'true',
                                    'wcep.agent/thread': thread},
                         'annotations': ann},
            'status': {'phase': 'Active'}}


def lease_cm(ns, lease_id, uid, thread, expires_at):
    return {'apiVersion': 'v1', 'kind': 'ConfigMap',
            'metadata': {'name': LEASE_CM, 'namespace': ns, 'uid': 'uid-cm', 'resourceVersion': '20',
                         'labels': {NS_UID: uid}},
            'data': {'lease_id': lease_id, 'expires_at': expires_at, 'thread': thread, 'uid': uid}}


class GuardCase(unittest.TestCase):
    """One temporary world per test: profiles, kubeconfigs, fakes, cluster state and lease state."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / 'home'
        self.bot_kubeconfig = self.home / '.local/share/tbot/example/kube/kubeconfig.yaml'
        self.bot_kubeconfig.parent.mkdir(parents=True)
        # tbot as the dotfiles install it for one user, in the (fake) passwd home.
        self.tbot = self.home / '.local/bin/tbot'
        self.tbot.parent.mkdir(parents=True)
        self.tbot.write_text('#!/bin/sh\nexit 0\n')
        self.tbot.chmod(0o755)
        self.bot_kubeconfig.write_text(kubeconfig(str(self.tbot), destination=self.bot_kubeconfig.parent))
        self.runner = self.tmp / 'runner.py'
        self.runner.write_text(RUNNER)
        for rel in ('.kube/config', '.tsh/keys/example/kubeconfig'):
            p = self.home / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('apiVersion: v1\nkind: Config\n')
        self.bin = self.tmp / 'bin'
        self.bin.mkdir()
        for name, body in (('kubectl', FAKE_KUBECTL), ('bb', FAKE_BB), ('fleet', FAKE_FLEET), ('tsh', FAKE_TSH)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.profiles = self.tmp / 'profiles.toml'
        self.profiles.write_text(EXAMPLE.read_text() + EXTRA_PROFILE)
        self.storage = self.tmp / 'thread-storage'
        self.storage.mkdir()
        self.state_home = self.tmp / 'state'
        self.k8s_state = self.tmp / 'k8s.json'
        self.bb_state = self.tmp / 'bb.json'
        self.kubectl_log = self.tmp / 'kubectl.log'
        self.bb_log = self.tmp / 'bb.log'
        self.fleet_log = self.tmp / 'fleet.log'
        self.tsh_log = self.tmp / 'tsh.log'
        self.k8s = {'namespaces': {}, 'objects': {}, 'pods': {}}
        self.bb = {'leases': {}}
        self.flush()

    def tearDown(self):
        self.assertFalse(self.tsh_log.exists(), 'tsh was invoked')
        for call in self.kubectl_calls():
            # The pinned bot kubeconfig, a context, no KUBECONFIG and kuberc off on every call.
            self.assertTrue(call['flags'].get('--kubeconfig', '').startswith('/proc/self/fd/'), call)
            self.assertIn('--context', call['flags'], call)
            self.assertEqual(call['env'], {'KUBECONFIG': None, 'KUBECTL_KUBERC': 'false'}, call)

    # --- world -------------------------------------------------------------

    def flush(self):
        self.k8s_state.write_text(json.dumps(self.k8s))
        self.bb_state.write_text(json.dumps(self.bb))

    def reload(self):
        self.k8s = json.loads(self.k8s_state.read_text())
        self.bb = json.loads(self.bb_state.read_text())

    def add_ns(self, name=NS, thread=ME, uid='uid-ns-1', expires_at=None, ledger=True):
        if ledger:
            # As `ns create` records it in the creating thread's ledger. Marked so ledger() skips it.
            with open(self.storage / 'k8s-ledger.jsonl', 'a') as f:
                f.write(json.dumps({'thread': thread, 'cluster': PROFILE, 'namespace': name, 'uid': uid,
                                    'object': f'Namespace/{name}', 'direction': 'up', 'created': True,
                                    'seed': True}) + '\n')
        self.k8s['namespaces'][name] = namespace(name, thread, uid, expires_at)
        self.k8s['objects'].setdefault(name, {})
        self.k8s['pods'].setdefault(name, [])
        self.flush()

    def add_obj(self, obj, ns=NS, uid=None, stamp=True):
        obj = copy.deepcopy(obj)
        obj['metadata'].update(namespace=ns, uid=uid or f'uid-{obj["metadata"]["name"]}', resourceVersion='30')
        if stamp and obj['kind'] != 'ConfigMap':
            # As `apply` writes it: stamped with the namespace instance's UID.
            obj['metadata'].setdefault('labels', {})[NS_UID] = self.k8s['namespaces'][ns]['metadata']['uid']
        self.k8s['objects'][ns][obj['kind'] + '/' + obj['metadata']['name']] = obj
        self.flush()

    def add_pod(self, p, ns=NS):
        self.k8s['pods'][ns].append(p)
        self.flush()

    def add_lease(self, lease_id='lse_test0001', ns=NS, uid='uid-ns-1', thread=ME, cluster=PROFILE, gpus=4,
                  gpu_type='gb300', state='active', expires_at=None, multi_node=False, cm=True):
        expires_at = expires_at or iso_ms(hours_from_now(2))
        self.bb['leases'][lease_id] = {'lease_id': lease_id, 'state': state, 'cluster': cluster, 'namespace': ns,
                                       'uid': uid, 'thread': thread, 'gpu_type': gpu_type, 'gpus': gpus,
                                       'expires_at': expires_at, 'multi_node': multi_node}
        if cm:
            self.add_obj(lease_cm(ns, lease_id, uid, thread, expires_at), ns=ns, uid='uid-cm')
        self.flush()
        return lease_id

    # --- running ----------------------------------------------------------

    def env(self, thread=ME, host_entry=False, kubeconfig=None, storage=True):
        env = {'PATH': f'{self.bin}:/usr/bin:/bin', 'HOME': str(self.home),
               'K8S_CLUSTERS_PROFILES': str(self.profiles),
               'KUBECONFIG': str(kubeconfig or self.bot_kubeconfig),
               'XDG_STATE_HOME': str(self.state_home),
               'FAKE_K8S_STATE': str(self.k8s_state), 'FAKE_KUBECTL_LOG': str(self.kubectl_log),
               'FAKE_BB_STATE': str(self.bb_state), 'FAKE_BB_LOG': str(self.bb_log),
               'FAKE_FLEET_LOG': str(self.fleet_log), 'FAKE_TSH_LOG': str(self.tsh_log),
               'FAKE_SECRET': SECRET, 'K8S_GUARD_DOWN_WAIT_SECONDS': '0', 'K8S_GUARD_POLL_SECONDS': '0',
               'FAKE_PW_DIR': str(self.home),
               # The caller's kubectl settings must never reach kubectl.
               'KUBECTL_KUBERC': 'true', 'KUBECTL_EXPLAIN_OPENAPIV3': 'true'}
        if storage is False:
            env['BB_THREAD_ID'] = thread
        elif host_entry:
            # What the k8s-leases host entry gives the guard: no BB_THREAD_ID, no thread storage.
            env['BB_SERVER_URL'] = 'http://127.0.0.1:1'
        else:
            env['BB_THREAD_ID'] = thread
            env['BB_THREAD_STORAGE'] = str(self.storage)
        if kubeconfig == '':
            del env['KUBECONFIG']
        return env

    def guard(self, *args, stdin_file=None, **env_kw):
        p = subprocess.run(['python3', '-I', str(self.runner), str(GUARD), *args], env=self.env(**env_kw),
                           capture_output=True,
                           text=True, timeout=60, stdin=subprocess.DEVNULL)
        self.reload()
        try:
            body = json.loads(p.stdout)
        except json.JSONDecodeError:
            self.fail(f'k8s-guard printed no JSON (exit {p.returncode}): {p.stdout!r} {p.stderr!r}')
        return p.returncode, body, p

    def refused(self, code, *args, **env_kw):
        rc, body, p = self.guard(*args, **env_kw)
        self.assertNotEqual(rc, 0, body)
        self.assertEqual(body.get('code'), code, body)
        self.assertIn('error', body)
        return body

    def ok(self, *args, **env_kw):
        rc, body, p = self.guard(*args, **env_kw)
        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        return body

    def manifest(self, *objs):
        path = self.tmp / f'manifest-{len(list(self.tmp.glob("manifest-*")))}.json'
        path.write_text('\n'.join(json.dumps(o) for o in objs))
        return str(path)

    def apply(self, *objs, ns=NS, **env_kw):
        return self.guard('--context', CTX, '-n', ns, 'apply', '-f', self.manifest(*objs), **env_kw)

    def down_waiting(self, *args, wait='0.3', **env_kw):
        """Run down with a short bounded wait, so the poll loop really runs."""
        env = self.env(**env_kw)
        env.update(K8S_GUARD_DOWN_WAIT_SECONDS=wait, K8S_GUARD_POLL_SECONDS='0.05')
        p = subprocess.run(['python3', '-I', str(self.runner), str(GUARD), '--context', CTX, 'down', *args],
                           env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        self.reload()
        return p.returncode, json.loads(p.stdout)

    def pod_polls(self):
        return sum(1 for c in self.kubectl_calls() if c['pos'][:2] == ['get', 'pods'])

    def kubectl_calls(self):
        if not self.kubectl_log.exists():
            return []
        return [json.loads(line) for line in self.kubectl_log.read_text().splitlines()]

    def writes(self):
        """kubectl calls that change the cluster (dry runs excluded)."""
        out = []
        for c in self.kubectl_calls():
            verb = c['pos'][0] if c['pos'] else ''
            if verb in ('create', 'apply', 'patch', 'replace', 'delete') and c['flags'].get('--dry-run') is None:
                out.append(c)
        return out

    def bb_calls(self):
        if not self.bb_log.exists():
            return []
        return [json.loads(line) for line in self.bb_log.read_text().splitlines()]

    def ledger(self, host=False):
        path = (self.state_home / 'k8s-guard/ledger.jsonl') if host else (self.storage / 'k8s-ledger.jsonl')
        if not path.exists():
            return []
        return [l for l in map(json.loads, path.read_text().splitlines()) if not l.get('seed')]

    def dgd_obj(self, name, ns=NS):
        return self.k8s['objects'][ns].get('DynamoGraphDeployment/' + name)


# --- rule 1: identity ------------------------------------------------------

class Identity(GuardCase):
    def test_unset_kubeconfig_refused_before_any_call(self):
        self.add_ns()
        self.refused('kubeconfig_not_bot', '--context', CTX, 'inspect', NS, kubeconfig='')
        self.assertEqual(self.kubectl_calls(), [])

    def test_human_kubeconfig_refused(self):
        self.add_ns()
        self.refused('kubeconfig_not_bot', '--context', CTX, 'inspect', NS, kubeconfig=self.home / '.kube/config')
        self.assertEqual(self.kubectl_calls(), [])

    def test_tsh_kubeconfig_refused(self):
        self.add_ns()
        self.refused('kubeconfig_not_bot', '--context', CTX, 'inspect', NS,
                     kubeconfig=self.home / '.tsh/keys/example/kubeconfig')
        self.assertEqual(self.kubectl_calls(), [])

    def test_kubeconfig_credentials_other_than_tbot_refused(self):
        self.add_ns()
        users = {'token': {'token': 'not-a-real-token'},
                 'tsh exec': {'exec': {'apiVersion': 'client.authentication.k8s.io/v1beta1', 'command': 'tsh',
                                       'args': ['kube', 'credentials']}},
                 'impersonation': {'exec': {'apiVersion': 'client.authentication.k8s.io/v1beta1',
                                            'command': 'tbot'}, 'as': 'cluster-admin'},
                 'tbot with other args': {'exec': {'apiVersion': 'client.authentication.k8s.io/v1beta1',
                                                   'command': str(self.tbot), 'args': ['kube', 'credentials',
                                                                               '--destination-dir=/elsewhere']}}}
        for label, user in users.items():
            with self.subTest(user=label):
                self.bot_kubeconfig.write_text(kubeconfig(str(self.tbot), user=user, destination=self.bot_kubeconfig.parent))
                self.kubectl_log.unlink(missing_ok=True)
                self.refused('kubeconfig_credentials', '--context', CTX, 'inspect', NS)
                self.assertEqual([c['pos'][:2] for c in self.kubectl_calls()], [['config', 'view']])

    def test_tbot_name_linking_to_tsh_refused(self):
        self.add_ns()
        fake_tbot = self.tmp / 'evil/tbot'
        fake_tbot.parent.mkdir()
        fake_tbot.symlink_to(self.bin / 'tsh')
        self.bot_kubeconfig.write_text(kubeconfig(str(fake_tbot), destination=self.bot_kubeconfig.parent))
        self.refused('kubeconfig_credentials', '--context', CTX, 'inspect', NS)
        self.assertEqual([c['pos'][:2] for c in self.kubectl_calls()], [['config', 'view']])

    def test_kubeconfig_rewritten_after_the_check_is_not_read(self):
        # kubectl reads a sealed in-memory copy: rewriting the file after the guard read it changes
        # nothing, so every call sees the credentials the check passed.
        self.add_ns()
        hook = """if pos[:2] == ['config', 'view']:
    open(os.environ['FAKE_BOT_KUBECONFIG'], 'w').write('{"rewritten": true}')"""
        fake = FAKE_KUBECTL.replace("if pos[:2] == ['config', 'view']:", hook + "\nif pos[:2] == ['config', 'view']:", 1)
        fake = fake.replace("if what == 'namespace':", """if what == 'namespace':
        if json.load(open(flags['--kubeconfig'])).get('rewritten'):
            fail('fake kubectl: read the rewritten kubeconfig', 96)""", 1)
        self.assertNotEqual(fake, FAKE_KUBECTL)
        (self.bin / 'kubectl').write_text(fake)
        env = self.env()
        env['FAKE_BOT_KUBECONFIG'] = str(self.bot_kubeconfig)
        p = subprocess.run(['python3', '-I', str(self.runner), str(GUARD), '--context', CTX, 'inspect', NS], env=env,
                           capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('rewritten', self.bot_kubeconfig.read_text())

    def test_context_missing_from_kubeconfig_refused(self):
        self.add_ns()
        self.bot_kubeconfig.write_text(kubeconfig(str(self.tbot), contexts=(CTX_QA,), destination=self.bot_kubeconfig.parent))
        self.refused('kubeconfig_credentials', '--context', CTX, 'inspect', NS)
        self.assertEqual([c['pos'][:2] for c in self.kubectl_calls()], [['config', 'view']])

    def test_two_kubeconfigs_refused(self):
        self.add_ns()
        both = f'{self.bot_kubeconfig}{os.pathsep}{self.home / ".kube/config"}'
        self.refused('kubeconfig_not_bot', '--context', CTX, 'inspect', NS, kubeconfig=both)


# --- rule 2: cluster -------------------------------------------------------

class Cluster(GuardCase):
    def test_context_required(self):
        self.add_ns()
        self.refused('context_required', 'inspect', NS)

    def test_context_with_no_profile_refused(self):
        self.add_ns()
        self.refused('unknown_context', '--context', 'proxy.example.test-nowhere', 'inspect', NS)
        self.assertEqual(self.kubectl_calls(), [])

    def test_profile_name_is_not_a_context(self):
        self.add_ns()
        self.refused('unknown_context', '--context', PROFILE, 'inspect', NS)

    def test_never_cluster_refused(self):
        self.add_ns()
        self.refused('cluster_never', '--context', CTX_CI, 'inspect', NS)
        self.assertEqual(self.kubectl_calls(), [])

    def test_read_only_cluster_refuses_writes_but_inspects(self):
        self.add_ns()
        self.refused('cluster_read_only', '--context', CTX_QA, 'ns', 'create', 'smoke', '--issue', '12',
                     '--hours', '1')
        self.refused('cluster_read_only', '--context', CTX_QA, '-n', NS, 'apply', '-f', self.manifest(dgd('m')))
        self.assertEqual(self.writes(), [])
        self.ok('--context', CTX_QA, 'inspect', NS)


# --- rule 3: commands ------------------------------------------------------

class Commands(GuardCase):
    def test_other_verbs_refused_without_a_call(self):
        self.add_ns()
        for verb in ('scale', 'patch', 'edit', 'replace', 'set', 'label', 'annotate', 'exec', 'create', 'get'):
            with self.subTest(verb=verb):
                self.refused('command_refused', '--context', CTX, '-n', NS, verb, 'dgd/x')
        self.assertEqual(self.kubectl_calls(), [])

    def test_all_namespaces_refused(self):
        self.add_ns()
        self.refused('command_refused', '--context', CTX, '-A', 'apply', '-f', self.manifest(dgd('m')))
        self.refused('command_refused', '--context', CTX, '--all-namespaces', 'inspect', NS)
        self.assertEqual(self.kubectl_calls(), [])

    def test_unknown_flag_refused(self):
        self.add_ns()
        self.refused('usage', '--context', CTX, '-n', NS, 'apply', '-f', self.manifest(dgd('m')), '--force')
        self.assertEqual(self.writes(), [])


# --- rule 3: ns create; rule 5: never adopt -----------------------------------

class NamespaceCreate(GuardCase):
    def test_creates_labelled_namespace_with_expiry(self):
        body = self.ok('--context', CTX, 'ns', 'create', 'smoke', '--issue', '12', '--hours', '2')
        self.assertRegex(body['namespace'], '^' + NS + '-[a-z0-9]{10}$')
        ns = self.k8s['namespaces'][body['namespace']]
        labels = ns['metadata']['labels']
        self.assertEqual(labels['wcep.agent/thread'], ME)
        self.assertEqual(labels['owner'], 'alice')
        self.assertEqual(labels['dynamo.nvidia.com/developer'], 'alice')
        self.assertEqual(labels['dynamo.nvidia.com/developer-agent-rbac'], 'true')
        self.assertEqual(labels['wcep.agent/issue'], '12')
        expires = datetime.fromisoformat(ns['metadata']['annotations']['wcep.agent/expires-at'].replace('Z', '+00:00'))
        self.assertAlmostEqual((expires - datetime.now(timezone.utc)).total_seconds(), 7200, delta=120)
        self.assertEqual(body['uid'], ns['metadata']['uid'])
        [line] = self.ledger()
        self.assertEqual((line['namespace'], line['direction'], line['uid'], line['cluster'], line['created']),
                         (body['namespace'], 'up', ns['metadata']['uid'], PROFILE, True))
        self.assertEqual(len(self.ledger(host=True)), 1)

    def create_with_suffix(self, char, *args):
        # Run the guard with the CSPRNG answering one fixed character, to force a name.
        launcher = self.tmp / 'fixed-suffix.py'
        launcher.write_text(RUNNER.replace('sys.argv = sys.argv[1:]',
                                           f'import secrets\nsecrets.choice = lambda seq: {char!r}\n'
                                           'sys.argv = sys.argv[1:]'))
        p = subprocess.run(['python3', '-I', str(launcher), str(GUARD), '--context', CTX, 'ns', 'create', *args],
                           env=self.env(), capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        self.reload()
        return p.returncode, json.loads(p.stdout)

    def test_existing_namespace_never_adopted(self):
        taken = NS + '-' + 'a' * 10
        self.add_ns(name=taken, thread=OTHER, ledger=False)
        before = copy.deepcopy(self.k8s['namespaces'][taken])
        rc, body = self.create_with_suffix('a', 'smoke', '--issue', '12', '--hours', '1')
        self.assertEqual(body.get('code'), 'namespace_exists', body)
        self.assertEqual(self.k8s['namespaces'][taken], before)
        self.assertEqual(self.ledger(), [])

    def test_names_are_never_reused(self):
        names = {self.ok('--context', CTX, 'ns', 'create', 'smoke', '--issue', '12', '--hours', '1')['namespace']
                 for _ in range(3)}
        self.assertEqual(len(names), 3, names)
        self.assertTrue(all(n.startswith(NS + '-') for n in names), names)

    def test_63_character_limit_at_the_longest_issue_and_purpose(self):
        # prefix (14) + issue (10, the longest accepted) + '-' + purpose + '-' + suffix (10) = 63 at purpose 27.
        issue = '1234567890'
        rc, body = self.create_with_suffix('b', 'p' * 27, '--issue', issue, '--hours', '1')
        self.assertEqual(rc, 0, body)
        self.assertEqual(len(body['namespace']), 63)
        self.assertTrue(body['namespace'].endswith('-' + 'b' * 10), body)
        self.refused('usage', '--context', CTX, 'ns', 'create', 'p' * 28, '--issue', issue, '--hours', '1')
        self.refused('usage', '--context', CTX, 'ns', 'create', 'p', '--issue', '12345678901', '--hours', '1')

    def test_f14_residual_is_left_alone_but_counted(self):
        # The F14 residual as it is (ruling on F15: the doc claimed a one-tick cleanup; it was wrong).
        # The accepted collision leaves a late apply stamped with the deleted namespace's UID.
        ns, uid = NS + '-' + 'a' * 10, 'uid-current-ns'
        self.add_ns(name=ns, thread=OTHER, uid=uid)
        lid = self.add_lease(ns=ns, uid=uid, thread=OTHER, gpus=1, state='revoking')
        self.add_obj(gpu_dgd('current', 1, lid), ns=ns)
        late = gpu_dgd('late', 2, 'lse_previous01')
        late['metadata']['labels'] = {NS_UID: 'uid-deleted-ns'}
        self.add_obj(late, ns=ns, stamp=False)
        self.add_pod(pod('late', 2), ns=ns)
        before = self.ok('--context', CTX, 'inspect', ns, host_entry=True)
        self.assertEqual(before['allocation'], 3)

        # Reconcile has revoked the new lease and sends expire with that namespace's UID.
        rc, body, _ = self.guard('--context', CTX, 'expire', ns, '--uid', uid, '--lease', lid,
                                 host_entry=True)

        self.assertEqual(rc, 0, body)
        self.assertEqual(body['gpu_pods_left'], 1)
        current = self.k8s['objects'][ns]['DynamoGraphDeployment/current']
        self.assertEqual(current['spec']['components'][1]['replicas'], 0)
        # The late DGD carries the deleted namespace's stamp, so teardown never touches it...
        late = self.k8s['objects'][ns]['DynamoGraphDeployment/late']
        self.assertEqual(late['spec']['components'][1]['replicas'], 1, body)
        # ...but inspect counts it, so the plugin sees the over-allocation and flags it.
        after = self.ok('--context', CTX, 'inspect', ns, host_entry=True)
        self.assertEqual((after['allocation'], after['gpu_pods_left']), (2, 1), after)
        # down deletes its own stamped DGDs, then the namespace under the UID precondition: that removes
        # the stray DGD and its pods too, so the residual ends at the grace deadline.
        body = self.ok('--context', CTX, 'down', ns, '--uid', uid, '--lease', lid, host_entry=True)
        self.assertNotIn(ns, self.k8s['namespaces'])
        self.assertEqual(body['gpu_pods_left'], 1, body)

    def test_f14_collision_down_removes_the_stray_and_nothing_else(self):
        # Reproduce F14: a late apply from a deleted namespace lands in the new one under the same name.
        ns, uid = NS + '-' + 'c' * 10, 'uid-current-ns'
        neighbour = NS + '-' + 'd' * 10
        self.add_ns(name=neighbour, thread=OTHER, uid='uid-neighbour')
        self.add_obj(gpu_dgd('theirs', 1, 'lse_neighbour1'), ns=neighbour)
        self.add_pod(pod('theirs', 1), ns=neighbour)
        self.add_ns(name=ns, thread=OTHER, uid=uid)
        lid = self.add_lease(ns=ns, uid=uid, thread=OTHER, gpus=1, state='revoking')
        self.add_obj(gpu_dgd('current', 1, lid), ns=ns)
        self.add_pod(pod('current', 1), ns=ns)
        late = gpu_dgd('late', 2, 'lse_previous01')
        late['metadata']['labels'] = {NS_UID: 'uid-deleted-ns'}
        self.add_obj(late, ns=ns, stamp=False)
        self.add_pod(pod('late', 2), ns=ns)
        before = copy.deepcopy(self.k8s)

        body = self.ok('--context', CTX, 'down', ns, '--uid', uid, '--lease', lid, host_entry=True)

        self.assertNotIn(ns, self.k8s['namespaces'])
        self.assertIn(ns, self.k8s['deleted_namespaces'])
        # Only this namespace instance: the namespace delete carried its UID; the neighbour is untouched.
        raws = [c for c in self.writes() if '--raw' in c['flags']]
        self.assertEqual([c['flags']['--raw'] for c in raws],
                         [f'/apis/nvidia.com/v1beta1/namespaces/{ns}/dynamographdeployments/current',
                          f'/api/v1/namespaces/{ns}'])
        self.assertEqual(self.k8s['namespaces'][neighbour], before['namespaces'][neighbour])
        self.assertEqual(self.k8s['objects'][neighbour], before['objects'][neighbour])
        self.assertEqual(self.k8s['pods'][neighbour], before['pods'][neighbour])
        self.assertEqual(body['gpu_pods_left'], 1, body)

    def test_down_counts_its_own_and_stray_gpu_pods(self):
        ns, uid = NS + '-' + 'e' * 10, 'uid-current-ns'
        self.add_ns(name=ns, thread=OTHER, uid=uid)
        lid = self.add_lease(ns=ns, uid=uid, thread=OTHER, gpus=1, state='revoking')
        self.add_obj(gpu_dgd('current', 1, lid), ns=ns)
        self.add_pod(pod('current', 1), ns=ns)
        late = gpu_dgd('late', 2, 'lse_previous01')
        late['metadata']['labels'] = {NS_UID: 'uid-deleted-ns'}
        self.add_obj(late, ns=ns, stamp=False)
        self.add_pod(pod('late', 2), ns=ns)
        self.k8s['terminate_pods_on_delete'] = False
        self.flush()
        body = self.ok('--context', CTX, 'down', ns, '--uid', uid, '--lease', lid, host_entry=True)
        self.assertEqual(body['gpu_pods_left'], 2, body)
        self.assertNotIn(ns, self.k8s['namespaces'])

    def test_hours_bounded(self):
        for hours in ('0', '4.5', '-1', 'nan', 'inf'):
            with self.subTest(hours=hours):
                self.refused('usage', '--context', CTX, 'ns', 'create', 'smoke', '--issue', '12', '--hours', hours)
        self.assertEqual(self.writes(), [])

    def test_bad_names_refused(self):
        for purpose, issue in (('Smoke', '12'), ('smoke_1', '12'), ('-x', '12'), ('smoke', 'STRGT-12'),
                               ('x' * 50, '12')):
            with self.subTest(purpose=purpose, issue=issue):
                self.refused('usage', '--context', CTX, 'ns', 'create', purpose, '--issue', issue, '--hours', '1')
        self.assertEqual(self.writes(), [])

    def test_thread_required(self):
        self.refused('thread_required', '--context', CTX, 'ns', 'create', 'smoke', '--issue', '12', '--hours', '1',
                     host_entry=True)


# --- rule 5: ownership -----------------------------------------------------

class Ownership(GuardCase):
    def test_other_threads_namespace_refused(self):
        self.add_ns(thread=OTHER)
        self.assertNotEqual(ME, OTHER)
        self.refused('not_your_namespace', '--context', CTX, '-n', NS, 'apply', '-f', self.manifest(dgd('m')))
        self.refused('not_your_namespace', '--context', CTX, '-n', NS, 'secret', 'hf')
        self.refused('not_your_namespace', '--context', CTX, '-n', NS, 'delete', 'configmap/x')
        self.refused('not_your_namespace', '--context', CTX, '-n', NS, 'lease', 'acquire', '--gpus', '1',
                     '--hours', '1')
        self.assertEqual(self.writes(), [])
        # The owner can.
        self.assertEqual(self.guard('--context', CTX, '-n', NS, 'apply', '-f', self.manifest(dgd('m')),
                                    thread=OTHER)[0], 0)

    def test_namespace_outside_prefix_refused(self):
        self.add_ns(name='team-x-work', thread=ME)
        self.refused('namespace_not_ours', '--context', CTX, '-n', 'team-x-work', 'apply', '-f',
                     self.manifest(dgd('m')))
        self.refused('namespace_not_ours', '--context', CTX, 'down', 'team-x-work')
        self.assertEqual(self.writes(), [])

    def test_namespace_taken_from_the_ledger(self):
        self.add_ns()
        # One live namespace in the thread's ledger: -n may be left out.
        self.assertEqual(self.guard('--context', CTX, 'apply', '-f', self.manifest(dgd('m')))[0], 0)
        self.add_ns(name=PREFIX + '13-other', uid='uid-ns-2')
        self.refused('namespace_required', '--context', CTX, 'apply', '-f', self.manifest(dgd('m')))

    def test_namespace_not_in_the_callers_ledger_refused(self):
        # Labelled for this thread, but not created by it: never adopted.
        self.add_ns(ledger=False)
        self.refused('namespace_not_in_ledger', '--context', CTX, '-n', NS, 'apply', '-f', self.manifest(dgd('m')))
        self.refused('namespace_not_in_ledger', '--context', CTX, '-n', NS, 'secret', 'hf')
        self.assertEqual(self.writes(), [])
        self.assertFalse(self.fleet_log.exists())

    def test_ledger_uid_must_still_match(self):
        self.add_ns(uid='uid-ns-1')
        self.k8s['namespaces'][NS]['metadata']['uid'] = 'uid-ns-recreated'
        self.flush()
        for args in (('apply', '-f', self.manifest(dgd('m'))), ('secret', 'hf'), ('delete', 'configmap/x'),
                     ('lease', 'acquire', '--gpus', '1', '--hours', '1'), ('selftest', 'fences')):
            with self.subTest(command=args[0]):
                self.refused('uid_mismatch', '--context', CTX, '-n', NS, *args)
        self.assertEqual(self.writes(), [])
        self.assertFalse(any(c[:3] == ['k8s', 'lease', 'acquire'] for c in self.bb_calls()))

    def test_owner_commands_need_the_thread_ledger(self):
        self.add_ns()
        self.refused('ledger_required', '--context', CTX, '-n', NS, 'apply', '-f', self.manifest(dgd('m')),
                     storage=False)
        self.assertEqual(self.writes(), [])

    def test_object_in_another_namespace_refused(self):
        self.add_ns()
        self.add_ns(name=PREFIX + '13-other', thread=ME, uid='uid-ns-2')
        stray = dgd('m')
        stray['metadata']['namespace'] = PREFIX + '13-other'
        rc, body, _ = self.apply(stray)
        self.assertNotEqual(rc, 0, body)
        self.assertEqual(self.writes(), [])


# --- rule 4: admitted kinds ------------------------------------------------

class Kinds(GuardCase):
    def setUp(self):
        super().setUp()
        self.add_ns()

    def test_refused_kinds(self):
        pod_spec = {'containers': [container()]}
        cases = {
            'Deployment': {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'name': 'd'},
                           'spec': {'template': {'spec': pod_spec}}},
            'StatefulSet': {'apiVersion': 'apps/v1', 'kind': 'StatefulSet', 'metadata': {'name': 's'}, 'spec': {}},
            'DaemonSet': {'apiVersion': 'apps/v1', 'kind': 'DaemonSet', 'metadata': {'name': 'ds'}, 'spec': {}},
            'Job': {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': {'name': 'j'}, 'spec': {}},
            'Pod': {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': 'p'}, 'spec': pod_spec},
            'HPA': {'apiVersion': 'autoscaling/v2', 'kind': 'HorizontalPodAutoscaler', 'metadata': {'name': 'h'},
                    'spec': {}},
            'KEDA': {'apiVersion': 'keda.sh/v1alpha1', 'kind': 'ScaledObject', 'metadata': {'name': 'k'},
                     'spec': {}},
            'adapter': {'apiVersion': DGD_API, 'kind': 'DynamoGraphDeploymentScalingAdapter',
                        'metadata': {'name': 'a'}, 'spec': {}},
            'Secret': {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 's'}, 'stringData': {'a': 'b'}},
            'ClusterRole': {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'ClusterRole',
                            'metadata': {'name': 'cr'}, 'rules': []},
            'Namespace': {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': PREFIX + '99-x'}},
            'LoadBalancer': {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': 'lb'},
                             'spec': {'type': 'LoadBalancer', 'ports': [{'port': 80}]}},
            'NodePort': {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': 'np'},
                         'spec': {'type': 'NodePort', 'ports': [{'port': 80}]}},
            'lease ConfigMap': lease_cm(NS, 'lse_forged001', 'uid-ns-1', ME, '2099-01-01T00:00:00.000Z'),
            'old DGD API': dict(dgd('old'), apiVersion='nvidia.com/v1alpha0'),
        }
        for label, obj in cases.items():
            with self.subTest(kind=label):
                rc, body, _ = self.apply(obj)
                self.assertNotEqual(rc, 0, body)
                self.assertIn(body['code'], ('kind_refused', 'field_refused', 'service_type_refused'), body)
        self.assertEqual(self.writes(), [])

    def test_admitted_kinds(self):
        cm = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'cfg'}, 'data': {'a': 'b'}}
        svc = {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': 'svc'}, 'spec': {'ports': [{'port': 80}]}}
        self.assertEqual(self.apply(cm, svc, dgd('mocker'))[0], 0)
        self.assertIn('ConfigMap/cfg', self.k8s['objects'][NS])
        self.assertIn('Service/svc', self.k8s['objects'][NS])
        self.assertIsNotNone(self.dgd_obj('mocker'))

    def test_dgd_scaling_adapter_autoscaling_planner_refused(self):
        lid = self.add_lease()
        cases = {
            'v1beta1 scalingAdapter': dgd('a', component('decode', gpus=1, lease_id=lid, scalingAdapter={})),
            'planner': dgd('b', component('planner', ctype='planner')),
            'epp': dgd('c', component('epp', ctype='epp')),
            'roles': dgd('d', component('decode', roles=[{'name': 'r', 'replicas': 2}])),
            'v1alpha1 scalingAdapter': {
                'apiVersion': 'nvidia.com/v1alpha1', 'kind': 'DynamoGraphDeployment', 'metadata': {'name': 'e'},
                'spec': {'services': {'decode': {'componentType': 'worker', 'replicas': 1,
                                                 'scalingAdapter': {'enabled': True},
                                                 'resources': {'requests': {'cpu': '1', 'memory': '1Gi'},
                                                               'limits': {'cpu': '1', 'memory': '1Gi'}}}}}},
            'v1alpha1 autoscaling': {
                'apiVersion': 'nvidia.com/v1alpha1', 'kind': 'DynamoGraphDeployment', 'metadata': {'name': 'f'},
                'spec': {'services': {'decode': {'componentType': 'worker', 'replicas': 1,
                                                 'autoscaling': {'enabled': True, 'maxReplicas': 8},
                                                 'resources': {'requests': {'cpu': '1', 'memory': '1Gi'},
                                                               'limits': {'cpu': '1', 'memory': '1Gi'}}}}}},
            'v1alpha1 planner': {
                'apiVersion': 'nvidia.com/v1alpha1', 'kind': 'DynamoGraphDeployment', 'metadata': {'name': 'g'},
                'spec': {'services': {'Planner': {'componentType': 'planner', 'replicas': 1,
                                                  'resources': {'requests': {'cpu': '1', 'memory': '1Gi'},
                                                                'limits': {'cpu': '1', 'memory': '1Gi'}}}}}},
            'unknown component field': dgd('h', component('decode', lpx={'enabled': True})),
            'DRA claim': dgd('i', component('decode', podTemplate={'spec': {
                'containers': [container()], 'resourceClaims': [{'name': 'gpu', 'resourceClaimName': 'x'}]}})),
            'other GPU resource': dgd('j', component('decode', podTemplate={'spec': {'containers': [
                dict(container(), resources={'requests': {'cpu': '1', 'memory': '1Gi', 'nvidia.com/mig-1g.10gb': '1'},
                                             'limits': {'cpu': '1', 'memory': '1Gi',
                                                        'nvidia.com/mig-1g.10gb': '1'}})]}})),
        }
        for label, obj in cases.items():
            with self.subTest(case=label):
                rc, body, _ = self.apply(obj)
                self.assertNotEqual(rc, 0, body)
                self.assertEqual(body['code'], 'field_refused', body)
        self.assertEqual(self.writes(), [])

    def test_two_documents_only_second_breaks_a_rule(self):
        good = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'good'}, 'data': {}}
        bad = {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'name': 'bad'}, 'spec': {}}
        rc, body, _ = self.apply(good, bad)
        self.assertNotEqual(rc, 0)
        self.assertEqual(body['code'], 'kind_refused')
        self.assertEqual(self.writes(), [])
        self.assertNotIn('ConfigMap/good', self.k8s['objects'][NS])

    def test_same_bytes_dry_run_and_apply(self):
        self.assertEqual(self.apply(dgd('mocker'))[0], 0)
        calls = [c for c in self.kubectl_calls() if c['pos'][:1] == ['apply']]
        self.assertEqual([c['flags'].get('--dry-run') for c in calls], ['server', None])
        for c in calls:
            self.assertEqual(c['flags'].get('-f'), '-')
            self.assertEqual(c['flags'].get('-n'), NS)


# --- rule 6: manifest checks on the server dry run ------------------------

class Manifest(GuardCase):
    def setUp(self):
        super().setUp()
        self.add_ns()

    def test_forbidden_field_added_by_dry_run_refused(self):
        mutations = {
            'hostPath': {'spec': {'components': [component('Frontend', ctype='frontend', podTemplate={'spec': {
                'containers': [container()], 'volumes': [{'name': 'h', 'hostPath': {'path': '/'}}]}})]}},
            'priorityClassName': {'spec': {'priorityClassName': 'high'}},
            'privileged': {'spec': {'components': [component('Frontend', ctype='frontend', podTemplate={'spec': {
                'containers': [dict(container(), securityContext={'privileged': True})]}})]}},
            'blanket toleration': {'spec': {'components': [component('Frontend', ctype='frontend', podTemplate={
                'spec': {'containers': [container()], 'tolerations': [{'operator': 'Exists'}]}})]}},
            'hostNetwork': {'spec': {'components': [component('Frontend', ctype='frontend', podTemplate={'spec': {
                'containers': [container()], 'hostNetwork': True}})]}},
        }
        for label, extra in mutations.items():
            with self.subTest(field=label):
                self.k8s['mutate'] = {'DynamoGraphDeployment': extra}
                self.flush()
                rc, body, _ = self.apply(dgd('m'))
                self.assertNotEqual(rc, 0, body)
                self.assertEqual(body['code'], 'field_refused', body)
        self.assertEqual(self.writes(), [])

    def test_dry_run_turns_service_into_load_balancer(self):
        self.k8s['mutate'] = {'Service': {'spec': {'type': 'LoadBalancer'}}}
        self.flush()
        svc = {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': 'svc'}, 'spec': {'ports': [{'port': 80}]}}
        rc, body, _ = self.apply(svc)
        self.assertEqual(body.get('code'), 'service_type_refused', body)
        self.assertEqual(self.writes(), [])

    def test_container_without_requests_and_limits_refused(self):
        bare = dgd('m', component('Frontend', ctype='frontend',
                                  podTemplate={'spec': {'containers': [container(resources=False)]}}))
        no_limits = copy.deepcopy(dgd('n'))
        del no_limits['spec']['components'][0]['podTemplate']['spec']['containers'][0]['resources']['limits']
        sidecar = dgd('o', component('Frontend', ctype='frontend', podTemplate={'spec': {
            'containers': [container(), container(name='side', resources=False)]}}))
        for obj in (bare, no_limits, sidecar):
            with self.subTest(dgd=obj['metadata']['name']):
                rc, body, _ = self.apply(obj)
                self.assertEqual(body.get('code'), 'resources_required', body)
        self.assertEqual(self.writes(), [])

    def test_toleration_with_a_key_allowed(self):
        tol = dgd('m', component('Frontend', ctype='frontend', podTemplate={'spec': {
            'containers': [container()], 'tolerations': [
                {'key': 'nvidia.com/gpu', 'operator': 'Exists', 'effect': 'NoSchedule'}]}}))
        self.assertEqual(self.apply(tol)[0], 0)

    def test_dgd_name_length(self):
        self.assertEqual(self.apply(dgd('a' * 39))[0], 0)
        rc, body, _ = self.apply(dgd('b' * 40))
        self.assertEqual(body.get('code'), 'dgd_name_too_long', body)


# --- rule 7: budget --------------------------------------------------------

class Budget(GuardCase):
    def setUp(self):
        super().setUp()
        self.add_ns()

    def test_gpu_dgd_with_no_lease_refused(self):
        rc, body, _ = self.apply(gpu_dgd('w', 1, 'lse_none0001'))
        self.assertEqual(body.get('code'), 'lease_required', body)
        self.assertEqual(self.writes(), [])

    def test_duplicate_dgd_documents_cannot_leave_gpus_without_a_lease(self):
        # Each object is a separate API write; a later failure keeps earlier writes.
        hook = """for obj in docs(stdin):
        if not dry and out:
            save()
            fail('Error from server (InternalError): second apply request failed')"""
        fake = FAKE_KUBECTL.replace('for obj in docs(stdin):', hook)
        self.assertNotEqual(fake, FAKE_KUBECTL)
        (self.bin / 'kubectl').write_text(fake)
        running = gpu_dgd('w', 4, 'lse_none0001')
        stopped = gpu_dgd('w', 4, 'lse_none0001', replicas=0)

        rc, body, _ = self.apply(running, stopped)

        self.assertEqual(self.writes(), [], f'{body}; DGD left behind: {self.dgd_obj("w")}')
        self.assertNotEqual(rc, 0, body)
        self.assertIsNone(self.dgd_obj('w'))
        self.assertEqual(self.bb_calls(), [])

    def test_cpu_dgd_needs_no_lease(self):
        self.assertEqual(self.apply(dgd('mocker'))[0], 0)
        self.assertEqual(self.bb_calls(), [])
        [line] = self.ledger()
        self.assertEqual((line['object'], line['gpus'], line['direction']), ('DynamoGraphDeployment/mocker', 0, 'up'))

    def lease_mismatch(self, code, **lease_kw):
        lid = self.add_lease(**lease_kw)
        rc, body, _ = self.apply(gpu_dgd('w', 1, lid))
        self.assertEqual(body.get('code'), code, body)
        self.assertEqual(self.writes(), [])
        self.assertNotIn(['k8s', 'lease', 'op', 'begin', lid], self.bb_calls())

    def test_another_threads_lease_refused(self):
        self.lease_mismatch('lease_mismatch', thread=OTHER)

    def test_lease_for_another_cluster_refused(self):
        self.lease_mismatch('lease_mismatch', cluster='lab-b200')

    def test_lease_for_another_namespace_uid_refused(self):
        self.lease_mismatch('lease_mismatch', uid='uid-ns-old')

    def test_lease_for_another_gpu_type_refused(self):
        self.lease_mismatch('lease_mismatch', gpu_type='h100')

    def test_lease_not_active_refused(self):
        self.lease_mismatch('lease_not_active', state='revoking')

    def test_lease_too_small_refused(self):
        lid = self.add_lease(gpus=2)
        rc, body, _ = self.apply(gpu_dgd('w', 3, lid))
        self.assertEqual(body.get('code'), 'over_lease', body)
        self.assertEqual(self.writes(), [])
        self.assertIn(['k8s', 'lease', 'op', 'end', lid, 'tok-' + lid], self.bb_calls())

    def test_lease_unreachable_refuses_writes(self):
        lid = self.add_lease()
        self.bb['unreachable'] = True
        self.flush()
        rc, body, _ = self.apply(gpu_dgd('w', 1, lid))
        self.assertEqual(body.get('code'), 'lease_unavailable', body)
        self.assertEqual(self.writes(), [])

    def test_happy_gpu_apply_is_fenced_and_recorded(self):
        lid = self.add_lease(gpus=4)
        rc, body, p = self.apply(gpu_dgd('w', 2, lid, replicas=2))
        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        self.assertEqual(body['allocation'], 4)
        names = [c[:4] for c in self.bb_calls()]
        self.assertEqual(names, [['k8s', 'lease', 'show', lid], ['k8s', 'lease', 'op', 'begin'],
                                 ['k8s', 'lease', 'op', 'end']])
        [line] = self.ledger()
        self.assertEqual((line['gpus'], line['lease'], line['direction']), (4, lid, 'up'))
        self.assertEqual(len(self.ledger(host=True)), 1)

    def test_two_dgds_over_one_lease_refused(self):
        lid = self.add_lease(gpus=4)
        self.add_obj(gpu_dgd('first', 3, lid))
        rc, body, _ = self.apply(gpu_dgd('second', 2, lid))
        self.assertEqual(body.get('code'), 'over_lease', body)
        self.assertEqual(self.writes(), [])

    def test_reapply_of_same_dgd_counted_once(self):
        lid = self.add_lease(gpus=4)
        self.add_obj(gpu_dgd('w', 4, lid))
        self.add_pod(pod('w', 4))
        self.assertEqual(self.apply(gpu_dgd('w', 4, lid))[0], 0)

    def test_dgd_admitted_before_its_pods_exist_is_counted(self):
        lid = self.add_lease(gpus=4)
        self.add_obj(gpu_dgd('first', 4, lid))  # no pods yet
        rc, body, _ = self.apply(gpu_dgd('second', 1, lid))
        self.assertEqual(body.get('code'), 'over_lease', body)

    def test_terminating_pod_of_shrunk_dgd_still_counted(self):
        lid = self.add_lease(gpus=4)
        self.add_obj(gpu_dgd('old', 4, lid, replicas=0))
        self.add_pod(pod('old', 4, terminating=True))
        rc, body, _ = self.apply(gpu_dgd('new', 1, lid))
        self.assertEqual(body.get('code'), 'over_lease', body)
        self.assertEqual(self.writes(), [])
        self.k8s['pods'][NS] = []
        self.flush()
        self.assertEqual(self.apply(gpu_dgd('new', 1, lid))[0], 0)

    def test_finished_pods_not_counted_and_orphan_pods_counted(self):
        lid = self.add_lease(gpus=4)
        self.add_pod(pod('gone', 4, phase='Succeeded'))
        self.assertEqual(self.apply(gpu_dgd('a', 2, lid))[0], 0)
        self.add_pod(pod('deleted-dgd', 2, terminating=True))
        rc, body, _ = self.apply(gpu_dgd('b', 1, lid))
        self.assertEqual(body.get('code'), 'over_lease', body)

    def test_v1alpha1_dgd_counted(self):
        lid = self.add_lease(gpus=4)
        alpha = {'apiVersion': 'nvidia.com/v1alpha1', 'kind': 'DynamoGraphDeployment', 'metadata': {'name': 'w'},
                 'spec': {'services': {'decode': {
                     'componentType': 'worker', 'replicas': 3,
                     'resources': {'requests': {'cpu': '1', 'memory': '1Gi', 'gpu': '2'},
                                   'limits': {'cpu': '1', 'memory': '1Gi', 'gpu': '2'}},
                     'extraPodMetadata': {'labels': {'wcep.agent/lease': lid}},
                     'extraPodSpec': {'affinity': one_node(lid)}}}}}
        rc, body, _ = self.apply(alpha)
        self.assertEqual(body.get('code'), 'over_lease', body)
        self.assertEqual(body.get('allocation'), 6, body)

    def test_one_node_affinity_required(self):
        lid = self.add_lease(gpus=4)
        no_affinity = gpu_dgd('a', 1, lid)
        del no_affinity['spec']['components'][1]['podTemplate']['spec']['affinity']
        no_label = gpu_dgd('b', 1, lid)
        del no_label['spec']['components'][1]['podTemplate']['metadata']['labels']
        wrong_lease = gpu_dgd('c', 1, 'lse_other0001')
        wrong_lease['spec']['components'][1]['podTemplate']['metadata']['labels'] = {'wcep.agent/lease': lid}
        narrowed = gpu_dgd('d', 1, lid)
        narrowed['spec']['components'][1]['podTemplate']['spec']['affinity']['podAffinity'][
            'requiredDuringSchedulingIgnoredDuringExecution'][0]['labelSelector']['matchLabels']['x'] = 'y'
        zone = gpu_dgd('e', 1, lid)
        zone['spec']['components'][1]['podTemplate']['spec']['affinity']['podAffinity'][
            'requiredDuringSchedulingIgnoredDuringExecution'][0]['topologyKey'] = 'topology.kubernetes.io/zone'
        for obj in (no_affinity, no_label, wrong_lease, narrowed, zone):
            with self.subTest(dgd=obj['metadata']['name']):
                rc, body, _ = self.apply(obj)
                self.assertEqual(body.get('code'), 'one_node_required', body)
        self.assertEqual(self.writes(), [])

    def test_every_prefix_of_the_apply_must_fit(self):
        # Growing a new DGD before shrinking an old one: the end state fits, the moment between does not.
        lid = self.add_lease(gpus=4)
        self.add_obj(gpu_dgd('old', 4, lid))
        rc, body, _ = self.apply(gpu_dgd('new', 4, lid), gpu_dgd('old', 4, lid, replicas=0))
        self.assertEqual(body.get('code'), 'over_lease', body)
        self.assertEqual(body.get('allocation'), 8, body)
        self.assertEqual(self.writes(), [])
        # Shrink then grow in one file: the shrink may fail while the grow lands, so it is refused too.
        rc, body, _ = self.apply(gpu_dgd('old', 4, lid, replicas=0), gpu_dgd('new', 4, lid))
        self.assertEqual(body.get('code'), 'over_lease', body)
        # In two applies, it fits.
        self.assertEqual(self.apply(gpu_dgd('old', 4, lid, replicas=0))[0], 0)
        self.assertEqual(self.apply(gpu_dgd('new', 4, lid))[0], 0)

    def test_scheduler_name_refused(self):
        lid = self.add_lease(gpus=2)
        c = component('decode', gpus=1, lease_id=lid)
        c['podTemplate']['spec']['schedulerName'] = 'some-scheduler'
        rc, body, _ = self.apply(dgd('w', c))
        self.assertEqual(body.get('code'), 'field_refused', body)
        self.assertEqual(self.writes(), [])

    def test_namespace_recreated_before_the_apply_refused(self):
        lid = self.add_lease(gpus=4)
        hook = """if cmd == 'apply':
    if flags.get('--dry-run') == 'server':
        st['namespaces'][ns_flag]['metadata']['uid'] = 'uid-recreated'
        save()"""
        fake = FAKE_KUBECTL.replace("if cmd == 'apply':", hook, 1)
        self.assertNotEqual(fake, FAKE_KUBECTL)
        (self.bin / 'kubectl').write_text(fake.replace(hook, hook + "\n    st = json.load(open(STATE))", 1))
        rc, body, _ = self.apply(gpu_dgd('w', 1, lid))
        self.assertEqual(body.get('code'), 'uid_mismatch', body)
        self.assertEqual(self.writes(), [])

    def test_node_name_cannot_bypass_single_node_lease(self):
        lid = self.add_lease(gpus=2, multi_node=False)
        first = component('first', gpus=1, lease_id=lid)
        second = component('second', gpus=1, lease_id=lid)
        first['podTemplate']['spec']['nodeName'] = 'gpu-node-a'
        second['podTemplate']['spec']['nodeName'] = 'gpu-node-b'

        rc, body, _ = self.apply(dgd('w', first, second))

        self.assertEqual(self.writes(), [], body)
        self.assertNotEqual(rc, 0, body)

    def test_larger_than_one_node_refused_without_multi_node(self):
        lid = self.add_lease(gpus=8)
        rc, body, _ = self.apply(gpu_dgd('big', 4, lid, replicas=2))  # 8 GPUs, 4 per node
        self.assertEqual(body.get('code'), 'one_node_required', body)
        multinode = gpu_dgd('mn', 2, lid)
        multinode['spec']['components'][1]['multinode'] = {'nodeCount': 2}
        rc, body, _ = self.apply(multinode)
        self.assertEqual(body.get('code'), 'one_node_required', body)
        self.assertEqual(self.writes(), [])

    def test_multi_node_lease_allows_more_than_one_node(self):
        lid = self.add_lease(gpus=8, multi_node=True)
        big = gpu_dgd('big', 4, lid, replicas=2)
        del big['spec']['components'][1]['podTemplate']['spec']['affinity']
        self.assertEqual(self.apply(big)[0], 0)

    def test_slot_deadline_passed_refused(self):
        lid = self.add_lease()
        self.bb['slot_seconds'] = -5
        self.flush()
        rc, body, _ = self.apply(gpu_dgd('w', 1, lid))
        self.assertEqual(body.get('code'), 'slot_expired', body)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.bb_calls()[-1][:4], ['k8s', 'lease', 'op', 'end'])

    def test_slot_busy_refused(self):
        lid = self.add_lease()
        self.bb['leases'][lid]['slot'] = 'tok-someone-else'
        self.flush()
        rc, body, _ = self.apply(gpu_dgd('w', 1, lid))
        self.assertEqual(body.get('code'), 'slot_busy', body)
        self.assertEqual(self.writes(), [])

    def test_op_end_reports_revoked_runs_expire_at_once(self):
        lid = self.add_lease(gpus=4)
        self.bb['op_end_state'] = 'revoking'
        self.flush()
        rc, body, _ = self.apply(gpu_dgd('w', 2, lid))
        self.assertNotEqual(rc, 0, body)
        self.assertEqual(body.get('code'), 'lease_revoked', body)
        self.assertEqual([c['replicas'] for c in self.dgd_obj('w')['spec']['components']], [0, 0])
        directions = [l['direction'] for l in self.ledger()]
        self.assertEqual(directions, ['up', 'down'])

    def test_apply_failure_still_ends_the_slot(self):
        lid = self.add_lease()
        self.k8s['apply_fail'] = True
        self.flush()
        rc, body, _ = self.apply(gpu_dgd('w', 1, lid))
        self.assertEqual(body.get('code'), 'kubectl_error', body)
        self.assertEqual(self.bb_calls()[-1][:4], ['k8s', 'lease', 'op', 'end'])
        self.assertEqual(self.ledger(), [])

    def test_failed_apply_then_revoked_still_expires(self):
        # A timed-out apply may have landed: a revoked lease is undone whatever the apply reported.
        lid = self.add_lease(gpus=4)
        self.add_obj(gpu_dgd('w', 2, lid))
        self.k8s['apply_fail'] = True
        self.bb['op_end_state'] = 'revoking'
        self.flush()
        rc, body, _ = self.apply(gpu_dgd('w', 2, lid))
        self.assertEqual(body.get('code'), 'lease_revoked', body)
        self.assertEqual([c['replicas'] for c in self.dgd_obj('w')['spec']['components']], [0, 0])

    def test_leased_namespace_fences_cpu_writes_too(self):
        lid = self.add_lease()
        self.assertEqual(self.apply(dgd('mocker'))[0], 0)
        self.assertIn(['k8s', 'lease', 'op', 'begin', lid], self.bb_calls())


# --- lease acquire ---------------------------------------------------------

class LeaseAcquire(GuardCase):
    def test_acquire_passes_profile_name_uid_thread_and_type(self):
        self.add_ns()
        self.k8s['forbid'] = ['patch namespace', 'replace']  # never touch the Namespace after creation
        self.flush()
        body = self.ok('--context', CTX, '-n', NS, 'lease', 'acquire', '--gpus', '2', '--hours', '3')
        call = next(c for c in self.bb_calls() if c[:3] == ['k8s', 'lease', 'acquire'])
        opts = {call[i][2:]: call[i + 1] for i in range(3, len(call) - 1, 2)}
        self.assertEqual(opts, {'cluster': PROFILE, 'namespace': NS, 'uid': 'uid-ns-1', 'thread': ME,
                                'gpu-type': 'gb300', 'gpus': '2', 'hours': '3'})
        cm = self.k8s['objects'][NS]['ConfigMap/' + LEASE_CM]
        self.assertEqual(cm['data'], {'lease_id': body['lease_id'], 'expires_at': body['expires_at'],
                                      'thread': ME, 'uid': 'uid-ns-1'})
        self.assertEqual(self.k8s['namespaces'][NS]['metadata']['resourceVersion'], '10')

    def test_second_acquire_refused(self):
        self.add_ns()
        self.add_lease()
        self.refused('lease_exists', '--context', CTX, '-n', NS, 'lease', 'acquire', '--gpus', '1', '--hours', '1')
        self.assertFalse(any(c[:3] == ['k8s', 'lease', 'acquire'] for c in self.bb_calls()))

    def test_plugin_refusal_passed_through(self):
        self.add_ns()
        self.bb['acquire_refuse'] = 'over_limit'
        self.flush()
        self.refused('over_limit', '--context', CTX, '-n', NS, 'lease', 'acquire', '--gpus', '4', '--hours', '1')
        self.assertNotIn('ConfigMap/' + LEASE_CM, self.k8s['objects'][NS])


# --- rule 9: teardown ------------------------------------------------------

class Expire(GuardCase):
    def setUp(self):
        super().setUp()
        self.add_ns(thread=OTHER)
        self.lid = None

    def busy(self, **lease_kw):
        self.lid = self.add_lease(thread=OTHER, **lease_kw)
        self.add_obj(gpu_dgd('w', 2, self.lid, replicas=2))
        self.add_pod(pod('w', 2, name='w-0'))
        self.add_pod(pod('w', 2, name='w-1'))

    def expire(self, *extra, **env_kw):
        env_kw.setdefault('host_entry', True)
        return self.guard('--context', CTX, 'expire', NS, '--uid', 'uid-ns-1', '--lease', self.lid, *extra, **env_kw)

    def replicas(self):
        return [c['replicas'] for c in self.dgd_obj('w')['spec']['components']]

    def test_revoking_lease_scales_busy_dgd_to_zero(self):
        self.busy(state='revoking')
        rc, body, p = self.expire()
        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        self.assertEqual(self.replicas(), [0, 0])
        self.assertEqual(body['gpu_pods_left'], 2)
        self.assertEqual(self.ledger(host=True)[-1]['direction'], 'down')
        patches = [c for c in self.writes() if c['pos'][0] == 'patch']
        ops = json.loads(patches[0]['flags']['-p'])
        self.assertEqual(ops[0], {'op': 'test', 'path': '/metadata/uid', 'value': 'uid-w'})

    def test_releasing_lease_allowed(self):
        self.busy(state='releasing')
        self.assertEqual(self.expire()[0], 0)
        self.assertEqual(self.replicas(), [0, 0])

    def test_offline_past_expiry_allowed(self):
        past = iso_ms(hours_from_now(-1))
        self.add_ns(thread=OTHER, expires_at=past)
        self.busy(expires_at=past)
        self.bb['unreachable'] = True
        self.flush()
        self.assertEqual(self.expire()[0], 0)
        self.assertEqual(self.replicas(), [0, 0])

    def test_offline_expiry_read_from_the_lease_configmap(self):
        # The creation annotation says expired; an extension moved the ConfigMap's expiry later.
        past = iso_ms(hours_from_now(-1))
        self.add_ns(thread=OTHER, expires_at=past)
        self.busy(expires_at=iso_ms(hours_from_now(1)))
        self.bb['unreachable'] = True
        self.flush()
        self.assertEqual(self.expire()[1].get('code'), 'not_expired')
        self.assertEqual(self.replicas(), [1, 2])

    def test_active_lease_refused(self):
        self.busy(state='active')
        self.assertEqual(self.expire()[1].get('code'), 'lease_active')
        self.assertEqual(self.replicas(), [1, 2])

    def test_wrong_uid_refused(self):
        self.busy(state='revoking')
        rc, body, _ = self.guard('--context', CTX, 'expire', NS, '--uid', 'uid-ns-OLD', '--lease', self.lid,
                                 host_entry=True)
        self.assertEqual(body.get('code'), 'uid_mismatch')
        self.assertEqual(self.replicas(), [1, 2])

    def test_lease_for_another_namespace_uid_refused(self):
        self.busy(state='revoking', uid='uid-ns-OLD')
        self.assertEqual(self.expire()[1].get('code'), 'lease_mismatch')
        self.assertEqual(self.replicas(), [1, 2])

    def test_lease_for_another_cluster_refused(self):
        self.busy(state='revoking', cluster='lab-b200')
        self.assertEqual(self.expire()[1].get('code'), 'lease_mismatch')

    def test_offline_not_yet_expired_refused(self):
        self.busy(state='active')
        self.bb['unreachable'] = True
        self.flush()
        self.assertEqual(self.expire()[1].get('code'), 'not_expired')
        self.assertEqual(self.replicas(), [1, 2])

    def test_unknown_lease_is_a_refusal_not_offline(self):
        past = iso_ms(hours_from_now(-1))
        self.add_ns(thread=OTHER, expires_at=past)
        self.busy(state='revoking', expires_at=past)
        rc, body, _ = self.guard('--context', CTX, 'expire', NS, '--uid', 'uid-ns-1', '--lease', 'lse_made0up01',
                                 host_entry=True)
        self.assertEqual(body.get('code'), 'lease_not_found')
        self.assertEqual(self.replicas(), [1, 2])

    def test_terminal_lease_reply_cannot_use_offline_teardown_authority(self):
        for method in ('expire', 'down'):
            for state in ('released', 'torn-down'):
                with self.subTest(method=method, state=state):
                    self.k8s = {'namespaces': {}, 'objects': {}, 'pods': {}}
                    self.bb = {'leases': {}}
                    self.kubectl_log.unlink(missing_ok=True)
                    past = iso_ms(hours_from_now(-1))
                    self.add_ns(thread=OTHER, expires_at=past)
                    lid = self.add_lease(thread=OTHER, state=state, expires_at=past)
                    self.add_obj(gpu_dgd('w', 2, lid))

                    rc, body, _ = self.guard('--context', CTX, method, NS, '--uid', 'uid-ns-1',
                                             '--lease', lid, host_entry=True)

                    self.assertEqual(self.bb['leases'][lid]['state'], state)
                    self.assertEqual(self.writes(), [], body)
                    self.assertNotEqual(rc, 0, body)
                    self.assertIn(NS, self.k8s['namespaces'])
                    self.assertEqual(self.dgd_obj('w')['spec']['components'][1]['replicas'], 1)

    def test_owner_cannot_expire_own_active_lease(self):
        self.busy(state='active')
        self.assertEqual(self.expire(host_entry=False, thread=OTHER)[1].get('code'), 'lease_active')

    def test_namespace_recreated_during_expire_left_alone(self):
        self.busy(state='revoking')
        self.k8s['recreate_ns_on_list'] = True
        self.flush()
        self.assertEqual(self.expire()[1].get('code'), 'uid_mismatch')
        self.assertEqual(self.replicas(), [1, 2])
        self.assertEqual([c for c in self.writes() if c['pos'][0] == 'patch'], [])

    def test_unstamped_dgd_never_touched(self):
        # A DGD without this namespace instance's stamp was not applied here by the guard.
        self.busy(state='revoking')
        self.add_obj(gpu_dgd('other', 2, self.lid), stamp=False)
        self.assertEqual(self.expire()[0], 0)
        self.assertEqual(self.replicas(), [0, 0])
        self.assertEqual([c['replicas'] for c in self.dgd_obj('other')['spec']['components']], [1, 1])

    def test_v1alpha1_scaled_to_zero(self):
        self.busy(state='revoking')
        alpha = {'apiVersion': 'nvidia.com/v1alpha1', 'kind': 'DynamoGraphDeployment', 'metadata': {'name': 'a'},
                 'spec': {'services': {'Frontend': {'replicas': 2}, 'decode/x': {'replicas': 3}}}}
        self.add_obj(alpha)
        self.assertEqual(self.expire()[0], 0)
        svcs = self.dgd_obj('a')['spec']['services']
        self.assertEqual((svcs['Frontend']['replicas'], svcs['decode/x']['replicas']), (0, 0))


class Down(GuardCase):
    def test_revoking_lease_down_by_host_entry(self):
        self.add_ns(thread=OTHER)
        lid = self.add_lease(thread=OTHER, state='revoking')
        self.add_obj(gpu_dgd('w', 2, lid))
        self.add_pod(pod('w', 2))
        rc, body, p = self.guard('--context', CTX, 'down', NS, '--uid', 'uid-ns-1', '--lease', lid, host_entry=True)
        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        self.assertNotIn(NS, self.k8s['namespaces'])
        raws = [c for c in self.writes() if '--raw' in c['flags']]
        self.assertEqual([c['flags']['--raw'] for c in raws],
                         [f'/apis/nvidia.com/v1beta1/namespaces/{NS}/dynamographdeployments/w',
                          f'/api/v1/namespaces/{NS}'])
        self.assertEqual(self.ledger(host=True)[-1]['direction'], 'down')

    def test_releasing_lease_down_by_host_entry(self):
        self.add_ns(thread=OTHER)
        lid = self.add_lease(thread=OTHER, state='releasing')
        self.assertEqual(self.guard('--context', CTX, 'down', NS, '--uid', 'uid-ns-1', '--lease', lid,
                                    host_entry=True)[0], 0)
        self.assertNotIn(NS, self.k8s['namespaces'])

    def test_offline_past_expiry_down(self):
        self.add_ns(thread=OTHER, expires_at=iso_ms(hours_from_now(-2)))
        self.bb['unreachable'] = True
        self.flush()
        self.assertEqual(self.guard('--context', CTX, 'down', NS, '--uid', 'uid-ns-1', host_entry=True)[0], 0)
        self.assertNotIn(NS, self.k8s['namespaces'])

    def test_offline_needs_uid(self):
        self.add_ns(thread=OTHER, expires_at=iso_ms(hours_from_now(-2)))
        self.bb['unreachable'] = True
        self.flush()
        self.refused('uid_required', '--context', CTX, 'down', NS, host_entry=True)
        self.assertIn(NS, self.k8s['namespaces'])

    def test_owner_down_on_active_lease_releases_first(self):
        self.add_ns()
        lid = self.add_lease()
        self.add_obj(gpu_dgd('w', 2, lid))
        self.add_pod(pod('w', 2))
        rc, body, p = self.guard('--context', CTX, 'down', NS)
        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        self.assertIn(['k8s', 'lease', 'release', lid], self.bb_calls())
        self.assertEqual(self.bb['leases'][lid]['state'], 'releasing')
        # The release came before the first cluster write.
        first_write = self.writes()[0]
        self.assertTrue(first_write['pos'][0] == 'delete')
        self.assertNotIn(NS, self.k8s['namespaces'])
        self.assertEqual(self.ledger()[-1]['direction'], 'down')

    def test_owner_down_without_lease(self):
        self.add_ns()
        self.add_obj(dgd('mocker'))
        self.assertEqual(self.guard('--context', CTX, 'down', NS)[0], 0)
        self.assertEqual(self.bb_calls(), [])
        self.assertNotIn(NS, self.k8s['namespaces'])

    def test_other_thread_down_on_active_lease_refused(self):
        self.add_ns(thread=OTHER)
        lid = self.add_lease(thread=OTHER)
        self.refused('lease_active', '--context', CTX, 'down', NS, '--uid', 'uid-ns-1', '--lease', lid)
        self.assertNotIn(['k8s', 'lease', 'release', lid], self.bb_calls())
        self.assertIn(NS, self.k8s['namespaces'])
        self.assertEqual(self.writes(), [])

    def test_owner_down_when_release_fails_offline_and_not_expired(self):
        self.add_ns()
        self.add_lease()
        self.bb['unreachable'] = True
        self.flush()
        self.refused('lease_release_failed', '--context', CTX, 'down', NS)
        self.assertIn(NS, self.k8s['namespaces'])

    def test_reports_gpu_pods_left_and_deletes_the_namespace(self):
        self.add_ns(thread=OTHER)
        lid = self.add_lease(thread=OTHER, state='releasing')
        self.add_obj(gpu_dgd('w', 2, lid))
        self.add_pod(pod('w', 2))
        self.k8s['terminate_pods_on_delete'] = False
        self.flush()
        rc, body = self.down_waiting(NS, '--uid', 'uid-ns-1', '--lease', lid, host_entry=True)
        self.assertEqual(rc, 0, body)
        self.assertGreaterEqual(self.pod_polls(), 2)
        self.assertEqual(body['gpu_pods_left'], 1)
        self.assertNotIn(NS, self.k8s['namespaces'])

    def test_wrong_uid_refused(self):
        self.add_ns(thread=OTHER)
        lid = self.add_lease(thread=OTHER, state='revoking')
        self.refused('uid_mismatch', '--context', CTX, 'down', NS, '--uid', 'uid-ns-OLD', '--lease', lid,
                     host_entry=True)
        self.assertIn(NS, self.k8s['namespaces'])

    def test_namespace_recreated_during_down_left_alone(self):
        self.add_ns(thread=OTHER)
        lid = self.add_lease(thread=OTHER, state='revoking')
        self.add_obj(gpu_dgd('w', 2, lid))
        self.k8s['recreate_ns_on_list'] = True
        self.flush()
        self.refused('uid_mismatch', '--context', CTX, 'down', NS, '--uid', 'uid-ns-1', '--lease', lid,
                     host_entry=True)
        self.assertIsNotNone(self.dgd_obj('w'))
        self.assertIn(NS, self.k8s['namespaces'])
        self.assertEqual(self.writes(), [])

    def test_missing_namespace_not_found(self):
        self.refused('not_found', '--context', CTX, 'down', NS, '--uid', 'uid-ns-1', '--lease', 'lse_test0001',
                     host_entry=True)


class DownWaitDeadline(GuardCase):
    def down_elapsed(self, wait, with_gpu_pod):
        self.add_ns(thread=OTHER)
        lid = self.add_lease(thread=OTHER, state='releasing')
        if with_gpu_pod:
            self.add_pod(pod('unmatched', 1))
        self.runner.write_text(RUNNER.replace("runpy.run_path(sys.argv[0], run_name='__main__')", r'''
import json
from pathlib import Path

class Clock:
    elapsed = 0.0

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.elapsed += seconds

guard = runpy.run_path(sys.argv[0])
clock = Clock()
guard['cmd_down'].__globals__['time'] = clock
status = guard['main']()
state_path = Path(os.environ['FAKE_K8S_STATE'])
state = json.loads(state_path.read_text())
state['down_elapsed_seconds'] = clock.elapsed
state_path.write_text(json.dumps(state))
sys.exit(status)
''', 1))
        env = self.env(host_entry=True)
        env['K8S_GUARD_DOWN_WAIT_SECONDS'] = str(wait)
        del env['K8S_GUARD_POLL_SECONDS']
        p = subprocess.run(['python3', '-I', str(self.runner), str(GUARD), '--context', CTX, 'down', NS,
                            '--uid', 'uid-ns-1', '--lease', lid], env=env, capture_output=True, text=True,
                           timeout=60, stdin=subprocess.DEVNULL)
        self.reload()
        body = json.loads(p.stdout)
        self.assertEqual(p.returncode, 0, f'{body} {p.stderr}')
        self.assertEqual(body['gpu_pods_left'], int(with_gpu_pod), body)
        self.assertNotIn(NS, self.k8s['namespaces'])
        return self.k8s['down_elapsed_seconds']

    def test_no_gpu_pods_delete_without_waiting(self):
        self.assertEqual(self.down_elapsed(wait=1, with_gpu_pod=False), 0)

    def test_full_poll_intervals_respect_the_deadline(self):
        self.assertEqual(self.down_elapsed(wait=10, with_gpu_pod=True), 10)

    @unittest.skip("F20: down sleeps past its wait deadline when the remaining time is less than one poll")
    def test_f20_last_poll_is_capped_at_the_deadline(self):
        elapsed = self.down_elapsed(wait=1, with_gpu_pod=True)
        self.assertLessEqual(elapsed, 1, 'the default 5-second poll exceeded the configured 1-second wait')


class DownClassificationRaces(GuardCase):
    def test_f18_retry_after_foreign_name_reuse_counts_every_pod(self):
        # Ruling on F16, F18, F19 (driver): no pod classification. down waits, bounded, for every GPU
        # pod, then deletes the namespace under its UID precondition and reports what was left.
        self.add_ns(thread=OTHER)
        lid = self.add_lease(thread=OTHER, state='revoking')
        self.add_obj(gpu_dgd('shared', 1, lid), uid='uid-owned-dgd')
        owned_pod = pod('shared', 1, name='owned-still-running')
        owned_pod['metadata']['ownerReferences'] = [
            {'apiVersion': DGD_API, 'kind': 'DynamoGraphDeployment', 'name': 'shared',
             'uid': 'uid-owned-dgd', 'controller': True}]
        self.add_pod(owned_pod)
        self.k8s['terminate_pods_on_delete'] = False
        self.flush()

        # The foreign DGD takes the name after the owned one is gone, and its pod runs too.
        replacement = gpu_dgd('shared', 2, 'lse_previous01')
        replacement['metadata'].update(namespace=NS, uid='uid-foreign-dgd', resourceVersion='1000',
                                       labels={NS_UID: 'uid-deleted-ns'})
        anchor = "        del st['objects'][ns]['DynamoGraphDeployment/' + name]\n"
        hook = anchor + "        st['objects'][ns]['DynamoGraphDeployment/' + name] = REPLACEMENT\n" \
                        "        st['pods'][ns].append(FOREIGN_POD)\n"
        foreign_pod = pod('shared', 2, name='foreign-running')
        hook = hook.replace('REPLACEMENT', repr(replacement)).replace('FOREIGN_POD', repr(foreign_pod))
        self.assertEqual(FAKE_KUBECTL.count(anchor), 1)
        (self.bin / 'kubectl').write_text(FAKE_KUBECTL.replace(anchor, hook, 1))

        rc, body = self.down_waiting(NS, '--uid', 'uid-ns-1', '--lease', lid, host_entry=True)

        self.assertEqual(rc, 0, body)
        self.assertGreaterEqual(self.pod_polls(), 2, 'down did not wait for the pods')
        self.assertEqual(body['gpu_pods_left'], 2, body)  # the owned pod is counted, not hidden
        self.assertNotIn(NS, self.k8s['namespaces'])

    def test_f19_foreign_replacement_pods_do_not_hold_the_namespace(self):
        self.add_ns(thread=OTHER)
        lid = self.add_lease(thread=OTHER, state='revoking')
        self.add_obj(gpu_dgd('shared', 1, lid), uid='uid-owned-dgd')
        self.add_pod(pod('shared', 1, name='owned-pod'))
        replacement = gpu_dgd('shared', 2, 'lse_previous01')
        replacement['metadata'].update(namespace=NS, uid='uid-foreign-dgd', resourceVersion='1000',
                                       labels={NS_UID: 'uid-deleted-ns'})
        foreign_pod = pod('shared', 2, name='foreign-still-running')
        foreign_pod['metadata']['ownerReferences'] = [
            {'apiVersion': DGD_API, 'kind': 'DynamoGraphDeployment', 'name': 'shared',
             'uid': 'uid-foreign-dgd', 'controller': True}]
        self.k8s['replacement_dgd'] = replacement
        self.k8s['replacement_pod'] = foreign_pod
        self.flush()
        anchor = "        save(); emit({'kind': 'Status', 'status': 'Success'})\n    fail('fake kubectl: no handler for raw delete '"
        hook = """        st['pods_after_owned_delete'] = copy.deepcopy(st['pods'][ns])
        st['objects'][ns]['DynamoGraphDeployment/' + name] = st['replacement_dgd']
        st['pods'][ns] = [st['replacement_pod']]
""" + anchor
        self.assertEqual(FAKE_KUBECTL.count(anchor), 1)
        (self.bin / 'kubectl').write_text(FAKE_KUBECTL.replace(anchor, hook, 1))

        rc, body = self.down_waiting(NS, '--uid', 'uid-ns-1', '--lease', lid, host_entry=True)

        self.assertEqual(self.k8s['pods_after_owned_delete'], [])
        self.assertEqual(rc, 0, f'only the foreign replacement has GPU pods, but down kept the namespace: {body}')
        self.assertNotIn(NS, self.k8s['namespaces'])
        self.assertEqual(body['gpu_pods_left'], 1, body)

    def test_f16_foreign_replacement_cannot_hide_a_deleted_owned_dgds_pod(self):
        # New behavior: the owned pod is waited for and counted; the namespace then goes anyway.
        self.add_ns(thread=OTHER)
        lid = self.add_lease(thread=OTHER, state='revoking')
        self.add_obj(gpu_dgd('shared', 1, lid), uid='uid-owned-dgd')
        owned_pod = pod('shared', 1, name='owned-still-running')
        owned_pod['metadata']['ownerReferences'] = [
            {'apiVersion': DGD_API, 'kind': 'DynamoGraphDeployment', 'name': 'shared',
             'uid': 'uid-owned-dgd', 'controller': True}]
        self.add_pod(owned_pod)
        replacement = gpu_dgd('shared', 2, 'lse_previous01')
        replacement['metadata'].update(namespace=NS, uid='uid-foreign-dgd', resourceVersion='1000',
                                       labels={NS_UID: 'uid-deleted-ns'})
        self.k8s['replacement_dgd'] = replacement
        self.k8s['terminate_pods_on_delete'] = False
        self.flush()
        # A late foreign apply reuses the DGD name after its delete, while its owned pod still runs.
        anchor = "        del st['objects'][ns]['DynamoGraphDeployment/' + name]\n"
        hook = anchor + """        st['objects'][ns]['DynamoGraphDeployment/' + name] = st['replacement_dgd']
        st['owned_pods_after_dgd_delete'] = copy.deepcopy(st['pods'][ns])
"""
        self.assertEqual(FAKE_KUBECTL.count(anchor), 1)
        (self.bin / 'kubectl').write_text(FAKE_KUBECTL.replace(anchor, hook, 1))

        rc, body = self.down_waiting(NS, '--uid', 'uid-ns-1', '--lease', lid, host_entry=True)

        self.assertEqual(self.k8s['owned_pods_after_dgd_delete'], [owned_pod])
        self.assertEqual(rc, 0, body)
        self.assertGreaterEqual(self.pod_polls(), 2, 'down did not wait for the owned pod')
        self.assertEqual(body['gpu_pods_left'], 1, body)
        self.assertNotIn(NS, self.k8s['namespaces'])

    def test_down_on_a_terminating_namespace_sends_no_second_delete(self):
        # k8s-leases calls down every tick until the namespace is gone.
        self.add_ns(thread=OTHER)
        lid = self.add_lease(thread=OTHER, state='releasing')
        self.k8s['namespaces'][NS]['metadata']['deletionTimestamp'] = '2026-01-01T00:00:00Z'
        self.k8s['namespaces'][NS]['status']['phase'] = 'Terminating'
        self.flush()
        body = self.ok('--context', CTX, 'down', NS, '--uid', 'uid-ns-1', '--lease', lid, host_entry=True)
        self.assertTrue(body['terminating'], body)
        self.assertEqual([c for c in self.writes() if c['flags'].get('--raw', '').startswith('/api/v1/')], [])


# --- load-bearing: a namespace re-created under the same name is never touched ---

class RecreatedNamespace(GuardCase):
    """k8s-leases sends expire, down and extend without inspecting first, with the UID it stored.

    If the namespace was deleted and made again under the same name, the stored UID is old.
    The guard must refuse every one of them before any write reaches the API, whatever the
    lease state says and whether the plugin can be reached.
    """

    def setUp(self):
        super().setUp()
        # The new namespace: same name, new UID, another thread, already past its creation expiry.
        self.add_ns(thread=ME, uid='uid-ns-NEW', expires_at=iso_ms(hours_from_now(-1)))
        self.add_obj(gpu_dgd('theirs', 2, 'lse_new000001'))
        self.add_pod(pod('theirs', 2))

    def send(self, method, state, offline=False):
        lid = self.add_lease('lse_old000001', thread=OTHER, uid='uid-ns-1', state=state,
                             expires_at=iso_ms(hours_from_now(-1)), cm=False)
        self.bb['unreachable'] = offline
        self.flush()
        before = copy.deepcopy(self.k8s)
        body = self.refused('uid_mismatch', '--context', CTX, method, NS, '--uid', 'uid-ns-1', '--lease', lid,
                            host_entry=True)
        self.assertEqual(self.writes(), [], f'{method} wrote to a re-created namespace')
        self.assertEqual(self.k8s['namespaces'], before['namespaces'])
        self.assertEqual(self.k8s['objects'], before['objects'])
        self.assertEqual(self.ledger(host=True), [])
        return body

    def test_expire_with_old_uid_refused(self):
        for state in ('revoking', 'releasing'):
            with self.subTest(state=state):
                self.send('expire', state)

    def test_down_with_old_uid_refused(self):
        for state in ('revoking', 'releasing'):
            with self.subTest(state=state):
                self.send('down', state)

    def test_extend_with_old_uid_refused(self):
        self.send('extend', 'active')

    def test_offline_with_old_uid_refused(self):
        for method in ('expire', 'down', 'extend'):
            with self.subTest(method=method):
                body = self.send(method, 'revoking', offline=True)
                self.assertIn('uid-ns-NEW', body['error'])


# --- extend ----------------------------------------------------------------

class Extend(GuardCase):
    def setUp(self):
        super().setUp()
        self.add_ns(thread=OTHER)
        self.k8s['forbid'] = ['patch namespace']
        self.flush()

    def test_recreated_namespace_between_uid_and_record_read_refused(self):
        lid = self.add_lease(thread=OTHER)
        hook = """if what == 'configmap':
        st['namespaces'][ns_flag]['metadata']['uid'] = 'uid-recreated'
        st['namespaces'][ns_flag]['metadata']['labels']['wcep.agent/thread'] = 'thr_recreated'
        st['objects'][ns_flag] = {}
        st['pods'][ns_flag] = []
        save()"""
        fake = FAKE_KUBECTL.replace("if what == 'configmap':", hook)
        self.assertNotEqual(fake, FAKE_KUBECTL)
        (self.bin / 'kubectl').write_text(fake)

        rc, body, _ = self.guard('--context', CTX, 'extend', NS, '--uid', 'uid-ns-1', '--lease', lid,
                                 host_entry=True)

        self.assertEqual(self.k8s['namespaces'][NS]['metadata']['uid'], 'uid-recreated')
        self.assertEqual(self.writes(), [], body)
        self.assertNotEqual(rc, 0, body)
        self.assertEqual(body.get('code'), 'uid_mismatch', body)
        self.assertNotIn('ConfigMap/' + LEASE_CM, self.k8s['objects'][NS])

    def test_sets_expiry_to_exactly_the_lease_and_never_patches_namespace(self):
        lid = self.add_lease(thread=OTHER, expires_at='2026-11-05T03:00:00.000Z')
        self.bb['leases'][lid]['expires_at'] = '2026-11-05T04:30:00.123Z'  # the panel extended it
        self.flush()
        rc, body, p = self.guard('--context', CTX, 'extend', NS, '--uid', 'uid-ns-1', '--lease', lid,
                                 host_entry=True)
        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        cm = self.k8s['objects'][NS]['ConfigMap/' + LEASE_CM]
        self.assertEqual(cm['data']['expires_at'], '2026-11-05T04:30:00.123Z')
        self.assertEqual(self.k8s['namespaces'][NS]['metadata']['resourceVersion'], '10')
        replace = [c for c in self.writes() if c['pos'][0] == 'replace']
        self.assertEqual(len(replace), 1)
        inspect = self.ok('--context', CTX, 'inspect', NS, host_entry=True)
        self.assertEqual(inspect['expires_at'], '2026-11-05T04:30:00.123Z')

    def test_late_extend_with_older_expiry_keeps_the_newer_one(self):
        lid = self.add_lease(thread=OTHER, expires_at='2026-11-05T05:00:00.000Z')
        # A call that read the lease before the last extension lands now, with the older expiry.
        self.bb['leases'][lid]['expires_at'] = '2026-11-05T04:00:00.000Z'
        self.flush()
        body = self.ok('--context', CTX, 'extend', NS, '--uid', 'uid-ns-1', '--lease', lid, host_entry=True)
        self.assertEqual(body['expires_at'], '2026-11-05T05:00:00.000Z')
        self.assertTrue(body['unchanged'])
        cm = self.k8s['objects'][NS]['ConfigMap/' + LEASE_CM]
        self.assertEqual(cm['data']['expires_at'], '2026-11-05T05:00:00.000Z')
        self.assertEqual([c for c in self.writes() if c['pos'][0] == 'replace'], [])

    def test_extend_writes_under_resource_version(self):
        lid = self.add_lease(thread=OTHER, expires_at='2026-11-05T03:00:00.000Z')
        self.bb['leases'][lid]['expires_at'] = '2026-11-05T04:00:00.000Z'
        # Another writer changed the record after the guard read it: the fake's stored version moves on.
        (self.bin / 'kubectl').write_text(FAKE_KUBECTL.replace(
            "if cmd == 'replace':", "if cmd == 'replace':\n    st['objects'][ns_flag]['ConfigMap/' + "
                                    "'wcep-agent-lease']['metadata']['resourceVersion'] = 'moved'"))
        self.flush()
        self.refused('record_conflict', '--context', CTX, 'extend', NS, '--uid', 'uid-ns-1', '--lease', lid,
                     host_entry=True)
        self.assertEqual(self.k8s['objects'][NS]['ConfigMap/' + LEASE_CM]['data']['expires_at'],
                         '2026-11-05T03:00:00.000Z')

    def test_any_caller_may_extend(self):
        lid = self.add_lease(thread=OTHER)
        self.assertEqual(self.guard('--context', CTX, 'extend', NS, '--lease', lid, thread=ME)[0], 0)

    def test_refused_for_revoking_lease(self):
        lid = self.add_lease(thread=OTHER, state='revoking')
        self.refused('lease_not_active', '--context', CTX, 'extend', NS, '--uid', 'uid-ns-1', '--lease', lid,
                     host_entry=True)

    def test_refused_for_wrong_uid(self):
        lid = self.add_lease(thread=OTHER)
        self.refused('uid_mismatch', '--context', CTX, 'extend', NS, '--uid', 'uid-ns-OLD', '--lease', lid,
                     host_entry=True)
        lid2 = self.add_lease('lse_test0002', thread=OTHER, uid='uid-ns-OLD', cm=False)
        self.refused('lease_mismatch', '--context', CTX, 'extend', NS, '--uid', 'uid-ns-1', '--lease', lid2,
                     host_entry=True)

    def test_refused_for_lease_of_another_cluster(self):
        lid = self.add_lease(thread=OTHER, cluster='lab-b200')
        self.refused('lease_mismatch', '--context', CTX, 'extend', NS, '--uid', 'uid-ns-1', '--lease', lid,
                     host_entry=True)

    def test_refused_offline(self):
        lid = self.add_lease(thread=OTHER)
        self.bb['unreachable'] = True
        self.flush()
        self.refused('lease_unavailable', '--context', CTX, 'extend', NS, '--uid', 'uid-ns-1', '--lease', lid,
                     host_entry=True)

    def test_configmap_conflict_refused(self):
        lid = self.add_lease(thread=OTHER)
        self.k8s['objects'][NS]['ConfigMap/' + LEASE_CM]['data']['lease_id'] = 'lse_someother1'
        self.flush()
        self.refused('lease_record_mismatch', '--context', CTX, 'extend', NS, '--uid', 'uid-ns-1', '--lease', lid,
                     host_entry=True)


# --- inspect ---------------------------------------------------------------

class Inspect(GuardCase):
    def test_reports_the_contract_fields(self):
        self.add_ns(thread=OTHER)
        lid = self.add_lease(thread=OTHER, gpus=4, expires_at='2026-11-05T03:00:00.000Z')
        self.add_obj(gpu_dgd('a', 2, lid))
        self.add_obj(gpu_dgd('b', 1, lid, replicas=0))
        self.add_pod(pod('a', 1, name='a-0'))
        self.add_pod(pod('b', 2, terminating=True, name='b-0'))
        self.add_pod(pod('a', 1, phase='Failed', name='a-old'))
        body = self.ok('--context', CTX, 'inspect', NS, host_entry=True)
        self.assertEqual({k: body[k] for k in ('uid', 'thread', 'expires_at', 'dgd_gpus', 'pod_gpus', 'gpu_type',
                                                'gpu_pods_left')},
                         {'uid': 'uid-ns-1', 'thread': OTHER, 'expires_at': '2026-11-05T03:00:00.000Z',
                          'dgd_gpus': 2, 'pod_gpus': 3, 'gpu_type': 'gb300', 'gpu_pods_left': 2})
        self.assertEqual(body['allocation'], 4)  # max per DGD: a 2, b 2
        self.assertEqual(body['lease_id'], lid)
        self.assertEqual(self.writes(), [])

    def test_expiry_falls_back_to_creation_annotation(self):
        self.add_ns(thread=OTHER, expires_at='2026-11-05T01:00:00Z')
        body = self.ok('--context', CTX, 'inspect', NS, host_entry=True)
        self.assertEqual(body['expires_at'], '2026-11-05T01:00:00Z')

    def test_missing_namespace_is_not_found(self):
        self.refused('not_found', '--context', CTX, 'inspect', NS, host_entry=True)

    def test_lookup_failure_is_not_not_found(self):
        self.add_ns()
        self.k8s['unreachable'] = True
        self.flush()
        body = self.refused('kubectl_error', '--context', CTX, 'inspect', NS, host_entry=True)
        self.assertNotEqual(body['code'], 'not_found')


# --- secrets ---------------------------------------------------------------

class Secrets(GuardCase):
    def assert_no_leak(self, p):
        self.assertNotIn(SECRET, p.stdout)
        self.assertNotIn(SECRET, p.stderr)
        self.assertNotIn(SECRET, self.kubectl_log.read_text())
        for path in (self.storage / 'k8s-ledger.jsonl', self.state_home / 'k8s-guard/ledger.jsonl'):
            if path.exists():
                self.assertNotIn(SECRET, path.read_text())

    def test_hf_secret_piped_never_printed(self):
        self.add_ns()
        rc, body, p = self.guard('--context', CTX, '-n', NS, 'secret', 'hf')
        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        self.assertEqual(body, {'secret': 'hf-token-secret', 'namespace': NS})
        stored = self.k8s['objects'][NS]['Secret/hf-token-secret']
        self.assertEqual(stored['stringData'], {'HF_TOKEN': SECRET})
        self.assertEqual(self.fleet_log.read_text().split(), ['secret', 'get', 'op://Fleet/HuggingFace/credential'])
        self.assert_no_leak(p)
        self.assertEqual(self.ledger()[0]['object'], 'Secret/hf-token-secret')

    def test_ngc_pull_secret_piped_never_printed(self):
        self.add_ns()
        rc, body, p = self.guard('--context', CTX, '-n', NS, 'secret', 'ngc')
        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        self.assertEqual(body['secret'], 'nvcr-pull')
        stored = self.k8s['objects'][NS]['Secret/nvcr-pull']
        self.assertEqual(stored['type'], 'kubernetes.io/dockerconfigjson')
        cfg = json.loads(stored['stringData']['.dockerconfigjson'])
        self.assertEqual(cfg['auths']['nvcr.io']['password'], SECRET)
        self.assertEqual(cfg['auths']['nvcr.io']['username'], '$oauthtoken')
        self.assert_no_leak(p)

    def test_existing_secret_refused_without_leak(self):
        self.add_ns()
        self.add_obj({'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'hf-token-secret'}})
        rc, body, p = self.guard('--context', CTX, '-n', NS, 'secret', 'hf')
        self.assertEqual(body.get('code'), 'secret_exists', body)
        self.assert_no_leak(p)

    def test_kubectl_error_echoing_the_value_is_scrubbed(self):
        self.add_ns()
        self.k8s['forbid'] = ['create secret']
        self.flush()
        # Make the fake's error text carry the value, as a worst case.
        leaky = FAKE_KUBECTL.replace("fail(f'Error from server (Forbidden): fake forbids \"{needle}\"')",
                                     "fail('Error from server (Forbidden): ' + stdin)")
        self.assertNotEqual(leaky, FAKE_KUBECTL)
        (self.bin / 'kubectl').write_text(leaky)
        rc, body, p = self.guard('--context', CTX, '-n', NS, 'secret', 'hf')
        self.assertNotEqual(rc, 0)
        self.assert_no_leak(p)

    def test_secret_crossing_error_truncation_boundary_is_scrubbed(self):
        self.add_ns()
        self.k8s['forbid'] = ['create secret']
        self.flush()
        # Truncating before redaction leaves a credential prefix that no full-value match can find.
        leaky = FAKE_KUBECTL.replace(
            "fail(f'Error from server (Forbidden): fake forbids \"{needle}\"')",
            "fail('x' * 580 + stdin)")
        self.assertNotEqual(leaky, FAKE_KUBECTL)
        (self.bin / 'kubectl').write_text(leaky)

        rc, body, p = self.guard('--context', CTX, '-n', NS, 'secret', 'hf')

        self.assertNotEqual(rc, 0, body)
        self.assertEqual(body.get('code'), 'kubectl_error', body)
        self.assert_no_leak(p)
        self.assertNotIn(SECRET[:20], p.stdout)
        self.assertNotIn(SECRET[:20], p.stderr)

    def test_unknown_secret_refused(self):
        self.add_ns()
        self.refused('usage', '--context', CTX, '-n', NS, 'secret', 'aws')
        self.assertFalse(self.fleet_log.exists())


# --- delete ----------------------------------------------------------------

class Delete(GuardCase):
    def setUp(self):
        super().setUp()
        self.add_ns()

    def test_owner_deletes_dgd(self):
        lid = self.add_lease()
        self.add_obj(gpu_dgd('w', 2, lid))
        body = self.ok('--context', CTX, '-n', NS, 'delete', 'dgd/w')
        self.assertIsNone(self.dgd_obj('w'))
        self.assertEqual((self.ledger()[-1]['direction'], self.ledger()[-1]['gpus']), ('down', 2))
        self.assertEqual(body['deleted'], 'DynamoGraphDeployment/w')

    def test_delete_in_gpu_namespace_requires_active_lease_and_slot(self):
        for mode in ('no_lease', 'revoking', 'offline', 'slot_busy', 'slot_expired'):
            with self.subTest(mode=mode):
                self.k8s = {'namespaces': {}, 'objects': {}, 'pods': {}}
                self.bb = {'leases': {}}
                self.kubectl_log.unlink(missing_ok=True)
                self.add_ns()
                lid = 'lse_none0001' if mode == 'no_lease' else self.add_lease(
                    state='revoking' if mode == 'revoking' else 'active')
                self.add_obj(gpu_dgd('w', 2, lid))
                self.add_obj({'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'scratch'}})
                if mode == 'offline':
                    self.bb['unreachable'] = True
                elif mode == 'slot_busy':
                    self.bb['leases'][lid]['slot'] = 'tok-another-operation'
                elif mode == 'slot_expired':
                    self.bb['slot_seconds'] = -5
                self.flush()

                rc, body, _ = self.guard('--context', CTX, '-n', NS, 'delete', 'configmap/scratch')

                self.assertEqual(self.writes(), [], body)
                self.assertNotEqual(rc, 0, body)
                self.assertIn('ConfigMap/scratch', self.k8s['objects'][NS])

    def test_protected_objects_refused(self):
        self.add_lease()
        for target in (f'configmap/{LEASE_CM}', 'pvc/shared-model-cache', 'deployment/x', 'pod/x',
                       'namespace/' + NS, 'clusterrole/x', 'dgd/../x', 'dgd/'):
            with self.subTest(target=target):
                rc, body, _ = self.guard('--context', CTX, '-n', NS, 'delete', target)
                self.assertNotEqual(rc, 0, body)
                self.assertIn(body['code'], ('kind_refused', 'object_protected', 'usage'), body)
        self.assertEqual(self.writes(), [])

    def test_leftovers_deletable(self):
        for kind, name in (('PersistentVolumeClaim', 'scratch'), ('PodCliqueSet', 'w-pcs'), ('Service', 's')):
            self.add_obj({'apiVersion': 'v1', 'kind': kind, 'metadata': {'name': name}})
        for target in ('pvc/scratch', 'podcliqueset/w-pcs', 'svc/s'):
            with self.subTest(target=target):
                self.ok('--context', CTX, '-n', NS, 'delete', target)


# --- environment the k8s-leases host entry gives -----------------------------

class HostEntryEnvironment(GuardCase):
    def test_every_host_command_runs_with_only_the_host_environment(self):
        self.add_ns(thread=OTHER)
        lid = self.add_lease(thread=OTHER, state='active')
        self.bb['leases'][lid]['expires_at'] = iso_ms(hours_from_now(3))  # the panel extended it
        self.flush()
        self.ok('--context', CTX, 'inspect', NS, host_entry=True)
        self.ok('--context', CTX, 'extend', NS, '--uid', 'uid-ns-1', '--lease', lid, host_entry=True)
        self.bb['leases'][lid]['state'] = 'revoking'
        self.flush()
        self.ok('--context', CTX, 'expire', NS, '--uid', 'uid-ns-1', '--lease', lid, host_entry=True)
        self.ok('--context', CTX, 'down', NS, '--uid', 'uid-ns-1', '--lease', lid, host_entry=True)
        self.assertEqual(self.ledger(), [])  # no thread storage: the host ledger only
        self.assertEqual([l['direction'] for l in self.ledger(host=True)], ['extend', 'down', 'down'])


class SlotWriteWindows(GuardCase):
    def test_final_namespace_read_cannot_outlive_the_operation_slot(self):
        self.add_ns()
        lid = self.add_lease(gpus=1)
        fake = FAKE_KUBECTL.replace("if what == 'namespace':", """if what == 'namespace':
        st['namespace_reads'] = st.get('namespace_reads', 0) + 1
        if st['namespace_reads'] == 3:
            st['elapsed_seconds'] = 125
        save()""", 1)
        fake = fake.replace("if what == " + repr(DGD_LIST) + ":", "if what == " + repr(DGD_LIST) + ":\n"
                            "        st['elapsed_seconds'] = 60\n        save()", 1)
        fake = fake.replace("if what == 'pods':", "if what == 'pods':\n"
                            "        st['elapsed_seconds'] = 75\n        save()", 1)
        self.assertNotEqual(fake, FAKE_KUBECTL)
        (self.bin / 'kubectl').write_text(fake)
        # Model bounded calls: dry run 45s, DGD read 15s, pod read 15s, final UID read 50s.
        launcher = self.tmp / 'slot-clock.py'
        launcher.write_text(RUNNER.split('sys.argv = sys.argv[1:]')[0] + """import json, os, runpy, sys
from datetime import timedelta
from pathlib import Path
scope = runpy.run_path(sys.argv[1])
real_now = scope['now']
def delayed_now():
    state = json.loads(Path(os.environ['FAKE_K8S_STATE']).read_text())
    return real_now() + timedelta(seconds=state.get('elapsed_seconds', 0))
scope['main'].__globals__['now'] = delayed_now
sys.exit(scope['main'](sys.argv[2:]))
""")
        manifest = self.manifest(gpu_dgd('w', 1, lid))

        p = subprocess.run(['python3', '-I', str(launcher), str(GUARD), '--context', CTX, '-n', NS,
                            'apply', '-f', manifest], env=self.env(), capture_output=True, text=True,
                           timeout=60, stdin=subprocess.DEVNULL)
        self.reload()
        body = json.loads(p.stdout)

        self.assertEqual(self.k8s.get('elapsed_seconds'), 125, body)
        self.assertEqual([c['pos'][0] for c in self.writes()], [], f'write after the 120s slot: {body}')
        self.assertNotEqual(p.returncode, 0, body)
        self.assertEqual(body.get('code'), 'slot_expired', body)


class GpuTypes(GuardCase):
    def test_gpu_node_selector_must_match_the_lease_type(self):
        self.add_ns()
        lid = self.add_lease(gpus=1, gpu_type='gb300')
        obj = gpu_dgd('wrong-type', 1, lid)
        obj['spec']['components'][1]['podTemplate']['spec']['nodeSelector'] = {
            'nvidia.com/gpu.product': 'NVIDIA-H100'}

        rc, body, _ = self.apply(obj)

        self.assertEqual([c['pos'][0] for c in self.writes()], [], f'gb300 lease admits an H100 GPU pod: {body}')
        self.assertNotEqual(rc, 0, body)
        self.assertIsNone(self.dgd_obj('wrong-type'))


class PartialApplyFailures(GuardCase):
    def test_failed_shrink_cannot_leave_a_later_growth_over_the_lease(self):
        self.add_ns()
        lid = self.add_lease(gpus=4)
        self.add_obj(gpu_dgd('old', 4, lid))
        # kubectl collects per-object errors and still applies the remaining objects.
        hook = """for obj in docs(stdin):
        if not dry and obj['metadata']['name'] == 'old':
            st['failed_shrink'] = True
            continue"""
        fake = FAKE_KUBECTL.replace('for obj in docs(stdin):', hook, 1)
        fake = fake.replace("    if not dry:\n        save()\n    emit(out[0]", """    if not dry:
        save()
        if st.get('failed_shrink'):
            fail('Error from server (InternalError): old shrink failed; later objects still applied')
    emit(out[0]""", 1)
        self.assertNotEqual(fake, FAKE_KUBECTL)
        (self.bin / 'kubectl').write_text(fake)

        rc, body, _ = self.apply(gpu_dgd('old', 4, lid, replicas=0), gpu_dgd('new', 4, lid))

        self.assertNotEqual(rc, 0, body)
        old, new = self.dgd_obj('old'), self.dgd_obj('new')
        held = sum(4 * obj['spec']['components'][1]['replicas'] for obj in (old, new) if obj is not None)
        self.assertLessEqual(held, self.bb['leases'][lid]['gpus'], f'{body}; allocation after partial apply={held}')


class NamespaceWriteWindows(GuardCase):
    def test_recreation_after_last_uid_read_refuses_every_host_write(self):
        for method, has_record in (('expire', True), ('down', True), ('extend', True), ('extend', False)):
            with self.subTest(method=method, has_record=has_record):
                self.k8s = {'namespaces': {}, 'objects': {}, 'pods': {}}
                self.bb = {'leases': {}}
                self.kubectl_log.unlink(missing_ok=True)
                self.bb_log.unlink(missing_ok=True)
                self.add_ns(thread=OTHER)
                lid = self.add_lease(thread=OTHER, state='active' if method == 'extend' else 'revoking',
                                     cm=has_record)
                if method == 'extend':
                    self.bb['leases'][lid]['expires_at'] = iso_ms(hours_from_now(3))
                    self.flush()
                self.add_obj(gpu_dgd('w', 2, lid))
                replacement = gpu_dgd('w', 2, 'lse_new000001')
                replacement['metadata'].update(namespace=NS, uid='uid-new-dgd', resourceVersion='1000')
                hook = """if what == 'namespace':
        st['namespace_reads'] = st.get('namespace_reads', 0) + 1
        save()
        if st['namespace_reads'] == 3:
            reply = copy.deepcopy(st['namespaces'][pos[2]])
            st['namespaces'][pos[2]]['metadata'].update(uid='uid-recreated', resourceVersion='1001')
            st['namespaces'][pos[2]]['metadata']['labels']['wcep.agent/thread'] = 'thr_recreated'
            st['objects'][pos[2]] = {'DynamoGraphDeployment/w': REPLACEMENT}
            st['pods'][pos[2]] = []
            save()
            emit(reply)""".replace('REPLACEMENT', repr(replacement))
                fake = FAKE_KUBECTL.replace("if what == 'namespace':", hook, 1)
                self.assertNotEqual(fake, FAKE_KUBECTL)
                (self.bin / 'kubectl').write_text(fake)

                rc, body, _ = self.guard('--context', CTX, method, NS, '--uid', 'uid-ns-1', '--lease', lid,
                                         host_entry=True)

                if not has_record:
                    # extend never creates the record, so it stops before the race can matter.
                    self.assertEqual(body.get('code'), 'lease_record_missing', body)
                    self.assertEqual(self.writes(), [])
                    self.assertNotIn('ConfigMap/' + LEASE_CM, self.k8s['objects'][NS])
                    continue
                self.assertEqual(self.k8s['namespaces'][NS]['metadata']['uid'], 'uid-recreated')
                self.assertEqual(self.dgd_obj('w')['spec']['components'][1]['replicas'], 1)
                # Ruling on F7: in this race no client-side read can stop the request; the server's
                # preconditions must refuse it. So every write may be sent, but none may take effect.
                self.assertEqual(self.k8s['objects'][NS], {'DynamoGraphDeployment/w': replacement}, body)
                self.assertNotEqual(rc, 0, body)
                self.assertNotIn('ConfigMap/' + LEASE_CM, self.k8s['objects'][NS])


class ApplySlotLifetime(GuardCase):
    def test_multi_object_apply_cannot_outlive_its_slot_and_overallocate(self):
        self.add_ns()
        lid = self.add_lease(gpus=3)
        # Advance a logical clock so each request takes 14s without slowing the suite.
        launcher = self.tmp / 'slot-clock.py'
        launcher.write_text(RUNNER.split('sys.argv = sys.argv[1:]')[0] + """import json, os, runpy, sys
from datetime import timedelta
from pathlib import Path
scope = runpy.run_path(sys.argv[1])
real_now = scope['now']
def delayed_now():
    state = json.loads(Path(os.environ['FAKE_K8S_STATE']).read_text())
    return real_now() + timedelta(seconds=state.get('elapsed_seconds', 0))
scope['main'].__globals__['now'] = delayed_now
sys.exit(scope['main'](sys.argv[2:]))
""")
        concurrent = self.manifest(gpu_dgd('concurrent', 1, lid))
        concurrent_argv = ['python3', '-I', str(launcher), str(GUARD), '--context', CTX, '-n', NS,
                           'apply', '-f', concurrent]
        bb_clock = """real_now = datetime.now
def logical_now():
    state = json.load(open(os.environ['FAKE_K8S_STATE']))
    return real_now(timezone.utc) + timedelta(seconds=state.get('elapsed_seconds', 0))
"""
        fake_bb = FAKE_BB.replace("argv = sys.argv[1:]", bb_clock + "\nargv = sys.argv[1:]", 1)
        fake_bb = fake_bb.replace('datetime.now(timezone.utc)', 'logical_now()')
        fake_bb = fake_bb.replace("    if l.get('slot'):\n", """    if l.get('slot') and l.get('slot_deadline'):
        if datetime.fromisoformat(l['slot_deadline'].replace('Z', '+00:00')) <= logical_now():
            l['slot'] = None
    if l.get('slot'):
""", 1)
        fake_bb = fake_bb.replace("    save(); out({'token': l['slot'], 'deadline': iso(dl)})",
                                  "    l['slot_deadline'] = iso(dl)\n"
                                  "    save(); out({'token': l['slot'], 'deadline': iso(dl)})", 1)
        self.assertNotEqual(fake_bb, FAKE_BB)
        (self.bin / 'bb').write_text(fake_bb)
        fake = FAKE_KUBECTL.replace('import copy, json, os, sys, time',
                                    'import copy, json, os, subprocess, sys, time', 1)
        fake = fake.replace("if what == 'namespace':", """if what == 'namespace':
        st['namespace_reads'] = st.get('namespace_reads', 0) + 1
        if st['namespace_reads'] == 3:
            st['elapsed_seconds'] = 79
        save()""", 1)
        # The first two writes land at 93s and 107s. Another guard gets the expired slot at 121s.
        hook = """    for obj in docs(stdin):
        if not dry and obj['metadata']['name'] in ('first', 'second', 'late'):
            st['elapsed_seconds'] += 14
            save()
            if obj['metadata']['name'] == 'late':
                other = subprocess.run(CONCURRENT_ARGV, env=dict(os.environ, KUBECONFIG=BOT_KUBECONFIG),
                                       capture_output=True,
                                       text=True, timeout=60, stdin=subprocess.DEVNULL)
                st = json.load(open(STATE))
                st['concurrent_rc'] = other.returncode
                st['concurrent_result'] = json.loads(other.stdout)
                save()
""".replace('CONCURRENT_ARGV', repr(concurrent_argv)).replace('BOT_KUBECONFIG', repr(str(self.bot_kubeconfig)))
        fake = fake.replace("    for obj in docs(stdin):\n        meta = obj.setdefault",
                            hook + "        meta = obj.setdefault", 1)
        fake = fake.replace("            st['objects'][ns_flag][key(merged)] = merged",
                            "            st['objects'][ns_flag][key(merged)] = merged\n            save()", 1)
        self.assertNotEqual(fake, FAKE_KUBECTL)
        (self.bin / 'kubectl').write_text(fake)
        manifest = self.manifest(*(gpu_dgd(name, 1, lid) for name in ('first', 'second', 'late')))

        p = subprocess.run(['python3', '-I', str(launcher), str(GUARD), '--context', CTX, '-n', NS,
                            'apply', '-f', manifest], env=self.env(), capture_output=True, text=True,
                           timeout=60, stdin=subprocess.DEVNULL)
        self.reload()

        # Ruling on F12: the guard applies one object per request and checks the slot before each.
        # After 'first' lands at 93s, 27s are left, less than one request may take, so it stops.
        body = json.loads(p.stdout)
        self.assertEqual(body.get('code'), 'slot_expired', body)
        self.assertEqual(body.get('applied'), ['DynamoGraphDeployment/first'], body)
        self.assertEqual(self.k8s.get('elapsed_seconds'), 93, body)
        # The other guard still gets the expired slot at 121s.
        self.k8s['elapsed_seconds'] = 121
        self.flush()
        other = subprocess.run(concurrent_argv, env=self.env(), capture_output=True, text=True, timeout=60,
                               stdin=subprocess.DEVNULL)
        self.reload()
        self.k8s['concurrent_rc'], self.k8s['concurrent_result'] = other.returncode, json.loads(other.stdout)
        self.assertEqual(self.k8s.get('concurrent_rc'), 0, self.k8s.get('concurrent_result'))
        self.assertEqual(self.k8s['concurrent_result']['allocation'], 2)
        self.assertEqual(sum(c[:4] == ['k8s', 'lease', 'op', 'begin'] for c in self.bb_calls()), 2)
        held = sum(int(c['podTemplate']['spec']['containers'][0]['resources']['limits'].get('nvidia.com/gpu', 0))
                   * c['replicas'] for obj in self.k8s['objects'][NS].values()
                   if obj['kind'] == 'DynamoGraphDeployment' for c in obj['spec']['components'])
        self.assertLessEqual(held, 3, f'42s apply crosses the 120s slot and leaves {held} GPUs: {p.stdout}')


class SelftestFences(GuardCase):
    def setUp(self):
        super().setUp()
        self.add_ns()
        self.lid = self.add_lease(gpus=4)
        self.add_obj(gpu_dgd('smoke', 1, self.lid))

    def test_every_fence_holds_and_nothing_changes(self):
        before = copy.deepcopy(self.k8s)
        body = self.ok('--context', CTX, '-n', NS, 'selftest', 'fences')
        self.assertTrue(body['all_held'], body)
        self.assertEqual([p['fence'] for p in body['probes']], [
            'patch: wrong DGD UID', 'patch: wrong namespace-uid label', 'delete DGD: wrong UID precondition',
            'delete namespace: wrong UID precondition', 'replace lease record: stale resourceVersion'])
        self.assertTrue(all(p['refused'] for p in body['probes']), body)
        self.assertEqual(self.k8s['namespaces'], before['namespaces'])
        self.assertEqual(self.k8s['objects'], before['objects'])
        # It ran inside the lease's slot, like every other write.
        self.assertIn(['k8s', 'lease', 'op', 'begin', self.lid], self.bb_calls())

    def test_unrelated_request_errors_do_not_prove_fences_hold(self):
        for error in ('Forbidden', 'UnsupportedMediaType'):
            with self.subTest(error=error):
                self.k8s['forbid'] = ['patch ', 'delete --raw', 'replace -f'] if error == 'Forbidden' else []
                self.flush()
                fake = FAKE_KUBECTL
                if error == 'UnsupportedMediaType':
                    fake = fake.replace("if cmd == 'delete' and '--raw' in flags:", """if cmd == 'delete' and '--raw' in flags:
    fail('Error from server (UnsupportedMediaType): unsupported Content-Type for DeleteOptions')""", 1)
                    self.assertNotEqual(fake, FAKE_KUBECTL)
                (self.bin / 'kubectl').write_text(fake)

                rc, body, _ = self.guard('--context', CTX, '-n', NS, 'selftest', 'fences')

                self.assertTrue(any(error in p['response'] for p in body.get('probes', [])), body)
                self.assertNotEqual(rc, 0, f'unrelated errors certified as working fences: {body}')

    def test_a_server_that_ignores_a_fence_is_reported(self):
        # A fake server that ignores the UID precondition on a DGD delete.
        fake = FAKE_KUBECTL.replace("        if o['metadata']['uid'] != want:\n            fail('Error from server (Conflict): Precondition failed')",
                                    "        pass", 1)
        self.assertNotEqual(fake, FAKE_KUBECTL)
        (self.bin / 'kubectl').write_text(fake)
        body = self.refused('fence_failed', '--context', CTX, '-n', NS, 'selftest', 'fences')
        self.assertFalse(body['all_held'])
        self.assertEqual([p['refused'] for p in body['probes']], [True, True, False, True, True])

    def test_another_threads_namespace_refused(self):
        self.refused('not_your_namespace', '--context', CTX, '-n', NS, 'selftest', 'fences', thread=OTHER)
        self.assertEqual(self.writes(), [])

    def test_needs_a_stamped_dgd(self):
        self.k8s['objects'][NS].pop('DynamoGraphDeployment/smoke')
        self.flush()
        self.refused('selftest_needs_dgd', '--context', CTX, '-n', NS, 'selftest', 'fences')
        self.assertEqual(self.writes(), [])


if __name__ == '__main__':
    unittest.main()

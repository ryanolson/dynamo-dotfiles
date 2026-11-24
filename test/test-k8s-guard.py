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
import re
import shlex
import subprocess
import tempfile
import time
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
LEASE_LABEL = 'wcep.agent/lease'
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

# What the API server answers the fence probe's two dry-run Pod creates, by the "cluster's" fence state. The
# refusal texts are the kind fixture's (test/kind-fence.sh, Kyverno v1.15.2 on kind): kubectl's prefix, the
# webhook that answered, and Kyverno's block message with the policy and the rule. None means admitted.
FINE = 'validate.kyverno.svc-fail-finegrained-agent-lease-fence'
CANARY_FINE = 'validate.kyverno.svc-fail-finegrained-agent-lease-fence-canary'


def kyverno_denial(webhook, rule, message, policy='agent-lease-fence'):
    return (f'Error from server: error when creating "STDIN": admission webhook "{webhook}" denied the request: '
            f'\n\nresource Pod/{{ns}}/{{name}} was blocked due to the following policies \n\n{policy}:\n'
            f"  {rule}: '{message}'\n")


def canary_denial(webhook=CANARY_FINE):
    return kyverno_denial(webhook, 'probe-canary', CANARY_MESSAGE, 'agent-lease-fence-canary')


CANARY_MESSAGE = ("agent-lease-fence canary (owner alice, contact alice; off-switch in the policy''s description): this "
                  "probe Pod is always refused; it is not a workload.")
MODE_MESSAGE = ("agent-lease-fence (owner alice, contact alice; off-switch in the policy''s description): {ns} admits "
                "a Pod only under its open agent lease, with a controller owner. Gate: missing for " + "''" * 2 +
                " (''unreadable'' means the policy could not read its gate: the policy is broken, not the lease). Pod "
                "lease label: ''lse_fenceprobe0''.")
# Whichever of our two entries the API server called first names the failure.
ENGINE_DOWN = ('Error from server (InternalError): error when creating "STDIN": Internal error occurred: failed calling '
               'webhook "{entry}": failed to call webhook: Post "https://kyverno-svc.kyverno.svc:443/validate/fail/'
               'finegrained/{policy}?timeout=10s": dial tcp 10.96.61.105:443: connect: connection refused\n')
AUDIT_WARNING = 'Warning: policy agent-lease-fence.pods-need-the-open-lease: ' + MODE_MESSAGE.replace("''", "'") + '\n'
FENCE_ANSWERS = {
    'armed': {'canary': canary_denial(), 'mode': kyverno_denial(FINE, 'pods-need-the-open-lease', MODE_MESSAGE)},
    # Admitted with Kyverno's warning on stderr: the main policy in Audit.
    'audit': {'canary': canary_denial(), 'mode': AUDIT_WARNING},
    # Both admitted, no warning: no entry of ours answered (policies gone, label gone, or Kyverno at 0 replicas).
    'disarmed': {'canary': None, 'mode': None},
    'shared': {'canary': canary_denial('validate.kyverno.svc-fail'),
               'mode': kyverno_denial('validate.kyverno.svc-fail', 'pods-need-the-open-lease', MODE_MESSAGE)},
    'main_shared': {'canary': canary_denial(),
                    'mode': kyverno_denial('validate.kyverno.svc-fail', 'pods-need-the-open-lease', MODE_MESSAGE)},
    'engine_unavailable': {'canary': ENGINE_DOWN.format(entry=FINE, policy='agent-lease-fence'),
                           'mode': ENGINE_DOWN.format(entry=CANARY_FINE, policy='agent-lease-fence-canary')},
    'main_absent': {'canary': canary_denial(), 'mode': None},
    # A cluster whose mutating Kyverno policy covers every Pod create: in an outage that webhook fails first.
    'mutate_down': {'canary': ENGINE_DOWN.format(entry='mutate.kyverno.svc-fail', policy='mutate'),
                    'mode': ENGINE_DOWN.format(entry='mutate.kyverno.svc-fail', policy='mutate')},
    # An outage where the API server called the same one of our entries first for both Pods.
    'one_entry_down': {'canary': ENGINE_DOWN.format(entry=FINE, policy='agent-lease-fence'),
                       'mode': ENGINE_DOWN.format(entry=FINE, policy='agent-lease-fence')},
    'canary_absent': {'canary': None, 'mode': kyverno_denial(FINE, 'pods-need-the-open-lease', MODE_MESSAGE)},
    # Refused, but not by our entry with our canary rule: proves nothing.
    'other_webhook': {'canary': kyverno_denial('validate.kyverno.svc-fail-finegrained-someone-else', 'probe-canary',
                                               CANARY_MESSAGE),
                      'mode': kyverno_denial('validate.kyverno.svc-fail-finegrained-someone-else',
                                             'pods-need-the-open-lease', MODE_MESSAGE)},
    'other_rule': {'canary': kyverno_denial(CANARY_FINE, 'pods-need-the-open-lease', MODE_MESSAGE,
                                            'agent-lease-fence-canary'),
                   'mode': kyverno_denial(FINE, 'pods-need-the-open-lease', MODE_MESSAGE)},
    'forbidden': {'canary': 'Error from server (Forbidden): pods is forbidden: User "bot-example-agent" cannot create '
                            'resource "pods"\n',
                  'mode': 'Error from server (Forbidden): pods is forbidden\n'},
}

FAKE_KUBECTL = r'''#!/usr/bin/env python3
"""Stateful fake kubectl. State: FAKE_K8S_STATE. Log: FAKE_KUBECTL_LOG (argv only, never stdin)."""
import copy, json, os, sys, time

STATE = os.environ['FAKE_K8S_STATE']
argv = sys.argv[1:]
VALUED = {'--kubeconfig', '--context', '-n', '--namespace', '-o', '--output', '-f', '--filename', '--type',
          '-p', '--patch', '--raw', '--request-timeout', '--from-file', '--dry-run', '--field-selector', '-l',
          '--selector', '--wait', '--tail'}
flags, pos, i = {}, [], 0
while i < len(argv):
    a = argv[i]
    if a.startswith('--') and '=' in a and a.split('=', 1)[0] in VALUED:
        k, v = a.split('=', 1); flags[k] = v; i += 1
    elif a in VALUED and not (a == '--raw' and (i + 1 >= len(argv) or argv[i + 1].startswith('-'))):
        flags[a] = argv[i + 1]; i += 2
    else:
        pos.append(a); i += 1
stdin = sys.stdin.read() if flags.get('-f', flags.get('--filename')) == '-' or \
    '/dev/stdin' in flags.get('--from-file', '') else ''
try:
    # Which program sent the call: k8s-sweep's tests require every write to come from k8s-guard.
    parent = open(f'/proc/{os.getppid()}/cmdline').read().split('\0')
except OSError:
    parent = []
with open(os.environ['FAKE_KUBECTL_LOG'], 'a') as log:
    log.write(json.dumps({'argv': argv, 'pos': pos, 'flags': flags, 'parent': parent,
                          'env': {k: os.environ.get(k) for k in ('KUBECONFIG', 'KUBECTL_KUBERC')}}) + '\n')
# The guard sends a fence probe's two Pods at once: hold the state file for the whole call.
import fcntl
_lock = open(STATE + '.lock', 'a')
fcntl.flock(_lock, fcntl.LOCK_EX)
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


# kubectl resource names and REST plurals, to kinds.
RESOURCES = {DGD_LIST_NAME: 'DynamoGraphDeployment', 'configmaps': 'ConfigMap', 'services': 'Service',
             'secrets': 'Secret', 'persistentvolumeclaims': 'PersistentVolumeClaim',
             'podcliquesets.grove.io': 'PodCliqueSet', 'podcliques.grove.io': 'PodClique',
             'deployments.apps': 'Deployment', 'statefulsets.apps': 'StatefulSet',
             'jobs.batch': 'Job'}
PLURALS = {r.split('.', 1)[0]: k for r, k in RESOURCES.items()}
PLURALS['pods'] = 'Pod'


def kind_of(res):
    if res.startswith('dynamographdeployments.'):
        return 'DynamoGraphDeployment'
    return RESOURCES.get(res) or PLURALS.get(res.split('.', 1)[0])


def labelled(items):
    if not flags.get('-l'):
        return items
    lk, lv = flags['-l'].split('=', 1)
    return [o for o in items if (o['metadata'].get('labels') or {}).get(lk) == lv]


def interfere(k, when):
    """Another writer acts on object k just before the guard's request reaches the server."""
    o = st['objects'].get(ns_flag, {}).get(k)
    for hook in st.get('interfere', []):
        if hook['key'] != k or hook['when'] != when or hook.get('times', 1) == 0:
            continue
        hook['times'] = hook.get('times', 1) - 1
        if hook['what'] == 'bump' and o is not None:
            o['metadata']['resourceVersion'] = rv()
            o.setdefault('metadata', {}).setdefault('annotations', {})['other-writer'] = o['metadata']['resourceVersion']
        elif hook['what'] == 'relabel' and o is not None:
            o['metadata'].setdefault('labels', {})['wcep.agent/lease'] = 'lse_someone9999'
            o['metadata']['resourceVersion'] = rv()
        elif hook['what'] in ('replace', 'create'):
            new = copy.deepcopy(hook['object'])
            new['metadata'].update(namespace=ns_flag, uid=next_uid(new['metadata']['name']), resourceVersion=rv())
            st['objects'].setdefault(ns_flag, {})[k] = new
        save()


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
    # Local: parse the manifest, set every label given on every object, print the objects. No API call.
    pairs = [p.split('=', 1) for p in pos[1:] if '=' in p]
    for obj in docs(stdin):
        for k, v in pairs:
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

if pos[:2] == ['auth', 'whoami']:
    emit({'kind': 'SelfSubjectReview', 'apiVersion': 'authentication.k8s.io/v1',
          'status': {'userInfo': {'username': st.get('whoami', 'bot-example-agent')}}})

if cmd == 'get':
    what = pos[1]
    if what in ('namespaces', 'ns') and len(pos) == 2:
        emit({'kind': 'List', 'apiVersion': 'v1', 'items': labelled(list(st['namespaces'].values()))})
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
        if st.get('strip_lease_after_list'):
            # Another writer takes the label off after the guard's list, changing the object in place.
            for o in items:
                o['metadata'].get('labels', {}).pop('wcep.agent/lease', None)
                o['metadata']['resourceVersion'] = rv()
            save()
        emit({'kind': 'List', 'apiVersion': 'v1', 'items': items})
    if what == 'pods':
        emit({'kind': 'List', 'apiVersion': 'v1', 'items': labelled(st['pods'].get(ns_flag, []))})
    if what.startswith('pods/'):
        name = what.split('/', 1)[1]
        for p_ in st['pods'].get(ns_flag, []):
            if p_['metadata']['name'] == name:
                emit(dict(p_, kind='Pod', apiVersion='v1'))
        fail(f'Error from server (NotFound): pods "{name}" not found')
    if what in ('policyreports.wgpolicyk8s.io', 'events'):
        emit({'kind': 'List', 'apiVersion': 'v1', 'items': st.get(what, {}).get(ns_flag, [])})
    if what in RESOURCES:
        if what in st.get('missing_resources', []):
            fail(f'error: the server doesn\'t have a resource type "{what}"')
        items = [o for o in st['objects'].get(ns_flag, {}).values() if o['kind'] == RESOURCES[what]]
        emit({'kind': 'List', 'apiVersion': 'v1', 'items': labelled(items)})
    if what == 'configmap':
        o = st['objects'].get(ns_flag, {}).get('ConfigMap/' + pos[2])
        if o is None:
            fail(f'Error from server (NotFound): configmaps "{pos[2]}" not found')
        emit(o)
    if '/' in what:
        res, name = what.split('/', 1)
        o = st['objects'].get(ns_flag, {}).get(f'{kind_of(res)}/{name}')
        if o is None:
            fail(f'Error from server (NotFound): {res} "{name}" not found')
        emit(o)
    fail('fake kubectl: no handler for get ' + ' '.join(pos[1:]), 97)

if cmd == 'create' and len(pos) == 1:
    obj = json.loads(stdin)
    if obj.get('kind') == 'Pod':
        # The fence probe: two server dry runs. Which answer the "cluster" gives comes from the test:
        # st['fence'] for every probe, or st['fence_seq'] consumed one probe (canary, then mode) at a time.
        which = (obj['metadata'].get('labels') or {}).get('wcep.agent/fence-probe')
        if flags.get('--dry-run') != 'server' or which not in ('canary', 'mode'):
            fail('fake kubectl: k8s-guard sent a Pod create that is not a fence probe dry run', 96)
        st.setdefault('probe_bodies', []).append(obj)
        if st.get('probe_sleep'):
            # Show whether the two dry runs overlap: release the state while "the server" works.
            started = time.time()
            save(); fcntl.flock(_lock, fcntl.LOCK_UN)
            time.sleep(st['probe_sleep'])
            fcntl.flock(_lock, fcntl.LOCK_EX); st = json.load(open(STATE))
            st.setdefault('probe_times', []).append([which, started, time.time()])
        # Each Pod takes its answer from its own queue, so the order the two arrive in does not matter.
        queue = st.setdefault(f'fence_seq_{which}', list(st.get('fence_seq') or []))
        state = queue.pop(0) if queue else st.get('fence', 'armed')
        answer = FENCE_ANSWERS[state][which]
        save()
        if answer is None:
            emit(obj)
        if answer.startswith('Warning:'):
            sys.stderr.write(answer)
            emit(obj)
        fail(answer.replace('{ns}', obj['metadata']['namespace']).replace('{name}', obj['metadata']['name']))
    if obj['kind'] == 'Namespace':
        if st.get('forbid_namespace_create'):
            fail('Error from server (Forbidden): namespaces is forbidden: User "bot-example-agent" cannot create '
                 'resource "namespaces" in API group "" at the cluster scope')
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
    dry = flags.get('--dry-run')
    if dry not in (None, 'server'):
        fail('fake kubectl: only server dry runs', 97)
    if obj['metadata'].get('namespace', ns_flag) != ns_flag:
        fail('error: the namespace from the provided object does not match the namespace')
    if not dry and key(obj) in st.get('stall_create', []):
        # The request reached the server, but no reply comes back: it lands only when the test says.
        st['stall_create'].remove(key(obj))
        st.setdefault('pending', []).append({'ns': ns, 'object': obj})
        save()
        if st.get('hold_file'):
            open(st['held_marker'], 'w').write('held')
            while not os.path.exists(st['hold_file']):
                time.sleep(0.02)
        fail('Unable to connect to the server: net/http: request canceled (Client.Timeout exceeded while awaiting '
             'headers)')
    if not dry:
        interfere(key(obj), 'create')
        if st.get('apply_fail'):
            if st['apply_fail'] is not True:
                st['apply_fail'] -= 1  # a count: only the first writes fail
                save()
            fail('Error from server (InternalError): fake write failure')
    if key(obj) in st['objects'][ns]:
        fail(f'Error from server (AlreadyExists): {obj["kind"].lower()}s "{obj["metadata"]["name"]}" already exists')
    if obj.get('kind') == 'Service':
        obj.setdefault('spec', {}).setdefault('type', 'ClusterIP')
    for kind, extra in st.get('mutate', {}).items():
        if kind == obj.get('kind'):
            deep_merge(obj, extra)
    if dry:
        obj['metadata'].update(namespace=ns, uid='dry-run-uid')
        emit(obj)
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
    if not dry:
        # A merge patch omits an unchanged resourceVersion, so apply is no concurrency fence: never a write.
        fail('fake kubectl: k8s-guard sent a write with kubectl apply', 96)
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
    kind = kind_of(res)
    if kind is None or flags.get('--type') != 'json':
        fail('fake kubectl: no handler for patch ' + target, 97)
    dry = flags.get('--dry-run')
    k = f'{kind}/{name}'
    if not dry:
        interfere(k, 'patch')
        if st.get('apply_fail'):
            if st['apply_fail'] is not True:
                st['apply_fail'] -= 1  # a count: only the first writes fail
                save()
            fail('Error from server (InternalError): fake write failure')
    o = st['objects'].get(ns_flag, {}).get(k)
    if o is None:
        fail(f'Error from server (NotFound): {res} "{name}" not found')
    if st.get('strip_lease_on_patch') and not dry:
        # Another writer changes the object between the guard's read and its patch.
        o['metadata'].get('labels', {}).pop('wcep.agent/lease', None)
        o['metadata']['resourceVersion'] = rv()
        save()
    o = copy.deepcopy(o); json_patch(o, json.loads(flags.get('-p') or flags.get('--patch')))
    if dry:
        emit(o)
    o['metadata']['resourceVersion'] = rv()
    st['objects'][ns_flag][k] = o; save(); emit(o)

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
        if st.get('recreate_ns_on_delete'):
            # Deleted and made again under the same name just before this delete reaches the server.
            ns['metadata']['uid'] = 'uid-recreated'
            save()
        if ns['metadata']['uid'] != want:
            fail('Error from server (Conflict): Precondition failed: UID in precondition: %s, UID in object meta: %s'
                 % (want, ns['metadata']['uid']))
        del st['namespaces'][parts[3]]; st['objects'].pop(parts[3], None); st['pods'].pop(parts[3], None)
        st.setdefault('deleted_namespaces', []).append(parts[3]); save()
        emit({'kind': 'Status', 'status': 'Success'})
    if parts[:2] == ['api', 'v1'] and len(parts) == 6 and parts[2] == 'namespaces':
        ns, plural, name = parts[3], parts[4], parts[5]
    elif parts[0] == 'apis' and len(parts) == 7 and parts[3] == 'namespaces':
        ns, plural, name = parts[4], parts[5], parts[6]
    else:
        fail('fake kubectl: no handler for raw delete ' + flags['--raw'], 97)
    kind = PLURALS.get(plural)
    k = f'{kind}/{name}'
    ns_flag = ns
    if kind == 'Pod':
        want_rv = (body.get('preconditions') or {}).get('resourceVersion')
        pods_ = st['pods'].get(ns, [])
        hit = [p_ for p_ in pods_ if p_['metadata']['name'] == name]
        if not hit:
            fail(f'Error from server (NotFound): pods "{name}" not found')
        m_ = hit[0]['metadata']
        if want is None or want_rv is None:
            fail('fake kubectl: Pod delete without UID and resourceVersion preconditions', 96)
        if m_.get('uid') != want or m_.get('resourceVersion') != want_rv:
            fail('Error from server (Conflict): Operation cannot be fulfilled: Precondition failed: UID in precondition')
        st['pods'][ns] = [p_ for p_ in pods_ if p_ is not hit[0]]
        st.setdefault('deleted_pods', []).append(name)
        save(); emit({'kind': 'Status', 'status': 'Success'})
    interfere(k, 'delete')
    o = st['objects'].get(ns, {}).get(k)
    if o is None:
        fail(f'Error from server (NotFound): {plural} "{name}" not found')
    want_rv = (body.get('preconditions') or {}).get('resourceVersion')
    if want is None or want_rv is None:
        fail(f'fake kubectl: {kind} delete without UID and resourceVersion preconditions', 96)
    if kind == 'DynamoGraphDeployment' and st.get('strip_lease_before_delete') and not st.get('stripped_lease'):
        # The label goes after the guard's read of the object, before its delete lands.
        st['stripped_lease'] = True
        o['metadata'].get('labels', {}).pop('wcep.agent/lease', None)
        o['metadata']['resourceVersion'] = rv()
        save()
    if o['metadata']['uid'] != want:
        fail('Error from server (Conflict): Operation cannot be fulfilled: Precondition failed: UID in precondition: '
             '%s, UID in object meta: %s' % (want, o['metadata']['uid']))
    if o['metadata'].get('resourceVersion') != want_rv:
        st.setdefault('delete_conflicts', []).append(name); save()
        fail('Error from server (Conflict): Operation cannot be fulfilled: Precondition failed: ResourceVersion in '
             'precondition: %s, ResourceVersion in object meta: %s' % (want_rv, o['metadata'].get('resourceVersion')))
    st.setdefault('raw_delete_bodies', []).append({'path': flags['--raw'], 'body': body})
    del st['objects'][ns][k]
    if kind == 'Job':
        # Background propagation: the Job goes at once, its Pods with it (the garbage collector's work).
        st['pods'][ns] = [p for p in st['pods'].get(ns, [])
                          if p['metadata'].get('labels', {}).get('job-name') != name]
    if kind == 'DynamoGraphDeployment':
        pods = st['pods'].get(ns, [])
        if st.get('terminate_pods_on_delete', True):
            st['pods'][ns] = [p for p in pods if p['metadata'].get('labels', {}).get(DGD_LABEL) != name]
        else:
            for p in pods:
                if p['metadata'].get('labels', {}).get(DGD_LABEL) == name:
                    p['metadata']['deletionTimestamp'] = now_iso()
    save(); emit({'kind': 'Status', 'status': 'Success'})
    fail('fake kubectl: no handler for raw delete ' + flags['--raw'], 97)

if cmd == 'logs':
    # kubectl logs job/<name> --tail=200: the fake's logs come from the test's job_logs map.
    st.setdefault('logs_calls', []).append({'argv': argv, 'pos': pos, 'flags': flags})
    save()
    key = pos[1] if len(pos) > 1 else ''
    sys.stdout.write(st.get('job_logs', {}).get(key, ''))
    sys.exit(int(st.get('job_logs_rc', 0)))

if cmd == 'delete':
    fail('fake kubectl: k8s-guard sent a delete without UID and resourceVersion preconditions', 96)
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
'''.replace('DGD_LIST_NAME', repr(DGD_LIST)).replace('DGD_LABEL', repr(DGD_NAME_LABEL)).replace(
    'FENCE_ANSWERS', 'json.loads(' + repr(json.dumps(FENCE_ANSWERS)) + ')')

FAKE_BB = r'''#!/usr/bin/env python3
"""Fake bb: the k8s-leases commands the guard may call. State: FAKE_BB_STATE. Log: FAKE_BB_LOG."""
import json, os, sys
from datetime import datetime, timedelta, timezone

argv = sys.argv[1:]
st = json.load(open(os.environ['FAKE_BB_STATE']))
with open(os.environ['FAKE_BB_LOG'], 'a') as log:
    log.write(json.dumps(argv) + '\n')
if st.get('unreachable'):
    if '--json' in argv:
        # bb's own form: with --json the CLI prints its error envelope on stdout as well.
        print(json.dumps({'ok': False, 'error': {'code': 'server_unreachable',
                                                 'message': 'bb is not running at http://127.0.0.1:1'}}, indent=2))
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


def live_uid(ns):
    # The plugin reads the live namespace on a host before a fresh claim decides. The fake kubectl's state
    # file is that read; a replay already returned above, so only a fresh claim reaches this.
    path = os.environ.get('FAKE_K8S_STATE')
    if not path or not os.path.exists(path):
        return None
    try:
        state = json.load(open(path))
    except json.JSONDecodeError:
        return None
    return state.get('namespaces', {}).get(ns, {}).get('metadata', {}).get('uid')


def kubectl_writes():
    # How many cluster writes the fake kubectl has seen: an attempt must be recorded before its write.
    path = os.environ.get('FAKE_KUBECTL_LOG')
    if not path or not os.path.exists(path):
        return 0
    n = 0
    for line in open(path):
        call = json.loads(line)
        pos = call['pos']
        if pos and pos[0] in ('create', 'apply', 'patch', 'replace', 'delete') and call['flags'].get('--dry-run') is None:
            n += 1
    return n


if argv[:4] == ['k8s', 'lease', 'acquire', '--claim']:
    trial = '--audit-trial' in argv
    o = {a[2:]: b for a, b in zip(*[iter([x for x in argv[4:] if x != '--audit-trial'])] * 2)}
    if st.get('claim_refuse'):
        err(st['claim_refuse'], 'refused by the fake plugin (the fence probe)')
    ledger = os.path.join(os.environ.get('BB_THREAD_STORAGE', '/nonexistent'), 'k8s-ledger.jsonl')
    st.setdefault('claim_calls', []).append({'request': o['request'], 'replaces': o['replaces'],
                                             'uid': o['uid'],
                                             'ledger_had_request': os.path.exists(ledger) and o['request'] in open(ledger).read()})
    save()
    if st.get('claims', 'off') == 'off':
        err('enforcement_unhealthy', 'Claims of a pre-made namespace stay off until the admission webhook is installed.')
    if st.get('claim_error_before_commit'):
        st['claim_error_before_commit'] = False
        save()
        err('internal', 'the claim failed before it committed')
    for l in st['leases'].values():
        if l.get('request') == o['request']:
            out({'lease_id': l['lease_id'], 'expires_at': l['expires_at'], 'state': l['state'],
                 'seq': l.get('claim_seq'), 'trial': l.get('trial', False)})
    for l in st['leases'].values():
        # The same owner adopts its own matching active claim (the plugin's adopt-or-replace).
        if (l['namespace'] == o['namespace'] and l['state'] == 'active' and l.get('mode') == 'claimed'
                and l['thread'] == os.environ.get('BB_THREAD_ID') and l['uid'] == o['uid']):
            out({'lease_id': l['lease_id'], 'expires_at': l['expires_at'], 'state': l['state'],
                 'seq': l.get('claim_seq'), 'trial': l.get('trial', False), 'adopted': True})
    ns_uid = live_uid(o['namespace'])
    if ns_uid is not None and ns_uid != o['uid']:
        err('uid_mismatch', f'{o["namespace"]} has UID {ns_uid}, not {o["uid"]}.')
    latest = st.get('latest', {}).get(o['namespace'])
    if (latest or 'none') != o['replaces']:
        print(json.dumps({'error': 'stale predecessor', 'code': 'predecessor_mismatch', 'current': latest or 'none'}))
        sys.exit(1)
    lid = st.get('next_id', 'lse_fake0001')
    now = datetime.now(timezone.utc)
    st['leases'][lid] = {'lease_id': lid, 'state': 'active', 'cluster': o['cluster'], 'namespace': o['namespace'],
                         'uid': o['uid'], 'thread': os.environ.get('BB_THREAD_ID'), 'gpu_type': o['gpu-type'],
                         'gpus': int(o['gpus']), 'expires_at': iso(now + timedelta(hours=float(o['hours']))),
                         'multi_node': False, 'mode': 'claimed', 'request': o['request'], 'trial': trial,
                         'claim_seq': 1 + max([x.get('claim_seq') or 0 for x in st['leases'].values()
                                               if x['namespace'] == o['namespace']] + [st.get('seq_floor', 0)])}
    st.setdefault('latest', {})[o['namespace']] = lid
    save()
    if st.get('claim_error_after_commit'):
        st['claim_error_after_commit'] = False
        save()
        err('internal', 'publish failed after the claim committed')
    if st.get('lose_claim_reply'):
        st['lose_claim_reply'] = False
        save()
        sys.stderr.write('Cannot connect to BB server.\n')
        sys.exit(1)
    out({'lease_id': lid, 'expires_at': st['leases'][lid]['expires_at'], 'state': 'active',
         'seq': st['leases'][lid]['claim_seq'], 'trial': trial})
if argv[:4] == ['k8s', 'lease', 'fence-retired', argv[3] if len(argv) > 3 else '']:
    l = lease(argv[3])
    if os.environ.get('BB_THREAD_ID'):
        err('not_from_thread', 'A retired fence is recorded from a host, not a bb thread.')
    l['fence_retired_proof'] = argv[argv.index('--proof') + 1]
    save(); out({'lease_id': argv[3], 'fence_retired_proof': l['fence_retired_proof']})
if argv[:2] == ['k8s', 'leases'] and '--json' in argv:
    # As the plugin's showJson prints a row; live leases unless --all.
    rows = []
    for l in st['leases'].values():
        if '--all' not in argv and l['state'] not in ('active', 'revoking', 'releasing'):
            continue
        row = {'lease_id': l['lease_id'], 'state': l['state'], 'cluster': l['cluster'], 'context': l.get('context'),
               'namespace': l['namespace'], 'uid': l['uid'], 'thread': l['thread'], 'gpu_type': l['gpu_type'],
               'gpus': l['gpus'], 'expires_at': l['expires_at'], 'multi_node': l.get('multi_node', False),
               'renewals': 0, 'reason': l.get('reason'), 'grace_deadline': l.get('grace_deadline'),
               'mode': l.get('mode', 'created'), 'predecessor': None}
        rows.append(row)
    print(json.dumps(rows)); sys.exit(0)
if argv[:3] == ['k8s', 'lease', 'latest']:
    o = {argv[i][2:]: argv[i + 1] for i in range(3, len(argv) - 1, 2)}
    lid = st.get('latest', {}).get(o['namespace'])
    out({'lease_id': lid, 'state': st['leases'][lid]['state'] if lid else None})
if argv[:4] == ['k8s', 'lease', 'attempt', 'begin']:
    l = lease(argv[4])
    o = {argv[i][2:]: argv[i + 1] for i in range(6, len(argv) - 1, 2)}
    if l['state'] != 'active':
        err('lease_not_active', f'Lease {argv[4]} is {l["state"]}.')
    if l.get('slot') != argv[5]:
        err('slot_lost', 'Not your slot.')
    if any(a.get('outcome') is None for a in l.get('attempts', [])):
        err('attempt_unresolved', 'An earlier write is unresolved.')
    l.setdefault('attempts', []).append({'attempt': o['attempt'], 'verb': o['verb'], 'kind': o['kind'], 'name': o['name'],
                                         'uid': o.get('uid'), 'rv': o.get('rv'), 'outcome': None,
                                         'writes_before': kubectl_writes()})
    save(); out({'attempt': o['attempt'], 'epoch': len(l['attempts'])})
if argv[:4] == ['k8s', 'lease', 'attempt', 'resolve']:
    l = lease(argv[4])
    outcome = argv[argv.index('--outcome') + 1]
    done = False
    for a in l.get('attempts', []):
        if a['attempt'] == argv[5] and a['outcome'] is None:
            a['outcome'], done = outcome, True
    save(); out({'resolved': done})
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
    l = dict(lease(argv[3]))
    l['open_attempts'] = [{k: a[k] for k in ('attempt', 'verb', 'kind', 'name', 'uid', 'rv')}
                          for a in l.pop('attempts', []) if a.get('outcome') is None]
    out(l)
if argv[:4] == ['k8s', 'lease', 'op', 'begin']:
    l = lease(argv[4])
    if l['state'] != 'active':
        err('not_active', f'Lease {argv[4]} is {l["state"]}.')
    if any(a.get('outcome') is None for a in l.get('attempts', [])):
        err('attempt_unresolved', 'An earlier write is unresolved.')
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
if [ -n "$FAKE_FLEET_FAIL" ]; then echo "no item found" >&2; exit 1; fi
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
# The fake kubectl is a script; the real rule (an ELF binary at a fixed path) is unit-tested in
# test-k8s-render.py. The pin (sha256) is still checked, against the fake.
sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[1])))
import k8s_profiles
k8s_profiles.KUBECTL_INSTALLS = (os.environ['FAKE_KUBECTL'],)
k8s_profiles.is_elf = lambda path: True
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
'''

# tbot v18.7.6 `kube credentials`, as in test-k8s-check.py: prints an ExecCredential with a PEM
# cert and key. Each run is logged, so a test can count the snapshots.
FAKE_TBOT = r'''#!/usr/bin/env python3
import json, os, sys
from datetime import datetime, timedelta, timezone
assert len(sys.argv) == 4 and sys.argv[1:3] == ['kube', 'credentials'], sys.argv
open(os.environ['FAKE_TBOT_LOG'], 'a').write('snapshot\n')
expiry = (datetime.now(timezone.utc) + timedelta(seconds=3600)).strftime('%Y-%m-%dT%H:%M:%SZ')
print(json.dumps({'kind': 'ExecCredential', 'apiVersion': 'client.authentication.k8s.io/v1beta1', 'spec': {},
                  'status': {'expirationTimestamp': expiry,
                             'clientCertificateData': '-----BEGIN CERTIFICATE-----\nbot-example-agent\n-----END CERTIFICATE-----\n',
                             'clientKeyData': '-----BEGIN PRIVATE KEY-----\nmade-up-key\n-----END PRIVATE KEY-----\n'}}))
'''


def kubeconfig(tbot, user=None, contexts=(CTX, CTX_QA, CTX_B200), destination=None):
    """A bot kubeconfig as tbot writes it with its exec plugin on, in JSON (also valid YAML)."""
    user = user if user is not None else {'exec': {
        'apiVersion': 'client.authentication.k8s.io/v1beta1', 'command': tbot,
        'args': ['kube', 'credentials', f'--destination-dir={destination}']}}
    return json.dumps({
        'apiVersion': 'v1', 'kind': 'Config',
        'clusters': [{'name': 'example', 'cluster': {'server': 'https://proxy.example.test:443',
                                                     'certificate-authority-data': 'Q0E='}}],
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
        'labelSelector': {'matchLabels': {LEASE_LABEL: lease_id}},
        'topologyKey': 'kubernetes.io/hostname'}]}}


def component(name, gpus=0, replicas=1, lease_id=None, ctype='worker', **extra):
    spec = {'containers': [container(gpus)]}
    meta = {}
    if lease_id:
        # Every pod template of a leased write carries the lease, GPU or not.
        meta['labels'] = {LEASE_LABEL: lease_id}
    if gpus and lease_id:
        spec['affinity'] = one_node(lease_id)
        spec['nodeSelector'] = {'nvidia.com/gpu.product': 'NVIDIA-GB300'}
    c = {'name': name, 'type': ctype, 'replicas': replicas, 'podTemplate': {'metadata': meta, 'spec': spec}}
    c.update(extra)
    return c


def dgd(name, *components, **spec_extra):
    spec = {'components': list(components) or [component('Frontend', ctype='frontend')]}
    spec.update(spec_extra)
    return {'apiVersion': DGD_API, 'kind': 'DynamoGraphDeployment', 'metadata': {'name': name}, 'spec': spec}


def gpu_dgd(name, gpus, lease_id, replicas=1):
    """A GPU DGD as `apply` writes it: the DGD itself carries the lease it was applied under."""
    d = dgd(name, component('Frontend', ctype='frontend', lease_id=lease_id),
            component('decode', gpus=gpus, replicas=replicas, lease_id=lease_id))
    d['metadata'].setdefault('labels', {})[LEASE_LABEL] = lease_id
    return d


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


def spend_job(name='gpu-run', *, namespace, recorded=False, gpus=2, parallelism=2, start=None):
    job = {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': {'name': name},
           'spec': {'parallelism': parallelism, 'activeDeadlineSeconds': 1200, 'template': {'spec': {
               'restartPolicy': 'Never', 'containers': [container(gpus)]}}}}
    if recorded:
        record = {'thread': ME, 'gpus': gpus * parallelism, 'start': start or iso_ms(hours_from_now(-0.01)),
                  'max_hours': 0.5, 'cluster': PROFILE, 'namespace': namespace}
        job['metadata']['annotations'] = {'wcep.agent/gpu-spend': json.dumps(record)}
    return job


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
        self.tbot.write_text(FAKE_TBOT)
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
        for name, body in (('bb', FAKE_BB), ('fleet', FAKE_FLEET), ('tsh', FAKE_TSH)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        # The fake kubectl is NOT on PATH: only the pinned path from k8s_profiles can run it.
        self.kubectl_path = self.tmp / 'pinned/kubectl'
        self.kubectl_path.parent.mkdir()
        self.kubectl_path.write_text(FAKE_KUBECTL)
        self.kubectl_path.chmod(0o755)
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
        self.kubectl_pin = None
        self.tbot_log = self.tmp / 'tbot.log'
        self.flush()

    def tearDown(self):
        self.assertFalse(self.tsh_log.exists(), 'tsh was invoked')
        for call in self.kubectl_calls():
            # The pinned bot kubeconfig, a context, no KUBECONFIG and kuberc off on every call.
            self.assertTrue(call['flags'].get('--kubeconfig', '').startswith('/proc/self/fd/'), call)
            if call['pos'][:2] != ['config', 'view']:  # the renderer's local read takes no context
                self.assertIn('--context', call['flags'], call)
            self.assertEqual(call['env'], {'KUBECONFIG': None, 'KUBECTL_KUBERC': 'false'}, call)

    # --- world -------------------------------------------------------------

    def flush(self):
        self.k8s_state.write_text(json.dumps(self.k8s))
        self.bb_state.write_text(json.dumps(self.bb))

    def reload(self):
        self.k8s = json.loads(self.k8s_state.read_text())
        self.bb = json.loads(self.bb_state.read_text())

    def add_ns(self, name=NS, thread=ME, uid='uid-ns-1', expires_at=None, ledger=True, cluster=PROFILE):
        if ledger:
            # As `ns create` records it in the creating thread's ledger. Marked so ledger() skips it.
            with open(self.storage / 'k8s-ledger.jsonl', 'a') as f:
                f.write(json.dumps({'thread': thread, 'cluster': cluster, 'namespace': name, 'uid': uid,
                                    'object': f'Namespace/{name}', 'direction': 'up', 'created': True,
                                    'seed': True}) + '\n')
        self.k8s['namespaces'][name] = namespace(name, thread, uid, expires_at)
        self.k8s['objects'].setdefault(name, {})
        self.k8s['pods'].setdefault(name, [])
        self.flush()

    def add_obj(self, obj, ns=NS, uid=None, stamp=True):
        obj = copy.deepcopy(obj)
        obj['metadata'].update(namespace=ns, uid=uid or f'uid-{obj["metadata"]["name"]}', resourceVersion='30')
        labels = obj['metadata'].setdefault('labels', {})
        if obj['kind'] != 'ConfigMap':
            # As `apply` writes it: stamped with the namespace instance's UID and the lease it was
            # written under. With stamp=False, an object `apply` never wrote.
            if stamp:
                labels[NS_UID] = self.k8s['namespaces'][ns]['metadata']['uid']
            else:
                labels.pop(NS_UID, None)
                labels.pop(LEASE_LABEL, None)
        if not labels:
            del obj['metadata']['labels']
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

    def pin_kubectl(self):
        """Write the fake kubectl's sha256 into the profiles, as the real pin is written per host."""
        import hashlib
        import re
        pin = self.kubectl_pin or hashlib.sha256(self.kubectl_path.read_bytes()).hexdigest()
        self.profiles.write_text(re.sub(r'kubectl_sha256 = "[0-9a-f]*"', f'kubectl_sha256 = "{pin}"',
                                        self.profiles.read_text()))

    def env(self, thread=ME, host_entry=False, kubeconfig=None, storage=True, fleet_fail=False, secret=None):
        self.pin_kubectl()
        env = {'PATH': f'{self.bin}:/usr/bin:/bin', 'HOME': str(self.home),
               'K8S_CLUSTERS_PROFILES': str(self.profiles),
               'KUBECONFIG': str(kubeconfig or self.bot_kubeconfig),
               'XDG_STATE_HOME': str(self.state_home),
               'FAKE_K8S_STATE': str(self.k8s_state), 'FAKE_KUBECTL_LOG': str(self.kubectl_log),
               'FAKE_BB_STATE': str(self.bb_state), 'FAKE_BB_LOG': str(self.bb_log),
               'FAKE_FLEET_LOG': str(self.fleet_log), 'FAKE_TSH_LOG': str(self.tsh_log),
               'FAKE_SECRET': SECRET if secret is None else secret,
               'K8S_GUARD_DOWN_WAIT_SECONDS': '0', 'K8S_GUARD_POLL_SECONDS': '0',
               # The fence's drain and probe interval; FenceArmed's Clock tests run the real defaults.
               'K8S_GUARD_FENCE_DRAIN_SECONDS': '0', 'K8S_GUARD_FENCE_PROBE_SECONDS': '0',
               'FAKE_FLEET_FAIL': '1' if fleet_fail else '',
               'FAKE_PW_DIR': str(self.home), 'FAKE_KUBECTL': str(self.kubectl_path),
               'FAKE_TBOT_LOG': str(self.tbot_log),
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

    def assert_no_leak(self, p):
        self.assertNotIn(SECRET, p.stdout)
        self.assertNotIn(SECRET, p.stderr)
        if self.kubectl_log.exists():
            self.assertNotIn(SECRET, self.kubectl_log.read_text())
        for path in (self.storage / 'k8s-ledger.jsonl', self.state_home / 'k8s-guard/ledger.jsonl'):
            if path.exists():
                self.assertNotIn(SECRET, path.read_text())


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
        self.kubectl_path.write_text(fake)
        env = self.env()
        env['FAKE_BOT_KUBECONFIG'] = str(self.bot_kubeconfig)
        p = subprocess.run(['python3', '-I', str(self.runner), str(GUARD), '--context', CTX, 'inspect', NS], env=env,
                           capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('rewritten', self.bot_kubeconfig.read_text())

    def test_kubectl_pin_mismatch_refused_before_any_call(self):
        self.add_ns()
        self.kubectl_pin = 'f' * 64
        self.refused('kubectl_mismatch', '--context', CTX, 'inspect', NS)
        self.assertEqual(self.kubectl_calls(), [])

    def test_each_run_takes_a_fresh_snapshot_and_kubectl_sees_no_exec_plugin(self):
        self.add_ns()
        hook = """if what == 'namespace':
        cfg = json.load(open(flags['--kubeconfig']))
        st.setdefault('users_seen', []).append(cfg['users'][0]['user'])
        save()"""
        fake = FAKE_KUBECTL.replace("if what == 'namespace':", hook, 1)
        self.assertNotEqual(fake, FAKE_KUBECTL)
        self.kubectl_path.write_text(fake)
        self.ok('--context', CTX, 'inspect', NS)
        self.ok('--context', CTX, 'inspect', NS)
        self.assertEqual(self.tbot_log.read_text().split(), ['snapshot', 'snapshot'])
        for user in self.k8s['users_seen']:
            self.assertEqual(set(user), {'client-certificate-data', 'client-key-data'}, user)

    def test_context_missing_from_kubeconfig_refused(self):
        self.add_ns()
        self.bot_kubeconfig.write_text(kubeconfig(str(self.tbot), contexts=(CTX_QA,), destination=self.bot_kubeconfig.parent))
        self.refused('context_missing', '--context', CTX, 'inspect', NS)
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

    def test_bot_without_namespace_create_is_told_to_claim(self):
        # The real bot has namespaced admin only: the API server refuses a Namespace create at the cluster scope.
        self.k8s['forbid_namespace_create'] = True
        self.flush()
        body = self.refused('namespace_create_denied', '--context', CTX, 'ns', 'create', 'smoke', '--issue', '12',
                            '--hours', '1')
        self.assertIn('ns claim', body['error'])
        self.assertEqual(self.k8s['namespaces'], {})
        self.assertEqual(self.ledger(), [])


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

    def test_same_request_dry_run_and_write(self):
        self.add_obj(dgd('present'))
        self.assertEqual(self.apply(dgd('mocker'), dgd('present'))[0], 0)
        for verb in ('create', 'patch'):
            calls = [c for c in self.kubectl_calls() if c['pos'][:1] == [verb] and c['flags'].get('-n') == NS]
            self.assertEqual([c['flags'].get('--dry-run') for c in calls], ['server', None], verb)
            dry, real = ([a for a in c['argv'] if not a.startswith(('--dry-run', '--request-timeout'))]
                         for c in calls)
            self.assertEqual(dry, real, 'the dry run is not the request that is sent')


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

    def test_object_without_the_lease_stamp_refused(self):
        # The local label step writes both stamps. A write that loses the lease stamp would leave a
        # DGD no teardown of that lease can find, so admission refuses it.
        self.add_lease(gpus=2)
        fake = FAKE_KUBECTL.replace(
            "            obj.setdefault('metadata', {}).setdefault('labels', {})[k] = v",
            "            if not k.endswith('/lease'):\n"
            "                obj.setdefault('metadata', {}).setdefault('labels', {})[k] = v", 1)
        self.assertNotEqual(fake, FAKE_KUBECTL)
        self.kubectl_path.write_text(fake)
        rc, body, _ = self.apply(dgd('m'))  # no lease label in the manifest: only the stamp writes it
        self.assertNotEqual(rc, 0, body)
        self.assertEqual(body.get('code'), 'field_refused', body)
        self.assertIn('wcep.agent/lease', body['error'])
        self.assertEqual(self.writes(), [])


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
        self.kubectl_path.write_text(fake)
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
        # The write is an attempt, recorded before it is sent and resolved after, inside the slot.
        self.assertEqual(names, [['k8s', 'lease', 'show', lid], ['k8s', 'lease', 'op', 'begin'],
                                 ['k8s', 'lease', 'attempt', 'begin'], ['k8s', 'lease', 'attempt', 'resolve'],
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
        for c in wrong_lease['spec']['components']:  # the labels name the lease; only the affinity names another
            c['podTemplate']['metadata']['labels'] = {'wcep.agent/lease': lid}
        narrowed = gpu_dgd('d', 1, lid)
        narrowed['spec']['components'][1]['podTemplate']['spec']['affinity']['podAffinity'][
            'requiredDuringSchedulingIgnoredDuringExecution'][0]['labelSelector']['matchLabels']['x'] = 'y'
        zone = gpu_dgd('e', 1, lid)
        zone['spec']['components'][1]['podTemplate']['spec']['affinity']['podAffinity'][
            'requiredDuringSchedulingIgnoredDuringExecution'][0]['topologyKey'] = 'topology.kubernetes.io/zone'
        for obj in (no_affinity, no_label, wrong_lease, narrowed, zone):
            with self.subTest(dgd=obj['metadata']['name']):
                rc, body, _ = self.apply(obj)
                # A missing label is check_lease_label's refusal, for every template; the rest is placement.
                want = 'lease_label_required' if obj is no_label else 'one_node_required'
                self.assertEqual(body.get('code'), want, body)
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
        hook = """if cmd == 'create' and len(pos) == 1:
    if flags.get('--dry-run') == 'server':
        st['namespaces'][ns_flag]['metadata']['uid'] = 'uid-recreated'
        save()"""
        fake = FAKE_KUBECTL.replace("if cmd == 'create' and len(pos) == 1:", hook, 1)
        self.assertNotEqual(fake, FAKE_KUBECTL)
        self.kubectl_path.write_text(fake.replace(hook, hook + "\n    st = json.load(open(STATE))", 1))
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

    def lease_label_refused(self, obj, lid):
        rc, body, _ = self.apply(obj)
        self.assertEqual(body.get('code'), 'lease_label_required', body)
        self.assertEqual(self.writes(), [])
        return body

    def test_multi_node_template_without_the_lease_label_fails_at_the_guard_v1beta1(self):
        lid = self.add_lease(gpus=8, multi_node=True)
        big = gpu_dgd('big', 4, lid)
        big['spec']['components'][1]['multinode'] = {'nodeCount': 2}
        del big['spec']['components'][1]['podTemplate']['spec']['affinity']
        del big['spec']['components'][1]['podTemplate']['metadata']['labels']
        body = self.lease_label_refused(big, lid)
        self.assertIn('components[1]', body['error'])

    def test_multi_node_template_without_the_lease_label_fails_at_the_guard_v1alpha1(self):
        lid = self.add_lease(gpus=8, multi_node=True)
        alpha = {'apiVersion': 'nvidia.com/v1alpha1', 'kind': 'DynamoGraphDeployment', 'metadata': {'name': 'w'},
                 'spec': {'services': {'decode': {
                     'componentType': 'worker', 'replicas': 1, 'multinode': {'nodeCount': 2},
                     'resources': {'requests': {'cpu': '1', 'memory': '1Gi', 'gpu': '4'},
                                   'limits': {'cpu': '1', 'memory': '1Gi', 'gpu': '4'}},
                     'extraPodSpec': {'nodeSelector': {'nvidia.com/gpu.product': 'NVIDIA-GB300'}}}}}}
        body = self.lease_label_refused(alpha, lid)
        self.assertIn('services.decode', body['error'])
        alpha['spec']['services']['decode']['extraPodMetadata'] = {'labels': {LEASE_LABEL: 'lse_other0001'}}
        self.lease_label_refused(alpha, lid)

    def test_zero_replica_and_cpu_templates_need_the_lease_label_too(self):
        lid = self.add_lease(gpus=4)
        stopped = gpu_dgd('stopped', 1, lid, replicas=0)
        del stopped['spec']['components'][1]['podTemplate']['metadata']['labels']
        self.lease_label_refused(stopped, lid)  # scaling it up later is a patch the label check never sees
        cpu = gpu_dgd('cpu', 1, lid)
        del cpu['spec']['components'][0]['podTemplate']['metadata']['labels']  # the Frontend
        self.lease_label_refused(cpu, lid)

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
        # An internal error leaves the outcome unknown: the attempt stays open, and the slot still ends.
        self.assertEqual(body.get('code'), 'write_unresolved', body)
        [attempt] = self.bb['leases'][lid]['attempts']
        self.assertIsNone(attempt['outcome'])
        self.assertEqual(self.bb_calls()[-1][:4], ['k8s', 'lease', 'op', 'end'])
        self.assertEqual(self.ledger(), [])

    def test_failed_apply_then_revoked_still_expires(self):
        # A timed-out apply may have landed: a revoked lease is undone whatever the apply reported.
        lid = self.add_lease(gpus=4)
        self.add_obj(gpu_dgd('w', 2, lid))
        self.k8s['apply_fail'] = 1  # the apply's write fails; expire's patch after it does not
        self.bb['op_end_state'] = 'revoking'
        self.flush()
        rc, body, _ = self.apply(gpu_dgd('w', 2, lid))
        self.assertEqual(body.get('code'), 'lease_revoked', body)
        self.assertEqual([c['replicas'] for c in self.dgd_obj('w')['spec']['components']], [0, 0])

    def test_leased_namespace_fences_cpu_writes_too(self):
        lid = self.add_lease()
        self.assertEqual(self.apply(dgd('mocker', component('Frontend', ctype='frontend', lease_id=lid)))[0], 0)
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

    def test_expire_scales_the_dgd_apply_stamped_with_the_lease(self):
        # Round trip: apply writes the lease label, and expire finds the DGD by that label alone.
        self.add_ns()  # this thread's namespace, so apply may write in it
        self.lid = self.add_lease()
        rc, body, p = self.apply(gpu_dgd('w', 2, self.lid))
        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        self.assertEqual(self.dgd_obj('w')['metadata']['labels'][LEASE_LABEL], self.lid)
        self.bb['leases'][self.lid]['state'] = 'revoking'
        self.flush()
        rc, body, p = self.expire()
        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        self.assertEqual(body['scaled'], ['w'])
        self.assertEqual(self.replicas(), [0, 0])

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
        alpha = {'apiVersion': 'nvidia.com/v1alpha1', 'kind': 'DynamoGraphDeployment',
                 'metadata': {'name': 'a', 'labels': {LEASE_LABEL: self.lid}},
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

    def test_owner_down_deletes_the_dgd_apply_stamped_with_the_lease(self):
        # Round trip: the owner's apply writes the lease label, and down finds the DGD by it alone.
        self.add_ns()
        lid = self.add_lease()
        rc, body, p = self.apply(gpu_dgd('w', 2, lid))
        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        self.assertEqual(self.dgd_obj('w')['metadata']['labels'][LEASE_LABEL], lid)
        rc, body, p = self.guard('--context', CTX, 'down', NS)
        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        raws = [c['flags']['--raw'] for c in self.writes() if 'dynamographdeployments' in c['flags'].get('--raw', '')]
        self.assertEqual(raws, [f'/apis/nvidia.com/v1beta1/namespaces/{NS}/dynamographdeployments/w'])
        self.assertNotIn(NS, self.k8s['namespaces'])

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


class SharedNamespaceTeardown(GuardCase):
    """Teardown of a pre-made namespace selects the lease, never the namespace UID.

    Every task that ever applied in a long-lived namespace shares one namespace UID, so a selector
    on that stamp picks up the DGDs of every earlier task as well.
    """

    def setUp(self):
        super().setUp()
        self.add_ns(thread=OTHER)
        self.lid = self.add_lease(thread=OTHER, state='revoking')
        self.add_obj(gpu_dgd('current', 2, self.lid))
        # An earlier task's GPU DGD in the same namespace: it carries only the shared UID stamp.
        self.add_obj(dgd('neighbour', component('Frontend', ctype='frontend'),
                         component('decode', gpus=2, lease_id='lse_earlier01')))

    def dgd_writes(self):
        """Every request whose target is a DGD: a raw delete, a named delete, or a patch."""
        out = []
        for c in self.writes():
            named = c['pos'][1] if len(c['pos']) > 1 else ''
            if ('dynamographdeployments' in c['flags'].get('--raw', '')
                    or (c['pos'][:1] in (['delete'], ['patch']) and named.startswith('dynamographdeployments.'))):
                out.append(c)
        return out

    def raw_deletes(self):
        return [c['flags']['--raw'] for c in self.dgd_writes() if '--raw' in c['flags']]

    def replicas(self, name):
        return [c['replicas'] for c in self.dgd_obj(name)['spec']['components']]

    def teardown(self, command):
        return self.guard('--context', CTX, command, NS, '--uid', 'uid-ns-1', '--lease', self.lid,
                          host_entry=True)

    def test_down_deletes_only_the_lease_dgd(self):
        rc, body, p = self.teardown('down')
        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        self.assertEqual(self.raw_deletes(),
                         [f'/apis/nvidia.com/v1beta1/namespaces/{NS}/dynamographdeployments/current'])
        # One DGD-directed request in the whole log: no other route (a plain delete, a patch) reaches
        # the neighbour either.
        self.assertEqual(len(self.dgd_writes()), 1)

    def test_down_leaves_another_leases_dgd_alone(self):
        self.add_obj(gpu_dgd('colleague', 2, 'lse_other0001'))
        rc, body, p = self.teardown('down')
        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        self.assertEqual(self.raw_deletes(),
                         [f'/apis/nvidia.com/v1beta1/namespaces/{NS}/dynamographdeployments/current'])
        self.assertEqual(len(self.dgd_writes()), 1)

    def test_expire_scales_only_the_lease_dgd(self):
        rc, body, p = self.teardown('expire')
        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        self.assertEqual(body['scaled'], ['current'])
        self.assertEqual(self.replicas('current'), [0, 0])
        self.assertEqual(self.replicas('neighbour'), [1, 1])

    def test_expire_patch_tests_the_lease_label(self):
        rc, body, p = self.teardown('expire')
        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        self.assertEqual(len(self.dgd_writes()), 1)
        ops = json.loads(self.dgd_writes()[0]['flags']['-p'])
        self.assertIn({'op': 'test', 'path': '/metadata/labels/wcep.agent~1lease', 'value': self.lid}, ops)

    def test_expire_refuses_a_dgd_that_lost_the_lease_label(self):
        # The label can go between the guard's read and its patch; the patch tests it, so the server
        # refuses, rather than scaling a DGD that is no longer this lease's.
        self.k8s['strip_lease_on_patch'] = True
        self.flush()
        rc, body, p = self.teardown('expire')
        self.assertNotEqual(rc, 0, body)
        self.assertEqual(body.get('code'), 'uid_mismatch', body)
        self.assertEqual(self.replicas('current'), [1, 1])

    def test_down_leaves_a_dgd_that_lost_its_lease_label(self):
        # The label can go after the list, as another writer could take it. A delete can test only the
        # UID and resourceVersion, so the guard reads the DGD again and does not send the delete at all.
        self.k8s['strip_lease_after_list'] = True
        self.flush()
        rc, body, p = self.teardown('down')
        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        self.assertEqual(self.dgd_writes(), [])
        self.assertEqual(body['dgds_deleted'], [])
        self.assertEqual(body['dgds_kept'], ['current'])

    def test_down_refuses_a_dgd_that_changes_after_its_read(self):
        # A change between the guard's read and its delete fails the resourceVersion precondition; the
        # guard reads again, sees the label gone, and stops.
        self.k8s['strip_lease_before_delete'] = True
        self.flush()
        rc, body, p = self.teardown('down')
        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        self.assertEqual(self.k8s.get('delete_conflicts'), ['current'])
        self.assertEqual(len(self.dgd_writes()), 1)  # one delete sent, refused by the server
        self.assertEqual(body['dgds_kept'], ['current'])

    def test_teardown_uses_the_given_lease_without_a_record(self):
        # The plugin passes --lease from its own state; the record is only the guard's copy. With the
        # ConfigMap gone the lease id still selects the DGD, so nothing is skipped.
        for command in ('down', 'expire'):
            with self.subTest(command=command):
                self.k8s = {'namespaces': {}, 'objects': {}, 'pods': {}}
                self.bb = {'leases': {}}
                self.kubectl_log.unlink(missing_ok=True)
                self.add_ns(thread=OTHER)
                self.add_lease(thread=OTHER, state='revoking', cm=False)
                self.add_obj(gpu_dgd('current', 2, self.lid))
                rc, body, p = self.teardown(command)
                self.assertEqual(rc, 0, f'{body} {p.stderr}')
                self.assertNotIn('dgds_skipped', body)
                if command == 'down':
                    self.assertEqual(self.raw_deletes(),
                                     [f'/apis/nvidia.com/v1beta1/namespaces/{NS}/dynamographdeployments/current'])
                else:
                    self.assertEqual(body['scaled'], ['current'])

    def lease_less_world(self):
        self.k8s = {'namespaces': {}, 'objects': {}, 'pods': {}}
        self.bb = {'leases': {}, 'unreachable': True}
        self.kubectl_log.unlink(missing_ok=True)
        self.add_ns(thread=OTHER, expires_at=iso_ms(hours_from_now(-1)))
        self.add_obj(dgd('earlier', component('Frontend', ctype='frontend'),
                         component('decode', gpus=2, lease_id='lse_earlier01')))
        self.flush()

    def test_teardown_without_a_lease_selects_no_dgd_and_names_why(self):
        # No lease record means no lease to select by, so no DGD is touched. The namespace delete
        # at the end of `down` still ends everything in it.
        for command in ('down', 'expire'):
            with self.subTest(command=command):
                self.lease_less_world()
                rc, body, p = self.guard('--context', CTX, command, NS, '--uid', 'uid-ns-1', host_entry=True)
                self.assertEqual(rc, 0, f'{body} {p.stderr}')
                self.assertEqual(self.dgd_writes(), [])
                self.assertIn('no lease id', body['dgds_skipped'])
                if command == 'down':
                    self.assertEqual(body['dgds_deleted'], [])
                    self.assertNotIn(NS, self.k8s['namespaces'])  # the namespace takes the DGD with it
                    self.assertEqual(self.ledger(host=True)[-1]['gpus'], 2)  # it ends every DGD there
                else:
                    self.assertEqual(body['scaled'], [])
                    self.assertEqual(self.replicas('earlier'), [1, 1])


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
        anchor = "    del st['objects'][ns][k]\n"
        hook = anchor + "    st['objects'][ns][k] = REPLACEMENT\n" \
                        "    st['pods'][ns].append(FOREIGN_POD)\n"
        foreign_pod = pod('shared', 2, name='foreign-running')
        hook = hook.replace('REPLACEMENT', repr(replacement)).replace('FOREIGN_POD', repr(foreign_pod))
        self.assertEqual(FAKE_KUBECTL.count(anchor), 1)
        self.kubectl_path.write_text(FAKE_KUBECTL.replace(anchor, hook, 1))

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
        anchor = "    save(); emit({'kind': 'Status', 'status': 'Success'})\n    fail('fake kubectl: no handler for raw delete '"
        hook = """    st['pods_after_owned_delete'] = copy.deepcopy(st['pods'][ns])
    st['objects'][ns][k] = st['replacement_dgd']
    st['pods'][ns] = [st['replacement_pod']]
""" + anchor
        self.assertEqual(FAKE_KUBECTL.count(anchor), 1)
        self.kubectl_path.write_text(FAKE_KUBECTL.replace(anchor, hook, 1))

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
        anchor = "    del st['objects'][ns][k]\n"
        hook = anchor + """    st['objects'][ns][k] = st['replacement_dgd']
    st['owned_pods_after_dgd_delete'] = copy.deepcopy(st['pods'][ns])
"""
        self.assertEqual(FAKE_KUBECTL.count(anchor), 1)
        self.kubectl_path.write_text(FAKE_KUBECTL.replace(anchor, hook, 1))

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
        self.kubectl_path.write_text(fake)

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
        self.kubectl_path.write_text(FAKE_KUBECTL.replace(
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
        self.k8s['forbid'] = ['create -f -']  # the Secret create
        self.flush()
        # Make the fake's error text carry the value, as a worst case.
        leaky = FAKE_KUBECTL.replace("fail(f'Error from server (Forbidden): fake forbids \"{needle}\"')",
                                     "fail('Error from server (Forbidden): ' + stdin)")
        self.assertNotEqual(leaky, FAKE_KUBECTL)
        self.kubectl_path.write_text(leaky)
        rc, body, p = self.guard('--context', CTX, '-n', NS, 'secret', 'hf')
        self.assertNotEqual(rc, 0)
        self.assert_no_leak(p)

    def test_secret_crossing_error_truncation_boundary_is_scrubbed(self):
        self.add_ns()
        self.k8s['forbid'] = ['create -f -']  # the Secret create
        self.flush()
        # Truncating before redaction leaves a credential prefix that no full-value match can find.
        leaky = FAKE_KUBECTL.replace(
            "fail(f'Error from server (Forbidden): fake forbids \"{needle}\"')",
            "fail('x' * 580 + stdin)")
        self.assertNotEqual(leaky, FAKE_KUBECTL)
        self.kubectl_path.write_text(leaky)

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


# --- ownership tested on the mutation itself ------------------------------------

class MutationOwnership(GuardCase):
    """Every write carries its ownership checks to the server: create-only POST on absence, JSON Patch with
    tests for UID, resourceVersion and the lease label on presence, and DeleteOptions preconditions with
    both UID and resourceVersion. A client-side read alone proves nothing: the object can change after it."""

    def setUp(self):
        super().setUp()
        self.add_ns()
        self.lid = self.add_lease(gpus=4)

    def interfere(self, key, when, what, times=1, obj=None):
        hook = {'key': key, 'when': when, 'what': what, 'times': times}
        if obj is not None:
            hook['object'] = obj
        self.k8s.setdefault('interfere', []).append(hook)
        self.flush()

    def mutations(self):
        """Writes the server received, dry runs excluded, with their stdin bodies where the guard sent one."""
        return [c for c in self.writes()]

    def test_live_version_changed_between_get_and_patch_is_refused_and_unchanged(self):
        self.add_obj(gpu_dgd('w', 1, self.lid))
        before = copy.deepcopy(self.dgd_obj('w'))
        # Another writer bumps the version before every patch the guard sends.
        self.interfere('DynamoGraphDeployment/w', 'patch', 'bump', times=99)
        rc, body, _ = self.apply(gpu_dgd('w', 2, self.lid))
        self.assertEqual(body.get('code'), 'write_conflict', body)
        after = self.dgd_obj('w')
        self.assertEqual(after['spec'], before['spec'], 'a patch landed over a changed version')
        self.assertEqual(after['metadata']['labels'], before['metadata']['labels'])
        patches = [c for c in self.writes() if c['pos'][0] == 'patch']
        self.assertTrue(patches, body)
        for c in patches:
            ops = json.loads(c['flags']['-p'])
            tested = {op['path'] for op in ops if op['op'] == 'test'}
            self.assertLessEqual({'/metadata/uid', '/metadata/resourceVersion',
                                  '/metadata/labels/wcep.agent~1lease'}, tested, ops)
            self.assertEqual([op['op'] for op in ops][:3], ['test'] * 3, 'the tests come before the edits')

    def test_one_conflict_rereads_and_rechecks_before_the_retry(self):
        self.add_obj(gpu_dgd('w', 1, self.lid))
        self.interfere('DynamoGraphDeployment/w', 'patch', 'bump', times=1)
        rc, body, _ = self.apply(gpu_dgd('w', 2, self.lid))
        self.assertEqual(rc, 0, body)
        self.assertEqual(self.dgd_obj('w')['spec']['components'][1]['podTemplate']['spec']['containers'][0]
                         ['resources']['limits']['nvidia.com/gpu'], '2')
        calls = self.kubectl_calls()
        patches = [i for i, c in enumerate(calls) if c['pos'][0] == 'patch' and c['flags'].get('--dry-run') is None]
        self.assertEqual(len(patches), 2, [c['pos'] for c in calls])
        between = calls[patches[0] + 1:patches[1]]
        self.assertIn(['get', 'dynamographdeployments.nvidia.com/w'], [c['pos'][:2] for c in between])
        self.assertIn('server', [c['flags'].get('--dry-run') for c in between], 'admission ran again')

    def test_absent_object_is_a_create_only_post(self):
        rc, body, _ = self.apply(gpu_dgd('w', 1, self.lid), {'apiVersion': 'v1', 'kind': 'ConfigMap',
                                                            'metadata': {'name': 'cfg'}, 'data': {'a': '1'}})
        self.assertEqual(rc, 0, body)
        verbs = [c['pos'][0] for c in self.writes()]
        self.assertEqual(verbs, ['create', 'create'], [c['argv'] for c in self.writes()])
        self.assertEqual(self.k8s['objects'][NS]['ConfigMap/cfg']['metadata']['labels'][LEASE_LABEL], self.lid)

    def test_competing_create_gets_already_exists_and_is_left_alone(self):
        foreign = dgd('w')  # someone else's object, same name, made between the guard's read and its create
        self.interfere('DynamoGraphDeployment/w', 'create', 'create', obj=foreign)
        self.interfere('DynamoGraphDeployment/w', 'apply', 'create', obj=foreign)
        rc, body, _ = self.apply(gpu_dgd('w', 1, self.lid))
        self.assertNotEqual(rc, 0, body)
        self.assertEqual(body.get('code'), 'object_not_yours', body)
        self.assertEqual(self.dgd_obj('w')['spec'], foreign['spec'], 'the guard merged into a foreign object')
        self.assertNotIn(LEASE_LABEL, self.dgd_obj('w')['metadata'].get('labels') or {})

    def test_present_object_of_another_lease_is_not_yours(self):
        self.add_obj(gpu_dgd('w', 1, 'lse_someone9999'))
        before = copy.deepcopy(self.dgd_obj('w'))
        rc, body, _ = self.apply(gpu_dgd('w', 1, self.lid))
        self.assertEqual(body.get('code'), 'object_not_yours', body)
        self.assertEqual(self.dgd_obj('w'), before)
        self.assertEqual(self.writes(), [])

    def test_relabel_defeats_a_stale_owner_delete(self):
        self.add_obj(gpu_dgd('w', 1, self.lid))
        self.interfere('DynamoGraphDeployment/w', 'delete', 'relabel')
        rc, body, _ = self.guard('--context', CTX, '-n', NS, 'delete', 'dgd/w')
        self.assertNotEqual(rc, 0, body)
        self.assertEqual(body.get('code'), 'object_not_yours', body)
        self.assertIsNotNone(self.dgd_obj('w'), 'a stale delete removed a relabelled object')

    def test_replacement_defeats_a_stale_owner_delete(self):
        self.add_obj(gpu_dgd('w', 1, self.lid))
        self.interfere('DynamoGraphDeployment/w', 'delete', 'replace', obj=dgd('w'))
        rc, body, _ = self.guard('--context', CTX, '-n', NS, 'delete', 'dgd/w')
        self.assertNotEqual(rc, 0, body)
        self.assertIn(body.get('code'), ('object_not_yours', 'not_found'), body)
        self.assertIsNotNone(self.dgd_obj('w'), 'a stale delete removed the replacement')

    def test_owner_delete_carries_uid_and_version_preconditions(self):
        self.add_obj({'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'cfg', 'labels': {
            LEASE_LABEL: self.lid}}, 'data': {}})
        self.ok('--context', CTX, '-n', NS, 'delete', 'configmap/cfg')
        [write] = self.writes()
        self.assertEqual(write['pos'][0], 'delete')
        self.assertEqual(write['flags'].get('--raw'), f'/api/v1/namespaces/{NS}/configmaps/cfg')

    def test_owner_delete_of_an_object_of_another_lease_refused(self):
        self.add_obj({'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'cfg', 'labels': {
            LEASE_LABEL: 'lse_someone9999'}}, 'data': {}})
        self.refused('object_not_yours', '--context', CTX, '-n', NS, 'delete', 'configmap/cfg')
        self.assertEqual(self.writes(), [])

    def test_scale_to_zero_tests_the_version_and_rechecks_on_conflict(self):
        self.bb['leases'][self.lid]['state'] = 'revoking'
        self.flush()
        self.add_obj(gpu_dgd('w', 1, self.lid))
        self.interfere('DynamoGraphDeployment/w', 'patch', 'bump', times=99)
        rc, body, _ = self.guard('--context', CTX, 'expire', NS, '--uid', 'uid-ns-1', '--lease', self.lid,
                                 host_entry=True)
        self.assertNotEqual(rc, 0, body)
        self.assertEqual(self.dgd_obj('w')['spec']['components'][1]['replicas'], 1)
        for c in (c for c in self.writes() if c['pos'][0] == 'patch'):
            self.assertIn('/metadata/resourceVersion', {op['path'] for op in json.loads(c['flags']['-p'])
                                                         if op['op'] == 'test'})

    def test_secret_is_created_once_with_the_lease(self):
        rc, body, p = self.guard('--context', CTX, '-n', NS, 'secret', 'hf')
        self.assertEqual(rc, 0, body)
        stored = self.k8s['objects'][NS]['Secret/hf-token-secret']
        self.assertEqual(stored['metadata']['labels'].get(LEASE_LABEL), self.lid)
        self.assertEqual(stored['metadata']['labels'].get(NS_UID), 'uid-ns-1')
        [write] = self.writes()
        self.assertEqual((write['pos'], write['flags'].get('-f')), (['create'], '-'))

    def test_no_write_uses_kubectl_apply(self):
        self.add_obj(gpu_dgd('old', 1, self.lid))
        rc, body, _ = self.apply(gpu_dgd('old', 2, self.lid), gpu_dgd('new', 1, self.lid))
        self.assertEqual(rc, 0, body)
        self.assertEqual(sorted(c['pos'][0] for c in self.writes()), ['create', 'patch'])


# --- a claimed namespace: the profile's pre-made one --------------------------------

PREMADE = PREFIX + 'agent'


class ClaimedCase(GuardCase):
    """The pre-made namespace: the bot cannot label, create or delete it. A k8s-leases lease is its only
    authority, the thread ledger only points at that lease, and teardown never writes the Namespace."""

    def setUp(self):
        super().setUp()
        self.profiles.write_text(self.profiles.read_text().replace(
            'namespace_prefix = "dyntest-alice-"',
            f'namespace_prefix = "dyntest-alice-"\npremade_namespace = "{PREMADE}"', 1))
        ns = namespace(PREMADE, None, 'uid-premade-1')
        del ns['metadata']['labels']['wcep.agent/thread']
        # Enrolled in the lease fence: ops set this label; a claim refuses without it.
        ns['metadata']['labels']['dynamo.nvidia.com/agent-lease-fence'] = 'true'
        del ns['metadata']['annotations']
        self.k8s['namespaces'][PREMADE] = ns
        self.k8s['objects'][PREMADE] = {}
        self.k8s['pods'][PREMADE] = []
        self.flush()

    def claim(self, gpus='2', **env_kw):
        self.bb['claims'] = 'on'
        self.flush()
        return self.guard('--context', CTX, '-n', PREMADE, 'ns', 'claim', '--gpus', gpus, '--hours', '2', **env_kw)

    def claimed(self, gpus=4, state='active'):
        rc, body, _ = self.claim(str(gpus))
        self.assertEqual(rc, 0, body)
        if state != 'active':
            self.bb['leases'][body['lease_id']]['state'] = state
            self.flush()
        return body['lease_id']

    def namespace_writes(self):
        out = []
        for c in self.writes():
            text = ' '.join(c['argv'])
            if c['pos'][:2] in (['create', '-f'],) and 'Namespace' in text:
                out.append(c)
            if '/api/v1/namespaces/' + PREMADE in (c['flags'].get('--raw') or '') and \
                    c['flags']['--raw'].rstrip('/').endswith('/namespaces/' + PREMADE):
                out.append(c)
            if c['pos'][:2] in (['patch', 'namespace'], ['label', 'namespace'], ['annotate', 'namespace']):
                out.append(c)
        return out

    def land_pending(self):
        self.reload()
        for p in self.k8s.pop('pending', []):
            obj = p['object']
            obj['metadata'].update(uid='uid-landed-' + obj['metadata']['name'], resourceVersion='900')
            self.k8s['objects'][p['ns']][obj['kind'] + '/' + obj['metadata']['name']] = obj
        self.flush()

    def teardown(self, verb, lid, **kw):
        return self.guard('--context', CTX, verb, PREMADE, '--uid', 'uid-premade-1', '--lease', lid,
                          host_entry=True, **kw)

    def earlier_lease(self, state='released'):
        """A lease of the namespace an earlier task made: the one `lease latest` names."""
        self.bb['latest'] = {PREMADE: 'lse_earlier001'}
        self.bb['leases']['lse_earlier001'] = {'lease_id': 'lse_earlier001', 'state': state, 'cluster': PROFILE,
                                               'namespace': PREMADE, 'uid': 'uid-premade-1', 'thread': OTHER,
                                               'gpu_type': 'gb300', 'gpus': 1, 'multi_node': False, 'mode': 'claimed',
                                               'expires_at': iso_ms(hours_from_now(-1 if state == 'released' else 2))}
        self.flush()

    def recreate_namespace(self):
        """Ops re-created the pre-made namespace under its name, so its live UID is a new one."""
        self.k8s['namespaces'][PREMADE]['metadata']['uid'] = 'uid-premade-2'
        self.flush()
        return 'uid-premade-2'

    def claim_lines(self):
        return [l for l in self.ledger() if l['object'] == f'Claim/{PREMADE}']


class NamespaceClaim(ClaimedCase):
    def test_claims_are_off_until_the_admission_webhook_exists(self):
        self.bb['claims'] = 'off'
        self.flush()
        body = self.refused('enforcement_unhealthy', '--context', CTX, '-n', PREMADE, 'ns', 'claim', '--gpus', '2',
                            '--hours', '2')
        self.assertIn('admission', body['error'])
        self.assertEqual(self.writes(), [])

    def test_claim_keeps_its_request_before_the_call_and_writes_no_namespace(self):
        rc, body, _ = self.claim()
        self.assertEqual(rc, 0, body)
        self.assertEqual((body['namespace'], body['claimed']), (PREMADE, True))
        [call] = self.bb['claim_calls']
        self.assertTrue(call['ledger_had_request'], 'the request id must be kept before acquire is called')
        self.assertEqual(call['replaces'], 'none')
        argv = next(c for c in self.bb_calls() if c[:4] == ['k8s', 'lease', 'acquire', '--claim'])
        self.assertNotIn('--thread', argv, 'the owner is the calling thread, not an argument')
        # Its one cluster write is the fence's gate, opened for this lease; never the Namespace.
        [gate] = self.writes()
        self.assertEqual((gate['pos'][:1], gate['flags'].get('-n')), (['create'], PREMADE))
        self.assertEqual(self.k8s['objects'][PREMADE]['ConfigMap/wcep-agent-fence']['data']['lease_id'], body['lease_id'])
        self.assertEqual(self.namespace_writes(), [])
        self.assertEqual(body['pod_label'], {LEASE_LABEL: body['lease_id']})

    def test_a_lost_reply_is_retried_with_the_same_request(self):
        self.bb['lose_claim_reply'] = True
        self.flush()
        rc, body, _ = self.claim()
        self.assertEqual(body.get('code'), 'lease_unavailable', body)
        rc, body, _ = self.claim()
        self.assertEqual(rc, 0, body)
        first, second = self.bb['claim_calls']
        self.assertEqual(first['request'], second['request'])
        self.assertEqual(len(self.bb['leases']), 1)

    def test_an_error_after_the_claim_commits_keeps_the_request(self):
        # The plugin answers with an error, but the claim may have committed: the request stays the one to retry.
        self.bb['claim_error_after_commit'] = True
        self.flush()
        rc, body, _ = self.claim()
        self.assertEqual(body.get('code'), 'internal', body)
        last = [l for l in self.ledger() if l['object'] == f'Claim/{PREMADE}'][-1]
        self.assertEqual(last['direction'], 'claim-request', 'an unknown outcome was recorded as a refusal')
        # Other terms would make a new request the plugin must refuse; the guard says to retry the same one.
        rc, body, _ = self.claim(gpus='3')
        self.assertEqual(body.get('code'), 'claim_pending', body)
        rc, body, _ = self.claim()
        self.assertEqual(rc, 0, body)
        first, second = self.bb['claim_calls']  # claim_pending is the guard's own refusal: no call
        self.assertEqual(first['request'], second['request'])
        self.assertEqual(len(self.bb['leases']), 1)
        self.assertEqual(body['lease_id'], next(iter(self.bb['leases'])))

    def test_a_fresh_claim_names_the_latest_lease_as_its_predecessor(self):
        self.earlier_lease()
        rc, body, _ = self.claim()
        self.assertEqual(rc, 0, body)
        self.assertEqual(self.bb['claim_calls'][0]['replaces'], 'lse_earlier001')

    def test_a_re_created_namespace_still_asks_with_the_pending_request(self):
        # The plugin failed, so the claim may have committed: its request stays pending, naming the namespace
        # UID it was made against. Ops re-created that namespace meanwhile. The UID is not a term the caller
        # can set, so it must never strand the request: asking again with the same id learns the outcome,
        # and the plugin replays its grant.
        self.bb['claim_error_after_commit'] = True
        self.flush()
        rc, body, _ = self.claim()
        self.assertEqual(body.get('code'), 'internal', body)
        [pending] = self.claim_lines()
        self.assertEqual(pending['direction'], 'claim-request')
        [lease_id] = self.bb['leases']
        live_uid = self.recreate_namespace()
        asked = len(self.bb['claim_calls'])
        rc, body, _ = self.claim()
        self.assertEqual(rc, 0, body)
        [replay] = self.bb['claim_calls'][asked:]
        self.assertEqual(replay['request'], pending['request'], 'the pending request is the one asked with')
        self.assertEqual(replay['uid'], pending['uid'],
                         'the replay names the UID the pending request was made against')
        self.assertEqual(body['lease_id'], lease_id)
        # The claim names the UID the request was made against, not the live one: that is what keeps every
        # owned write away from the re-created namespace.
        claims = self.claim_lines()
        self.assertEqual([(l['direction'], l['lease']) for l in claims], [('claim-request', None), ('claim', lease_id)])
        self.assertEqual((claims[-1]['uid'], claims[-1]['request']), (pending['uid'], pending['request']))
        self.assertNotEqual(claims[-1]['uid'], live_uid, 'the claim names the UID its request was made against')
        self.assertNotIn('claim_pending', [l.get('code') for l in claims])
        rc, body, _ = self.apply(gpu_dgd('w', 1, lease_id), ns=PREMADE)
        self.assertEqual(body.get('code'), 'uid_mismatch', body)
        self.assertEqual(self.writes(), [], 'the re-created namespace is left alone for on-call')

    def test_a_refused_replay_starts_one_fresh_request(self):
        # The plugin refused the pending request before it committed, and its error hid that. The replay
        # names the UID the request was made against, so the plugin's host read refuses it `uid_mismatch`:
        # record that refusal, then make one new request, which names the live UID and `latest` as its
        # predecessor.
        self.earlier_lease()
        self.bb['claim_error_before_commit'] = True
        self.bb['next_id'] = 'lse_fresh0001'
        self.flush()
        rc, body, _ = self.claim()
        self.assertEqual(body.get('code'), 'internal', body)
        [pending] = self.claim_lines()
        live_uid = self.recreate_namespace()
        asked = len(self.bb['claim_calls'])
        rc, body, _ = self.claim()
        self.assertEqual(rc, 0, body)
        replay, fresh = self.bb['claim_calls'][asked:]
        self.assertEqual(replay['request'], pending['request'])
        self.assertEqual(replay['uid'], pending['uid'],
                         'the replay names the UID the pending request was made against')
        self.assertNotEqual(fresh['request'], pending['request'])
        self.assertEqual(fresh['uid'], live_uid, 'the fresh request names the live UID')
        self.assertEqual(fresh['replaces'], 'lse_earlier001')
        self.assertEqual(body['lease_id'], 'lse_fresh0001')
        self.assertEqual([(l['direction'], l.get('request'), l.get('code')) for l in self.claim_lines()],
                         [('claim-request', pending['request'], None),
                          ('claim-refused', pending['request'], 'uid_mismatch'),
                          ('claim-request', fresh['request'], None),
                          ('claim', fresh['request'], None)])
        self.assertEqual(self.claim_lines()[-1]['uid'], live_uid, 'the fresh request names the live UID')

    def test_only_the_profiles_premade_namespace_with_both_labels(self):
        self.bb['claims'] = 'on'
        other = PREFIX + 'other'
        self.k8s['namespaces'][other] = copy.deepcopy(self.k8s['namespaces'][PREMADE])
        self.k8s['namespaces'][other]['metadata']['name'] = other
        self.flush()
        self.refused('namespace_not_premade', '--context', CTX, '-n', other, 'ns', 'claim', '--gpus', '1', '--hours', '1')
        del self.k8s['namespaces'][PREMADE]['metadata']['labels']['dynamo.nvidia.com/developer-agent-rbac']
        self.flush()
        self.refused('namespace_not_ours', '--context', CTX, '-n', PREMADE, 'ns', 'claim', '--gpus', '1', '--hours', '1')
        self.assertEqual(self.bb.get('claim_calls', []), [])

    def test_a_profile_without_the_key_has_nothing_to_claim(self):
        self.profiles.write_text(self.profiles.read_text().replace(f'premade_namespace = "{PREMADE}"\n', ''))
        self.refused('premade_namespace_unset', '--context', CTX, '-n', PREMADE, 'ns', 'claim', '--gpus', '1',
                     '--hours', '1')


class ClaimedWrites(ClaimedCase):
    def test_no_lease_no_writes_cpu_only_included(self):
        rc, body, _ = self.apply(dgd('cpu'), ns=PREMADE)
        self.assertEqual(body.get('code'), 'namespace_not_claimed', body)
        self.assertEqual(self.writes(), [])

    def test_ownership_comes_from_the_plugin_lease_not_the_ledger(self):
        lid = self.claimed()
        self.bb['leases'][lid]['thread'] = OTHER  # the plugin's row says another thread owns it
        self.flush()
        before = len(self.writes())  # the claim's own gate write
        rc, body, _ = self.apply(gpu_dgd('w', 1, lid), ns=PREMADE)
        self.assertEqual(body.get('code'), 'lease_mismatch', body)
        self.assertEqual(len(self.writes()), before)

    def test_every_write_is_an_attempt_recorded_before_it_is_sent_and_stamped(self):
        lid = self.claimed()
        rc, body, _ = self.apply(gpu_dgd('w', 1, lid), {'apiVersion': 'v1', 'kind': 'ConfigMap',
                                                       'metadata': {'name': 'cfg'}, 'data': {}}, ns=PREMADE)
        self.assertEqual(rc, 0, body)
        attempts = self.bb['leases'][lid]['attempts']
        self.assertEqual([(a['verb'], a['kind'], a['name'], a['outcome']) for a in attempts],
                         [('create', 'DynamoGraphDeployment', 'w', 'committed'), ('create', 'ConfigMap', 'cfg', 'committed')])
        # The claim wrote the gate first (one write); each attempt is recorded before its own write.
        self.assertEqual([a['writes_before'] for a in attempts], [1, 2], 'an attempt must be recorded before its write')
        for a in attempts:
            obj = self.k8s['objects'][PREMADE][f'{a["kind"]}/{a["name"]}']
            self.assertEqual(obj['metadata']['annotations']['wcep.agent/attempt'], a['attempt'])
        self.assertEqual(self.namespace_writes(), [])

    def test_a_timed_out_create_then_404_stays_outstanding_and_blocks_the_next_write(self):
        lid = self.claimed()
        self.k8s['stall_create'] = ['DynamoGraphDeployment/w']
        self.flush()
        rc, body, _ = self.apply(gpu_dgd('w', 1, lid), ns=PREMADE)
        self.assertEqual(body.get('code'), 'write_unresolved', body)
        [a] = self.bb['leases'][lid]['attempts']
        self.assertIsNone(a['outcome'], 'a timed-out create followed by 404 was cleared')
        rc, body, _ = self.apply(dgd('other', component('Frontend', ctype='frontend', lease_id=lid)), ns=PREMADE)
        self.assertEqual(body.get('code'), 'attempt_unresolved', body)
        self.assertNotIn(['k8s', 'lease', 'op', 'begin', lid], self.bb_calls()[-3:])
        # The create lands; the next write first observes its stamp, resolves it, then goes on.
        self.land_pending()
        rc, body, _ = self.apply(dgd('other', component('Frontend', ctype='frontend', lease_id=lid)), ns=PREMADE)
        self.assertEqual(rc, 0, body)
        self.assertEqual([x['outcome'] for x in self.bb['leases'][lid]['attempts']], ['committed', 'committed'])

    def test_secret_delete_and_scale_writes_are_attempts_too(self):
        lid = self.claimed()
        self.ok('--context', CTX, '-n', PREMADE, 'secret', 'hf')
        self.add_obj(gpu_dgd('w', 1, lid), ns=PREMADE)
        self.ok('--context', CTX, '-n', PREMADE, 'delete', 'dgd/w')
        verbs = [(a['verb'], a['kind'], a['outcome']) for a in self.bb['leases'][lid]['attempts']]
        self.assertEqual(verbs, [('create', 'Secret', 'committed'), ('delete', 'DynamoGraphDeployment', 'committed')])


class ClaimedTeardown(ClaimedCase):
    def leased(self, lid):
        self.add_obj(gpu_dgd('w', 2, lid), ns=PREMADE)
        self.k8s['objects'][PREMADE]['Secret/hf-token-secret'] = {
            'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'hf-token-secret', 'namespace': PREMADE,
                                                              'uid': 'uid-secret', 'resourceVersion': '31',
                                                              'labels': {LEASE_LABEL: lid}}}
        self.k8s['objects'][PREMADE]['ConfigMap/cfg'] = {
            'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'cfg', 'namespace': PREMADE, 'uid': 'uid-cfg',
                                                                 'resourceVersion': '32', 'labels': {LEASE_LABEL: lid}}}
        self.flush()

    def leased_pod(self, lid, terminating=False, name='w-decode'):
        p = pod('w', 2, terminating=terminating, name=name)
        p['metadata']['labels'][LEASE_LABEL] = lid
        self.add_pod(p, ns=PREMADE)

    def test_teardown_deletes_the_lease_inventory_and_never_writes_the_namespace(self):
        lid = self.claimed(state='releasing')
        self.leased(lid)
        body = self.ok('--context', CTX, 'down', PREMADE, '--uid', 'uid-premade-1', '--lease', lid, host_entry=True)
        self.assertEqual((body['complete'], body['namespace_kept']), (True, True), body)
        # Everything of the lease is gone; the fence's gate stays, closed for it, and is never deleted.
        self.assertEqual(list(self.k8s['objects'][PREMADE]), ['ConfigMap/wcep-agent-fence'])
        self.assertEqual(self.k8s['objects'][PREMADE]['ConfigMap/wcep-agent-fence']['data']['state'], 'closing')
        self.assertIn(PREMADE, self.k8s['namespaces'])
        self.assertEqual(self.namespace_writes(), [])
        for c in self.writes():
            if c['pos'][0] == 'delete':
                self.assertIn('--raw', c['flags'])

    def test_a_terminating_leased_pod_holds_the_charge_and_a_foreign_pod_survives(self):
        lid = self.claimed(state='releasing')
        self.leased(lid)
        self.leased_pod(lid, terminating=True)
        self.k8s['terminate_pods_on_delete'] = False  # the DGD delete leaves its pods terminating
        foreign = pod('w', 2, name='foreign-same-dgd-name')  # same DGD name label, no lease label
        self.add_pod(foreign, ns=PREMADE)
        rc, body, _ = self.teardown('down', lid)
        self.assertEqual(body.get('code'), 'teardown_incomplete', body)
        self.assertIn('w-decode', json.dumps(body.get('left')))
        self.assertIn('foreign-same-dgd-name', [p['metadata']['name'] for p in self.k8s['pods'][PREMADE]])
        self.k8s['pods'][PREMADE] = [foreign]
        self.flush()
        body = self.ok('--context', CTX, 'down', PREMADE, '--uid', 'uid-premade-1', '--lease', lid, host_entry=True)
        self.assertTrue(body['complete'], body)
        self.assertEqual([p['metadata']['name'] for p in self.k8s['pods'][PREMADE]], ['foreign-same-dgd-name'])

    def test_an_incomplete_inventory_list_keeps_the_charge(self):
        lid = self.claimed(state='releasing')
        self.k8s['forbid'] = ['get secrets']
        self.flush()
        rc, body, _ = self.teardown('down', lid)
        self.assertEqual(body.get('code'), 'teardown_incomplete', body)

    def test_relabel_defeats_a_stale_teardown_delete_and_keeps_the_charge(self):
        lid = self.claimed(state='releasing')
        self.add_obj(gpu_dgd('w', 1, lid), ns=PREMADE)
        self.interfere_relabel = {'key': 'DynamoGraphDeployment/w', 'when': 'delete', 'what': 'relabel', 'times': 1}
        self.k8s.setdefault('interfere', []).append(self.interfere_relabel)
        self.flush()
        rc, body, _ = self.teardown('down', lid)
        self.assertEqual(body.get('code'), 'teardown_incomplete', body)
        self.assertIsNotNone(self.dgd_obj('w', ns=PREMADE), 'a stale delete removed an object that changed hands')
        self.assertEqual(self.namespace_writes(), [])

    def test_unresolved_write_crossing_teardown_then_commit_observe_drain(self):
        # The phase-1 crossing case, guard side, with separate client and storage lifetimes: the guard is
        # killed while its create is in flight; teardown against an empty list completes nothing; the
        # create then lands, and the next teardown observes its stamp, deletes it and completes.
        import signal
        lid = self.claimed()
        hold, held = self.tmp / 'release-write', self.tmp / 'write-held'
        self.k8s.update(stall_create=['DynamoGraphDeployment/w'], hold_file=str(hold), held_marker=str(held))
        self.flush()
        proc = subprocess.Popen(['python3', '-I', str(self.runner), str(GUARD), '--context', CTX, '-n', PREMADE, 'apply',
                                 '-f', self.manifest(gpu_dgd('w', 2, lid))], env=self.env(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            for _ in range(500):
                if held.exists():
                    break
                time.sleep(0.02)
            self.assertTrue(held.exists(), 'the write never reached the server')
            os.killpg(proc.pid, signal.SIGKILL)
        finally:
            proc.wait(timeout=10)
            proc.stdout.close()
            proc.stderr.close()
        self.reload()
        [a] = self.bb['leases'][lid]['attempts']
        self.assertIsNone(a['outcome'])
        # The slot expires with its holder gone; the lease is returned.
        self.bb['leases'][lid].update(slot=None, state='releasing')
        self.k8s.pop('hold_file')
        self.flush()
        rc, body, _ = self.teardown('down', lid)
        self.assertEqual(body.get('code'), 'teardown_incomplete', body)
        self.assertNotIn('complete', body)
        self.assertIsNone(self.bb['leases'][lid]['attempts'][0]['outcome'])
        self.land_pending()
        body = self.ok('--context', CTX, 'down', PREMADE, '--uid', 'uid-premade-1', '--lease', lid, host_entry=True)
        self.assertTrue(body['complete'], body)
        self.assertEqual(body['resolutions'], [{'attempt': a['attempt'], 'outcome': 'committed'}])
        self.assertIsNone(self.dgd_obj('w', ns=PREMADE))
        self.assertEqual(self.namespace_writes(), [])

    # A hook on the fake's patch: right after the scale-to-zero lands, the DGD changes hands.
    PATCH_LANDS = "    st['objects'][ns_flag][k] = o; save(); emit(o)"

    def after_scale(self, change):
        hook = """    st['objects'][ns_flag][k] = o
    if st.get('after_scale') and k == 'DynamoGraphDeployment/w':
        CHANGE
        st.pop('after_scale')
    save(); emit(o)""".replace('CHANGE', change)
        self.assertEqual(FAKE_KUBECTL.count(self.PATCH_LANDS), 1)
        self.kubectl_path.write_text(FAKE_KUBECTL.replace(self.PATCH_LANDS, hook, 1))
        self.k8s['after_scale'] = True
        self.flush()

    def test_a_replacement_after_the_scale_patch_is_left_and_keeps_the_charge(self):
        lid = self.claimed(state='releasing')
        self.add_obj(gpu_dgd('w', 2, lid), ns=PREMADE)
        foreign = dgd('w')  # someone else's DGD, no lease label, same name
        self.after_scale("new = FOREIGN\n        new['metadata'].update(namespace=ns_flag, uid='uid-foreign-w', "
                         "resourceVersion=rv())\n        st['objects'][ns_flag][k] = new".replace('FOREIGN', repr(foreign)))
        rc, body, _ = self.teardown('down', lid)
        self.assertEqual(body.get('code'), 'teardown_incomplete', body)
        self.assertEqual(self.dgd_obj('w', ns=PREMADE)['metadata']['uid'], 'uid-foreign-w',
                         'teardown deleted the replacement')
        self.assertIn('DynamoGraphDeployment/w', body['changed_hands'])

    def test_a_relabel_after_the_scale_patch_is_left_and_keeps_the_charge(self):
        lid = self.claimed(state='releasing')
        self.add_obj(gpu_dgd('w', 2, lid), ns=PREMADE)
        self.after_scale("o['metadata']['labels']['wcep.agent/lease'] = 'lse_someone9999'\n"
                         "        o['metadata']['resourceVersion'] = rv()")
        rc, body, _ = self.teardown('down', lid)
        self.assertEqual(body.get('code'), 'teardown_incomplete', body)
        left = self.dgd_obj('w', ns=PREMADE)
        self.assertIsNotNone(left, 'teardown deleted a DGD relabelled to another lease')
        self.assertEqual(left['metadata']['labels'][LEASE_LABEL], 'lse_someone9999')

    def stalled_create_then_foreign_holder(self):
        lid = self.claimed()
        self.k8s['stall_create'] = ['DynamoGraphDeployment/w']
        self.flush()
        rc, body, _ = self.apply(gpu_dgd('w', 2, lid), ns=PREMADE)
        self.assertEqual(body.get('code'), 'write_unresolved', body)
        foreign = dgd('w')
        foreign['metadata'].update(namespace=PREMADE, uid='uid-foreign-w', resourceVersion='77')
        self.k8s['objects'][PREMADE]['DynamoGraphDeployment/w'] = foreign
        self.flush()
        return lid

    def test_a_create_whose_name_another_object_holds_stays_open_through_teardown(self):
        # Another holder proves nothing more than a 404: once it goes, the create in flight can still land.
        lid = self.stalled_create_then_foreign_holder()
        self.bb['leases'][lid].update(slot=None, state='releasing')
        self.flush()
        rc, body, _ = self.teardown('down', lid)
        self.assertEqual(body.get('code'), 'teardown_incomplete', body)
        self.assertEqual(body.get('resolutions'), [])
        self.assertIsNone(self.bb['leases'][lid]['attempts'][0]['outcome'])
        self.assertIsNotNone(self.dgd_obj('w', ns=PREMADE), 'the foreign holder is not the lease\'s to delete')
        # The holder goes; the create lands; the next teardown sees its stamp, deletes it and completes.
        del self.k8s['objects'][PREMADE]['DynamoGraphDeployment/w']
        self.flush()
        self.land_pending()
        body = self.ok('--context', CTX, 'down', PREMADE, '--uid', 'uid-premade-1', '--lease', lid, host_entry=True)
        self.assertTrue(body['complete'], body)
        self.assertEqual([r['outcome'] for r in body['resolutions']], ['committed'])
        self.assertIsNone(self.dgd_obj('w', ns=PREMADE))

    def test_a_create_whose_name_another_object_holds_still_blocks_the_next_write(self):
        lid = self.stalled_create_then_foreign_holder()
        rc, body, _ = self.apply(dgd('other', component('Frontend', ctype='frontend', lease_id=lid)), ns=PREMADE)
        self.assertEqual(body.get('code'), 'attempt_unresolved', body)
        self.assertIsNone(self.bb['leases'][lid]['attempts'][0]['outcome'])

    def test_a_restarted_teardown_after_the_last_deletion_finishes_without_a_namespace_write(self):
        lid = self.claimed(state='revoking')
        self.leased(lid)
        self.ok('--context', CTX, 'down', PREMADE, '--uid', 'uid-premade-1', '--lease', lid, host_entry=True)
        body = self.ok('--context', CTX, 'down', PREMADE, '--uid', 'uid-premade-1', '--lease', lid, host_entry=True)
        self.assertTrue(body['complete'], body)
        self.assertEqual(self.namespace_writes(), [])

    def test_no_offline_teardown_authority_for_a_claimed_namespace(self):
        lid = self.claimed(state='revoking')
        self.add_obj(gpu_dgd('w', 1, lid), ns=PREMADE)
        self.bb['unreachable'] = True
        self.flush()
        before = len(self.writes())  # the claim's own gate write
        for verb in ('expire', 'down'):
            with self.subTest(verb=verb):
                self.refused('lease_unavailable', '--context', CTX, verb, PREMADE, '--uid', 'uid-premade-1', '--lease',
                             lid, host_entry=True)
        self.assertEqual(len(self.writes()), before)

    def test_expire_scales_the_lease_dgds_with_the_plugins_word(self):
        lid = self.claimed(state='revoking')
        self.add_obj(gpu_dgd('w', 1, lid), ns=PREMADE)
        self.add_obj(gpu_dgd('theirs', 1, 'lse_someone9999'), ns=PREMADE)
        body = self.ok('--context', CTX, 'expire', PREMADE, '--uid', 'uid-premade-1', '--lease', lid, host_entry=True)
        self.assertEqual(body['scaled'], ['w'])
        self.assertEqual(self.dgd_obj('theirs', ns=PREMADE)['spec']['components'][1]['replicas'], 1)

    def test_owner_down_returns_the_lease_and_leaves_the_namespace(self):
        lid = self.claimed()
        body = self.ok('--context', CTX, '-n', PREMADE, 'down', PREMADE)
        self.assertEqual(self.bb['leases'][lid]['state'], 'releasing')
        self.assertIn(['k8s', 'lease', 'release', lid], self.bb_calls())
        self.assertTrue(body['namespace_kept'], body)
        self.assertIn(PREMADE, self.k8s['namespaces'])
        self.assertEqual(self.namespace_writes(), [])

    def test_selftest_in_a_claimed_namespace_sends_no_namespace_request(self):
        lid = self.claimed()
        self.add_obj(gpu_dgd('smoke', 1, lid), ns=PREMADE)
        body = self.ok('--context', CTX, '-n', PREMADE, 'selftest', 'fences')
        self.assertTrue(body['all_held'], body)
        self.assertNotIn('delete namespace: wrong UID precondition', [p['fence'] for p in body['probes']])
        self.assertFalse(any('/namespaces/' + PREMADE == (c['flags'].get('--raw') or '')[-len('/namespaces/' + PREMADE):]
                             for c in self.kubectl_calls()), 'a Namespace request was sent')
        # The fence's gate gets its own stale patch: refused for its version, and the gate unchanged.
        [gate] = [p for p in body['probes'] if p['fence'] == 'patch gate: stale resourceVersion']
        self.assertTrue(gate['held'], gate)
        self.assertEqual(body['unchanged']['gate'], True)
        self.assertEqual(self.k8s['objects'][PREMADE]['ConfigMap/wcep-agent-fence']['data']['state'], 'active')


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
        self.kubectl_path.write_text(fake)
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
        self.kubectl_path.write_text(fake)

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
                # A late apply stamps the old namespace's lease, so the label alone cannot tell the
                # replacement apart; only its UID can.
                replacement = gpu_dgd('w', 2, lid)
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
                self.kubectl_path.write_text(fake)

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
        hook = """    if not dry and obj['metadata']['name'] in ('first', 'second', 'late'):
        st['elapsed_seconds'] += 14
        save()
        if obj['metadata']['name'] == 'late':
            other = subprocess.run(CONCURRENT_ARGV, env=dict(os.environ, KUBECONFIG=BOT_KUBECONFIG),
                                   capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
            st = json.load(open(STATE))
            st['concurrent_rc'] = other.returncode
            st['concurrent_result'] = json.loads(other.stdout)
            save()
""".replace('CONCURRENT_ARGV', repr(concurrent_argv)).replace('BOT_KUBECONFIG', repr(str(self.bot_kubeconfig)))
        anchor = "    if not dry:\n        interfere(key(obj), 'create')\n"
        self.assertEqual(fake.count(anchor), 1)
        fake = fake.replace(anchor, hook + anchor, 1)
        self.assertNotEqual(fake, FAKE_KUBECTL)
        self.kubectl_path.write_text(fake)
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
        # An earlier task's DGD, stamped with the shared UID only, listed first: a selector on the UID
        # would probe it instead of the lease's DGD.
        self.add_obj(dgd('earlier', component('Frontend', ctype='frontend'),
                         component('decode', gpus=1, lease_id='lse_earlier01')))
        self.add_obj(gpu_dgd('smoke', 1, self.lid))

    def test_every_fence_holds_and_nothing_changes(self):
        before = copy.deepcopy(self.k8s)
        body = self.ok('--context', CTX, '-n', NS, 'selftest', 'fences')
        self.assertTrue(body['all_held'], body)
        self.assertEqual(body['dgd'], 'smoke')  # the lease's DGD, not the UID-only one listed first
        self.assertEqual([p['fence'] for p in body['probes']], [
            'patch: wrong DGD UID', 'patch: stale resourceVersion', 'patch: wrong lease label',
            'delete DGD: wrong UID precondition', 'delete DGD: stale resourceVersion precondition',
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
                self.kubectl_path.write_text(fake)

                rc, body, _ = self.guard('--context', CTX, '-n', NS, 'selftest', 'fences')

                self.assertTrue(any(error in p['response'] for p in body.get('probes', [])), body)
                self.assertNotEqual(rc, 0, f'unrelated errors certified as working fences: {body}')

    def test_a_server_that_ignores_a_fence_is_reported(self):
        # A fake server that ignores the UID precondition on a DGD delete.
        anchor = "    if o['metadata']['uid'] != want:\n        fail("
        self.assertEqual(FAKE_KUBECTL.count(anchor), 1)
        fake = FAKE_KUBECTL.replace(anchor, "    if False:\n        fail(", 1)
        self.assertNotEqual(fake, FAKE_KUBECTL)
        self.kubectl_path.write_text(fake)
        body = self.refused('fence_failed', '--context', CTX, '-n', NS, 'selftest', 'fences')
        self.assertFalse(body['all_held'])
        self.assertEqual([p['refused'] for p in body['probes']], [True, True, True, False, True, True, True])
        self.assertFalse(body['probes'][3]['held'])

    def test_another_threads_namespace_refused(self):
        self.refused('not_your_namespace', '--context', CTX, '-n', NS, 'selftest', 'fences', thread=OTHER)
        self.assertEqual(self.writes(), [])

    def test_needs_a_stamped_dgd(self):
        self.k8s['objects'][NS].pop('DynamoGraphDeployment/smoke')
        self.flush()
        self.refused('selftest_needs_dgd', '--context', CTX, '-n', NS, 'selftest', 'fences')
        self.assertEqual(self.writes(), [])


# --- build Jobs -------------------------------------------------------------

COMMIT = 'a1' * 20
TAG = COMMIT[:12]
REGISTRY = 'nvcr.io/goirlvsxnepa/llm_nim'
BUILDKIT = ('mirror.gcr.io/moby/buildkit:v0.23.2-rootless'
            '@sha256:cab936745de5d673465948f1e93ff4d6e372bbe33f218afd3314eba45a6f85a9')
CRANE = ('gcr.io/go-containerregistry/crane/debug:v0.20.3'
         '@sha256:94f61956845714bea3b788445454ae4827f49a90dcd9dac28255c4cccb6220ad')
GITHUB_KEY = 'github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl'
PULL_REF = 'op://Fleet/NGC Pull Key/credential'
PUSH_REF = 'op://Fleet/NGC NIM Operations PAT/credential'


def build_argv(arch=None, **over):
    a = {'repo': 'git@github.com:owner/repo.git', 'commit': COMMIT,
         'dockerfile': 'crates/gdl-operator/Dockerfile', 'image': 'gdl-operator'}
    a.update(over)
    out = ['--repo', a['repo'], '--commit', a['commit'], '--dockerfile', a['dockerfile'], '--image', a['image']]
    if arch:
        out += ['--arch', arch]
    return out


def pod_of(job):
    return job['spec']['template']['spec']


def named(items, name):
    return next(c for c in items if c['name'] == name)


class BuildCase(GuardCase):
    """A namespace the guard created, with one lease: enough for every build write except a claim."""

    ctx, cluster, gpu_type, arch = CTX, PROFILE, 'gb300', 'arm64'

    def setUp(self):
        super().setUp()
        self.add_ns(thread=ME, cluster=self.cluster)
        self.lid = self.add_lease(cluster=self.cluster, gpu_type=self.gpu_type, gpus=1)
        rc, body, p = self.guard('--context', self.ctx, '-n', NS, 'secret', 'ngc-push', '--exposure-accepted')
        self.assertEqual(rc, 0, f'{body} {p.stderr}')

    def run_argv(self, **over):
        over.setdefault('arch', self.arch)
        return build_argv(**over)

    def build_guard(self, *rest, **env_kw):
        return self.guard('--context', self.ctx, '-n', NS, 'build', *rest, **env_kw)

    def ensure_git(self):
        if 'Secret/git' not in self.k8s['objects'].get(NS, {}):
            rc, body, p = self.guard('--context', self.ctx, '-n', NS, 'secret', 'git', '--repo', 'owner/repo')
            self.assertEqual(rc, 0, f'{body} {p.stderr}')

    def built(self, **over):
        repo = over.get('repo', 'git@github.com:owner/repo.git')
        if repo.startswith('git@'):
            self.ensure_git()
        rc, body, p = self.build_guard('run', *self.run_argv(**over))
        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        return body

    def job(self, name, ns=NS):
        return self.k8s['objects'][ns].get('Job/' + name)

    def rendered(self, **over):
        """A Job the guard really made, lifted out of the store so a test can mutate one piece of it."""
        self.k8s.pop('mutate', None)
        self.flush()
        body = self.built(**over)
        job = copy.deepcopy(self.job(body['job']))
        del self.k8s['objects'][NS]['Job/' + body['job']]
        self.flush()
        return job

    def refused_render(self, extra, code, **over):
        if 'repo' not in over:
            self.ensure_git()
        self.k8s['mutate'] = {'Job': extra}
        self.flush()
        before = len(self.writes())
        body = self.refused(code, '--context', self.ctx, '-n', NS, 'build', 'run', *self.run_argv(**over))
        self.assertEqual(len(self.writes()), before, body)
        return body

    def jobs(self):
        return [o for o in self.k8s['objects'].get(NS, {}).values() if o['kind'] == 'Job']


class BuildAmd64(BuildCase):
    ctx, cluster, gpu_type, arch = CTX_B200, 'lab-b200', 'b200', 'amd64'


class BuildRun(BuildCase):
    def test_build_submission_records_one_ledger_line_and_names_the_job(self):
        body = self.built()

        self.assertEqual(body, {'job': f'build-{TAG}-{self.arch}'})
        [line] = [l for l in self.ledger() if l['object'].startswith('Job/')]
        self.assertEqual((line['object'], line['gpus'], line['thread'], line['cluster'], line['namespace'],
                          line['direction']),
                         (f'Job/build-{TAG}-{self.arch}', 1, ME, self.cluster, NS, 'up'))

    def test_the_job_is_the_pinned_template(self):
        job = self.job(self.built()['job'])

        self.assertEqual((job['apiVersion'], job['kind']), ('batch/v1', 'Job'))
        spec = job['spec']
        self.assertEqual((spec['backoffLimit'], spec['activeDeadlineSeconds'], spec['ttlSecondsAfterFinished']),
                         (0, 3600, 86400))
        pod = pod_of(job)
        self.assertEqual(pod['restartPolicy'], 'Never')
        self.assertEqual(pod['nodeSelector'], {'kubernetes.io/arch': self.arch})
        self.assertEqual(pod['securityContext'], {
            'runAsUser': 1000, 'runAsGroup': 1000, 'fsGroup': 1000,
            'seccompProfile': {'type': 'Unconfined'}, 'appArmorProfile': {'type': 'Unconfined'}})
        self.assertEqual(job['metadata']['labels'][LEASE_LABEL], self.lid)
        self.assertEqual(job['metadata']['labels'][NS_UID], 'uid-ns-1')
        self.assertEqual(job['spec']['template']['metadata']['labels'], {LEASE_LABEL: self.lid})
        self.assertNotIn('imagePullSecrets', pod)
        for c in pod['containers'] + pod['initContainers']:
            self.assertNotIn('allowPrivilegeEscalation', c.get('securityContext') or {}, c['name'])
            self.assertEqual(set(c['resources']), {'requests', 'limits'}, c['name'])
            for section in ('requests', 'limits'):
                self.assertLessEqual({'cpu', 'memory', 'ephemeral-storage'}, set(c['resources'][section]), c['name'])
            self.assertRegex(c['image'], r'@sha256:[0-9a-f]{64}$')
        # The push credential is a Secret's dockerconfigjson mounted as the file BuildKit reads.
        cfg = named(pod['volumes'], 'docker-config')
        self.assertEqual(cfg['secret']['secretName'], 'nvcr-push')
        self.assertEqual(cfg['secret']['items'], [{'key': '.dockerconfigjson', 'path': 'config.json'}])
        build = named(pod['containers'], 'build')
        self.assertEqual(build['image'], BUILDKIT)
        self.assertEqual(build['command'],
                         ['buildctl-daemonless.sh', 'build', '--oci-worker-no-process-sandbox',
                          '--frontend', 'dockerfile.v0',
                          '--local', 'context=/src',
                          '--local', 'dockerfile=/src/crates/gdl-operator',
                          '--output', f'type=image,name={REGISTRY}/gdl-operator:{TAG}-{self.arch},push=true'])
        self.assertEqual({e['name']: e['value'] for e in build['env']}['DOCKER_CONFIG'], '/home/user/.docker')
        cfg_mount = named(build['volumeMounts'], 'docker-config')
        self.assertEqual((cfg_mount['mountPath'], cfg_mount['readOnly']), ('/home/user/.docker', True))
        # The tag check runs first, in the same pinned image, and carries the plan's three answers.
        check = named(pod['initContainers'], 'tag-check')
        self.assertEqual(check['image'], CRANE)
        self.assertEqual(check['command'][0], '/busybox/sh')
        script = check['command'][-1]
        for marker in ('tag_exists', 'registry_denied', 'MANIFEST_UNKNOWN', 'exit 0'):
            self.assertIn(marker, script)
        check_env = {e['name']: e['value'] for e in check['env']}
        self.assertEqual(check_env['REF'], f'{REGISTRY}/gdl-operator:{TAG}-{self.arch}')
        self.assertEqual(check_env['CRANE'], '/ko-app/crane')
        # The clone passes the whole commit and verifies the checkout is exactly it.
        clone = named(pod['initContainers'], 'clone')
        self.assertEqual(clone['image'], BUILDKIT)
        self.assertEqual({e['name']: e['value'] for e in clone['env']}['COMMIT'], COMMIT)
        self.assertIn('git rev-parse HEAD', clone['command'][-1])
        self.assertIn('git clone --filter=blob:none', clone['command'][-1])

    def test_the_evidence_init_containers_run_in_this_order(self):
        job = self.job(self.built()['job'])
        self.assertEqual([c['name'] for c in pod_of(job)['initContainers']], ['tag-check', 'clone'])

    def test_build_without_a_lease_is_refused(self):
        self.k8s['objects'][NS].pop('ConfigMap/' + LEASE_CM)
        self.flush()
        before = len(self.writes())

        self.refused('lease_required', '--context', self.ctx, '-n', NS, 'build', 'run', *self.run_argv())
        self.assertEqual(len(self.writes()), before)

    def test_commit_must_be_40_hex(self):
        before = len(self.writes())
        for commit in ('a1' * 19, 'a1' * 21, 'A1' * 20, 'g' * 40, ''):
            with self.subTest(commit=commit):
                self.refused('invalid_commit', '--context', self.ctx, '-n', NS, 'build', 'run',
                             *self.run_argv(commit=commit))
        self.assertEqual(len(self.writes()), before)

    def test_bad_values_refused_before_any_call(self):
        before = len(self.writes())
        cases = [('image', 'Bad Name', 'invalid_image'), ('image', '', 'invalid_image'),
                 ('dockerfile', '/etc/passwd', 'usage'), ('dockerfile', '../Dockerfile', 'usage'),
                 ('repo', 'https://example.test/a;rm -rf /', 'usage'), ('repo', 'not a url', 'usage')]
        for key, value, code in cases:
            with self.subTest(field=key, value=value):
                self.refused(code, '--context', self.ctx, '-n', NS, 'build', 'run',
                             *self.run_argv(**{key: value}))
        self.assertEqual(len(self.writes()), before)

    def test_arch_comes_from_the_profile(self):
        self.assertEqual(self.built(arch=None)['job'], f'build-{TAG}-{self.arch}')
        other = 'amd64' if self.arch == 'arm64' else 'arm64'
        before = len(self.writes())
        self.refused('arch_mismatch', '--context', self.ctx, '-n', NS, 'build', 'run', *self.run_argv(arch=other))
        self.assertEqual(len(self.writes()), before)

    def test_a_job_whose_name_is_taken_is_refused_and_says_how(self):
        self.built()
        before = len(self.writes())
        body = self.refused('job_exists', '--context', self.ctx, '-n', NS, 'build', 'run', *self.run_argv())
        self.assertIn('build delete', body['error'])
        self.assertEqual(len(self.writes()), before)

    def test_a_forbidden_field_is_refused_with_its_code(self):
        pod = pod_of(self.rendered())

        host = copy.deepcopy(pod)
        host['volumes'] = pod['volumes'] + [{'name': 'h', 'hostPath': {'path': '/'}}]
        privileged = copy.deepcopy(pod)
        privileged['containers'][0]['securityContext'] = {'privileged': True}
        node_name = copy.deepcopy(pod)
        node_name['nodeName'] = 'gpu-node-a'
        priority = copy.deepcopy(pod)
        priority['priorityClassName'] = 'high'
        host_network = copy.deepcopy(pod)
        host_network['hostNetwork'] = True
        scheduler = copy.deepcopy(pod)
        scheduler['schedulerName'] = 'some-scheduler'
        pull = copy.deepcopy(pod)
        pull['imagePullSecrets'] = [{'name': 'nvcr-pull'}]
        no_storage = copy.deepcopy(pod)
        no_storage['containers'][0]['resources']['limits'].pop('ephemeral-storage')
        no_digest = copy.deepcopy(pod)
        no_digest['containers'][0]['image'] = 'registry.example.test/x:latest'
        wrong_arch = copy.deepcopy(pod)
        wrong_arch['nodeSelector'] = {'kubernetes.io/arch': 'amd64' if self.arch == 'arm64' else 'arm64'}
        cases = {
            'hostPath': (host, 'field_refused'),
            'privileged': (privileged, 'field_refused'),
            'nodeName': (node_name, 'field_refused'),
            'priorityClassName': (priority, 'field_refused'),
            'hostNetwork': (host_network, 'field_refused'),
            'schedulerName': (scheduler, 'field_refused'),
            'imagePullSecrets': (pull, 'field_refused'),
            'ephemeral-storage limit': (no_storage, 'resources_required'),
            'image without a digest': (no_digest, 'field_refused'),
            'arch nodeSelector': (wrong_arch, 'arch_mismatch'),
        }
        for label, (mutated, code) in cases.items():
            with self.subTest(case=label):
                self.refused_render({'spec': {'template': {'spec': mutated}}}, code)

    def test_tag_names_the_commit_and_never_latest(self):
        pod = pod_of(self.rendered())
        build = named(pod['containers'], 'build')
        for tag in ('latest', f'{TAG}0', 'main'):
            with self.subTest(tag=tag):
                mutated = copy.deepcopy(build)
                mutated['command'] = [f'type=image,name={REGISTRY}/gdl-operator:{tag},push=true'
                                      if a.startswith('type=image') else a for a in build['command']]
                self.refused_render({'spec': {'template': {'spec': {'containers': [mutated]}}}}, 'invalid_tag')
        # A build that does not push has not produced the tag the index names.
        mutated = copy.deepcopy(build)
        mutated['command'] = [f'type=image,name={REGISTRY}/gdl-operator:{TAG}-{self.arch}'
                              if a.startswith('type=image') else a for a in build['command']]
        self.refused_render({'spec': {'template': {'spec': {'containers': [mutated]}}}}, 'invalid_tag')

    def test_a_job_without_the_lease_label_on_its_pod_template_is_refused(self):
        body = self.refused_render({'spec': {'template': {'metadata': {'labels': None}}}},
                                   'lease_label_required')
        self.assertIn(LEASE_LABEL, body['error'])


class BuildIndex(BuildCase):
    def index(self, **over):
        a = {'image': 'gdl-operator', 'commit': COMMIT}
        a.update(over)
        return self.build_guard('index', '--image', a['image'], '--commit', a['commit'])

    def test_index_names_both_jobs(self):
        rc, body, p = self.index()
        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        self.assertEqual(body, {'job': f'index-{TAG}'})
        job = self.job(body['job'])
        pod = pod_of(job)
        steps = [c['args'] for c in pod['initContainers'] + pod['containers']]
        self.assertEqual(steps, [
            ['index', 'append', '-t', f'{REGISTRY}/gdl-operator:{TAG}',
             '-m', f'{REGISTRY}/gdl-operator:{TAG}-amd64', '-m', f'{REGISTRY}/gdl-operator:{TAG}-arm64'],
            ['manifest', f'{REGISTRY}/gdl-operator:{TAG}'],
            ['digest', f'{REGISTRY}/gdl-operator:{TAG}'],
        ])
        for c in pod['initContainers'] + pod['containers']:
            self.assertEqual(c['image'], CRANE)
            self.assertEqual(c['command'], ['/ko-app/crane'])
            self.assertEqual(named(c['volumeMounts'], 'docker-config')['mountPath'], '/home/user/.docker')
            self.assertEqual({e['name']: e['value'] for e in c['env']}['DOCKER_CONFIG'], '/home/user/.docker')
        self.assertEqual(job['metadata']['labels'][LEASE_LABEL], self.lid)
        [line] = [l for l in self.ledger() if l['object'].startswith('Job/')]
        self.assertEqual((line['object'], line['gpus']), (f'Job/index-{TAG}', 1))

    def test_index_step_order_is_append_manifest_digest(self):
        job = self.job(self.index()[1]['job'])
        self.assertEqual([c['name'] for c in pod_of(job)['initContainers']], ['index-append', 'index-manifest'])
        self.assertEqual([c['name'] for c in pod_of(job)['containers']], ['index-digest'])

    def test_index_argv_refuses_a_changed_step(self):
        pod = pod_of(self.rendered_index())
        append = dict(named(pod['initContainers'], 'index-append'))
        append['args'] = ['index', 'append', '-t', f'{REGISTRY}/gdl-operator:latest',
                          '-m', f'{REGISTRY}/gdl-operator:{TAG}-amd64']
        self.k8s['mutate'] = {'Job': {'spec': {'template': {'spec': {'initContainers': [
            append, named(pod['initContainers'], 'index-manifest')]}}}}}
        self.flush()
        before = len(self.writes())
        self.refused('invalid_tag', '--context', self.ctx, '-n', NS, 'build', 'index',
                     '--image', 'gdl-operator', '--commit', COMMIT)
        self.assertEqual(len(self.writes()), before)

    def rendered_index(self):
        body = self.job(self.index()[1]['job'])
        job = copy.deepcopy(body)
        del self.k8s['objects'][NS]['Job/' + job['metadata']['name']]
        self.flush()
        return job


class BuildShow(BuildCase):
    def failed_job(self, logs, name=None):
        name = name or f'build-{TAG}-{self.arch}'
        job = {'apiVersion': 'batch/v1', 'kind': 'Job',
               'metadata': {'name': name, 'labels': {LEASE_LABEL: self.lid}},
               'spec': {'template': {'metadata': {'labels': {LEASE_LABEL: self.lid}},
                                     'spec': {'containers': [{'name': 'build', 'image': BUILDKIT}]}}},
               'status': {'conditions': [{'type': 'Failed', 'status': 'True', 'reason': 'BackoffLimitExceeded'}]}}
        self.add_obj(job)
        self.k8s['job_logs'] = {f'job/{name}': logs}
        self.flush()
        return name

    def test_a_failed_build_is_reported_with_its_reason_and_the_tail(self):
        name = self.failed_job('step 1/2\nnvcr.io: DENIED\nnvapi-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345\n'
                               '"auth": "YWJjZGVm"\n')

        rc, body, p = self.build_guard('show', name)

        self.assertNotEqual(rc, 0)
        self.assertEqual((body.get('code'), body.get('state'), body.get('build')),
                         ('build_failed', 'failed', name))
        self.assertIn('DENIED', body['tail'])
        self.assertNotIn('nvapi-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345', json.dumps(body))
        self.assertNotIn('YWJjZGVm', json.dumps(body))
        self.assertIn('[secret]', body['tail'])
        logs = [c for c in self.kubectl_calls() if c['pos'][:1] == ['logs']]
        self.assertTrue(logs and '--all-containers=true' in logs[0]['pos'], logs)
        self.assertEqual([c for c in self.writes() if c['pos'][:1] == ['delete']], [])

    def test_the_marker_survives_a_non_zero_logs_exit(self):
        name = self.failed_job('tag_exists\n')
        self.k8s['job_logs_rc'] = 1
        self.flush()

        rc, body, _ = self.build_guard('show', name)

        self.assertNotEqual(rc, 0)
        self.assertEqual(body.get('code'), 'tag_exists', body)
        self.assertIn('tag_exists', body['tail'])
        logs = [c for c in self.kubectl_calls() if c['pos'][:1] == ['logs']]
        self.assertTrue(logs and '--ignore-errors=true' in logs[0]['pos'], logs)

    def test_the_tag_check_answers_become_the_refusal_codes(self):
        cases = {'tag_exists': 'tag_exists', 'registry_denied': 'registry_denied',
                 'registry_error: connection reset': 'registry_error'}
        for line, code in cases.items():
            with self.subTest(answer=line):
                name = self.failed_job(line + '\n')
                rc, body, _ = self.build_guard('show', name)
                self.assertNotEqual(rc, 0)
                self.assertEqual(body.get('code'), code, body)

    def test_a_succeeded_build_prints_its_tail_and_exits_zero(self):
        name = f'build-{TAG}-{self.arch}'
        self.add_obj({'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': {'name': name},
                      'spec': {}, 'status': {'conditions': [{'type': 'Complete', 'status': 'True'}],
                                             'succeeded': 1}})
        self.k8s['job_logs'] = {f'job/{name}': 'exporting to nvcr.io\n'}
        self.flush()

        rc, body, _ = self.build_guard('show', name)

        self.assertEqual(rc, 0, body)
        self.assertEqual((body['build'], body['state']), (name, 'succeeded'))
        self.assertIn('exporting', body['tail'])

    def test_show_of_a_missing_job_is_not_found(self):
        self.refused('not_found', '--context', self.ctx, '-n', NS, 'build', 'show', 'build-nope')


class BuildDelete(BuildCase):
    def test_delete_of_a_job_propagates_to_its_pods(self):
        name = self.built()['job']
        pod = {'metadata': {'name': name + '-0', 'labels': {LEASE_LABEL: self.lid, 'job-name': name}},
               'spec': {'containers': [container(0)]}, 'status': {'phase': 'Running'}}
        self.add_pod(pod)

        body = self.ok('--context', self.ctx, '-n', NS, 'build', 'delete', name)

        self.assertEqual(body, {'deleted': f'Job/{name}', 'namespace': NS})
        self.assertIsNone(self.job(name))
        self.assertEqual(self.k8s['pods'][NS], [])
        [delete] = [b for b in self.k8s['raw_delete_bodies'] if '/jobs/' in b['path']]
        self.assertEqual(delete['body']['propagationPolicy'], 'Background')
        self.assertEqual(self.ledger()[-1]['direction'], 'down')

    def test_the_plain_delete_alias_knows_jobs(self):
        name = self.built()['job']

        body = self.ok('--context', self.ctx, '-n', NS, 'delete', f'job/{name}')

        self.assertEqual(body['deleted'], f'Job/{name}')
        self.assertIsNone(self.job(name))
        [delete] = [b for b in self.k8s['raw_delete_bodies'] if '/jobs/' in b['path']]
        self.assertEqual(delete['body']['propagationPolicy'], 'Background')

    def test_delete_of_another_leases_job_is_refused(self):
        job = {'apiVersion': 'batch/v1', 'kind': 'Job',
               'metadata': {'name': 'build-theirs', 'labels': {LEASE_LABEL: 'lse_someone9999'}},
               'spec': {'template': {'metadata': {'labels': {LEASE_LABEL: 'lse_someone9999'}}}}}
        self.add_obj(job)
        self.refused('object_not_yours', '--context', self.ctx, '-n', NS, 'build', 'delete', 'build-theirs')
        self.assertIsNotNone(self.job('build-theirs'))


class BuildAttempts(BuildCase):
    def test_a_timed_out_job_create_leaves_an_attempt_and_refuses(self):
        self.ensure_git()
        name = f'build-{TAG}-{self.arch}'
        self.k8s['stall_create'] = [f'Job/{name}']
        self.flush()

        rc, body, _ = self.build_guard('run', *self.run_argv())
        self.assertEqual(body.get('code'), 'write_unresolved', body)
        [a] = [a for a in self.bb['leases'][self.lid]['attempts'] if a['kind'] == 'Job']
        self.assertEqual((a['verb'], a['name'], a['outcome']), ('create', name, None))

        before = len(self.writes())
        rc, body, _ = self.build_guard('run', *self.run_argv(image='other'))
        self.assertEqual(body.get('code'), 'attempt_unresolved', body)
        self.assertEqual(len(self.writes()), before)
        self.assertNotIn(['k8s', 'lease', 'op', 'begin', self.lid], self.bb_calls()[-2:])


class GitBuild(BuildCase):
    """The git secret and the mount the clone initContainer reads it through."""

    REPO = 'owner/repo'
    SSH = 'git@github.com:owner/repo.git'
    REF = 'op://Fleet/owner-repo deploy key/private key'

    def secret_git(self, repo=REPO, **env_kw):
        return self.guard('--context', self.ctx, '-n', NS, 'secret', 'git', '--repo', repo, **env_kw)

    def made(self):
        rc, body, p = self.secret_git()
        self.assertEqual(rc, 0, f'{body} {p.stderr}')

    def test_secret_git_builds_the_convention_ref_and_never_prints_the_key(self):
        rc, body, p = self.secret_git()

        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        self.assertEqual([l for l in self.fleet_log.read_text().splitlines() if 'deploy key' in l],
                         ['secret get ' + self.REF])
        stored = self.k8s['objects'][NS]['Secret/git']
        self.assertEqual(stored['stringData'], {'id_ed25519': SECRET + '\n'})
        self.assertEqual(stored['metadata']['labels'], {NS_UID: 'uid-ns-1', LEASE_LABEL: self.lid})
        cm = self.k8s['objects'][NS]['ConfigMap/git-known-hosts']
        self.assertEqual(cm['data'], {'known_hosts': GITHUB_KEY})
        self.assertEqual(cm['metadata']['labels'], {NS_UID: 'uid-ns-1', LEASE_LABEL: self.lid})
        self.assertEqual(body, {'secret': 'git', 'namespace': NS, 'known_hosts': 'git-known-hosts'})
        self.assert_no_leak(p)

    def test_the_git_key_keeps_exactly_one_trailing_newline(self):
        rc, body, p = self.guard('--context', self.ctx, '-n', NS, 'secret', 'git', '--repo', self.REPO,
                                 secret=SECRET + '\n\n')

        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        stored = self.k8s['objects'][NS]['Secret/git']
        self.assertEqual(stored['stringData'], {'id_ed25519': SECRET + '\n'})

    def test_the_clone_mounts_the_key_and_known_hosts_read_only_for_user_1000(self):
        self.made()

        job = self.job(self.built(repo=self.SSH)['job'])
        pod = pod_of(job)
        self.assertEqual(pod['securityContext']['fsGroup'], 1000)
        self.assertEqual(pod['securityContext']['runAsUser'], 1000)
        volume = named(pod['volumes'], 'git-ssh')
        self.assertEqual(volume['projected']['defaultMode'], 0o400)
        self.assertEqual(volume['projected']['sources'], [
            {'secret': {'name': 'git', 'items': [{'key': 'id_ed25519', 'path': 'id_ed25519'}]}},
            {'configMap': {'name': 'git-known-hosts',
                           'items': [{'key': 'known_hosts', 'path': 'known_hosts'}]}},
        ])
        clone = named(pod['initContainers'], 'clone')
        self.assertEqual(clone['image'], BUILDKIT)
        self.assertEqual(clone['securityContext']['runAsUser'], 1000)
        mount = named(clone['volumeMounts'], 'git-ssh')
        self.assertEqual((mount['mountPath'], mount['readOnly']), ('/home/user/.ssh', True))
        options = shlex.split({e['name']: e['value'] for e in clone['env']}['GIT_SSH_COMMAND'])
        self.assertEqual(options, ['ssh', '-i', '/home/user/.ssh/id_ed25519', '-o', 'IdentitiesOnly=yes',
                                   '-o', 'UserKnownHostsFile=/home/user/.ssh/known_hosts',
                                   '-o', 'StrictHostKeyChecking=yes'])
        self.assertNotIn('accept-new', options)
        # The clone container overrides the image entrypoint (rootlesskit buildkitd) with its own command.
        self.assertEqual(clone['command'][:2], ['sh', '-c'])
        self.assertIn('git clone --filter=blob:none', clone['command'][-1])
        self.assertIn('git checkout', clone['command'][-1])
        self.assertNotIn('alpine/git', json.dumps(job))

    def test_an_ssh_repo_without_the_secret_is_refused_before_any_write(self):
        before = len(self.writes())
        body = self.refused('secret_unavailable', '--context', self.ctx, '-n', NS, 'build', 'run',
                            *self.run_argv(repo=self.SSH))
        self.assertIn('secret git', body['error'])
        self.assertEqual(len(self.writes()), before)

    def test_a_missing_known_hosts_configmap_is_refused_before_any_write(self):
        self.made()
        del self.k8s['objects'][NS]['ConfigMap/git-known-hosts']
        self.flush()
        before = len(self.writes())

        body = self.refused('secret_unavailable', '--context', self.ctx, '-n', NS, 'build', 'run',
                            *self.run_argv(repo=self.SSH))

        self.assertIn('git-known-hosts', body['error'])
        self.assertEqual(len(self.writes()), before)

    def test_a_public_repo_needs_no_mount(self):
        job = self.job(self.built(repo='https://github.com/owner/repo.git')['job'])
        self.assertNotIn('git-ssh', [v['name'] for v in pod_of(job)['volumes']])

    def ssh_mutation(self, pod_change, code):
        pod = pod_of(self.rendered(repo=self.SSH))
        changed = copy.deepcopy(pod)
        pod_change(changed)
        self.refused_render({'spec': {'template': {'spec': changed}}}, code)

    def test_a_writable_misplaced_or_loose_key_is_refused(self):
        def writable(pod):
            named(named(pod['initContainers'], 'clone')['volumeMounts'], 'git-ssh')['readOnly'] = False

        def outside(pod):
            named(named(pod['initContainers'], 'clone')['volumeMounts'], 'git-ssh')['mountPath'] = '/root/.ssh'

        def loose(pod):
            named(pod['volumes'], 'git-ssh')['projected']['defaultMode'] = 0o600

        def plain_secret(pod):
            volume = named(pod['volumes'], 'git-ssh')
            self.assertIn('projected', volume)
            volume['secret'] = {'secretName': 'git', 'defaultMode': 0o400}
            del volume['projected']

        def no_known_hosts(pod):
            volume = named(pod['volumes'], 'git-ssh')
            volume['projected']['sources'] = volume['projected']['sources'][:1]

        for label, change in (('writable', writable), ('outside', outside), ('mode 0600', loose),
                              ('plain secret', plain_secret), ('no known_hosts', no_known_hosts)):
            with self.subTest(case=label):
                self.ssh_mutation(change, 'ssh_mount_refused')

    def test_a_clone_that_is_not_the_pinned_image_or_sets_no_command_is_refused(self):
        def other_image(pod):
            named(pod['initContainers'], 'clone')['image'] = 'alpine/git:2.45'

        def no_command(pod):
            named(pod['initContainers'], 'clone').pop('command')

        def accept_new(pod):
            clone = named(pod['initContainers'], 'clone')
            for env in clone['env']:
                if env['name'] == 'GIT_SSH_COMMAND':
                    env['value'] = env['value'].replace('StrictHostKeyChecking=yes',
                                                        'StrictHostKeyChecking=accept-new')

        for label, change in (('image', other_image), ('no command', no_command), ('accept-new', accept_new)):
            with self.subTest(case=label):
                self.ssh_mutation(change, 'field_refused')

    def test_bad_repos_and_missing_items_are_refused(self):
        before = len(self.writes())
        for repo in ('owner', 'owner/repo/extra', '../repo', 'owner/re po', 'owner/repo;id', ''):
            with self.subTest(repo=repo):
                rc, body, _ = self.secret_git(repo=repo)
                self.assertNotEqual(rc, 0, body)
                self.assertEqual(body.get('code'), 'usage', body)
        self.assertEqual(len(self.writes()), before)
        self.assertFalse(any('deploy key' in l for l in self.fleet_log.read_text().splitlines()))

        rc, body, p = self.secret_git(fleet_fail=True)
        self.assertEqual(body.get('code'), 'secret_unavailable', body)
        self.assertNotIn('Secret/git', self.k8s['objects'][NS])
        self.assertNotIn('ConfigMap/git-known-hosts', self.k8s['objects'][NS])
        self.assert_no_leak(p)

    def test_repo_is_only_for_git(self):
        before = self.fleet_log.read_text() if self.fleet_log.exists() else ''
        self.refused('usage', '--context', self.ctx, '-n', NS, 'secret', 'hf', '--repo', self.REPO)
        self.assertEqual(self.fleet_log.read_text() if self.fleet_log.exists() else '', before)


class ClaimedBuild(ClaimedCase):
    def claimed_build(self, **over):
        over.setdefault('arch', 'arm64')
        for which, extra in (('ngc-push', ('--exposure-accepted',)), ('git', ('--repo', 'owner/repo'))):
            if f'Secret/{which}' not in self.k8s['objects'].get(PREMADE, {}):
                rc, body, p = self.guard('--context', CTX, '-n', PREMADE, 'secret', which, *extra)
                self.assertEqual(rc, 0, f'{body} {p.stderr}')
        return self.guard('--context', CTX, '-n', PREMADE, 'build', 'run', *build_argv(**over))

    def test_no_claim_no_build(self):
        self.bb['claims'] = 'off'
        self.flush()

        self.refused('namespace_not_claimed', '--context', CTX, '-n', PREMADE, 'build', 'run',
                     *build_argv(arch='arm64'))
        self.assertEqual(self.writes(), [])

    def test_the_ledger_records_the_claim_charge_not_the_jobs_zero(self):
        lid = self.claimed(gpus=1)
        rc, body, p = self.claimed_build()
        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        [line] = [l for l in self.ledger() if l['object'].startswith('Job/')]
        self.assertEqual((line['object'], line['gpus'], line['thread'], line['cluster'], line['namespace']),
                         (f'Job/build-{TAG}-arm64', 1, ME, PROFILE, PREMADE))
        pod = pod_of(self.k8s['objects'][PREMADE][f'Job/{body["job"]}'])
        for c in pod['containers'] + pod['initContainers']:
            self.assertNotIn('nvidia.com/gpu', c['resources']['requests'])
            self.assertNotIn('nvidia.com/gpu', c['resources']['limits'])
        self.assertIn('Job', [a['kind'] for a in self.bb['leases'][lid]['attempts']])

    def test_claimed_teardown_deletes_jobs_with_background_propagation_and_lists_them_again(self):
        lid = self.claimed(state='releasing')
        name = f'build-{TAG}-arm64'
        job = {'apiVersion': 'batch/v1', 'kind': 'Job',
               'metadata': {'name': name, 'labels': {LEASE_LABEL: lid}},
               'spec': {'template': {'metadata': {'labels': {LEASE_LABEL: lid}},
                                     'spec': {'containers': [{'name': 'build', 'image': BUILDKIT}]}}}}
        self.add_obj(job, ns=PREMADE)
        self.add_pod({'metadata': {'name': name + '-0', 'labels': {LEASE_LABEL: lid, 'job-name': name}},
                      'spec': {'containers': [container(0)]}, 'status': {'phase': 'Running'}}, ns=PREMADE)

        body = self.ok('--context', CTX, 'down', PREMADE, '--uid', 'uid-premade-1', '--lease', lid, host_entry=True)

        self.assertTrue(body['complete'], body)
        self.assertIn(f'Job/{name}', body['deleted'])
        self.assertIsNone(self.k8s['objects'][PREMADE].get(f'Job/{name}'))
        self.assertEqual(self.k8s['pods'][PREMADE], [])
        [delete] = [b for b in self.k8s['raw_delete_bodies'] if '/jobs/' in b['path']]
        self.assertEqual(delete['body']['propagationPolicy'], 'Background')
        self.assertTrue(any(c['pos'][:2] == ['get', 'jobs.batch']
                            and c['flags'].get('-l') == f'{LEASE_LABEL}={lid}'
                            for c in self.kubectl_calls()), 'the teardown re-read did not list Jobs')

    def test_a_timed_out_job_create_blocks_writes_and_teardown_until_it_lands(self):
        lid = self.claimed()
        name = f'build-{TAG}-arm64'
        self.k8s['stall_create'] = [f'Job/{name}']
        self.flush()

        rc, body, _ = self.claimed_build()
        self.assertEqual(body.get('code'), 'write_unresolved', body)
        self.bb['leases'][lid].update(slot=None, state='revoking')
        self.flush()

        rc, body, _ = self.teardown('down', lid)
        self.assertEqual(body.get('code'), 'teardown_incomplete', body)
        self.assertIn('unresolved', json.dumps(body))

        self.land_pending()
        body = self.ok('--context', CTX, 'down', PREMADE, '--uid', 'uid-premade-1', '--lease', lid, host_entry=True)
        self.assertTrue(body['complete'], body)
        self.assertEqual([r['outcome'] for r in body['resolutions']], ['committed'])
        self.assertIsNone(self.k8s['objects'][PREMADE].get(f'Job/{name}'))

    def test_a_timed_out_known_hosts_create_resolves_and_tears_down(self):
        lid = self.claimed()
        self.k8s['stall_create'] = ['ConfigMap/git-known-hosts']
        self.flush()

        rc, body, _ = self.guard('--context', CTX, '-n', PREMADE, 'secret', 'git', '--repo', 'owner/repo')
        self.assertEqual(body.get('code'), 'write_unresolved', body)
        cm = next(a for a in self.bb['leases'][lid]['attempts'] if a['kind'] == 'ConfigMap')

        self.land_pending()
        self.bb['leases'][lid].update(slot=None, state='revoking')
        self.flush()
        body = self.ok('--context', CTX, 'down', PREMADE, '--uid', 'uid-premade-1', '--lease', lid, host_entry=True)

        self.assertTrue(body['complete'], body)
        self.assertIn({'attempt': cm['attempt'], 'outcome': 'committed'}, body['resolutions'])
        self.assertNotIn('ConfigMap/git-known-hosts', self.k8s['objects'][PREMADE])

    def test_nvcr_push_lives_only_as_long_as_the_claim(self):
        lid = self.claimed(gpus=1)
        rc, body, p = self.guard('--context', CTX, '-n', PREMADE, 'secret', 'ngc-push', '--exposure-accepted')
        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        stored = self.k8s['objects'][PREMADE]['Secret/nvcr-push']
        self.assertEqual(stored['metadata']['labels'][LEASE_LABEL], lid)
        self.assertEqual(self.fleet_log.read_text().splitlines(), ['secret get ' + PUSH_REF])
        self.assert_no_leak(p)

        self.bb['leases'][lid]['state'] = 'releasing'
        self.flush()
        body = self.ok('--context', CTX, 'down', PREMADE, '--uid', 'uid-premade-1', '--lease', lid, host_entry=True)
        self.assertTrue(body['complete'], body)
        self.assertNotIn('Secret/nvcr-push', self.k8s['objects'][PREMADE])
        self.assertIn('Secret/nvcr-push', body['deleted'])

        lid = self.claimed(gpus=1)
        rc, body, p = self.guard('--context', CTX, '-n', PREMADE, 'secret', 'ngc-push', '--exposure-accepted')
        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        self.assertEqual(self.k8s['objects'][PREMADE]['Secret/nvcr-push']['metadata']['labels'][LEASE_LABEL], lid)


class BuildSecrets(GuardCase):
    def setUp(self):
        super().setUp()
        self.add_ns()
        self.lid = self.add_lease(gpus=1)

    def test_push_secret_needs_the_stated_acceptance(self):
        self.refused('push_not_approved', '--context', CTX, '-n', NS, 'secret', 'ngc-push')
        self.assertNotIn('Secret/nvcr-push', self.k8s['objects'][NS])
        self.assertFalse(self.fleet_log.exists())
        self.refused('usage', '--context', CTX, '-n', NS, 'secret', 'hf', '--exposure-accepted')
        self.assertFalse(self.fleet_log.exists())

    def test_push_secret_is_piped_and_never_printed(self):
        rc, body, p = self.guard('--context', CTX, '-n', NS, 'secret', 'ngc-push', '--exposure-accepted')

        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        self.assertEqual(body, {'secret': 'nvcr-push', 'namespace': NS})
        stored = self.k8s['objects'][NS]['Secret/nvcr-push']
        self.assertEqual((stored['type'], stored['metadata']['labels'][LEASE_LABEL]),
                         ('kubernetes.io/dockerconfigjson', self.lid))
        cfg = json.loads(stored['stringData']['.dockerconfigjson'])
        self.assertEqual((cfg['auths']['nvcr.io']['username'], cfg['auths']['nvcr.io']['password']),
                         ('$oauthtoken', SECRET))
        self.assertEqual(self.fleet_log.read_text().splitlines(), ['secret get ' + PUSH_REF])
        self.assert_no_leak(p)
        [line] = [l for l in self.ledger() if l['object'] == 'Secret/nvcr-push']
        self.assertTrue(line['exposure_accepted'], line)

    def test_pull_secret_uses_the_pull_only_item_and_never_falls_back(self):
        rc, body, p = self.guard('--context', CTX, '-n', NS, 'secret', 'ngc')
        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        self.assertEqual(self.fleet_log.read_text().splitlines(), ['secret get ' + PULL_REF])

        rc, body, p = self.guard('--context', CTX, '-n', NS, 'secret', 'ngc', fleet_fail=True)
        self.assertEqual(body.get('code'), 'secret_unavailable', body)
        self.assertIn('Secret/nvcr-pull', self.k8s['objects'][NS])  # the first one is still the only one
        self.assertEqual(self.fleet_log.read_text().splitlines(),
                         ['secret get ' + PULL_REF, 'secret get ' + PULL_REF])
        self.assertNotIn('NGC NIM Operations PAT', self.fleet_log.read_text())
        self.assert_no_leak(p)

    def test_no_secret_shaped_string_is_committed(self):
        for path in sorted((SKILL / 'scripts').glob('*')):
            if not path.is_file():
                continue
            with self.subTest(path=path.name):
                self.assertIsNone(re.search(r'nv(?:api|cf|pr)-[A-Za-z0-9_-]{16,}', path.read_text()),
                                  f'{path} carries a secret-shaped string')


# --- the tag-check probe ----------------------------------------------------

FAKE_CRANE = r'''#!/usr/bin/env python3
"""Fake crane for the tag-check probe: prints what the test says and exits with its code."""
import json, os, sys

open(os.environ['FAKE_CRANE_LOG'], 'a').write(json.dumps(sys.argv[1:]) + '\n')
sys.stderr.write(os.environ.get('FAKE_CRANE_ERR', ''))
sys.stdout.write(os.environ.get('FAKE_CRANE_OUT', ''))
sys.exit(int(os.environ.get('FAKE_CRANE_RC', '0')))
'''


class TagCheckScript(GuardCase):
    """The tag-check probe the initContainer runs, against a fake crane: the plan's answers, no cluster.

    crane is the plan's tool: it sends every manifest Accept type itself, so a 404 is a missing tag and
    not an unaccepted OCI index, and its stderr carries MANIFEST_UNKNOWN. The probe turns its exit code
    into the initContainer's answer.
    """

    def setUp(self):
        super().setUp()
        import sys
        sys.path.insert(0, str(SKILL / 'scripts'))
        from k8s_guard_rules import TAG_CHECK_SCRIPT
        self.script = self.tmp / 'tag-check.sh'
        self.script.write_text(TAG_CHECK_SCRIPT)
        (self.bin / 'crane').write_text(FAKE_CRANE)
        (self.bin / 'crane').chmod(0o755)
        self.ref = f'{REGISTRY}/gdl-operator:{TAG}-amd64'
        self.crane_log = self.tmp / 'crane.log'

    def probe(self, rc, err='', out=''):
        env = {'PATH': f'{self.bin}:/usr/bin:/bin', 'FAKE_CRANE_LOG': str(self.crane_log),
               'FAKE_CRANE_RC': str(rc), 'FAKE_CRANE_ERR': err, 'FAKE_CRANE_OUT': out,
               'CRANE': 'crane', 'REF': self.ref, 'DOCKER_CONFIG': str(self.tmp)}
        p = subprocess.run(['/bin/sh', str(self.script)], env=env, capture_output=True, text=True, timeout=20,
                           stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout + p.stderr).strip()

    def test_the_tag_that_exists_refuses(self):
        rc, out = self.probe(0, out='sha256:' + 'a' * 64)
        self.assertEqual((rc, out), (1, 'tag_exists'))

    def test_a_denied_registry_refuses(self):
        for err in ('Error: GET https://nvcr.io/...: UNAUTHORIZED: authentication required',
                    'Error: GET https://nvcr.io/...: DENIED: requested access to the resource is denied',
                    'unexpected status code 403 Forbidden'):
            with self.subTest(err=err):
                rc, out = self.probe(1, err=err)
                self.assertEqual(rc, 1)
                self.assertIn('registry_denied', out)

    def test_only_an_unknown_tag_or_repository_lets_the_build_run(self):
        # A repository that does not exist yet cannot hold the tag, so it is absent in the same way.
        for err in ('Error: GET https://nvcr.io/...: MANIFEST_UNKNOWN: Failed to fetch "x"',
                    'Error: manifest unknown',
                    'Error: GET https://nvcr.io/...: NAME_UNKNOWN: repository name not known to registry',
                    'Error: name unknown'):
            with self.subTest(err=err):
                rc, out = self.probe(1, err=err)
                self.assertEqual((rc, out), (0, ''))
        for err in ('Error: unexpected status code 404 Not Found', ''):
            with self.subTest(err=err):
                rc, out = self.probe(1, err=err)
                self.assertEqual(rc, 1)
                self.assertIn('registry_error', out)

    def test_any_other_answer_refuses(self):
        for err in ('Error: unexpected status code 500 Internal Server Error',
                    'Get "https://nvcr.io/v2/": dial tcp: lookup nvcr.io: no such host'):
            with self.subTest(err=err):
                rc, out = self.probe(1, err=err)
                self.assertEqual(rc, 1)
                self.assertIn('registry_error', out)

    def test_the_probe_digests_the_pinned_tag_with_crane(self):
        self.probe(0)
        [call] = [json.loads(line) for line in self.crane_log.read_text().splitlines()]
        self.assertEqual(call, ['digest', self.ref])

# --- the lease fence (references/k8s-guard.md, "The fence policy") ------------------------------------------

FENCE_LABEL = 'dynamo.nvidia.com/agent-lease-fence'
GATE = 'ConfigMap/wcep-agent-fence'
# The clock the drain uses, patched in: monotonic time moves only by sleep, and wall time jumps a day ahead.
CLOCK_RUNNER = r'''
import json
from datetime import timedelta
from pathlib import Path

class Clock:
    elapsed = 0.0
    sleeps = []

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.elapsed += seconds

guard = runpy.run_path(sys.argv[0])
clock = Clock()
g = guard['main'].__globals__
g['time'] = clock
# Slow cluster calls, which the Clock alone never shows: the deletes after the close, or each inventory list.
slow_after_close = float(os.environ.get('FENCE_TEST_SLOW_AFTER_CLOSE', '0'))
slow_list = float(os.environ.get('FENCE_TEST_SLOW_LIST', '0'))
real_confirm, real_labelled, calls = g['confirm_uid'], g['labelled'], {'confirm': 0}

def confirm_uid(*args, **kw):
    calls['confirm'] += 1
    if calls['confirm'] == 3:  # the first confirm after the gate's close (NsView, move_gate, then this one)
        clock.elapsed += slow_after_close
    return real_confirm(*args, **kw)

def labelled(*args, **kw):
    clock.elapsed += slow_list
    return real_labelled(*args, **kw)

g['confirm_uid'] = confirm_uid
g['labelled'] = labelled
per_call = float(os.environ.get('FENCE_TEST_PER_CALL', '0'))
Kubectl = g['Kubectl']
real_run, real_pair = Kubectl.run, getattr(Kubectl, 'run_pair', None)

def run(self, *args, **kw):
    clock.elapsed += per_call
    return real_run(self, *args, **kw)

Kubectl.run = run
if real_pair is not None:
    def run_pair(self, *args, **kw):
        clock.elapsed += per_call  # the two Pods go at once: one call's time
        return real_pair(self, *args, **kw)
    Kubectl.run_pair = run_pair
real_now = g['now']
g['now'] = lambda: real_now() + timedelta(days=1)  # a wall-clock step: must not end the drain
status = guard['main']()
state_path = Path(os.environ['FAKE_K8S_STATE'])
state = json.loads(state_path.read_text())
state['clock_elapsed'] = clock.elapsed
state['clock_sleeps'] = clock.sleeps
state_path.write_text(json.dumps(state))
sys.exit(status)
'''


class UnfencedGpuSpend(GuardCase):
    def setUp(self):
        super().setUp()
        self.profiles.write_text(self.profiles.read_text().replace(
            '# premade_namespace = "dyntest-alice-agent"', f'premade_namespace = "{PREMADE}"'))
        self.add_ns(PREMADE, ledger=False)

    def test_gpu_job_requires_a_spend_record_before_submission(self):
        rc, body, _ = self.apply(spend_job(namespace=PREMADE), ns=PREMADE)
        self.assertEqual(rc, 1, body)
        self.assertEqual(body['code'], 'gpu_spend_required', body)
        self.assertEqual(body['error'], f'Job/gpu-run requests 4 GPUs in {PROFILE}/{PREMADE} without a lease fence. '
                         'Add the wcep.agent/gpu-spend annotation with thread, gpus, start, max_hours, cluster and namespace.')
        self.assertFalse([c for c in self.kubectl_calls() if c['pos'][:1] == ['create']])

    def test_gpu_job_with_spend_record_is_created_without_a_lease(self):
        job = spend_job(namespace=PREMADE, recorded=True)
        rc, body, _ = self.apply(job, ns=PREMADE)
        self.assertEqual(rc, 0, body)
        self.assertEqual(body['gpu_spend'], json.loads(job['metadata']['annotations']['wcep.agent/gpu-spend']))
        stored = self.k8s['objects'][PREMADE]['Job/gpu-run']
        self.assertEqual(stored['metadata']['annotations'], job['metadata']['annotations'])
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.bb_calls(), [])

    def test_spend_record_must_match_the_job_target_and_thread(self):
        for field, value in [('gpus', 2), ('cluster', 'other'), ('namespace', NS), ('thread', 'thr_other'),
                             ('start', '2026-11-24T16:00:00'), ('max_hours', 0), ('max_hours', 1e12)]:
            with self.subTest(field=field):
                job = spend_job(namespace=PREMADE, recorded=True)
                annotations = job['metadata']['annotations']
                record = json.loads(annotations['wcep.agent/gpu-spend'])
                annotations['wcep.agent/gpu-spend'] = json.dumps(dict(record, **{field: value}))
                rc, body, _ = self.apply(job, ns=PREMADE)
                self.assertEqual(rc, 1, body)
                self.assertEqual(body['code'], 'gpu_spend_invalid', body)
                self.assertIn(field, body['error'])
        self.k8s['mutate'] = {'Job': {'spec': {'parallelism': 3}}}
        self.flush()
        rc, body, _ = self.apply(spend_job(namespace=PREMADE, recorded=True), ns=PREMADE)
        self.assertEqual(rc, 1, body)
        self.assertEqual(body['code'], 'gpu_spend_invalid', body)
        self.assertIn('Set gpus to 6', body['error'])
        self.assertEqual(self.writes(), [])

    def test_spend_window_must_be_open_and_cap_the_job_deadline(self):
        cases = [
            ('gpu_spend_expired', '2001-01-01T00:00:00Z', 1200),
            ('gpu_spend_not_started', iso_ms(hours_from_now(1)), 1200),
            ('gpu_spend_deadline_invalid', None, None),
            ('gpu_spend_deadline_invalid', None, 1801),
            ('gpu_spend_deadline_invalid', iso_ms(hours_from_now(-0.48)), 1200),
        ]
        for i, (code, start, deadline) in enumerate(cases):
            with self.subTest(code=code, deadline=deadline):
                job = spend_job(f'gpu-window-{i}', namespace=PREMADE, recorded=True, start=start)
                if deadline is None:
                    del job['spec']['activeDeadlineSeconds']
                else:
                    job['spec']['activeDeadlineSeconds'] = deadline
                rc, body, _ = self.apply(job, ns=PREMADE)
                self.assertEqual(rc, 1, body)
                self.assertEqual(body['code'], code, body)
        self.assertFalse([c for c in self.kubectl_calls() if c['pos'][:1] == ['create']])
        self.k8s['mutate'] = {'Job': {'spec': {'activeDeadlineSeconds': 1801}}}
        self.flush()
        rc, body, _ = self.apply(spend_job(namespace=PREMADE, recorded=True), ns=PREMADE)
        self.assertEqual(rc, 1, body)
        self.assertEqual(body['code'], 'gpu_spend_deadline_invalid', body)
        self.assertEqual(self.writes(), [])


class FenceCase(ClaimedCase):
    """The pre-made namespace enrolled in the fence (ops set the label), with the fence armed by default."""

    def setUp(self):
        super().setUp()
        self.k8s['namespaces'][PREMADE]['metadata']['labels'][FENCE_LABEL] = 'true'
        self.flush()

    def gate(self):
        cm = self.k8s['objects'][PREMADE].get(GATE)
        return None if cm is None else (cm['data']['lease_id'], cm['data']['state'], cm['data']['seq'])

    def set_gate(self, lease_id, state, seq):
        self.k8s['objects'][PREMADE][GATE] = {
            'apiVersion': 'v1', 'kind': 'ConfigMap',
            'metadata': {'name': 'wcep-agent-fence', 'namespace': PREMADE, 'uid': 'uid-gate', 'resourceVersion': '77'},
            'data': {'lease_id': lease_id, 'state': state, 'seq': str(seq), 'uid': 'uid-premade-1'}}
        self.flush()

    def gate_writes(self):
        return [c for c in self.writes() if 'wcep-agent-fence' in ' '.join(c['argv'])
                or (c['pos'][:2] == ['create', '-f'] and c['flags'].get('-o') == 'json'
                    and 'wcep-agent-fence' in json.dumps(self.k8s['objects'][PREMADE].get(GATE)))]

    def probe(self, **kw):
        return self.guard('--context', CTX, 'fence', 'probe', PREMADE, host_entry=True, **kw)

    def clocked_down(self, lid, env_extra=None):
        """Claimed teardown on a host with the Clock: real defaults unless env_extra sets them."""
        self.runner.write_text(RUNNER.replace("runpy.run_path(sys.argv[0], run_name='__main__')", CLOCK_RUNNER, 1))
        env = self.env(host_entry=True)
        env.pop('K8S_GUARD_FENCE_DRAIN_SECONDS', None)
        env.pop('K8S_GUARD_FENCE_PROBE_SECONDS', None)
        env.update(env_extra or {})
        p = subprocess.run(['python3', '-I', str(self.runner), str(GUARD), '--context', CTX, 'down', PREMADE, '--uid',
                            'uid-premade-1', '--lease', lid], env=env, capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL)
        self.reload()
        return p.returncode, json.loads(p.stdout)

    def probes_sent(self):
        return [b['metadata']['labels']['wcep.agent/fence-probe'] for b in self.k8s.get('probe_bodies', [])]


DEV_DIGEST = 'sha256:' + 'a' * 64
DEV_REPO = 'https://github.com/owner/repo.git'


def dev_argv(repo=DEV_REPO):
    return ['--repo', repo, '--commit', COMMIT, '--image', 'gdl-dev', '--image-commit', COMMIT,
            '--digest', DEV_DIGEST]


class JobAdmissionSplit(unittest.TestCase):
    def test_dev_job_passes_the_shared_admission(self):
        code = """
import k8s_guard_rules as rules
args = dict(repo={repo!r}, commit={commit!r}, image='gdl-dev', image_commit={commit!r},
            arch='arm64', digest={digest!r}, ssh=False, deadline=1200)
job = rules.dev_job({ns!r}, 'uid-test', 'lse_test0001', 'att_12345678901234567890', **args)
rules.check_job(job, 'lse_test0001', dict(args, kind='dev', ns={ns!r}, uid='uid-test',
                                        attempt='att_12345678901234567890'), rendered=True)
""".format(repo=DEV_REPO, commit=COMMIT, digest=DEV_DIGEST, ns=PREMADE)
        p = subprocess.run(['python3', '-B', '-c', code], cwd=GUARD.parent,
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)


class DevRun(FenceCase):
    def dev(self, *args):
        return '--context', CTX, '-n', PREMADE, 'dev', *args

    def pull_secret(self):
        self.ok('--context', CTX, '-n', PREMADE, 'secret', 'ngc')

    def launched(self, repo=DEV_REPO):
        body = self.ok(*self.dev('run', *dev_argv(repo)))
        return self.k8s['objects'][PREMADE]['Job/' + body['job']]

    def test_claim_and_open_gate_are_required(self):
        self.refused('namespace_not_claimed', *self.dev('run', *dev_argv()))
        lid = self.claimed()
        self.set_gate(lid, 'closing', self.bb['leases'][lid]['claim_seq'])
        self.refused('fence_not_open', *self.dev('run', *dev_argv()))

    def test_trial_and_missing_secrets_are_refused(self):
        lid = self.claimed()
        self.bb['leases'][lid]['trial'] = True
        self.flush()
        self.refused('trial_dev_refused', *self.dev('run', *dev_argv()))
        self.bb['leases'][lid].pop('trial')
        self.flush()
        self.refused('secret_unavailable', *self.dev('run', *dev_argv()))
        self.pull_secret()
        self.refused('secret_unavailable', *self.dev('run', *dev_argv('git@github.com:owner/repo.git')))

    def test_attempt_name_open_job_refusal_and_same_commit_relaunch(self):
        lid = self.claimed()
        self.pull_secret()
        expires = datetime.fromisoformat(self.bb['leases'][lid]['expires_at'].replace('Z', '+00:00'))
        before_run = datetime.now(timezone.utc)
        job = self.launched()
        after_run = datetime.now(timezone.utc)
        deadline = job['spec']['activeDeadlineSeconds']
        self.assertLessEqual(int((expires - after_run).total_seconds()), deadline)
        self.assertLessEqual(deadline, int((expires - before_run).total_seconds()))
        name = job['metadata']['name']
        attempt = job['metadata']['annotations']['wcep.agent/attempt']
        self.assertEqual(name, f'dev-{TAG}-arm64-{attempt[4:10]}')
        self.assertEqual(job['metadata']['namespace'], PREMADE)
        self.assertEqual(job['metadata']['labels'][LEASE_LABEL], lid)
        self.assertEqual(job['spec']['ttlSecondsAfterFinished'], 3600)
        self.assertEqual(pod_of(job)['imagePullSecrets'], [{'name': 'nvcr-pull'}])
        before = len(self.writes())
        error = self.refused('dev_exists', *self.dev('run', *dev_argv()))['error']
        self.assertIn(f'dev show {name}', error)
        self.assertIn(f'delete job/{name}', error)
        self.assertEqual(len(self.writes()), before)
        self.k8s['objects'][PREMADE]['Job/' + name]['status'] = {
            'conditions': [{'type': 'Failed', 'status': 'True'}]}
        self.flush()
        def fix_attempt(hex_digits):
            self.runner.write_text(RUNNER.replace('runpy.run_path(', 'import secrets\n'
                                                  f'secrets.token_hex = lambda _: {hex_digits!r}\nrunpy.run_path(', 1))

        fix_attempt(attempt.removeprefix('att_'))
        collision = self.refused('job_exists', *self.dev('run', *dev_argv()))['error']
        self.assertIn(f'dev show {name}', collision)
        self.assertIn(f'delete job/{name}', collision)
        self.assertEqual(len(self.writes()), before)
        fix_attempt('f' * 20 if attempt[4:10] != 'ffffff' else '0' * 20)
        self.assertNotEqual(self.launched()['metadata']['name'], name)

    def test_server_defaults_and_ssh_clone(self):
        self.claimed()
        self.pull_secret()
        self.ok('--context', CTX, '-n', PREMADE, 'secret', 'git', '--repo', 'owner/repo')
        self.k8s['mutate'] = {'Job': {'spec': {
            'suspend': False, 'parallelism': 1, 'completions': 1, 'completionMode': 'NonIndexed',
            'manualSelector': False, 'podReplacementPolicy': 'TerminatingOrFailed',
            'selector': {'matchLabels': {'batch.kubernetes.io/controller-uid': 'dry-run-uid'}},
            'template': {'metadata': {'creationTimestamp': None, 'labels': {'controller-uid': 'dry-run-uid',
                                                                            'batch.kubernetes.io/controller-uid':
                                                                                'dry-run-uid'}},
                         'spec': {'schedulerName': 'default-scheduler', 'dnsPolicy': 'ClusterFirst'}},
        }}}
        self.flush()
        pod = pod_of(self.launched('git@github.com:owner/repo.git'))
        clone, = pod['initContainers']
        main, = pod['containers']
        self.assertIn('git clone "$REPO" /work/src', clone['command'][-1])
        self.assertIn('git-ssh', [m['name'] for m in clone['volumeMounts']])
        self.assertEqual([m['name'] for m in main['volumeMounts']], ['work', 'tmp'])
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertEqual(main['command'], ['/usr/bin/tini', '--', 'sleep', 'infinity'])

    def test_short_lease_is_refused(self):
        lid = self.claimed()
        self.pull_secret()
        self.bb['leases'][lid]['expires_at'] = iso_ms(hours_from_now(0.1))
        self.flush()
        self.refused('lease_too_short', *self.dev('run', *dev_argv()))

    def test_job_dry_run_refuses_credential_and_lifecycle_mutations(self):
        self.claimed()
        self.pull_secret()
        job = self.launched()
        del self.k8s['objects'][PREMADE]['Job/' + job['metadata']['name']]
        pod = pod_of(job)
        before = len(self.writes())
        changes = [
            {'suspend': True},
            {'parallelism': 2},
            {'template': {'metadata': {'labels': {LEASE_LABEL: 'lse_wrong'}}}},
            {'template': {'metadata': {'labels': {'wcep.agent/build': 'run'}}}},
            {'template': {'spec': {'nodeName': 'wrong-node'}}},
            {'template': {'spec': {'imagePullSecrets': [{'name': 'nvcr-push'}]}}},
            {'template': {'spec': {'containers': pod['containers'] + [{'name': 'extra'}]}}},
            {'template': {'spec': {'volumes': pod['volumes'] + [
                {'name': 'push', 'secret': {'secretName': 'nvcr-push'}}]}}},
            {'template': {'spec': {'automountServiceAccountToken': True}}},
            {'template': {'spec': {'volumes': pod['volumes'] + [
                {'name': 'token', 'projected': {'sources': [{'serviceAccountToken': {'path': 'token'}}]}}]}}},
        ]
        for extra in [
            {'envFrom': [{'secretRef': {'name': 'nvcr-push'}}]},
            {'env': [{'name': 'X', 'valueFrom': {'secretKeyRef': {'name': 'git', 'key': 'id_ed25519'}}}]},
            {'args': ['bash']},
            {'command': ['sleep', 'infinity']},
            {'image': 'nvcr.io/goirlvsxnepa/llm_nim/gdl-dev:latest'},
            {'image': f'registry.example.test/dev@{DEV_DIGEST}'},
            {'resources': {'limits': {'nvidia.com/gpu': 1}}},
            {'resources': {'requests': {'cpu': '8'}, 'limits': {'cpu': '16'}}},
            {'securityContext': {'allowPrivilegeEscalation': True}},
            {'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'add': ['SYS_ADMIN']}}},
        ]:
            changes.append({'template': {'spec': {'containers': [dict(pod['containers'][0], **extra)]}}})
        for change in changes:
            with self.subTest(change=change):
                self.k8s['mutate'] = {'Job': {'spec': change}}
                self.flush()
                self.refused('field_refused', *self.dev('run', *dev_argv()))
                self.assertEqual(len(self.writes()), before)

    def test_show_reports_state_digest_and_its_own_pod(self):
        lid = self.claimed()
        self.pull_secret()
        job = self.launched()
        name = job['metadata']['name']
        status = self.ok(*self.dev('show', name))
        self.assertEqual((status['state'], status['digest']), ('pending', DEV_DIGEST))
        self.k8s['objects'][PREMADE]['Job/' + name]['status'] = {
            'active': 1, 'startTime': iso_ms(hours_from_now(-0.1))}
        for uid in ('wrong-job', job['metadata']['uid']):
            self.add_pod({'metadata': {'name': f'{name}-{uid}', 'labels': {LEASE_LABEL: lid, 'job-name': name},
                                       'ownerReferences': [{'kind': 'Job', 'controller': True, 'uid': uid}]},
                          'spec': {'nodeName': uid}, 'status': {'phase': 'Running',
                              'containerStatuses': [{'name': 'dev', 'restartCount': 0}]}}, ns=PREMADE)
        before = len(self.writes())
        status = self.ok(*self.dev('show', name))
        self.assertEqual((status['state'], status['pod_phase'], status['node'], status['restart_count']),
                         ('running', 'Running', job['metadata']['uid'], 0))
        self.assertLess(status['deadline_left_seconds'], job['spec']['activeDeadlineSeconds'])
        self.assertEqual(len(self.writes()), before)

    def test_show_refuses_a_missing_or_non_dev_job(self):
        self.refused('not_found', *self.dev('show', 'missing'))
        self.add_obj({'apiVersion': 'batch/v1', 'kind': 'Job',
                      'metadata': {'name': 'a-build', 'labels': {'wcep.agent/build': 'run'}}}, ns=PREMADE)
        self.refused('not_dev_job', *self.dev('show', 'a-build'))

    def test_claimed_teardown_removes_the_dev_job_and_its_pod(self):
        lid = self.claimed()
        self.pull_secret()
        job = self.launched()
        name = job['metadata']['name']
        self.add_pod({'metadata': {'name': f'{name}-pod', 'labels': {LEASE_LABEL: lid, 'job-name': name}},
                      'spec': {'containers': [container(0)]}, 'status': {'phase': 'Running'}}, ns=PREMADE)
        self.bb['leases'][lid]['state'] = 'releasing'
        self.flush()
        rc, body, _ = self.teardown('down', lid)
        self.assertEqual(rc, 0, body)
        self.assertTrue(body['complete'], body)
        self.assertIn('Job/' + name, body['deleted'])
        self.assertEqual(self.k8s['pods'][PREMADE], [])
        [delete] = [d for d in self.k8s['raw_delete_bodies'] if '/jobs/' in d['path']]
        self.assertEqual(delete['body']['propagationPolicy'], 'Background')


class FenceProbe(FenceCase):
    EXPECT = {  # state: (canary, mode, scope, armed)
        'armed': ('finegrained', 'enforcing', 'finegrained', True),
        'audit': ('finegrained', 'audit', 'finegrained', False),
        'disarmed': ('disarmed', 'absent', 'unknown', False),
        'shared': ('shared', 'enforcing', 'shared', False),
        'main_shared': ('finegrained', 'enforcing', 'shared', False),
        'engine_unavailable': ('engine_unavailable', 'engine_unavailable', 'finegrained', True),
        'main_absent': ('finegrained', 'absent', 'finegrained', False),
        'mutate_down': ('probe_failed', 'probe_failed', 'unknown', False),
        'one_entry_down': ('engine_unavailable', 'engine_unavailable', 'finegrained', True),
        'canary_absent': ('disarmed', 'enforcing', 'finegrained', False),
        'other_webhook': ('probe_failed', 'probe_failed', 'unknown', False),
        'other_rule': ('probe_failed', 'enforcing', 'finegrained', False),
        'forbidden': ('probe_failed', 'probe_failed', 'unknown', False),
    }

    def test_each_server_answer_maps_to_its_verdict(self):
        for state, (canary, mode, scope, armed) in self.EXPECT.items():
            with self.subTest(state=state):
                self.k8s['fence'] = state
                self.flush()
                body = self.ok('--context', CTX, 'fence', 'probe', PREMADE, host_entry=True)
                self.assertEqual((body['canary'], body['mode'], body['scope'], body['armed']),
                                 (canary, mode, scope, armed), body)
                self.assertEqual(body['lease_fence'], 'true')
                self.assertEqual('cause' in body, not armed, body)

    def test_a_removed_policy_is_never_armed_and_says_why(self):
        # The steward's negative case: with the policy (or its entry) gone, both probe Pods are admitted. That
        # must read as a fence state with its causes, the 0-replica controller among them, never as armed.
        self.k8s['fence'] = 'disarmed'
        self.flush()
        body = self.ok('--context', CTX, 'fence', 'probe', PREMADE, host_entry=True)
        self.assertFalse(body['armed'])
        self.assertIn('scaled to 0 replicas', body['cause'])
        self.assertIn('policies or the namespace label are gone', body['cause'])

    def test_another_kyverno_webhook_failing_first_names_itself_in_the_cause(self):
        # T3 F3: on a cluster whose mutating Kyverno webhook covers every Pod create, an outage reads not armed,
        # and the cause says ours were never reached, not that the probe proved nothing.
        self.k8s['fence'] = 'mutate_down'
        self.flush()
        body = self.ok('--context', CTX, 'fence', 'probe', PREMADE, host_entry=True)
        self.assertFalse(body['armed'])
        self.assertIn('mutate.kyverno.svc-fail', body['cause'])
        self.assertIn('ours were not reached', body['cause'])

    def test_the_probe_pods_are_dry_runs_that_request_no_gpu(self):
        self.ok('--context', CTX, 'fence', 'probe', PREMADE, host_entry=True)
        creates = [c for c in self.kubectl_calls() if c['pos'][:1] == ['create']]
        self.assertEqual(len(creates), 2)
        for c in creates:
            self.assertEqual(c['flags'].get('--dry-run'), 'server')
        self.assertEqual(self.writes(), [])
        bodies = {b['metadata']['labels']['wcep.agent/fence-probe']: b for b in self.k8s['probe_bodies']}
        self.assertEqual(sorted(bodies), ['canary', 'mode'])  # sent at once, so in either order
        self.assertEqual(bodies['mode']['metadata']['labels'][LEASE_LABEL], 'lse_fenceprobe0')
        self.assertNotIn('nvidia.com/gpu', json.dumps(bodies))

    def test_the_two_probe_pods_are_sent_at_once(self):
        # One probe costs one call's time, not two: the canary's and the mode Pod's dry runs overlap.
        self.k8s['probe_sleep'] = 0.4
        self.flush()
        self.ok('--context', CTX, 'fence', 'probe', PREMADE, host_entry=True)
        spans = {w: (a, b) for w, a, b in self.k8s['probe_times']}
        (c0, c1), (m0, m1) = spans['canary'], spans['mode']
        self.assertLess(max(c0, m0), min(c1, m1), f'the two dry runs did not overlap: {spans}')

    def test_each_probe_is_counted_in_the_host_ledger(self):
        self.ok('--context', CTX, 'fence', 'probe', PREMADE, host_entry=True)
        [line] = [l for l in self.ledger(host=True) if l['object'] == f'FenceProbe/{PREMADE}']
        self.assertEqual((line['direction'], line['canary'], line['mode'], line['armed']),
                         ('probe', 'finegrained', 'enforcing', True))

    def test_inspect_reports_the_fence_label(self):
        body = self.ok('--context', CTX, 'inspect', PREMADE, host_entry=True)
        self.assertEqual(body['lease_fence'], 'true')


class FenceGate(FenceCase):
    def test_an_unenrolled_claim_refuses_without_asking_the_plugin(self):
        del self.k8s['namespaces'][PREMADE]['metadata']['labels'][FENCE_LABEL]
        self.flush()
        rc, body, _ = self.claim()
        self.assertEqual(body.get('code'), 'namespace_not_enrolled', body)
        self.assertEqual(self.bb.get('claim_calls', []), [])
        self.assertEqual(self.writes(), [])

    def test_a_claim_opens_the_gate_once_and_a_replay_changes_nothing(self):
        rc, body, _ = self.claim()
        self.assertEqual(rc, 0, body)
        lid = body['lease_id']
        self.assertEqual(self.gate(), (lid, 'active', '1'))
        self.assertEqual((body['seq'], body['fence']['state']), (1, 'active'), body)
        [write] = self.writes()
        self.assertEqual((write['pos'][:1], write['flags'].get('-f')), (['create'], '-'), 'the first open is a create-only POST')
        rc, again, _ = self.claim()
        self.assertEqual((rc, again['lease_id'], again['seq']), (0, lid, 1), again)
        self.assertEqual(len(self.writes()), 1, 'a replay over an open gate writes nothing')

    def test_a_replay_that_returns_released_writes_no_gate(self):
        self.bb['lose_claim_reply'] = True
        self.flush()
        rc, body, _ = self.claim()
        self.assertEqual(body.get('code'), 'lease_unavailable', body)
        [lid] = self.bb['leases']
        self.bb['leases'][lid]['state'] = 'released'
        self.flush()
        rc, body, _ = self.claim()
        self.assertEqual((body['lease_id'], body['state']), (lid, 'released'), body)
        self.assertIsNone(self.gate())
        self.assertEqual(self.writes(), [])

    def test_a_gate_teardown_closed_is_never_reopened(self):
        lid = self.claimed()
        self.set_gate(lid, 'closing', 1)
        rc, body, _ = self.claim()
        self.assertEqual(rc, 0, body)
        self.assertEqual(body['fence']['state'], 'closing', body)
        self.assertEqual(self.gate(), (lid, 'closing', '1'))
        self.assertEqual([w for w in self.writes() if w['pos'][:1] == ['patch']], [])
        rc, body, _ = self.apply(dgd('cpu', component('Frontend', ctype='frontend', lease_id=lid)), ns=PREMADE)
        self.assertEqual(body.get('code'), 'fence_not_open', body)

    def test_a_gate_above_this_claims_seq_refuses_fence_mismatch(self):
        self.set_gate('lse_later0001', 'active', 5)
        rc, body, _ = self.claim()
        self.assertEqual(body.get('code'), 'fence_mismatch', body)
        self.assertEqual(self.gate(), ('lse_later0001', 'active', '5'))

    def test_a_new_claim_moves_the_gate_up_over_an_older_lease(self):
        self.k8s['objects'][PREMADE]  # an earlier claim's gate, closed
        self.set_gate('lse_earlier001', 'closing', 1)
        self.bb['seq_floor'] = 1
        self.flush()
        rc, body, _ = self.claim()
        self.assertEqual(rc, 0, body)
        self.assertEqual(self.gate(), (body['lease_id'], 'active', '2'))
        [patch] = [w for w in self.writes() if w['pos'][:1] == ['patch']]
        tests = [op for op in json.loads(patch['flags']['-p']) if op['op'] == 'test']
        self.assertEqual({t['path'] for t in tests},
                         {'/metadata/uid', '/metadata/resourceVersion', '/data/seq', '/data/lease_id', '/data/state'})

    def test_a_kill_after_commit_before_the_gate_then_teardown_completes(self):
        for before in (None, ('lse_earlier001', 'active', 0)):
            with self.subTest(gate_before=before):
                self.setUp()
                if before:
                    self.set_gate(*before)
                # The claim commits, but its reply is an error, so no gate is written in that run.
                self.bb['claim_error_after_commit'] = True
                self.flush()
                rc, body, _ = self.claim()
                self.assertEqual(body.get('code'), 'internal', body)
                [lid] = [l for l in self.bb['leases'] if l != 'lse_earlier001']
                self.bb['leases'][lid]['state'] = 'releasing'
                self.flush()
                body = self.ok('--context', CTX, 'down', PREMADE, '--uid', 'uid-premade-1', '--lease', lid,
                               host_entry=True)
                self.assertTrue(body['complete'], body)
                self.assertEqual(body['fence'], {'lease_id': lid, 'seq': 1, 'state': 'closing'})
                self.assertEqual(self.gate(), (lid, 'closing', '1'))
                # The late open, a replay of the pending request, now meets an ended lease: it writes nothing.
                self.bb['leases'][lid]['state'] = 'released'
                self.flush()
                rc, body, _ = self.claim()
                self.assertEqual(self.gate(), (lid, 'closing', '1'))

    def test_claimed_writes_refuse_until_the_gate_is_open(self):
        lid = self.claimed()
        del self.k8s['objects'][PREMADE][GATE]
        self.flush()
        rc, body, _ = self.apply(dgd('cpu', component('Frontend', ctype='frontend', lease_id=lid)), ns=PREMADE)
        self.assertEqual(body.get('code'), 'fence_not_open', body)
        self.assertNotIn(['k8s', 'lease', 'op', 'begin', lid], self.bb_calls())
        rc, body, _ = self.claim()
        self.assertEqual(rc, 0, body)
        rc, body, _ = self.apply(dgd('cpu', component('Frontend', ctype='frontend', lease_id=lid)), ns=PREMADE)
        self.assertEqual(rc, 0, body)

    def test_a_manifest_carrying_the_probe_label_is_refused(self):
        # T3 F2: the main Pod rule excludes the canary-labelled Pod, so a workload carrying that label would
        # skip the fence. Only the probe may carry it, and the probe is a dry run.
        lid = self.claimed()
        for value in ('canary', 'mode'):
            with self.subTest(value=value):
                c = component('Frontend', ctype='frontend', lease_id=lid)
                c['podTemplate']['metadata']['labels']['wcep.agent/fence-probe'] = value
                rc, body, _ = self.apply(dgd('sneaky', c), ns=PREMADE)
                self.assertEqual(body.get('code'), 'fence_probe_label_refused', body)
        cm = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'x', 'labels': {'wcep.agent/fence-probe': 'canary'}}}
        rc, body, _ = self.apply(cm, ns=PREMADE)
        self.assertEqual(body.get('code'), 'fence_probe_label_refused', body)

    def test_the_gate_is_never_applied_or_deleted(self):
        lid = self.claimed()
        rc, body, _ = self.apply({'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'wcep-agent-fence'},
                                  'data': {'lease_id': lid, 'state': 'active', 'seq': '9'}}, ns=PREMADE)
        self.assertEqual(body.get('code'), 'fence_protected', body)
        body = self.refused('fence_protected', '--context', CTX, '-n', PREMADE, 'delete', 'cm/wcep-agent-fence')
        self.assertEqual(self.gate(), (lid, 'active', '1'))

    def test_teardown_leaves_the_gate_in_place_closing(self):
        lid = self.claimed(state='releasing')
        body = self.ok('--context', CTX, 'down', PREMADE, '--uid', 'uid-premade-1', '--lease', lid, host_entry=True)
        self.assertTrue(body['complete'], body)
        self.assertEqual(self.gate(), (lid, 'closing', '1'))
        self.assertNotIn('wcep-agent-fence', json.dumps(body['deleted']))


class FenceArmed(FenceCase):
    def test_a_complete_teardown_reports_the_fence_the_drain_and_armed(self):
        lid = self.claimed(state='releasing')
        body = self.ok('--context', CTX, 'down', PREMADE, '--uid', 'uid-premade-1', '--lease', lid, host_entry=True)
        self.assertEqual((body['complete'], body['armed'], body['lease_fence']), (True, 'continuous', 'true'), body)
        self.assertEqual(body['fence'], {'lease_id': lid, 'seq': 1, 'state': 'closing'})
        self.assertIn('drain_monotonic_seconds', body)
        self.assertGreaterEqual(body['probes'], 2, 'before the close and after the final read')

    def test_a_probe_that_is_not_armed_keeps_the_charge(self):
        for state in ('disarmed', 'audit', 'shared', 'main_shared', 'main_absent', 'canary_absent', 'other_webhook'):
            for when in ('before the close', 'after the final read'):
                with self.subTest(state=state, when=when):
                    self.setUp()
                    lid = self.claimed(state='releasing')
                    self.k8s['fence_seq'] = [state] if when == 'before the close' else ['armed', state]
                    self.flush()
                    rc, body, _ = self.teardown('down', lid)
                    self.assertEqual(body.get('code'), 'teardown_incomplete', body)
                    self.assertTrue(body.get('fence_disarmed'), body)
                    self.assertEqual(body['probe']['canary'], FenceProbe.EXPECT[state][0], body)
                    self.assertIn('cause', body['probe'])

    def test_a_removed_policy_never_completes_however_often_teardown_runs(self):
        lid = self.claimed(state='releasing')
        self.k8s['fence'] = 'disarmed'
        self.flush()
        for _ in range(3):
            rc, body, _ = self.teardown('down', lid)
            self.assertEqual(body.get('code'), 'teardown_incomplete', body)
            self.assertNotIn('complete', body)
        self.assertEqual(self.gate(), (lid, 'closing', '1'), 'the gate is closed anyway')

    def test_the_drain_waits_120_monotonic_seconds_and_probes_every_5(self):
        lid = self.claimed(state='releasing')
        rc, body = self.clocked_down(lid)
        self.assertEqual(rc, 0, body)
        self.assertEqual(body['drain_monotonic_seconds'], 120)
        self.assertEqual(body['probes'], 26, 'before the close, every 5 s through 120 s, after the final read')
        self.assertTrue(all(s <= 5 for s in self.k8s['clock_sleeps']), self.k8s['clock_sleeps'])
        # The two Pods of a probe go at once, so they arrive in either order.
        self.assertEqual(sorted(self.probes_sent()), sorted(['canary', 'mode'] * 26))

    def test_a_slow_delete_phase_with_no_probe_in_it_keeps_the_charge(self):
        # T3 F1: the deletes after the close take 130 s, longer than the drain. Before the fix the run reported
        # complete, armed: continuous, with 2 probes and none in the drain. A gap between probes longer than the
        # interval plus its slack is a span nobody watched: not armed.
        lid = self.claimed(state='releasing')
        rc, body = self.clocked_down(lid, {'FENCE_TEST_SLOW_AFTER_CLOSE': '130'})
        self.assertEqual(body.get('code'), 'teardown_incomplete', body)
        self.assertTrue(body.get('fence_disarmed'), body)
        self.assertGreaterEqual(body['max_probe_gap_seconds'], 130)
        self.assertIn('apart', body['probe']['cause'])

    def test_slow_inventory_reads_are_probed_through(self):
        # Each of the 11 inventory lists takes 3 s: the guard probes between them whenever one is due, so the
        # gap stays within the interval plus slack and the run completes.
        lid = self.claimed(state='releasing')
        rc, body = self.clocked_down(lid, {'FENCE_TEST_SLOW_LIST': '3'})
        self.assertEqual(rc, 0, body)
        self.assertLessEqual(body['max_probe_gap_seconds'], 8)
        self.assertGreater(body['probes'], 26, 'probes between the slow lists, too')

    def test_one_inventory_read_slower_than_the_gap_limit_keeps_the_charge(self):
        lid = self.claimed(state='releasing')
        rc, body = self.clocked_down(lid, {'FENCE_TEST_SLOW_LIST': '20'})
        self.assertEqual(body.get('code'), 'teardown_incomplete', body)
        self.assertTrue(body.get('fence_disarmed'), body)
        self.assertGreater(body['max_probe_gap_seconds'], 8)

    def leased_with_an_open_attempt(self):
        """A releasing claim with objects to delete and one open attempt, whose create landed with its stamp."""
        lid = self.claimed()
        for name in ('w', 'v'):
            d = gpu_dgd(name, 1, lid)
            d['metadata'].setdefault('annotations', {})['wcep.agent/attempt'] = f'att_{name}0123456789'
            self.add_obj(d, ns=PREMADE)
        self.bb['leases'][lid]['attempts'] = [{'attempt': 'att_w0123456789', 'verb': 'create',
                                               'kind': 'DynamoGraphDeployment', 'name': 'w', 'uid': None, 'rv': None,
                                               'outcome': None}]
        self.bb['leases'][lid]['state'] = 'releasing'
        self.flush()
        return lid

    def test_teardown_completes_at_the_stated_latency_of_2_5_s_per_call(self):
        # T3 N1: the gap bound must leave a teardown able to complete at a real per-call latency. The design
        # tolerates 2.5 s per kubectl call: a probe's two Pods go at once, and the guard probes after any call
        # once 2 s passed since the last probe began (5 s in the drain, where nothing else runs).
        lid = self.leased_with_an_open_attempt()
        rc, body = self.clocked_down(lid, {'FENCE_TEST_PER_CALL': '2.5'})
        self.assertEqual(rc, 0, body)
        self.assertTrue(body['complete'])
        self.assertLessEqual(body['max_probe_gap_seconds'], 8, body)
        self.assertEqual(body['resolutions'], [{'attempt': 'att_w0123456789', 'outcome': 'committed'}])
        self.assertEqual(sorted(body['deleted']), ['DynamoGraphDeployment/v', 'DynamoGraphDeployment/w'])

    def test_beyond_the_stated_latency_the_charge_stays(self):
        # At 3 s a call, a probe, a call and a probe already pass 8 s: the bound holds and the run says so.
        lid = self.leased_with_an_open_attempt()
        rc, body = self.clocked_down(lid, {'FENCE_TEST_PER_CALL': '3'})
        self.assertEqual(body.get('code'), 'teardown_incomplete', body)
        self.assertTrue(body['fence_disarmed'])
        self.assertGreater(body['max_probe_gap_seconds'], 8)

    def test_a_gap_failure_carries_the_texts_the_plugin_requires(self):
        # T3 N2: the plugin drops a probe record without canary_text and mode_text, and with it the cause.
        lid = self.claimed(state='releasing')
        rc, body = self.clocked_down(lid, {'FENCE_TEST_SLOW_AFTER_CLOSE': '130'})
        for key in ('canary', 'mode', 'scope', 'armed', 'cause', 'canary_text', 'mode_text'):
            self.assertIn(key, body['probe'])
        self.assertIn('apart', body['probe']['cause'])

    def test_the_gap_is_reported_and_bounded_on_a_normal_run(self):
        lid = self.claimed(state='releasing')
        rc, body = self.clocked_down(lid)
        self.assertEqual(rc, 0, body)
        self.assertLessEqual(body['max_probe_gap_seconds'], 5)

    def test_a_wall_clock_step_does_not_end_the_drain(self):
        # The runner moves wall time a day ahead; only monotonic time, moved by sleep, may end the drain.
        lid = self.claimed(state='releasing')
        rc, body = self.clocked_down(lid)
        self.assertEqual(self.k8s['clock_elapsed'], 120, body)

    def test_a_disarmed_probe_in_the_drain_keeps_the_charge(self):
        lid = self.claimed(state='releasing')
        self.k8s['fence_seq'] = ['armed'] * 5 + ['disarmed']
        self.flush()
        rc, body = self.clocked_down(lid)
        self.assertEqual(body.get('code'), 'teardown_incomplete', body)
        self.assertTrue(body['fence_disarmed'])
        self.assertLess(self.k8s['clock_elapsed'], 120, 'a disarmed probe ends the wait: it cannot complete anyway')

    def test_an_open_attempt_keeps_teardown_incomplete_long_after_the_drain(self):
        lid = self.claimed()
        self.bb['leases'][lid]['attempts'] = [{'attempt': 'att_0123456789ab', 'verb': 'create', 'kind': 'DynamoGraphDeployment',
                                               'name': 'w', 'uid': None, 'rv': None, 'outcome': None}]
        self.bb['leases'][lid]['state'] = 'releasing'
        self.flush()
        rc, body = self.clocked_down(lid)
        self.assertEqual(body.get('code'), 'teardown_incomplete', body)
        self.assertGreaterEqual(self.k8s['clock_elapsed'], 120)
        self.assertEqual(len(body['unresolved']), 1)

    def test_a_rerun_waits_the_drain_again_from_its_own_close(self):
        lid = self.claimed()
        self.bb['leases'][lid]['attempts'] = [{'attempt': 'att_0123456789ab', 'verb': 'create', 'kind': 'DynamoGraphDeployment',
                                               'name': 'w', 'uid': None, 'rv': None, 'outcome': None}]
        self.bb['leases'][lid]['state'] = 'releasing'
        self.flush()
        self.clocked_down(lid)
        self.bb['leases'][lid]['attempts'] = []
        self.flush()
        rc, body = self.clocked_down(lid)
        self.assertEqual(rc, 0, body)
        self.assertEqual((body['drain_monotonic_seconds'], self.k8s['clock_elapsed']), (120, 120))

    def test_the_drain_env_is_for_tests_and_the_default_is_120(self):
        lid = self.claimed(state='releasing')
        rc, body = self.clocked_down(lid, {'K8S_GUARD_FENCE_DRAIN_SECONDS': '10', 'K8S_GUARD_FENCE_PROBE_SECONDS': '5'})
        self.assertEqual((rc, body['drain_monotonic_seconds'], body['probes']), (0, 10, 4), body)

    def test_a_retired_fence_needs_no_label_and_a_disarmed_canary(self):
        lid = self.claimed(state='releasing')
        before = len(self.writes())
        url = 'https://example.test/proof/1'
        body = self.refused('fence_not_retired', '--context', CTX, 'down', PREMADE, '--uid', 'uid-premade-1', '--lease',
                            lid, '--fence-retired', url, host_entry=True)
        self.assertIn('label', body['error'])
        del self.k8s['namespaces'][PREMADE]['metadata']['labels'][FENCE_LABEL]
        self.flush()
        body = self.refused('fence_not_retired', '--context', CTX, 'down', PREMADE, '--uid', 'uid-premade-1', '--lease',
                            lid, '--fence-retired', url, host_entry=True)
        self.assertIn('canary', body['error'])
        self.assertEqual(len(self.writes()), before, 'a refused retire writes nothing')

    def test_a_retired_fence_completes_and_records_the_proof(self):
        lid = self.claimed(state='releasing')
        del self.k8s['namespaces'][PREMADE]['metadata']['labels'][FENCE_LABEL]
        self.k8s['fence'] = 'disarmed'
        self.flush()
        url = 'https://example.test/proof/1'
        body = self.ok('--context', CTX, 'down', PREMADE, '--uid', 'uid-premade-1', '--lease', lid, '--fence-retired',
                       url, host_entry=True)
        self.assertEqual((body['complete'], body['armed'], body['lease_fence']), (True, 'retired', None), body)
        self.assertEqual(body['fence_retired_proof'], url)
        self.assertEqual(self.bb['leases'][lid]['fence_retired_proof'], url)
        self.assertIn(['k8s', 'lease', 'fence-retired', lid, '--proof', url], self.bb_calls())


# A v1.15.2 PolicyReport and a v1.15.2 Event, in the shapes of Kyverno's own test data at v1.15.2:
# test/conformance/chainsaw/webhook-configurations/match-conditions-standard/report-assert.yaml (the report)
# and test/conformance/chainsaw/events/policy/policy-violation/event-assert.yaml (the event), with the
# resource-violation message pkg/event/events.go NewResourceViolationEvent builds and the kind fixture saw.
REPORT = {'apiVersion': 'wgpolicyk8s.io/v1alpha2', 'kind': 'PolicyReport',
          'metadata': {'namespace': PREMADE, 'name': 'uid-pod-1',
                       'ownerReferences': [{'apiVersion': 'v1', 'kind': 'Pod', 'name': 'someones-pod'}]},
          'scope': {'apiVersion': 'v1', 'kind': 'Pod', 'name': 'someones-pod', 'namespace': PREMADE, 'uid': 'uid-pod-1'},
          'summary': {'error': 0, 'fail': 1, 'pass': 0, 'skip': 0, 'warn': 0},
          'results': [{'policy': 'agent-lease-fence', 'rule': 'pods-need-the-open-lease', 'result': 'fail',
                       'message': 'agent-lease-fence (owner alice ...): Gate: missing', 'source': 'kyverno',
                       'scored': True, 'timestamp': {'nanos': 0, 'seconds': 1795400000}}]}


def event(name, uid, message, count=1, last='2026-11-22T21:00:00Z'):
    return {'apiVersion': 'v1', 'kind': 'Event', 'metadata': {'name': f'{name}.18', 'uid': f'ev-{name}-{count}'},
            'involvedObject': {'apiVersion': 'v1', 'kind': 'Pod', 'name': name, 'namespace': PREMADE, 'uid': uid},
            'type': 'Warning', 'reason': 'PolicyViolation', 'reportingComponent': 'kyverno-admission',
            'message': message, 'count': count, 'firstTimestamp': '2026-11-22T20:59:00Z', 'lastTimestamp': last}


VIOLATION = 'policy agent-lease-fence/pods-need-the-open-lease fail: agent-lease-fence (owner alice ...): Gate: missing'


class FenceRecord(FenceCase):
    def record(self):
        return self.ok('--context', CTX, 'fence', 'record', host_entry=True)  # the pre-made namespace by default

    def rows(self):
        path = self.state_home / f'k8s-fence/{PROFILE}.jsonl'
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def test_the_real_v1_15_2_shapes_give_the_would_be_denial_and_no_probe(self):
        fixtures = ROOT / 'test/fixtures/k8s-fence'
        self.k8s['policyreports.wgpolicyk8s.io'] = {PREMADE: json.loads((fixtures / 'policyreports.json').read_text())['items']}
        self.k8s['events'] = {PREMADE: json.loads((fixtures / 'events.json').read_text())['items']}
        self.flush()
        body = self.record()
        rows = self.rows()
        self.assertEqual(sorted((r['source'], r['name']) for r in rows),
                         [('event', 'audit-would-deny'), ('event', 'audit-would-deny'), ('report', 'audit-would-deny')])
        self.assertEqual(body['new_rows'], 3)
        self.assertEqual(self.record()['new_rows'], 0, 'a second run appends nothing')

    def test_rows_come_from_the_report_and_the_event_shapes_only(self):
        self.k8s['policyreports.wgpolicyk8s.io'] = {PREMADE: [REPORT]}
        self.k8s['events'] = {PREMADE: [event('someones-pod', 'uid-pod-1', VIOLATION)]}
        self.flush()
        body = self.record()
        self.assertEqual(body['new_rows'], 2, body)
        report_row, event_row = self.rows()
        self.assertEqual((report_row['source'], report_row['name'], report_row['rule'], report_row['result']),
                         ('report', 'someones-pod', 'pods-need-the-open-lease', 'fail'))
        self.assertEqual((event_row['source'], event_row['name'], event_row['reason']),
                         ('event', 'someones-pod', 'PolicyViolation'))
        for row in self.rows():
            self.assertNotIn('user', row)
            self.assertNotIn('operation', row)

    def test_rows_are_appended_once_across_runs(self):
        self.k8s['events'] = {PREMADE: [event('someones-pod', 'uid-pod-1', VIOLATION)]}
        self.flush()
        self.record()
        self.assertEqual(self.record()['new_rows'], 0)
        self.k8s['events'] = {PREMADE: [event('someones-pod', 'uid-pod-1', VIOLATION, count=2,
                                              last='2026-11-22T21:10:00Z')]}
        self.flush()
        self.assertEqual(self.record()['new_rows'], 1, 'the same Event seen again with a new count is new')
        self.assertEqual(len(self.rows()), 2)

    def test_probe_rows_are_left_out(self):
        self.k8s['events'] = {PREMADE: [
            event('wcep-fence-probe-mode', 'uid-x', VIOLATION),
            event('wcep-fence-probe-canary', 'uid-y', 'policy agent-lease-fence-canary/probe-canary fail: ...'),
            event('someones-pod', 'uid-pod-1', VIOLATION)]}
        self.flush()
        self.record()
        self.assertEqual([r['name'] for r in self.rows()], ['someones-pod'])

    def test_since_counts_only_the_probes_after_the_counter_started(self):
        # Kyverno's counter lives in memory: an admission pod restart zeroes it, so the tally counts the probes
        # sent since that pod started.
        ledger = self.state_home / 'k8s-guard/ledger.jsonl'
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps({'time': '2026-01-01T00:00:00Z', 'cluster': PROFILE, 'namespace': PREMADE,
                                      'object': f'FenceProbe/{PREMADE}', 'direction': 'probe', 'canary': 'finegrained',
                                      'mode': 'enforcing', 'armed': True}) + '\n')
        self.probe()
        self.assertEqual(self.record()['probes_sent']['probe-canary'], 2)
        body = self.ok('--context', CTX, 'fence', 'record', '--since', '2026-06-01T00:00:00Z', host_entry=True)
        self.assertEqual(body['probes_sent'], {'probe-canary': 1, 'pods-need-the-open-lease': 1}, body)
        self.assertEqual(body['since'], '2026-06-01T00:00:00Z')
        self.refused('invalid_time', '--context', CTX, 'fence', 'record', '--since', 'yesterday', host_entry=True)

    def test_the_tally_counts_only_probes_that_reached_the_policy(self):
        # T3 F4: a probe during an outage, with the entries emptied, or refused for another reason never moves
        # Kyverno's counter; counting it would reset the window for nothing.
        self.probe()                                   # armed: both rules counted
        for state in ('engine_unavailable', 'disarmed', 'forbidden', 'audit', 'canary_absent'):
            self.k8s['fence'] = state
            self.flush()
            self.probe()
        body = self.record()
        # canary counted for armed and audit; the Pod rule for armed, audit and canary_absent (enforcing).
        self.assertEqual(body['probes_sent'], {'probe-canary': 2, 'pods-need-the-open-lease': 3}, body)

    def test_the_ledger_counts_the_probes_it_sent_by_rule(self):
        self.probe()
        self.probe()
        body = self.record()
        self.assertEqual(body['probes_sent'], {'probe-canary': 2, 'pods-need-the-open-lease': 2}, body)


class TrialClaim(FenceCase):
    def trial(self, gpus='1'):
        self.bb['claims'] = 'on'
        self.flush()
        return self.guard('--context', CTX, '-n', PREMADE, 'ns', 'claim', '--gpus', gpus, '--hours', '1',
                          '--audit-trial')

    def test_a_trial_asks_with_audit_trial_and_only_one_gpu(self):
        rc, body, _ = self.trial(gpus='2')
        self.assertEqual(body.get('code'), 'usage', body)
        rc, body, _ = self.trial()
        self.assertEqual((rc, body['trial']), (0, True), body)
        argv = next(c for c in self.bb_calls() if c[:4] == ['k8s', 'lease', 'acquire', '--claim'])
        self.assertIn('--audit-trial', argv)

    def test_a_gpu_manifest_under_a_trial_refuses(self):
        rc, body, _ = self.trial()
        lid = body['lease_id']
        rc, body, _ = self.apply(gpu_dgd('w', 1, lid), ns=PREMADE)
        self.assertEqual(body.get('code'), 'trial_gpu_refused', body)
        rc, body, _ = self.apply(dgd('mocker', component('Frontend', ctype='frontend', lease_id=lid)), ns=PREMADE)
        self.assertEqual(rc, 0, body)

    def test_a_build_under_a_trial_refuses(self):
        rc, body, _ = self.trial()
        body = self.refused('trial_build_refused', '--context', CTX, '-n', PREMADE, 'build', 'run', '--repo',
                            'https://github.com/example/repo', '--commit', 'a' * 40, '--dockerfile', 'Dockerfile',
                            '--image', 'img', '--arch', 'arm64')

    def test_the_trial_steps_refuse_outside_a_trial(self):
        lid = self.claimed()
        self.refused('trial_only', '--context', CTX, '-n', PREMADE, 'fence', 'trial', 'close')
        self.refused('trial_only', '--context', CTX, '-n', PREMADE, 'fence', 'trial', 'delete-pod', 'w-decode')
        self.assertEqual(self.gate(), (lid, 'active', '1'))

    def test_the_trial_closes_the_gate_and_deletes_only_its_own_pod(self):
        rc, body, _ = self.trial()
        lid = body['lease_id']
        mine = pod('mocker', 0, name='mocker-0')
        mine['metadata'].update(uid='uid-mocker-0', resourceVersion='41', labels={LEASE_LABEL: lid})
        other = pod('mocker', 0, name='other-0')
        other['metadata'].update(uid='uid-other-0', resourceVersion='42', labels={LEASE_LABEL: 'lse_someone9999'})
        self.add_pod(mine, ns=PREMADE)
        self.add_pod(other, ns=PREMADE)
        body = self.ok('--context', CTX, '-n', PREMADE, 'fence', 'trial', 'close')
        self.assertEqual(self.gate(), (lid, 'closing', '1'))
        self.refused('object_not_yours', '--context', CTX, '-n', PREMADE, 'fence', 'trial', 'delete-pod', 'other-0')
        body = self.ok('--context', CTX, '-n', PREMADE, 'fence', 'trial', 'delete-pod', 'mocker-0')
        self.assertEqual(self.k8s['deleted_pods'], ['mocker-0'])
        [a] = self.bb['leases'][lid]['attempts']
        self.assertEqual((a['verb'], a['kind'], a['name'], a['outcome']), ('delete', 'Pod', 'mocker-0', 'committed'))

    def test_a_trial_teardown_completes_under_audit_as_trial(self):
        rc, body, _ = self.trial()
        lid = body['lease_id']
        self.bb['leases'][lid]['state'] = 'releasing'
        self.k8s['fence'] = 'audit'
        self.flush()
        body = self.ok('--context', CTX, 'down', PREMADE, '--uid', 'uid-premade-1', '--lease', lid, host_entry=True)
        self.assertEqual((body['complete'], body['armed']), (True, 'trial'), body)

    def test_a_trial_teardown_with_the_policy_gone_keeps_the_charge(self):
        rc, body, _ = self.trial()
        lid = body['lease_id']
        self.bb['leases'][lid]['state'] = 'releasing'
        self.k8s['fence'] = 'disarmed'
        self.flush()
        rc, body, _ = self.teardown('down', lid)
        self.assertEqual(body.get('code'), 'teardown_incomplete', body)


class FenceRender(GuardCase):
    def test_render_fills_the_template_from_the_profiles_and_needs_no_cluster(self):
        self.profiles.write_text(self.profiles.read_text().replace(
            'namespace_prefix = "dyntest-alice-"', f'namespace_prefix = "dyntest-alice-"\npremade_namespace = "{PREMADE}"', 1))
        body = self.ok('--context', CTX, 'fence', 'render', '--owner', 'alice-example', '--contact', 'example-contact',
                       '--runbook', 'runbook section 10')
        self.assertIn("request.namespace in ['dyntest-alice-agent']", body['policy'])
        self.assertIn("name: 'bot-example-agent'", body['policy'])
        self.assertIn('name: agent-lease-fence-canary', body['policy'])
        self.assertEqual(self.kubectl_calls(), [], 'render talks to no cluster')


class FenceVerdictMutants(unittest.TestCase):
    """armed() needs both entries: the canary refused by its own policy's entry, and the mode Pod refused by the
    main policy's entry (the Pod rule excludes the canary-labelled Pod, so neither entry can stand in for the
    other). A verdict that drops either half must disagree with FenceProbe.EXPECT on the fakes' answers."""

    RULES = SKILL / 'scripts/k8s_fence_rules.py'
    MUTANTS = {
        'the canary half is dropped': ("return (verdict.get('canary') in ('finegrained', 'engine_unavailable')\n"
                                       "            and (", "return (True\n            and ("),
        'the main-entry half is dropped': ("or (verdict.get('mode') == 'enforcing' and verdict.get('mode_entry') == "
                                           "'finegrained'))", "or verdict.get('mode') in ('enforcing', 'absent', 'audit'))"),
    }

    def verdicts(self, source):
        module = {'__name__': 'k8s_fence_rules_mutant'}
        exec(compile(source, str(self.RULES), 'exec'), module)
        out = {}
        for state, answers in FENCE_ANSWERS.items():
            def answer(which):
                a = answers[which]
                if a is None or a.startswith('Warning:'):
                    return 0, a or ''
                return 1, a.replace('{ns}', PREMADE).replace('{name}', f'wcep-fence-probe-{which}')
            out[state] = module['fence_verdict'](*answer('canary'), *answer('mode'))['armed']
        return out

    def test_the_real_verdict_matches_the_table(self):
        got = self.verdicts(self.RULES.read_text())
        self.assertEqual(got, {k: v[3] for k, v in FenceProbe.EXPECT.items()})

    def test_each_half_of_armed_is_load_bearing(self):
        source = self.RULES.read_text()
        want = {k: v[3] for k, v in FenceProbe.EXPECT.items()}
        for what, (old, new) in self.MUTANTS.items():
            with self.subTest(mutant=what):
                self.assertEqual(source.count(old), 1, old)
                self.assertNotEqual(self.verdicts(source.replace(old, new)), want, f'{what}: no row noticed')


if __name__ == '__main__':
    unittest.main()

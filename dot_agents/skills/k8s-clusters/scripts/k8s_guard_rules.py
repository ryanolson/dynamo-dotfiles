"""The admission rules of k8s-guard: what a manifest may hold, and how many GPUs a namespace holds.

Pure functions over the JSON a server-side dry run returns and over listed pods. No I/O, so every rule
is checkable without a cluster. k8s-guard is the only caller. Standard library only.
"""
import json
import posixpath
import re
import shlex

GPU = 'nvidia.com/gpu'
ARCH_LABEL = 'kubernetes.io/arch'
DGD_KIND = 'DynamoGraphDeployment'
DGD_VERSIONS = ('nvidia.com/v1alpha1', 'nvidia.com/v1beta1')
DGD_NAME_LABEL = 'nvidia.com/dynamo-graph-deployment-name'
DGD_NAME_MAX = 39
LEASE_LABEL = 'wcep.agent/lease'
LEASE_RECORD = 'wcep-agent-lease'
# Every object the guard applies carries the UID of the namespace instance it was applied in.
NS_UID_LABEL = 'wcep.agent/namespace-uid'
# Every leased write carries the id of its attempt, recorded in k8s-leases before the write is sent.
ATTEMPT_ANNOTATION = 'wcep.agent/attempt'
GPU_PRODUCT_LABEL = 'nvidia.com/gpu.product'
HOSTNAME_TOPOLOGY = 'kubernetes.io/hostname'

# DGD fields, per API version. A field not listed is refused: a field the guard does not
# model could start a pod it does not count.
V1BETA1_SPEC = {'annotations', 'labels', 'priorityClassName', 'components', 'env', 'backendFramework', 'restart',
                'topologyConstraint', 'kvTransferPolicy', 'experimental', 'providerOverride'}
V1BETA1_COMPONENT = {'name', 'type', 'podTemplate', 'replicas', 'minAvailable', 'multinode', 'sharedMemorySize',
                     'modelRef', 'globalDynamoNamespace', 'runtimeVersionOverride', 'topologyConstraint'}
V1ALPHA1_SPEC = {'annotations', 'labels', 'priorityClassName', 'services', 'envs', 'backendFramework', 'restart',
                 'topologyConstraint', 'pvcs', 'experimental', 'providerOverride'}
V1ALPHA1_COMPONENT = {'annotations', 'labels', 'serviceName', 'componentType', 'subComponentType', 'dynamoNamespace',
                      'globalDynamoNamespace', 'resources', 'envs', 'envFromSecret', 'volumeMounts', 'modelRef',
                      'sharedMemory', 'extraPodMetadata', 'extraPodSpec', 'livenessProbe', 'readinessProbe',
                      'replicas', 'minAvailable', 'multinode', 'topologyConstraint', 'runtimeVersionOverride'}
# v1alpha1 fields allowed only while switched off.
V1ALPHA1_OFF = {'autoscaling', 'scalingAdapter', 'gpuMemoryService', 'failover', 'ingress', 'checkpoint'}
# Allowed only when empty.
EMPTY_ONLY = {'priorityClassName', 'experimental', 'providerOverride'}
COMPONENT_TYPES = {'frontend', 'worker', 'prefill', 'decode', 'default'}


class Refusal(Exception):
    """A refusal or failure. `code` is stable; `extra` joins the JSON output."""

    def __init__(self, code, message, status=1, **extra):
        super().__init__(message)
        self.code = code
        self.status = status
        self.extra = extra


# --- GPU counting ------------------------------------------------------------------

def quantity(value, where):
    if isinstance(value, bool):
        raise Refusal('field_refused', f'{where}: GPU count {value!r} is not a whole number')
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    if value in (None, ''):
        return 0
    raise Refusal('field_refused', f'{where}: GPU count {value!r} is not a whole number')


def check_resource_names(section, where):
    for name in section or {}:
        if name != GPU and ('gpu' in name.lower() or name.startswith('nvidia.com/')):
            raise Refusal('field_refused', f'{where}: resource {name} is not counted by the guard; use {GPU}')


def container_gpus(container, where):
    res = container.get('resources') or {}
    if res.get('claims'):
        raise Refusal('field_refused', f'{where}: resource claims (DRA) are not counted by the guard')
    check_resource_names(res.get('requests'), where)
    check_resource_names(res.get('limits'), where)
    return max(quantity((res.get('requests') or {}).get(GPU), where),
               quantity((res.get('limits') or {}).get(GPU), where))


def pod_spec_gpus(spec, where, main_extra=0):
    """GPUs one pod holds: running containers plus sidecar inits, or the largest init step."""
    spec = spec or {}
    if spec.get('resourceClaims'):
        raise Refusal('field_refused', f'{where}: resourceClaims (DRA) are not counted by the guard')
    main = sum(container_gpus(c, f'{where}.containers[{i}]') for i, c in enumerate(spec.get('containers') or []))
    sidecars, inits = 0, [0]
    for i, c in enumerate(spec.get('initContainers') or []):
        n = container_gpus(c, f'{where}.initContainers[{i}]')
        if c.get('restartPolicy') == 'Always':
            sidecars += n
        else:
            inits.append(n)
    return max(main + main_extra + sidecars, max(inits) + sidecars)


def replicas_of(c, where):
    r = c.get('replicas', 1)
    if r is None:
        return 1
    if isinstance(r, bool) or not isinstance(r, int) or r < 0:
        raise Refusal('field_refused', f'{where}.replicas must be a whole number')
    return r


def nodes_of(c, where):
    mn = c.get('multinode')
    if not mn:
        return 1
    n = mn.get('nodeCount', 1)
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise Refusal('field_refused', f'{where}.multinode.nodeCount must be at least 1')
    return n


class Component:
    def __init__(self, where, per_pod, replicas, nodes, labels, affinity, node_selector=None):
        self.where, self.per_pod, self.replicas, self.nodes = where, per_pod, replicas, nodes
        self.labels, self.affinity, self.node_selector = labels or {}, affinity or {}, node_selector or {}

    @property
    def gpus(self):
        return self.per_pod * self.replicas * self.nodes


def components(dgd):
    """The pod groups of a DGD, with GPUs per pod. Counts leniently: admission refuses what it cannot model."""
    api = dgd.get('apiVersion')
    name = (dgd.get('metadata') or {}).get('name')
    spec = dgd.get('spec') or {}
    out = []
    if api == 'nvidia.com/v1beta1':
        for i, c in enumerate(spec.get('components') or []):
            where = f'{name}.spec.components[{i}]'
            tmpl = c.get('podTemplate') or {}
            out.append(Component(where, pod_spec_gpus(tmpl.get('spec'), where + '.podTemplate.spec'),
                                 replicas_of(c, where), nodes_of(c, where),
                                 (tmpl.get('metadata') or {}).get('labels'), (tmpl.get('spec') or {}).get('affinity'),
                                 (tmpl.get('spec') or {}).get('nodeSelector')))
            for j, role in enumerate(c.get('roles') or []):
                rw = f'{where}.roles[{j}]'
                rt = role.get('podTemplate') or tmpl
                out.append(Component(rw, pod_spec_gpus(rt.get('spec'), rw + '.podTemplate.spec'),
                                     replicas_of(role, rw), nodes_of(c, where),
                                     (rt.get('metadata') or {}).get('labels'), (rt.get('spec') or {}).get('affinity'),
                                     (rt.get('spec') or {}).get('nodeSelector')))
    elif api == 'nvidia.com/v1alpha1':
        for svc, c in (spec.get('services') or {}).items():
            c = c or {}
            where = f'{name}.spec.services.{svc}'
            res = c.get('resources') or {}
            if res.get('claims'):
                raise Refusal('field_refused', f'{where}.resources.claims (DRA) are not counted by the guard')
            main = 0
            for section in ('requests', 'limits'):
                item = res.get(section) or {}
                main = max(main, quantity(item.get('gpu'), f'{where}.resources.{section}.gpu'))
                check_resource_names(item.get('custom'), f'{where}.resources.{section}.custom')
                main = max(main, quantity((item.get('custom') or {}).get(GPU), f'{where}.resources.{section}.custom'))
            extra = c.get('extraPodSpec') or {}
            mc = extra.get('mainContainer')
            if mc:
                main = max(main, container_gpus(mc, where + '.extraPodSpec.mainContainer'))
            out.append(Component(where, pod_spec_gpus(extra, where + '.extraPodSpec', main_extra=main),
                                 replicas_of(c, where), nodes_of(c, where),
                                 (c.get('extraPodMetadata') or {}).get('labels'), extra.get('affinity'),
                                 extra.get('nodeSelector')))
    else:
        raise Refusal('count_failed', f'{name}: DGD API {api!r} is not one the guard counts')
    return out


def dgd_gpus(dgd):
    return sum(c.gpus for c in components(dgd))


def pod_gpus(pod):
    if ((pod.get('status') or {}).get('phase')) in ('Succeeded', 'Failed'):
        return 0  # a finished pod holds no device; a terminating one still does
    name = (pod.get('metadata') or {}).get('name')
    try:
        return pod_spec_gpus(pod.get('spec'), f'pod {name}')
    except Refusal:
        # A GPU in a form the guard cannot count makes every budget answer wrong: stop.
        raise Refusal('count_failed', f'pod {name} requests GPUs in a form the guard cannot count')


def allocation(dgds, pods):
    """Per DGD, the larger of its spec and its live pods; plus GPU pods that belong to no DGD."""
    spec = {}
    for d in dgds:
        spec[(d.get('metadata') or {}).get('name')] = dgd_gpus(d)
    held = {}
    for p in pods:
        owner = ((p.get('metadata') or {}).get('labels') or {}).get(DGD_NAME_LABEL)
        held[owner] = held.get(owner, 0) + pod_gpus(p)
    total = held.pop(None, 0)
    for name in set(spec) | set(held):
        total += max(spec.get(name, 0), held.get(name, 0))
    return total


def budget_allocation(current, new, pods):
    """The allocation if any subset of the new DGDs lands: each DGD at the larger of its old and new spec.

    kubectl applies a file object by object and goes on after a failure, so a shrink earlier in the
    file may fail while a later growth lands. A failed reduction must never fund a growth.
    """
    old = {(d.get('metadata') or {}).get('name'): d for d in current}
    worst = dict(old)
    for d in new:
        name = d['metadata']['name']
        if name not in old or dgd_gpus(d) >= dgd_gpus(old[name]):
            worst[name] = d
    return allocation(worst.values(), pods)


def gpu_pod_summary(pods):
    counts = [pod_gpus(p) for p in pods]
    return sum(counts), sum(1 for n in counts if n > 0)


# --- admission checks on the dry-run result -------------------------------------------

def walk_forbidden(node, where, allow_default_scheduler=False):
    """Fields refused wherever they appear in a pod-bearing spec.

    `allow_default_scheduler` is for a server-side dry run, which fills in schedulerName: default-scheduler
    on a Job's pod template; any other scheduler still bypasses the rules the lease relies on.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            here = f'{where}.{key}'
            if key == 'hostPath':
                raise Refusal('field_refused', f'{here}: hostPath volumes are refused')
            if key == 'priorityClassName' and value:
                raise Refusal('field_refused', f'{here}: priorityClassName is refused')
            if key == 'privileged' and value is True:
                raise Refusal('field_refused', f'{here}: privileged containers are refused')
            if key == 'schedulerName' and allow_default_scheduler and value == 'default-scheduler':
                pass
            elif key in ('nodeName', 'schedulerName') and value:
                raise Refusal('field_refused', f'{here}: {key} bypasses the scheduler rules the one-node lease '
                                               'relies on')
            if key in ('hostNetwork', 'hostPID', 'hostIPC') and value is True:
                raise Refusal('field_refused', f'{here}: {key} is refused')
            if key in ('resourceClaims', 'resourceClaimTemplateName') and value:
                raise Refusal('field_refused', f'{here}: resource claims (DRA) are not counted by the guard')
            if key == 'tolerations' and isinstance(value, list):
                for i, t in enumerate(value):
                    if isinstance(t, dict) and t.get('operator') == 'Exists' and not t.get('key'):
                        raise Refusal('field_refused', f'{here}[{i}]: a toleration with operator Exists and no key '
                                                       'tolerates every taint, reservations included')
            walk_forbidden(value, here, allow_default_scheduler)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            walk_forbidden(item, f'{where}[{i}]', allow_default_scheduler)


def require_resources(container, where):
    res = container.get('resources') or {}
    for section in ('requests', 'limits'):
        got = res.get(section) or {}
        missing = [k for k in ('cpu', 'memory') if not got.get(k)]
        if missing:
            raise Refusal('resources_required', f'{where}: resources.{section} needs {" and ".join(missing)}')


def alpha_main_resources_ok(c):
    res = c.get('resources') or {}
    mc = ((c.get('extraPodSpec') or {}).get('mainContainer') or {}).get('resources') or {}
    for section in ('requests', 'limits'):
        merged = dict((mc.get(section) or {}))
        merged.update({k: v for k, v in (res.get(section) or {}).items() if k in ('cpu', 'memory') and v})
        if not (merged.get('cpu') and merged.get('memory')):
            return section
    return None


def check_keys(obj, allowed, where):
    for key, value in obj.items():
        if key not in allowed:
            raise Refusal('field_refused', f'{where}.{key} is a field the guard does not admit')
        if key in EMPTY_ONLY and value:
            raise Refusal('field_refused', f'{where}.{key} is refused')


def check_dgd(dgd, where):
    name = (dgd.get('metadata') or {}).get('name') or ''
    if len(name) > DGD_NAME_MAX:
        raise Refusal('dgd_name_too_long', f'{where}: DGD name {name} is {len(name)} characters, more than '
                                           f'{DGD_NAME_MAX}')
    spec = dgd.get('spec') or {}
    walk_forbidden(spec, where + '.spec')
    if dgd['apiVersion'] == 'nvidia.com/v1beta1':
        check_keys(spec, V1BETA1_SPEC, where + '.spec')
        for i, c in enumerate(spec.get('components') or []):
            cw = f'{where}.spec.components[{i}]'
            check_keys(c, V1BETA1_COMPONENT, cw)
            ctype = c.get('type')
            if ctype is not None and ctype not in COMPONENT_TYPES:
                raise Refusal('field_refused', f'{cw}.type {ctype!r} is refused (planners, EPP and LPX scale or '
                                               'start pods the guard does not count)')
            pod_spec = (c.get('podTemplate') or {}).get('spec') or {}
            for kind in ('containers', 'initContainers'):
                for j, ctr in enumerate(pod_spec.get(kind) or []):
                    require_resources(ctr, f'{cw}.podTemplate.spec.{kind}[{j}]')
            if not pod_spec.get('containers'):
                raise Refusal('resources_required', f'{cw}: podTemplate.spec.containers must name the main container '
                                                    'with its requests and limits')
    else:
        check_keys(spec, V1ALPHA1_SPEC, where + '.spec')
        for i, pvc in enumerate(spec.get('pvcs') or []):
            if (pvc or {}).get('create'):
                raise Refusal('field_refused', f'{where}.spec.pvcs[{i}]: the guard does not create PVCs')
        for svc, c in (spec.get('services') or {}).items():
            c = c or {}
            cw = f'{where}.spec.services.{svc}'
            for key, value in c.items():
                if key in V1ALPHA1_OFF:
                    if (value or {}).get('enabled'):
                        raise Refusal('field_refused', f'{cw}.{key} is enabled; the guard admits fixed replicas only')
                elif key not in V1ALPHA1_COMPONENT:
                    raise Refusal('field_refused', f'{cw}.{key} is a field the guard does not admit')
            for key in ('componentType', 'subComponentType'):
                if str(c.get(key, '')).lower() in ('planner', 'epp', 'lpx'):
                    raise Refusal('field_refused', f'{cw}.{key} {c[key]!r} is refused')
            if str(svc).lower() == 'planner':
                raise Refusal('field_refused', f'{cw}: a planner component is refused')
            ctype = c.get('componentType')
            if ctype not in (None, '') and ctype not in COMPONENT_TYPES:
                raise Refusal('field_refused', f'{cw}.componentType {ctype!r} is refused')
            missing = alpha_main_resources_ok(c)
            if missing:
                raise Refusal('resources_required', f'{cw}: resources.{missing} needs cpu and memory')
            extra = c.get('extraPodSpec') or {}
            for kind in ('containers', 'initContainers'):
                for j, ctr in enumerate(extra.get(kind) or []):
                    require_resources(ctr, f'{cw}.extraPodSpec.{kind}[{j}]')
    components(dgd)  # refuses GPU forms the guard cannot count


def admit(objects, ns, uid, lease_id):
    """Check every object the dry run returned. Returns the DGDs among them.

    Every object must carry both stamps `apply` writes: the namespace instance's UID and, when the
    write runs under a lease, that lease. Teardown selects by the lease label.
    """
    dgds, seen = [], set()
    for i, obj in enumerate(objects):
        kind, api = obj.get('kind'), obj.get('apiVersion')
        meta = obj.get('metadata') or {}
        labels = meta.get('labels') or {}
        where = f'{kind}/{meta.get("name")}'
        if (kind, meta.get('name')) in seen:
            raise Refusal('duplicate_object', f'{where} appears twice; kubectl would write both, one at a time')
        seen.add((kind, meta.get('name')))
        if meta.get('namespace') != ns:
            raise Refusal('kind_refused', f'{where} is not a namespaced object in {ns}')
        if labels.get(NS_UID_LABEL) != uid:
            raise Refusal('field_refused', f'{where} lost the label {NS_UID_LABEL}={uid}')
        if lease_id and labels.get(LEASE_LABEL) != lease_id:
            raise Refusal('field_refused', f'{where} lost the label {LEASE_LABEL}={lease_id}')
        if kind == DGD_KIND and api in DGD_VERSIONS:
            check_dgd(obj, where)
            dgds.append(obj)
        elif kind == 'ConfigMap' and api == 'v1':
            if meta.get('name') == LEASE_RECORD:
                raise Refusal('kind_refused', f'{where} is the guard\'s own lease record')
        elif kind == 'Service' and api == 'v1':
            spec = obj.get('spec') or {}
            if spec.get('type', 'ClusterIP') != 'ClusterIP' or spec.get('externalIPs'):
                raise Refusal('service_type_refused', f'{where}: only ClusterIP Services are admitted, not '
                                                      f'{spec.get("type")}')
            walk_forbidden(spec, where + '.spec')
        else:
            raise Refusal('kind_refused', f'{where} ({api}) is not an admitted kind; admitted: DynamoGraphDeployment, '
                                          'ConfigMap, ClusterIP Service')
    return dgds


def check_gpu_product(after, profile):
    """GPU pods must select the profile's GPU product, the type the lease counts."""
    want = profile['gpu_product']
    for d in after:
        for c in components(d):
            if c.per_pod and c.replicas and c.node_selector.get(GPU_PRODUCT_LABEL) != want:
                raise Refusal('gpu_product_required', f'{c.where}: GPU pods need nodeSelector '
                                                      f'{GPU_PRODUCT_LABEL}: {want}, the GPU the lease is for')


def check_lease_label(dgds, lease):
    """Every pod template of a leased write carries exactly that lease, whatever its GPUs, replicas or placement.

    Teardown finds a lease's pods by this label, so a template without it would start pods no teardown
    waits for. Zero replicas count too: a later scale-up is a patch that never shows the template again.
    """
    lid = lease['lease_id']
    for d in dgds:
        for c in components(d):
            if c.labels.get(LEASE_LABEL) != lid:
                raise Refusal('lease_label_required', f'{c.where}: every pod template needs the label '
                                                      f'{LEASE_LABEL}={lid}')


def check_one_node(after, lease, profile):
    """Without multi-node approval, every GPU pod must land on one node with the lease's other pods.

    The lease label itself is check_lease_label's, for every template and both placement modes; only the
    node count and the affinity depend on single-node placement.
    """
    if lease['multi_node']:
        return
    lid = lease['lease_id']
    for d in after:
        for c in components(d):
            if c.per_pod == 0 or c.replicas == 0:
                continue
            if c.nodes > 1:
                raise Refusal('one_node_required', f'{c.where}: multinode needs Ryan\'s multi-node approval')
            if c.labels.get(LEASE_LABEL) != lid:
                raise Refusal('one_node_required', f'{c.where}: GPU pods need the label {LEASE_LABEL}={lid}')
            terms = (((c.affinity.get('podAffinity') or {})
                      .get('requiredDuringSchedulingIgnoredDuringExecution')) or [])
            want = {'labelSelector': {'matchLabels': {LEASE_LABEL: lid}}, 'topologyKey': HOSTNAME_TOPOLOGY}
            if not any(t == want for t in terms):
                raise Refusal('one_node_required', f'{c.where}: GPU pods need a required podAffinity term exactly '
                                                   f'{json.dumps(want)}')



# --- scaling to 0 ---------------------------------------------------------------------

def esc(name):
    return str(name).replace('~', '~0').replace('/', '~1')


def zero_patch(dgd, lease_id):
    """A JSON patch that sets every replica count to 0.

    Its test operations make the server refuse it unless the object is still the one read (its UID and
    resourceVersion), still carries the lease label of the teardown being run, and every component index
    still names the same component.
    """
    ops = [{'op': 'test', 'path': '/metadata/uid', 'value': dgd['metadata']['uid']},
           {'op': 'test', 'path': '/metadata/resourceVersion', 'value': dgd['metadata']['resourceVersion']},
           {'op': 'test', 'path': '/metadata/labels/' + esc(LEASE_LABEL), 'value': lease_id}]
    spec = dgd.get('spec') or {}
    changed = False
    if dgd.get('apiVersion') == 'nvidia.com/v1beta1':
        for i, c in enumerate(spec.get('components') or []):
            ops.append({'op': 'test', 'path': f'/spec/components/{i}/name', 'value': c.get('name')})
            if c.get('replicas', 1) != 0:
                ops.append({'op': 'add', 'path': f'/spec/components/{i}/replicas', 'value': 0})
                changed = True
            for j, role in enumerate(c.get('roles') or []):
                if role.get('replicas', 1) != 0:
                    ops.append({'op': 'add', 'path': f'/spec/components/{i}/roles/{j}/replicas', 'value': 0})
                    changed = True
    elif dgd.get('apiVersion') == 'nvidia.com/v1alpha1':
        for svc, c in (spec.get('services') or {}).items():
            if (c or {}).get('replicas', 1) != 0:
                ops.append({'op': 'add', 'path': f'/spec/services/{esc(svc)}/replicas', 'value': 0})
                changed = True
    else:
        raise Refusal('count_failed', f'{dgd["metadata"]["name"]}: DGD API {dgd.get("apiVersion")!r} is unknown')
    return ops if changed else None


# --- build Jobs ---------------------------------------------------------------------
#
# A build is one Job rendered from the fixed template below, never a manifest a caller writes. The
# registry, the tag and the pinned images live here so a caller cannot choose them; the plan that
# fixed them (STRGT-1836) and the reasons are in references/k8s-guard.md.

REGISTRY = 'nvcr.io/goirlvsxnepa/llm_nim'
BUILDKIT_IMAGE = ('mirror.gcr.io/moby/buildkit:v0.23.2-rootless'
                  '@sha256:cab936745de5d673465948f1e93ff4d6e372bbe33f218afd3314eba45a6f85a9')
# The :debug variant of the same release: ko adds a busybox shell at /busybox, which the tag-check
# script needs to turn crane's exit code into the init container's answer. The crane binary is the same.
CRANE_IMAGE = ('gcr.io/go-containerregistry/crane/debug:v0.20.3'
               '@sha256:94f61956845714bea3b788445454ae4827f49a90dcd9dac28255c4cccb6220ad')
PUSH_SECRET = 'nvcr-push'
GIT_SECRET = 'git'
KNOWN_HOSTS_CONFIGMAP = 'git-known-hosts'
# The ed25519 host key GitHub publishes (https://api.github.com/meta, ssh_keys). No accept-new: a
# changed host key fails the clone instead of being trusted.
GITHUB_KNOWN_HOSTS = ('github.com ssh-ed25519 '
                      'AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl')
BUILD_LABEL = 'wcep.agent/build'
ARCHES = ('amd64', 'arm64')
COMMIT = re.compile(r'^[0-9a-f]{40}$')
IMAGE = re.compile(r'^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$')
DOCKERFILE = re.compile(r'^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$')
SSH_REPO = re.compile(r'^git@([A-Za-z0-9.-]+):[A-Za-z0-9._/-]+$')
HTTPS_REPO = re.compile(r'^https://[A-Za-z0-9.-]+/[A-Za-z0-9._/-]+$')
OWNER = re.compile(r'^[A-Za-z0-9]([A-Za-z0-9-]{0,38})$')
REPOSITORY = re.compile(r'^[A-Za-z0-9._-]{1,100}$')
DIGEST_IMAGE = re.compile(r'@sha256:[0-9a-f]{64}$')

# The tag-check initContainer runs crane under the debug image's busybox shell. `crane digest` sends
# every manifest Accept type itself, so a 404 is a missing tag and not an unaccepted OCI index; its
# error text carries MANIFEST_UNKNOWN for an absent tag. Only that string lets the build run: 401/403
# refuse, and any other failure refuses rather than guessing. Its test is TagCheckScript.
TAG_CHECK_SCRIPT = r'''set -u
out=$("$CRANE" digest "$REF" 2>&1) && { echo tag_exists; exit 1; }
case "$out" in
  *MANIFEST_UNKNOWN*|*"manifest unknown"*) exit 0 ;;
  *UNAUTHORIZED*|*DENIED*|*"401"*|*"403"*) echo registry_denied; exit 1 ;;
  *) echo "registry_error: $out"; exit 1 ;;
esac
'''

CLONE_SCRIPT = ('git clone --filter=blob:none "$REPO" /src && cd /src && git checkout "$COMMIT" '
                '&& test "$(git rev-parse HEAD)" = "$COMMIT"')
GIT_SSH_COMMAND = ('ssh -i /home/user/.ssh/id_ed25519 -o IdentitiesOnly=yes '
                   '-o UserKnownHostsFile=/home/user/.ssh/known_hosts -o StrictHostKeyChecking=yes')

JOB_SPEC = {'backoffLimit', 'activeDeadlineSeconds', 'ttlSecondsAfterFinished', 'template'}
JOB_TEMPLATE = {'metadata', 'spec'}
JOB_POD_SPEC = {'containers', 'initContainers', 'restartPolicy', 'securityContext', 'nodeSelector', 'volumes'}
JOB_CONTAINER = {'name', 'image', 'command', 'args', 'env', 'volumeMounts', 'resources', 'securityContext'}
JOB_POD_SECURITY = {'runAsUser', 'runAsGroup', 'fsGroup', 'seccompProfile', 'appArmorProfile'}
JOB_VOLUME = {'name', 'emptyDir', 'secret', 'projected'}


def commit_arg(text):
    value = str(text or '')
    if not COMMIT.match(value):
        raise Refusal('invalid_commit', f'--commit {value!r} is not a 40-character lower-case hex commit')
    return value


def image_arg(text):
    value = str(text or '')
    if len(value) > 63 or not IMAGE.match(value):
        raise Refusal('invalid_image', f'--image {value!r} is not a lower-case image name')
    return value


def dockerfile_arg(text):
    value = str(text or '')
    if not DOCKERFILE.match(value) or '..' in value.split('/'):
        raise Refusal('usage', f'--dockerfile {value!r} must be a relative path in the source tree', status=2)
    return value


def repo_arg(text):
    value = str(text or '')
    if SSH_REPO.match(value) or HTTPS_REPO.match(value):
        return value
    raise Refusal('usage', f'--repo {value!r} is not a git URL (git@host:path or https://host/path)', status=2)


def repo_is_ssh(repo):
    return repo.startswith('git@')


def arch_arg(requested, profile):
    """The profile decides the arch; --arch may only name it."""
    arch = profile.get('arch')
    if arch not in ARCHES:
        raise Refusal('profiles_invalid', f'the profile arch {arch!r} is not one of {", ".join(ARCHES)}')
    if requested and requested not in ARCHES:
        raise Refusal('usage', f'--arch must be one of {", ".join(ARCHES)}', status=2)
    if requested and requested != arch:
        raise Refusal('arch_mismatch', f'--arch {requested} is not this cluster arch ({arch})')
    return arch


def image_tag(commit, arch=None):
    tag = commit[:12] + (f'-{arch}' if arch else '')
    if not re.match(r'^[0-9a-f]{12}(-amd64|-arm64)?$', tag):
        raise Refusal('invalid_tag', f'cannot derive a tag from commit {commit!r} and arch {arch!r}')
    return tag


def image_ref(image, commit, arch=None):
    return f'{REGISTRY}/{image}:{image_tag(commit, arch)}'


def build_job_name(commit, arch):
    return f'build-{image_tag(commit, arch)}'


def index_job_name(commit):
    return f'index-{image_tag(commit)}'


def git_secret_spec(repo):
    owner, slash, name = str(repo or '').partition('/')
    if (not slash or not OWNER.match(owner) or not REPOSITORY.match(name) or name in ('.', '..')):
        raise Refusal('usage', f'--repo {repo!r} must be <owner>/<repo>', status=2)
    return {'ref': f'op://Fleet/{owner}-{name} deploy key/private key', 'name': GIT_SECRET,
            'key': 'id_ed25519'}


def index_argv(image, commit):
    """The crane argv of the index Job, in order: append both per-arch tags, then read it back."""
    ref = image_ref(image, commit)
    return [['index', 'append', '-t', ref, '-m', f'{ref}-amd64', '-m', f'{ref}-arm64'],
            ['manifest', ref],
            ['digest', ref]]


def build_verdict(log):
    """What a failed build Job's log says: the probe's marker, or a plain build failure."""
    text = str(log or '')
    for code in ('tag_exists', 'registry_denied', 'registry_error'):
        if code in text:
            return code
    return 'build_failed'


def redact_build_log(text):
    """A build log can echo the push credential; the shapes it takes are known even when the value is not."""
    text = re.sub(r'nv(?:api|cf|pr)-[A-Za-z0-9_-]{16,}', '[secret]', str(text or ''))
    return re.sub(r'("auth"\s*:\s*")[^"]+(")', r'\1[secret]\2', text)


def job_state(job):
    status = job.get('status') or {}
    for condition in status.get('conditions') or []:
        if condition.get('status') != 'True':
            continue
        if condition.get('type') == 'Complete':
            return 'succeeded'
        if condition.get('type') == 'Failed':
            return 'failed'
    return 'running' if status.get('active') else 'pending'


def resources(cpu, memory, storage, max_cpu, max_memory, max_storage):
    return {'requests': {'cpu': cpu, 'memory': memory, 'ephemeral-storage': storage},
            'limits': {'cpu': max_cpu, 'memory': max_memory, 'ephemeral-storage': max_storage}}


def _metadata(name, ns, uid, lease_id, kind, attempt=None):
    meta = {'name': name, 'namespace': ns,
            'labels': {NS_UID_LABEL: uid, LEASE_LABEL: lease_id, BUILD_LABEL: kind}}
    if attempt:
        meta['annotations'] = {ATTEMPT_ANNOTATION: attempt}
    return meta


def _docker_config_volume():
    return {'name': 'docker-config',
            'secret': {'secretName': PUSH_SECRET, 'defaultMode': 0o400,
                       'items': [{'key': '.dockerconfigjson', 'path': 'config.json'}]}}


def _docker_config_mount():
    return {'name': 'docker-config', 'mountPath': '/home/user/.docker', 'readOnly': True}


def _template(lease_id, pod):
    return {'metadata': {'labels': {LEASE_LABEL: lease_id}}, 'spec': pod}


def build_job(ns, uid, lease_id, attempt, repo, commit, dockerfile, image, arch, ssh):
    """The one build Job, from the fixed template. The guard admits nothing here it did not render."""
    name = build_job_name(commit, arch)
    directory = posixpath.dirname(dockerfile) or '.'
    volumes = [{'name': 'buildkit-state', 'emptyDir': {}}, {'name': 'src', 'emptyDir': {}},
               _docker_config_volume()]
    clone_mounts = [{'name': 'src', 'mountPath': '/src'}]
    clone_env = [{'name': 'REPO', 'value': repo}, {'name': 'COMMIT', 'value': commit}]
    if ssh:
        volumes.append({'name': 'git-ssh', 'projected': {
            'defaultMode': 0o400,
            'sources': [{'secret': {'name': GIT_SECRET,
                                    'items': [{'key': 'id_ed25519', 'path': 'id_ed25519'}]}},
                        {'configMap': {'name': KNOWN_HOSTS_CONFIGMAP,
                                       'items': [{'key': 'known_hosts', 'path': 'known_hosts'}]}}]}})
        clone_mounts.append({'name': 'git-ssh', 'mountPath': '/home/user/.ssh', 'readOnly': True})
        clone_env.append({'name': 'GIT_SSH_COMMAND', 'value': GIT_SSH_COMMAND})
    pod = {
        'restartPolicy': 'Never',
        'securityContext': {'runAsUser': 1000, 'runAsGroup': 1000, 'fsGroup': 1000,
                            'seccompProfile': {'type': 'Unconfined'},
                            'appArmorProfile': {'type': 'Unconfined'}},
        'nodeSelector': {ARCH_LABEL: arch},
        'volumes': volumes,
        'initContainers': [
            {'name': 'tag-check', 'image': CRANE_IMAGE,
             'command': ['/busybox/sh', '-c', TAG_CHECK_SCRIPT],
             'env': [{'name': 'CRANE', 'value': '/ko-app/crane'},
                     {'name': 'REF', 'value': image_ref(image, commit, arch)},
                     {'name': 'DOCKER_CONFIG', 'value': '/home/user/.docker'}],
             'volumeMounts': [_docker_config_mount()],
             'resources': resources('1', '1Gi', '5Gi', '2', '2Gi', '10Gi')},
            {'name': 'clone', 'image': BUILDKIT_IMAGE,
             'command': ['sh', '-c', CLONE_SCRIPT],
             'env': clone_env,
             'securityContext': {'runAsUser': 1000, 'runAsGroup': 1000},
             'volumeMounts': clone_mounts,
             'resources': resources('1', '2Gi', '5Gi', '2', '4Gi', '10Gi')},
        ],
        'containers': [
            {'name': 'build', 'image': BUILDKIT_IMAGE,
             'command': ['buildctl-daemonless.sh', 'build', '--oci-worker-no-process-sandbox',
                         '--frontend', 'dockerfile.v0',
                         '--local', 'context=/src',
                         '--local', f'dockerfile=/src/{directory}',
                         '--output', f'type=image,name={image_ref(image, commit, arch)},push=true'],
             'env': [{'name': 'DOCKER_CONFIG', 'value': '/home/user/.docker'}],
             'volumeMounts': [{'name': 'buildkit-state', 'mountPath': '/home/user/.local/share/buildkit'},
                              {'name': 'src', 'mountPath': '/src'},
                              _docker_config_mount()],
             'resources': resources('4', '8Gi', '20Gi', '8', '16Gi', '40Gi')},
        ],
    }
    return {'apiVersion': 'batch/v1', 'kind': 'Job',
            'metadata': _metadata(name, ns, uid, lease_id, 'run', attempt),
            'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': 3600, 'ttlSecondsAfterFinished': 86400,
                     'template': _template(lease_id, pod)}}


def index_job(ns, uid, lease_id, attempt, image, commit, arch):
    """The index Job: append both per-arch tags to the commit's manifest list, then read it back."""
    name = index_job_name(commit)
    steps = index_argv(image, commit)
    steps_meta = (('index-append', steps[0]), ('index-manifest', steps[1]))
    pod = {
        'restartPolicy': 'Never',
        'securityContext': {'runAsUser': 65532, 'runAsGroup': 65532, 'fsGroup': 65532},
        'nodeSelector': {ARCH_LABEL: arch},
        'volumes': [_docker_config_volume()],
        'initContainers': [{'name': n, 'image': CRANE_IMAGE, 'command': ['/ko-app/crane'], 'args': a,
                            'env': [{'name': 'DOCKER_CONFIG', 'value': '/home/user/.docker'}],
                            'volumeMounts': [_docker_config_mount()],
                            'resources': resources('1', '512Mi', '1Gi', '2', '1Gi', '2Gi')}
                           for n, a in steps_meta],
        'containers': [{'name': 'index-digest', 'image': CRANE_IMAGE, 'command': ['/ko-app/crane'],
                        'args': steps[2],
                        'env': [{'name': 'DOCKER_CONFIG', 'value': '/home/user/.docker'}],
                        'volumeMounts': [_docker_config_mount()],
                        'resources': resources('1', '512Mi', '1Gi', '2', '1Gi', '2Gi')}],
    }
    return {'apiVersion': 'batch/v1', 'kind': 'Job',
            'metadata': _metadata(name, ns, uid, lease_id, 'index', attempt),
            'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': 1800, 'ttlSecondsAfterFinished': 86400,
                     'template': _template(lease_id, pod)}}


def require_job_resources(container, where):
    res = container.get('resources') or {}
    for section in ('requests', 'limits'):
        got = res.get(section) or {}
        missing = [k for k in ('cpu', 'memory', 'ephemeral-storage') if not got.get(k)]
        if missing:
            raise Refusal('resources_required',
                          f'{where}: resources.{section} needs {" and ".join(missing)}')


def check_ssh_mount(pod, clone):
    """The key and the pinned host key are projected together, read-only, at /home/user/.ssh for user 1000."""
    volumes = [v for v in pod.get('volumes') or [] if v.get('name') == 'git-ssh']
    if len(volumes) != 1:
        raise Refusal('ssh_mount_refused', 'the clone needs exactly one git-ssh volume')
    projected = volumes[0].get('projected')
    if not isinstance(projected, dict):
        raise Refusal('ssh_mount_refused', 'git-ssh must be projected: the key and known_hosts together')
    if projected.get('defaultMode') != 0o400:
        raise Refusal('ssh_mount_refused', 'the projected git-ssh files must have mode 0400')
    sources = projected.get('sources') or []
    want = [{'secret': {'name': GIT_SECRET, 'items': [{'key': 'id_ed25519', 'path': 'id_ed25519'}]}},
            {'configMap': {'name': KNOWN_HOSTS_CONFIGMAP,
                           'items': [{'key': 'known_hosts', 'path': 'known_hosts'}]}}]
    if sources != want:
        raise Refusal('ssh_mount_refused', 'git-ssh must project the secret key as id_ed25519 beside '
                                           f'{KNOWN_HOSTS_CONFIGMAP} as known_hosts')
    mounts = [m for m in clone.get('volumeMounts') or [] if m.get('name') == 'git-ssh']
    if len(mounts) != 1 or mounts[0].get('mountPath') != '/home/user/.ssh' \
            or mounts[0].get('readOnly') is not True:
        raise Refusal('ssh_mount_refused', 'the key must be mounted read-only at /home/user/.ssh')
    env = {e.get('name'): e.get('value') for e in clone.get('env') or []}
    command = env.get('GIT_SSH_COMMAND') or ''
    try:
        options = shlex.split(command)
    except ValueError:
        raise Refusal('field_refused', 'GIT_SSH_COMMAND is not a shell-words string')
    pairs = [options[i:i + 2] for i in range(len(options) - 1)]
    for flag, value in (('-i', '/home/user/.ssh/id_ed25519'), ('-o', 'IdentitiesOnly=yes'),
                        ('-o', 'UserKnownHostsFile=/home/user/.ssh/known_hosts'),
                        ('-o', 'StrictHostKeyChecking=yes')):
        if [flag, value] not in pairs:
            raise Refusal('field_refused', f'GIT_SSH_COMMAND must carry {flag} {value}')
    if 'accept-new' in command:
        raise Refusal('field_refused', 'GIT_SSH_COMMAND must not accept a new host key')


def check_build_pod(pod, expect):
    init, main = pod.get('initContainers') or [], pod.get('containers') or []
    checks = [c for c in init if c.get('name') == 'tag-check']
    if len(checks) != 1 or checks[0].get('image') != CRANE_IMAGE:
        raise Refusal('field_refused', 'the tag check must run the pinned crane debug image')
    check_command = checks[0].get('command') or []
    if check_command[:1] != ['/busybox/sh'] or 'digest' not in ' '.join(check_command):
        raise Refusal('field_refused', 'the tag check runs the crane probe under the debug image shell')
    check_env = {e.get('name'): e.get('value') for e in checks[0].get('env') or []}
    if check_env.get('REF') != image_ref(expect['image'], expect['commit'], expect['arch']) \
            or check_env.get('CRANE') != '/ko-app/crane' \
            or check_env.get('DOCKER_CONFIG') != '/home/user/.docker':
        raise Refusal('field_refused', 'the tag check must digest this image tag with the mounted credential')
    check_mounts = [m for m in checks[0].get('volumeMounts') or [] if m.get('name') == 'docker-config']
    if len(check_mounts) != 1 or check_mounts[0].get('readOnly') is not True:
        raise Refusal('field_refused', 'the tag check reads the push credential read-only')
    clones = [c for c in init if c.get('name') == 'clone']
    if len(clones) != 1:
        raise Refusal('field_refused', 'the build needs one clone initContainer')
    clone = clones[0]
    if clone.get('image') != BUILDKIT_IMAGE:
        raise Refusal('field_refused', 'the clone initContainer must run the pinned builder image')
    command = clone.get('command') or []
    if len(command) < 2 or command[0] != 'sh' or 'git clone --filter=blob:none' not in command[-1] \
            or 'git rev-parse HEAD' not in command[-1]:
        raise Refusal('field_refused', 'the clone initContainer sets its own command: clone at the commit and '
                                       'verify HEAD')
    if (clone.get('securityContext') or {}).get('runAsUser') != 1000:
        raise Refusal('field_refused', 'the clone initContainer runs as runAsUser 1000')
    env = {e.get('name'): e.get('value') for e in clone.get('env') or []}
    if env.get('COMMIT') != expect['commit'] or env.get('REPO') != expect['repo']:
        raise Refusal('field_refused', 'the clone initContainer must take this repo and the full commit')
    if expect['ssh']:
        check_ssh_mount(pod, clone)
    elif any(v.get('name') == 'git-ssh' for v in pod.get('volumes') or []):
        raise Refusal('field_refused', 'a public repository runs without the git mount')
    if any('alpine/git' in (c.get('image') or '') for c in init + main):
        raise Refusal('field_refused', 'alpine/git is not part of the build pod')
    builds = [c for c in main if c.get('name') == 'build']
    if len(builds) != 1 or builds[0].get('image') != BUILDKIT_IMAGE:
        raise Refusal('field_refused', 'the build container must run the pinned builder image')
    outputs = [builds[0]['command'][i + 1] for i, a in enumerate(builds[0]['command']) if a == '--output']
    want = f'type=image,name={image_ref(expect["image"], expect["commit"], expect["arch"])},push=true'
    if outputs != [want]:
        raise Refusal('invalid_tag', f'the build output must be exactly {want!r}, not {outputs!r}')
    docker = {e.get('name'): e.get('value') for e in builds[0].get('env') or []}
    if docker.get('DOCKER_CONFIG') != '/home/user/.docker':
        raise Refusal('field_refused', 'DOCKER_CONFIG must point at the mounted push credential')
    mounts = [m for m in builds[0].get('volumeMounts') or [] if m.get('name') == 'docker-config']
    if len(mounts) != 1 or mounts[0].get('mountPath') != '/home/user/.docker' \
            or mounts[0].get('readOnly') is not True:
        raise Refusal('field_refused', 'the push credential is mounted read-only at /home/user/.docker')


def check_index_pod(pod, expect):
    steps = [c.get('args') for c in (pod.get('initContainers') or []) + (pod.get('containers') or [])]
    if steps != index_argv(expect['image'], expect['commit']):
        raise Refusal('invalid_tag', 'the index Job must run the fixed crane argv: append the commit tag '
                                     'with both per-arch tags, then manifest and digest')
    for container in (pod.get('initContainers') or []) + (pod.get('containers') or []):
        if container.get('image') != CRANE_IMAGE or container.get('command') != ['/ko-app/crane']:
            raise Refusal('field_refused', 'the index steps run the pinned crane image')
        if {e.get('name'): e.get('value') for e in container.get('env') or []}.get('DOCKER_CONFIG') \
                != '/home/user/.docker':
            raise Refusal('field_refused', 'the index steps need DOCKER_CONFIG at the mounted push credential')
        mounts = [m for m in container.get('volumeMounts') or [] if m.get('name') == 'docker-config']
        if len(mounts) != 1 or mounts[0].get('mountPath') != '/home/user/.docker' \
                or mounts[0].get('readOnly') is not True:
            raise Refusal('field_refused', 'the index steps read the push credential at /home/user/.docker')


def check_job(job, lease_id, expect, rendered=False):
    """Admit a guard-rendered Job. The strict field allowlists run on our own template (`rendered`); the
    checks that matter run on the server dry run's answer, so a webhook cannot add a way out."""
    where = f'{job.get("kind")}/{((job.get("metadata") or {}).get("name"))}'
    if job.get('apiVersion') != 'batch/v1' or job.get('kind') != 'Job':
        raise Refusal('kind_refused', f'{where} is not a batch/v1 Job')
    meta = job.get('metadata') or {}
    labels = meta.get('labels') or {}
    if labels.get(LEASE_LABEL) != lease_id:
        raise Refusal('lease_label_required', f'{where}: every object needs the label {LEASE_LABEL}={lease_id}')
    if labels.get(BUILD_LABEL) != expect['kind']:
        raise Refusal('field_refused', f'{where}: {BUILD_LABEL} must be {expect["kind"]}')
    spec = job.get('spec') or {}
    if rendered:
        check_keys(spec, JOB_SPEC, where + '.spec')
    if spec.get('backoffLimit') != 0:
        raise Refusal('field_refused', f'{where}.spec.backoffLimit must be 0')
    if not isinstance(spec.get('activeDeadlineSeconds'), int) or spec['activeDeadlineSeconds'] <= 0:
        raise Refusal('field_refused', f'{where}.spec.activeDeadlineSeconds must bound the build')
    if spec.get('ttlSecondsAfterFinished') != 86400:
        raise Refusal('field_refused', f'{where}.spec.ttlSecondsAfterFinished must be 86400')
    template = spec.get('template') or {}
    if rendered:
        check_keys(template, JOB_TEMPLATE, where + '.spec.template')
    if ((template.get('metadata') or {}).get('labels') or {}).get(LEASE_LABEL) != lease_id:
        raise Refusal('lease_label_required',
                      f'{where}.spec.template.metadata.labels needs {LEASE_LABEL}={lease_id}: teardown finds the '
                      'Job pods by it')
    pod = template.get('spec') or {}
    if rendered:
        check_keys(pod, JOB_POD_SPEC, where + '.spec.template.spec')
    walk_forbidden(pod, where + '.spec.template.spec', allow_default_scheduler=not rendered)
    if pod.get('imagePullSecrets'):
        raise Refusal('field_refused', f'{where}: no imagePullSecrets; the images are public and the push '
                                       'credential is mounted')
    if pod.get('restartPolicy') != 'Never':
        raise Refusal('field_refused', f'{where}: restartPolicy must be Never')
    if (pod.get('nodeSelector') or {}).get(ARCH_LABEL) != expect['arch']:
        raise Refusal('arch_mismatch', f'{where}: nodeSelector {ARCH_LABEL} must be {expect["arch"]}')
    security = pod.get('securityContext') or {}
    if rendered:
        check_keys(security, JOB_POD_SECURITY, where + '.spec.template.spec.securityContext')
    uid = expect.get('uid', 1000)
    for key, value in (('runAsUser', uid), ('runAsGroup', uid), ('fsGroup', uid)):
        if security.get(key) != value:
            raise Refusal('field_refused', f'{where}: securityContext.{key} must be {value}')
    if expect['kind'] == 'run':
        for key in ('seccompProfile', 'appArmorProfile'):
            if (security.get(key) or {}).get('type') != 'Unconfined':
                raise Refusal('field_refused', f'{where}: securityContext.{key}.type must be Unconfined')
    for group in ('containers', 'initContainers'):
        for i, container in enumerate(pod.get(group) or []):
            cw = f'{where}.spec.template.spec.{group}[{i}]'
            if rendered:
                check_keys(container, JOB_CONTAINER, cw)
            if (container.get('securityContext') or {}).get('allowPrivilegeEscalation') is not None:
                raise Refusal('field_refused', f'{cw}: allowPrivilegeEscalation must not be set (it sets '
                                               'no_new_privs, and the rootless build never starts)')
            require_job_resources(container, cw)
            if not DIGEST_IMAGE.search(container.get('image') or ''):
                raise Refusal('field_refused', f'{cw}: image {container.get("image")!r} must be pinned by digest')
    for i, volume in enumerate(pod.get('volumes') or []):
        if rendered:
            check_keys(volume, JOB_VOLUME, f'{where}.spec.template.spec.volumes[{i}]')
    docker = [v for v in pod.get('volumes') or [] if v.get('name') == 'docker-config']
    if len(docker) != 1 or (docker[0].get('secret') or {}).get('items') != [
            {'key': '.dockerconfigjson', 'path': 'config.json'}]:
        raise Refusal('field_refused', f'{where}: the push credential must mount .dockerconfigjson as config.json')
    if expect['kind'] == 'run':
        check_build_pod(pod, expect)
    else:
        check_index_pod(pod, expect)

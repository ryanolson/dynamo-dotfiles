"""The admission rules of k8s-guard: what a manifest may hold, and how many GPUs a namespace holds.

Pure functions over the JSON a server-side dry run returns and over listed pods. No I/O, so every rule
is checkable without a cluster. k8s-guard is the only caller. Standard library only.
"""
import json

GPU = 'nvidia.com/gpu'
DGD_KIND = 'DynamoGraphDeployment'
DGD_VERSIONS = ('nvidia.com/v1alpha1', 'nvidia.com/v1beta1')
DGD_NAME_LABEL = 'nvidia.com/dynamo-graph-deployment-name'
DGD_NAME_MAX = 39
LEASE_LABEL = 'wcep.agent/lease'
LEASE_RECORD = 'wcep-agent-lease'
# Every object the guard applies carries the UID of the namespace instance it was applied in.
NS_UID_LABEL = 'wcep.agent/namespace-uid'
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

def walk_forbidden(node, where):
    """Fields refused wherever they appear in a pod-bearing spec."""
    if isinstance(node, dict):
        for key, value in node.items():
            here = f'{where}.{key}'
            if key == 'hostPath':
                raise Refusal('field_refused', f'{here}: hostPath volumes are refused')
            if key == 'priorityClassName' and value:
                raise Refusal('field_refused', f'{here}: priorityClassName is refused')
            if key == 'privileged' and value is True:
                raise Refusal('field_refused', f'{here}: privileged containers are refused')
            if key in ('nodeName', 'schedulerName') and value:
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
            walk_forbidden(value, here)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            walk_forbidden(item, f'{where}[{i}]')


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


def admit(objects, ns, uid):
    """Check every object the dry run returned. Returns the DGDs among them."""
    dgds, seen = [], set()
    for i, obj in enumerate(objects):
        kind, api = obj.get('kind'), obj.get('apiVersion')
        meta = obj.get('metadata') or {}
        where = f'{kind}/{meta.get("name")}'
        if (kind, meta.get('name')) in seen:
            raise Refusal('duplicate_object', f'{where} appears twice; kubectl would write both, one at a time')
        seen.add((kind, meta.get('name')))
        if meta.get('namespace') != ns:
            raise Refusal('kind_refused', f'{where} is not a namespaced object in {ns}')
        if (meta.get('labels') or {}).get(NS_UID_LABEL) != uid:
            raise Refusal('field_refused', f'{where} lost the label {NS_UID_LABEL}={uid}')
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


def check_one_node(after, lease, profile):
    """Without multi-node approval, every GPU pod must land on one node with the lease's other pods."""
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


def zero_patch(dgd, ns_uid):
    """A JSON patch that sets every replica count to 0.

    Its test operations make the server refuse it unless the object is still the one read (UID) and
    was applied in the namespace instance being torn down (the UID label), and every component
    index still names the same component.
    """
    ops = [{'op': 'test', 'path': '/metadata/uid', 'value': dgd['metadata']['uid']},
           {'op': 'test', 'path': '/metadata/labels/' + NS_UID_LABEL.replace('~', '~0').replace('/', '~1'),
            'value': ns_uid}]
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

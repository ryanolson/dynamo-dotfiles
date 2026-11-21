"""Rules that find stale deployments and GPU holds that could be released.

Pure functions: parsed `kubectl get -o json` output in, findings out. Nothing here calls a
cluster. The API shows no GPU utilization, so a finding is a candidate for release, not proof
that the GPUs are idle. A person decides.

A finding is a dict with: kind, namespace, owner, name, gpus, age_hours, gpu_hours, reason.
`cluster` is added by the caller.
"""
from datetime import datetime, timezone

GPU = 'nvidia.com/gpu'
OPT_OUT = 'gpu-reaper.nvidia.com/opt-out'
OPT_OUT_EXPIRES = 'gpu-reaper.nvidia.com/opt-out-expires'
DGD_NAME_LABEL = 'nvidia.com/dynamo-graph-deployment-name'
DEVELOPER_LABEL = 'dynamo.nvidia.com/developer'
# A DGD whose state is one of these is healthy; any other state is "not done".
DGD_OK_STATES = ('successful', 'ready')
BAD_WAITING = ('CrashLoopBackOff', 'ImagePullBackOff', 'ErrImagePull', 'CreateContainerConfigError')

DEFAULTS = {'hold_days': 3.0, 'dgd_days': 14.0, 'stuck_hours': 2.0, 'unready_hours': 1.0}


def parse_time(value):
    """An RFC 3339 timestamp as an aware datetime, or None when it is missing or unreadable."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def hours_since(value, now):
    parsed = parse_time(value)
    return None if parsed is None else max(0.0, (now - parsed).total_seconds() / 3600)


def quantity(value):
    try:
        return int(str(value))
    except ValueError:
        return 0


def pod_gpus(pod):
    """GPUs the pod's containers ask for. Limits count when requests are absent."""
    total = 0
    for c in (pod.get('spec') or {}).get('containers') or []:
        res = c.get('resources') or {}
        total += quantity((res.get('requests') or {}).get(GPU) or (res.get('limits') or {}).get(GPU) or 0)
    return total


def owner_of(namespace, ns_labels, pod_labels=None):
    """Who to ask: the pod's owner label, the namespace's developer label, or a guess from the name."""
    for source, key in ((pod_labels, 'owner'), (ns_labels, DEVELOPER_LABEL), (ns_labels, 'owner')):
        if source and source.get(key):
            return source[key]
    parts = namespace.split('-')
    if parts[0] in ('dyntest', 'dynamo') and len(parts) > 1:
        return parts[1]
    return parts[0]


def pod_not_ready_hours(pod, now):
    """Hours a Running pod has been not Ready, or None when it is Ready or the time is unknown."""
    status = pod.get('status') or {}
    if status.get('phase') != 'Running':
        return None
    ready = next((c for c in status.get('conditions') or [] if c.get('type') == 'Ready'), None)
    waiting = [((c.get('state') or {}).get('waiting') or {}).get('reason')
               for c in status.get('containerStatuses') or []]
    broken = any(reason in BAD_WAITING for reason in waiting)
    if ready is not None and ready.get('status') == 'True' and not broken:
        return None
    since = (ready or {}).get('lastTransitionTime') or (pod.get('metadata') or {}).get('creationTimestamp')
    return hours_since(since, now)


def finding(kind, namespace, owner, name, gpus, age_hours, reason):
    age = age_hours or 0.0
    return {'kind': kind, 'namespace': namespace, 'owner': owner, 'name': name, 'gpus': gpus,
            'age_hours': round(age, 1), 'gpu_hours': round(gpus * age, 1), 'reason': reason}


def analyze(pods, dgds, namespaces, now, **thresholds):
    """Findings for one cluster, most GPU-hours first.

    pods and dgds are the `items` of `get -A -o json`; namespaces maps a name to its labels.
    """
    limit = {**DEFAULTS, **thresholds}
    found = []
    by_ns = {}
    gpu_pods_by_dgd = {}
    for pod in pods:
        meta = pod.get('metadata') or {}
        gpus = pod_gpus(pod)
        # Only a Running pod holds its GPUs: a Pending pod is not scheduled, and a finished pod has let go.
        if not gpus or (pod.get('status') or {}).get('phase') != 'Running':
            continue
        ns = meta.get('namespace', '')
        by_ns.setdefault(ns, []).append((pod, gpus))
        dgd = (meta.get('labels') or {}).get(DGD_NAME_LABEL)
        if dgd:
            gpu_pods_by_dgd.setdefault((ns, dgd), []).append(gpus)

    for ns, held in sorted(by_ns.items()):
        labels = namespaces.get(ns) or {}
        running = held
        total = sum(g for _, g in running)
        if not total:
            continue
        owner = owner_of(ns, labels, (running[0][0].get('metadata') or {}).get('labels'))
        oldest = max((hours_since((p.get('metadata') or {}).get('creationTimestamp'), now) or 0.0) for p, _ in running)
        if oldest >= limit['hold_days'] * 24:
            found.append(finding('gpu_hold_old', ns, owner, ns, total, oldest,
                                 f'{total} GPUs held, oldest pod {oldest / 24:.1f} days old'))

        opted = [(p, g) for p, g in running
                 if ((p.get('metadata') or {}).get('annotations') or {}).get(OPT_OUT) == 'true']
        if opted:
            opted_gpus = sum(g for _, g in opted)
            expiries = [parse_time(((p.get('metadata') or {}).get('annotations') or {}).get(OPT_OUT_EXPIRES))
                        for p, _ in opted]
            known = [e for e in expiries if e is not None]
            latest = max(known) if known else None
            if latest is None:
                found.append(finding('reaper_opt_out', ns, owner, ns, opted_gpus, oldest,
                                     f'{opted_gpus} GPUs opted out of the reaper with no readable expiry'))
            elif latest < now:
                found.append(finding('reaper_opt_out_expired', ns, owner, ns, opted_gpus, oldest,
                                     f'{opted_gpus} GPUs: the reaper opt-out ended {latest:%Y-%m-%d}; the reaper may be blind here'))
            else:
                found.append(finding('reaper_opt_out', ns, owner, ns, opted_gpus, oldest,
                                     f'{opted_gpus} GPUs opted out of the reaper until {latest:%Y-%m-%d}'))

        for pod, gpus in running:
            hours = pod_not_ready_hours(pod, now)
            if hours is not None and hours >= limit['unready_hours']:
                meta = pod.get('metadata') or {}
                found.append(finding('gpu_pod_not_ready', ns, owner, meta.get('name', ''), gpus, hours,
                                     f'holds {gpus} GPUs and has not been Ready for {hours:.1f} hours'))

    for dgd in dgds:
        meta = dgd.get('metadata') or {}
        ns, name = meta.get('namespace', ''), meta.get('name', '')
        state = (dgd.get('status') or {}).get('state')
        owner = owner_of(ns, namespaces.get(ns) or {})
        gpus = sum(gpu_pods_by_dgd.get((ns, name), []))
        age = hours_since(meta.get('creationTimestamp'), now)
        if age is None:
            continue
        if state == 'failed':
            found.append(finding('dgd_failed', ns, owner, name, gpus, age,
                                 f'failed {age / 24:.1f} days ago' + (f' and its pods hold {gpus} GPUs' if gpus else '')))
        elif state not in DGD_OK_STATES:
            if age >= limit['stuck_hours']:
                found.append(finding('dgd_stuck', ns, owner, name, gpus, age,
                                     f'state is {state or "unknown"} after {age:.1f} hours'))
        elif age >= limit['dgd_days'] * 24 and gpus:
            found.append(finding('dgd_old', ns, owner, name, gpus, age,
                                 f'{age / 24:.1f} days old and holding {gpus} GPUs'))

    found.sort(key=lambda f: (-f['gpu_hours'], -f['age_hours'], f['kind'], f['namespace'], f['name']))
    return found

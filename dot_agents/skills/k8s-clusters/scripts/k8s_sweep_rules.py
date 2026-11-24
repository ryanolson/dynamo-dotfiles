"""Rules for k8s-sweep: what our namespaces hold, what the leases cover, and when the backstop acts.

Pure functions: parsed `kubectl get -o json` output, `bb k8s leases --all --json` rows and ledger
lines in; rows, flags, decisions and the digest table out. Nothing here calls a cluster or bb.

GPUs are counted the guard's way (k8s_guard_rules.allocation): per DGD the larger of its spec
and its live pods, plus GPU pods no DGD owns. A namespace's expiry is the guard's too
(NsView.expires_at): the lease record's `expires_at` when the record names this namespace's
UID, else the creation annotation. So the backstop never acts on an expiry the guard would refuse.
"""
from collections import defaultdict
from datetime import datetime, timedelta
import json
from pathlib import Path

from k8s_guard_rules import (DGD_NAME_LABEL, Refusal, allocation, check_spend_deadline, check_spend_window,
                             gpu_spend, job_gpus, job_state, pod_gpus, pod_spec_gpus)

# expires-at plus this, and `k8s-guard down` deletes the namespace; before it, `expire` scales to 0.
GRACE = timedelta(hours=1)
DEVELOPER_LABEL = 'dynamo.nvidia.com/developer'
THREAD_LABEL = 'wcep.agent/thread'
EXPIRES_ANNOTATION = 'wcep.agent/expires-at'
REAPER_STATE = 'gpu-reaper.nvidia.com/state'
LIVE = ('active', 'revoking', 'releasing')
ENDED = ('released', 'torn-down')
FINISHED = ('Succeeded', 'Failed')
# The guard's teardown must never run as the namespace's owner thread, whose `down` releases the lease.
THREAD_ENV = ('BB_THREAD_ID', 'BB_THREAD_STORAGE')


def parse_time(value):
    """An RFC 3339 time with a zone, or None. The guard refuses a time without a zone, so do we."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def hours_since(value, now):
    parsed = parse_time(value)
    return None if parsed is None else round(max(0.0, (now - parsed).total_seconds() / 3600), 1)


def flag(kind, namespace, detail, **extra):
    return {'kind': kind, 'namespace': namespace, 'detail': detail, **extra}


def effective_expiry(ns, record):
    meta = ns.get('metadata') or {}
    data = (record or {}).get('data') or {}
    from_record = data.get('expires_at') if record and data.get('uid') == meta.get('uid') else None
    return from_record or (meta.get('annotations') or {}).get(EXPIRES_ANNOTATION)


def audit_gpu_spend(cluster, namespace, jobs, pods, now):
    """Read the Job record even when no Pod exists. Match children by controller UID."""
    records, flags, covered_pods, recorded_jobs = [], [], set(), {}
    job_holds = defaultdict(int)
    for kind, objects in (('Job', jobs), ('Pod', pods)):
        for obj in sorted(objects, key=lambda o: o['metadata']['name']):
            meta = obj['metadata']
            name = meta['name']
            where = f'{kind}/{name}'
            try:
                gpus = job_gpus(obj, where) if kind == 'Job' else pod_spec_gpus(obj.get('spec'), where + '.spec')
            except Refusal as e:
                flags.append(flag('gpu_spend_count_failed', namespace, str(e), object=where))
                continue
            if not gpus:
                continue
            finished = (job_state(obj) in ('succeeded', 'failed') if kind == 'Job'
                        else (obj.get('status') or {}).get('phase') in FINISHED)
            if kind == 'Pod':
                controller = next((ref for ref in meta.get('ownerReferences') or []
                                   if ref.get('kind') == 'Job' and ref.get('controller') is True), None)
                job_key = (controller.get('uid'), controller.get('name')) if controller else None
                if job_key in recorded_jobs:
                    _, start, end = recorded_jobs[job_key]
                    if not finished:
                        job_holds[job_key] += gpus
                        try:
                            check_spend_window(where, start, end, now)
                        except Refusal as e:
                            flags.append(flag(e.code, namespace, str(e), object=where, gpus=gpus))
                    covered_pods.add(name)
                    continue
            try:
                record, start, end = gpu_spend(obj, where, gpus, cluster, namespace)
            except Refusal as e:
                flags.append(flag(e.code, namespace, str(e), object=where, gpus=gpus))
                continue
            if kind == 'Job':
                records.append({'object': where, **record})
                if meta.get('uid'):
                    recorded_jobs[meta['uid'], name] = record, start, end
            try:
                if not finished:
                    check_spend_window(where, start, end, now)
                if kind == 'Pod':
                    raise Refusal('gpu_spend_job_required', f'{where} must run under a Job with a GPU spend record.')
                started = parse_time((obj.get('status') or {}).get('startTime'))
                check_spend_deadline(obj, where, end, started or (start if finished else now))
            except Refusal as e:
                flags.append(flag(e.code, namespace, str(e), object=where, gpus=gpus))
    for job_key, gpus in job_holds.items():
        recorded = recorded_jobs[job_key][0]['gpus']
        where = f'Job/{job_key[1]}'
        if gpus > recorded:
            flags.append(flag('gpu_spend_over_count', namespace, f'{where} has {gpus} live Pod GPUs and records '
                              f'{recorded}.', object=where, gpus=gpus, recorded_gpus=recorded))
    return records, flags, covered_pods


def namespace_row(ns, record, dgds, pods, now, premade, covered_pods=frozenset()):
    """One namespace of ours: who owns it, when it ends, and the GPUs it holds."""
    meta = ns.get('metadata') or {}
    name = meta.get('name', '')
    labels = meta.get('labels') or {}
    claimed = premade is not None and name == premade
    row = {'namespace': name, 'uid': meta.get('uid'), 'thread': labels.get(THREAD_LABEL),
           'age_hours': hours_since(meta.get('creationTimestamp'), now),
           # The pre-made namespace keeps no expiry: only k8s-leases ends its claims.
           'expires_at': None if claimed else effective_expiry(ns, record),
           'claimed': claimed, 'terminating': bool(meta.get('deletionTimestamp')),
           'lease_record': ((record or {}).get('data') or {}).get('lease_id') or None,
           'gpus': None, 'pod_gpus': 0, 'oldest_pod_hours': None, 'reaper_warned': 0, 'orphan_gpu_pods': [],
           'flags': [], 'lease': None, 'action': None}
    try:
        row['gpus'] = allocation(dgds, pods)
    except Refusal as e:
        row['flags'].append(flag('count_failed', name, str(e)))
    owned = {(d.get('metadata') or {}).get('name') for d in dgds}
    covered_gpus = 0
    for p in pods:
        pmeta = p.get('metadata') or {}
        if (p.get('status') or {}).get('phase') in FINISHED:
            continue
        age = hours_since(pmeta.get('creationTimestamp'), now)
        if age is not None and (row['oldest_pod_hours'] is None or age > row['oldest_pod_hours']):
            row['oldest_pod_hours'] = age
        marks = {**(pmeta.get('labels') or {}), **(pmeta.get('annotations') or {})}
        if marks.get(REAPER_STATE) == 'warned':
            row['reaper_warned'] += 1
        try:
            gpus = pod_gpus(p)
        except Refusal:
            continue  # allocation() above already raised count_failed for it
        row['pod_gpus'] += gpus
        if gpus and (pmeta.get('labels') or {}).get(DGD_NAME_LABEL) not in owned:
            if pmeta.get('name') in covered_pods:
                covered_gpus += gpus
            else:
                row['orphan_gpu_pods'].append(pmeta.get('name', ''))
    if covered_gpus and row['gpus'] is not None:
        row['lease_gpus'] = row['gpus'] - covered_gpus
    if row['orphan_gpu_pods']:
        names = row['orphan_gpu_pods']
        row['flags'].append(flag('gpu_pod_no_dgd', name, f'{len(names)} GPU pod(s) that no DGD owns: '
                                 + ', '.join(names[:5]) + (' ...' if len(names) > 5 else '')))
    return row


def unread_row(ns, now, premade, why):
    """A namespace whose pods, DGDs or lease record could not be read: no counts, no action."""
    row = namespace_row(ns, None, [], [], now, premade)
    row.update(gpus=None, expires_at=None, read_failed=True)
    row['flags'] = [flag('read_failed', row['namespace'], why[:300])]
    return row


def lease_view(lease):
    return {'lease_id': lease['lease_id'], 'state': lease['state'], 'gpus': lease['gpus']}


def match_leases(cluster, rows, leases, exists):
    """Flag what the leases do not cover, and leases whose namespace is gone. Returns the new flags.

    `leases` is None when bb could not be read: then no lease flag is raised, since an unread list
    is not an empty one. `exists` maps a namespace name read by name to its UID, or None when the
    cluster said NotFound; a lease whose namespace was not read is not judged.
    """
    if leases is None:
        return []
    mine = [l for l in leases if l.get('cluster') == cluster]
    live = [l for l in mine if l.get('state') in LIVE]
    found = []
    by_name = {r['namespace']: r for r in rows}
    for r in rows:
        same = [l for l in live if l['namespace'] == r['namespace'] and l['uid'] == r['uid']]
        active = [l for l in same if l['state'] == 'active']
        shown = active[0] if active else (same[0] if same else None)
        r['lease'] = lease_view(shown) if shown else {}
        gpus = r.get('lease_gpus', r['gpus'])
        if gpus and not active:
            others = [f'{l["lease_id"]} is {l["state"]}' for l in live if l['namespace'] == r['namespace']]
            new = flag('gpus_no_lease', r['namespace'], f'{gpus} GPUs held and no active lease'
                       + (f' ({", ".join(others)})' if others else ''), cluster=cluster)
        elif active and gpus is not None and gpus > active[0]['gpus']:
            new = flag('gpus_over_lease', r['namespace'], f'{gpus} GPUs held; lease {active[0]["lease_id"]} '
                       f'grants {active[0]["gpus"]}', cluster=cluster)
        else:
            continue
        r['flags'].append(new)
        found.append(new)
    for l in live:
        r = by_name.get(l['namespace'])
        if r is not None:
            if r['uid'] == l['uid']:
                continue
            why = f're-created: its UID is now {r["uid"]}, the lease names {l["uid"]}'
        elif l['namespace'] in exists:
            uid = exists[l['namespace']]
            if uid == l['uid']:
                continue
            why = 'not found' if uid is None else f're-created: its UID is now {uid}, the lease names {l["uid"]}'
        else:
            continue
        found.append(flag('lease_namespace_gone', l['namespace'], f'{why}; k8s-leases should release it',
                          cluster=cluster, lease_id=l['lease_id'], state=l['state'], thread=l.get('thread'),
                          gpus=l.get('gpus')))
    return found


def decide(row, known_uid, now, grace=GRACE):
    """What --enforce does to one namespace: {'action': None|'expire'|'down', 'reason', 'flag'}.

    `known_uid` comes from a record older than this read (the ledger's create line, or the lease),
    never from the live namespace: a namespace re-created under the same name has another UID,
    and is never touched.
    """
    def none(reason, flag_kind=None):
        return {'action': None, 'reason': reason, 'flag': flag_kind}

    if row['claimed']:
        return none('the pre-made namespace keeps no expiry; only k8s-leases ends a claim')
    if row.get('read_failed'):
        return none('its reads failed; a lease record not read could hold a later expiry')
    if known_uid is not None and known_uid != row['uid']:
        return none(f'the namespace was re-created: its UID is {row["uid"]}, the record says {known_uid}; left alone',
                    'namespace_recreated')
    if row['terminating']:
        return none('already being deleted')
    expires = row['expires_at']
    if not expires:
        return none('no expiry')
    when = parse_time(expires)
    if when is None:
        return none(f'expiry {expires!r} is not an RFC 3339 time with a zone', 'expiry_unreadable')
    if now < when:
        return none(f'expires at {expires}')
    if known_uid is None:
        return none(f'expired at {expires}, but no ledger line or lease records its UID; left alone', 'uid_unknown')
    if now >= when + grace:
        return {'action': 'down', 'reason': f'expired at {expires}, past the grace period', 'flag': None}
    return {'action': 'expire', 'reason': f'expired at {expires}, inside the grace period', 'flag': None}


def _ledger_lines(pairs):
    """(line, sources) for each distinct line, oldest first. A host keeps each line twice: once in its
    own ledger and once in the thread's."""
    seen = {}
    for line, source in pairs:
        if not isinstance(line, dict) or not isinstance(line.get('object'), str):
            continue
        key = json.dumps(line, sort_keys=True)
        if key in seen:
            if source not in seen[key][1]:
                seen[key][1].append(source)
        else:
            seen[key] = (line, [source])
    return sorted(seen.values(), key=lambda item: str(item[0].get('time') or ''))


def created_uids(pairs):
    """(cluster, namespace) to the UID its newest `ns create` line recorded."""
    out = {}
    for line, _ in _ledger_lines(pairs):
        ns = line.get('namespace')
        if line['object'] == f'Namespace/{ns}' and line.get('direction') == 'up' and line.get('created'):
            out[(line.get('cluster'), ns)] = line.get('uid')
    return out


def open_ledger(pairs, ended):
    """Ledger entries no `down` closed, by the guard's rule (Guard.ledger_namespaces).

    A namespace opens on `up` with `created` and closes on `down` with `deleted`: `expire` also
    writes `down`, without deleting, so its line closes nothing. A claim opens on `claim` and closes
    on its `Claim/<ns>` `down`. An entry whose lease k8s-leases reports released or torn down is
    closed too: its teardown may have run, and been recorded, on another host. Object lines and the
    sweep's own `Sweep/<ns>` lines open nothing.
    """
    opened = {}
    leases_of = {}
    for line, sources in _ledger_lines(pairs):
        obj, direction = line['object'], line.get('direction')
        cluster, ns, uid, lease = line.get('cluster'), line.get('namespace'), line.get('uid'), line.get('lease')
        if lease and uid:
            leases_of.setdefault((cluster, ns, uid), set()).add(lease)
        entry = {'cluster': cluster, 'namespace': ns, 'uid': uid, 'lease': None, 'thread': line.get('thread'),
                 'since': line.get('time'), 'sources': sources}
        if obj == f'Namespace/{ns}':
            key = ('namespace', cluster, ns, uid)
            if direction == 'up' and line.get('created'):
                opened[key] = dict(entry, kind='namespace')
            elif direction == 'down' and line.get('deleted'):
                opened.pop(key, None)
        elif obj == f'Claim/{ns}' and lease:
            # The guard writes every claim with its lease. A line without one (hand-edited, truncated, or
            # from another writer) would open an entry keyed on no lease, which no `down` could close.
            key = ('claim', cluster, ns, lease)
            if direction == 'claim':
                opened[key] = dict(entry, kind='claim', lease=lease)
            elif direction == 'down':
                opened.pop(key, None)
    out = []
    for e in opened.values():
        if e['kind'] == 'claim' and e['lease'] in ended:
            continue
        if e['kind'] == 'namespace' and leases_of.get((e['cluster'], e['namespace'], e['uid']), set()) & ended:
            continue
        out.append(e)
    return out


def bb_env(environ, home):
    """The environment bb needs from a timer: the shared server's URL from the file chezmoi writes,
    as ~/.profile reads it, unless it is already set."""
    env = dict(environ)
    if not env.get('BB_SERVER_URL'):
        try:
            url = (Path(home) / '.config/bb/server-url').read_text().splitlines()[0].strip()
        except (OSError, IndexError):
            url = ''
        if url:
            env['BB_SERVER_URL'] = url
    return env


def guard_env(environ, home):
    env = bb_env(environ, home)
    for key in THREAD_ENV:
        env.pop(key, None)
    return env


# --- the digest -----------------------------------------------------------------------------

COLUMNS = ('Kind', 'Cluster', 'Namespace', 'Thread', 'GPUs', 'Lease', 'Age', 'Expires', 'Flags', 'Action')


def cell(value):
    text = '' if value is None else str(value)
    return text.replace('\\', '\\\\').replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')


def lease_cell(lease):
    if lease is None:
        return '?'  # bb could not be read
    if not lease:
        return '-'
    return f'{lease["lease_id"]} {lease["state"]} {lease["gpus"]}'


def flags_cell(flags):
    return '; '.join(f'{f["kind"]}: {f["detail"]}' for f in flags)


def digest_table(report):
    """One Markdown table for the steward's digest: a row per namespace of ours, per stale lease,
    and per open ledger entry, with when and where it was read above it and any errors below."""
    head = (f'k8s-sweep on {report.get("host")} at {report.get("checked_at")} ({report.get("mode")}); '
            f'clusters read: {", ".join(report.get("clusters") or []) or "none"}')
    rows = []
    for r in report.get('namespaces') or []:
        action = r.get('action')
        rows.append(['namespace', r.get('cluster'), r['namespace'], r.get('thread'),
                     '?' if r.get('gpus') is None else r['gpus'], lease_cell(r.get('lease')),
                     '' if r.get('age_hours') is None else f'{r["age_hours"]:.1f}h',
                     'claimed' if r.get('claimed') else (r.get('expires_at') or '-'), flags_cell(r.get('flags') or []),
                     f'{action["action"]}: {action["result"]}' if action else ''])
        for spend in r.get('gpu_spend') or []:
            rows.append(['GPU spend', r.get('cluster'), r['namespace'], spend['thread'], spend['gpus'], '-', '',
                         f'{spend["start"]} + {spend["max_hours"]}h', spend['object'], ''])
    for f in report.get('stale_leases') or []:
        rows.append(['lease', f.get('cluster'), f['namespace'], f.get('thread'), f.get('gpus'),
                     f'{f["lease_id"]} {f.get("state")} {f.get("gpus")}', '', '', flags_cell([f]), ''])
    for e in report.get('open_ledger') or []:
        exists = {True: 'it still exists', False: 'it is gone', None: 'not read'}[e.get('exists')]
        rows.append(['ledger', e.get('cluster'), e.get('namespace'), e.get('thread'), '', e.get('lease') or '-',
                     f'since {e.get("since") or "?"}', '',
                     f'open_{e["kind"]}: no down in {", ".join(e.get("sources") or [])}; {exists}', ''])
    lines = [head, '']
    if rows:
        lines.append('| ' + ' | '.join(COLUMNS) + ' |')
        lines.append('|' + '|'.join('---' for _ in COLUMNS) + '|')
        lines += ['| ' + ' | '.join(cell(v) for v in row) + ' |' for row in rows]
    else:
        lines.append('Nothing to report: no namespace of ours, no stale lease, no open ledger line.')
    errors = report.get('errors') or []
    if errors:
        lines.append('')
        lines += [f'Error: {e.get("cluster") or "-"}: {e.get("code")}: {e.get("message")}' for e in errors]
    return '\n'.join(lines) + '\n'


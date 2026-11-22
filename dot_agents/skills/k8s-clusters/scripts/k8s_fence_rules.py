"""The lease fence's rules: the policy render, the probe's Pods and verdict, the gate's moves, and the recorder.

Pure functions. No I/O, so every rule is checkable without a cluster. k8s-guard and the kind fixture
(test/kind-fence.sh) are the callers: the fixture runs the same probe Pods and the same verdict against a real
Kyverno v1.15.2, so the strings below are the engine's, not paraphrases. Standard library only. The reasons
are in references/k8s-guard.md, "The fence policy".
"""
import json
import re

POLICY = 'agent-lease-fence'
# The canary is its own policy: one always-enforcing rule in the main policy would make all of it enforce.
CANARY_POLICY = 'agent-lease-fence-canary'
# The gate the policy reads, in the pre-made namespace. Never deleted; written only by the guard as the bot.
FENCE_RECORD = 'wcep-agent-fence'
# Ops set this label on the pre-made namespace to enroll it; the bot cannot label a Namespace.
FENCE_LABEL = 'dynamo.nvidia.com/agent-lease-fence'
PROBE_LABEL = 'wcep.agent/fence-probe'
LEASE_LABEL = 'wcep.agent/lease'
# A lease id the plugin can never issue (it issues lse_ and hex), so the mode Pod can never match a gate.
PROBE_LEASE = 'lse_fenceprobe0'
CANARY_RULE = 'probe-canary'
POD_RULE = 'pods-need-the-open-lease'
ROOT_RULE = 'bot-roots-need-the-open-lease-and-an-attempt'
GATE_RULE = 'gate-is-the-bots-and-moves-forward'
# The per-policy webhook entry a policy with webhookConfiguration.matchConditions gets, and the shared one.
FINEGRAINED_WEBHOOK = 'validate.kyverno.svc-fail-finegrained-' + POLICY
CANARY_WEBHOOK = 'validate.kyverno.svc-fail-finegrained-' + CANARY_POLICY
SHARED_WEBHOOK = 'validate.kyverno.svc-fail'
# Why both probe Pods can be admitted. The guard cannot read the kyverno namespace, so it names every cause.
DISARMED_CAUSE = ('no fence webhook answered: the policies or the namespace label are gone, or Kyverno has no '
                  'webhook entries (it deletes them all when its admission controller is scaled to 0 replicas, and '
                  'empties them after a restart more than 100 s away)')
GATE_STATES = ('active', 'closing')
PROBE_IMAGE = 'registry.k8s.io/pause:3.10'

PLACEHOLDERS = ('<premade>', '<prefix>', '<bot user>', '<owner>', '<contact>', '<runbook section>')
DNS_LABEL = re.compile(r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$')
BOT_USER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9@._:-]{0,252}$')
# Owner, contact and runbook land in YAML folded scalars and single-quoted titles: no quote, brace,
# angle bracket, colon-space, '#' or newline.
FREE_TEXT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 @._/()+,-]{0,119}$')


class FenceError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# --- the policy --------------------------------------------------------------------

def render_fence_policy(template, premade, prefix, bot_user, owner, contact, runbook):
    """The ClusterPolicy text for one cluster: the template with each placeholder filled, checked first."""
    if not (isinstance(prefix, str) and prefix.endswith('-') and DNS_LABEL.match(prefix[:-1])):
        raise FenceError('usage', f'prefix {prefix!r} must be a namespace prefix ending in "-"')
    if not (isinstance(premade, str) and DNS_LABEL.match(premade) and len(premade) <= 63
            and premade.startswith(prefix)):
        raise FenceError('usage', f'pre-made namespace {premade!r} must be a namespace name under {prefix}*')
    if not (isinstance(bot_user, str) and BOT_USER.match(bot_user)):
        raise FenceError('usage', f'bot user {bot_user!r} is not a Kubernetes user name')
    for what, value in (('owner', owner), ('contact', contact), ('runbook section', runbook)):
        if not (isinstance(value, str) and FREE_TEXT.match(value)):
            raise FenceError('usage', f'{what} {value!r}: letters, digits, spaces and @._/()+,- only, at most 120')
    for p in PLACEHOLDERS:
        if p not in template:
            raise FenceError('template_invalid', f'the template has no {p}')
    text = template
    for p, value in zip(PLACEHOLDERS, (premade, prefix, bot_user, owner, contact, runbook)):
        text = text.replace(p, value)
    left = re.findall(r'<(premade|prefix|bot user|owner|contact|runbook section)>', text)
    if left:
        raise FenceError('template_invalid', f'placeholders left after the render: {left}')
    return text


# --- the probe -------------------------------------------------------------------------

def probe_pod(which, ns):
    """One of the probe's two Pods, sent only as a server dry run: no GPU, tiny requests, never runs."""
    if which not in ('canary', 'mode'):
        raise ValueError(which)
    labels = {PROBE_LABEL: which}
    if which == 'mode':
        labels[LEASE_LABEL] = PROBE_LEASE
    return {'apiVersion': 'v1', 'kind': 'Pod',
            'metadata': {'name': f'wcep-fence-probe-{which}', 'namespace': ns, 'labels': labels},
            'spec': {'restartPolicy': 'Never', 'automountServiceAccountToken': False,
                     'containers': [{'name': 'probe', 'image': PROBE_IMAGE,
                                     'resources': {'requests': {'cpu': '1m', 'memory': '4Mi'},
                                                   'limits': {'cpu': '1m', 'memory': '4Mi'}}}]}}


def _denied_by(err, webhook):
    return f'admission webhook "{webhook}" denied the request' in err


def _names_rule(err, policy, rule):
    # Kyverno lists the failed rules under the policy name: "<policy>:\n  <rule>: <message>".
    return re.search(rf'(^|\n){re.escape(policy)}:\s*\n(\s+\S.*\n)*?\s+{re.escape(rule)}:', err + '\n') is not None


def _engine_down(err):
    """A refusal because our entry is in place and Kyverno cannot answer: either entry, whichever was called first."""
    return any(f'failed calling webhook "{w}"' in err for w in (FINEGRAINED_WEBHOOK, CANARY_WEBHOOK))


def _clip(text, n=400):
    return re.sub(r'\s+', ' ', str(text)).strip()[:n]


def canary_verdict(rc, err):
    """finegrained, shared, engine_unavailable, disarmed or probe_failed, and the text it matched.

    Only the canary policy's own entry refusing with its rule is finegrained. A refusal by anything else (another
    webhook, another policy or rule, Forbidden) proves nothing: probe_failed.
    """
    if rc == 0:
        return 'disarmed', 'the canary Pod was admitted'
    if _denied_by(err, CANARY_WEBHOOK) and _names_rule(err, CANARY_POLICY, CANARY_RULE):
        return 'finegrained', _clip(err)
    if _denied_by(err, SHARED_WEBHOOK) and _names_rule(err, CANARY_POLICY, CANARY_RULE):
        return 'shared', _clip(err)
    if _engine_down(err):
        return 'engine_unavailable', _clip(err)
    return 'probe_failed', _clip(err)


def mode_verdict(rc, err):
    """(mode, entry, text): enforcing, audit, absent, engine_unavailable or probe_failed, and which entry refused.

    The main policy refusing the mode Pod through its own entry is the proof that it is in place and enforces.
    Admitted with Kyverno's warning naming the Pod rule is audit; admitted without it, absent.
    """
    if rc == 0:
        if f'policy {POLICY}.{POD_RULE}:' in err:
            return 'audit', None, _clip(err)
        return 'absent', None, _clip(err) or 'the mode Pod was admitted with no warning from the policy'
    for entry, name in ((FINEGRAINED_WEBHOOK, 'finegrained'), (SHARED_WEBHOOK, 'shared')):
        if _denied_by(err, entry) and _names_rule(err, POLICY, POD_RULE):
            return 'enforcing', name, _clip(err)
    if _engine_down(err):
        return 'engine_unavailable', 'finegrained', _clip(err)
    return 'probe_failed', None, _clip(err)


def fence_verdict(canary_rc, canary_err, mode_rc, mode_err):
    """The probe's verdict from the two dry-run answers (exit code and stderr of each)."""
    canary, canary_text = canary_verdict(canary_rc, canary_err)
    mode, entry, mode_text = mode_verdict(mode_rc, mode_err)
    if entry is not None:
        scope = entry
    else:
        scope = {'finegrained': 'finegrained', 'engine_unavailable': 'finegrained', 'shared': 'shared'}.get(canary,
                                                                                                           'unknown')
    verdict = {'canary': canary, 'mode': mode, 'scope': scope, 'mode_entry': entry, 'canary_text': canary_text,
               'mode_text': mode_text}
    verdict['armed'] = armed(verdict)
    if not verdict['armed']:
        verdict['cause'] = cause(verdict)
    return verdict


def armed(verdict):
    """Armed: both entries answered (or are in place with the engine down) and the main one refuses the mode Pod."""
    return (verdict.get('canary') in ('finegrained', 'engine_unavailable')
            and (verdict.get('mode') == 'engine_unavailable'
                 or (verdict.get('mode') == 'enforcing' and verdict.get('mode_entry') == 'finegrained')))


def cause(verdict):
    """Why the fence is not armed, in words an operator can act on: a fence state, not a policy bug."""
    canary, mode = verdict.get('canary'), verdict.get('mode')
    if canary == 'disarmed' and mode in ('absent', 'audit'):
        return DISARMED_CAUSE if mode == 'absent' else 'the canary policy is gone; the main policy is in Audit'
    if canary == 'shared' or verdict.get('mode_entry') == 'shared':
        return 'a policy answered through the shared webhook: its matchConditions are lost, so it is not scoped'
    if mode == 'audit':
        return 'the policy is in Audit: it records but does not deny'
    if mode == 'absent':
        return 'the main policy did not answer: it is gone or its entry is missing'
    if canary == 'disarmed':
        return 'the canary policy did not answer: it is gone or its entry is missing'
    return 'a probe was refused for a reason that proves nothing (see the texts)'


# --- the gate ----------------------------------------------------------------------------

def gate_of(cm):
    """The gate's fields from its ConfigMap, or None when it is missing. A seq that is not a decimal number
    reads as None, so no move is planned over it."""
    if cm is None:
        return None
    data = cm.get('data') or {}
    seq = data.get('seq')
    try:
        number = int(seq) if isinstance(seq, str) and re.match(r'^[0-9]{1,18}$', seq) else None
    except ValueError:
        number = None
    return {'lease_id': data.get('lease_id') or '', 'state': data.get('state') or '', 'seq': number,
            'uid': data.get('uid') or '', 'meta': cm.get('metadata') or {}}


def gate_data(lease_id, state, seq, uid):
    return {'lease_id': lease_id, 'state': state, 'seq': str(int(seq)), 'uid': uid}


def gate_object(ns, lease_id, state, seq, uid):
    return {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': FENCE_RECORD, 'namespace': ns},
            'data': gate_data(lease_id, state, seq, uid)}


def _esc(key):
    return key.replace('~', '~0').replace('/', '~1')


def gate_patch(live, lease_id, state, seq, uid):
    """A JSON Patch that moves the gate only if it is still exactly what was read."""
    meta = live.get('metadata') or {}
    data = live.get('data') or {}
    ops = [{'op': 'test', 'path': '/metadata/uid', 'value': meta.get('uid')},
           {'op': 'test', 'path': '/metadata/resourceVersion', 'value': meta.get('resourceVersion')}]
    for key in ('seq', 'lease_id', 'state'):
        ops.append({'op': 'test', 'path': f'/data/{_esc(key)}', 'value': data.get(key)})
    ops.append({'op': 'replace' if 'data' in live else 'add', 'path': '/data',
                'value': gate_data(lease_id, state, seq, uid)})
    return ops


def plan_open(gate, lease_id, seq):
    """What `ns claim` does to open the gate for (lease_id, seq): create, patch, nothing, or refuse.

    Returns ('create'|'patch'|'open'|'closing', reason). 'open': it already reads active(L, s). 'closing': it
    reads closing(L, s): teardown came first, so the claim writes nothing.
    """
    if gate is None:
        return 'create', 'no gate yet'
    if gate['seq'] is None:
        raise FenceError('fence_mismatch', f'{FENCE_RECORD} holds a seq that is not a number; ops must look')
    if gate['seq'] > seq:
        raise FenceError('fence_mismatch', f'{FENCE_RECORD} is at seq {gate["seq"]}, above this claim\'s {seq}: '
                                           'a later claim holds the namespace')
    if gate['seq'] == seq:
        if gate['lease_id'] != lease_id:
            raise FenceError('fence_mismatch', f'{FENCE_RECORD} names lease {gate["lease_id"]!r} at this claim\'s '
                                               f'seq {seq}, not {lease_id}')
        if gate['state'] == 'active':
            return 'open', 'already open'
        return 'closing', 'teardown closed the gate first'
    return 'patch', f'moves the gate from seq {gate["seq"]} to {seq}'


def plan_close(gate, lease_id, seq):
    """What teardown's step 0 does: write closing(L, s) over anything below s, or leave closing(L, s)."""
    if gate is None:
        return 'create', 'no gate: the claim never opened it'
    if gate['seq'] is None:
        raise FenceError('fence_mismatch', f'{FENCE_RECORD} holds a seq that is not a number; ops must look')
    if gate['seq'] > seq:
        raise FenceError('fence_mismatch', f'{FENCE_RECORD} is at seq {gate["seq"]}, above this lease\'s {seq}')
    if gate['seq'] == seq:
        if gate['lease_id'] != lease_id:
            raise FenceError('fence_mismatch', f'{FENCE_RECORD} names lease {gate["lease_id"]!r} at seq {seq}, '
                                               f'not {lease_id}')
        if gate['state'] == 'closing':
            return 'closed', 'already closing'
    return 'patch', f'closes the gate at seq {seq}'


def gate_reads(gate, lease_id, seq, state):
    return (gate is not None and gate['lease_id'] == lease_id and gate['seq'] == seq and gate['state'] == state)


# --- trial claims ---------------------------------------------------------------------------

def trial_admit(components_of, objects):
    """Under a trial claim no Pod may request a GPU: refuse any DGD whose components ask for one.

    `components_of` is k8s_guard_rules.components, passed in so this module stays standalone.
    """
    for obj in objects:
        if obj.get('kind') != 'DynamoGraphDeployment':
            continue
        for c in components_of(obj):
            if c.per_pod:
                raise FenceError('trial_gpu_refused', f'{c.where} requests {c.per_pod} GPU(s) per pod; a trial claim '
                                                      'runs only 0-GPU work (a mocker), because the fence is in Audit')


# --- the recorder --------------------------------------------------------------------------

def _probe_object(labels):
    return bool((labels or {}).get(PROBE_LABEL))


def record_rows(reports, events, probe_names=()):
    """Rows for the audit window, from v1.15.2 PolicyReports and Kyverno Events, only fields they hold.

    Rows of the canary policy are left out: it refuses only probes.

    A report keeps the latest result per policy, rule and resource, and no user or operation; an Event names
    the involved object, its reason and message. Probe objects (by label, by name, or by rule probe-canary) are
    left out. Each row carries a key that is the same across runs, so the recorder appends only new rows.
    """
    rows = []
    for report in reports:
        scope = report.get('scope') or {}
        for result in report.get('results') or []:
            if result.get('policy') != POLICY:
                continue
            for res in (result.get('resources') or [scope]):
                if res.get('name') in probe_names:
                    continue
                ts = result.get('timestamp') or {}
                stamp = ts.get('seconds') if isinstance(ts, dict) else ts
                row = {'source': 'report', 'kind': res.get('kind'), 'name': res.get('name'), 'uid': res.get('uid'),
                       'rule': result.get('rule'), 'result': result.get('result'), 'time': stamp,
                       'message': _clip(result.get('message') or '', 300)}
                row['key'] = json.dumps(['report', row['uid'], row['rule'], row['result'], stamp])
                rows.append(row)
    for ev in events:
        message = ev.get('message') or ev.get('note') or ''
        if POLICY not in message or CANARY_POLICY in message or re.search(rf'\b{re.escape(CANARY_RULE)}\b', message):
            continue
        obj = ev.get('involvedObject') or ev.get('regarding') or {}
        if obj.get('name') in probe_names or (obj.get('kind') == 'ClusterPolicy' and
                                              any(n in message for n in probe_names)):
            continue
        first = ev.get('firstTimestamp') or ev.get('eventTime') or (ev.get('metadata') or {}).get('creationTimestamp')
        last = ev.get('lastTimestamp') or first
        row = {'source': 'event', 'kind': obj.get('kind'), 'name': obj.get('name'), 'uid': obj.get('uid'),
               'reason': ev.get('reason'), 'count': ev.get('count') or (ev.get('series') or {}).get('count') or 1,
               'first': first, 'last': last, 'message': _clip(message, 300)}
        row['key'] = json.dumps(['event', (ev.get('metadata') or {}).get('uid'), row['count'], last])
        rows.append(row)
    return rows


def probe_names():
    return tuple(probe_pod(w, 'x')['metadata']['name'] for w in ('canary', 'mode'))


def without_match_conditions(text):
    """See below; applied to every policy document in the text."""
    return '\n---\n'.join(_without_mc(doc) for doc in text.split('\n---\n'))


def _without_mc(text):
    """The rendered policy without its webhook match conditions, for the Kyverno CLI only.

    The v1.15.2 CLI panics on a policy that sets webhookConfiguration.matchConditions (it asks a nil cluster
    client for the server version), and it never evaluates them: the API server does. So the CLI suites run
    on this copy, and the kind fixture tests the match conditions on a real API server.
    """
    start = text.index('    matchConditions:\n')
    end = text.index('  rules:\n', start)
    return text[:start] + text[end:]

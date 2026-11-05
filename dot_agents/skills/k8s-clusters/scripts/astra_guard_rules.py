"""Pure rules for astra-guard: what a merge request may change, the inverse patch,
the helm:plan reader, the approval check and the ask text.

No function here does I/O. The reasons for each rule are in
references/astra-guard.md.
"""
import difflib
import re

import yaml

TARGET_BRANCH = 'main'

# The only files a merge request may change, by site. Everything else in the
# project, including every prd path and every template, is refused.
SITES = {
    'pdx04-dev': 'dynamo-workloads/values/pdx04-dev/values.yaml',
    'pdx04-stg': 'dynamo-workloads/values/pdx04-stg/values.yaml',
}
ZERO_GPU_SITES = ('pdx04-dev',)

# Our entries live only as items of these top-level workload lists. A `wcep-*`
# item anywhere else (secrets, pull secrets, routes) is not an entry.
ENTRY_LISTS = ('mockerRepros', 'vllmModels', 'sglangModels', 'trtllmModels', 'vllmDisaggModels',
               'sglangDisaggModels', 'stockVllmModels', 'nimModels')
OUR_PREFIX = 'wcep-'
ENTRY_NAME = re.compile(r'^wcep-[a-z0-9]+(?:-[a-z0-9]+)*$')
BRANCH = re.compile(r'^wcep/(?P<issue>[A-Z][A-Z0-9]*-[0-9]+)-[a-z0-9]+(?:-[a-z0-9]+)*$')
ISSUE = re.compile(r'^[A-Z][A-Z0-9]*-[0-9]+$')
SHA = re.compile(r'^[0-9a-f]{40}$')
ASK_ID = re.compile(r'^bbq_[A-Za-z0-9]{1,64}$')
REQUIRED_JOBS = ('helm:plan', 'validate:tests')
APPROVE = 'Approve'
OPTIONS = (APPROVE, 'Reject')
ASK_LIMIT = 4000
WINDOW_NOTE = ("GitLab's merge call checks the head but not main. A change to main in the last second "
               "before the merge is not caught until IT turns on fast-forward merges.")


class Refusal(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# -- names -------------------------------------------------------------------

def check_branch(name, issue=None):
    m = BRANCH.match(name or '')
    if not m or len(name) > 100:
        raise Refusal('branch_not_allowed', f'{name!r} is not a wcep/<ISSUE>-<purpose> branch')
    if issue is not None and m.group('issue') != issue:
        raise Refusal('branch_issue_mismatch', f'branch {name!r} does not name issue {issue}')
    return m.group('issue')


def site_of(path):
    for site, p in SITES.items():
        if p == path:
            return site
    raise Refusal('path_not_allowed', f'{path!r} is not one of the two values files')


# -- YAML --------------------------------------------------------------------

class _Loader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            raise Refusal('yaml_invalid', f'duplicate key {key!r} at line {key_node.start_mark.line + 1}')
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def load_values(text):
    """Parse one values file. Duplicate keys and multiple documents are refused,
    because Helm's parser and PyYAML would disagree on them."""
    try:
        docs = list(yaml.load_all(text, Loader=_Loader))
    except Refusal:
        raise
    except yaml.YAMLError as e:
        raise Refusal('yaml_invalid', f'values file does not parse: {str(e)[:200]}')
    if len(docs) != 1 or not isinstance(docs[0], dict):
        raise Refusal('yaml_invalid', 'a values file must be one YAML mapping')
    _json_shaped(docs[0], '')
    return docs[0]


def _json_shaped(obj, path):
    """Helm values are JSON. Anything else (a set, a timestamp, binary, a non-string key) is
    refused here, once, so no later comparison has to know how such a type compares."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise Refusal('yaml_invalid', f'{path or "/"} has a {type(k).__name__} key; values are JSON')
            _json_shaped(v, f'{path}.{k}')
    elif isinstance(obj, list):
        for n, v in enumerate(obj):
            _json_shaped(v, f'{path}[{n}]')
    elif not (obj is None or isinstance(obj, (str, bool, int, float))):
        raise Refusal('yaml_invalid', f'{path or "/"} is a {type(obj).__name__}; values are JSON')


def _is_ours(item):
    return isinstance(item, dict) and isinstance(item.get('name'), str) and item['name'].startswith(OUR_PREFIX)


def entries(doc):
    """Our entries: {(list key, name): entry} for `wcep-*` items of the workload lists."""
    out = {}
    for key in ENTRY_LISTS:
        items = doc.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if _is_ours(item):
                if (key, item['name']) in out:
                    raise Refusal('entry_name', f'entry {item["name"]!r} appears twice in {key}')
                out[(key, item['name'])] = item
    return out


def strip(doc):
    """The document without our entries. Two commits that differ only in our
    entries strip to equal documents."""
    out = dict(doc)
    for key in ENTRY_LISTS:
        if isinstance(out.get(key), list):
            out[key] = [i for i in out[key] if not _is_ours(i)]
    return out


def same(a, b):
    """Equality that also compares types, so `true` never equals `1` and `1` never equals `1.0`."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return {(type(k), k) for k in a} == {(type(k), k) for k in b} and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, float) and a != a:
        return b != b
    return a == b


def _walk(obj, path=()):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield path + (str(k),), k, v
            yield from _walk(v, path + (str(k),))
    elif isinstance(obj, list):
        for n, v in enumerate(obj):
            yield from _walk(v, path + (str(n),))


def _zero(v):
    return v is None or v is False or v == 0 or (isinstance(v, str) and v.strip() in ('', '0'))


_ACCELERATOR = re.compile(r'gpu|mig-|^(nvidia\.com|amd\.com|gpu\.intel\.com|habana\.ai)/', re.I)


def gpu_fields(entry):
    """Every field of an entry whose key names a GPU or another accelerator resource
    (MIG slices included), with a non-zero value."""
    return [('.'.join(p), v) for p, k, v in _walk(entry)
            if _ACCELERATOR.search(str(k)) and not isinstance(v, (dict, list)) and not _zero(v)] + \
           [('.'.join(p), '<structure>') for p, k, v in _walk(entry)
            if _ACCELERATOR.search(str(k)) and isinstance(v, (dict, list)) and v]


def classify(site, base_text, head_text, kind):
    """Check one values file's change against the rules. Returns the change.

    kind is 'deploy' or 'teardown', as recorded when the guard opened the merge request.
    """
    base, head = load_values(base_text), load_values(head_text)
    if not same(strip(base), strip(head)):
        raise Refusal('outside_entries', 'the change touches more than entries named wcep-* '
                                         f'in the workload lists ({", ".join(ENTRY_LISTS)})')
    old, new = entries(base), entries(head)
    added = sorted(k for k in new if k not in old)
    removed = sorted(k for k in old if k not in new)
    changed = sorted(k for k in new if k in old and not same(new[k], old[k]))
    for (_, name) in added + removed + changed:
        if not ENTRY_NAME.match(name) or name.endswith('-prd'):
            raise Refusal('entry_name', f'entry {name!r} is not a lower-case wcep-* name, or ends in -prd')
    if not (added or removed or changed):
        raise Refusal('no_entry_change', 'the change adds, changes and removes no wcep-* entry')
    if kind == 'any':  # a push: either a deploy or a teardown, checked again by kind at check
        kind = 'deploy' if added or changed else 'teardown'
    if kind == 'teardown':
        if added or changed:
            raise Refusal('teardown_not_removal', 'a teardown may only remove our entries')
    elif kind == 'deploy':
        if changed or removed:
            raise Refusal('deploy_not_addition', 'a deploy only adds wcep-* entries; to change one, tear it down '
                                                 f'and deploy again (changed {[k[1] for k in changed]}, '
                                                 f'removed {[k[1] for k in removed]})')
    else:
        raise Refusal('kind_unknown', f'unknown kind {kind!r}')
    gpus = {}
    for key in added + changed:
        fields = gpu_fields(new[key])
        if fields:
            # Keyed by values file, list and name: names are unique only within one list.
            gpus[f'{site} {key[0]}/{key[1]}'] = fields
    if site in ZERO_GPU_SITES and gpus:
        raise Refusal('dev_gpu', f'dev entries request no GPU; found {gpus}')
    return {'site': site, 'added': [k[1] for k in added], 'changed': [k[1] for k in changed],
            'removed': [k[1] for k in removed], 'gpus': gpus}


# -- the inverse patch -------------------------------------------------------

def inverse_patch(base_text, head_text, context=3):
    """Hunks that turn the head's file back into the base's: [{'old': [...], 'new': [...]}].

    'old' includes the context lines, so a hunk applies only where its text is
    found exactly once. Merges are squashed, so the teardown applies these to the
    landed main, never to the deploy branch.
    """
    a, b = head_text.splitlines(keepends=True), base_text.splitlines(keepends=True)
    hunks = []
    for group in difflib.SequenceMatcher(None, a, b, autojunk=False).get_grouped_opcodes(context):
        i1, i2 = group[0][1], group[-1][2]
        j1, j2 = group[0][3], group[-1][4]
        hunks.append({'old': a[i1:i2], 'new': b[j1:j2]})
    return hunks


def render_patch(base_text, head_text, path):
    return ''.join(difflib.unified_diff(head_text.splitlines(keepends=True), base_text.splitlines(keepends=True),
                                        fromfile=f'a/{path}', tofile=f'b/{path}', n=3))


def apply_patch(text, hunks):
    lines = text.splitlines(keepends=True)
    for n, h in enumerate(hunks):
        old, new = h['old'], h['new']
        if not old:
            raise Refusal('inverse_patch_conflict', f'hunk {n + 1} has no text to anchor it')
        at = [i for i in range(len(lines) - len(old) + 1) if lines[i:i + len(old)] == old]
        if len(at) != 1:
            raise Refusal('inverse_patch_conflict',
                          f'hunk {n + 1} matches {len(at)} places in the landed file; it must match one')
        lines[at[0]:at[0] + len(old)] = new
    return ''.join(lines)


# -- helm:plan ---------------------------------------------------------------

_ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
_PLAN = re.compile(r'^(?P<ns>[^,\s]*), (?P<name>[A-Za-z0-9][A-Za-z0-9._-]*), (?P<kind>[A-Za-z0-9]+) '
                   r'\([^)]*\) has (?P<action>been added|changed|been removed):\s*$')
_PLAN_ANY = re.compile(r'has (been added|changed|been removed)')
_HEADERISH = re.compile(r'^[^,]*, [^,]+, [A-Za-z0-9]+ \(')


def parse_plan(trace):
    """Resources named in a helm:plan job log, in helm-diff form:
    `<namespace>, <name>, <Kind> (<group>) has been added|changed|been removed:`.

    The format has not yet been read from a live job; when it differs, the
    namespace check fails closed with rendered_namespace_unverified.
    """
    out = []
    for raw in trace.replace('\r', '\n').splitlines():
        line = _ANSI.sub('', raw).strip()
        m = _PLAN.match(line)
        if not m and (_PLAN_ANY.search(line) or _HEADERISH.match(line)):
            raise Refusal('rendered_namespace_unverified', f'cannot read the helm:plan line {line[:120]!r}')
        if m:
            action = m.group('action').split()[-1]
            out.append((m.group('ns'), m.group('name'), m.group('kind'), action))
    return out


def check_plan(resources, names, namespace):
    """Every changed entry appears in the plan, and every resource in it is in `namespace`."""
    ours = [r for r in resources if r[1].startswith(OUR_PREFIX)]
    missing = [n for n in names if not any(r[1] == n or r[1].startswith(n + '-') for r in ours)]
    if not ours or missing:
        raise Refusal('rendered_namespace_unverified',
                      f'helm:plan names none of entries {missing or names}; cannot confirm the namespace')
    # Every resource, whatever its name: the plan diffs this merge request's base and head, so
    # each resource in it is one this merge request changes. A cluster-scoped one has no namespace.
    wrong = sorted({f'{r[1]} in {r[0] or "no namespace"}' for r in resources if r[0] != namespace})
    if wrong:
        raise Refusal('rendered_namespace_mismatch', f'helm:plan changes resources outside {namespace}: {wrong}')


def dig(doc, key):
    """Follow a dotted key; a list step picks the item whose `name` equals it."""
    cur = doc
    for part in key.split('.'):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list):
            hits = [i for i in cur if isinstance(i, dict) and i.get('name') == part]
            if len(hits) != 1:
                return None
            cur = hits[0]
        else:
            return None
    return cur


# -- the approval ------------------------------------------------------------

def _has_token(text, token, boundary):
    return re.search(rf'(?<!{boundary}){re.escape(token)}(?!{boundary})', text) is not None


def check_answer(record, thread, iid, head, target):
    """The conditions of the answer-of contract, and only those.

    The question must name the merge request as `!<iid>` and both commits in full,
    each as a whole token, so `!12` never matches `!123` and a short prefix never
    matches a commit.
    """
    if not isinstance(record, dict):
        raise Refusal('answer_invalid', 'answer-of did not print a JSON object')
    if record.get('status') != 'answered':
        raise Refusal('answer_not_answered', f'the question is {record.get("status")!r}, not answered')
    if record.get('channel') != 'bb':
        raise Refusal('answer_channel', f'the answer came from {record.get("channel")!r}, not bb')
    option = record.get('option')
    if not isinstance(option, dict) or option.get('text') != APPROVE:
        raise Refusal('answer_not_approve', 'the answer is not the option Approve')
    if not thread or record.get('threadId') != thread:
        raise Refusal('answer_thread', 'the question was asked by another thread')
    q = record.get('question')
    if not isinstance(q, str) or not (_has_token(q, f'!{iid}', r'[0-9]') and _has_token(q, head, r'[0-9a-f]')
                                      and _has_token(q, target, r'[0-9a-f]')):
        raise Refusal('answer_mismatch', f'the approved question does not name !{iid}, head {head} '
                                         f'and target {target}')


def answer_summary(record):
    if not isinstance(record, dict):
        return None
    return {k: record.get(k) for k in ('status', 'channel', 'option', 'answeredAt')}


# -- the ask -----------------------------------------------------------------

def ask_text(*, iid, web_url, head, target, kind, cluster, site, namespace, shared, change, expires, patch):
    def names(label):
        return [f'{n} ({label})' for n in change[label]]
    entries_line = ', '.join(names('added') + names('changed') + names('removed'))
    if change['gpus']:
        gpus = 'GPUs (fields as written, per pod, replicas not multiplied): ' + '; '.join(
            f'{n}: ' + ', '.join(f'{p}={v}' for p, v in f) for n, f in sorted(change['gpus'].items()))
    else:
        gpus = 'GPUs: 0'
    lines = [
        f'Merge request !{iid}: {web_url}',
        f'Head commit: {head}',
        f'Target main commit: {target}',
        f'Window: {WINDOW_NOTE}',
        'Approve merges exactly this head onto exactly this main. Any push to the branch or to main voids it.',
        f'Kind: {kind}',
        f'Cluster: {cluster} ({site})',
        f'Namespace: {namespace}',
    ]
    if shared:
        lines.append(f"This is IT's shared namespace: approving puts our entries in {namespace}, next to IT's "
                     'workloads.')
    lines += [f'Entries: {entries_line}', gpus]
    if kind == 'deploy':
        lines += [f'Expires: {expires}', 'Checks passed on the head: helm:plan, validate:tests.',
                  'Inverse patch (the teardown applies it to the landed main):', patch.rstrip('\n')]
    else:
        lines += ['Checks passed on the head: helm:plan, validate:tests.']
    text = '\n'.join(lines) + '\n'
    if len(text) > ASK_LIMIT:
        raise Refusal('ask_too_long', f'the ask is {len(text)} characters; bb sgc ask takes {ASK_LIMIT}')
    return text

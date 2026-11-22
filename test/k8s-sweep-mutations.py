"""Mutation check for k8s-sweep: each risky branch broken in turn, the whole sweep suite run against it.

Run after a change to k8s_sweep_rules.py or k8s-sweep, from the repository root:

    timeout 1500 python3 test/k8s-sweep-mutations.py </dev/null

Each mutation replaces one line or block that must occur exactly once (SKIP otherwise: update the
pattern). It prints CAUGHT or SURVIVED for each and exits 1 if any survived or was skipped. A
survivor is a branch no test needs: add a test, or delete the branch. The files are restored,
byte for byte, after every run and on any exit. Use the system python3: the suite needs
os.memfd_create, which uv's managed Python 3.14 lacks.
"""
import os
from pathlib import Path
import subprocess
import sys

D = str(Path(__file__).resolve().parents[1])
R = f'{D}/dot_agents/skills/k8s-clusters/scripts/k8s_sweep_rules.py'
S = f'{D}/dot_agents/skills/k8s-clusters/scripts/executable_k8s-sweep'
M = [
 ('decide: claimed check', R, "    if row['claimed']:\n        return none(", "    if False:\n        return none("),
 ('decide: read_failed check', R, "    if row.get('read_failed'):", "    if False:"),
 ('decide: terminating check', R, "    if row['terminating']:", "    if False:"),
 ('decide: expiry boundary < to <=', R, "    if now < when:", "    if now <= when:"),
 ('decide: unknown uid check', R, "    if known_uid is None:\n", "    if False:\n"),
 ('decide: grace >= to >', R, "    if now >= when + grace:", "    if now > when + grace:"),
 ('decide: expire and down swapped', R, "        return {'action': 'down', 'reason'", "        return {'action': 'expire', 'reason'"),
 ('decide: unreadable flag dropped', R, "is not an RFC 3339 time with a zone', 'expiry_unreadable')", "is not an RFC 3339 time with a zone')"),
 ('parse_time: naive time accepted', R, "    return parsed if parsed.tzinfo else None", "    return parsed if parsed.tzinfo else parsed.replace(tzinfo=__import__('datetime').timezone.utc)"),
 ('match: revoking lease covers GPUs', R, "        if gpus and not active:", "        if gpus and not same:"),
 ('match: over-lease > to >=', R, "gpus > active[0]['gpus']:", "gpus >= active[0]['gpus']:"),
 ('match: lease matched without UID', R, "if l['namespace'] == r['namespace'] and l['uid'] == r['uid']]", "if l['namespace'] == r['namespace']]"),
 ('match: other clusters leases count', R, "    mine = [l for l in leases if l.get('cluster') == cluster]", "    mine = list(leases)"),
 ('match: ended leases are live', R, "    live = [l for l in mine if l.get('state') in LIVE]", "    live = list(mine)"),
 ('match: unread name judged gone', R, "        elif l['namespace'] in exists:\n            uid = exists[l['namespace']]", "        elif True:\n            uid = exists.get(l['namespace'])"),
 ('match: same-UID lookup still flagged', R, "            if uid == l['uid']:\n                continue\n            why = 'not found'", "            why = 'not found'"),
 ('match: unread bb list read as empty', R, "    if leases is None:\n        return []", "    if leases is None:\n        leases = []"),
 ('row: finished pods counted', R, "        if (p.get('status') or {}).get('phase') in FINISHED:\n            continue", "        pass"),
 ('row: every GPU pod an orphan', R, "        if gpus and (pmeta.get('labels') or {}).get(DGD_NAME_LABEL) not in owned:", "        if gpus:"),
 ('row: reaper label ignored', R, "        marks = {**(pmeta.get('labels') or {}), **(pmeta.get('annotations') or {})}", "        marks = pmeta.get('annotations') or {}"),
 ('row: record expiry without UID match', R, "if record and data.get('uid') == meta.get('uid') else None", "if record else None"),
 ('row: claimed namespace keeps expiry', R, "           'expires_at': None if claimed else effective_expiry(ns, record),", "           'expires_at': effective_expiry(ns, record),"),
 ('ledger: down without deleted closes', R, "            elif direction == 'down' and line.get('deleted'):", "            elif direction == 'down':"),
 ('ledger: any claim down closes', R, "        elif obj == f'Claim/{ns}' and lease:", "        elif obj == f'Claim/{ns}':"),
 ('ledger: ended leases ignored', R, "        if e['kind'] == 'claim' and e['lease'] in ended:\n            continue", "        pass"),
 ('ledger: no dedupe', R, "        if key in seen:", "        if False:"),
 ('created_uids: any up line', R, "        if line['object'] == f'Namespace/{ns}' and line.get('direction') == 'up' and line.get('created'):", "        if line['object'] == f'Namespace/{ns}' and line.get('direction') == 'up':"),
 ('digest: no pipe escape', R, ".replace('|', '\\\\|')", ""),
 ('bb_env: server-url ignored', R, "        if url:\n            env['BB_SERVER_URL'] = url", "        pass"),
 ('script: Reader allows any verb', S, "        if args[0] != 'get' and tuple(args[:2]) != ('auth', 'whoami'):", "        if False:"),
 ('script: lookup without name check', S, "        if DNS_LABEL.match(name) and len(name) <= 63:", "        if True:"),
 ('script: envelope taken as leases', S, "    if p.returncode != 0 or not isinstance(body, list):", "    if p.returncode != 0 and not isinstance(body, dict):"),
 ('script: not under prefix kept', S, "if ((n.get('metadata') or {}).get('name') or '').startswith(prefix)]", "]"),
 ('script: lease UID fallback dropped', S, "    uids = {l['uid'] for l in leases or []", "    uids = {l['uid'] for l in []"),
 ('script: read-only cluster enforced', S, "        elif not prof.get('bot_can_write'):", "        elif False:"),
 ('script: premade record read', S, "        record = None if name == premade else reader.json(", "        record = reader.json("),
 ('script: guard failure not an error', S, "            if result in GUARD_FAILURES:", "            if False:"),
 ('script: no sweep ledger line', S, "    record({'time': now.strftime", "    (lambda *a: None)({'time': now.strftime"),
 ('script: identity check skipped', S, "    reader.verify_bot(bot)\n", "    pass\n"),
 ('script: identity never refused', S, "        if name != bot['user']:", "        if False:"),
 ('script: kubectl_error not a failure', S, "'guard_timeout', 'internal_error', 'kubectl_error')", "'guard_timeout', 'internal_error')"),
 ('script: internal_error not a failure', S, "'guard_timeout', 'internal_error', 'kubectl_error')", "'guard_timeout', 'kubectl_error')"),
 ('script: guard_timeout not a failure', S, "('guard_no_json', 'guard_timeout', 'internal_error',", "('guard_no_json', 'internal_error',"),
 ('script: non-zero exit without code is ok', S, "        elif p.returncode == 0 and 'code' not in body:", "        elif 'code' not in body:"),
]
orig = {p: open(p, 'rb').read() for p in (R, S)}
bad = 0
try:
    for name, path, old, new in M:
        s = orig[path].decode()
        if s.count(old) != 1:
            print(f'SKIP (pattern x{s.count(old)}): {name}', flush=True)
            bad += 1
            continue
        open(path, 'w').write(s.replace(old, new))
        p = subprocess.run(['timeout', '300', sys.executable, f'{D}/test/test-k8s-sweep.py'], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        last = (p.stderr.strip().splitlines() or ['?'])[-1]
        print(f'{"CAUGHT" if p.returncode else "SURVIVED"}: {name}  [{last}]', flush=True)
        bad += p.returncode == 0
        open(path, 'wb').write(orig[path])
finally:
    for p, b in orig.items():
        open(p, 'wb').write(b)
    restored = all(open(p, 'rb').read() == b for p, b in orig.items())
    print('restored' if restored else 'NOT RESTORED: check git diff', flush=True)
sys.exit(1 if bad or not restored else 0)

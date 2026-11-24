"""k8s-sweep: the read-only report of our cluster use, and the --enforce expiry backstop.

Two layers, both on made-up names:

- the rules (k8s_sweep_rules.py) are pure functions: parsed kubectl JSON, `bb k8s leases`
  rows and ledger lines in; rows, flags, decisions and the digest table out;
- the script runs against the same fakes as test-k8s-guard.py: a stateful fake `kubectl` at
  the pinned path, a fake `bb` holding the leases, and, for --enforce, the real k8s-guard.
  It never contacts a cluster.

The systemd user units are rendered with chezmoi from placeholder values, as in test-k8s-tbot.py.
"""
from datetime import datetime, timedelta, timezone
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'dot_agents/skills/k8s-clusters/scripts'
SWEEP = SCRIPTS / 'executable_k8s-sweep'
sys.dont_write_bytecode = True
sys.path.insert(0, str(SCRIPTS))
import k8s_sweep_rules as rules  # noqa: E402


def load_guard_tests():
    """test-k8s-guard.py as a module: its fakes and its world, so both suites share one fake."""
    path = ROOT / 'test/test-k8s-guard.py'
    loader = importlib.machinery.SourceFileLoader('guard_tests', str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader('guard_tests', loader))
    loader.exec_module(module)
    return module


G = load_guard_tests()
CTX, PROFILE, NS, PREFIX, ME, OTHER = G.CTX, G.PROFILE, G.NS, G.PREFIX, G.ME, G.OTHER
GUARD = G.GUARD
NOW = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
WRITE_VERBS = ('create', 'apply', 'patch', 'replace', 'delete', 'label', 'annotate', 'scale', 'edit', 'set',
               'rollout', 'drain', 'cordon', 'taint', 'exec', 'cp', 'run', 'expose', 'autoscale')


def at(**kw):
    return (NOW + timedelta(**kw)).strftime('%Y-%m-%dT%H:%M:%SZ')


# --- made-up cluster objects for the pure rules --------------------------------------------

def ns_obj(name=NS, uid='uid-ns-1', thread=ME, expires=None, labels=None, created=None, terminating=False):
    meta = {'name': name, 'uid': uid, 'creationTimestamp': created or at(hours=-3),
            'labels': labels if labels is not None else {
                'dynamo.nvidia.com/developer': 'alice', 'dynamo.nvidia.com/developer-agent-rbac': 'true',
                'wcep.agent/thread': thread},
            'annotations': {'wcep.agent/expires-at': expires or at(hours=1)} if expires is not False else {}}
    if terminating:
        meta['deletionTimestamp'] = at(minutes=-5)
    return {'kind': 'Namespace', 'metadata': meta}


def record(lease_id='lse_test0001', uid='uid-ns-1', expires=None):
    return {'kind': 'ConfigMap', 'metadata': {'name': 'wcep-agent-lease'},
            'data': {'lease_id': lease_id, 'uid': uid, 'expires_at': expires or at(hours=1), 'thread': ME}}


def gpu_dgd(name='w', gpus=2, replicas=1):
    return {'apiVersion': 'nvidia.com/v1beta1', 'kind': 'DynamoGraphDeployment', 'metadata': {'name': name},
            'spec': {'components': [
                {'name': 'decode', 'type': 'worker', 'replicas': replicas,
                 'podTemplate': {'spec': {'containers': [G.container(gpus)]}}}]}}


def gpu_pod(dgd='w', gpus=2, name=None, phase='Running', age_hours=1, annotations=None, labels=None):
    lab = {G.DGD_NAME_LABEL: dgd} if dgd else {}
    lab.update(labels or {})
    return {'metadata': {'name': name or f'{dgd or "bare"}-{gpus}', 'labels': lab,
                         'annotations': annotations or {}, 'creationTimestamp': at(hours=-age_hours)},
            'spec': {'containers': [G.container(gpus)]}, 'status': {'phase': phase}}


def lease_row(lease_id='lse_test0001', ns=NS, uid='uid-ns-1', gpus=4, state='active', cluster=PROFILE,
              thread=ME, mode='created'):
    return {'lease_id': lease_id, 'state': state, 'cluster': cluster, 'namespace': ns, 'uid': uid,
            'thread': thread, 'gpu_type': 'gb300', 'gpus': gpus, 'expires_at': at(hours=1), 'mode': mode}


def kinds(flags):
    return sorted(f['kind'] for f in flags)


class Rows(unittest.TestCase):
    """What the report says about one namespace."""

    def row(self, ns=None, rec=None, dgds=(), pods=(), premade=None):
        return rules.namespace_row(ns or ns_obj(), rec, list(dgds), list(pods), NOW, premade)

    def test_row_fields(self):
        warned = {'gpu-reaper.nvidia.com/state': 'warned'}
        r = self.row(rec=record(), dgds=[gpu_dgd(gpus=2, replicas=2)],
                     pods=[gpu_pod(name='w-0', age_hours=5, annotations=warned), gpu_pod(name='w-1', age_hours=2)])
        self.assertEqual((r['namespace'], r['uid'], r['thread']), (NS, 'uid-ns-1', ME))
        self.assertEqual(r['age_hours'], 3.0)
        self.assertEqual(r['gpus'], 4)  # the guard's allocation: the larger of spec and pods
        self.assertEqual(r['pod_gpus'], 4)
        self.assertEqual(r['oldest_pod_hours'], 5.0)
        self.assertEqual(r['reaper_warned'], 1)
        self.assertEqual(r['lease_record'], 'lse_test0001')
        self.assertFalse(r['claimed'])
        self.assertEqual(r['orphan_gpu_pods'], [])

    def test_reaper_warning_as_a_label_counts_too(self):
        r = self.row(pods=[gpu_pod(labels={'gpu-reaper.nvidia.com/state': 'warned'})], dgds=[gpu_dgd()])
        self.assertEqual(r['reaper_warned'], 1)

    def test_spec_counts_when_pods_are_not_up_yet(self):
        self.assertEqual(self.row(dgds=[gpu_dgd(gpus=4)])['gpus'], 4)

    def test_finished_pods_hold_nothing(self):
        r = self.row(pods=[gpu_pod(dgd=None, phase='Succeeded'), gpu_pod(dgd=None, name='f', phase='Failed')])
        self.assertEqual((r['gpus'], r['orphan_gpu_pods']), (0, []))

    def test_finished_pods_do_not_age_or_warn(self):
        warned = {'gpu-reaper.nvidia.com/state': 'warned'}
        r = self.row(pods=[gpu_pod(dgd=None, phase='Succeeded', age_hours=50, annotations=warned)])
        self.assertEqual((r['oldest_pod_hours'], r['reaper_warned']), (None, 0))

    def test_premade_namespace_keeps_no_expiry_even_with_an_annotation(self):
        r = self.row(ns_obj(PREFIX + 'agent', expires=at(days=-1)), rec=record(expires=at(days=-1)),
                     premade=PREFIX + 'agent')
        self.assertIsNone(r['expires_at'])

    def test_orphan_gpu_pod(self):
        # No DGD label, or a label naming a DGD that is not there: nothing the guard applied owns it.
        r = self.row(dgds=[gpu_dgd('w')], pods=[gpu_pod('w', name='w-0'), gpu_pod(None, name='bare-0'),
                                                gpu_pod('gone', name='gone-0'), gpu_pod(None, gpus=0, name='cpu')])
        self.assertEqual(r['orphan_gpu_pods'], ['bare-0', 'gone-0'])
        self.assertEqual(r['gpus'], 2 + 2 + 2)

    def test_a_gpu_form_the_guard_cannot_count_is_a_flag_not_a_crash(self):
        p = gpu_pod(None)
        p['spec']['containers'][0]['resources']['requests']['nvidia.com/gpu'] = 'many'
        r = self.row(pods=[p])
        self.assertIsNone(r['gpus'])
        self.assertIn('count_failed', kinds(r['flags']))

    def test_expiry_follows_the_guard(self):
        # The record's expiry counts when its uid is this namespace's, even when it is earlier.
        r = self.row(ns_obj(expires=at(hours=3)), rec=record(expires=at(hours=1)))
        self.assertEqual(r['expires_at'], at(hours=1))
        # A record written for another instance does not count: the annotation does.
        r = self.row(ns_obj(expires=at(hours=3)), rec=record(uid='uid-other', expires=at(hours=9)))
        self.assertEqual(r['expires_at'], at(hours=3))
        self.assertIsNone(self.row(ns_obj(expires=False))['expires_at'])

    def test_premade_namespace_is_claimed(self):
        r = self.row(ns_obj(PREFIX + 'agent', expires=False), premade=PREFIX + 'agent')
        self.assertTrue(r['claimed'])

    def test_terminating(self):
        self.assertTrue(self.row(ns_obj(terminating=True))['terminating'])


class Matching(unittest.TestCase):
    """Rows against `bb k8s leases`."""

    def match(self, rows, leases, exists=None, available=True):
        return rules.match_leases(PROFILE, rows, leases if available else None, exists or {})

    def row(self, gpus=4, uid='uid-ns-1', ns=NS):
        pods = [gpu_pod(name=f'w-{i}', gpus=1) for i in range(gpus)]
        return rules.namespace_row(ns_obj(ns, uid), None, [gpu_dgd(gpus=1, replicas=gpus)] if gpus else [], pods,
                                   NOW, None)

    def test_matching_lease_no_flag(self):
        rows = [self.row(4)]
        self.assertEqual(self.match(rows, [lease_row(gpus=4)]), [])
        self.assertEqual(rows[0]['lease'], {'lease_id': 'lse_test0001', 'state': 'active', 'gpus': 4})

    def test_gpus_with_no_lease(self):
        flags = self.match([self.row(2)], [])
        self.assertEqual(kinds(flags), ['gpus_no_lease'])
        self.assertEqual(flags[0]['namespace'], NS)

    def test_a_lease_that_is_not_active_does_not_cover_gpus(self):
        flags = self.match([self.row(2)], [lease_row(state='revoking')])
        self.assertEqual(kinds(flags), ['gpus_no_lease'])
        self.assertIn('revoking', flags[0]['detail'])

    def test_a_lease_on_another_cluster_does_not_cover_gpus(self):
        self.assertEqual(kinds(self.match([self.row(2)], [lease_row(cluster='lab-b200')])), ['gpus_no_lease'])

    def test_no_gpus_needs_no_lease(self):
        self.assertEqual(self.match([self.row(0)], []), [])

    def test_more_gpus_than_the_lease(self):
        flags = self.match([self.row(4)], [lease_row(gpus=2)])
        self.assertEqual(kinds(flags), ['gpus_over_lease'])
        self.assertIn('4', flags[0]['detail'])

    def test_stale_lease_whose_namespace_is_gone(self):
        flags = self.match([], [lease_row(ns=PREFIX + '7-gone')], exists={PREFIX + '7-gone': None})
        self.assertEqual(kinds(flags), ['lease_namespace_gone'])
        self.assertEqual(flags[0]['lease_id'], 'lse_test0001')

    def test_lease_on_a_recreated_namespace_is_stale_and_does_not_cover_it(self):
        flags = self.match([self.row(2, uid='uid-new')], [lease_row(uid='uid-ns-1')])
        self.assertEqual(kinds(flags), ['gpus_no_lease', 'lease_namespace_gone'])

    def test_lease_on_a_namespace_outside_the_list_that_still_exists_is_not_stale(self):
        other = PREFIX + '8-unlabelled'
        self.assertEqual(self.match([], [lease_row(ns=other)], exists={other: 'uid-ns-1'}), [])
        self.assertEqual(kinds(self.match([], [lease_row(ns=other)], exists={other: 'uid-x'})),
                         ['lease_namespace_gone'])

    def test_a_lease_whose_namespace_was_not_read_is_not_judged(self):
        self.assertEqual(self.match([], [lease_row(ns=PREFIX + '7-unread')], exists={}), [])

    def test_ended_leases_are_not_stale(self):
        for state in ('released', 'torn-down'):
            self.assertEqual(self.match([], [lease_row(state=state, ns=PREFIX + '9-x')],
                                        exists={PREFIX + '9-x': None}), [], state)

    def test_without_the_lease_list_no_lease_flag_is_raised(self):
        rows = [self.row(4)]
        self.assertEqual(self.match(rows, None, available=False), [])
        self.assertIsNone(rows[0]['lease'])


class Decide(unittest.TestCase):
    """--enforce: the one write, as a decision from one row."""

    def decide(self, expires=None, uid='uid-ns-1', known='uid-ns-1', rec=None, **kw):
        row = rules.namespace_row(ns_obj(uid=uid, expires=expires, **kw), rec, [], [], NOW, kw.get('premade'))
        return rules.decide(row, known, NOW)

    def test_not_expired_is_untouched(self):
        self.assertIsNone(self.decide(at(seconds=1))['action'])

    def test_just_expired_scales_to_zero(self):
        self.assertEqual(self.decide(at(seconds=0))['action'], 'expire')
        self.assertEqual(self.decide(at(minutes=-59))['action'], 'expire')

    def test_after_the_grace_period_deletes(self):
        self.assertEqual(self.decide(at(hours=-1))['action'], 'down')
        self.assertEqual(self.decide(at(days=-3))['action'], 'down')

    def test_grace_counts_from_the_guards_expiry(self):
        # The record's (earlier) expiry is the one the guard checks, so grace starts there.
        self.assertEqual(self.decide(at(hours=2), rec=record(expires=at(hours=-1)))['action'], 'down')

    def test_recreated_namespace_is_never_touched(self):
        d = self.decide(at(days=-1), uid='uid-new', known='uid-ns-1')
        self.assertIsNone(d['action'])
        self.assertEqual(d['flag'], 'namespace_recreated')

    def test_unknown_uid_is_never_taken_from_the_live_read(self):
        d = self.decide(at(days=-1), known=None)
        self.assertIsNone(d['action'])
        self.assertEqual(d['flag'], 'uid_unknown')

    def test_claimed_and_unread_rows_are_never_acted_on_whatever_their_expiry(self):
        # decide() does not rely on the row builders blanking the expiry.
        row = rules.namespace_row(ns_obj(expires=at(days=-1)), None, [], [], NOW, None)
        for key in ('claimed', 'read_failed'):
            with self.subTest(key=key):
                self.assertIsNone(rules.decide(dict(row, **{key: True}), 'uid-ns-1', NOW)['action'])

    def test_an_expiry_without_a_time_zone_is_unreadable(self):
        d = self.decide('2026-01-10T09:00:00')
        self.assertEqual((d['action'], d['flag']), (None, 'expiry_unreadable'))

    def test_no_expiry_unreadable_expiry_claimed_and_terminating(self):
        self.assertIsNone(self.decide(False)['action'])
        d = self.decide('next tuesday')
        self.assertEqual((d['action'], d['flag']), (None, 'expiry_unreadable'))
        row = rules.namespace_row(ns_obj(PREFIX + 'agent', expires=at(days=-1)), None, [], [], NOW, PREFIX + 'agent')
        self.assertIsNone(rules.decide(row, 'uid-ns-1', NOW)['action'])
        self.assertIsNone(self.decide(at(days=-1), terminating=True)['action'])


class Ledger(unittest.TestCase):
    """Ledger lines with no `down`, by the guard's own rule."""

    def line(self, obj, direction, ns=NS, uid='uid-ns-1', lease=None, thread=ME, **extra):
        return {'time': at(hours=-2), 'thread': thread, 'cluster': PROFILE, 'namespace': ns, 'uid': uid,
                'object': obj, 'direction': direction, 'lease': lease, **extra}

    def opened(self, lines, ended=()):
        return rules.open_ledger([(l, 'thread-a') for l in lines], set(ended))

    def test_created_namespace_without_down_is_open(self):
        [entry] = self.opened([self.line(f'Namespace/{NS}', 'up', created=True)])
        self.assertEqual((entry['kind'], entry['namespace'], entry['uid'], entry['thread']),
                         ('namespace', NS, 'uid-ns-1', ME))

    def test_down_that_deleted_closes_it(self):
        lines = [self.line(f'Namespace/{NS}', 'up', created=True), self.line(f'Namespace/{NS}', 'down', deleted=True)]
        self.assertEqual(self.opened(lines), [])

    def test_expire_writes_down_without_deleting_and_leaves_it_open(self):
        lines = [self.line(f'Namespace/{NS}', 'up', created=True), self.line(f'Namespace/{NS}', 'down')]
        self.assertEqual(len(self.opened(lines)), 1)

    def test_down_of_another_instance_does_not_close_it(self):
        lines = [self.line(f'Namespace/{NS}', 'up', created=True),
                 self.line(f'Namespace/{NS}', 'down', uid='uid-other', deleted=True)]
        self.assertEqual(len(self.opened(lines)), 1)

    def test_claim_without_down_is_open_and_closes_on_claim_down(self):
        claim = self.line(f'Claim/{NS}', 'claim', lease='lse_c0001')
        [entry] = self.opened([claim])
        self.assertEqual((entry['kind'], entry['lease']), ('claim', 'lse_c0001'))
        self.assertEqual(self.opened([claim, self.line(f'Claim/{NS}', 'down', lease='lse_c0001')]), [])
        self.assertEqual(len(self.opened([claim, self.line(f'Claim/{NS}', 'down', lease='lse_c0002')])), 1)

    def test_a_claim_line_without_a_lease_opens_nothing(self):
        self.assertEqual(self.opened([self.line(f'Claim/{NS}', 'claim')]), [])

    def test_a_lease_the_plugin_reports_ended_closes_its_entry(self):
        claim = self.line(f'Claim/{NS}', 'claim', lease='lse_c0001')
        self.assertEqual(self.opened([claim], ended={'lse_c0001'}), [])
        created = self.line(f'Namespace/{NS}', 'up', created=True)
        acquired = self.line('ConfigMap/wcep-agent-lease', 'up', lease='lse_n0001')
        self.assertEqual(self.opened([created, acquired], ended={'lse_n0001'}), [])

    def test_object_lines_and_the_sweeps_own_lines_open_nothing(self):
        lines = [self.line('DynamoGraphDeployment/w', 'up', lease='lse_x0001'),
                 self.line(f'Sweep/{NS}', 'enforce', action='expire', result='ok')]
        self.assertEqual(self.opened(lines), [])

    def test_the_same_line_in_two_ledgers_is_one_entry_with_both_sources(self):
        line = self.line(f'Namespace/{NS}', 'up', created=True)
        [entry] = rules.open_ledger([(line, 'host'), (dict(line), 'thread-a')], set())
        self.assertEqual(entry['sources'], ['host', 'thread-a'])

    def test_known_uids_come_from_create_lines(self):
        lines = [(self.line(f'Namespace/{NS}', 'up', created=True), 'host'),
                 (self.line(f'Namespace/{PREFIX}2-x', 'up', ns=PREFIX + '2-x', uid='uid-2'), 'host')]
        self.assertEqual(rules.created_uids(lines), {(PROFILE, NS): 'uid-ns-1'})

    def test_junk_lines_are_skipped(self):
        self.assertEqual(rules.open_ledger([('not a dict', 'host'), ({'object': 5}, 'host')], set()), [])


class Digest(unittest.TestCase):
    def report(self, **kw):
        base = {'host': 'host-a', 'checked_at': at(), 'mode': 'report', 'clusters': [PROFILE], 'namespaces': [],
                'stale_leases': [], 'open_ledger': [], 'errors': [], 'enforced': []}
        base.update(kw)
        return base

    def test_one_table_with_a_row_for_each_namespace_lease_and_ledger_entry(self):
        row = rules.namespace_row(ns_obj(), record(), [gpu_dgd(gpus=2)], [gpu_pod()], NOW, None)
        rules.match_leases(PROFILE, [row], [lease_row(gpus=1)], {})
        row['cluster'] = PROFILE
        row['action'] = {'action': 'expire', 'result': 'ok'}
        stale = {'kind': 'lease_namespace_gone', 'cluster': PROFILE, 'namespace': PREFIX + '7-gone',
                 'lease_id': 'lse_old0001', 'detail': 'not found', 'thread': OTHER, 'gpus': 4, 'state': 'active'}
        opened = {'kind': 'namespace', 'cluster': PROFILE, 'namespace': PREFIX + '5-old', 'uid': 'uid-5',
                  'lease': None, 'thread': OTHER, 'since': at(hours=-9), 'sources': ['host']}
        text = rules.digest_table(self.report(namespaces=[row], stale_leases=[stale], open_ledger=[opened]))
        lines = text.splitlines()
        table = [l for l in lines if l.startswith('|')]
        self.assertEqual(table[0].split('|')[1].strip(), 'Kind')
        self.assertRegex(table[1], r'^\|(\s*-+\s*\|)+$')
        self.assertEqual(len(table), 2 + 3)
        self.assertEqual([l.split('|')[1].strip() for l in table[2:]], ['namespace', 'lease', 'ledger'])
        self.assertIn('gpus_over_lease', table[2])
        self.assertIn('expire: ok', table[2])
        self.assertIn('lse_old0001', table[3])
        self.assertIn('host-a', text)
        self.assertIn(at(), text)

    def test_empty_digest_still_says_when_and_where(self):
        text = rules.digest_table(self.report())
        self.assertIn('host-a', text)
        self.assertIn('nothing to report', text.lower())

    def test_errors_are_listed_below_the_table(self):
        text = rules.digest_table(self.report(errors=[{'cluster': None, 'code': 'leases_unavailable',
                                                       'message': 'bb is not running'}]))
        self.assertIn('leases_unavailable', text)

    def test_a_pipe_in_a_value_cannot_break_the_table(self):
        row = rules.namespace_row(ns_obj(thread='a|b'), None, [], [], NOW, None)
        row['cluster'] = PROFILE
        table = [l for l in rules.digest_table(self.report(namespaces=[row])).splitlines() if l.startswith('|')]
        self.assertEqual(len({l.count('|') - l.count('\\|') for l in table}), 1)


class ServerUrl(unittest.TestCase):
    def test_bb_server_url_from_the_managed_file_when_unset(self):
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / '.config/bb').mkdir(parents=True)
            (Path(home) / '.config/bb/server-url').write_text('https://bb.example.test\n')
            self.assertEqual(rules.bb_env({'PATH': '/bin'}, Path(home))['BB_SERVER_URL'], 'https://bb.example.test')
            self.assertEqual(rules.bb_env({'BB_SERVER_URL': 'http://x'}, Path(home))['BB_SERVER_URL'], 'http://x')
        with tempfile.TemporaryDirectory() as home:
            self.assertNotIn('BB_SERVER_URL', rules.bb_env({}, Path(home)))

    def test_the_guard_never_runs_as_a_thread(self):
        env = rules.guard_env({'BB_THREAD_ID': 'thr_x', 'BB_THREAD_STORAGE': '/s', 'PATH': '/bin'}, Path('/nonexistent'))
        self.assertNotIn('BB_THREAD_ID', env)
        self.assertNotIn('BB_THREAD_STORAGE', env)
        self.assertEqual(env['PATH'], '/bin')


# --- the script, on the fakes ---------------------------------------------------------------

# Loads the sweep as a module so the test can name the guard command; the script itself has no such hook.
SWEEP_RUNNER = r'''
import importlib.machinery, importlib.util, json, os, pwd, sys
real = pwd.getpwuid
class Entry:
    def __init__(self, entry):
        self.pw_name, self.pw_uid, self.pw_dir = entry.pw_name, entry.pw_uid, os.environ['FAKE_PW_DIR']
pwd.getpwuid = lambda uid: Entry(real(uid))
sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[1])))
import k8s_profiles
k8s_profiles.KUBECTL_INSTALLS = (os.environ['FAKE_KUBECTL'],)
k8s_profiles.is_elf = lambda path: True
loader = importlib.machinery.SourceFileLoader('k8s_sweep', sys.argv[1])
module = importlib.util.module_from_spec(importlib.util.spec_from_loader('k8s_sweep', loader))
loader.exec_module(module)
command = json.loads(os.environ['SWEEP_TEST_GUARD'])
module.guard_command = lambda: list(command)
if os.environ.get('SWEEP_TEST_GUARD_TIMEOUT'):
    module.GUARD_TIMEOUT = float(os.environ['SWEEP_TEST_GUARD_TIMEOUT'])
sys.exit(module.main(sys.argv[2:]))
'''

# Logs its argv and changes nothing: proves when the sweep calls the guard at all.
LOGGING_GUARD = r'''#!/usr/bin/env python3
import json, os, sys
open(os.environ['FAKE_GUARD_LOG'], 'a').write(json.dumps(sys.argv[1:]) + '\n')
print(json.dumps({'namespace': sys.argv[4] if len(sys.argv) > 4 else None, 'logged': True}))
'''


class SweepCase(G.GuardCase):
    def setUp(self):
        super().setUp()
        self.sweep_runner = self.tmp / 'sweep-runner.py'
        self.sweep_runner.write_text(SWEEP_RUNNER)
        self.guard_log = self.tmp / 'guard.log'
        self.logging_guard = self.tmp / 'logging-guard'
        self.logging_guard.write_text(LOGGING_GUARD)
        self.logging_guard.chmod(0o755)
        # Host and thread ledgers as a host keeps them: one thread's storage under ~/.bb-machines.
        self.thread_dir = self.home / '.bb-machines/m1/thread-storage/thr_alpha1'
        self.thread_dir.mkdir(parents=True)
        self.storage = self.thread_dir  # add_ns() writes the creating thread's ledger here

    def sweep(self, *args, clusters=(PROFILE,), real_guard=False, thread=None, guard_timeout=None, **env_kw):
        # The fake kubectl serves one world whatever the context, so each run names its cluster.
        env_kw.setdefault('host_entry', True)
        env = self.env(**env_kw)
        if thread:
            env['BB_THREAD_ID'] = thread
            env['BB_THREAD_STORAGE'] = str(self.thread_dir)
        env['FAKE_GUARD_LOG'] = str(self.guard_log)
        guard = ['python3', '-I', str(self.runner), str(GUARD)] if real_guard else [str(self.logging_guard)]
        env['SWEEP_TEST_GUARD'] = json.dumps(guard)
        if guard_timeout:
            env['SWEEP_TEST_GUARD_TIMEOUT'] = str(guard_timeout)
        p = subprocess.run(['python3', '-I', str(self.sweep_runner), str(SWEEP), *clusters, *args], env=env,
                           capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        self.reload()
        try:
            body = json.loads(p.stdout)
        except json.JSONDecodeError:
            self.fail(f'k8s-sweep printed no JSON (exit {p.returncode}): {p.stdout!r} {p.stderr!r}')
        return p.returncode, body, p

    def guard_calls(self):
        if not self.guard_log.exists():
            return []
        return [json.loads(l) for l in self.guard_log.read_text().splitlines()]

    def sweep_calls(self):
        return [c for c in self.kubectl_calls() if str(SWEEP) in c['parent']]

    def assert_sweep_only_reads(self):
        calls = self.sweep_calls()
        self.assertTrue(calls, 'the sweep made no kubectl call')
        for c in calls:
            verb = tuple(c['pos'][:2])
            self.assertTrue(c['pos'][0] == 'get' or verb in (('auth', 'whoami'), ('config', 'view')), c['argv'])

    def assert_writes_only_from_the_guard(self):
        for c in self.kubectl_calls():
            if c['pos'] and c['pos'][0] in WRITE_VERBS and '--local' not in c['pos']:
                self.assertIn(str(GUARD), c['parent'], f'a write not sent by k8s-guard: {c["argv"]}')

    def row(self, body, ns=NS):
        [row] = [r for r in body['namespaces'] if r['namespace'] == ns]
        return row

    def expired_world(self, hours_ago, ns=NS, uid='uid-ns-1', lease_state='active', thread=OTHER):
        """A namespace whose lease record expired `hours_ago`, with a busy GPU DGD of that lease."""
        past = G.iso_ms(G.hours_from_now(-hours_ago))
        self.add_ns(ns, thread=thread, uid=uid, expires_at=past)
        lid = self.add_lease(f'lse_{ns[-6:].replace("-", "")}01', ns=ns, uid=uid, thread=thread, state=lease_state,
                             expires_at=past)
        self.add_obj(G.gpu_dgd('w', 2, lid, replicas=2), ns=ns)
        self.add_pod(G.pod('w', 2, name='w-0'), ns=ns)
        self.add_pod(G.pod('w', 2, name='w-1'), ns=ns)
        return lid


class Report(SweepCase):
    def test_matching_lease_has_no_flag_and_the_digest_is_written(self):
        self.add_ns()
        lid = self.add_lease()
        self.add_obj(G.gpu_dgd('w', 2, lid, replicas=2))
        self.add_pod(G.pod('w', 2, name='w-0'))
        self.add_pod(G.pod('w', 2, name='w-1'))
        rc, body, p = self.sweep()
        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        row = self.row(body)
        self.assertEqual((row['cluster'], row['uid'], row['thread'], row['gpus']), (PROFILE, 'uid-ns-1', ME, 4))
        self.assertEqual(row['lease'], {'lease_id': lid, 'state': 'active', 'gpus': 4})
        self.assertEqual(row['flags'], [])
        self.assertEqual(body['flags'], [])
        digest = (self.state_home / 'k8s-sweep/digest.md').read_text()
        self.assertIn(NS, digest)
        self.assertEqual(json.loads((self.state_home / 'k8s-sweep/report.json').read_text())['checked_at'],
                         body['checked_at'])
        self.assert_sweep_only_reads()
        # Per namespace, never -A: no read of every namespace's pods.
        self.assertFalse([c for c in self.kubectl_calls() if '-A' in c['argv'] or '--all-namespaces' in c['argv']])

    def test_text_prints_the_digest(self):
        self.add_ns()
        env = self.env(host_entry=True)
        env['FAKE_GUARD_LOG'] = str(self.guard_log)
        env['SWEEP_TEST_GUARD'] = json.dumps([str(self.logging_guard)])
        p = subprocess.run(['python3', '-I', str(self.sweep_runner), str(SWEEP), PROFILE, '--text'], env=env,
                           capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, (self.state_home / 'k8s-sweep/digest.md').read_text())

    def test_each_flag_on_one_cluster(self):
        # GPUs with no lease.
        self.add_ns(PREFIX + '1-nolease', uid='uid-1')
        self.add_pod(G.pod(None, 2, name='p'), ns=PREFIX + '1-nolease')
        self.k8s['pods'][PREFIX + '1-nolease'][0]['metadata']['labels'] = {}
        # More GPUs than the lease.
        lid = self.add_lease('lse_over0001', ns=PREFIX + '2-over', uid='uid-2', gpus=2, cm=False)
        self.add_ns(PREFIX + '2-over', uid='uid-2')
        self.add_obj(G.gpu_dgd('w', 4, lid), ns=PREFIX + '2-over')
        # An orphan GPU pod next to a DGD that owns its own.
        lid3 = self.add_lease('lse_orph0001', ns=PREFIX + '3-orphan', uid='uid-3', gpus=8, cm=False)
        self.add_ns(PREFIX + '3-orphan', uid='uid-3')
        self.add_obj(G.gpu_dgd('w', 2, lid3), ns=PREFIX + '3-orphan')
        self.add_pod(G.pod('w', 2, name='w-0'), ns=PREFIX + '3-orphan')
        self.add_pod(G.pod('ghost', 2, name='ghost-0'), ns=PREFIX + '3-orphan')
        # A stale lease: its namespace no longer exists.
        self.add_lease('lse_stale001', ns=PREFIX + '4-gone', uid='uid-4', cm=False)
        rc, body, p = self.sweep()
        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        got = sorted((f['kind'], f['namespace']) for f in body['flags'])
        self.assertEqual(got, [('gpu_pod_no_dgd', PREFIX + '1-nolease'), ('gpu_pod_no_dgd', PREFIX + '3-orphan'),
                               ('gpus_no_lease', PREFIX + '1-nolease'), ('gpus_over_lease', PREFIX + '2-over'),
                               ('lease_namespace_gone', PREFIX + '4-gone')])
        self.assertEqual(body['stale_leases'][0]['lease_id'], 'lse_stale001')
        digest = (self.state_home / 'k8s-sweep/digest.md').read_text()
        for kind, _ in got:
            self.assertIn(kind, digest)
        self.assert_sweep_only_reads()

    def test_namespaces_without_our_labels_are_ignored(self):
        self.add_ns()
        theirs = G.namespace(PREFIX + '5-theirs', ME, 'uid-5')
        theirs['metadata']['labels']['dynamo.nvidia.com/developer'] = 'bob'
        self.k8s['namespaces'][PREFIX + '5-theirs'] = theirs
        # Our label, but not under the bot's prefix: a person's namespace, not an agent's.
        mine = G.namespace('alice-manual', ME, 'uid-6')
        self.k8s['namespaces']['alice-manual'] = mine
        for name in (PREFIX + '5-theirs', 'alice-manual'):
            self.k8s['pods'][name] = [G.pod(None, 8, name='p')]
        self.flush()
        rc, body, p = self.sweep()
        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        self.assertEqual([r['namespace'] for r in body['namespaces']], [NS])
        self.assertEqual(body['flags'], [])
        self.assertFalse([c for c in self.sweep_calls() if c['flags'].get('-n') in (PREFIX + '5-theirs', 'alice-manual')])

    def test_without_enforce_no_write_and_no_guard_even_past_grace(self):
        self.expired_world(5)
        rc, body, p = self.sweep()
        self.assertEqual(self.guard_calls(), [])
        self.assertEqual(self.writes(), [])
        for c in self.kubectl_calls():
            self.assertNotIn(c['pos'][0] if c['pos'] else '', WRITE_VERBS, c['argv'])
        self.assert_sweep_only_reads()
        self.assertIn(NS, self.k8s['namespaces'])
        self.assertEqual([c['replicas'] for c in self.dgd_obj('w')['spec']['components']], [1, 2])
        self.assertEqual(self.row(body)['action'], {'action': 'down', 'result': 'not run: report only'})

    def test_bb_down_is_an_error_not_a_flag_for_every_namespace(self):
        self.add_ns()
        self.add_obj(G.gpu_dgd('w', 2, 'lse_test0001'))
        self.add_pod(G.pod('w', 2, name='w-0'))
        self.bb['unreachable'] = True
        self.flush()
        rc, body, p = self.sweep()
        self.assertEqual(rc, 1)
        self.assertEqual([e['code'] for e in body['errors']], ['leases_unavailable'])
        self.assertEqual(body['flags'], [])
        self.assertIsNone(self.row(body)['lease'])
        self.assertTrue((self.state_home / 'k8s-sweep/digest.md').exists())

    def test_open_ledger_lines_from_every_thread_on_the_host(self):
        self.add_ns()  # a live namespace whose create line has no down
        other = self.home / '.bb-machines/m1/thread-storage/thr_bravo2'
        other.mkdir(parents=True)
        (other / 'k8s-ledger.jsonl').write_text(json.dumps(
            {'time': '2026-01-01T00:00:00Z', 'thread': OTHER, 'cluster': PROFILE, 'namespace': PREFIX + '9-old',
             'uid': 'uid-9', 'object': f'Namespace/{PREFIX}9-old', 'direction': 'up', 'created': True}) + '\n')
        rc, body, p = self.sweep()
        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        got = sorted((e['namespace'], e['thread'], e['exists']) for e in body['open_ledger'])
        self.assertEqual(got, [(NS, ME, True), (PREFIX + '9-old', OTHER, False)])

    def test_the_premade_namespace_is_reported_as_claimed_and_matched_to_its_claim(self):
        premade = PREFIX + 'agent'
        self.profiles.write_text(self.profiles.read_text().replace(
            '# premade_namespace = "dyntest-alice-agent"', f'premade_namespace = "{premade}"'))
        ns = G.namespace(premade, ME, 'uid-pm')
        del ns['metadata']['annotations']['wcep.agent/expires-at']
        del ns['metadata']['labels']['wcep.agent/thread']
        ns['metadata']['labels']['dynamo.nvidia.com/agent-lease-fence'] = 'true'
        self.k8s['namespaces'][premade] = ns
        self.k8s['objects'][premade] = {}
        self.k8s['pods'][premade] = [G.pod('w', 1, name='w-0')]
        self.k8s['objects'][premade]['DynamoGraphDeployment/w'] = dict(G.gpu_dgd('w', 1, 'lse_claim001'),
                                                                     metadata={'name': 'w', 'uid': 'u'})
        self.bb['leases']['lse_claim001'] = dict(lease_row('lse_claim001', premade, 'uid-pm', 1, mode='claimed'),
                                                 multi_node=False)
        self.flush()
        rc, body, p = self.sweep()
        self.assertEqual(rc, 0, f'{body} {p.stderr}')
        row = self.row(body, premade)
        self.assertTrue(row['claimed'])
        self.assertEqual(row['lease']['lease_id'], 'lse_claim001')
        self.assertEqual(row['flags'], [])
        self.assertFalse([c for c in self.sweep_calls() if c['pos'][:3] == ['get', 'configmap', 'wcep-agent-lease']
                          and c['flags'].get('-n') == premade])

    def test_unfenced_sweep_reports_unrecorded_jobs_and_pods_and_shows_valid_spend(self):
        premade = PREFIX + 'agent'
        self.profiles.write_text(self.profiles.read_text().replace(
            '# premade_namespace = "dyntest-alice-agent"', f'premade_namespace = "{premade}"'))
        self.add_ns(premade, ledger=False)
        self.add_obj(G.spend_job('pending-job', namespace=premade), ns=premade, stamp=False)
        start = G.iso_ms(G.hours_from_now(-0.01))
        self.add_obj(G.spend_job('recorded-job', namespace=premade, recorded=True, start=start),
                     ns=premade, stamp=False)
        self.add_obj(G.spend_job('cpu-job', namespace=premade, gpus=0), ns=premade, stamp=False)
        self.add_pod(gpu_pod(dgd=None, name='bare-pod', phase='Pending'), ns=premade)
        child = gpu_pod(dgd=None, name='recorded-pod')
        child['metadata']['ownerReferences'] = [
            {'kind': 'Job', 'name': 'recorded-job', 'uid': 'uid-recorded-job', 'controller': True}]
        self.add_pod(child, ns=premade)
        other = gpu_pod(dgd=None, name='wrong-owner')
        other['metadata']['ownerReferences'] = [
            {'kind': 'Job', 'name': 'recorded-job', 'uid': 'uid-previous-job', 'controller': True}]
        self.add_pod(other, ns=premade)

        rc, body, _ = self.sweep()
        self.assertEqual(rc, 0, body)
        flags = [f for f in body['flags'] if f['kind'] == 'gpu_spend_required']
        self.assertEqual([(f['object'], f['gpus']) for f in flags],
                         [('Job/pending-job', 4), ('Pod/bare-pod', 2), ('Pod/wrong-owner', 2)])
        self.assertEqual(self.row(body, premade)['gpu_spend'], [
            {'object': 'Job/recorded-job', 'thread': ME, 'gpus': 4, 'start': start,
             'max_hours': 0.5, 'cluster': PROFILE, 'namespace': premade}])
        digest = (self.state_home / 'k8s-sweep/digest.md').read_text()
        for value in ('Job/pending-job', 'Pod/bare-pod', 'Job/recorded-job', start):
            self.assertIn(value, digest)
        self.assert_sweep_only_reads()

    def test_unfenced_sweep_checks_spend_window_thread_and_total_gpu_holds(self):
        premade = PREFIX + 'agent'
        self.profiles.write_text(self.profiles.read_text().replace(
            '# premade_namespace = "dyntest-alice-agent"', f'premade_namespace = "{premade}"'))
        self.add_ns(premade, ledger=False)
        self.add_obj(G.spend_job('over', namespace=premade, recorded=True), ns=premade, stamp=False)
        for i in range(4):
            child = gpu_pod(dgd=None, name=f'over-{i}', phase='Succeeded' if i == 3 else 'Running')
            child['metadata']['ownerReferences'] = [
                {'kind': 'Job', 'name': 'over', 'uid': 'uid-over', 'controller': True}]
            self.add_pod(child, ns=premade)
        expired = G.spend_job('expired', namespace=premade, recorded=True, start='2001-01-01T00:00:00Z')
        self.add_obj(expired, ns=premade, stamp=False)
        completed = G.spend_job('completed', namespace=premade, recorded=True, start='2001-01-01T00:00:00Z')
        completed['status'] = {'conditions': [{'type': 'Complete', 'status': 'True'}]}
        self.add_obj(completed, ns=premade, stamp=False)
        no_deadline = G.spend_job('no-deadline', namespace=premade, recorded=True)
        del no_deadline['spec']['activeDeadlineSeconds']
        self.add_obj(no_deadline, ns=premade, stamp=False)
        start = G.iso_ms(G.hours_from_now(-0.4))
        late = G.spend_job('late-start', namespace=premade, recorded=True, start=start)
        late['status'] = {'startTime': G.iso_ms(G.hours_from_now(-0.05))}
        self.add_obj(late, ns=premade, stamp=False)
        early = G.spend_job('early-start', namespace=premade, recorded=True, start=start)
        early['status'] = {'startTime': start}
        self.add_obj(early, ns=premade, stamp=False)
        pending = G.spend_job('late-pending', namespace=premade, recorded=True, start=start)
        self.add_obj(pending, ns=premade, stamp=False)
        active = dict(json.loads(early['metadata']['annotations']['wcep.agent/gpu-spend']), gpus=2)
        self.add_pod(gpu_pod(dgd=None, name='recorded-direct',
                             annotations={'wcep.agent/gpu-spend': json.dumps(active)}), ns=premade)
        annotations = expired['metadata']['annotations']
        child = gpu_pod(dgd=None, name='expired-pod')
        child['metadata']['ownerReferences'] = [
            {'kind': 'Job', 'name': 'expired', 'uid': 'uid-expired', 'controller': True}]
        self.add_pod(child, ns=premade)
        direct = dict(json.loads(annotations['wcep.agent/gpu-spend']), gpus=2)
        self.add_pod(gpu_pod(dgd=None, name='expired-direct',
                             annotations={'wcep.agent/gpu-spend': json.dumps(direct)}), ns=premade)
        invalid = dict(json.loads(annotations['wcep.agent/gpu-spend']), thread='x', gpus=2)
        standalone = gpu_pod(dgd=None, name='bad-thread',
                             annotations={'wcep.agent/gpu-spend': json.dumps(invalid)})
        self.add_pod(standalone, ns=premade)

        rc, body, _ = self.sweep()
        self.assertEqual(rc, 0, body)
        flags = {(f['object'], f['kind']) for f in body['flags'] if f['kind'].startswith('gpu_spend_')}
        self.assertEqual(flags, {('Job/expired', 'gpu_spend_expired'), ('Pod/expired-pod', 'gpu_spend_expired'),
                                 ('Pod/expired-direct', 'gpu_spend_expired'), ('Job/over', 'gpu_spend_over_count'),
                                 ('Pod/bad-thread', 'gpu_spend_invalid'),
                                 ('Pod/recorded-direct', 'gpu_spend_job_required'),
                                 ('Job/no-deadline', 'gpu_spend_deadline_invalid'),
                                 ('Job/late-start', 'gpu_spend_deadline_invalid'),
                                 ('Job/late-pending', 'gpu_spend_deadline_invalid')})
        over = next(f for f in body['flags'] if f['kind'] == 'gpu_spend_over_count')
        self.assertEqual((over['gpus'], over['recorded_gpus']), (6, 4))

    def test_expiry_matches_the_guards_inspect(self):
        # The record's expiry is earlier than the creation annotation; the sweep reads what the guard reads.
        self.add_ns(expires_at=G.iso_ms(G.hours_from_now(3)))
        self.add_lease(expires_at=G.iso_ms(G.hours_from_now(1)))
        rc, body, _ = self.sweep()
        rc2, inspected, p = self.guard('--context', CTX, 'inspect', NS, host_entry=True)
        self.assertEqual(rc2, 0, f'{inspected} {p.stderr}')
        self.assertEqual(self.row(body)['expires_at'], inspected['expires_at'])


class Enforce(SweepCase):
    def test_bb_down_just_after_expiry_scales_to_zero_and_keeps_the_namespace(self):
        self.expired_world(0.25)
        fresh = PREFIX + '13-fresh'
        self.add_ns(fresh, thread=OTHER, uid='uid-fresh', expires_at=G.iso_ms(G.hours_from_now(1)))
        lid = self.add_lease('lse_fresh001', ns=fresh, uid='uid-fresh', thread=OTHER)
        self.add_obj(G.gpu_dgd('f', 2, lid), ns=fresh)
        self.bb['unreachable'] = True
        self.flush()
        rc, body, p = self.sweep('--enforce', real_guard=True)
        self.assertEqual([c['replicas'] for c in self.dgd_obj('w')['spec']['components']], [0, 0])
        self.assertIn(NS, self.k8s['namespaces'])
        self.assertEqual(self.row(body)['action']['action'], 'expire')
        self.assertEqual(self.row(body)['action']['result'], 'ok', body)
        # Not expired: untouched, and the guard was never asked.
        self.assertEqual([c['replicas'] for c in self.dgd_obj('f', ns=fresh)['spec']['components']], [1, 1])
        self.assertIsNone(self.row(body, fresh)['action'])
        self.assertFalse([c for c in self.writes() if fresh in c['argv'] or c['flags'].get('-n') == fresh])
        # The guard's own line, then the sweep's, in the host ledger.
        host = self.ledger(host=True)
        self.assertEqual(host[-2]['direction'], 'down')
        self.assertNotIn('deleted', host[-2])
        self.assertEqual({k: host[-1][k] for k in ('object', 'direction', 'action', 'result', 'uid', 'by')},
                         {'object': f'Sweep/{NS}', 'direction': 'enforce', 'action': 'expire', 'result': 'ok',
                          'uid': 'uid-ns-1', 'by': 'k8s-sweep'})
        self.assert_writes_only_from_the_guard()
        self.assert_sweep_only_reads()
        self.assertEqual(rc, 1)  # the lease list was unreachable: reported as an error, after enforcing

    def test_expire_repeats_on_every_run_inside_the_grace_period(self):
        self.expired_world(0.5)
        self.bb['unreachable'] = True
        self.flush()
        for _ in range(2):
            self.sweep('--enforce', real_guard=True)
        enforced = [l for l in self.ledger(host=True) if l['object'] == f'Sweep/{NS}']
        self.assertEqual([l['action'] for l in enforced], ['expire', 'expire'])

    def test_bb_down_after_the_grace_period_deletes_the_namespace(self):
        self.expired_world(1.5)
        self.bb['unreachable'] = True
        self.flush()
        rc, body, p = self.sweep('--enforce', real_guard=True)
        self.assertNotIn(NS, self.k8s['namespaces'])
        self.assertIn(NS, self.k8s['deleted_namespaces'])
        self.assertEqual(self.row(body)['action'], {'action': 'down', 'result': 'ok'})
        self.assertEqual(self.ledger(host=True)[-1]['action'], 'down')
        self.assert_writes_only_from_the_guard()

    def test_down_carries_the_recorded_uid_to_the_guard(self):
        self.expired_world(1.5)
        self.bb['unreachable'] = True
        self.flush()
        self.sweep('--enforce')
        self.assertEqual(self.guard_calls(), [['--context', CTX, 'down', NS, '--uid', 'uid-ns-1']])

    def test_a_namespace_recreated_since_its_ledger_line_is_never_touched(self):
        # The ledger recorded uid-ns-1; the cluster now holds a new instance under the same name.
        self.expired_world(5, uid='uid-ns-1')
        self.k8s['namespaces'][NS]['metadata']['uid'] = 'uid-new'
        self.bb['unreachable'] = True
        self.flush()
        rc, body, p = self.sweep('--enforce', real_guard=True)
        self.assertIn(NS, self.k8s['namespaces'])
        self.assertEqual(self.k8s['namespaces'][NS]['metadata']['uid'], 'uid-new')
        self.assertEqual(self.writes(), [])
        self.assertIn('namespace_recreated', [f['kind'] for f in self.row(body)['flags']])
        self.assertIsNone(self.row(body)['action'])

    def test_a_namespace_recreated_while_the_guard_tears_down_is_left(self):
        self.expired_world(5)
        self.k8s['recreate_ns_on_list'] = True
        self.bb['unreachable'] = True
        self.flush()
        rc, body, p = self.sweep('--enforce', real_guard=True)
        self.assertIn(NS, self.k8s['namespaces'])
        self.assertEqual(self.row(body)['action'], {'action': 'down', 'result': 'uid_mismatch'})
        self.assertEqual(self.ledger(host=True)[-1]['result'], 'uid_mismatch')

    def test_a_namespace_recreated_at_the_delete_is_refused_by_the_uid_precondition(self):
        self.expired_world(5)
        self.k8s['recreate_ns_on_delete'] = True
        self.bb['unreachable'] = True
        self.flush()
        rc, body, p = self.sweep('--enforce', real_guard=True)
        self.assertIn(NS, self.k8s['namespaces'])
        self.assertEqual(self.k8s['namespaces'][NS]['metadata']['uid'], 'uid-recreated')
        self.assertEqual(self.row(body)['action']['result'], 'uid_mismatch')

    def test_without_a_recorded_uid_nothing_is_torn_down(self):
        self.expired_world(5)
        (self.thread_dir / 'k8s-ledger.jsonl').unlink()
        self.bb['unreachable'] = True
        self.flush()
        rc, body, p = self.sweep('--enforce')
        self.assertEqual(self.guard_calls(), [])
        self.assertIn('uid_unknown', [f['kind'] for f in self.row(body)['flags']])

    def test_the_lease_uid_stands_in_when_bb_answers(self):
        self.expired_world(5, lease_state='revoking')
        (self.thread_dir / 'k8s-ledger.jsonl').unlink()
        rc, body, p = self.sweep('--enforce', real_guard=True)
        self.assertNotIn(NS, self.k8s['namespaces'])
        self.assertEqual(self.row(body)['action'], {'action': 'down', 'result': 'ok'})

    def test_run_inside_the_owners_thread_the_guard_still_acts_without_owner_rights(self):
        # Owner rights would release the active lease; the sweep acts only with teardown authority.
        self.expired_world(5, thread=ME)
        rc, body, p = self.sweep('--enforce', real_guard=True, thread=ME)
        self.assertEqual(self.row(body)['action'], {'action': 'down', 'result': 'lease_active'})
        self.assertFalse([c for c in self.bb_calls() if c[:3] == ['k8s', 'lease', 'release']])
        self.assertIn(NS, self.k8s['namespaces'])

    def test_one_unreadable_namespace_costs_only_that_namespace(self):
        self.expired_world(0.25)
        bad = PREFIX + '14-bad'
        self.add_ns(bad, thread=OTHER, uid='uid-bad', expires_at=G.iso_ms(G.hours_from_now(-5)))
        with open(self.thread_dir / 'k8s-ledger.jsonl', 'a') as f:  # an open entry whose namespace is read by name
            f.write(json.dumps({'thread': OTHER, 'cluster': PROFILE, 'namespace': PREFIX + '15-gone', 'uid': 'uid-15',
                                'object': f'Namespace/{PREFIX}15-gone', 'direction': 'up', 'created': True}) + '\n')
        self.k8s['forbid'] = [f'get pods -n {bad}', f'get namespace {PREFIX}15-gone']
        self.bb['unreachable'] = True
        self.flush()
        rc, body, p = self.sweep('--enforce', real_guard=True)
        self.assertEqual(rc, 1)
        self.assertEqual(self.row(body)['action'], {'action': 'expire', 'result': 'ok'})
        self.assertEqual([c['replicas'] for c in self.dgd_obj('w')['spec']['components']], [0, 0])
        row = self.row(body, bad)
        self.assertEqual([f['kind'] for f in row['flags']], ['read_failed'])
        self.assertIsNone(row['action'])
        self.assertIsNone(row['gpus'])
        self.assertIn(bad, self.k8s['namespaces'])
        self.assertFalse([c for c in self.writes() if bad in c['argv'] or c['flags'].get('-n') == bad])
        self.assertEqual(sorted(e['code'] for e in body['errors']),
                         ['kubectl_error', 'kubectl_error', 'leases_unavailable'])
        self.assertEqual(body['clusters'], [PROFILE])
        [gone] = [e for e in body['open_ledger'] if e['namespace'] == PREFIX + '15-gone']
        self.assertIsNone(gone['exists'])  # its read failed: not judged

    def test_a_guard_that_prints_no_json_fails_the_run(self):
        self.expired_world(5)
        self.bb['unreachable'] = True
        self.flush()
        self.logging_guard.write_text('#!/bin/sh\necho "Traceback: boom" >&2\nexit 1\n')
        rc, body, p = self.sweep('--enforce')
        self.assertEqual(self.row(body)['action'], {'action': 'down', 'result': 'guard_no_json'})
        self.assertIn('enforce_failed', [e['code'] for e in body['errors']])
        self.assertEqual(rc, 1)
        self.assertEqual(self.ledger(host=True)[-1]['result'], 'guard_no_json')

    def test_an_identity_that_is_not_the_bot_stops_the_cluster_before_any_guard_call(self):
        self.expired_world(5)
        self.k8s['whoami'] = 'someone-else'
        self.bb['unreachable'] = True
        self.flush()
        rc, body, p = self.sweep('--enforce')
        self.assertEqual(rc, 1)
        self.assertIn({'cluster': PROFILE, 'code': 'not_bot'}, [{k: e[k] for k in ('cluster', 'code')} for e in body['errors']])
        self.assertEqual((body['clusters'], body['namespaces']), ([], []))
        self.assertEqual(self.guard_calls(), [])
        self.assertEqual([c['pos'][:2] for c in self.sweep_calls() if c['pos'][:2] != ['config', 'view']],
                         [['auth', 'whoami']])

    def guard_answers(self, script, **kw):
        """An expired namespace past grace, bb up (so only the guard's answer can fail the run), and a guard
        that answers with `script`."""
        self.expired_world(5)
        self.logging_guard.write_text('#!/bin/sh\n' + script)
        return self.sweep('--enforce', **kw)

    def assert_enforce_failed(self, body, rc, result):
        self.assertEqual(self.row(body)['action'], {'action': 'down', 'result': result})
        self.assertEqual([e['code'] for e in body['errors']], ['enforce_failed'])
        self.assertEqual(rc, 1)
        self.assertEqual(self.ledger(host=True)[-1]['result'], result)

    def test_a_guard_kubectl_error_fails_the_run(self):
        rc, body, p = self.guard_answers('echo \'{"error": "Forbidden", "code": "kubectl_error"}\'\nexit 1\n')
        self.assert_enforce_failed(body, rc, 'kubectl_error')

    def test_a_guard_internal_error_fails_the_run(self):
        rc, body, p = self.guard_answers('echo \'{"error": "boom", "code": "internal_error"}\'\nexit 1\n')
        self.assert_enforce_failed(body, rc, 'internal_error')

    def test_a_guard_that_exits_nonzero_without_a_code_fails_the_run(self):
        rc, body, p = self.guard_answers('echo \'{"namespace": "x"}\'\nexit 1\n')
        self.assert_enforce_failed(body, rc, 'guard_error')

    def test_a_guard_that_hangs_fails_the_run(self):
        rc, body, p = self.guard_answers('exec sleep 30\n', guard_timeout=1)
        self.assert_enforce_failed(body, rc, 'guard_timeout')

    def test_a_guard_refusal_on_its_rules_does_not_fail_the_run(self):
        rc, body, p = self.guard_answers('echo \'{"error": "active", "code": "lease_active"}\'\nexit 1\n')
        self.assertEqual(self.row(body)['action'], {'action': 'down', 'result': 'lease_active'})
        self.assertEqual((body['errors'], rc), ([], 0))

    def test_premade_and_terminating_namespaces_are_never_enforced(self):
        premade = PREFIX + 'agent'
        self.profiles.write_text(self.profiles.read_text().replace(
            '# premade_namespace = "dyntest-alice-agent"', f'premade_namespace = "{premade}"'))
        self.add_ns(premade, uid='uid-pm', expires_at=G.iso_ms(G.hours_from_now(-9)))
        self.expired_world(5)
        self.k8s['namespaces'][NS]['metadata']['deletionTimestamp'] = '2026-01-01T00:00:00Z'
        self.bb['unreachable'] = True
        self.flush()
        rc, body, p = self.sweep('--enforce')
        self.assertEqual(self.guard_calls(), [])
        self.assertTrue(self.row(body, premade)['claimed'])

    def test_a_read_only_cluster_is_reported_and_never_enforced(self):
        self.add_ns(expires_at=G.iso_ms(G.hours_from_now(-9)), cluster='lab-x86-qa')
        rc, body, p = self.sweep('--enforce', clusters=('lab-x86-qa',))
        self.assertEqual(self.guard_calls(), [])
        self.assertEqual(body['clusters'], ['lab-x86-qa'])
        self.assertEqual(self.row(body)['action'], {'action': 'down', 'result': 'not run: cluster read only'})

    def test_a_never_cluster_is_never_read(self):
        rc, body, p = self.sweep(clusters=('lab-ci',))
        self.assertEqual(body['skipped'], [{'cluster': 'lab-ci', 'reason': 'profile says never'}])
        self.assertFalse([c for c in self.kubectl_calls() if c['flags'].get('--context') == G.CTX_CI])


# --- the systemd user units ----------------------------------------------------------------

UNITS = ROOT / 'dot_config/systemd/user'
BASE = 'data:\n  name: "N"\n  email: "e@example.com"\n  machine_class: "sudo"\n  machine_seed: "blank"\n' \
       '  cursor_ssh_host: "h"\n  onepassword:\n    enabled: false\n'
BLOCK = ('  k8s_tbot:\n    name: "tenant-a"\n    proxy_server: "proxy.example.test:443"\n    token: "bot-a-join"\n'
         '    selector_labels:\n      environment: "dev"\n')
TARGETS = {'.config/systemd/user/k8s-sweep.service', '.config/systemd/user/k8s-sweep.timer',
           '.config/systemd/user/k8s-sweep-enforce.service', '.config/systemd/user/k8s-sweep-enforce.timer',
           '.config/systemd/user/k8s-fence-record@.service', '.config/systemd/user/k8s-fence-record@.timer'}


def load_sweep():
    loader = importlib.machinery.SourceFileLoader('sweep_module', str(SWEEP))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader('sweep_module', loader))
    loader.exec_module(module)
    return module


class FakeKc:
    def __init__(self):
        self.calls = []

    def run(self, *args):
        self.calls.append(args)
        return 0, '{"metadata": {"uid": "u"}}', ''


class ReadOnly(unittest.TestCase):
    def reader(self, module):
        r = object.__new__(module.Reader)
        r.check, r.kc = types.SimpleNamespace(KubectlError=Exception), FakeKc()
        return r

    def test_the_reader_refuses_every_verb_but_get_and_whoami_before_kubectl_runs(self):
        module = load_sweep()
        r = self.reader(module)
        for verb in ('delete', 'patch', 'apply', 'create', 'scale', 'label', 'annotate', 'replace', 'auth'):
            with self.subTest(verb=verb):
                with self.assertRaises(module.ProfileError) as e:
                    r.json(verb, 'namespace', NS)
                self.assertEqual(e.exception.code, 'not_a_read')
        self.assertEqual(r.kc.calls, [])
        r.json('get', 'namespace', NS, '-o', 'json')
        r.json('auth', 'whoami', '-o', 'json')
        self.assertEqual(len(r.kc.calls), 2)

    def test_lookup_reads_only_names_that_could_be_namespaces(self):
        module = load_sweep()
        r = self.reader(module)
        errors = []
        out = module.lookup(r, {PREFIX + '1-ok', '-A', '--all-namespaces', 'Bad_Name', 'x' * 64}, PROFILE, errors)
        self.assertEqual(out, {PREFIX + '1-ok': 'u'})
        self.assertEqual([c[2] for c in r.kc.calls], [PREFIX + '1-ok'])


class GuardCommand(unittest.TestCase):
    def test_the_guard_is_found_next_to_the_sweep_under_either_name(self):
        for name in ('k8s-guard', 'executable_k8s-guard'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as d:
                for f in ('k8s_sweep_rules.py', 'k8s_guard_rules.py', 'k8s_profiles.py'):
                    shutil.copy(SCRIPTS / f, d)
                shutil.copy(SWEEP, Path(d) / 'k8s-sweep')
                (Path(d) / name).write_text('#!/bin/sh\n')
                loader = importlib.machinery.SourceFileLoader('sweep_under_test', str(Path(d) / 'k8s-sweep'))
                module = importlib.util.module_from_spec(importlib.util.spec_from_loader('sweep_under_test', loader))
                loader.exec_module(module)
                sys.path.remove(d)
                cmd = module.guard_command()
                self.assertEqual(cmd[1:], ['-I', str(Path(d).resolve() / name)])
                self.assertEqual(cmd[0], sys.executable)


@unittest.skipUnless(shutil.which('chezmoi'), 'chezmoi not installed')
class Units(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / 'home'
        self.home.mkdir()
        self.config = Path(tmp.name) / 'chezmoi.yaml'
        self.env = {**os.environ, 'HOME': str(self.home), 'DOTFILES_MACHINE_CLASS': ''}

    def chezmoi(self, data, *args, stdin=None, check=True):
        self.config.write_text(BASE + data)
        return subprocess.run(['chezmoi', '--no-tty', '--config', str(self.config), '--source', str(ROOT),
                               '--destination', str(self.home), *args], input=stdin if stdin is not None else '',
                              capture_output=True, text=True, env=self.env, check=check, timeout=120)

    def render(self, name):
        src = UNITS / name
        if not src.exists():
            src = UNITS / (name + '.tmpl')
        return self.chezmoi(BLOCK, 'execute-template', stdin=src.read_text()).stdout

    def lines(self, name):
        return self.render(name).splitlines()

    def test_only_hosts_with_the_bot_get_the_units(self):
        managed = lambda data: set(self.chezmoi(data, 'managed', '--include', 'files').stdout.split())
        self.assertFalse(TARGETS & managed(''))
        self.assertLessEqual(TARGETS, managed(BLOCK))

    def test_services_run_the_sweep_as_the_bot_once(self):
        for unit, extra in (('k8s-sweep.service', []), ('k8s-sweep-enforce.service', ['--enforce'])):
            with self.subTest(unit=unit):
                lines = self.lines(unit)
                self.assertIn('Type=oneshot', lines)
                [start] = [l for l in lines if l.startswith('ExecStart=')]
                self.assertEqual(start.split('=', 1)[1].split(),
                                 ['%h/.agents/skills/k8s-clusters/scripts/k8s-sweep', *extra])
                self.assertIn('Environment=KUBECONFIG=%h/.local/share/tbot/tenant-a/kube/kubeconfig.yaml', lines)
                [path] = [l.split('=', 2)[2] for l in lines if l.startswith('Environment=PATH=')]
                self.assertEqual(path.split(':')[0], '%h/.local/bin')  # the bb wrapper
                self.assertTrue([l for l in lines if l.startswith('TimeoutStartSec=')])
                self.assertFalse([l for l in lines if l.startswith(('User=', 'Restart='))])

    def test_the_fence_recorder_runs_the_guard_per_cluster_every_15_minutes(self):
        lines = self.lines('k8s-fence-record@.service')
        self.assertIn('Type=oneshot', lines)
        [start] = [l for l in lines if l.startswith('ExecStart=')]
        self.assertEqual(start.split('=', 1)[1].split(),
                         ['%h/.agents/skills/k8s-clusters/scripts/k8s-guard', '--context', '%i', 'fence', 'record'])
        self.assertIn('Environment=KUBECONFIG=%h/.local/share/tbot/tenant-a/kube/kubeconfig.yaml', lines)
        timer = self.lines('k8s-fence-record@.timer')
        self.assertIn('OnCalendar=*:0/15', timer, 'inside the 1-hour Event lifetime')
        self.assertIn('Persistent=true', timer)

    def test_timers(self):
        report = self.lines('k8s-sweep.timer')
        enforce = self.lines('k8s-sweep-enforce.timer')
        self.assertIn('OnCalendar=hourly', report)
        self.assertIn('OnCalendar=*:0/15', enforce)
        for lines in (report, enforce):
            self.assertIn('Persistent=true', lines)
            self.assertIn('WantedBy=timers.target', lines)

    @unittest.skipUnless(shutil.which('systemd-analyze'), 'systemd-analyze not installed')
    def test_units_pass_systemd_verify(self):
        script = self.home / '.agents/skills/k8s-clusters/scripts/k8s-sweep'
        script.parent.mkdir(parents=True)
        script.write_text('#!/bin/sh\n')
        script.chmod(0o755)
        for name in ('k8s-sweep', 'k8s-sweep-enforce'):
            for kind in ('service', 'timer'):
                (self.home / f'{name}.{kind}').write_text(
                    self.render(f'{name}.{kind}').replace('%h', str(self.home)))
        for name in ('k8s-sweep', 'k8s-sweep-enforce'):
            with self.subTest(unit=name):
                p = subprocess.run(['systemd-analyze', '--user', 'verify', str(self.home / f'{name}.timer'),
                                    str(self.home / f'{name}.service')], capture_output=True, text=True,
                                   env=self.env, stdin=subprocess.DEVNULL, timeout=60)
                self.assertEqual(p.returncode, 0, p.stderr)

    def test_no_internal_value_in_the_sources(self):
        for src in UNITS.glob('k8s-sweep*'):
            for bad in ('teleport.sh', 'nvidia', 'dgxc', 'rolson', 'dynamo'):
                self.assertNotIn(bad, src.read_text().lower(), src.name)


if __name__ == '__main__':
    unittest.main()

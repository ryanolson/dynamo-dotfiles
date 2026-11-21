"""k8s-stale rules: stale deployments and GPU holds, from made-up cluster JSON.

The rules are pure functions, so these tests need no cluster, no kubectl and no Linux.
Every name here is made up.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dot_agents/skills/k8s-clusters/scripts'))
import k8s_stale_rules as rules  # noqa: E402

NOW = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)


def ago(**kw):
    return (NOW - timedelta(**kw)).strftime('%Y-%m-%dT%H:%M:%SZ')


def pod(ns, name, gpus=4, age=None, phase='Running', ready=True, waiting=None, labels=None, annotations=None,
        limits_only=False):
    res = {'limits': {rules.GPU: str(gpus)}} if limits_only else {'requests': {rules.GPU: str(gpus)}}
    status = {'phase': phase, 'conditions': [{'type': 'Ready', 'status': 'True' if ready else 'False',
                                             'lastTransitionTime': ago(hours=5)}]}
    if waiting:
        status['containerStatuses'] = [{'state': {'waiting': {'reason': waiting}}}]
    return {'metadata': {'namespace': ns, 'name': name, 'creationTimestamp': age or ago(hours=1),
                         'labels': labels or {}, 'annotations': annotations or {}},
            'spec': {'containers': [{'name': 'c', 'resources': res}]}, 'status': status}


def dgd(ns, name, state='successful', age=None):
    return {'metadata': {'namespace': ns, 'name': name, 'creationTimestamp': age or ago(hours=1)},
            'status': {'state': state}}


def kinds(found):
    return [f['kind'] for f in found]


class Rules(unittest.TestCase):
    def test_nothing_is_stale_when_everything_is_young_and_ready(self):
        found = rules.analyze([pod('team-a', 'w1')], [dgd('team-a', 'g1')], {}, NOW)
        self.assertEqual(found, [])

    def test_long_gpu_hold(self):
        found = rules.analyze([pod('team-a', 'w1', 8, age=ago(days=5)), pod('team-a', 'w2', 4, age=ago(days=1))],
                              [], {}, NOW)
        self.assertEqual(kinds(found), ['gpu_hold_old'])
        f = found[0]
        self.assertEqual((f['namespace'], f['gpus'], f['age_hours']), ('team-a', 12, 120.0))
        self.assertEqual(f['gpu_hours'], 1440.0)

    def test_hold_threshold_is_an_option(self):
        pods = [pod('team-a', 'w1', 4, age=ago(days=2))]
        self.assertEqual(rules.analyze(pods, [], {}, NOW), [])
        self.assertEqual(kinds(rules.analyze(pods, [], {}, NOW, hold_days=1)), ['gpu_hold_old'])

    def test_limits_count_when_requests_are_absent(self):
        found = rules.analyze([pod('team-a', 'w1', 2, age=ago(days=9), limits_only=True)], [], {}, NOW)
        self.assertEqual(found[0]['gpus'], 2)

    def test_pending_pods_hold_nothing(self):
        self.assertEqual(rules.analyze([pod('team-a', 'w1', 8, age=ago(days=9), phase='Pending')], [], {}, NOW), [])

    def test_pods_without_gpus_are_ignored(self):
        self.assertEqual(rules.analyze([pod('team-a', 'cpu', 0, age=ago(days=9))], [], {}, NOW), [])

    def test_running_pod_that_is_not_ready(self):
        found = rules.analyze([pod('team-a', 'w1', 4, ready=False)], [], {}, NOW)
        self.assertEqual(kinds(found), ['gpu_pod_not_ready'])
        self.assertEqual(found[0]['name'], 'w1')
        self.assertEqual(found[0]['age_hours'], 5.0)

    def test_crash_looping_pod_counts_even_if_marked_ready(self):
        found = rules.analyze([pod('team-a', 'w1', 4, ready=True, waiting='CrashLoopBackOff')], [], {}, NOW)
        self.assertEqual(kinds(found), ['gpu_pod_not_ready'])

    def test_a_brief_wait_is_not_a_finding(self):
        p = pod('team-a', 'w1', 4, ready=False)
        p['status']['conditions'][0]['lastTransitionTime'] = ago(minutes=20)
        self.assertEqual(rules.analyze([p], [], {}, NOW), [])

    def test_reaper_opt_out_with_a_future_expiry(self):
        ann = {rules.OPT_OUT: 'true', rules.OPT_OUT_EXPIRES: '2026-01-15T00:00:00Z'}
        found = rules.analyze([pod('team-a', 'w1', 4, annotations=ann)], [], {}, NOW)
        self.assertEqual(kinds(found), ['reaper_opt_out'])
        self.assertIn('2026-01-15', found[0]['reason'])

    def test_reaper_opt_out_that_has_expired(self):
        ann = {rules.OPT_OUT: 'true', rules.OPT_OUT_EXPIRES: '2026-01-02T00:00:00Z'}
        found = rules.analyze([pod('team-a', 'w1', 4, annotations=ann)], [], {}, NOW)
        self.assertEqual(kinds(found), ['reaper_opt_out_expired'])

    def test_reaper_opt_out_without_a_readable_expiry(self):
        for expires in (None, 'soon'):
            ann = {rules.OPT_OUT: 'true'}
            if expires:
                ann[rules.OPT_OUT_EXPIRES] = expires
            found = rules.analyze([pod('team-a', 'w1', 4, annotations=ann)], [], {}, NOW)
            self.assertEqual(kinds(found), ['reaper_opt_out'], expires)
            self.assertIn('no readable expiry', found[0]['reason'])

    def test_opt_out_false_is_not_an_opt_out(self):
        ann = {rules.OPT_OUT: 'false'}
        self.assertEqual(rules.analyze([pod('team-a', 'w1', 4, annotations=ann)], [], {}, NOW), [])

    def test_opt_out_counts_only_the_opted_out_gpus(self):
        ann = {rules.OPT_OUT: 'true', rules.OPT_OUT_EXPIRES: '2026-02-01T00:00:00Z'}
        pods = [pod('team-a', 'w1', 4, annotations=ann), pod('team-a', 'w2', 8)]
        found = rules.analyze(pods, [], {}, NOW)
        self.assertEqual([f['gpus'] for f in found if f['kind'] == 'reaper_opt_out'], [4])

    def test_dgd_stuck_in_a_state_that_is_not_done(self):
        found = rules.analyze([], [dgd('team-a', 'g1', 'pending', age=ago(hours=6)),
                                   dgd('team-a', 'g2', 'pending', age=ago(minutes=30))], {}, NOW)
        self.assertEqual(kinds(found), ['dgd_stuck'])
        self.assertEqual(found[0]['name'], 'g1')

    def test_failed_dgd_is_its_own_kind_and_counts_its_gpus(self):
        held = pod('team-a', 'w1', 8, labels={rules.DGD_NAME_LABEL: 'g1'})
        found = rules.analyze([held], [dgd('team-a', 'g1', 'failed', age=ago(days=3)),
                                       dgd('team-a', 'g2', 'failed', age=ago(days=200))], {}, NOW)
        self.assertEqual([(f['kind'], f['name'], f['gpus']) for f in found],
                         [('dgd_failed', 'g1', 8), ('dgd_failed', 'g2', 0)])
        self.assertIn('hold 8 GPUs', found[0]['reason'])

    def test_dgd_with_no_state_is_stuck_once_old_enough(self):
        d = dgd('team-a', 'g1', age=ago(hours=6))
        d['status'] = {}
        found = rules.analyze([], [d], {}, NOW)
        self.assertEqual(kinds(found), ['dgd_stuck'])
        self.assertIn('unknown', found[0]['reason'])

    def test_old_dgd_is_a_finding_only_when_it_holds_gpus(self):
        old = ago(days=30)
        held = pod('team-a', 'w1', 4, labels={rules.DGD_NAME_LABEL: 'g1'})
        found = rules.analyze([held], [dgd('team-a', 'g1', age=old), dgd('team-a', 'g2', age=old)], {}, NOW)
        self.assertEqual([(f['kind'], f['name'], f['gpus']) for f in found], [('dgd_old', 'g1', 4)])

    def test_pending_pods_do_not_count_toward_a_dgd_that_holds_gpus(self):
        # A Pending pod is not scheduled, so it holds no GPU; an old DGD with only such pods holds none.
        waiting = pod('team-a', 'w1', 4, phase='Pending', labels={rules.DGD_NAME_LABEL: 'g1'})
        self.assertEqual(rules.analyze([waiting], [dgd('team-a', 'g1', age=ago(days=30))], {}, NOW), [])
        done = pod('team-a', 'w2', 4, phase='Succeeded', labels={rules.DGD_NAME_LABEL: 'g1'})
        self.assertEqual(rules.analyze([done], [dgd('team-a', 'g1', age=ago(days=30))], {}, NOW), [])

    def test_unreadable_dgd_age_is_skipped(self):
        d = dgd('team-a', 'g1', 'pending')
        d['metadata']['creationTimestamp'] = 'not a time'
        self.assertEqual(rules.analyze([], [d], {}, NOW), [])

    def test_owner_comes_from_labels_before_the_name(self):
        self.assertEqual(rules.owner_of('dyntest-bob-qwen', {}), 'bob')
        self.assertEqual(rules.owner_of('dynamo-team', {rules.DEVELOPER_LABEL: 'carol'}), 'carol')
        self.assertEqual(rules.owner_of('x-y', {rules.DEVELOPER_LABEL: 'carol'}, {'owner': 'dave'}), 'dave')
        self.assertEqual(rules.owner_of('alice-dynamo', {}), 'alice')
        self.assertEqual(rules.owner_of('solo', {}), 'solo')

    def test_findings_are_ranked_by_gpu_hours(self):
        pods = [pod('small', 'w', 1, age=ago(days=9)), pod('big', 'w', 8, age=ago(days=4))]
        found = rules.analyze(pods, [], {}, NOW)
        self.assertEqual([f['namespace'] for f in found], ['big', 'small'])

    def test_junk_in_the_json_does_not_crash(self):
        junk = {'metadata': {}, 'spec': {'containers': [{'resources': {'requests': {rules.GPU: 'many'}}}]},
                'status': {'phase': 'Running'}}
        self.assertEqual(rules.analyze([junk, {}], [{}, {'metadata': {'name': 'g'}}], {}, NOW), [])

    def test_parse_time(self):
        self.assertEqual(rules.parse_time('2026-01-10T12:00:00Z'), NOW)
        self.assertEqual(rules.parse_time('2026-01-10T12:00:00'), NOW)
        for bad in (None, '', 'tomorrow', 5):
            self.assertIsNone(rules.parse_time(bad), bad)


if __name__ == '__main__':
    unittest.main()

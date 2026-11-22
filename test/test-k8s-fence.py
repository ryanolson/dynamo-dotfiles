"""The lease fence policy: the template renders, and its Kyverno CLI suites pass on the real CLI.

Renders dot_agents/skills/k8s-clusters/policy/agent-lease-fence.template.yaml with made-up names, writes it as
policy.yaml beside each kyverno-test.yaml of a copy of policy/tests/, and runs `$KYVERNO_CLI test` (v1.15.2).
It fails when KYVERNO_CLI is unset: a policy test that cannot run must not read as a pass. Each suite is also
run against a policy with one rule broken, and must fail: a suite that passes a broken policy proves nothing.

Two facts about the v1.15.2 CLI shape this file. It panics on a policy with webhook match conditions, so the
suites run on the policy without them (k8s_fence_rules.without_match_conditions; the kind fixture tests them).
And for a validate rule it counts an expected `result: fail` as passed whatever the policy did (commands/test/
output.go: `success := ok || (!ok && test.Result == fail)`), and exits 0. So the CLI's summary and exit code are
not the verdict here: every row of its table must carry the reason Ok, and the number of rows must be the
number of results and checks the suite declares. A row reads Excluded when the rule gave no response at all,
whatever was expected, and it loses the resource's name; so per rule, the Excluded rows must number exactly the
results the suite declares as skip.

    KYVERNO_CLI=~/.local/bin/kyverno timeout 300 python3 test/test-k8s-fence.py
    KYVERNO_CLI=... timeout 300 python3 test/test-k8s-fence.py Suites.test_scope
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'dot_agents/skills/k8s-clusters'
TEMPLATE = SKILL / 'policy/agent-lease-fence.template.yaml'
TESTS = SKILL / 'policy/tests'
sys.dont_write_bytecode = True
sys.path.insert(0, str(SKILL / 'scripts'))
import k8s_fence_rules as fence  # noqa: E402

NAMES = dict(premade='dyntest-alice-agent', prefix='dyntest-alice-', bot_user='bot-example-agent',
             owner='alice-example', contact='example-contact', runbook='example runbook section 10')


def rendered(**over):
    names = dict(NAMES, **over)
    return fence.render_fence_policy(TEMPLATE.read_text(), names['premade'], names['prefix'], names['bot_user'],
                                     names['owner'], names['contact'], names['runbook'])


def kyverno():
    cli = os.environ.get('KYVERNO_CLI')
    if not cli:
        raise AssertionError('KYVERNO_CLI is unset: name the Kyverno CLI v1.15.2 binary; these tests never skip')
    return cli


ROW = re.compile(r'^\u2502 \d+\s+\u2502 \S+\s+\u2502 (\S+)\s+\u2502 (\S+)\s+\u2502 (Pass|Fail)\s+\u2502 (.*?)\s+\u2502')


def rows(out):
    """Every result row of the CLI's tables: (rule, resource name, result, reason)."""
    return [(m[1], m[2].rsplit('/', 1)[-1], m[3], m[4]) for m in map(ROW.match, out.splitlines()) if m]


def declared_skips(suite):
    """(rule, resource) for every result a suite declares as skip."""
    skips = set()
    for t in (TESTS / suite).rglob('kyverno-test.yaml'):
        text = t.read_text()
        results = text.split('\nresults:', 1)[1].split('\nchecks:', 1)[0] if '\nresults:' in text else ''
        for entry in re.split(r'\n  - ', results)[1:]:
            rule = re.search(r'rule: ([\w-]+)', entry)
            names = re.search(r'resources: \[([^\]]*)\]', entry)
            if rule and names and re.search(r'result: skip\b', entry):
                skips |= {(rule[1], n.strip()) for n in names[1].split(',')}
    return skips


def wrong_rows(out, suite):
    """Rows the policy got wrong: any reason but Ok and Excluded, and per rule any Excluded beyond the skips."""
    got = rows(out)
    wrong = [r for r in got if r[3] not in ('Ok', 'Excluded')]
    skips = declared_skips(suite)
    for rule in {r[0] for r in got}:
        excluded = [r for r in got if r[0] == rule and r[3] == 'Excluded']
        if len(excluded) != len([s for s in skips if s[0] == rule]):
            wrong += excluded or [(rule, '(a declared skip)', 'missing', 'not Excluded')]
    return wrong


def run_suite(suite, policy_text):
    """Run one suite directory (and its subdirectories) against policy_text; return (exit, output, tests)."""
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / suite
        shutil.copytree(TESTS / suite, dest)
        tests = list(dest.rglob('kyverno-test.yaml'))
        assert tests, f'{suite} holds no kyverno-test.yaml'
        for t in tests:
            (t.parent / "policy.yaml").write_text(fence.without_match_conditions(policy_text))
        p = subprocess.run([kyverno(), 'test', str(dest), '--require-tests', '--remove-color'], capture_output=True,
                           text=True, timeout=240, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout + p.stderr, len(tests)


def expected_results(suite):
    """How many results and checks the suite's kyverno-test.yaml files declare: the CLI must pass every one."""
    n = 0
    for t in (TESTS / suite).rglob("kyverno-test.yaml"):
        text = t.read_text()
        results = text.split("\nresults:", 1)[1].split("\nchecks:", 1)[0] if "\nresults:" in text else ""
        n += sum(len(m.split(",")) for m in re.findall(r"resources: \[([^\]]*)\]", results))
        n += text.count("  - match:")
    return n


class Render(unittest.TestCase):
    def test_every_placeholder_is_filled(self):
        text = rendered()
        for p in fence.PLACEHOLDERS:
            self.assertNotIn(p, text)
        self.assertIn("request.namespace in ['dyntest-alice-agent']", text)
        self.assertIn("namespaces: ['dyntest-alice-*']", text)
        self.assertIn("name: 'bot-example-agent'", text)

    def test_the_description_names_owner_contact_and_the_off_switch_in_order(self):
        text = rendered()
        desc = text[text.index('policies.kyverno.io/description'):text.index('spec:')]
        self.assertIn('Owner: alice-example; contact: example-contact', desc)
        steps = [desc.index(s) for s in ('(1) set this policy to Audit', '(2) remove the label', '(3) delete this policy')]
        self.assertEqual(steps, sorted(steps))
        self.assertIn('admission controller to 0 deletes every Kyverno webhook', ' '.join(desc.split()))
        self.assertIn('Runbook: example runbook section 10', desc)

    def test_every_rule_message_names_owner_and_contact(self):
        text = rendered()
        messages = re.findall(r'message: >-\n((?:\s{10}.*\n)+)', text)
        self.assertEqual(len(messages), 4, messages)  # the four rules, the canary among them
        for m in messages:
            flat = ' '.join(m.split())
            self.assertIn('owner alice-example', flat)
            self.assertIn('contact example-contact', flat)
            self.assertIn('off-switch', flat)

    def test_the_canary_is_its_own_always_enforcing_policy_and_the_fence_starts_in_audit(self):
        # One rule-level Enforce would make the whole policy enforce on its own webhook (Kyverno 1.15.2,
        # measured on kind), so no rule sets a failure action and the canary is a second policy.
        main, canary = rendered().split('\n---\n')
        self.assertIn('  name: agent-lease-fence\n', main)
        self.assertIn('validationFailureAction: Audit', main)
        self.assertNotIn('probe-canary', main)
        self.assertIn('  name: agent-lease-fence-canary\n', canary)
        self.assertIn('validationFailureAction: Enforce', canary)
        self.assertEqual(canary.count('- name: '), 2)  # the match condition and the one rule
        self.assertNotIn('        failureAction:', main + canary)
        for doc in (main, canary):
            self.assertIn("(has(request.name) && request.name == 'wcep-agent-fence')", doc)

    def test_the_gate_rule_does_not_claim_to_cover_delete(self):
        main = rendered().split('\n---\n')[0]
        gate = main[main.index('- name: gate-is-the-bots-and-moves-forward'):]
        self.assertIn('operations: [CREATE, UPDATE]', gate)
        self.assertNotIn('DELETE', gate.split('validate:')[1])

    def test_the_match_condition_keeps_request_name_and_says_why(self):
        text = rendered()
        self.assertIn("has(request.name) && request.name == 'wcep-agent-fence'", text)
        self.assertIn('bulk delete', text)
        self.assertNotIn('oldObject.metadata.name', text)

    def test_bad_values_are_refused(self):
        for over in ({'premade': 'team-x-agent'}, {'prefix': 'dyntest-alice'}, {'bot_user': 'bot user'},
                     {'owner': "o'neil"}, {'contact': 'a: b'}, {'runbook': 'x\ny'}, {'owner': '<owner>'}):
            with self.subTest(over=over), self.assertRaises(fence.FenceError):
                rendered(**over)

    def test_the_cli_copy_drops_only_the_match_conditions(self):
        text = rendered()
        cli = fence.without_match_conditions(text)
        self.assertNotIn('matchConditions', cli)
        self.assertIn('failurePolicy: Fail', cli)
        self.assertEqual(len(text.splitlines()) - len(cli.splitlines()),
                         2 * text[text.index('    matchConditions:'):text.index('  rules:')].count('\n'))

    def test_the_template_is_not_a_chezmoi_template(self):
        # A .tmpl file is run through Go templates by chezmoi, which would fail on {{ request.namespace }}.
        self.assertFalse(TEMPLATE.name.endswith('.tmpl'))
        self.assertEqual(list(TESTS.rglob('*.tmpl')), [])


class Suites(unittest.TestCase):
    def assert_suite(self, suite):
        rc, out, n = run_suite(suite, rendered())
        self.assertEqual(rc, 0, out[-4000:])
        got = rows(out)
        self.assertEqual(wrong_rows(out, suite), [], out[-4000:])
        self.assertEqual(len(got), expected_results(suite), out[-3000:])

    def test_scope(self):
        self.assert_suite('scope')

    def test_enforce(self):
        self.assert_suite('enforce')

    def test_gate(self):
        self.assert_suite('gate')

    def test_attribution(self):
        self.assert_suite('attribution')


# A broken policy each suite must catch: (suite, what is broken, old text, new text).
MUTANTS = [
    ('scope', 'the opt-in label is dropped from the Pod rule',
     '''      exclude:''', None),
    ('enforce', 'the controller-owner condition is dropped',
     '''              - key: "{{ length(request.object.metadata.ownerReferences[?controller] || `[]`) }}"
                operator: Equals
                value: 0
''', ''),
    ('gate', 'seq is compared as a string',
     "to_number(request.object.data.seq || '') == to_number(request.oldObject.data.seq || '') && request.object.data.state",
     "request.object.data.seq == request.oldObject.data.seq && request.object.data.state"),
    ('gate', 'another user may write the gate',
     '''              - key: "{{ request.userInfo.username }}"
                operator: NotEquals
                value: '<bot user>'
''', ''),
    ('attribution', 'the canary message loses its owner',
     'agent-lease-fence canary (owner <owner>, contact <contact>;', 'agent-lease-fence canary (contact <contact>;'),
]


def mutate(suite, old, new):
    template = TEMPLATE.read_text()
    if suite == 'scope':
        # Drop the namespaceSelector from the Pod rule's match only.
        rule = template[template.index('- name: pods-need-the-open-lease'):template.index('- name: bot-roots')]
        broken = rule.replace('''              namespaceSelector:
                matchLabels:
                  dynamo.nvidia.com/agent-lease-fence: "true"
''', '', 1)
        assert broken != rule
        return template.replace(rule, broken)
    assert template.count(old) >= 1, old
    return template.replace(old, new, 1)


class Mutants(unittest.TestCase):
    def test_each_suite_fails_on_the_defect_it_exists_for(self):
        for suite, what, old, new in MUTANTS:
            with self.subTest(suite=suite, broken=what):
                text = fence.render_fence_policy(mutate(suite, old, new), *NAMES.values())
                rc, out, _ = run_suite(suite, text)
                bad = wrong_rows(out, suite)
                self.assertTrue(rc != 0 or bad, f'{suite} passed a policy where {what}:\n{out[-2000:]}')


if __name__ == '__main__':
    unittest.main()

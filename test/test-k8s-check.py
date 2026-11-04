"""k8s-check: the read-only access check of the k8s-clusters skill.

A fake `kubectl` on PATH replays made-up outputs keyed by its arguments, with
`--kubeconfig` and `--context` stripped and logged. It fails loudly on any call
it has no recording for, so a new cluster call cannot slip in untested. A fake
`tsh` fails every test that reaches it: agents never use the owner's login.

Every name here is made up. The real profiles file is never read: HOME,
K8S_CLUSTERS_PROFILES and KUBECONFIG all point into a temporary directory.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'dot_agents/skills/k8s-clusters'
SCRIPT = SKILL / 'scripts/executable_k8s-check'
EXAMPLE = SKILL / 'references/profiles.example.toml'

CTX = 'proxy.example.test-lab-arm-a'
BOT = 'bot-example-agent'
CRD_JSONPATH = '{range .spec.versions[?(@.served==true)]}{.name}{" "}{end}'
MUTATING = {'create', 'apply', 'delete', 'patch', 'label', 'annotate', 'scale', 'edit', 'replace', 'set',
            'rollout', 'drain', 'cordon', 'uncordon', 'taint', 'expose', 'run', 'exec', 'cp', 'port-forward'}
READ_ONLY = {('config', 'get-contexts'), ('auth', 'whoami'), ('auth', 'can-i'), 'get'}

FAKE_KUBECTL = r'''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
flags, rest, i = {}, [], 0
while i < len(argv):
    a = argv[i]
    name = next((f for f in ('--kubeconfig', '--context') if a == f or a.startswith(f + '=')), None)
    if name is None:
        rest.append(a); i += 1; continue
    if a == name:
        flags[name] = argv[i + 1]; i += 2
    else:
        flags[name] = a.split('=', 1)[1]; i += 1
with open(os.environ['FAKE_KUBECTL_LOG'], 'a') as log:
    log.write(json.dumps({'argv': argv, 'flags': flags, 'key': ' '.join(rest)}) + '\n')
scenario = json.load(open(os.environ['FAKE_KUBECTL_SCENARIO']))
reply = scenario.get(' '.join(rest))
if reply is None:
    sys.stderr.write('fake kubectl: no recording for: ' + ' '.join(rest) + '\n')
    sys.exit(97)
sys.stdout.write(reply.get('stdout', ''))
sys.stderr.write(reply.get('stderr', ''))
sys.exit(reply.get('exit', 0))
'''

FAKE_TSH = '#!/bin/sh\necho "fake tsh: agents never use tsh" >&2\necho tsh >> "$FAKE_TSH_LOG"\nexit 98\n'


def j(obj):
    return {'stdout': json.dumps(obj)}


def can_i(answer):
    return {'stdout': answer + '\n', 'exit': 0 if answer == 'yes' else 1}


def node(name, gpus, taints=(), product='NVIDIA-GB300'):
    return {'metadata': {'name': name, 'labels': {'nvidia.com/gpu.present': 'true', 'nvidia.com/gpu.product': product}},
            'spec': {'taints': [dict(t) for t in taints]},
            'status': {'allocatable': {'nvidia.com/gpu': str(gpus), 'cpu': '72'}}}


def pod(ns, name, node_name, gpus, phase='Running', init_gpus=0):
    containers = [{'name': 'main', 'resources': {'requests': {'nvidia.com/gpu': str(gpus)}} if gpus else {}}]
    spec = {'containers': containers}
    if node_name:
        spec['nodeName'] = node_name
    if init_gpus:
        spec['initContainers'] = [{'name': 'init', 'resources': {'requests': {'nvidia.com/gpu': str(init_gpus)}}}]
    return {'metadata': {'namespace': ns, 'name': name}, 'spec': spec, 'status': {'phase': phase}}


GPU_TAINT = {'key': 'nvidia.com/gpu', 'value': 'true', 'effect': 'NoSchedule'}
RESERVED = {'key': 'example.test/reserved', 'value': 'team-b', 'effect': 'NoSchedule'}
OUR_NS = 'dyntest-alice-12-smoke'


def happy_scenario():
    return {
        'config get-contexts -o name': {'stdout': f'{CTX}\nproxy.example.test-lab-x86-qa\n'},
        'auth whoami -o json': j({'kind': 'SelfSubjectReview', 'status': {'userInfo': {
            'username': BOT, 'groups': ['system:authenticated']}}}),
        'auth can-i create dynamographdeployments.nvidia.com -n default': can_i('no'),
        'auth can-i create dynamographdeployments.nvidia.com -n dynamo-system': can_i('no'),
        f'auth can-i create dynamographdeployments.nvidia.com -n {OUR_NS}': can_i('yes'),
        'auth can-i patch namespaces/dyntest-alice-probe': can_i('no'),
        'auth can-i create configmaps -n dyntest-alice-probe': can_i('yes'),
        f'get crd dynamographdeployments.nvidia.com -o jsonpath={CRD_JSONPATH}': {'stdout': 'v1alpha1 v1beta1 '},
        'get deploy -n dynamo-system -o json': j({'items': [{
            'metadata': {'name': 'dynamo-operator-controller-manager'},
            'spec': {'template': {'spec': {'containers': [
                {'name': 'manager', 'image': 'registry.example.test/dynamo-operator:main.abc1234'}]}}}}]}),
        'get nodes -l nvidia.com/gpu.present=true -o json': j({'items': [
            node('node-a', 4, [GPU_TAINT]),
            node('node-b', 4, [GPU_TAINT, RESERVED]),
        ]}),
        'get pods -A --field-selector=status.phase!=Succeeded,status.phase!=Failed -o json': j({'items': [
            pod('team-x', 'w1', 'node-a', 2),
            pod('team-x', 'w2', 'node-a', 0, init_gpus=1),
            pod('team-y', 'w3', 'node-b', 4),
            pod('team-z', 'pending', None, 4, phase='Pending'),
            pod('gpu-reaper', 'gpu-reaper-abc', 'cpu-node', 0),
        ]}),
        'get ns -l dynamo.nvidia.com/developer=alice -o json': j({'items': [{
            'metadata': {'name': OUR_NS, 'labels': {'wcep.agent/thread': 'thr_example'},
                         'annotations': {'wcep.agent/expires-at': '2026-01-01T04:00:00Z'}}}]}),
        'get storageclass -o name': {'stdout': 'storageclass.storage.k8s.io/example-rwx\n'},
    }


class K8sCheck(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / 'home'
        self.bot_kubeconfig = self.home / '.local/share/tbot/example/kube/kubeconfig.yaml'
        self.bot_kubeconfig.parent.mkdir(parents=True)
        self.bot_kubeconfig.write_text('apiVersion: v1\nkind: Config\n')
        self.human_kubeconfig = self.home / '.kube/config'
        self.human_kubeconfig.parent.mkdir(parents=True)
        self.human_kubeconfig.write_text('apiVersion: v1\nkind: Config\n')
        bin_dir = self.tmp / 'bin'
        bin_dir.mkdir()
        for name, body in (('kubectl', FAKE_KUBECTL), ('tsh', FAKE_TSH)):
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        self.profiles = self.tmp / 'profiles.toml'
        self.profiles.write_text(EXAMPLE.read_text())
        self.log = self.tmp / 'kubectl.log'
        self.tsh_log = self.tmp / 'tsh.log'
        self.scenario_path = self.tmp / 'scenario.json'
        self.scenario = happy_scenario()
        self.env = {
            'PATH': f'{bin_dir}:/usr/bin:/bin',
            'HOME': str(self.home),
            'K8S_CLUSTERS_PROFILES': str(self.profiles),
            'KUBECONFIG': str(self.bot_kubeconfig),
            'FAKE_KUBECTL_LOG': str(self.log),
            'FAKE_KUBECTL_SCENARIO': str(self.scenario_path),
            'FAKE_TSH_LOG': str(self.tsh_log),
        }

    def run_check(self, *args):
        self.scenario_path.write_text(json.dumps(self.scenario))
        proc = subprocess.run([sys.executable, '-I', str(SCRIPT), *args], env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60)
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError:
            self.fail(f'stdout is not JSON (exit {proc.returncode}):\n{proc.stdout}\nstderr:\n{proc.stderr}')
        self.assertFalse(self.tsh_log.exists(), 'tsh was called')
        return proc.returncode, out

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def codes(self, out, kind='failures'):
        return [f['code'] for f in out.get(kind, [])]

    def assert_refused_without_contact(self, out, rc, code):
        self.assertEqual(rc, 1, out)
        self.assertFalse(out['ok'])
        self.assertIn(code, self.codes(out))
        cluster_calls = [c for c in self.calls() if not c['key'].startswith('config ')]
        self.assertEqual(cluster_calls, [], 'refusal must come before any cluster call')

    def assert_read_only(self):
        for call in self.calls():
            words = call['key'].split()
            # Only these subcommands; `auth can-i create` asks a question and changes nothing.
            self.assertIn(tuple(words[:2]) if words[0] in ('config', 'auth') else words[0],
                          READ_ONLY, f'not a read: {call["argv"]}')
            if words[0] == 'get':
                self.assertFalse(set(words) & MUTATING, call['argv'])
            self.assertEqual(call['flags'].get('--kubeconfig'), str(self.bot_kubeconfig), call['argv'])
            if not call['key'].startswith('config '):
                self.assertEqual(call['flags'].get('--context'), CTX, call['argv'])
            if call['key'].startswith('auth can-i'):
                # Every can-i names a namespace, or a namespace object by name.
                self.assertTrue('-n' in call['key'].split() or ' namespaces/' in call['key'], call['argv'])

    # --- the happy path ---

    def test_happy_path(self):
        rc, out = self.run_check('lab-arm-a')
        self.assertEqual(rc, 0, out)
        self.assertTrue(out['ok'])
        self.assertEqual(out['failures'], [])
        self.assertEqual(out['cluster'], 'lab-arm-a')
        self.assertEqual(out['context'], CTX)
        self.assertEqual(out['identity']['username'], BOT)
        self.assertEqual(out['can_i']['outside'], {'default': 'no', 'dynamo-system': 'no'})
        self.assertEqual(out['can_i']['inside'], {OUR_NS: 'yes'})
        self.assertEqual(out['can_i']['probe'], {'namespace': 'dyntest-alice-probe',
                                                 'patch_namespace': 'no', 'create_configmaps': 'yes'})
        self.assertEqual(out['crd']['served'], ['v1alpha1', 'v1beta1'])
        self.assertEqual(out['operator'], [{'name': 'dynamo-operator-controller-manager',
                                            'images': ['registry.example.test/dynamo-operator:main.abc1234']}])
        nodes = {n['name']: n for n in out['gpu_nodes']}
        self.assertEqual(nodes['node-a']['free'], 1)
        self.assertEqual(nodes['node-a']['allocatable'], 4)
        self.assertEqual(nodes['node-b']['free'], 0)
        self.assertEqual(nodes['node-a']['taints'], ['nvidia.com/gpu=true:NoSchedule'])
        self.assertEqual(nodes['node-b']['extra_taints'], ['example.test/reserved=team-b:NoSchedule'])
        self.assertEqual(nodes['node-a']['extra_taints'], [])
        self.assertEqual(out['gpus'], {'allocatable': 8, 'free': 1, 'free_untainted': 1})
        self.assertEqual(out['namespaces'], [{'name': OUR_NS, 'thread': 'thr_example',
                                              'expires_at': '2026-01-01T04:00:00Z'}])
        self.assertEqual(out['reaper'], {'profile': True, 'running_pods': 1})
        self.assertEqual(out['warnings'], [])
        self.assert_read_only()

    def test_happy_path_with_matching_crd(self):
        rc, out = self.run_check('lab-arm-a', '--want-crd', 'v1beta1')
        self.assertEqual(rc, 0, out)
        self.assertEqual(out['crd']['want'], 'v1beta1')

    def test_restartable_init_container_reserves_gpus_alongside_main(self):
        workload = pod('team-x', 'with-sidecar', 'node-a', 2, init_gpus=1)
        workload['spec']['initContainers'][0]['restartPolicy'] = 'Always'
        for container in workload['spec']['containers'] + workload['spec']['initContainers']:
            resources = container['resources']
            resources['limits'] = dict(resources['requests'])
        self.scenario['get nodes -l nvidia.com/gpu.present=true -o json'] = j({
            'items': [node('node-a', 4, [GPU_TAINT])]})
        self.scenario['get pods -A --field-selector=status.phase!=Succeeded,status.phase!=Failed -o json'] = j({
            'items': [workload]})
        rc, out = self.run_check('lab-arm-a')
        self.assertEqual(rc, 0, out)
        self.assert_read_only()
        self.assertEqual(out['gpu_nodes'][0]['free'], 1)
        self.assertEqual(out['gpus'], {'allocatable': 4, 'free': 1, 'free_untainted': 1})

    def test_symlink_to_bot_kubeconfig_is_the_bot_file(self):
        link = self.tmp / 'kc-link.yaml'
        link.symlink_to(self.bot_kubeconfig)
        self.env['KUBECONFIG'] = str(link)
        rc, out = self.run_check('lab-arm-a')
        self.assertEqual(rc, 0, out)

    # --- refused before any cluster call ---

    def test_unknown_cluster(self):
        rc, out = self.run_check('lab-nowhere')
        self.assert_refused_without_contact(out, rc, 'unknown_cluster')

    def test_never_cluster(self):
        rc, out = self.run_check('lab-ci')
        self.assert_refused_without_contact(out, rc, 'cluster_never')

    def test_human_kubeconfig(self):
        self.env['KUBECONFIG'] = str(self.human_kubeconfig)
        rc, out = self.run_check('lab-arm-a')
        self.assert_refused_without_contact(out, rc, 'kubeconfig_not_bot')
        self.assertEqual(self.calls(), [])

    def test_kubeconfig_unset(self):
        # kubectl would fall back to ~/.kube/config, the owner's login.
        del self.env['KUBECONFIG']
        rc, out = self.run_check('lab-arm-a')
        self.assert_refused_without_contact(out, rc, 'kubeconfig_not_bot')
        self.assertEqual(self.calls(), [])

    def test_kubeconfig_with_two_paths(self):
        self.env['KUBECONFIG'] = f'{self.bot_kubeconfig}:{self.human_kubeconfig}'
        rc, out = self.run_check('lab-arm-a')
        self.assert_refused_without_contact(out, rc, 'kubeconfig_not_bot')
        self.assertEqual(self.calls(), [])

    def test_context_missing_from_bot_kubeconfig(self):
        self.scenario['config get-contexts -o name'] = {'stdout': 'proxy.example.test-lab-x86-qa\n'}
        rc, out = self.run_check('lab-arm-a')
        self.assert_refused_without_contact(out, rc, 'context_missing')

    def test_missing_profiles_file(self):
        self.profiles.unlink()
        rc, out = self.run_check('lab-arm-a')
        self.assert_refused_without_contact(out, rc, 'profiles_missing')

    # --- cluster answers that fail the check ---

    def test_wrong_identity_stops_at_once(self):
        self.scenario['auth whoami -o json'] = j({'status': {'userInfo': {'username': 'alice@example.test'}}})
        rc, out = self.run_check('lab-arm-a')
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.codes(out), ['not_bot'])
        self.assertEqual(out['identity']['username'], 'alice@example.test')
        self.assertEqual([c['key'] for c in self.calls()], ['config get-contexts -o name', 'auth whoami -o json'])

    def test_can_create_outside_our_namespaces(self):
        self.scenario['auth can-i create dynamographdeployments.nvidia.com -n default'] = can_i('yes')
        rc, out = self.run_check('lab-arm-a')
        self.assertEqual(rc, 1, out)
        self.assertIn('can_i_outside', self.codes(out))
        self.assertEqual(out['can_i']['outside']['default'], 'yes')
        self.assert_read_only()

    def test_probe_reports_answers_as_given(self):
        # The guard assumes "no" and "yes"; the check reports what the cluster says and leaves the ruling to the run.
        self.scenario['auth can-i patch namespaces/dyntest-alice-probe'] = can_i('yes')
        self.scenario['auth can-i create configmaps -n dyntest-alice-probe'] = can_i('no')
        rc, out = self.run_check('lab-arm-a')
        self.assertEqual(rc, 0, out)
        self.assertEqual(out['can_i']['probe'], {'namespace': 'dyntest-alice-probe',
                                                 'patch_namespace': 'yes', 'create_configmaps': 'no'})
        self.assert_read_only()

    def test_probe_error_fails(self):
        self.scenario['auth can-i patch namespaces/dyntest-alice-probe'] = {'stderr': 'error: forbidden\n', 'exit': 1}
        rc, out = self.run_check('lab-arm-a')
        self.assertEqual(rc, 1, out)
        self.assertIn('kubectl_error', self.codes(out))

    def test_crd_version_not_served(self):
        self.scenario[f'get crd dynamographdeployments.nvidia.com -o jsonpath={CRD_JSONPATH}'] = {'stdout': 'v1alpha1 '}
        rc, out = self.run_check('lab-arm-a', '--want-crd', 'v1beta1')
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.codes(out), ['crd_version_not_served'])
        self.assertEqual(out['crd']['served'], ['v1alpha1'])

    def test_crd_absent(self):
        self.scenario[f'get crd dynamographdeployments.nvidia.com -o jsonpath={CRD_JSONPATH}'] = {
            'stderr': 'Error from server (NotFound): customresourcedefinitions "x" not found\n', 'exit': 1}
        rc, out = self.run_check('lab-arm-a')
        self.assertEqual(rc, 0, out)
        self.assertEqual(out['crd']['served'], [])
        self.assertIn('crd_missing', self.codes(out, 'warnings'))
        rc, out = self.run_check('lab-arm-a', '--want-crd', 'v1beta1')
        self.assertEqual(rc, 1, out)
        self.assertIn('crd_version_not_served', self.codes(out))

    def test_can_i_error_is_not_no(self):
        # can-i exits 1 for "no"; any other failure is an error, not an answer.
        self.scenario['auth can-i create dynamographdeployments.nvidia.com -n default'] = {
            'stderr': 'error: You must be logged in to the server (Unauthorized)\n', 'exit': 1}
        rc, out = self.run_check('lab-arm-a')
        self.assertEqual(rc, 1, out)
        self.assertIn('kubectl_error', self.codes(out))

    def test_unexpected_cluster_error_fails(self):
        self.scenario['get nodes -l nvidia.com/gpu.present=true -o json'] = {
            'stderr': 'Unable to connect to the server: timeout\n', 'exit': 1}
        rc, out = self.run_check('lab-arm-a')
        self.assertEqual(rc, 1, out)
        self.assertIn('kubectl_error', self.codes(out))

    # --- drift between the profile and the cluster is a warning ---

    def test_gpu_taint_without_value_matches_exists_toleration(self):
        self.profiles.write_text(EXAMPLE.read_text().replace(
            'gpu_taint = { key = "nvidia.com/gpu", value = "true", effect = "NoSchedule" }',
            'gpu_taint = { key = "nvidia.com/gpu", effect = "NoSchedule" }'))
        rc, out = self.run_check('lab-arm-a')
        self.assertEqual(rc, 0, out)
        self.assertEqual(out['gpus']['free'], 1)
        self.assertEqual(out['gpus']['free_untainted'], 1)
        self.assertNotIn('gpu_taint_missing', self.codes(out, 'warnings'))
        nodes = {n['name']: n for n in out['gpu_nodes']}
        self.assertEqual(nodes['node-a']['taints'], ['nvidia.com/gpu=true:NoSchedule'])
        self.assertEqual(nodes['node-a']['extra_taints'], [])
        self.assertEqual(nodes['node-b']['extra_taints'], ['example.test/reserved=team-b:NoSchedule'])

    def test_profile_drift_warns(self):
        s = self.scenario
        nodes = json.loads(s['get nodes -l nvidia.com/gpu.present=true -o json']['stdout'])
        nodes['items'][0]['metadata']['labels']['nvidia.com/gpu.product'] = 'NVIDIA-OTHER'
        nodes['items'][0]['spec']['taints'] = []
        s['get nodes -l nvidia.com/gpu.present=true -o json'] = j(nodes)
        s['get storageclass -o name'] = {'stdout': 'storageclass.storage.k8s.io/other\n'}
        pods_key = 'get pods -A --field-selector=status.phase!=Succeeded,status.phase!=Failed -o json'
        pods = json.loads(s[pods_key]['stdout'])
        pods['items'] = [p for p in pods['items'] if p['metadata']['namespace'] != 'gpu-reaper']
        s[pods_key] = j(pods)
        rc, out = self.run_check('lab-arm-a')
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(self.codes(out, 'warnings')),
                         ['gpu_product_mismatch', 'gpu_taint_missing', 'reaper_mismatch', 'storage_class_missing'])
        self.assertEqual(out['reaper'], {'profile': True, 'running_pods': 0})

    # --- the profiles file ---

    def test_list_profiles(self):
        rc, out = self.run_check('--list')
        self.assertEqual(rc, 0, out)
        self.assertEqual(out['schema_version'], 1)
        self.assertEqual(out['bot']['kubeconfig'], str(self.bot_kubeconfig))
        self.assertEqual(sorted(out['clusters']), ['lab-arm-a', 'lab-ci', 'lab-x86-qa'])
        self.assertEqual(out['clusters']['lab-arm-a']['gpu_taint'], GPU_TAINT)
        self.assertEqual(self.calls(), [])

    def invalid(self, text, needle):
        self.profiles.write_text(text)
        rc, out = self.run_check('lab-arm-a')
        self.assert_refused_without_contact(out, rc, 'profiles_invalid')
        self.assertIn(needle, ' '.join(f['message'] for f in out['failures']))

    def test_schema_rejects(self):
        base = EXAMPLE.read_text()
        cases = {
            'bad allowed': (base.replace('allowed = "qa-idle"', 'allowed = "sometimes"'), 'allowed'),
            'unknown key': (base.replace('reaper = true', 'reaper = true\nreapr = true'), 'reapr'),
            'missing key': (base.replace('storage_class = "example-rwx"\n', ''), 'storage_class'),
            'schema version': (base.replace('schema_version = 1', 'schema_version = 2'), 'schema_version'),
            'prefix without dash': (base.replace('"dyntest-alice-"', '"dyntest-alice"'), 'namespace_prefix'),
            'bad arch': (base.replace('arch = "arm64"', 'arch = "arm"'), 'arch'),
            'bad taint effect': (base.replace('effect = "NoSchedule"', 'effect = "Never"'), 'effect'),
            'never without source': (base.replace('source = ["ops shared cluster guide"]\n', ''), 'source'),
            'not toml': ('schema_version = = 1', 'TOML'),
        }
        for name, (text, needle) in cases.items():
            with self.subTest(name):
                if self.log.exists():
                    self.log.unlink()
                self.invalid(text, needle)

    def test_never_profile_needs_only_context_allowed_source(self):
        rc, out = self.run_check('--list')
        self.assertEqual(rc, 0, out)
        self.assertEqual(set(out['clusters']['lab-ci']), {'context', 'allowed', 'source'})


if __name__ == '__main__':
    unittest.main()

"""Check the AKS smoke Job and its namespace-scoped launcher Role."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import unittest

import yaml

K8S = Path(__file__).resolve().parents[1] / 'dot_agents/skills/k8s-clusters'
SMOKE = K8S / 'smoke'
NAMESPACE = 'agent-namespace'
NAME = 'gdl-build-smoke'
BUILDER = 'gdl-build-smoke-pod'


class SmokeManifest(unittest.TestCase):
    def setUp(self):
        self.job = yaml.safe_load((SMOKE / 'job.yaml').read_text())
        self.pod = self.job['spec']['template']['spec']

    def test_job_is_bounded(self):
        job = self.job['spec']
        self.assertEqual(self.job['kind'], 'Job')
        self.assertNotIn('namespace', self.job['metadata'])
        self.assertEqual(job['backoffLimit'], 0)
        self.assertTrue(0 < job['activeDeadlineSeconds'] <= 600)
        self.assertTrue(0 < job['ttlSecondsAfterFinished'] <= 300)
        self.assertEqual(self.pod['restartPolicy'], 'Never')
        self.assertFalse(self.pod.get('imagePullSecrets'))
        self.assertCountEqual(self.pod['volumes'], [
            {'name': 'workspace', 'emptyDir': {}},
            {'name': 'buildkit-state', 'emptyDir': {}},
        ])
        self.assertIn(
            {'name': 'workspace', 'mountPath': '/workspace'},
            self.pod['containers'][0]['volumeMounts'],
        )

    def test_scheduling_is_amd64_and_cpu_only(self):
        self.assertEqual(self.pod['nodeSelector']['kubernetes.io/arch'], 'amd64')
        self.assertEqual(self.pod['tolerations'], [{
            'key': 'nvidia.com/gpu', 'operator': 'Equal', 'value': 'true', 'effect': 'NoSchedule',
        }])
        terms = self.pod['affinity']['nodeAffinity'][
            'requiredDuringSchedulingIgnoredDuringExecution'
        ]['nodeSelectorTerms']
        self.assertTrue(terms)
        for term in terms:
            self.assertIn({
                'key': 'demo.nvidia.com/same-node-imex',
                'operator': 'NotIn',
                'values': ['reserved'],
            }, term['matchExpressions'])
        self.assertFalse(self.pod.get('hostPID'))
        for key in ('containers', 'initContainers', 'ephemeralContainers'):
            for container in self.pod.get(key, []):
                self.assertFalse(container.get('securityContext', {}).get('privileged'))
                self.assertFalse(container.get('envFrom'))
                for env in container.get('env', []):
                    self.assertEqual(env, {
                        'name': 'BUILDKITD_FLAGS', 'value': '--oci-worker-no-process-sandbox',
                    })
                for resources in container.get('resources', {}).values():
                    self.assertLessEqual(set(resources), {'cpu', 'memory', 'ephemeral-storage'})

    def test_native_build_requires_an_oci_digest(self):
        script = self.pod['containers'][0]['command'][2]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / 'buildctl-daemonless.sh'
            fake.write_text("""#!/bin/sh
printf '%s\\n' "$@" > "$WORK/args"
printf '%s\\n' "$BUILD_METADATA" > "$WORK/metadata.json"
""")
            fake.chmod(0o755)
            digest = 'sha256:' + 'a' * 64
            env = dict(os.environ, WORK=str(root), PATH=f'{root}:{os.environ["PATH"]}')
            script = script.replace('/workspace', str(root))
            result = subprocess.run(
                ['sh', '-ec', script], capture_output=True, text=True,
                env=dict(env, BUILD_METADATA=f'{{"containerimage.digest": "{digest}"}}'),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(digest, result.stdout)
            args = (root / 'args').read_text().splitlines()
            self.assertEqual(args[args.index('--opt') + 1], 'platform=linux/amd64')
            self.assertEqual(args[args.index('--output') + 1], f'type=oci,dest={root}/smoke.tar')
            dockerfile = (root / 'src/Dockerfile').read_text()
            self.assertIn('RUN test "$(uname -m)" = x86_64', dockerfile)
            (root / 'src/Dockerfile').unlink()
            (root / 'src').rmdir()
            missing = subprocess.run(
                ['sh', '-ec', script], capture_output=True, text=True,
                env=dict(env, BUILD_METADATA='{}'),
            )
            self.assertNotEqual(missing.returncode, 0)

    def test_launcher_grants_only_job_and_log_access(self):
        with (SMOKE / 'rbac.yaml').open() as source:
            objects = list(yaml.safe_load_all(source))
        self.assertCountEqual([(obj['kind'], obj['metadata']['name']) for obj in objects], [
            ('ServiceAccount', NAME), ('ServiceAccount', BUILDER), ('Role', NAME), ('RoleBinding', NAME),
        ])
        for obj in objects:
            self.assertNotIn('namespace', obj['metadata'])
        accounts = [obj for obj in objects if obj['kind'] == 'ServiceAccount']
        for account in accounts:
            self.assertIs(account['automountServiceAccountToken'], False)
            self.assertFalse(account.get('imagePullSecrets'))
            self.assertFalse(account.get('secrets'))
        role = next(obj for obj in objects if obj['kind'] == 'Role')
        binding = next(obj for obj in objects if obj['kind'] == 'RoleBinding')
        self.assertEqual(self.pod['serviceAccountName'], BUILDER)
        self.assertIs(self.pod['automountServiceAccountToken'], False)
        self.assertEqual(binding['roleRef'], {
            'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': NAME,
        })
        self.assertEqual(binding['subjects'], [{
            'kind': 'ServiceAccount', 'name': NAME,
        }])
        self.assertCountEqual(role['rules'], [
            {'apiGroups': ['batch'], 'resources': ['jobs'],
             'verbs': ['create', 'get', 'delete']},
            {'apiGroups': [''], 'resources': ['pods'], 'verbs': ['get', 'list']},
            {'apiGroups': [''], 'resources': ['pods/log'], 'verbs': ['get']},
        ])



class SmokeRunbook(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.calls = self.root / 'calls'
        self.kubeconfig = self.root / 'launcher-config'
        self.doc = (K8S / 'references/bot.md').read_text().split('### AKS smoke build')[1].split('\n## ')[0]
        self.blocks = re.findall(r'\x60{3}bash\n(.*?)\x60{3}', self.doc, re.S)
        self.env = {
            'PATH': f'{self.root}:{os.environ["PATH"]}', 'KUBECONFIG': 'ambient-operator',
            'C': 'fake-aks', 'SMOKE_NAMESPACE': NAMESPACE,
            'FAKE_CALLS': str(self.calls), 'FAKE_TOKEN': 'issued-fixture-token',
            'DRY_RUN_EXIT': '0', 'pod': 'fake-pod', 'MISSING_CA': '0', 'EXPIRED_TOKEN': '0',
        }
        self.fake('fleet', 'touch fleet-called\nexit 1\n')
        self.fake('kubectl', r'''
printf '%s\n' "$*" >> "$FAKE_CALLS"
case "$*" in
  *" aks-smoke "*)
    if [ "$EXPIRED_TOKEN" = 1 ]; then
      printf '%s\n' Unauthorized >&2
      exit 1
    fi
    ;;
esac
case "$*" in
  *'get configmap kube-root-ca.crt'*)
    if [ "$MISSING_CA" = 1 ]; then
      printf '%s\n' '{"data":{}}'
    else
      printf '%s\n' '{"data":{"ca.crt":"namespace CA\n"}}'
    fi
    ;;
  *'config view'*) printf '%s\n' '{"clusters":[{"name":"api","cluster":{"server":"https://aks.example.test","certificate-authority-data":"dGVzdA=="}}],"users":[{"name":"operator","user":{"token":"operator-fixture-token"}}]}' ;;
  *'create token'*) printf '%s\n' "$FAKE_TOKEN" ;;
  *--dry-run=server*) exit "$DRY_RUN_EXIT" ;;
  *'logs --follow'*) printf '%s\n' buildkit-smoke-ok ;;
esac
''')

    def fake(self, name, script):
        path = self.root / name
        path.write_text('#!/bin/sh\n' + script)
        path.chmod(0o755)

    def block(self, marker):
        return next(block for block in self.blocks if marker in block)

    def run_block(self, block, **env):
        return subprocess.run(
            ['bash'], input=block, text=True, capture_output=True,
            env={**self.env, **env}, cwd=self.root,
        )

    def test_cluster_commands_refuse_ambient_kubeconfig(self):
        for block in self.blocks:
            if 'kubectl' not in block:
                continue
            with self.subTest(command=block):
                result = self.run_block(block)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.calls.exists())

    def test_cluster_commands_require_explicit_namespace(self):
        for block in self.blocks:
            if 'kubectl' not in block:
                continue
            with self.subTest(command=block):
                result = self.run_block(
                    block, SMOKE_NAMESPACE='', OPERATOR_KUBECONFIG='explicit-operator',
                    SMOKE_KUBECONFIG=str(self.kubeconfig),
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.calls.exists())

    def test_job_submission_uses_supplied_namespace(self):
        result = self.run_block(
            self.block('--dry-run=server'), SMOKE_KUBECONFIG=str(self.kubeconfig),
            SMOKE_NAMESPACE='another-namespace',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text().splitlines()
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertIn(' --context aks-smoke -n another-namespace create ', call)

    def test_public_smoke_runs_without_a_pull_key(self):
        self.assertNotIn('nvcr-pull', self.doc)
        result = self.run_block(self.block('--dry-run=server'), SMOKE_KUBECONFIG=str(self.kubeconfig))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / 'fleet-called').exists())
        self.assertNotIn('create secret', self.calls.read_text())

    def test_token_stdin_writes_only_the_launcher_credentials(self):
        mint = self.block('create token')
        config = {'OPERATOR_KUBECONFIG': 'explicit-operator', 'SMOKE_KUBECONFIG': str(self.kubeconfig)}
        for missing in config:
            result = self.run_block(mint, **{key: value for key, value in config.items() if key != missing})
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(self.calls.exists())
        result = self.run_block(mint, **config, FAKE_TOKEN='')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.kubeconfig.exists())
        result = self.run_block(mint, **config)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('issued-fixture-token', result.stdout + result.stderr + self.calls.read_text())
        self.assertIn(f'--kubeconfig explicit-operator --context fake-aks -n {NAMESPACE} '
                      'create token gdl-build-smoke --duration=20m', self.calls.read_text())
        self.assertIn(f'--kubeconfig explicit-operator --context fake-aks -n {NAMESPACE} '
                      'get configmap kube-root-ca.crt -o json', self.calls.read_text())
        conf = json.loads(self.kubeconfig.read_text())
        self.assertEqual(conf['users'], [{'name': 'launcher', 'user': {'token': 'issued-fixture-token'}}])
        self.assertEqual(conf['current-context'], 'aks-smoke')
        self.assertEqual(conf['contexts'][0]['context']['namespace'], NAMESPACE)
        self.assertEqual(conf['clusters'], [{'name': 'api', 'cluster': {
            'server': 'https://aks.example.test', 'certificate-authority-data': 'bmFtZXNwYWNlIENBCg==',
        }}])
        self.assertEqual(stat.S_IMODE(self.kubeconfig.stat().st_mode), 0o600)
        result = self.run_block(mint, **config, FAKE_TOKEN='replacement')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(self.kubeconfig.read_text()), conf)

    def test_missing_namespace_ca_refuses_before_requesting_a_token(self):
        result = self.run_block(
            self.block('create token'), OPERATOR_KUBECONFIG='explicit-operator',
            SMOKE_KUBECONFIG=str(self.kubeconfig), MISSING_CA='1',
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.kubeconfig.exists())
        self.assertIn('kube-root-ca.crt is missing ca.crt', result.stderr)
        self.assertNotIn('create token', self.calls.read_text())

    def test_expired_token_reports_failure_without_retry_or_fallback(self):
        for command in ('logs --follow', 'get job gdl-build-smoke'):
            with self.subTest(command=command):
                result = self.run_block(
                    self.block(command), SMOKE_KUBECONFIG=str(self.kubeconfig), EXPIRED_TOKEN='1',
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Unauthorized', result.stderr)
                calls = self.calls.read_text().splitlines()
                self.assertEqual(len(calls), 1)
                self.assertIn(f'--kubeconfig {self.kubeconfig} --context aks-smoke', calls[0])
                self.calls.unlink()

    def test_diagnostics_use_the_intended_accounts(self):
        result = self.run_block(self.block('get events'), OPERATOR_KUBECONFIG='explicit-operator')
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_block(self.block('logs --follow'), SMOKE_KUBECONFIG=str(self.kubeconfig))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls.read_text().splitlines(), [
            f'--kubeconfig explicit-operator --context fake-aks -n {NAMESPACE} '
            'get events --field-selector involvedObject.name=gdl-build-smoke',
            f'--kubeconfig {self.kubeconfig} --context aks-smoke -n {NAMESPACE} '
            'logs --follow pod/fake-pod -c build',
        ])
        self.assertEqual((self.root / 'gdl-build-smoke.log').read_text(), 'buildkit-smoke-ok\n')

    def test_rejected_dry_run_prevents_create(self):
        result = self.run_block(
            self.block('--dry-run=server'), SMOKE_KUBECONFIG=str(self.kubeconfig), DRY_RUN_EXIT='1',
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls.read_text().splitlines(), [
            f'--kubeconfig {self.kubeconfig} --context aks-smoke -n {NAMESPACE} '
            'create --dry-run=server -f dot_agents/skills/k8s-clusters/smoke/job.yaml',
        ])


if __name__ == '__main__':
    unittest.main()

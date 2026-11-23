# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Exercise the first build against the guard harness and a fake registry."""

from importlib import import_module
import json
import subprocess
import sys
import unittest

sys.dont_write_bytecode = True
fake = import_module('test-k8s-guard')

SMOKE = 'crates/gdl-operator/smoke/Dockerfile'
GIT_REF = 'op://Fleet/owner-repo deploy key/private key'
GIT_KEY = f'-----BEGIN OPENSSH PRIVATE KEY-----\n{fake.SECRET}\n-----END OPENSSH PRIVATE KEY-----\n'
ARM = (fake.CTX, 'arm64', 'gb200')
X86 = (fake.CTX_B200, 'amd64', 'a100')


class FirstBuild(fake.FenceCase):
    def setUp(self):
        super().setUp()
        self.profiles.write_text(self.profiles.read_text()
                                 .replace('gpu_type = "gb300"', 'gpu_type = "gb200"')
                                 .replace('gpu_type = "b200"', 'gpu_type = "a100"'))
        self.bb['claims'] = 'on'
        self.flush()

    def use(self, target):
        self.context, self.arch, self.gpu_type = target

    def command(self, *args, **kw):
        return self.ok('--context', self.context, '-n', fake.PREMADE, *args, **kw)

    def acquire(self):
        self.assertFalse(any(l['state'] in ('active', 'releasing', 'revoking')
                             for l in self.bb['leases'].values()))
        self.bb['next_id'] = f'lse_build{len(self.bb["leases"]):04}'
        self.flush()
        claim = self.command('ns', 'claim', '--gpus', '1', '--hours', '2')
        self.lid = claim['lease_id']
        self.assertEqual(self.bb['leases'][self.lid]['gpu_type'], self.gpu_type)
        self.assertEqual(self.bb['leases'][self.lid]['gpus'], 1)

    def release(self):
        args = ('--context', self.context, 'down', fake.PREMADE, '--lease', self.lid)
        self.assertEqual(self.ok(*args)['state'], 'releasing')
        done = self.ok(*args, '--uid', 'uid-premade-1', host_entry=True)
        self.assertTrue(done['complete'], done)
        self.assertTrue(self.k8s['namespaces'][fake.PREMADE])
        self.assertFalse(any(obj['metadata'].get('labels', {}).get(fake.LEASE_LABEL) == self.lid
                             for obj in self.objects.values()))
        # The plugin records completion after reading the host teardown's reply.
        self.bb['leases'][self.lid]['state'] = 'released'
        self.flush()

    @property
    def objects(self):
        return self.k8s['objects'][fake.PREMADE]

    def credentials(self):
        prefix = ('--context', self.context, '-n', fake.PREMADE, 'secret')
        for args, options in ((('git', '--repo', 'owner/repo'), {'secret': GIT_KEY}),
                              (('ngc-push', '--exposure-accepted'), {})):
            rc, body, proc = self.guard(*prefix, *args, **options)
            self.assertEqual(rc, 0, body)
            self.assert_no_leak(proc)

    def set_job_result(self, name, condition, log):
        self.objects['Job/' + name]['status'] = {'conditions': [{'type': condition, 'status': 'True'}]}
        self.k8s['job_logs'] = {'job/' + name: log}
        self.flush()

    def test_credentials_are_scoped_to_the_claim_and_pull_has_no_fallback(self):
        self.use(ARM)
        self.acquire()
        prefix = ('--context', self.context, '-n', fake.PREMADE)
        self.refused('push_not_approved', *prefix, 'secret', 'ngc-push')
        self.assertFalse(self.fleet_log.exists())
        self.refused('secret_unavailable', *prefix, 'secret', 'ngc', fleet_fail=True)
        self.assertNotIn('Secret/nvcr-pull', self.objects)

        self.credentials()
        rc, answer, proc = self.guard(*prefix, 'secret', 'ngc')
        self.assertEqual(rc, 0, answer)
        self.assert_no_leak(proc)
        self.assertEqual(self.fleet_log.read_text().splitlines(),
                         [f'secret get {ref}' for ref in (fake.PULL_REF, GIT_REF, fake.PUSH_REF, fake.PULL_REF)])
        self.assertEqual(self.objects['Secret/git']['stringData']['id_ed25519'], GIT_KEY)
        for name in ('Secret/git', 'Secret/nvcr-pull', 'Secret/nvcr-push', 'ConfigMap/git-known-hosts'):
            self.assertEqual(self.objects[name]['metadata']['labels'][fake.LEASE_LABEL], self.lid)
        self.release()

    def smoke_command(self):
        return ('build', 'run', *fake.build_argv(self.arch, dockerfile=SMOKE, image='gdl-operator-smoke'))

    def probe_tag(self, container, code, error):
        crane = self.bin / 'crane'
        crane.write_text(fake.FAKE_CRANE)
        crane.chmod(0o755)
        env = {e['name']: e['value'] for e in container['env']}
        env.update(CRANE=str(crane), FAKE_CRANE_LOG=str(self.tmp / 'crane.log'),
                   FAKE_CRANE_RC=str(code), FAKE_CRANE_ERR=error)
        result = subprocess.run(['/bin/sh', '-c', container['command'][-1]], env=env,
                                capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        return result.returncode, result.stdout.strip()

    def test_smoke_and_operator_builds_use_native_claims(self):
        for target in (ARM, X86):
            self.use(target)
            smoke = self.smoke_command()
            self.acquire()
            self.credentials()
            name = self.command(*smoke)['job']
            job = self.objects['Job/' + name]
            pod = fake.pod_of(job)
            self.assertEqual(job['spec']['template']['metadata']['labels'][fake.LEASE_LABEL], self.lid)
            self.assertEqual(pod['nodeSelector']['kubernetes.io/arch'], self.arch)
            self.assertNotIn('schedulerName', pod)
            self.assertEqual(pod['securityContext'], {
                'runAsUser': 1000, 'runAsGroup': 1000, 'fsGroup': 1000,
                'seccompProfile': {'type': 'Unconfined'}, 'appArmorProfile': {'type': 'Unconfined'},
            })
            tag_check, clone = pod['initContainers']
            self.assertEqual([tag_check['image'], clone['image'], pod['containers'][0]['image']],
                             [fake.CRANE, fake.BUILDKIT, fake.BUILDKIT])
            self.assertIn({'name': 'REF', 'value': f'{fake.REGISTRY}/gdl-operator-smoke:{fake.TAG}-{self.arch}'},
                          tag_check['env'])
            self.assertEqual(fake.named(pod['volumes'], 'docker-config')['secret']['secretName'], 'nvcr-push')
            for container in [tag_check, clone, *pod['containers']]:
                for resources in container['resources'].values():
                    self.assertIn('ephemeral-storage', resources)
                    self.assertNotIn('nvidia.com/gpu', resources)
            for code, err, want in ((1, 'MANIFEST_UNKNOWN', (0, '')), (1, 'NAME_UNKNOWN', (0, '')),
                                    (0, '', (1, 'tag_exists')), (1, 'HTTP 401', (1, 'registry_denied')),
                                    (1, 'HTTP 403', (1, 'registry_denied')),
                                    (1, 'HTTP 404', (1, 'registry_error: HTTP 404'))):
                with self.subTest(arch=self.arch, registry=err):
                    self.assertEqual(self.probe_tag(tag_check, code, err), want)

            self.set_job_result(name, 'Complete', 'buildkit-smoke-ok')
            shown = self.command('build', 'show', name)
            self.assertEqual(shown['state'], 'succeeded')
            self.assertIn('buildkit-smoke-ok', shown['tail'])
            self.command('build', 'delete', name)
            duplicate = self.command(*smoke)['job']
            _, marker = self.probe_tag(tag_check, 0, '')
            self.set_job_result(duplicate, 'Failed', marker)
            self.refused('tag_exists', '--context', self.context, '-n', fake.PREMADE, 'build', 'show', duplicate)
            self.release()

        ref = f'{fake.REGISTRY}/gdl-operator:{fake.TAG}'
        for target in (ARM, X86):
            self.use(target)
            self.acquire()
            self.credentials()
            operator = self.command('build', 'run', *fake.build_argv(self.arch))['job']
            self.assertEqual(operator, f'build-{fake.TAG}-{self.arch}')
            operator_pod = fake.pod_of(self.objects['Job/' + operator])
            self.assertIn(f'type=image,name={ref}-{self.arch},push=true', operator_pod['containers'][0]['command'])
            self.set_job_result(operator, 'Complete', 'exporting to nvcr.io')
            self.assertEqual(self.command('build', 'show', operator)['state'], 'succeeded')

            if self.arch == 'amd64':
                index = self.command('build', 'index', '--image', 'gdl-operator', '--commit', fake.COMMIT)['job']
                index_job = self.objects['Job/' + index]
                self.assertEqual(index_job['metadata']['labels'][fake.LEASE_LABEL], self.lid)
                append = fake.pod_of(index_job)['initContainers'][0]['args']
                self.assertEqual(append, ['index', 'append', '-t', ref, '-m', ref + '-amd64', '-m', ref + '-arm64'])
                self.set_job_result(index, 'Complete', 'sha256:' + 'a' * 64)
                self.assertEqual(self.command('build', 'show', index)['state'], 'succeeded')
            self.release()

        for target in (ARM, X86):
            self.use(target)
            self.acquire()
            self.command('secret', 'ngc')
            self.assertNotIn('Secret/nvcr-push', self.objects)
            component = fake.component('Frontend', lease_id=self.lid, ctype='frontend')
            pod = component['podTemplate']['spec']
            pod.update(nodeSelector={'kubernetes.io/arch': self.arch}, imagePullSecrets=[{'name': 'nvcr-pull'}])
            probe = fake.container(name='operator-pull')
            probe.update(image=ref, imagePullPolicy='Always', args=['manifests', '--namespace', fake.PREMADE,
                                                                 '--image', ref])
            pod['initContainers'] = [probe]
            self.command('apply', '-f', self.manifest(fake.dgd('operator-pull', component)))
            stored = self.dgd_obj('operator-pull', ns=fake.PREMADE)['spec']['components'][0]['podTemplate']['spec']
            self.assertEqual(stored['imagePullSecrets'], [{'name': 'nvcr-pull'}])
            self.assertEqual(stored['initContainers'][0]['image'], ref)
            self.release()

    def test_admission_and_runtime_failures_are_observable(self):
        self.use(ARM)
        self.acquire()
        self.credentials()
        prefix = ('--context', self.context, '-n', fake.PREMADE)
        run = self.smoke_command()
        self.k8s['mutate'] = {'Job': {'spec': {'template': {'spec': {'schedulerName': 'kai-scheduler'}}}}}
        self.flush()
        self.refused('field_refused', *prefix, *run)
        self.assertFalse(any(name.startswith('Job/') for name in self.objects))
        del self.k8s['mutate']
        self.flush()

        name = self.command(*run)['job']
        self.set_job_result(name, 'Failed', 'rootlesskit: failed to create user namespace')
        result = self.refused('build_failed', *prefix, 'build', 'show', name)
        self.assertIn('failed to create user namespace', result['tail'])
        self.release()


if __name__ == '__main__':
    unittest.main()

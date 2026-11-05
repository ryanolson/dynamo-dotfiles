"""Tests for the bot kubeconfig renderer and the pinned kubectl (k8s_profiles.py).

The renderer reads `kubectl config view --raw --flatten -o json` of the bot kubeconfig and
writes a fresh kubeconfig with only the fields tbot v18.7.6 writes for one context
(lib/tbot/services/k8s/output_v2.go). kubectl only ever sees that rendered copy, so a field
the renderer does not copy cannot act. A field outside tbot's shape is refused by name, so a
mistake is reported rather than silently dropped.

Interface, in k8s_profiles.py:
  render_bot_kubeconfig(view, context, kubeconfig) -> (rendered JSON bytes, None) or (None, problem)
  kubectl_binary() -> absolute path of an installed kubectl ELF binary; never PATH.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(os.environ.get('K8S_CHECK_SCRIPTS') or ROOT / 'dot_agents/skills/k8s-clusters/scripts')
CTX = 'proxy.example.test-lab-arm-a'
CA = 'Q0EtREFUQQ=='       # base64 of made-up bytes
CERT, KEY = 'Q0VSVA==', 'S0VZ'


def load_module():
    spec = importlib.util.spec_from_file_location('k8s_profiles_under_test', SCRIPTS / 'k8s_profiles.py')
    mod = importlib.util.module_from_spec(spec)
    sys.dont_write_bytecode = True
    spec.loader.exec_module(mod)
    return mod


class Render(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / 'home'
        self.kube_dir = self.home / '.local/share/tbot/example/kube'
        self.kube_dir.mkdir(parents=True)
        self.kubeconfig = self.kube_dir / 'kubeconfig.yaml'
        self.kubeconfig.write_text('{}')
        self.tbot = self.home / '.local/bin/tbot'
        self.tbot.parent.mkdir(parents=True)
        self.tbot.write_text('#!/bin/sh\nexit 0\n')
        self.tbot.chmod(0o755)
        self.m = load_module()
        self.m.account_home = lambda: str(self.home)
        if not hasattr(self.m, 'render_bot_kubeconfig'):
            self.fail('render_bot_kubeconfig is missing')

    def view(self, cluster=None, user=None, context=None):
        cluster = cluster if cluster is not None else {
            'server': 'https://proxy.example.test:443/v1/teleport/example/lab-arm-a',
            'tls-server-name': 'kube-teleport-proxy-alpn.teleport.cluster.local',
            'certificate-authority-data': CA}
        user = user if user is not None else {'exec': {
            'command': str(self.tbot), 'args': ['kube', 'credentials', f'--destination-dir={self.kube_dir}'],
            'env': None, 'apiVersion': 'client.authentication.k8s.io/v1beta1',
            'provideClusterInfo': False, 'interactiveMode': 'IfAvailable'}}
        context = context if context is not None else {'cluster': CTX, 'user': 'example'}
        return {'kind': 'Config', 'apiVersion': 'v1', 'preferences': {},
                'clusters': [{'name': CTX, 'cluster': cluster}],
                'users': [{'name': 'example', 'user': user}],
                'contexts': [{'name': CTX, 'context': context}],
                'current-context': CTX}

    def render(self, view):
        return self.m.render_bot_kubeconfig(view, CTX, str(self.kubeconfig))

    def rendered(self, view):
        data, problem = self.render(view)
        self.assertIsNone(problem)
        return json.loads(data)

    def refused(self, view, needle):
        data, problem = self.render(view)
        self.assertIsNone(data)
        self.assertIn(needle, problem)

    # --- what the renderer copies ---

    def test_exec_shape_renders_only_tbot_fields(self):
        out = self.rendered(self.view())
        self.assertEqual(set(out), {'apiVersion', 'kind', 'clusters', 'users', 'contexts', 'current-context'})
        self.assertEqual(out['current-context'], CTX)
        self.assertEqual(len(out['clusters']), 1)
        self.assertEqual(set(out['clusters'][0]['cluster']), {'server', 'tls-server-name', 'certificate-authority-data'})
        self.assertEqual(out['users'][0]['user'], {'exec': {
            'apiVersion': 'client.authentication.k8s.io/v1beta1', 'command': str(self.tbot),
            'args': ['kube', 'credentials', f'--destination-dir={self.kube_dir}']}})

    def test_embedded_client_certificate_renders(self):
        out = self.rendered(self.view(user={'client-certificate-data': CERT, 'client-key-data': KEY}))
        self.assertEqual(out['users'][0]['user'], {'client-certificate-data': CERT, 'client-key-data': KEY})

    def test_namespace_is_kept(self):
        out = self.rendered(self.view(context={'cluster': CTX, 'user': 'example', 'namespace': 'ns'}))
        self.assertEqual(out['contexts'][0]['context']['namespace'], 'ns')

    def test_other_contexts_are_not_rendered(self):
        v = self.view()
        v['contexts'].append({'name': 'other', 'context': {'cluster': CTX, 'user': 'owner'}})
        v['users'].append({'name': 'owner', 'user': {'token': 'made-up'}})
        out = self.rendered(v)
        self.assertEqual([c['name'] for c in out['contexts']], [CTX])
        self.assertEqual([u['name'] for u in out['users']], ['example'])

    # --- the cluster entry (review findings 3 and 5) ---

    def test_cluster_refusals(self):
        base = self.view()['clusters'][0]['cluster']
        cases = {
            'http server': (dict(base, server='http://127.0.0.1:8001'), 'https'),
            'userinfo': (dict(base, server='https://alice:pw@proxy.example.test:443/x'), 'userinfo'),
            'proxy url': (dict(base, **{'proxy-url': 'http://127.0.0.1:3128'}), 'proxy-url'),
            'insecure': (dict(base, **{'insecure-skip-tls-verify': True}), 'insecure-skip-tls-verify'),
            'ca file': (dict({k: v for k, v in base.items() if k != 'certificate-authority-data'},
                             **{'certificate-authority': '/etc/ca.pem'}), 'certificate-authority'),
            'extensions': (dict(base, extensions=[{'name': 'x', 'extension': {}}]), 'extensions'),
        }
        for name, (cluster, needle) in cases.items():
            with self.subTest(name):
                self.refused(self.view(cluster=cluster), needle)

    # --- the user entry (review findings 1 and 2) ---

    def test_user_refusals(self):
        d = str(self.kube_dir)
        link = self.home / 'link'
        link.symlink_to(self.kube_dir.parent / 'other', target_is_directory=True)
        cases = {
            'cert file path': ({'client-certificate': '/x/tls.crt', 'client-key': '/x/tls.key'}, 'client-certificate'),
            'token': ({'token': 'made-up'}, 'token'),
            'dot-dot destination': (self.view()['users'][0]['user'] | {'exec': self.view()['users'][0]['user']['exec']
                                    | {'args': ['kube', 'credentials', f'--destination-dir={link}/../kube']}}, '..'),
            'destination elsewhere': (self.view()['users'][0]['user'] | {'exec': self.view()['users'][0]['user']['exec']
                                      | {'args': ['kube', 'credentials', f'--destination-dir={d}/x']}}, 'destination'),
        }
        for name, (user, needle) in cases.items():
            with self.subTest(name):
                self.refused(self.view(user=user), needle)

    def test_context_refusals(self):
        cases = {
            'extensions': ({'cluster': CTX, 'user': 'example', 'extensions': []}, 'extensions'),
            'missing user': ({'cluster': CTX, 'user': 'nobody'}, 'nobody'),
            'missing cluster': ({'cluster': 'nowhere', 'user': 'example'}, 'nowhere'),
        }
        for name, (context, needle) in cases.items():
            with self.subTest(name):
                self.refused(self.view(context=context), needle)

    def test_duplicate_names_refused(self):
        v = self.view()
        v['users'].append({'name': 'example', 'user': {'token': 'made-up'}})
        self.refused(v, 'example')

    # --- kubectl itself (review finding 4) ---

    def test_kubectl_binary_ignores_path_and_scripts(self):
        bindir = self.home / '.local/bin'
        wrapper = bindir / 'kubectl'
        wrapper.write_text('#!/bin/sh\ntsh status\nexec /usr/bin/kubectl "$@"\n')
        wrapper.chmod(0o755)
        old_path = os.environ.get('PATH', '')
        os.environ['PATH'] = f'{bindir}:{old_path}'
        try:
            chosen = self.m.kubectl_binary()
        except self.m.ProfileError as e:
            chosen = None
            self.assertEqual(e.code, 'kubectl_not_found')
        finally:
            os.environ['PATH'] = old_path
        self.assertNotEqual(chosen, str(wrapper), 'a script wrapper must never be chosen')
        if chosen is not None:
            self.assertTrue(os.path.isabs(chosen))
            with open(chosen, 'rb') as f:
                self.assertEqual(f.read(4), b'\x7fELF')

    def test_installed_elf_wrapper_cannot_run_tsh(self):
        compiler = shutil.which('cc', path='/usr/bin:/bin')
        self.assertIsNotNone(compiler, 'the compiled-wrapper reproduction requires cc')
        wrapper = self.home / '.local/bin/kubectl'
        source = textwrap.dedent(r'''
            #include <stdio.h>
            #include <stdlib.h>
            #include <string.h>
            #include <sys/wait.h>
            #include <unistd.h>
            int main(int argc, char **argv) {
                if (argc != 9 || strcmp(argv[1], "--kubeconfig") ||
                    strcmp(argv[3], "config") || strcmp(argv[4], "view")) return 90;
                if (getenv("REVIEW_CHECK_TSH")) {
                    pid_t child = fork();
                    if (child < 0) return 91;
                    if (child == 0) {
                        const char *tsh = getenv("REVIEW_FAKE_TSH");
                        execl(tsh, tsh, "status", (char *)NULL);
                        _exit(92);
                    }
                    if (waitpid(child, NULL, 0) < 0) return 93;
                }
                FILE *config = fopen(argv[2], "rb");
                if (!config) return 94;
                int c;
                while ((c = fgetc(config)) != EOF) putchar(c);
                return fclose(config);
            }
            ''')
        subprocess.run([compiler, '-x', 'c', '-', '-o', str(wrapper)], input=source,
                       capture_output=True, text=True, timeout=15, check=True,
                       env={'PATH': '/usr/bin:/bin'})
        fake_tsh = self.home / '.local/bin/tsh'
        log = self.home / 'fake-tsh.log'
        fake_tsh.write_text('#!/bin/sh\nprintf "tsh status\\n" >> "$REVIEW_TSH_LOG"\nexit 0\n')
        fake_tsh.chmod(0o755)
        self.kubeconfig.write_text(json.dumps(self.view()))
        env = {'PATH': '/usr/bin:/bin', 'REVIEW_FAKE_TSH': str(fake_tsh), 'REVIEW_TSH_LOG': str(log)}
        # Test the real ELF check at its default user path, excluding system binaries from execution.
        with mock.patch.object(self.m, 'KUBECTL_INSTALLS', ('~/.local/bin/kubectl',)), \
                mock.patch.dict(os.environ, env, clear=True):
            self.assertTrue(self.m.is_elf(wrapper))
            selected, fd = self.m.prepare_bot_kubeconfig(str(self.kubeconfig), CTX)
            os.close(fd)
            self.assertEqual(selected, str(wrapper))
            self.assertFalse(log.exists())
            # A terminal helper installs its compiled Teleport wrapper at ~/.local/bin/kubectl.
            os.environ['REVIEW_CHECK_TSH'] = 'yes'
            try:
                selected, fd = self.m.prepare_bot_kubeconfig(str(self.kubeconfig), CTX)
            except self.m.ProfileError:
                pass
            else:
                os.close(fd)
            self.assertFalse(log.exists(), 'the accepted ELF wrapper runs tsh during config view')


if __name__ == '__main__':
    unittest.main()

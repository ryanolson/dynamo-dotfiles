"""The tbot config and user unit that give agents their own Kubernetes identity.

The repository is public, so every internal value (Teleport proxy, join token,
storage directory name, cluster labels) comes from a `k8s_tbot` block in the
host's uncommitted chezmoi config. That block is also the opt-in: a host
without it gets neither file. These tests render with placeholder values only.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_SRC = ROOT / 'dot_config/tbot/k8s-agent.yaml.tmpl'
UNIT_SRC = ROOT / 'dot_config/systemd/user/tbot-k8s-agent.service.tmpl'
CONFIG_TARGET = '.config/tbot/k8s-agent.yaml'
UNIT_TARGET = '.config/systemd/user/tbot-k8s-agent.service'
BASE = 'data:\n  name: "N"\n  email: "e@example.com"\n  machine_class: "sudo"\n  machine_seed: "blank"\n' \
       '  cursor_ssh_host: "h"\n  onepassword:\n    enabled: false\n'

# Two hosts: one shares the bot ID and has tbot in /usr/local/bin; the other has
# its own token (in case it needs its own bot ID) and tbot in ~/.local/bin.
HOSTS = {
    'host-a': {'name': 'tenant-a', 'proxy_server': 'proxy.example.test:443', 'token': 'bot-a-join'},
    'host-b': {'name': 'tenant-a', 'proxy_server': 'proxy.example.test:443', 'token': 'bot-b-join'},
}
LABELS = {'resource-group': 'example-group', 'environment': 'dev'}


def block(host, labels=LABELS, **overrides):
    values = {**HOSTS[host], **overrides}
    out = '  k8s_tbot:\n' + ''.join(f'    {k}: "{v}"\n' for k, v in values.items() if v is not None)
    if labels is not None:
        out += '    selector_labels:\n' + ''.join(f'      {k}: "{v}"\n' for k, v in labels.items())
    return out


class K8sTbot(unittest.TestCase):
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
                              capture_output=True, text=True, env=self.env, check=check)

    def render(self, src, data, check=True):
        return self.chezmoi(data, 'execute-template', stdin=src.read_text(), check=check)

    def managed(self, data):
        return set(self.chezmoi(data, 'managed', '--include', 'files').stdout.split())

    def test_config_for_both_hosts(self):
        for host in HOSTS:
            with self.subTest(host=host):
                text = self.render(CONFIG_SRC, block(host)).stdout
                cfg = yaml.safe_load(text)
                share = self.home / '.local/share/tbot/tenant-a'
                self.assertEqual(cfg['version'], 'v2')
                self.assertEqual(cfg['proxy_server'], 'proxy.example.test:443')
                self.assertEqual(cfg['onboarding'], {'join_method': 'bound_keypair', 'token': HOSTS[host]['token']})
                # tbot does not expand $HOME, so every path is absolute under the rendered home.
                self.assertEqual(cfg['storage'], {'type': 'directory', 'path': str(share / 'storage')})
                self.assertEqual(cfg['credential_ttl'], '1h')
                self.assertEqual(cfg['renewal_interval'], '20m')
                [svc] = cfg['services']
                self.assertEqual(svc['type'], 'kubernetes/v2')
                self.assertEqual(svc['destination'], {'type': 'directory', 'path': str(share / 'kube')})
                # `true` makes DGD creates through the Python Kubernetes client fail with 403.
                self.assertIs(svc['disable_exec_plugin'], False)
                self.assertEqual(svc['selectors'], [{'labels': {'resource-group': ['example-group'],
                                                                'environment': ['dev']}}])

    @unittest.skipUnless(shutil.which('tbot'), 'tbot not installed')
    def test_selector_labels_match_tbot_v18_schema(self):
        rendered = yaml.safe_load(self.render(CONFIG_SRC, block('host-a')).stdout)
        env = {key: value for key, value in self.env.items()
               if not key.startswith(('TBOT_', 'TELEPORT_'))}
        # Use tbot's own generator as the control for the selector schema.
        result = subprocess.run(['tbot', 'configure', 'kubernetes/v2',
                                 '--proxy-server=proxy.example.test:443', '--token=bot-a-join',
                                 '--join-method=bound_keypair',
                                 f'--storage=file://{self.home}/storage',
                                 f'--destination=file://{self.home}/kube',
                                 '--label-selector=' + ','.join(f'{k}={v}' for k, v in LABELS.items())],
                                capture_output=True, text=True, env=env,
                                stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = yaml.safe_load(result.stdout)['services'][0]['selectors']
        self.assertEqual(expected, [{'labels': LABELS}])
        self.assertEqual(rendered['services'][0]['selectors'], expected)

    def test_rendered_files_hold_no_key_material_and_no_overwrite(self):
        for host in HOSTS:
            for src in (CONFIG_SRC, UNIT_SRC):
                with self.subTest(host=host, src=src.name):
                    text = self.render(src, block(host)).stdout
                    for bad in ('ssh-ed25519', 'PRIVATE KEY', 'BEGIN ', '--overwrite', 'keypair create', 'id_bkp.pub', '/opt/'):
                        self.assertNotIn(bad, text)
                    self.assertNotIn('$HOME', text)

    def test_an_empty_selector_is_refused(self):
        # An empty selector matches every cluster the bot can see, CI and production included.
        for labels in (None, {}):
            with self.subTest(labels=labels):
                result = self.render(CONFIG_SRC, block('host-a', labels=labels), check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('selector_labels', result.stderr)

    def test_missing_or_unsafe_values_are_refused(self):
        for key, value in (('token', None), ('proxy_server', None), ('name', None), ('name', '../x'),
                           ('name', 'a/b'), ('name', '')):
            with self.subTest(key=key, value=value):
                result = self.render(CONFIG_SRC, block('host-a', **{key: value}), check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f'k8s_tbot.{key}', result.stderr)

    def test_unit_finds_tbot_on_either_host_and_runs_one_bot(self):
        text = self.render(UNIT_SRC, block('host-a')).stdout
        lines = text.splitlines()
        self.assertIn('ExecStart=/usr/bin/env tbot start --config %h/.config/tbot/k8s-agent.yaml', lines)
        [path] = [line.split('=', 2)[2] for line in lines if line.startswith('Environment=PATH=')]
        dirs = path.split(':')
        # host-b installs tbot per user, host-a system-wide through /usr/local/bin.
        self.assertLess(dirs.index('%h/.local/bin'), dirs.index('/usr/local/bin'))
        # Without the key made by hand, tbot must not start: it would make a new key the bot does not know.
        self.assertIn('ConditionPathExists=%h/.local/share/tbot/tenant-a/storage/id_bkp', lines)
        self.assertIn('Restart=on-failure', lines)
        self.assertNotIn('Restart=always', lines)
        self.assertIn('WantedBy=default.target', lines)
        self.assertFalse([line for line in lines if line.startswith(('User=', 'Group=', 'PIDFile='))])

    @unittest.skipUnless(shutil.which('systemd-analyze'), 'systemd-analyze not installed')
    def test_unit_passes_systemd_verify(self):
        unit = self.home / 'tbot-k8s-agent.service'
        unit.write_text(self.render(UNIT_SRC, block('host-a')).stdout)
        result = subprocess.run(['systemd-analyze', '--user', 'verify', str(unit)], capture_output=True, text=True,
                                env=self.env, stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('tbot-k8s-agent.service:', result.stderr)

    def test_only_hosts_with_the_block_get_the_files(self):
        self.assertFalse({CONFIG_TARGET, UNIT_TARGET} & self.managed(''))
        self.assertLessEqual({CONFIG_TARGET, UNIT_TARGET}, self.managed(block('host-a')))

    def test_reinitializing_preserves_the_host_opt_in(self):
        self.assertLessEqual({CONFIG_TARGET, UNIT_TARGET}, self.managed(block('host-a')))
        self.chezmoi(block('host-a'), 'init', '--promptDefaults')
        regenerated = yaml.safe_load(self.config.read_text())
        self.assertEqual(regenerated['data']['name'], 'N')
        self.assertEqual(regenerated['data'].get('k8s_tbot'),
                         {**HOSTS['host-a'], 'selector_labels': LABELS})

    def test_key_storage_is_never_managed(self):
        for data in ('', block('host-a')):
            with self.subTest(opted_in=bool(data)):
                all_targets = self.chezmoi(data, 'managed').stdout.split()
                self.assertFalse([t for t in all_targets if t.startswith('.local/share/tbot')])

    def test_no_internal_value_in_the_sources(self):
        # The sources stay generic; the values live in the host's chezmoi config.
        for src in (CONFIG_SRC, UNIT_SRC):
            text = src.read_text()
            for bad in ('teleport.sh', 'nvidia', 'dgxc', 'rolson', 'dynamo'):
                self.assertNotIn(bad, text.lower(), src.name)


if __name__ == '__main__':
    unittest.main()

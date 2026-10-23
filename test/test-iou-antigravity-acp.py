"""Run iou_antigravity_acp against a local registry file and local archives.

The registry entry has the shape of the antigravity-acp entry in the ACP registry
(https://cdn.agentclientprotocol.com/registry/v1/latest/registry.json): one binary archive per
platform, with the command to run and, on Linux, an argument list.
"""
import http.server
import json
import os
from pathlib import Path
import platform
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
IOU = ROOT / 'dot_local/bin/executable_iou_antigravity_acp'
PYTHON = os.environ.get('AGY_ACP_PYTHON', 'python3')
PLATFORM = 'linux-aarch64'


def host_platform():
    system = {'Darwin': 'darwin', 'Linux': 'linux'}.get(platform.system())
    machine = {'arm64': 'aarch64', 'aarch64': 'aarch64', 'x86_64': 'x86_64', 'AMD64': 'x86_64'}.get(platform.machine())
    return f'{system}-{machine}' if system and machine else None


class IouAntigravityAcp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / 'servers'
        self.registry = self.tmp / 'registry.json'

    def archive(self, version, members=None):
        path = self.tmp / f'server-{version}.zip'
        members = members if members is not None else {
            'agy_acp_server.par': (0o755, f'server {version}'), 'localharness_external': (0o555, 'harness')}
        with zipfile.ZipFile(path, 'w') as archive:
            for name, (mode, content) in members.items():
                info = zipfile.ZipInfo(name)
                info.external_attr = (stat.S_IFREG | mode) << 16
                archive.writestr(info, content)
        return path

    def publish(self, version, archive, platform_key=PLATFORM, args=('--uid=',), agent='antigravity-acp'):
        entry = {'archive': archive.as_uri(), 'cmd': './agy_acp_server.par'}
        if args:
            entry['args'] = list(args)
        self.registry.write_text(json.dumps({'agents': [
            {'id': 'other', 'version': '0.0.1', 'distribution': {'binary': {}}},
            {'id': agent, 'version': version, 'distribution': {'binary': {platform_key: entry}}}]}))

    def run_iou(self, platform_key=PLATFORM):
        env = dict(os.environ, AGY_ACP_REGISTRY_URL=self.registry.as_uri(), AGY_ACP_SERVER_ROOT=str(self.root))
        env.pop('AGY_ACP_PLATFORM', None)
        if platform_key:
            env['AGY_ACP_PLATFORM'] = platform_key
        return subprocess.run([PYTHON, str(IOU)], capture_output=True, text=True, timeout=60, env=env)

    def install(self, version):
        self.publish(version, self.archive(version))
        result = self.run_iou()
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_installs_the_server_and_points_current_at_it(self):
        self.install('9.9.9')

        directory = self.root / '9.9.9'
        self.assertEqual((directory / 'agy_acp_server.par').read_text(), 'server 9.9.9')
        self.assertEqual(json.loads((directory / 'launch.json').read_text()),
                         {'version': '9.9.9', 'cmd': './agy_acp_server.par', 'args': ['--uid=']})
        self.assertEqual(os.readlink(self.root / 'current'), '9.9.9')
        self.assertEqual((self.root / 'current/launch.json').resolve(), (directory / 'launch.json').resolve())

    def test_keeps_the_permissions_that_the_archive_records(self):
        self.install('9.9.9')
        modes = {name: stat.S_IMODE((self.root / '9.9.9' / name).stat().st_mode)
                 for name in ('agy_acp_server.par', 'localharness_external')}
        self.assertTrue(modes['agy_acp_server.par'] & 0o100)
        self.assertTrue(modes['localharness_external'] & 0o100)

    def test_a_platform_without_arguments_gets_an_empty_list(self):
        self.publish('9.9.9', self.archive('9.9.9'), args=())
        self.assertEqual(self.run_iou().returncode, 0)
        self.assertEqual(json.loads((self.root / '9.9.9/launch.json').read_text())['args'], [])

    def test_a_second_run_with_the_same_version_downloads_nothing(self):
        self.install('9.9.9')
        archive = self.tmp / 'server-9.9.9.zip'
        archive.unlink()
        result = self.run_iou()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('already installed', result.stdout)

    def test_an_update_installs_the_new_version_and_removes_the_old_one(self):
        self.install('9.9.9')
        self.install('9.9.10')
        self.assertEqual(os.readlink(self.root / 'current'), '9.9.10')
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['9.9.10', 'current'])

    def test_an_unsafe_archive_installs_nothing(self):
        self.publish('9.9.9', self.archive('9.9.9', {'agy_acp_server.par': (0o755, 'x'), '../escape': (0o644, 'x')}))
        result = self.run_iou()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unsafe', result.stderr)
        self.assertFalse((self.tmp / 'escape').exists())
        self.assertEqual(list(self.root.iterdir()) if self.root.exists() else [], [])

    def test_an_archive_without_the_command_installs_nothing(self):
        self.publish('9.9.9', self.archive('9.9.9', {'other': (0o755, 'x')}))
        result = self.run_iou()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('agy_acp_server.par', result.stderr)
        self.assertEqual(list(self.root.iterdir()) if self.root.exists() else [], [])

    def test_a_failed_download_keeps_the_installed_version(self):
        self.install('9.9.9')
        self.publish('9.9.10', self.tmp / 'missing.zip')
        result = self.run_iou()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(os.readlink(self.root / 'current'), '9.9.9')
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['9.9.9', 'current'])

    def test_a_platform_that_the_registry_lacks_is_an_error_that_names_the_choices(self):
        self.publish('9.9.9', self.archive('9.9.9'), platform_key='darwin-aarch64')
        result = self.run_iou(platform_key='linux-aarch64')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('linux-aarch64', result.stderr)
        self.assertIn('darwin-aarch64', result.stderr)

    def test_a_registry_without_the_agent_is_an_error(self):
        self.publish('9.9.9', self.archive('9.9.9'), agent='someone-else')
        result = self.run_iou()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('antigravity-acp', result.stderr)

    def test_sends_a_user_agent_that_the_cdn_accepts(self):
        # cdn.agentclientprotocol.com and dl.google.com answer 403 to the default Python-urllib agent.
        archive = self.archive('9.9.9').read_bytes()
        registry = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.headers.get('User-Agent', '').startswith('Python-urllib'):
                    self.send_response(403)
                    self.end_headers()
                    return
                body = archive if self.path == '/server.zip' else registry[0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'
        entry = {'archive': f'{base}/server.zip', 'cmd': './agy_acp_server.par', 'args': ['--uid=']}
        registry.append(json.dumps({'agents': [{'id': 'antigravity-acp', 'version': '9.9.9',
                                                'distribution': {'binary': {PLATFORM: entry}}}]}).encode())
        self.registry = Path(self.tmp / 'unused.json')
        env = dict(os.environ, AGY_ACP_REGISTRY_URL=f'{base}/registry.json', AGY_ACP_SERVER_ROOT=str(self.root),
                   AGY_ACP_PLATFORM=PLATFORM)
        result = subprocess.run([PYTHON, str(IOU)], capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'current/agy_acp_server.par').exists())

    @unittest.skipUnless(host_platform(), 'unsupported host')
    def test_detects_the_platform_of_the_host(self):
        self.publish('9.9.9', self.archive('9.9.9'), platform_key=host_platform())
        result = self.run_iou(platform_key=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'current/agy_acp_server.par').exists())


if __name__ == '__main__':
    unittest.main()

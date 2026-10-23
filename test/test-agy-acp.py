"""Run agy-acp between a fake bb client and a stub Antigravity ACP server.

The stub speaks newline-delimited JSON-RPC like the real server and writes every line it gets to
STUB_LOG, so the tests see what reaches the server and in which order. The shapes of its
session/new reply and of its set_model reply come from agy_acp_server 1.4.1.
"""
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
AGY_ACP = ROOT / 'dot_local/bin/executable_agy-acp'
PYTHON = os.environ.get('AGY_ACP_PYTHON', 'python3')
ARGUMENT_LOG = 'argv.json'

STUB = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys, time
    log = open(os.environ['STUB_LOG'], 'a')
    json.dump(sys.argv[1:], open(os.path.join(os.path.dirname(os.environ['STUB_LOG']), 'argv.json'), 'w'))
    for raw in sys.stdin:
        log.write(raw); log.flush()
        msg = json.loads(raw)
        method, ident = msg.get('method'), msg.get('id')
        if ident is None:
            continue
        if method == 'session/new' and os.environ.get('STUB_FAIL_NEW'):
            print(json.dumps({'jsonrpc': '2.0', 'id': ident, 'error': {'code': -32000, 'message': 'Authentication required'}}),
                  flush=True)
            continue
        if method == 'session/new':
            result = {
                'sessionId': 'sess-1',
                'models': {'currentModelId': 'gemini-3.8-flash-high', 'availableModels': [
                    {'modelId': 'gemini-4-argon-high'}, {'modelId': 'gemini-3.8-flash-high'}]},
                'configOptions': [
                    {'id': 'model', 'category': 'model', 'currentValue': 'gemini-3.8-flash',
                     'options': [{'value': 'gemini-4-argon'}, {'value': 'gemini-3.8-flash'}]},
                    {'id': 'thinking_level', 'category': 'thought_level', 'currentValue': 'low',
                     'options': [{'value': 'low'}, {'value': 'high'}]},
                    {'id': 'mode', 'category': 'mode', 'currentValue': 'default',
                     'options': [{'value': 'default'}]}]}
        elif method == 'session/set_model':
            if os.environ.get('STUB_HANG_SET_MODEL'):
                continue
            if os.environ.get('STUB_FAIL_SET_MODEL'):
                print(json.dumps({'jsonrpc': '2.0', 'id': ident, 'error': {'code': -32602, 'message': 'no'}}),
                      flush=True)
                continue
            result = {}
        else:
            result = {'echo': method}
        print(json.dumps({'jsonrpc': '2.0', 'id': ident, 'result': result}), flush=True)
''')

LAUNCH = {'cmd': './agy_acp_server.par', 'args': ['--uid=']}


class Client:
    """A bb stand-in: sends lines to agy-acp and collects its output lines on a queue."""

    def __init__(self, test, env=None, argv=()):
        environment = dict(os.environ, AGY_ACP_SERVER_DIR=str(test.server), STUB_LOG=str(test.log))
        environment.pop('AGY_ACP_DEFAULT_MODEL', None)
        environment.update(env or {})
        self.proc = subprocess.Popen([PYTHON, str(AGY_ACP), *argv], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
        self.lines = queue.Queue()
        self.stderr = []
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=lambda: self.stderr.extend(self.proc.stderr), daemon=True).start()
        test.addCleanup(self.close)

    def _pump(self):
        for raw in self.proc.stdout:
            self.lines.put(raw)

    def send(self, message):
        self.proc.stdin.write((json.dumps(message) + '\n').encode())
        self.proc.stdin.flush()

    def call(self, ident, method, params=None, timeout=10):
        self.send({'jsonrpc': '2.0', 'id': ident, 'method': method, 'params': params or {}})
        return self.reply(timeout)

    def reply(self, timeout=10):
        return json.loads(self.lines.get(timeout=timeout))

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            stream.close()


class AgyAcp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.server = self.root / 'server'
        self.server.mkdir()
        self.log = self.root / 'received.log'
        self.write_server(LAUNCH)

    def write_server(self, launch):
        stub = self.server / 'agy_acp_server.par'
        stub.write_text(STUB.replace('#!/usr/bin/env python3', f'#!{sys.executable}'))
        stub.chmod(0o755)
        (self.server / 'launch.json').write_text(json.dumps(launch))

    def received(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def received_methods(self):
        return [m.get('method') for m in self.received()]

    def test_new_session_gets_the_default_model_before_the_client_sees_the_reply(self):
        client = Client(self)
        client.call(1, 'initialize', {'protocolVersion': 1})
        reply = client.call(2, 'session/new', {'cwd': '/tmp', 'mcpServers': []})

        set_model = [m for m in self.received() if m.get('method') == 'session/set_model']
        self.assertEqual([m['params'] for m in set_model],
                         [{'sessionId': 'sess-1', 'modelId': 'gemini-4-argon-high'}])
        self.assertEqual(reply['id'], 2)
        self.assertEqual(reply['result']['sessionId'], 'sess-1')
        self.assertEqual(reply['result']['models']['currentModelId'], 'gemini-4-argon-high')
        options = {o['id']: o['currentValue'] for o in reply['result']['configOptions']}
        self.assertEqual(options, {'model': 'gemini-4-argon', 'thinking_level': 'high', 'mode': 'default'})

    def test_the_default_model_request_never_reaches_the_client(self):
        client = Client(self)
        client.call(1, 'session/new')
        reply = client.call(2, 'ping')
        self.assertEqual(reply['id'], 2)
        self.assertTrue(client.lines.empty())

    def test_a_model_chosen_by_the_client_after_the_new_session_reaches_the_server(self):
        client = Client(self)
        client.call(1, 'session/new')
        reply = client.call(7, 'session/set_model', {'sessionId': 'sess-1', 'modelId': 'gemini-4-argon-medium'})
        self.assertEqual(reply, {'jsonrpc': '2.0', 'id': 7, 'result': {}})
        requested = [m['params']['modelId'] for m in self.received() if m.get('method') == 'session/set_model']
        self.assertEqual(requested, ['gemini-4-argon-high', 'gemini-4-argon-medium'])

    def test_the_environment_selects_another_default_model(self):
        client = Client(self, env={'AGY_ACP_DEFAULT_MODEL': 'gemini-3.8-flash-low'})
        reply = client.call(1, 'session/new')
        self.assertEqual(reply['result']['models']['currentModelId'], 'gemini-3.8-flash-low')
        self.assertEqual(self.received()[-1]['params']['modelId'], 'gemini-3.8-flash-low')

    def test_an_empty_default_model_leaves_the_session_alone(self):
        client = Client(self, env={'AGY_ACP_DEFAULT_MODEL': ''})
        reply = client.call(1, 'session/new')
        self.assertEqual(reply['result']['models']['currentModelId'], 'gemini-3.8-flash-high')
        self.assertEqual(self.received_methods(), ['session/new'])

    def test_a_rejected_default_model_returns_the_server_reply_unchanged(self):
        client = Client(self, env={'STUB_FAIL_SET_MODEL': '1'})
        reply = client.call(1, 'session/new')
        self.assertEqual(reply['result']['models']['currentModelId'], 'gemini-3.8-flash-high')
        self.assertIn('set_model', b''.join(client.stderr).decode())

    def test_a_silent_server_does_not_block_the_new_session_reply(self):
        client = Client(self, env={'STUB_HANG_SET_MODEL': '1', 'AGY_ACP_SET_MODEL_TIMEOUT': '0.5'})
        started = time.monotonic()
        reply = client.call(1, 'session/new', timeout=10)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(reply['result']['models']['currentModelId'], 'gemini-3.8-flash-high')

    def test_other_requests_pass_through_unchanged(self):
        client = Client(self)
        self.assertEqual(client.call('a', 'session/load', {'sessionId': 'old'}),
                         {'jsonrpc': '2.0', 'id': 'a', 'result': {'echo': 'session/load'}})
        self.assertEqual(self.received_methods(), ['session/load'])

    def test_a_failed_new_session_passes_through_without_a_model_request(self):
        client = Client(self, env={'STUB_FAIL_NEW': '1'})
        reply = client.call(1, 'session/new')
        self.assertEqual(reply['error']['message'], 'Authentication required')
        self.assertEqual(self.received_methods(), ['session/new'])

    def test_the_launch_file_and_the_command_line_set_the_server_arguments(self):
        client = Client(self, argv=['--debug'])
        client.call(1, 'initialize')
        self.assertEqual(json.loads((self.root / ARGUMENT_LOG).read_text()), ['--uid=', '--debug'])

    def test_a_missing_server_directory_stops_with_a_message(self):
        result = subprocess.run([PYTHON, str(AGY_ACP)], capture_output=True, text=True, timeout=30,
                                env=dict(os.environ, AGY_ACP_SERVER_DIR=str(self.root / 'nowhere')))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('iou_antigravity_acp', result.stderr)

    def test_a_launch_file_without_a_command_stops_with_a_message(self):
        (self.server / 'launch.json').write_text('{"args": []}')
        result = subprocess.run([PYTHON, str(AGY_ACP)], capture_output=True, text=True, timeout=30,
                                env=dict(os.environ, AGY_ACP_SERVER_DIR=str(self.server)))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('cmd', result.stderr)

    def test_closing_the_client_stops_the_server_and_exits(self):
        client = Client(self)
        client.call(1, 'initialize')
        client.proc.stdin.close()
        self.assertEqual(client.proc.wait(timeout=15), 0)

    def test_the_exit_status_of_the_server_is_the_exit_status_of_the_wrapper(self):
        (self.server / 'agy_acp_server.par').write_text(f'#!{sys.executable}\nimport sys\nsys.exit(7)\n')
        client = Client(self)
        self.assertEqual(client.proc.wait(timeout=15), 7)

    def test_termination_of_the_wrapper_stops_the_server(self):
        client = Client(self)
        client.call(1, 'initialize')
        client.proc.send_signal(signal.SIGTERM)
        client.proc.wait(timeout=15)
        time.sleep(0.3)
        out = subprocess.run(['pgrep', '-f', str(self.server / 'agy_acp_server.par')],
                             capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), '')


if __name__ == '__main__':
    unittest.main()

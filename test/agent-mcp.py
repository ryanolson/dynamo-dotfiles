"""Run the rendered agent MCP script with stub claude and codex binaries and a scratch home."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / 'run_after_agent-mcp.sh.tmpl'
ONE = [{'name': 'linear', 'command': 'linear-mcp', 'tools': ['claude', 'codex']}]


class AgentMcp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.log = self.root / 'log'

    def tool(self, name, fail_on=''):
        # `mcp add` registers the server; `mcp get` succeeds only for a registered one, as the real CLIs do.
        path = self.bin / name
        path.write_text('#!/bin/bash\necho "$(basename "$0") $*" >> "{log}"\n'
                        '[[ -n "{fail}" && "$*" == *"{fail}"* ]] && exit 1\n'
                        'case "$1 $2" in\n'
                        '  "mcp add") touch "{root}/registered-$(basename "$0")-$3"; [[ "$3" == --* ]] && '
                        'touch "{root}/registered-$(basename "$0")-$5" ;;\n'
                        '  "mcp get") [[ -e "{root}/registered-$(basename "$0")-$3" ]] || exit 1 ;;\n'
                        'esac\nexit 0\n'.format(log=self.log, fail=fail_on, root=self.root))
        path.chmod(0o755)

    def render(self, servers):
        config = self.root / 'chezmoi.json'
        config.write_text(json.dumps({'data': {'agent_mcp_servers': servers}}))
        return subprocess.run(['chezmoi', 'execute-template', '--config', str(config), '--source', str(ROOT)],
                              input=TEMPLATE.read_text(), capture_output=True, text=True, check=True).stdout

    def run_script(self, servers=ONE):
        script = self.root / 'mcp.sh'
        script.write_text(self.render(servers))
        result = subprocess.run(['bash', str(script)], capture_output=True, text=True, timeout=60,
                                env={'HOME': str(self.home), 'PATH': f'{self.bin}:/usr/bin:/bin'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def stamp(self):
        return self.home / '.local/state/dotfiles/agent-mcp'

    def add_commands(self, tool):
        return [c for c in self.calls() if c.startswith(f'{tool} mcp add')]

    def test_both_tools_register_the_wrapper_by_absolute_path(self):
        self.tool('claude')
        self.tool('codex')
        self.run_script()
        wrapper = f'{self.home}/.local/bin/linear-mcp'
        self.assertEqual(self.add_commands('claude'), [f'claude mcp add --scope user linear -- {wrapper}'])
        self.assertEqual(self.add_commands('codex'), [f'codex mcp add linear -- {wrapper}'])
        self.assertTrue(self.stamp().exists())

    def test_a_server_the_tool_already_has_is_left_alone(self):
        # A user who removed or edited the entry by hand keeps it: add runs only when get fails.
        self.tool('claude')
        (self.root / 'registered-claude-linear').touch()
        self.run_script()
        self.assertEqual(self.add_commands('claude'), [])

    def test_missing_tool_is_skipped(self):
        self.tool('codex')
        self.run_script()
        self.assertEqual(self.add_commands('claude'), [])
        self.assertEqual(len(self.add_commands('codex')), 1)

    def test_tools_list_limits_the_tools(self):
        self.tool('claude')
        self.tool('codex')
        self.run_script([dict(ONE[0], tools=['codex'])])
        self.assertEqual(self.add_commands('claude'), [])
        self.assertEqual(len(self.add_commands('codex')), 1)

    def test_failure_warns_and_writes_no_stamp(self):
        self.tool('claude', fail_on='mcp add')
        self.tool('codex')
        result = self.run_script()
        self.assertIn('WARN', result.stderr)
        self.assertEqual(len(self.add_commands('codex')), 1)
        self.assertFalse(self.stamp().exists())

    def test_the_stamp_skips_the_next_apply(self):
        self.tool('claude')
        self.run_script()
        calls = self.calls()
        self.run_script()
        self.assertEqual(self.calls(), calls)

    def test_a_changed_list_runs_again(self):
        self.tool('claude')
        self.run_script()
        self.run_script(ONE + [{'name': 'other', 'command': 'other-mcp', 'tools': ['claude']}])
        self.assertEqual(len(self.add_commands('claude')), 2)
        self.assertIn('claude mcp add --scope user other', self.add_commands('claude')[1])

    def test_no_tool_writes_no_stamp(self):
        self.run_script()
        self.assertFalse(self.stamp().exists())

    def test_empty_list_renders_no_script(self):
        self.assertEqual(self.render([]).strip(), '')


if __name__ == '__main__':
    unittest.main()

"""Run the rendered agent plugin script with stub claude and codex binaries and a scratch home."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / 'run_after_agent-plugins.sh.tmpl'
ONE = [{'marketplace': 'mkt', 'github': 'owner/repo', 'plugins': ['p1'], 'tools': ['claude', 'codex']}]
CLAUDE = ['claude plugin marketplace add owner/repo', 'claude plugin marketplace update mkt',
          'claude plugin install p1@mkt', 'claude plugin update p1@mkt']
CODEX = ['codex plugin marketplace add owner/repo', 'codex plugin marketplace upgrade mkt',
         'codex plugin add p1@mkt']


class AgentPlugins(unittest.TestCase):
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
        # Log the command and GIT_TERMINAL_PROMPT; exit 1 when the arguments contain fail_on.
        path = self.bin / name
        path.write_text('#!/bin/bash\necho "$(basename "$0") $* prompt=${{GIT_TERMINAL_PROMPT:-unset}}" >> "{}"\n'
                        '[[ -n "{}" && "$*" == *"{}"* ]] && exit 1\nexit 0\n'.format(self.log, fail_on, fail_on))
        path.chmod(0o755)

    def render(self, plugins):
        config = self.root / 'chezmoi.json'
        config.write_text(json.dumps({'data': {'agent_plugins': plugins}}))
        return subprocess.run(['chezmoi', 'execute-template', '--config', str(config), '--source', str(ROOT)],
                              input=TEMPLATE.read_text(), capture_output=True, text=True, check=True).stdout

    def run_script(self, plugins=ONE):
        script = self.root / 'plugins.sh'
        script.write_text(self.render(plugins))
        result = subprocess.run(['bash', str(script)], capture_output=True, text=True, timeout=60,
                                env={'HOME': str(self.home), 'PATH': '{}:/usr/bin:/bin'.format(self.bin)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def calls(self):
        lines = self.log.read_text().splitlines() if self.log.exists() else []
        return [line.rsplit(' prompt=', 1)[0] for line in lines]

    def stamp(self):
        return self.home / '.local/state/dotfiles/agent-plugins'

    def test_both_tools_add_install_and_update_in_order(self):
        self.tool('claude')
        self.tool('codex')
        self.run_script()
        self.assertEqual(self.calls(), CLAUDE + CODEX)
        self.assertTrue(self.stamp().exists())

    def test_git_never_prompts(self):
        self.tool('claude')
        self.run_script()
        self.assertTrue(all(line.endswith(' prompt=0') for line in self.log.read_text().splitlines()))

    def test_missing_tool_is_skipped(self):
        self.tool('codex')
        self.run_script()
        self.assertEqual(self.calls(), CODEX)

    def test_tools_list_limits_the_tools(self):
        self.tool('claude')
        self.tool('codex')
        self.run_script([dict(ONE[0], tools=['codex'])])
        self.assertEqual(self.calls(), CODEX)

    def test_failure_warns_stops_that_tool_and_writes_no_stamp(self):
        self.tool('claude', fail_on='marketplace add')
        self.tool('codex')
        result = self.run_script()
        self.assertIn('WARN', result.stderr)
        self.assertIn('gh auth setup-git', result.stderr)
        self.assertEqual(self.calls(), CLAUDE[:1] + CODEX)
        self.assertFalse(self.stamp().exists())

    def test_fresh_stamp_skips_the_next_apply(self):
        self.tool('claude')
        self.run_script()
        self.run_script()
        self.assertEqual(self.calls(), CLAUDE)

    def test_changed_list_runs_before_the_stamp_expires(self):
        self.tool('claude')
        self.run_script()
        self.run_script([dict(ONE[0], plugins=['p1', 'p2'])])
        self.assertEqual(len(self.calls()), 4 + 8)

    def test_stamp_older_than_a_week_updates_again(self):
        self.tool('claude')
        self.run_script()
        old = time.time() - 8 * 24 * 3600
        os.utime(str(self.stamp()), (old, old))
        self.run_script()
        self.assertEqual(self.calls(), CLAUDE + CLAUDE)

    def test_no_tool_writes_no_stamp(self):
        self.run_script()
        self.assertFalse(self.stamp().exists())

    def test_empty_list_renders_no_script(self):
        self.assertEqual(self.render([]).strip(), '')


if __name__ == '__main__':
    unittest.main()

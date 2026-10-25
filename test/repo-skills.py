"""Check the repo-skills script on temporary repositories and skill trees."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'dot_agents' / 'skills' / 'repo-skills' / 'scripts' / 'repo_skills.py'


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class RepoSkills(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.source = root / 'source'
        self.repo = root / 'repo'
        write(self.source / 'plain' / 'SKILL.md', '---\nname: plain\n---\nWrite short text.\n')
        write(self.source / 'plain' / 'references' / 'rules.md', 'Rule one.\n')
        write(self.source / 'personal' / 'SKILL.md', '---\nname: personal\n---\nRead ~/.agents/ENVIRONMENT.md first.\n')
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.set_remote('git@github.com:ai-dynamo/velo.git')
        (self.repo / '.agents' / 'skills').mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def set_remote(self, url):
        subprocess.run(['git', '-C', str(self.repo), 'remote', 'remove', 'origin'], capture_output=True)
        subprocess.run(['git', '-C', str(self.repo), 'remote', 'add', 'origin', url], check=True)

    def run_script(self, *args, check=True):
        r = subprocess.run(['python3', '-I', str(SCRIPT), *args, '--repo', str(self.repo), '--source', str(self.source)],
                           capture_output=True, text=True)
        if check and r.returncode != 0:
            self.fail(f'exit {r.returncode}: {r.stderr}')
        return r

    def status(self, *names):
        return json.loads(self.run_script('status', '--json', *names).stdout)

    def state(self, name):
        return {s['name']: s for s in self.status(name)['skills']}[name]['state']

    def test_personal_orgs_are_skipped(self):
        for url in ('git@github.com:ryanolson/gdl.git', 'https://github.com/NVIDIA-dev/dynamo-gdl.git'):
            self.set_remote(url)
            out = self.status()
            self.assertEqual(out['org_class'], 'personal')
            self.assertEqual(out['skills'], [])

    def test_unknown_org_is_reported(self):
        self.set_remote('git@github.com:someone/else.git')
        self.assertEqual(self.status()['org_class'], 'unknown')

    def test_missing_skill_is_new(self):
        self.assertEqual(self.state('plain'), 'new')

    def test_copy_then_unchanged(self):
        self.run_script('copy', 'plain')
        self.assertEqual((self.repo / '.agents/skills/plain/references/rules.md').read_text(), 'Rule one.\n')
        self.assertEqual(self.state('plain'), 'unchanged')

    def test_source_edit_is_mine_updated(self):
        self.run_script('copy', 'plain')
        write(self.source / 'plain' / 'SKILL.md', '---\nname: plain\n---\nWrite shorter text.\n')
        self.assertEqual(self.state('plain'), 'mine-updated')

    def test_repo_edit_is_theirs_updated_and_copy_refuses(self):
        self.run_script('copy', 'plain')
        write(self.repo / '.agents/skills/plain/SKILL.md', 'A teammate changed this.\n')
        self.assertEqual(self.state('plain'), 'theirs-updated')
        r = self.run_script('copy', 'plain', check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual((self.repo / '.agents/skills/plain/SKILL.md').read_text(), 'A teammate changed this.\n')

    def test_both_edited_is_both_changed(self):
        self.run_script('copy', 'plain')
        write(self.repo / '.agents/skills/plain/SKILL.md', 'A teammate changed this.\n')
        write(self.source / 'plain' / 'SKILL.md', 'I changed this.\n')
        self.assertEqual(self.state('plain'), 'both-changed')

    def test_repo_copy_without_manifest_is_first_contact_and_mark_records_it(self):
        write(self.repo / '.agents/skills/plain/SKILL.md', 'Upstream version.\n')
        self.assertEqual(self.state('plain'), 'first-contact')
        self.run_script('mark', 'plain')
        self.assertEqual(self.state('plain'), 'mine-updated')

    def test_nonportable_skill_is_flagged_and_not_copied(self):
        out = {s['name']: s for s in self.status('plain', 'personal')['skills']}
        self.assertTrue(any('ENVIRONMENT.md' in h for h in out['personal']['portability']))
        self.assertEqual(out['plain']['portability'], [])
        r = self.run_script('copy', 'personal', check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.repo / '.agents/skills/personal').exists())

    def test_default_status_covers_synced_and_shared_skills_only(self):
        self.run_script('copy', 'plain')
        write(self.repo / '.agents/skills/their-review/SKILL.md', 'Their own skill.\n')
        out = self.status()
        self.assertEqual([s['name'] for s in out['skills']], ['plain'])
        self.assertEqual(out['repo_only'], ['their-review'])

    def test_names_may_follow_options(self):
        r = subprocess.run(['python3', '-I', str(SCRIPT), 'status', '--repo', str(self.repo), '--source',
                            str(self.source), '--json', 'plain'], capture_output=True, text=True, check=True)
        self.assertEqual([s['name'] for s in json.loads(r.stdout)['skills']], ['plain'])

    def test_claude_only_layout_is_used_and_gap_reported(self):
        (self.repo / '.agents' / 'skills').rmdir()
        (self.repo / '.agents').rmdir()
        (self.repo / '.claude' / 'skills').mkdir(parents=True)
        out = self.status()
        self.assertEqual(out['skills_dir'], '.claude/skills')
        self.assertTrue(any('.agents/skills' in g for g in out['gaps']))
        self.run_script('copy', 'plain')
        self.assertTrue((self.repo / '.claude/skills/plain/SKILL.md').exists())


if __name__ == '__main__':
    unittest.main()

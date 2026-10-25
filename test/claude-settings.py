"""Run the ~/.claude/settings.json modify script on sample files."""
import json
from pathlib import Path
import subprocess
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'dot_claude' / 'modify_private_settings.json'


class ClaudeSettings(unittest.TestCase):
    def modify(self, current):
        out = subprocess.run(['bash', str(SCRIPT)], input=current, capture_output=True, text=True, check=True).stdout
        return out, json.loads(out)

    def test_new_file_gets_empty_attribution(self):
        _, d = self.modify('')
        self.assertEqual(d, {'attribution': {'commit': '', 'pr': ''}})

    def test_other_keys_stay_and_attribution_is_replaced(self):
        _, d = self.modify(json.dumps({'model': 'opus', 'attribution': {'commit': 'x'}, 'env': {'A': '1'}}))
        self.assertEqual(d, {'model': 'opus', 'attribution': {'commit': '', 'pr': ''}, 'env': {'A': '1'}})

    def test_a_second_run_changes_nothing(self):
        once, _ = self.modify('{"model": "opus"}')
        self.assertEqual(self.modify(once)[0], once)

    def test_invalid_json_fails_instead_of_overwriting(self):
        r = subprocess.run(['bash', str(SCRIPT)], input='{not json', capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)


if __name__ == '__main__':
    unittest.main()

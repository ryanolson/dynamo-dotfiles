"""Drive init_dotfiles from bootstrap.sh with a stub chezmoi whose apply fails once."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BootstrapApply(unittest.TestCase):
    def test_failed_entry_does_not_stop_the_rest_of_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            # bootstrap.sh ends with `main "$@"`; drop that line so its functions can be sourced.
            lines = (ROOT / 'bootstrap.sh').read_text().splitlines()
            self.assertEqual(lines[-1].strip(), 'main "$@"')
            (tmp / 'bootstrap.sh').write_text('\n'.join(lines[:-1]) + '\n')
            log = tmp / 'log'
            # Like chezmoi: without --keep-going, apply stops at the failing install script and skips
            # the entries after it. With --keep-going, it applies them and still exits 1.
            stub = tmp / 'chezmoi'
            stub.write_text('#!/bin/bash\necho "$*" >> "{log}"\n'
                            'if [ "$1" = apply ]; then\n'
                            '  case " $* " in *" --keep-going "*) echo rest-applied >> "{log}" ;; esac\n'
                            '  exit 1\nfi\n'.format(log=log))
            stub.chmod(0o755)
            result = subprocess.run(
                ['bash', '-c', 'source "$1"; NO_SUDO=0; REPO_URL=x; init_dotfiles', 'bash', str(tmp / 'bootstrap.sh')],
                env={'HOME': str(tmp), 'PATH': '{}:/usr/bin:/bin'.format(tmp)}, capture_output=True, text=True,
                timeout=30)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn('rest-applied', log.read_text())


if __name__ == '__main__':
    unittest.main()

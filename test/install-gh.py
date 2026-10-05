"""Source the rendered install script with stub commands and drive its GitHub CLI steps."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / 'run_onchange_install-packages.sh.tmpl'

# Each stub appends its argv to $STUB_LOG. pixi exposes a gh binary, as `pixi global install gh` does.
STUBS = {
    'pixi': 'if [ "$*" = "global install gh" ]; then [ -n "${PIXI_FAIL:-}" ] && exit 1;'
            ' printf "#!/bin/sh\\necho pixi-gh\\n" > "$(dirname "$0")/gh"; chmod +x "$(dirname "$0")/gh"; fi',
    'sudo': '"$@"',
    'apt-get': ':',
    'dpkg': '[ "$1 $2" = "-s gh" ] && [ -e "$APT_GH" ]',
    # The pixi installer honors PIXI_NO_PATH_UPDATE. Fail the test if the installer does not receive it.
    'curl': 'printf "%s\\n" \'[ "$PIXI_NO_PATH_UPDATE" = 1 ] || exit 9\' \'mkdir -p "$HOME/.pixi/bin"\''
            ' "cp \\"$STUB_DIR/pixi\\" \\"\\$HOME/.pixi/bin/pixi\\""',
}


class InstallGh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / 'home'
        self.home.mkdir()
        self.stubs = root / 'stubs'
        self.stubs.mkdir()
        self.log = root / 'log'
        self.apt_list = root / 'github-cli.list'
        self.apt_keyring = root / 'githubcli-archive-keyring.gpg'
        self.apt_gh = root / 'apt-gh-installed'
        for path in (self.apt_list, self.apt_keyring, self.apt_gh):
            path.write_text('x')
        for name, body in STUBS.items():
            stub = self.stubs / name
            stub.write_text('#!/bin/bash\necho "{} $*" >> "$STUB_LOG"\n{}\n'.format(name, body))
            stub.chmod(0o755)
        script = TEMPLATE.read_text().replace('{{ .machine_class }}', 'headless-sudo')
        script = script.replace('{{ .chezmoi.arch }}', 'arm64')
        self.script = root / 'install.sh'
        self.script.write_text(script)

    def run_steps(self, *steps, **extra):
        env = {'HOME': str(self.home), 'PATH': '{}:/usr/bin:/bin'.format(self.stubs), 'STUB_LOG': str(self.log),
               'STUB_DIR': str(self.stubs), 'APT_GH': str(self.apt_gh), 'GH_APT_LIST': str(self.apt_list),
               'GH_APT_KEYRING': str(self.apt_keyring)}
        env.update(extra)
        command = 'source "$1"; ' + '; '.join(steps)
        result = subprocess.run(['bash', '-c', command, 'bash', str(self.script)], env=env, capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_installs_pixi_and_gh_then_retires_apt_gh(self):
        # PATH has no pixi. ensure_pixi installs it to ~/.pixi/bin, and pixi_cmd must find it there.
        template = Path(self.tmp.name) / 'pixi-template'
        (self.stubs / 'pixi').rename(template)
        curl = self.stubs / 'curl'
        curl.write_text(curl.read_text().replace('$STUB_DIR/pixi', str(template)))
        log = self.run_steps('install_gh')
        self.assertIn('pixi global install gh', log)
        link = self.home / '.local/bin/gh'
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(str(link)), str(self.home / '.pixi/bin/gh'))
        self.assertIn('apt-get remove -y -qq gh', log)

    def test_pixi_failure_keeps_apt_gh(self):
        log = self.run_steps('install_gh', PIXI_FAIL='1')
        self.assertNotIn('apt-get remove -y -qq gh', log)
        self.assertFalse((self.home / '.local/bin/gh').exists())

    def test_no_apt_gh_means_no_removal(self):
        self.apt_gh.unlink()
        log = self.run_steps('install_gh')
        self.assertNotIn('apt-get remove -y -qq gh', log)
        self.assertTrue((self.home / '.local/bin/gh').is_symlink())

    def test_apt_repository_is_removed(self):
        self.run_steps('remove_gh_apt_repository')
        self.assertFalse(self.apt_list.exists())
        self.assertFalse(self.apt_keyring.exists())
        # A second run has nothing to remove and must not fail.
        self.run_steps('remove_gh_apt_repository')

    def test_existing_local_gh_file_is_not_replaced(self):
        local = self.home / '.local/bin/gh'
        local.parent.mkdir(parents=True)
        local.write_text('someone else')
        log = self.run_steps('install_gh')
        self.assertEqual(local.read_text(), 'someone else')
        self.assertNotIn('apt-get remove -y -qq gh', log)


if __name__ == '__main__':
    unittest.main()

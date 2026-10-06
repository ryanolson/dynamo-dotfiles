"""Source the rendered install script with stub commands and drive its pixi tool steps."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / 'run_onchange_install-packages.sh.tmpl'

# Each stub appends its argv to $STUB_LOG. `pixi global install PKG` exposes the binary of PKG,
# as the real command does, unless PKG is in $PIXI_FAIL.
PIXI = r'''
if [ "$1 $2" = "global install" ]; then
    pkg="${3%%==*}"
    case " ${PIXI_FAIL:-} " in *" $pkg "*) exit 1 ;; esac
    case "$pkg" in ripgrep) bin=rg ;; fd-find) bin=fd ;; helix) bin=hx ;; nodejs) bin=node ;; *) bin="$pkg" ;; esac
    expose="${PIXI_HOME:-$HOME/.pixi}/bin"
    mkdir -p "$expose"
    printf '#!/bin/sh\necho pixi-%s\n' "$bin" > "$expose/$bin"
    chmod +x "$expose/$bin"
fi
'''
STUBS = {
    'sudo': '"$@"',
    'apt-get': '[ "$1" = remove ] && [ -n "${APT_REMOVE_FAIL:-}" ] && exit 1; :',
    'dpkg': '[ "$1" = -s ] && [ -e "$APT_DIR/$2" ]',
    # The pixi installer honors PIXI_NO_PATH_UPDATE. Fail if the installer does not receive it.
    'curl': 'printf "%s\\n" \'[ "$PIXI_NO_PATH_UPDATE" = 1 ] || exit 9\' \'d="${PIXI_HOME:-$HOME/.pixi}/bin"; mkdir -p "$d"\''
            ' "cp \\"$PIXI_TEMPLATE\\" \\"\\$d/pixi\\""',
}
MIGRATED_BINARIES = ['bat', 'eza', 'rg', 'fd', 'zoxide', 'dust', 'procs', 'hx', 'zellij', 'lazygit', 'yazi',
                     'broot', 'just', 'watchexec', 'hyperfine', 'tokei', 'starship', 'rclone', 'gh']


class PixiTools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / 'home'
        self.home.mkdir()
        self.stubs = root / 'stubs'
        self.stubs.mkdir()
        self.log = root / 'log'
        self.system_bin = root / 'usr-local-bin'
        self.system_bin.mkdir()
        self.apt = root / 'apt'
        self.apt.mkdir()
        self.apt_list = root / 'github-cli.list'
        self.apt_keyring = root / 'githubcli-archive-keyring.gpg'
        for path in (self.apt_list, self.apt_keyring, self.apt / 'gh', self.apt / 'rclone'):
            path.write_text('x')
        for name in MIGRATED_BINARIES + ['kubectl']:
            (self.system_bin / name).write_text('release copy')
        self.pixi_template = root / 'pixi-template'
        self.pixi_template.write_text('#!/bin/bash\necho "pixi $*" >> "$STUB_LOG"\n' + PIXI)
        self.pixi_template.chmod(0o755)
        for name, body in STUBS.items():
            stub = self.stubs / name
            stub.write_text('#!/bin/bash\necho "{} $*" >> "$STUB_LOG"\n{}\n'.format(name, body))
            stub.chmod(0o755)

    def render(self, machine_class):
        script = TEMPLATE.read_text().replace('{{ .machine_class }}', machine_class)
        path = Path(self.tmp.name) / 'install-{}.sh'.format(machine_class)
        path.write_text(script.replace('{{ .chezmoi.arch }}', 'arm64'))
        return path

    def run_steps(self, *steps, machine_class='headless-sudo', pixi_on_path=True, expect=0, **extra):
        if pixi_on_path:
            pixi = self.stubs / 'pixi'
            pixi.write_bytes(self.pixi_template.read_bytes())
            pixi.chmod(0o755)
        env = {'HOME': str(self.home), 'PATH': '{}:/usr/bin:/bin'.format(self.stubs), 'STUB_LOG': str(self.log),
               'PIXI_TEMPLATE': str(self.pixi_template), 'APT_DIR': str(self.apt),
               'SYSTEM_BIN_DIR': str(self.system_bin), 'GH_APT_LIST': str(self.apt_list),
               'GH_APT_KEYRING': str(self.apt_keyring)}
        env.update(extra)
        command = 'source "$1"; ' + '; '.join(steps)
        result = subprocess.run(['bash', '-c', command, 'bash', str(self.render(machine_class))], env=env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return self.log.read_text().splitlines() if self.log.exists() else []

    def installed(self, log):
        """Return the package names that were installed, without version pins."""
        return [line.split()[3].split('==')[0] for line in log if re.match(r'pixi global install \S+$', line)]

    def pixi_bin(self):
        return self.home / '.pixi/bin'

    def test_fresh_machine_gets_pixi_and_every_tool(self):
        log = self.run_steps('install_pixi_tools', pixi_on_path=False)
        installed = self.installed(log)
        self.assertIn('fd-find', installed)
        self.assertNotIn('fd', installed)
        self.assertIn('broot', installed)
        self.assertIn('tokei', installed)
        for name in MIGRATED_BINARIES:
            self.assertTrue((self.pixi_bin() / name).exists(), name)
            self.assertFalse((self.system_bin / name).exists(), name)
        self.assertEqual((self.system_bin / 'kubectl').read_text(), 'release copy')
        # Every tool gets a link: services and agents that bb starts lack ~/.pixi/bin in PATH.
        for name in MIGRATED_BINARIES:
            link = self.home / '.local/bin' / name
            self.assertEqual(os.readlink(str(link)), str(self.pixi_bin() / name), name)
        removed = sorted(line.split()[-1] for line in log if line.startswith('apt-get remove'))
        self.assertEqual(removed, ['gh', 'rclone'])

    def test_critical_tools_are_pinned(self):
        log = self.run_steps('install_pixi_tools')
        pins = sorted(line.split()[3] for line in log if re.match(r'pixi global install \S+==\S+$', line))
        self.assertEqual([pin.split('==')[0] for pin in pins], ['gh', 'rclone', 'zellij'])
        self.assertIn('pixi global install bat', log)

    def test_failed_floating_tool_only_warns(self):
        self.run_steps('install_pixi_tools', PIXI_FAIL='tokei bat')
        self.assertEqual((self.system_bin / 'bat').read_text(), 'release copy')
        self.assertFalse((self.system_bin / 'eza').exists())

    def test_failed_tool_keeps_its_release_copy(self):
        self.run_steps('install_pixi_tools', PIXI_FAIL='zellij', expect=1)
        self.assertEqual((self.system_bin / 'zellij').read_text(), 'release copy')
        self.assertFalse((self.system_bin / 'bat').exists())

    def test_pixi_found_on_path_still_uses_the_expose_folder(self):
        # pixi on PATH outside ~/.pixi/bin (here: the stub folder) still exposes into ~/.pixi/bin.
        log = self.run_steps('install_pixi_tools')
        self.assertIn('gh', self.installed(log))
        self.assertEqual(os.readlink(str(self.home / '.local/bin/gh')), str(self.pixi_bin() / 'gh'))
        self.assertFalse((self.system_bin / 'bat').exists())

    def test_failed_apt_removal_is_a_failure(self):
        log = self.run_steps('install_pixi_tools', APT_REMOVE_FAIL='1', expect=1)
        self.assertIn('apt-get remove -y -qq gh', log)
        self.assertTrue((self.home / '.local/bin/gh').is_symlink())

    def test_failed_link_keeps_the_release_copy(self):
        # ~/.local/bin is a file, so no link can be made there.
        (self.home / '.local').mkdir()
        (self.home / '.local/bin').write_text('not a folder')
        log = self.run_steps('install_pixi_tools', expect=1)
        self.assertEqual((self.system_bin / 'bat').read_text(), 'release copy')
        self.assertNotIn('apt-get remove -y -qq gh', log)

    def test_failed_gh_keeps_apt_gh(self):
        log = self.run_steps('install_pixi_tools', PIXI_FAIL='gh', expect=1)
        self.assertNotIn('apt-get remove -y -qq gh', log)
        self.assertFalse((self.home / '.local/bin/gh').exists())
        self.assertIn('apt-get remove -y -qq rclone', log)

    def test_no_apt_package_means_no_removal(self):
        (self.apt / 'gh').unlink()
        log = self.run_steps('install_pixi_tools')
        self.assertNotIn('apt-get remove -y -qq gh', log)
        self.assertTrue((self.home / '.local/bin/gh').is_symlink())

    def test_local_file_is_not_replaced(self):
        local = self.home / '.local/bin/gh'
        local.parent.mkdir(parents=True)
        local.write_text('someone else')
        log = self.run_steps('install_pixi_tools')
        self.assertEqual(local.read_text(), 'someone else')
        self.assertNotIn('apt-get remove -y -qq gh', log)

    def test_apt_repository_is_removed(self):
        self.run_steps('remove_gh_apt_repository')
        self.assertFalse(self.apt_list.exists())
        self.assertFalse(self.apt_keyring.exists())
        self.run_steps('remove_gh_apt_repository')

    def test_nosudo_installs_into_the_arch_root_only(self):
        log = self.run_steps('install_pixi_tools', machine_class='headless-nosudo', pixi_on_path=False)
        arch_pixi = self.home / '.local' / os.uname().machine / 'pixi/bin'
        self.assertTrue((arch_pixi / 'gh').exists())
        self.assertFalse(any(line.startswith(('sudo', 'apt-get')) for line in log), log)
        installed = self.installed(log)
        for package in ('fish', 'nodejs', 'git', 'uv', 'fd-find', 'broot'):
            self.assertIn(package, installed)
        self.assertNotIn('fd', installed)
        # ~/.local/bin is shared across architectures there, so it must not link an arch-specific binary.
        self.assertFalse((self.home / '.local/bin/gh').exists())
        self.assertEqual((self.system_bin / 'bat').read_text(), 'release copy')


if __name__ == '__main__':
    unittest.main()

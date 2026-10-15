# Dynamo Development Environment

A modern, cross-platform development environment using native package managers and dotfile management.

[Kubernetes, Tailscale, and ephemeral worker provisioning](agent-docs/service-and-worker-provisioning.md) records the shared provisioning requirements and pending work.

[Worker client certificates](agent-docs/worker-certificates.md) describes `bb-worker-cert`, SSH renewal, and the copy/paste signing flow. Signing profiles and deployment configuration stay outside this repository.

## 🚀 Quick Start

**One-command installation:**
```bash
curl -fsSL https://raw.githubusercontent.com/ryanolson/dynamo-dotfiles/main/bootstrap.sh | bash
```

**No-sudo (SLURM login node / shared HPC):** everything installs into `$HOME`.
```bash
curl -fsSL https://raw.githubusercontent.com/ryanolson/dynamo-dotfiles/main/bootstrap.sh | bash -s -- --no-sudo
```
(`--no-sudo` is auto-detected when `sudo` is absent.)

### Machine classes

`chezmoi init` prompts for a **machine class** that selects the install path:

| Class    | Install path                | Sudo | Agent infra |
|----------|-----------------------------|------|-------------|
| `sudo`   | apt + pixi                  | yes  | yes         |
| `nosudo` | `~/.local/<arch>/` via pixi | no   | no          |

`chezmoi init` also asks once whether the machine has the 1Password CLI and administers the fleet. The answer sets `onepassword.enabled`, and later runs of `chezmoi init` keep it. It turns on the 1Password CLI, `openv`, and commit signing with a 1Password key. On a fleet machine, the fleet agent signs commits instead (see Fleet certificates). Configurations from before 2026-10-14 used the classes `primary`, `headless-sudo`, and `headless-nosudo`. `chezmoi init` converts them to `sudo` or `nosudo` and keeps their 1Password settings.

On `nosudo` the install is **pixi-centric**: nearly the whole toolset
(`fish`, `node`, `git`, `gh`, `bat`, `ripgrep`, `fd`, `helix`, `zellij`, `lazygit`,
`starship`, `uv`, …) comes from [pixi](https://pixi.sh)/conda-forge — glibc-independent
and arch-aware. Only `claude` (native installer) and `codex` (npm) are separate.

On the sudo classes, the command-line tools also come from pixi, in `~/.pixi/bin`: `gh`, `bat`, `eza`, `ripgrep`, `fd`, `zoxide`, `dust`, `procs`, `helix`, `zellij`, `lazygit`, `yazi`, `broot`, `just`, `watchexec`, `hyperfine`, `tokei`, `starship`, and `rclone`. apt provides the login shell `fish`, the build dependencies, and small system tools. `kubectl` comes from the official download, `uv` and `rustup` from their own installers, and the 1Password CLI from its apt repository.

- The install script links each tool into `~/.local/bin`. Services, cron jobs, and the agents that a bb server starts often have `~/.local/bin` but not `~/.pixi/bin` in `PATH`, and git's HTTPS credential helper runs `gh`.
- After the link exists, the script removes the tool's copy from `/usr/local/bin`, and the apt packages of `gh` and `rclone`. Copies that you installed elsewhere, for example with `cargo install`, stay. In fish, `~/.pixi/bin` comes before them in `PATH`.
- `gh`, `zellij`, and `rclone` are critical tools. `PIXI_TOOLS` pins each one to a version, and each machine installs that version. To update a critical tool, change its version in `PIXI_TOOLS`; each machine installs the new version on its next `chezmoi update`.
- If a pixi install, a link, or a removal fails, the tool keeps its old copy. For a critical tool, the script also exits with an error, and chezmoi runs it again on the next `chezmoi apply` or `chezmoi-headless-update`. For the other tools, the script only warns.
- `run_after_update-pixi-tools` runs `pixi global update` at most once per 7 days, on each `chezmoi apply` or `chezmoi update`. It updates the other tools and keeps the pins. A failed update only warns, and the next apply tries again.

**Multi-architecture shared `$HOME`** (e.g. an x86_64 SLURM login node with aarch64
GB200 compute nodes mounting the same home): everything installs under an
**arch-namespaced root** `~/.local/<uname -m>/` (`bin`, `pixi`, `npm`), and the
shell rc selects the right one via `uname -m` at startup. Run bootstrap **once per
architecture** (on the login node and on one compute node); the shared dotfiles are
arch-aware, so both just work. `chezmoi` itself is installed per-arch. If `$HOME` is
quota-capped, set `PIXI_HOME` to scratch before running.

There is no `chsh` on a login node, so bootstrap appends a hard-guarded, arch-aware
`exec fish` to `~/.bashrc` (interactive shells only — `scp`, non-interactive ssh,
and `#!/bin/bash -l` SLURM batch scripts are unaffected).

## 📦 What's Included

### Core Tools
- **Editor**: [Helix](https://helix-editor.com/) - Modern modal text editor
- **Shell**: [Fish](https://fishshell.com/) - User-friendly command line shell
- **Prompt**: [Starship](https://starship.rs/) - Fast, customizable prompt with 🦄
- **Multiplexer**: [Zellij](https://zellij.dev/) - Modern terminal workspace

### Modern CLI Replacements
- **`bat`** → enhanced `cat` with syntax highlighting
- **`eza`** → enhanced `ls` with colors and icons
- **`ripgrep`** → blazingly fast `grep` replacement
- **`fd`** → simple and fast `find` replacement  
- **`zoxide`** → smart `cd` with frecency
- **`dust`** → intuitive `du` replacement

### Language Runtimes
- **Rust** stable toolchain (via [rustup](https://rustup.rs/), installed automatically)
- **Python** via [uv](https://github.com/astral-sh/uv) (installed automatically)

### Backup tools

`chezmoi apply` installs `rclone`, `age`, and `zstd` through Homebrew, apt, or pixi. These tools transfer, encrypt, and compress backups. Backup credentials, service data paths, schedules, and retention policies are configured separately. No Google account credentials belong in this repository.

### AI Development Tools
- **claude** - Claude Code CLI (installed via [native installer](https://claude.ai/install.sh), auto-updates)
- **codex** - OpenAI Codex CLI (`npm i -g @openai/codex`; run `codex login` to authenticate)

> **Codex Claude Code plugin** ([openai/codex-plugin-cc](https://github.com/openai/codex-plugin-cc))
> is a separate, manual step — it installs interactively *inside* Claude Code and
> cannot be scripted by bootstrap:
> ```
> /plugin marketplace add openai/codex-plugin-cc
> /plugin install codex@openai-codex
> /codex:setup
> ```
>
> Leave the stop-time review gate off. `/codex:setup --enable-review-gate` makes Codex review each Claude turn when the turn stops. Instead, the agent suggests a review and its level when a unit of work is complete. See the "Review cadence" section of `dot_agents/AGENTS.md`.
>
> `codex-review-gate` lists and sets the gate for each workspace:
> ```
> codex-review-gate list [--enabled | --disabled] [--root DIR] [--json]
> codex-review-gate disable PATH...
> codex-review-gate enable PATH...
> ```
> `list` scans the home folder two levels deep and the worktrees of each repository that it finds. A gate whose workspace was not found shows as `(no workspace found: NAME)`. Such a gate cannot run until a repository is created at the same path again. `enable` and `disable` run the plugin's own `setup` command and need `node`.

## 🤖 Shared agent scaffold

One set of instructions and skills, used by both Claude Code and Codex.

`dot_agents/` in the repository returned by `chezmoi source-path` is the editable source. `~/.agents/` contains deployed copies:

```
~/.agents/AGENTS.md              global instructions
~/.agents/ENVIRONMENT.md         capabilities, discovery, and source-update procedures
~/.agents/skills/<name>/SKILL.md a skill
              .../agents/openai.yaml   Codex UI metadata (display name, default prompt)
              .../scripts/, references/, LICENSE
```

The scaffold uses `run_onchange_after_link-agent-scaffold.sh.tmpl` to link instructions and rostered skills into both agent homes. Codex also discovers user skills directly under `~/.agents/skills`:

| Link | Target |
|---|---|
| `~/.claude/CLAUDE.md` | `~/.agents/AGENTS.md` |
| `~/.codex/AGENTS.md` | `~/.agents/AGENTS.md` |
| `~/.claude/skills/<name>` | `~/.agents/skills/<name>` |
| `~/.codex/skills/<name>` | `~/.agents/skills/<name>` |

The roster lives in `.chezmoidata/agent_skills.yaml`. To add a skill, create `dot_agents/skills/<name>/SKILL.md` and add its name to the roster. Follow the environment guide for validation, scoped deployment, linker execution, commit, and push. Content-only edits still need an apply but do not need relinking.

Removing a roster entry prunes scaffold-owned agent-home links. Inspect the deployed `~/.agents/skills/<name>` directory separately; roster removal does not delete it. Other entries, including bundled system skills and BB-installed skills, have separate owners.

Installed skills:

| Skill | What it does |
|---|---|
| `thermo-nuclear-code-quality-review` | Strict adversarial review: correctness, hot-path performance, abstraction quality, file-size and spaghetti growth |
| `wills-mega-review` | Optional. Iterates the above in fresh read-only subagents until clean, then tags the PR `human-review` |
| `full-code-review` | One consolidated general + thermo-nuclear pass |
| `general-review` | Plans, designs, and documents rather than diffs |
| `rust-code-review` | Rust systems and concurrency rules |
| `phone-a-friend` | Independent second opinion from Claude / Cursor / Devin over ACP |
| `gh-comment-ledger` | Triages PR feedback into an actionable table before editing |
| `gh-pr-description` | Managed-block PR bodies that never clobber existing content |
| `pr-babysitter` | Drives CI green and adjudicates AI reviewers adversarially |
| `docs-sweep` | Repo-wide documentation drift ledger across `agent-docs/`, comments, and docs |
| `simple-english` | ASD-STE100 Simplified Technical English plus authorship rules for docs, PR bodies, commit messages |

Sources: [`ai-dynamo/rhino`](https://github.com/ai-dynamo/rhino),
[`ishandhanani/dotfiles`](https://github.com/ishandhanani/dotfiles), plus practice distilled from
`ryanolson/kvbm`, `ai-dynamo/velo`, and `ryanolson/roundhouse`. Each `SKILL.md` names its upstream.

Both agents are instructed to read [the environment guide](dot_agents/ENVIRONMENT.md) once per session. It maps tasks to tools, separates source ownership, and requires validation, scoped apply, commit, and push. Shared changes must work in both agents. See [the evaluation protocol](agent-docs/environment-evaluation.md) for behavioral checks.

Verify a machine after the intended scoped apply:

```bash
ls -la ~/.claude/skills ~/.codex/skills   # symlinks into ~/.agents/skills
chezmoi status ~/.agents/AGENTS.md ~/.agents/ENVIRONMENT.md ~/.agents/skills
# Start fresh sessions in both agents and follow the evaluation protocol.
```

### Development Environment
- **Version Control**: Git with team-standard configuration
- **File Management**: yazi (terminal file manager), broot (tree view)
- **Task Runner**: just (modern make alternative)

## 🏗️ Architecture

### Layered Configuration System

1. **Team Core** (this repo) - Shared tools, configs, and standards
2. **User Overrides** (local config) - Personal preferences and secrets  
3. **Machine-Specific** (templates) - OS/hardware specific settings

### Tool Stack
- **chezmoi** - Dotfiles and configuration management
- **rustup** - Rust toolchain management
- **uv** - Python package and project management
- **Homebrew** (macOS) / **apt** (Linux) - System package management
- **Fish + Starship** - Modern shell experience

## 📋 Requirements

- **macOS**: 10.15+ with Xcode command line tools
- **Linux (sudo)**: Ubuntu 20.04+ or equivalent with apt
- **Linux (no sudo / SLURM login node)**: just `git` + `curl` on PATH; everything else lands in `$HOME` (`--no-sudo`)

## 🔧 Customization

### Personal Configuration

Create `~/.config/chezmoi/chezmoi.yaml`:

```yaml
data:
  # Personal information
  name: "Your Name"
  email: "your.email@company.com"
  github_user: "yourusername"
  
  # Custom aliases (in addition to team aliases)
  custom_aliases:
    k: "kubectl"
    d: "docker"
    
  # Work directories for quick navigation
  work_dirs:
    - name: "work"
      path: "~/work"
    - name: "projects"  
      path: "~/projects"
```

### Secrets Management (1Password)

API keys and tokens are managed via 1Password CLI (`op`) with on-demand injection. Secrets stay in 1Password and are only resolved into the subprocess that needs them.

**Setup:**

1. Install and sign in to 1Password CLI:
   ```bash
   op account add    # first time
   op signin         # subsequent times
   ```

2. Ensure these items exist in your 1Password vault (`Development` by default; the
   `credentials_profile` in your local chezmoi config selects it):
   - `Anthropic API` (credential field = API key)
   - `HuggingFace` (credential field = HF token)

   There is deliberately no GitHub item — see [GitHub auth policy](#github-auth-policy).

3. Enable in your local chezmoi config (`~/.config/chezmoi/chezmoi.yaml`):
   ```yaml
   data:
     name: "Your Name"
     email: "your.email@company.com"
     onepassword:
       enabled: true
       ssh_agent: true
       signing_public_key: "ssh-ed25519 AAAA..."
   ```
   Get the signing public key from 1Password once:
   ```bash
   op item get "Git Signing Key" --vault Development --fields "public key"
   ```

4. Apply and restart your shell:
   ```bash
   chezmoi apply
   exec fish
   ```

**How it works:**
- Chezmoi writes `~/.config/dynamo/dev.env.op` with 1Password secret references only
- `openv <cmd...>` runs a command under `op run --env-file ~/.config/dynamo/dev.env.op`
- On a machine where `onepassword.enabled` is false, chezmoi writes `~/.config/dynamo/dev.env.fleet` instead, with one `VAR=name` line for each entry in `fleet_secrets` (`.chezmoidata/team.yaml`). `openv` then runs `fleet secret env VAR=name... -- <cmd...>`, and the fleet broker serves each name from the `Fleet` vault in 1Password. The machine must be enrolled in the fleet.
- `openv-file <env-file> <cmd...>` does the same for an alternate env-reference file
- No service-account token is stored in `chezmoi` config or auto-exported into every shell

**SSH Agent (1Password):**
- When `onepassword.ssh_agent` is `true`, SSH config points to the 1Password SSH agent
- Git is configured for SSH commit signing using your stored public key plus the 1Password SSH agent on macOS
- GitHub HTTPS URLs are rewritten to SSH automatically, so no HTTPS credential is ever requested
- Use `gh auth login -p ssh` to authenticate the GitHub CLI

### Local SSH keys on trusted nodes

Run this command on a node after `chezmoi apply`. It copies an existing key from 1Password for SSH authentication and Git signing:

```bash
provision-keys --ssh-key-ref 'op://Private/My SSH Key/private key'
```

The node needs `op`, an authenticated 1Password CLI session, Git 2.34 or later, and OpenSSH with SSH signing support. Bootstrap installs `op` on macOS, and on Linux where `onepassword.enabled` is set. On other Linux machines, install `op` before provisioning. Configure your Git name and email through chezmoi first.

To use a separate signing key, pass its reference with `--signing-key-ref`:

```bash
provision-keys --ssh-key-ref 'op://Private/My SSH Key/private key' --signing-key-ref 'op://Private/Git Signing Key/private key'
```

For bootstrap with an authenticated `op` session, pass the same references as flags:

```bash
bash bootstrap.sh --ssh-key-ref 'op://Private/My SSH Key/private key' --signing-key-ref 'op://Private/Git Signing Key/private key'
```

Repeat `--ssh-key-ref` to install multiple authentication keys. Omit `--signing-key-ref` to use the first authentication key for signing. A signing reference that is the same as an authentication reference uses that key file, and the command makes no second copy. The references contain vault and item names, not private key contents.

The command stores unencrypted private keys and derived public keys in `~/.ssh/provisioned`. Each key file has the name of its 1Password item in lowercase, with a hyphen in place of each run of other characters. For example, the item `Git Signing Key` gives the files `git-signing-key` and `git-signing-key.pub`. The comment of the public key is the item name as written. The command stops when two references give the same file name, or when an item gives the name `config` or `gitconfig`. The directory has mode `700`. Its files have mode `600`. It checks a signature before installation and refuses to replace different existing files. Identical repeat runs succeed. Private key contents never enter chezmoi templates or diffs.

Chezmoi includes the local SSH configuration before agent settings and the local Git configuration after agent settings. These includes survive `chezmoi apply`. Local provisioning disables agent use for SSH and supplies the authentication keys for all hosts. Explicit host-specific identity files remain additive. Git signing uses the local private key without a forwarded agent. GitHub HTTPS URLs use SSH with this key.

To return to agent configuration, move `~/.ssh/provisioned` out of that path. To replace keys, move the directory aside and run `provision-keys` again. Existing public-key registrations remain valid when you copy the same keys.

### Fleet certificates

`fleet` gives each machine its own short-lived identity from a [step-ca](https://smallstep.com/docs/step-ca) certificate authority. A machine with an identity can SSH to other enrolled machines without a password and without a 1Password approval. The design is in `agent-docs/fleet-identity.md`. The CA address, the root fingerprint, and the 1Password item are deployment values. They are not in this repository.

Each machine makes its own private keys, and the keys do not leave the machine. A person approves the first certificate with a one-time token from a laptop that has 1Password.

On macOS, `chezmoi apply` installs `step` with Homebrew. A laptop that shares your 1Password needs one command:

```bash
fleet login
```

`fleet login` reads the deployment values from the 1Password item `op://Personal/Fleet config` (fields `ca_url`, `fingerprint`, `broker_url`, `enroll_item`, `admin_item`). Set `FLEET_CONFIG_ITEM` to use another item. Then it enrolls the laptop as an admin with the hostname as its name, and it schedules renewal. Pass a name to override the hostname. Without 1Password, record the values with `fleet init --ca-url ... --fingerprint ... --enroll-item ... --admin-item ... --broker-url ...`.

To enroll a new machine, print a join command on the laptop:

```bash
fleet invite NAME [--san NAME]... [--dotfiles] [--ephemeral] [--no-trust]
```

Run the printed command on the new machine within 15 minutes. It works in bash, zsh, and fish. It downloads `join.sh` and `fleet` from GitHub at the commit that the laptop has checked out, so push that commit first. The token in the command works only once.

- Without options, `join.sh` installs `fleet` if it is missing, enrolls, and runs `fleet timer install`.
- `--dotfiles` first runs `bootstrap.sh`, which installs these dotfiles from `main`.
- `--ephemeral` runs `fleet ephemeral` in the foreground, for example in a Zellij pane. The keys are in memory-backed storage (`$XDG_RUNTIME_DIR` or `/dev/shm`), and renewal runs every hour. On exit or Ctrl-C, the command revokes its certificate and removes its keys, `~/.ssh/fleet`, and its `authorized_keys` block. It refuses to run on a machine with a persistent enrollment.

The token is on the command line of the new machine for the duration of `join.sh`. To keep it off the command line, use `fleet enroll-remote HOST NAME` for a machine that you can reach with SSH. That command sends the token on stdin. If the laptop cannot reach the machine, run `fleet token NAME` on the laptop. Then run `fleet enroll NAME --ca-url https://CA --fingerprint ROOT_SHA256` on the machine and paste the token. After `enroll-remote` or a manual `enroll`, run `fleet timer install` on the machine.

`fleet login` mints its own token through 1Password with the admin provisioner, and it does not add login trust. A laptop that sleeps longer than 24 hours has an expired certificate. Run `fleet login` again.

`fleet list` shows each active machine: its expiry, its last issue or renewal, its public address as the CA saw it, its Tailscale address, and its serial. `fleet list -a` adds machines whose latest certificate is expired or revoked. The broker drops such a machine from its inventory 7 days after the expiry or revocation. Add `--json` for scripts, for example `fleet list --json | jq -r '.machines[].name'`. The Tailscale address is empty for a machine that is offline on the tailnet, or whose name, or a name in its certificate, does not match a tailnet device. The command asks the fleet broker, an mTLS service on the CA host. The broker accepts only certificates from the fleet CA that are not on the CA's revocation list. It lists machines only for a certificate from the admin provisioner, that is, a laptop after `fleet login`.

The timer is a launchd agent on macOS, a systemd user timer on Linux, or a crontab entry when systemd is not available. In a container without a scheduler, run `fleet renew --loop` under the container supervisor. When `pylon` has the `renew` command, `fleet renew` runs `pylon renew` for both certificates and uses `step` only to write the SSH configuration. On Linux, `fleet` installs a pinned `step` release into `~/.local/bin` if `step` is missing. On a no-sudo host, where `~/.local/<arch>` exists, it uses `~/.local/<arch>/bin`. The shell profiles turn on the no-sudo layout when `~/.local/<arch>` exists, so `fleet` does not make that folder. It checks the download against a SHA-256 value in the script.

| Item | Lifetime | Location |
|---|---|---|
| Client certificate and key | 24 hours, renewed over mTLS | `~/.local/state/fleet/node.crt`, `node.key` |
| SSH key and user certificate | 16 hours | `~/.local/state/fleet/id_fleet`, `id_fleet-cert.pub` |
| SSH client settings and host CA | Rewritten at enrollment | `~/.ssh/fleet/config`, `~/.ssh/fleet/known_hosts` |
| Login trust | Until `fleet untrust` | A `# BEGIN fleet` block in `~/.ssh/authorized_keys` |

Chezmoi includes `~/.ssh/fleet/config` before the local provisioning file. The fleet file sets an agent only for `github.com`, so local provisioning still applies to other hosts. By default, enrollment adds a `cert-authority` line to `~/.ssh/authorized_keys` for the principal `ryan`. Use `--principal NAME` to accept a different principal. Use `--no-trust` on a machine that must not accept fleet logins, for example the CA host. Other lines in `authorized_keys` do not change.

No `sshd` presents a fleet host certificate yet, so the fleet file trusts fleet hosts on first use. For the broker's aliases, tailnet addresses (`100.64.0.0/10`), and `*.ts.net` names, it sets `StrictHostKeyChecking accept-new`: ssh records a new host key without asking and still refuses a changed one. Other hosts, such as GitHub, keep the normal prompt. When `tailscale status` reports a MagicDNS suffix, the file also makes ssh expand a bare name such as `mac-mini-m6` to its `ts.net` name, so bare tailnet names get the same rule. Names that do not resolve in the tailnet fall back to normal lookup.

`fleet status` shows the name, the serial number, and the expiry times. Serials are base62 (`0-9A-Za-z`). `fleet list --json` also gives `serial_decimal`, which is the form that `step` uses. To cut off a machine, run `fleet revoke SERIAL` on a laptop with the base62 serial.

CAUTION: Revocation does not end access immediately. The machine cannot renew, but it keeps its SSH certificate, and it can get new SSH certificates until its client certificate expires. Access ends within about 24 hours.

`fleet agent` gives an enrolled machine an ssh-agent whose private keys stay in the CA's KMS. It connects a local socket to the broker's agent service over mTLS with the machine certificate. It runs `pylon` when `pylon` is on `PATH` or in `~/.local/bin` (`~/.local/<arch>/bin` on a no-sudo host), and `socat` otherwise. `fleet install-pylon` downloads the pinned `pylon` build for the machine from the broker (`/v1/artifacts/pylon/VERSION/TARGET`), checks its SHA-256 against the value in the script, installs it, and restarts a running agent. It does nothing when the installed binary already matches. To release a new `pylon`, build it on each target, run `artifacts/publish` in fleet-config, and update `PYLON_VERSION` and the `PYLON_SHA256_*` values in `fleet`. `pylon` keeps a warm connection and caches the key list. `socat` opens a new connection for each agent client. `fleet agent install` runs it as a launchd agent or a systemd user service. The socket is `$XDG_RUNTIME_DIR/fleet-agent.sock` on Linux and `~/.ssh/fleet/agent.sock` otherwise. Use it with `SSH_AUTH_SOCK`:

```bash
fleet agent install
SSH_AUTH_SOCK=~/.ssh/fleet/agent.sock ssh-add -L
```

The agent lists the keys that the broker policy allows for this machine, and it only signs. `fleet agent install` also connects Git and GitHub to it, without a 1Password approval:

- `~/.ssh/fleet/gitconfig` sets the signing key to the agent's `fleet agent-signing` key and `gpg.ssh.program` to `fleet-ssh-keygen`. That wrapper signs through the fleet agent, whatever `SSH_AUTH_SOCK` the shell has. `~/.gitconfig` includes the file last, so it overrides the 1Password signing settings. Git skips the include when the file is missing.
- `~/.ssh/fleet/allowed_signers` holds the lines of `~/.ssh/allowed_signers` and the agent key, so `git log --show-signature` verifies both.
- `~/.ssh/fleet/config` gets a `Host github.com` block with `IdentityAgent` set to the fleet agent. Other hosts keep their agent.

`fleet agent remove` deletes these files, and Git and SSH return to the previous settings. `git-signing-status` reports whether the fleet agent holds the signing key. Each signature is a request to the CA host. From a laptop in the measured test, a signature took about 0.15 seconds through `pylon` and about 0.5 seconds through `socat`. If the CA host is down, signing with these keys stops; fleet certificates still work for SSH between fleet machines.

When the 1Password agent is active, OpenSSH offers the agent keys before the fleet certificate. A server that accepts one of those keys can still cause a 1Password approval.

Verification:

```bash
timeout 300 python3 test/test-fleet.py
timeout 300 python3 test/test-join.py
```

### Remove a machine from the fleet

`fleet uninstall` (or `fleet remove`) revokes the machine's certificate, stops the renew timer, the agent, and every tunnel, removes the CA line from `~/.ssh/authorized_keys`, and deletes `~/.ssh/fleet`, the fleet state, and the fleet settings. It asks first; pass `--yes` when there is no terminal. `--keep-cert` skips the revocation. It does not remove `pylon` or a bb host daemon.

### Fleet SSH names

`fleet renew` also writes short SSH names from the broker into `~/.ssh/fleet/config`, for example `ssh bb`. The broker holds the list, so host names stay out of this repository. If the broker does not answer, `fleet` keeps the last list.

### Fleet secrets

The broker serves named secrets to fleet machines. A policy on the CA host decides which machine names can read each secret. Values are not printed by `fleet secret env` or `fleet claude`.

```bash
fleet secret get NAME                          # print the value
fleet secret env VAR=NAME... -- CMD...         # run CMD with the values in its environment
fleet claude [ARGS...]                         # run claude with CLAUDE_CODE_OAUTH_TOKEN from the secret claude-oauth-token
```

The broker URL defaults to port 8443 on the CA host. `fleet init --broker-url` overrides it.

### bb server

`bb-server-service` runs a bb server as a systemd user service on an always-on Linux machine. It needs Node.js 22.19, 24, or 26, which `chezmoi apply` installs through pixi.

```bash
bb-server-service install            # bb-app at the exact version that "latest" names now
bb-server-service install --serve    # also publish it on the tailnet with Tailscale Serve
bb-server-service status
bb-server-service remove             # keeps ~/.bb and the install
```

The script installs `bb-app` under `~/.local/share/bb-server` and writes `bb-server.service`. The server listens on `127.0.0.1:38886`. `--serve` publishes it at `https://<machine>.<tailnet>.ts.net` and sets `BB_APP_URL` to that address. npm 11 refuses `--allow-scripts` for a `--prefix` install, so the script writes the setting to `.npmrc` in the install directory.

### GitHub auth policy

**`gh auth login` only. Never a personal access token.** No `GITHUB_TOKEN`, `GH_TOKEN`, or
`GITHUB_PAT` in a shell profile, an env file, `chezmoi` data, or 1Password. Nothing in this
repository reads one, and the tooling actively pushes back if one appears:

| Layer | What enforces it |
|---|---|
| Git transport | `dot_gitconfig.tmpl` rewrites `https://github.com/` → `git@github.com:` so HTTPS never asks for a credential. On machines without the 1Password agent it declares `credential.helper = !gh auth git-credential` instead. Both paths are token-free. |
| Shell | `~/.config/fish/conf.d/github-auth-guard.fish` erases GitHub token variables from interactive shells and says why. |
| Secrets | `.chezmoidata/*.yaml` carry no GitHub entry, and `setup-secrets` **fails** if one turns up in the environment or in `dev.env.op`. |
| GitHub API | `install-packages` calls `gh api` when authenticated and anonymous `curl` otherwise. It never sends an `Authorization` header of its own. |

Authenticate once per machine:

```bash
gh auth login -p ssh   # with the 1Password SSH agent (onepassword.enabled)
gh auth login          # device flow, everywhere else
gh auth status         # verify
```

If a private clone fails, the cause is almost always one of two things: `gh` is not logged in, or
something ran `git config --global url.https://github.com/.insteadOf ...` outside chezmoi and forced
HTTPS. Check with `git config --global --get-all url.https://github.com/.insteadOf` — it should
print nothing. `chezmoi apply` restores the correct rewrite.

**Remote Dev (Tailscale + zellij):**
- Use ordinary OpenSSH over the tailnet for sessions that need commit signing or secrets
- `dev-remote <host> [session]` primes a remote zellij session with per-session env vars and then attaches
- `dev-remote refresh <host> [session]` recreates the session after a secret rotation
- Without local key provisioning, commit signing on Linux remotes uses the forwarded SSH agent. Reconnect with `ssh -A` or `dev-remote` if the agent went stale.
- Every login path pins the same zellij socket directory, so `zellij a <name>` reaches one server no matter how you reached the host

**Manual auth steps (once per machine):**
- `claude login` — Claude Code uses OAuth, no static key needed
- `gh auth login -p ssh` — GitHub CLI piggybacks on the 1Password SSH agent

**Verification helper:**
```bash
setup-secrets
setup-secrets remote <host>
```

**Examples:**
```bash
openv sh -c 'test -n "${HF_TOKEN:-}"'   # exit status only; do not print secret values
openv claude
dev-remote spark-d
dev-remote refresh spark-d main
git-signing-status
```

> **Note:** `tailscale ssh` is not the default path for signing/secrets sessions. Use standard OpenSSH over the tailnet so SSH-agent forwarding works cleanly with remote zellij sessions.

**Zellij socket directory:**

Zellij picks its socket directory from `XDG_RUNTIME_DIR`, which only logins that run `pam_systemd`
set. That made `zellij a dynamo` reach a different server with its own panes and scrollback
depending on whether you arrived over the tailnet or over the VPN. `~/.local/bin/zellij-socket-dir`
now defines the directory once, and every entry point exports it: the fish `conf.d` drop-in,
`~/.profile`, `zellij-wrapper`, and `dynamo-remote-session` (which runs over non-interactive SSH and
so sources no shell rc). It resolves to `/run/user/$UID/zellij` where that exists, and falls back to
`/tmp/zellij-$UID` on macOS and on no-sudo login nodes that have no logind runtime directory.

`/run/user/$UID` needs lingering to survive your last logout, or logind removes it and every running
server with it:

```bash
loginctl enable-linger $USER   # needs sudo; check with: loginctl show-user $USER -p Linger
```

Verify all entry points agree:

```bash
bash test/zellij-socket-dir.sh
```

### Adding Custom Packages

Edit your local chezmoi config to install additional tools:

```yaml
data:
  additional_packages:
    - docker
    - kubectl
    - terraform
```

## ⌨️ Key Bindings

### Helix Editor
| Key | Action |
|-----|--------|
| `Space f` | File picker |
| `Space b` | Buffer picker |
| `Space s` | Symbol picker |
| `Space /` | Global search |
| `Ctrl-s` | Save file |
| `Ctrl-z` | Undo |
| `Ctrl-y` | Redo |

### Zellij Terminal
| Key | Action |
|-----|--------|
| `Ctrl-p` | Enter pane mode |
| `Ctrl-t` | Enter tab mode |
| `Ctrl-n` | Enter resize mode |
| `Ctrl-s` | Enter scroll mode |
| `Alt-n` | New pane |
| `Alt-[` / `Alt-]` | Switch tabs |

### Fish Shell Aliases
| Alias | Command |
|-------|---------|
| `h` | `hx` (helix editor) |
| `k` | `kubectl` (kubernetes) |
| `d` | `docker` |

> Additional optional aliases (tool replacements, git shortcuts) are available in `config.fish` — uncomment to enable.

## 📚 Usage

### Daily Commands
```bash
# Update dotfiles from repository
chezmoi update

# Edit configuration  
chezmoi edit ~/.gitconfig

# Apply changes
chezmoi apply

# Check what would change
chezmoi diff

# Add new dotfile
chezmoi add ~/.newconfig
```

### Runtime Management
```bash
# Update Rust toolchain
rustup update

# Install a specific Rust toolchain
rustup toolchain install nightly

# Manage Python projects
uv init myproject
uv add requests
uv run python main.py
```

### Shell Features
```bash
# Smart directory jumping
z myproject  # jumps to most frecent match

# Enhanced file operations  
bat README.md        # syntax highlighted cat
eza -la             # modern ls with colors
rg "TODO"           # fast text search
fd config.yaml      # fast file finding

# File management
yazi                # terminal file manager
broot               # interactive tree view
```

## 🔄 Updating

`chezmoi apply` installs these executable helpers in `~/.local/bin`. Run a helper to install or upgrade its CLI:

```bash
iou_claude
iou_codex
iou_antigravity
iou_cursor
iou_devin
iou_pi
iou_relay
iou_q
```

Claude, Cursor, and Devin use their native installers and update commands. `iou_devin` installs the Devin CLI when `devin` is missing, then runs `devin update`. Antigravity uses its native installer and replaces `agy` after installation succeeds. Codex and Pi use npm and preserve `NPM_CONFIG_PREFIX`, with `~/.npm-global` as the default. On sudo machines, `~/.npmrc` also sets `prefix` to `~/.npm-global`, so a service that runs npm without the shell environment, such as the bb host daemon, updates the same copy. chezmoi changes only the `prefix` line. Pi requires Node.js 22.19 or newer. Cursor refers to Cursor Agent CLI. Pi refers to the coding agent from [pi.dev](https://pi.dev).

`iou_relay` queries GitHub's latest stable release for `NVIDIA/NeMo-Relay` through `gh`. It downloads the current platform's binary and SHA-256 file from the same resolved tag. It verifies the checksum before replacing `~/.local/bin/nemo-relay`. Downloads and verification failures leave the existing binary intact. It requires authenticated `gh` access and `shasum`. Linux uses the musl release assets; macOS uses the Darwin assets. A missing platform asset produces an error.

`iou_q [checkout]` builds `q`, `q-proxy`, and `q-launch` with Cargo on the current platform. It installs release binaries into `~/.local/bin`. The checkout defaults to `~/repos/q`. Rust and the locked dependencies are required. The helper builds and stages q, runs `iou_relay`, then installs the q binaries. A Relay installation failure leaves the existing q binaries intact. The helper does not fetch source or change Git branches.

The environment auto-updates when team configuration changes. To manually update:

```bash
# Update dotfiles
chezmoi update

# Update packages (macOS)
brew update && brew upgrade

# Update packages (Linux)  
sudo apt update && sudo apt upgrade

# Update Rust toolchain
rustup update
```

### Headless update

`chezmoi-headless-update` updates the dotfiles when no terminal is available, for example from a bb server. It writes one JSON report to stdout and all command output to stderr.

1. The command fetches the source repository and merges `@{upstream}` as a fast-forward. If the fetch or the merge fails, the command applies nothing. The branch, the working tree, and the stash do not change.
2. The command runs `chezmoi apply --no-tty --keep-going`. It skips files that were changed outside chezmoi and does not overwrite them.
3. The command runs `chezmoi status --exclude=scripts` and reports each remaining difference. A failed script gives `apply_failed`.

| Exit status | `result` | Meaning |
|---|---|---|
| 0 | `updated` | The update is complete. Nothing needs a person. |
| 1 | `pull_failed` | `reasons` is `chezmoi_missing`, `source_missing`, `fetch_failed`, or `merge_failed`. Nothing was applied. |
| 2 | `needs_attention` | The update was applied. `reasons` contains one or more of `apply_failed`, `status_failed`, `pending`, and `config_template_changed`. |

The report also contains `before` and `after` (source commits), `pending` (status and path from `chezmoi status`), and `log_tail` (the last 40 output lines). The command does not regenerate the configuration with `chezmoi init`. If the report contains `config_template_changed`, run `chezmoi init` in a terminal. The `run_onchange` install script uses `sudo` on the `sudo` class, so a headless update on that class needs passwordless `sudo`.

## 🆚 Migration from Nix

If you're migrating from our previous Nix-based setup:

1. **Backup current config**: Your Nix config is preserved in the `nix` branch
2. **Run new bootstrap**: The new system installs alongside existing tools
3. **Compare configurations**: Use `chezmoi diff` to see what changes  
4. **Gradual transition**: You can run both systems in parallel

### Key Differences
- **Package Management**: Native (Homebrew/apt) instead of Nix
- **Configuration**: Templates instead of Nix expressions
- **Runtimes**: rustup + uv instead of Nix toolchains
- **Installation**: Lighter, no system-wide `/nix` directory

## 🤝 Team Contributions

### Adding New Tools
1. Add to `.chezmoidata/team.yaml`
2. Add the conda-forge package and its binary to `PIXI_TOOLS` in `run_onchange_install-packages.sh.tmpl`, with a version if the tool is critical, and the Homebrew name to its macOS list
3. Test on both macOS and Linux
4. Submit pull request

### Configuration Changes  
1. Edit templates in `dot_config/`
2. Test with `chezmoi apply --dry-run`
3. Commit changes - team gets updates automatically

## 🎨 Starship Prompt

The prompt shows:
- Current directory
- Git branch and status
- Language versions (only when relevant files present)
- Command execution time
- Exit status with 🦄 emoji

### Prompt Behavior
- **Rust version** only shows when `Cargo.toml` is present
- **Python version** shows in Python projects
- **Node version** shows in JavaScript/TypeScript projects
- **Lock icon 🔒** appears when you don't have write permissions

## 📖 Interactive Documentation

For detailed documentation, install and run the TUI guide:
```bash
# Install the guide
cargo install --git https://github.com/ryanolson/dynamo-tui

# Run the interactive documentation
dynamo-guide
```

The TUI provides:
- Complete keybinding references
- Tool usage guides
- Configuration examples
- Tips and tricks

## 📚 External Documentation

- **chezmoi**: https://chezmoi.io/
- **rustup**: https://rustup.rs/
- **uv**: https://docs.astral.sh/uv/
- **Fish Shell**: https://fishshell.com/
- **Starship**: https://starship.rs/
- **Helix Editor**: https://helix-editor.com/

## 🔗 Related

- **Previous Architecture**: See `nix` branch for Nix-based setup
- **Team Tools**: Core development tools and configurations
- **DevContainer**: Container-ready version coming soon

---

Built with ❤️ for productive development workflows

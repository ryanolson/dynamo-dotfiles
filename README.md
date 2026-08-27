# Dynamo Development Environment

A modern, cross-platform development environment using native package managers and dotfile management.

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

`chezmoi init` prompts for a **machine class** that selects the install path and features:

| Class             | Install path                | Sudo | 1Password / signing | Agent infra |
|-------------------|-----------------------------|------|---------------------|-------------|
| `headless-sudo`   | apt + `/usr/local/bin`      | yes  | no                  | yes         |
| `headless-nosudo` | `~/.local/<arch>/` via pixi | no   | no                  | no          |
| `primary`         | apt + `/usr/local/bin`      | yes  | **yes**             | yes         |

On `headless-nosudo` the install is **pixi-centric**: nearly the whole toolset
(`fish`, `node`, `git`, `gh`, `bat`, `ripgrep`, `fd`, `helix`, `zellij`, `lazygit`,
`starship`, `uv`, …) comes from [pixi](https://pixi.sh)/conda-forge — glibc-independent
and arch-aware. Only `claude` (native installer) and `codex` (npm) are separate.

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

## 🤖 Shared agent scaffold

One set of instructions and skills, used by both Claude Code and Codex.

`dot_agents/` → `~/.agents/` is the single source of truth:

```
~/.agents/AGENTS.md              global instructions
~/.agents/skills/<name>/SKILL.md a skill
              .../agents/openai.yaml   Codex UI metadata (display name, default prompt)
              .../scripts/, references/, LICENSE
```

Both agents discover skills at `<home>/skills/<name>/SKILL.md` and neither supports pointing at an
external directory, so `run_onchange_after_link-agent-scaffold.sh.tmpl` symlinks each skill into
both homes:

| Link | Target |
|---|---|
| `~/.claude/CLAUDE.md` | `~/.agents/AGENTS.md` |
| `~/.codex/AGENTS.md` | `~/.agents/AGENTS.md` |
| `~/.claude/skills/<name>` | `~/.agents/skills/<name>` |
| `~/.codex/skills/<name>` | `~/.agents/skills/<name>` |

The roster lives in `.chezmoidata/agent_skills.yaml`. **Adding a skill:** create
`dot_agents/skills/<name>/SKILL.md`, add the name to that list, `chezmoi apply`. **Removing one:**
delete the name; the linker drops both symlinks and leaves everything else, including Codex's own
`skills/.system`, untouched. Editing a skill's *content* needs no relink — the homes hold symlinks
into the live directory.

Installed skills:

| Skill | What it does |
|---|---|
| `thermo-nuclear-code-quality-review` | Strict adversarial review: correctness, hot-path performance, abstraction quality, file-size and spaghetti growth |
| `wills-mega-review` | Iterates the above in fresh read-only subagents until clean, then tags the PR `human-review` |
| `full-code-review` | One consolidated general + thermo-nuclear pass |
| `general-review` | Plans, designs, and documents rather than diffs |
| `rust-code-review` | Rust systems and concurrency rules |
| `phone-a-friend` | Independent second opinion from Claude / Cursor / Devin over ACP |
| `gh-comment-ledger` | Triages PR feedback into an actionable table before editing |
| `gh-pr-description` | Managed-block PR bodies that never clobber existing content |
| `pr-babysitter` | Drives CI green and adjudicates AI reviewers adversarially |
| `docs-sweep` | Repo-wide documentation drift ledger across `agent-docs/`, comments, and docs |
| `simple-english` | ASD-STE100 Simplified Technical English for docs, PR bodies, commit messages |

Sources: [`ai-dynamo/rhino`](https://github.com/ai-dynamo/rhino),
[`ishandhanani/dotfiles`](https://github.com/ishandhanani/dotfiles), plus practice distilled from
`ryanolson/kvbm`, `ai-dynamo/velo`, and `ryanolson/roundhouse`. Each `SKILL.md` names its upstream.

Verify a machine after `chezmoi apply`:

```bash
ls -la ~/.claude/skills ~/.codex/skills   # symlinks into ~/.agents/skills
claude plugin validate ~/.agents/skills   # catches malformed SKILL.md frontmatter
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
- `openv-file <env-file> <cmd...>` does the same for an alternate env-reference file
- No service-account token is stored in `chezmoi` config or auto-exported into every shell

**SSH Agent (1Password):**
- When `onepassword.ssh_agent` is `true`, SSH config points to the 1Password SSH agent
- Git is configured for SSH commit signing using your stored public key plus the 1Password SSH agent on macOS
- GitHub HTTPS URLs are rewritten to SSH automatically, so no HTTPS credential is ever requested
- Use `gh auth login -p ssh` to authenticate the GitHub CLI

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
gh auth login -p ssh   # with the 1Password SSH agent (primary machines)
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
- Commit signing on Linux remotes uses the forwarded SSH agent, so reconnect with `ssh -A` or `dev-remote` before signing if the agent went stale

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
openv env | rg 'ANTHROPIC_API_KEY|HF_TOKEN|NGC_API_KEY'
openv claude
dev-remote spark-d
dev-remote refresh spark-d main
git-signing-status
```

> **Note:** `tailscale ssh` is not the default path for signing/secrets sessions. Use standard OpenSSH over the tailnet so SSH-agent forwarding works cleanly with remote zellij sessions.

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
2. Update package lists in `.chezmoidata/packages_*.yaml`  
3. Update installation scripts in `run_onchange_*`
4. Test on both macOS and Linux
5. Submit pull request

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

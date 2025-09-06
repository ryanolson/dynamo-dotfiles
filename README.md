# Dynamo Development Environment

Modern, cross-platform development environment using **chezmoi** for dotfiles management and **mise** for runtime version management.

## 🚀 Quick Start

**One-command installation:**
```bash
curl -fsSL https://raw.githubusercontent.com/ryanolson/dynamo-dotfiles/main/bootstrap.sh | bash
```

## 🛠️ What You Get

### Core Tools
- **📝 Editor**: Helix (modern terminal editor)
- **🐠 Shell**: Fish (user-friendly shell with modern features)  
- **⭐ Prompt**: Starship (cross-shell prompt with git integration)
- **🖥️ Multiplexer**: Zellij (terminal multiplexer with modern UI)

### Modern CLI Replacements
- **`bat`** → enhanced `cat` with syntax highlighting
- **`eza`** → enhanced `ls` with colors and icons
- **`ripgrep`** → blazingly fast `grep` replacement
- **`fd`** → simple and fast `find` replacement  
- **`zoxide`** → smart `cd` with frecency
- **`dust`** → intuitive `du` replacement

### Development Environment
- **Language Runtimes**: Node.js, Python, Rust, Go, Zig (via mise)
- **Package Managers**: npm, cargo, pip/uv (user-managed)
- **Version Control**: Git with team-standard configuration
- **File Management**: yazi (terminal file manager), broot (tree view)

## 🏗️ Architecture

### Layered Configuration System

1. **Team Core** (this repo) - Shared tools, configs, and standards
2. **User Overrides** (local config) - Personal preferences and secrets  
3. **Machine-Specific** (templates) - OS/hardware specific settings

### Tool Stack
- **chezmoi** - Dotfiles and configuration management
- **mise** - Language runtime version management
- **Homebrew** (macOS) / **apt** (Linux) - System package management
- **Fish + Starship** - Modern shell experience

## 📋 Requirements

- **macOS**: 10.15+ with Xcode command line tools
- **Linux**: Ubuntu 20.04+ or equivalent with apt

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

### Secrets Management

chezmoi supports multiple secret storage options:

```yaml
# In ~/.config/chezmoi/chezmoi.yaml
data:
  # For 1Password integration
  onepassword_account: "your-account"
  
  # For Bitwarden integration  
  bitwarden_email: "your-email"
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
# List available versions
mise ls-remote node

# Install specific version
mise install node@20.10.0

# Set global default
mise use --global node@20.10.0

# Project-specific version
mise use node@18.19.0  # creates .tool-versions
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

# Update runtimes
mise install
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
- **Runtimes**: mise instead of Nix toolchains
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

## 📖 Documentation

- **chezmoi**: https://chezmoi.io/
- **mise**: https://mise.jdx.dev/  
- **Fish Shell**: https://fishshell.com/
- **Starship**: https://starship.rs/
- **Helix Editor**: https://helix-editor.com/

## 🔗 Related

- **Previous Architecture**: See `nix` branch for Nix-based setup
- **Team Tools**: Core development tools and configurations
- **DevContainer**: Container-ready version coming soon

---

Built with ❤️ for productive development workflows
#!/bin/bash
set -euo pipefail

# Dynamo Development Environment Bootstrap
# chezmoi + native package managers.
#   macOS                 -> Homebrew
#   Linux with sudo       -> apt + /usr/local/bin
#   Linux without sudo    -> everything into $HOME, pixi for fish+node (SLURM login node)
#
# Usage:
#   curl -fsSL <raw bootstrap.sh> | bash
#   curl -fsSL <raw bootstrap.sh> | bash -s -- --no-sudo
# No-sudo is auto-detected when the `sudo` command is absent.

RED='\033[0;31m'; GREEN='\033[0;32m'; BLUE='\033[0;34m'; YELLOW='\033[1;33m'; NC='\033[0m'
log() { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

REPO_URL="https://github.com/ryanolson/dynamo-dotfiles.git"
DOTFILES_DIR="$HOME/.local/share/chezmoi"
NO_SUDO=auto   # auto | 0 | 1  (forced by --no-sudo / --sudo)

detect_os() {
    case "$OSTYPE" in
        darwin*)  OS="macOS" ;;
        linux*)   OS="Linux" ;;
        *)        error "Unsupported operating system: $OSTYPE" ;;
    esac
    # Resolve sudo mode. A login node usually HAS the sudo binary but the user
    # can't use it — so test real usability with `sudo -n true`, not `command -v`.
    if [[ "$NO_SUDO" == "auto" ]]; then
        if [[ "$OS" == "macOS" ]]; then
            NO_SUDO=0
        elif ! command -v sudo >/dev/null 2>&1; then
            warn "sudo not found — using no-sudo install"
            NO_SUDO=1
        elif ! sudo -n true >/dev/null 2>&1; then
            warn "sudo present but not usable without a password — using no-sudo install"
            warn "(re-run with --sudo to force the apt path)"
            NO_SUDO=1
        else
            NO_SUDO=0
        fi
    fi
    log "🖥️  Detected OS: $OS$([[ $NO_SUDO -eq 1 ]] && echo ' (no-sudo)')"
}

install_dependencies() {
    log "📋 Checking system dependencies..."
    if [[ "$OS" == "macOS" ]]; then
        if ! xcode-select -p >/dev/null 2>&1; then
            log "Installing Xcode command line tools..."
            xcode-select --install
            warn "Complete Xcode CLT installation and re-run this script"; exit 1
        fi
        if ! command -v brew &> /dev/null; then
            log "🍺 Installing Homebrew..."
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" || error "Failed to install Homebrew"
            [[ -f /opt/homebrew/bin/brew ]] && eval "$(/opt/homebrew/bin/brew shellenv)"
            [[ -f /usr/local/bin/brew ]] && eval "$(/usr/local/bin/brew shellenv)"
        fi
        brew install git curl || warn "Some dependencies may already be installed"
    elif [[ $NO_SUDO -eq 1 ]]; then
        # No package manager available; just verify the essentials exist.
        for c in git curl; do
            command -v "$c" >/dev/null 2>&1 || error "$c is required but not installed (and no sudo to install it)"
        done
    else
        if command -v apt-get &> /dev/null; then
            sudo apt-get update -qq
            sudo apt-get install -y -qq curl git build-essential || error "Failed to install dependencies"
        else
            error "Linux distribution not supported (requires apt, or run with --no-sudo)"
        fi
    fi
}

install_chezmoi() {
    if command -v chezmoi &> /dev/null; then
        log "📦 chezmoi already installed ($(chezmoi --version | head -1))"; return
    fi
    log "📦 Installing chezmoi into ~/.local/bin..."
    mkdir -p "$HOME/.local/bin"
    if [[ "$OS" == "macOS" ]]; then
        brew install chezmoi || error "Failed to install chezmoi"
    else
        # Install to $HOME — no sudo needed on any Linux box.
        sh -c "$(curl -fsLS get.chezmoi.io)" -- -b "$HOME/.local/bin" || error "Failed to install chezmoi"
    fi
    export PATH="$HOME/.local/bin:$PATH"
    success "✅ chezmoi installed"
}

init_dotfiles() {
    log "🏠 Initializing dotfiles with chezmoi..."
    if [[ -d "$DOTFILES_DIR" ]]; then
        warn "Dotfiles already initialized. Use 'chezmoi update' to update."
        chezmoi apply || warn "chezmoi apply reported issues"
        return
    fi
    # Pre-seed the machine_class prompt default so a no-sudo node doesn't land on
    # the apt path (which would sudo-prompt and abort apply under set -e).
    if [[ $NO_SUDO -eq 1 ]]; then
        export DOTFILES_MACHINE_CLASS="headless-nosudo"
    fi
    # init prompts for machine_class etc.; apply runs the package install script.
    chezmoi init "$REPO_URL" || error "Failed to initialize dotfiles"
    chezmoi apply || error "Failed to apply dotfiles"
    success "✅ Dotfiles initialized and applied"
}

install_claude_code() {
    if command -v claude &> /dev/null; then
        log "📦 Claude Code already installed"; return
    fi
    log "📦 Installing Claude Code CLI..."
    curl -fsSL https://claude.ai/install.sh | bash || warn "Failed to install Claude Code"
}

# Append a hard-guarded exec-fish snippet to ~/.bashrc (no chsh on login nodes).
# The guard MUST avoid breaking scp/rsync, non-interactive ssh, and SLURM batch
# scripts that source login files (#!/bin/bash -l).
setup_fish_no_chsh() {
    local rc="$HOME/.bashrc"
    local marker="# >>> chezmoi exec-fish >>>"
    if [[ -f "$rc" ]] && grep -qF "$marker" "$rc"; then
        log "✓ exec-fish guard already present in ~/.bashrc"; return
    fi
    log "Adding guarded exec-fish snippet to ~/.bashrc..."
    cat >> "$rc" <<'EOF'

# >>> chezmoi exec-fish >>>
# Launch fish for interactive shells only. Guarded so scp/rsync,
# non-interactive ssh, and SLURM batch scripts are never affected.
# Self-contained PATH: pixi puts fish in ~/.pixi/bin, which may not be on PATH yet.
case $- in
    *i*)
        [ -d "$HOME/.pixi/bin" ] && PATH="$HOME/.pixi/bin:$PATH"
        [ -d "$HOME/.local/bin" ] && PATH="$HOME/.local/bin:$PATH"
        if [ -z "$FISH_VERSION" ] && [ -t 0 ] && [ -t 1 ] && command -v fish >/dev/null 2>&1; then
            exec fish
        fi
        ;;
esac
# <<< chezmoi exec-fish <<<
EOF
    success "✓ exec-fish guard added (interactive shells will launch fish)"
}

setup_shell() {
    log "🐠 Setting up Fish shell..."
    if ! command -v fish &> /dev/null; then
        warn "Fish not on PATH yet — open a new shell (pixi puts it in ~/.pixi/bin) and re-check."
    fi
    if [[ $NO_SUDO -eq 1 ]]; then
        setup_fish_no_chsh
        return
    fi
    # sudo path: register fish in /etc/shells and hint chsh
    if command -v fish &>/dev/null; then
        local fp; fp="$(command -v fish)"
        if [[ "$OS" == "Linux" ]] && ! grep -qxF "$fp" /etc/shells 2>/dev/null; then
            echo "$fp" | sudo tee -a /etc/shells >/dev/null
        fi
        [[ "$SHELL" != "$fp" ]] && log "To make fish your login shell: chsh -s $fp"
    fi
}

main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --no-sudo) NO_SUDO=1; shift ;;
            --sudo)    NO_SUDO=0; shift ;;
            *) shift ;;
        esac
    done

    log "🚀 Starting development environment bootstrap (chezmoi)"
    detect_os
    install_dependencies
    install_chezmoi
    init_dotfiles
    install_claude_code
    setup_shell

    success "🎉 Bootstrap complete!"
    log ""
    log "💡 Next steps:"
    if [[ $NO_SUDO -eq 1 ]]; then
        log "   1. Open a new shell (or: exec fish). fish/node live in ~/.pixi/bin."
        log "   2. Authenticate: claude login   &&   codex login"
        log "   3. Codex Claude Code plugin (manual, inside Claude Code):"
        log "        /plugin marketplace add openai/codex-plugin-cc"
        log "        /plugin install codex@openai-codex  &&  /codex:setup"
        log "   4. If \$HOME has a quota, set PIXI_HOME to scratch before re-running installs."
    else
        log "   1. Restart your terminal or run: exec \$SHELL"
        log "   2. chsh -s \$(command -v fish)   # make fish your login shell"
        log "   3. Authenticate: claude login   &&   codex login"
    fi
    log ""
    command -v chezmoi >/dev/null && log "   chezmoi: $(chezmoi --version | head -1)" || true
    command -v fish >/dev/null && log "   fish: $(fish --version)" || true
    command -v node >/dev/null && log "   node: $(node --version)" || true
}

main "$@"

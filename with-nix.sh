#!/bin/bash
set -euo pipefail

REPO_URL="github:ryanolson/dynamo-dotfiles"
HM_BRANCH="release-25.05"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log() { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

log "🏠 Setting up development environment (Nix already installed)"

# Check if Nix is available
if ! command -v nix &> /dev/null; then
    error "Nix is not installed or not in PATH. Use bootstrap.sh for full installation."
fi

log "✅ Nix found: $(nix --version)"

# Enable flakes if not already enabled
log "⚙️  Configuring Nix flakes..."
mkdir -p ~/.config/nix
echo "experimental-features = nix-command flakes" > ~/.config/nix/nix.conf

# Setup Rust toolchain (independent of Nix)
log "🦀 Setting up Rust toolchain (independent of Nix)..."
if command -v rustup &> /dev/null; then
    log "🦀 Rustup already available, updating..."
    rustup update stable 2>/dev/null || warn "Rustup update failed, continuing..."
    rustup default stable 2>/dev/null || warn "Rustup default failed, continuing..."
    rustup component add rust-analyzer clippy rustfmt 2>/dev/null || warn "Rustup component add failed, continuing..."
else
    warn "Rustup not found in PATH. After setup completes, run:"
    warn "  rustup install stable"
    warn "  rustup default stable" 
    warn "  rustup component add rust-analyzer clippy rustfmt"
fi

# Install Home Manager and apply configuration
log "🏠 Installing and configuring Home Manager..."
nix run home-manager/$HM_BRANCH -- init --switch --flake $REPO_URL || {
    warn "Home Manager setup encountered issues. You may need to resolve conflicts."
    warn "Try running: nix-env -q"
    warn "And remove conflicting packages with: nix-env -e <package-name>"
    exit 1
}

success "✅ Development environment setup complete!"
log ""
log "🎉 What you now have:"
log "   - Editors: helix, lazygit"
log "   - Shell: fish, starship prompt, zellij multiplexer"
log "   - CLI: bat, eza, broot, zoxide, ripgrep, fd, yazi"
log "   - Languages: rustup, python3, zig, nodejs+npm"
log "   - System: docker, kubectl, etcdctl"
log "   - AI Tools: ccmanager, ruler (installed via npm on first fish startup)"
log ""
log "⚙️  All configuration files have been installed to ~/.config/"
log ""
log "💡 To update later, run:"
log "   home-manager switch --flake $REPO_URL"
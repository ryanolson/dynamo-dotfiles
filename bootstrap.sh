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

# Check if running on supported system
if [[ "$OSTYPE" != "linux-gnu"* && "$OSTYPE" != "darwin"* ]]; then
    error "Unsupported operating system: $OSTYPE"
fi

log "🚀 Bootstrapping personal development environment from $REPO_URL"
log "📦 This includes the ryanolson/dynamo-nix team base configuration"

# Install Nix if not present
if ! command -v nix &> /dev/null; then
    log "📦 Installing Nix package manager..."
    curl -L https://nixos.org/nix/install | sh || error "Failed to install Nix"
    
    # Source Nix profile
    if [[ -f ~/.nix-profile/etc/profile.d/nix.sh ]]; then
        . ~/.nix-profile/etc/profile.d/nix.sh
    else
        error "Nix installation completed but profile not found"
    fi
else
    log "📦 Nix already installed"
fi

# Enable flakes
log "⚙️  Configuring Nix flakes..."
mkdir -p ~/.config/nix
echo "experimental-features = nix-command flakes" > ~/.config/nix/nix.conf

# Bootstrap Home Manager with personal config
log "🏠 Installing personal configuration (includes team base)..."
if [[ "$REPO_URL" == *"github:"* ]]; then
    # Use GitHub repo
    nix run home-manager/$HM_BRANCH -- init --switch --flake $REPO_URL || error "Failed to setup Home Manager"
else
    # Use local path for testing
    nix run home-manager/$HM_BRANCH -- init --switch --flake $REPO_URL || error "Failed to setup Home Manager"
fi

# Post-install setup instructions
log "🦀 Installing Rust toolchain (independent of Nix)..."
if ! command -v rustup &> /dev/null; then
    warn "Rustup not found in PATH. After setup completes, run:"
    warn "  rustup install stable"
    warn "  rustup default stable" 
    warn "  rustup component add rust-analyzer clippy rustfmt"
fi

success "✅ Personal development environment setup complete!"
log "💡 Next steps:"
log "   1. Restart your shell or run: source ~/.nix-profile/etc/profile.d/nix.sh"
log "   2. If using Fish shell, run: fish"
log "   3. Install Rust toolchain: rustup install stable && rustup default stable"
log "   4. Add Rust components: rustup component add rust-analyzer clippy rustfmt"
log "   5. Check your Git config: git config --list"
log ""
log "🔧 Your environment includes:"
log "   - All Dynamo team tools and configurations"
log "   - Your personal Git configuration"
log "   - Your personal shell aliases and shortcuts"
log ""
log "⚙️  Configuration files are in ~/.config/"
log "🔄 To update: home-manager switch --flake $REPO_URL"
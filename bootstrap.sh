#!/bin/bash
set -euo pipefail

# Dynamo Development Environment Bootstrap
# Modern cross-platform setup using chezmoi + native package managers
# Supports macOS (Homebrew) and Linux (apt + direct installs)

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

# Configuration
REPO_URL="https://github.com/ryanolson/dynamo-dotfiles.git"
DOTFILES_DIR="$HOME/.local/share/chezmoi"

# Topology globals (set by detect_topology)
SETUP_MODE="single"
PRIMARY_USER=""
SECONDARY_USERS=()

# Detect operating system
detect_os() {
    case "$OSTYPE" in
        darwin*)  OS="macOS" ;;
        linux*)   OS="Linux" ;;
        *)        error "Unsupported operating system: $OSTYPE" ;;
    esac
    log "🖥️  Detected OS: $OS"
}

# Install core dependencies
install_dependencies() {
    log "📋 Installing system dependencies..."
    
    if [[ "$OS" == "macOS" ]]; then
        # Install Xcode command line tools if needed
        if ! xcode-select -p >/dev/null 2>&1; then
            log "Installing Xcode command line tools..."
            xcode-select --install
            warn "Please complete Xcode command line tools installation and re-run this script"
            exit 1
        fi
        
        # Install Homebrew if not present
        if ! command -v brew &> /dev/null; then
            log "🍺 Installing Homebrew..."
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" || error "Failed to install Homebrew"
            
            # Add Homebrew to PATH for current session
            if [[ -f "/opt/homebrew/bin/brew" ]]; then
                eval "$(/opt/homebrew/bin/brew shellenv)"
            elif [[ -f "/usr/local/bin/brew" ]]; then
                eval "$(/usr/local/bin/brew shellenv)"
            fi
        fi
        
        # Install core tools via Homebrew
        brew install git curl || warn "Some dependencies may already be installed"
        
    else
        # Linux - install via package manager
        if command -v apt-get &> /dev/null; then
            sudo apt-get update -qq
            sudo apt-get install -y -qq curl git build-essential || error "Failed to install dependencies"
        else
            error "Linux distribution not supported (requires apt)"
        fi
    fi
}

# Install chezmoi
install_chezmoi() {
    if command -v chezmoi &> /dev/null; then
        log "📦 chezmoi already installed ($(chezmoi --version | head -1))"
        return
    fi
    
    log "📦 Installing chezmoi..."
    if [[ "$OS" == "macOS" ]]; then
        brew install chezmoi || error "Failed to install chezmoi"
    else
        curl -sfL https://get.chezmoi.io | sh || error "Failed to install chezmoi"
        sudo mv ./bin/chezmoi /usr/local/bin/ || error "Failed to move chezmoi to PATH"
    fi
    
    success "✅ chezmoi installed successfully"
}

# Install mise for runtime version management
install_mise() {
    if command -v mise &> /dev/null; then
        log "📦 mise already installed ($(mise --version))"
        return
    fi
    
    log "📦 Installing mise (runtime version manager)..."
    if [[ "$OS" == "macOS" ]]; then
        brew install mise || error "Failed to install mise"
    else
        curl https://mise.run | sh || error "Failed to install mise"
        
        # Add mise to PATH for current session
        export PATH="$HOME/.local/bin:$PATH"
        
        # Add to shell profile
        if [[ -f "$HOME/.bashrc" ]]; then
            echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
        fi
    fi
    
    success "✅ mise installed successfully"
}

# Initialize dotfiles with chezmoi
init_dotfiles() {
    log "🏠 Initializing dotfiles with chezmoi..."
    
    # Check if already initialized
    if [[ -d "$DOTFILES_DIR" ]]; then
        warn "Dotfiles already initialized. Use 'chezmoi update' to update."
        return
    fi
    
    # Initialize (generates config from template, prompts for values)
    chezmoi init "$REPO_URL" || error "Failed to initialize dotfiles"

    # Apply dotfiles
    chezmoi apply || error "Failed to apply dotfiles"

    success "✅ Dotfiles initialized and applied"
}

# Set up runtime environments
setup_runtimes() {
    if ! command -v mise &> /dev/null; then
        warn "mise not available, skipping runtime setup"
        return
    fi
    
    log "🔧 Setting up language runtimes with mise..."
    
    # Install default versions if no configuration exists
    if [[ ! -f "$HOME/.tool-versions" && ! -f "$HOME/.mise.toml" ]]; then
        log "Installing default language runtimes..."
        mise use --global node@22 || warn "Failed to install Node.js"
        mise use --global rust@stable || warn "Failed to install Rust"
        mise use --global zig@0.11 || warn "Failed to install Zig"
    else
        # Install from existing configuration
        mise install || warn "Some runtimes may have failed to install"
    fi
    
    success "✅ Language runtimes configured"
}

# Install Claude Code CLI via native installer
install_claude_code() {
    if command -v claude &> /dev/null; then
        log "📦 Claude Code already installed ($(claude --version 2>/dev/null || echo 'unknown'))"
        return
    fi

    log "📦 Installing Claude Code CLI..."
    curl -fsSL https://claude.ai/install.sh | bash || warn "Failed to install Claude Code"

    success "✅ Claude Code installed successfully"
}

# Install additional packages
install_packages() {
    log "📦 Installing additional packages..."
    
    # Run the package installation script if it exists in chezmoi source
    local pkg_script="$HOME/.local/share/chezmoi/run_onchange_install-packages.sh"
    
    if [[ -f "$pkg_script" ]]; then
        log "Running package installation script..."
        bash "$pkg_script" || warn "Some packages may have failed to install"
    else
        # Fallback: install core tools directly
        if [[ "$OS" == "macOS" ]]; then
            brew install bat eza ripgrep fd zoxide fish starship || warn "Some packages may have failed"
        else
            warn "Run package installation script manually for full setup"
        fi
    fi
}

# Setup shell
setup_shell() {
    log "🐠 Setting up Fish shell..."
    
    if ! command -v fish &> /dev/null; then
        warn "Fish shell not installed yet. Install packages first."
        return
    fi
    
    # Add fish to /etc/shells if not present
    if [[ "$OS" == "Linux" ]] && ! grep -q "$(which fish)" /etc/shells 2>/dev/null; then
        echo "$(which fish)" | sudo tee -a /etc/shells >/dev/null
    fi
    
    # Offer to change default shell
    if [[ "$SHELL" != "$(which fish)" ]]; then
        log "Current shell: $SHELL"
        log "To change to fish shell, run: chsh -s $(which fish)"
    fi
}

# Detect single vs multi-user topology
detect_topology() {
    PRIMARY_USER="$(whoami)"
    SECONDARY_USERS=()

    log "🔍 Detecting system topology..."

    local mode_input
    read -r -p "Setup mode [single/multi] (default: single): " mode_input
    SETUP_MODE="${mode_input:-single}"

    if [[ "$SETUP_MODE" == "multi" ]]; then
        log "Multi-user mode. Primary user: $PRIMARY_USER"
        log "Enter secondary usernames (space-separated, accounts must already exist):"
        local secondary_input
        read -r -p "Secondary users: " secondary_input
        read -ra SECONDARY_USERS <<< "$secondary_input"

        if [[ ${#SECONDARY_USERS[@]} -eq 0 ]]; then
            warn "No secondary users specified — falling back to single-user mode"
            SETUP_MODE="single"
        else
            log "Secondary users: ${SECONDARY_USERS[*]}"
        fi
    else
        SETUP_MODE="single"
        log "Single-user mode"
    fi
}

# Create shared infrastructure for multi-user workstations
setup_system_sharing() {
    [[ "$SETUP_MODE" != "multi" ]] && return

    log "🔧 Configuring system sharing for multi-user environment..."

    # Create devs group
    if ! getent group devs >/dev/null 2>&1; then
        sudo groupadd devs
        success "Created 'devs' group"
    else
        log "Group 'devs' already exists"
    fi

    sudo usermod -aG devs "$PRIMARY_USER"
    success "Added $PRIMARY_USER to devs group"

    # Shared model cache with setgid so new files inherit group
    sudo mkdir -p /opt/shared-cache
    sudo chgrp devs /opt/shared-cache
    sudo chmod 2775 /opt/shared-cache
    success "Created /opt/shared-cache (group=devs, mode=2775)"

    # Move existing caches into shared location if not already symlinks
    for cache_name in huggingface llama.cpp; do
        local src="$HOME/.cache/$cache_name"
        local dst="/opt/shared-cache/$cache_name"
        if [[ -L "$src" ]]; then
            log "$src is already a symlink, skipping"
        elif [[ -d "$src" ]]; then
            log "Moving $src → $dst ..."
            sudo mv "$src" "$dst"
            sudo chgrp -R devs "$dst"
            sudo chmod -R g+rwX "$dst"
            ln -s "$dst" "$src"
            success "Relocated and linked $cache_name cache"
        else
            log "$src not present, skipping"
        fi
    done

    # Per-secondary-user ACLs so they can reach the SSH auth socket
    if command -v setfacl &>/dev/null; then
        for user in "${SECONDARY_USERS[@]}"; do
            log "Setting ACLs for $user on \$HOME and \$HOME/.ssh..."
            setfacl -m "u:${user}:--x" "$HOME"
            setfacl -m "u:${user}:r-x" "$HOME/.ssh"
            success "ACLs set for $user"
        done
    else
        warn "setfacl not found — install the 'acl' package for SSH socket sharing"
    fi

    # Shared repos dir: group ownership + setgid so new subdirs stay in devs
    if [[ -d "$HOME/repos" ]]; then
        sudo chgrp -R devs "$HOME/repos"
        sudo chmod g+s "$HOME/repos"
        success "Set devs group + setgid on $HOME/repos"
    else
        log "$HOME/repos not found, skipping"
    fi
}

# Bootstrap a secondary user's dotfiles on this machine
setup_secondary_user() {
    local username="$1"

    log "👤 Setting up secondary user: $username"

    if ! id "$username" &>/dev/null; then
        warn "User '$username' does not exist — skipping"
        return
    fi

    sudo usermod -aG devs "$username"

    # Gather identity details
    local full_name email
    read -r -p "Full name for $username: " full_name
    read -r -p "Corporate email for $username: " email

    # Inherit primary user's signing key (same physical person)
    local signing_key=""
    if command -v jq &>/dev/null; then
        signing_key=$(chezmoi data --format=json 2>/dev/null | jq -r '.onepassword.signing_public_key // ""' || true)
    fi

    local user_home
    user_home=$(getent passwd "$username" | cut -d: -f6)

    # Write pre-populated chezmoi config for secondary user
    local chezmoi_cfg_dir="$user_home/.config/chezmoi"
    sudo mkdir -p "$chezmoi_cfg_dir"

    sudo tee "$chezmoi_cfg_dir/chezmoi.yaml" >/dev/null <<EOF
sourceDir: "$user_home/.local/share/chezmoi"

data:
  user_role: "secondary"
  credentials_profile: "corp"
  primary_user_auth_sock: "$HOME/.ssh/auth_sock"

  git:
    name: "$full_name"
    email: "$email"

  onepassword:
    signing_public_key: "$signing_key"
EOF

    sudo chown -R "$username:$username" "$chezmoi_cfg_dir"
    success "Wrote chezmoi config for $username"

    # Init and apply dotfiles as the secondary user
    log "Initializing dotfiles for $username..."
    sudo -u "$username" chezmoi init "$REPO_URL" --no-tty || warn "chezmoi init failed for $username"
    sudo -u "$username" chezmoi apply || warn "chezmoi apply failed for $username"
    success "Dotfiles applied for $username"

    # Set fish as login shell
    if command -v fish &>/dev/null; then
        local fish_path
        fish_path=$(command -v fish)
        if ! grep -qF "$fish_path" /etc/shells 2>/dev/null; then
            echo "$fish_path" | sudo tee -a /etc/shells >/dev/null
        fi
        sudo chsh -s "$fish_path" "$username"
        success "Set fish as default shell for $username"
    else
        warn "Fish not installed — skipping shell change for $username"
    fi

    log ""
    log "  Remaining steps for $username (must be done interactively):"
    log "    op signin"
    log "    claude login"
}

# Main installation flow
main() {
    log "🚀 Starting Dynamo development environment bootstrap"
    log "📦 Using modern tooling: chezmoi + native package managers"

    # Detect topology before anything else
    detect_topology

    # Detect system
    detect_os

    # Install dependencies
    install_dependencies

    # Install core tools
    install_chezmoi
    install_mise

    # Set up dotfiles
    init_dotfiles

    # Shared system resources (multi-user only)
    setup_system_sharing

    # Bootstrap each secondary user (multi-user only)
    if [[ ${#SECONDARY_USERS[@]} -gt 0 ]]; then
        for user in "${SECONDARY_USERS[@]}"; do
            setup_secondary_user "$user"
        done
    fi

    # Set up development environment
    setup_runtimes
    install_packages
    install_claude_code
    setup_shell
    
    # Final steps
    success "🎉 Bootstrap complete!"
    log ""
    log "💡 Next steps:"
    log "   1. Restart your terminal or run: exec \$SHELL"
    log "   2. Run: fish  # to start using Fish shell"
    log "   3. Customize your config in ~/.config/chezmoi/chezmoi.yaml"
    log "   4. Update dotfiles: chezmoi update"
    log "   5. Set up commit signing:"
    log "      op item get 'YOUR-KEY-NAME' --vault Development --fields 'public key'"
    log "      # Paste the key when chezmoi init prompts for signing key"
    log "   6. Upload signing key to GitHub (for Verified badges):"
    log "      gh ssh-key add ~/.ssh/allowed_signers --type signing --title 'chezmoi-signing'"
    log "      # Or: GitHub.com > Settings > SSH and GPG keys > New SSH key > Type: Signing Key"
    log ""
    log "🔧 What you now have:"
    log "   - Modern CLI tools (bat, eza, ripgrep, fd, zoxide)"
    log "   - Development environment (helix, fish, starship, zellij)"
    log "   - Language runtimes via mise (node, rust, zig)"
    log "   - Claude Code CLI (native installer)"
    log "   - Team-standard configurations and aliases"
    log ""
    log "📚 Documentation: https://github.com/ryanolson/dynamo-dotfiles"
    log "🔄 Architecture: chezmoi (dotfiles) + mise (runtimes) + native packages"
    
    # Show installed tool versions
    log ""
    log "📋 Installed versions:"
    command -v chezmoi && log "   chezmoi: $(chezmoi --version | head -1)" || true
    command -v mise && log "   mise: $(mise --version)" || true
    command -v git && log "   git: $(git --version)" || true
}

# Run main function
main "$@"

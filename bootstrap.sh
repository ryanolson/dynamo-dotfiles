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
    
    # Initialize and apply dotfiles
    chezmoi init --apply "$REPO_URL" || error "Failed to initialize dotfiles"
    
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
        mise use --global python@3.12 || warn "Failed to install Python" 
        mise use --global rust@stable || warn "Failed to install Rust"
        mise use --global go@1.21 || warn "Failed to install Go"
        mise use --global zig@0.11 || warn "Failed to install Zig"
    else
        # Install from existing configuration
        mise install || warn "Some runtimes may have failed to install"
    fi
    
    success "✅ Language runtimes configured"
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

# Main installation flow
main() {
    log "🚀 Starting Dynamo development environment bootstrap"
    log "📦 Using modern tooling: chezmoi + native package managers"
    
    # Detect system
    detect_os
    
    # Install dependencies
    install_dependencies
    
    # Install core tools
    install_chezmoi
    install_mise
    
    # Set up dotfiles
    init_dotfiles
    
    # Set up development environment
    setup_runtimes
    install_packages
    setup_shell
    
    # Final steps
    success "🎉 Bootstrap complete!"
    log ""
    log "💡 Next steps:"
    log "   1. Restart your terminal or run: exec \$SHELL"
    log "   2. Run: fish  # to start using Fish shell"
    log "   3. Customize your config in ~/.config/chezmoi/chezmoi.yaml"
    log "   4. Update dotfiles: chezmoi update"
    log ""
    log "🔧 What you now have:"
    log "   - Modern CLI tools (bat, eza, ripgrep, fd, zoxide)"
    log "   - Development environment (helix, fish, starship, zellij)"
    log "   - Language runtimes via mise (node, python, rust, go)" 
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
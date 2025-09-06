#!/bin/bash
# Package installation script - runs when package configuration changes
# This script installs all the tools defined in the team configuration

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Detect OS
case "$OSTYPE" in
    darwin*)  OS="macOS" ;;
    linux*)   OS="Linux" ;;
    *)        error "Unsupported OS: $OSTYPE" ;;
esac

# Install packages on macOS via Homebrew
install_macos_packages() {
    if ! command -v brew &> /dev/null; then
        error "Homebrew not installed. Run bootstrap.sh first."
    fi
    
    log "📦 Installing packages via Homebrew..."
    
    # Core CLI tools
    local packages=(
        # Modern CLI replacements
        bat eza ripgrep fd zoxide dust procs
        
        # Development environment
        helix fish starship zellij
        
        # File management
        yazi broot
        
        # Git tools
        lazygit gh
        
        # Development utilities
        just watchexec hyperfine tokei
        
        # Optional but useful
        jq tree htop wget
    )
    
    for package in "${packages[@]}"; do
        if brew list "$package" &>/dev/null; then
            log "✓ $package already installed"
        else
            log "Installing $package..."
            brew install "$package" || warn "Failed to install $package"
        fi
    done
    
    success "✅ macOS packages installed"
}

# Install packages on Linux
install_linux_packages() {
    log "📦 Installing packages on Linux..."
    
    # Update package list
    if command -v apt-get &> /dev/null; then
        sudo apt-get update -qq
        
        # Install packages available via apt
        local apt_packages=(
            htop tree jq build-essential pkg-config libssl-dev fish
        )
        
        for package in "${apt_packages[@]}"; do
            if dpkg -l | grep -q "^ii  $package "; then
                log "✓ $package already installed"
            else
                log "Installing $package..."
                sudo apt-get install -y -qq "$package" || warn "Failed to install $package"
            fi
        done
    fi
    
    # Install tools via direct download/GitHub releases
    install_github_release() {
        local repo="$1"
        local name="$2"
        local install_dir="$3"
        
        # Check if already installed (handle different binary names)
        case "$name" in
            "ripgrep")
                if command -v "rg" &> /dev/null; then
                    log "✓ ripgrep (rg) already installed"
                    return
                fi
                ;;
            "helix")
                if command -v "hx" &> /dev/null; then
                    log "✓ helix (hx) already installed"
                    return
                fi
                ;;
            *)
                if command -v "$name" &> /dev/null; then
                    log "✓ $name already installed"
                    return
                fi
                ;;
        esac
        
        log "Installing $name from GitHub..."
        local latest_url="https://api.github.com/repos/$repo/releases/latest"
        local download_url=""
        
        case "$name" in
            "bat")
                download_url=$(curl -s "$latest_url" | grep "browser_download_url.*bat.*x86_64.*linux.*musl.tar.gz" | cut -d'"' -f4 | head -1)
                ;;
            "eza")
                download_url=$(curl -s "$latest_url" | grep "browser_download_url.*eza.*x86_64.*linux.*tar.gz" | cut -d'"' -f4 | head -1)
                ;;
            "ripgrep")
                download_url=$(curl -s "$latest_url" | grep "browser_download_url.*ripgrep.*x86_64.*linux.*tar.gz" | cut -d'"' -f4 | head -1)
                ;;
            "fd")
                download_url=$(curl -s "$latest_url" | grep "browser_download_url.*fd.*x86_64.*linux.*tar.gz" | cut -d'"' -f4 | head -1)
                ;;
            "zoxide")
                download_url=$(curl -s "$latest_url" | grep "browser_download_url.*zoxide.*x86_64.*linux.*tar.gz" | cut -d'"' -f4 | head -1)
                ;;
            "dust")
                download_url=$(curl -s "$latest_url" | grep "browser_download_url.*dust.*x86_64.*linux.*tar.gz" | cut -d'"' -f4 | head -1)
                ;;
            "procs")
                download_url=$(curl -s "$latest_url" | grep "browser_download_url.*procs.*x86_64.*linux.*zip" | cut -d'"' -f4 | head -1)
                ;;
            "helix")
                download_url=$(curl -s "$latest_url" | grep "browser_download_url.*helix.*x86_64.*linux.*tar.xz" | cut -d'"' -f4 | head -1)
                ;;
            "zellij")
                download_url=$(curl -s "$latest_url" | grep "browser_download_url.*zellij.*x86_64.*linux.*tar.gz" | cut -d'"' -f4 | head -1)
                ;;
            "lazygit")
                download_url=$(curl -s "$latest_url" | grep "browser_download_url.*lazygit.*Linux_x86_64.tar.gz" | cut -d'"' -f4 | head -1)
                ;;
            "just")
                download_url=$(curl -s "$latest_url" | grep "browser_download_url.*just.*x86_64.*linux.*tar.gz" | cut -d'"' -f4 | head -1)
                ;;
            "watchexec")
                download_url=$(curl -s "$latest_url" | grep "browser_download_url.*watchexec.*x86_64.*linux.*tar.xz" | cut -d'"' -f4 | head -1)
                ;;
            "hyperfine")
                download_url=$(curl -s "$latest_url" | grep "browser_download_url.*hyperfine.*x86_64.*linux.*tar.gz" | cut -d'"' -f4 | head -1)
                ;;
            "tokei")
                download_url=$(curl -s "$latest_url" | grep "browser_download_url.*tokei.*x86_64.*linux.*tar.gz" | cut -d'"' -f4 | head -1)
                ;;
        esac
        
        if [[ -n "$download_url" ]]; then
            local temp_dir=$(mktemp -d)
            cd "$temp_dir"
            
            curl -L "$download_url" -o archive || { warn "Failed to download $name"; return; }
            
            # Extract based on file type
            if file archive | grep -q "gzip"; then
                tar -xzf archive
            elif file archive | grep -q "XZ"; then
                tar -xJf archive
            elif file archive | grep -q "Zip"; then
                unzip -q archive
            fi
            
            # Find and install binary - handle different binary names
            local binary=""
            case "$name" in
                "ripgrep")
                    binary=$(find . -name "rg" -type f -executable | head -1)
                    ;;
                "helix")
                    binary=$(find . -name "hx" -type f -executable | head -1)
                    ;;
                *)
                    binary=$(find . -name "$name" -type f -executable | head -1)
                    ;;
            esac
            
            if [[ -n "$binary" ]]; then
                # Install with expected name
                case "$name" in
                    "ripgrep")
                        sudo cp "$binary" "$install_dir/rg"
                        sudo chmod +x "$install_dir/rg"
                        success "✓ ripgrep installed as rg"
                        ;;
                    "helix")
                        sudo cp "$binary" "$install_dir/hx"
                        sudo chmod +x "$install_dir/hx"
                        success "✓ helix installed as hx"
                        ;;
                    *)
                        sudo cp "$binary" "$install_dir/$name"
                        sudo chmod +x "$install_dir/$name"
                        success "✓ $name installed"
                        ;;
                esac
            else
                warn "Binary not found for $name"
            fi
            
            cd - > /dev/null
            rm -rf "$temp_dir"
        else
            warn "Download URL not found for $name"
        fi
    }
    
    # Install tools from GitHub releases
    local github_tools=(
        "sharkdp/bat:bat"
        "eza-community/eza:eza" 
        "BurntSushi/ripgrep:ripgrep"  # Binary is named ripgrep, not rg
        "sharkdp/fd:fd"
        "ajeetdsouza/zoxide:zoxide"
        "bootandy/dust:dust"
        "dalance/procs:procs"
        "helix-editor/helix:helix"  # Binary is named helix, not hx
        "zellij-org/zellij:zellij"
        "jesseduffield/lazygit:lazygit"
        "casey/just:just"
        "watchexec/watchexec:watchexec"
        "sharkdp/hyperfine:hyperfine"
        "XAMPPRocky/tokei:tokei"
    )
    
    for tool in "${github_tools[@]}"; do
        IFS=':' read -r repo name <<< "$tool"
        install_github_release "$repo" "$name" "/usr/local/bin"
    done
    
    # Install tools via direct scripts
    
    # Starship
    if ! command -v starship &> /dev/null; then
        log "Installing starship..."
        curl -sS https://starship.rs/install.sh | sh -s -- --yes || warn "Failed to install starship"
    fi
    
    # GitHub CLI (via apt repository)
    if ! command -v gh &> /dev/null; then
        log "Installing GitHub CLI..."
        curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
        sudo apt-get update -qq
        sudo apt-get install -y gh || warn "Failed to install GitHub CLI"
    fi
    
    # Yazi (file manager)
    if ! command -v yazi &> /dev/null; then
        log "Installing yazi..."
        curl -L https://github.com/sxyazi/yazi/releases/latest/download/yazi-x86_64-unknown-linux-gnu.tar.gz | tar -xz -C /tmp/
        sudo cp /tmp/yazi-x86_64-unknown-linux-gnu/yazi /usr/local/bin/
        sudo chmod +x /usr/local/bin/yazi
    fi
    
    # Broot (via snap if available)
    if command -v snap &> /dev/null && ! command -v broot &> /dev/null; then
        log "Installing broot via snap..."
        sudo snap install broot || warn "Failed to install broot"
    fi
    
    success "✅ Linux packages installed"
}

# Main installation
main() {
    log "🚀 Installing team packages for $OS..."
    
    case "$OS" in
        "macOS")
            install_macos_packages
            ;;
        "Linux")  
            install_linux_packages
            ;;
    esac
    
    success "🎉 Package installation complete!"
    log "💡 Restart your terminal or run 'exec \$SHELL' to use new tools"
}

# Only run if executed directly (not sourced)
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
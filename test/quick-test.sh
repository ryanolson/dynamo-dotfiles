#!/bin/bash
# Quick test of specific functionality without full reset

set -euo pipefail

# Colors for output
BLUE='\033[0;34m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }

# Ensure running as ubuntu user
if [[ "$(whoami)" != "ubuntu" ]]; then
    echo "This script must be run as the ubuntu user"
    exit 1
fi

ACTION="${1:-help}"

case "$ACTION" in
    packages)
        log "Testing package installation..."
        sudo su - ryan -c "bash ~/.local/share/chezmoi/run_onchange_install-packages.sh"
        ;;
    
    chezmoi-update)
        log "Updating chezmoi configuration..."
        sudo su - ryan -c "chezmoi update --force"
        ;;
    
    chezmoi-apply)
        log "Applying chezmoi configuration..."
        sudo su - ryan -c "chezmoi apply --force"
        ;;
    
    fish)
        log "Testing fish shell..."
        sudo su - ryan -c "fish -c 'echo Fish shell works!; mise --version; exit'"
        ;;
    
    mise)
        log "Testing mise runtime management..."
        sudo su - ryan -c "mise ls --current"
        ;;
    
    tools)
        log "Checking installed tools..."
        # Check tools with their actual binary names
        declare -A tools=(
            ["chezmoi"]="chezmoi"
            ["mise"]="mise"
            ["fish"]="fish"
            ["bat"]="bat"
            ["eza"]="eza"
            ["ripgrep"]="rg"
            ["fd"]="fd"
            ["zoxide"]="zoxide"
            ["helix"]="hx"
            ["starship"]="starship"
            ["zellij"]="zellij"
            ["lazygit"]="lazygit"
            ["just"]="just"
            ["gh"]="gh"
        )
        
        for name in "${!tools[@]}"; do
            binary="${tools[$name]}"
            if sudo su - ryan -c "command -v $binary" >/dev/null 2>&1; then
                success "✓ $name ($binary)"
            else
                warn "✗ $name ($binary)"
            fi
        done | sort
        ;;
    
    shell)
        log "Starting interactive shell as ryan..."
        sudo su - ryan
        ;;
    
    help|*)
        echo "Usage: $0 [action]"
        echo ""
        echo "Actions:"
        echo "  packages        - Test package installation script"
        echo "  chezmoi-update  - Update chezmoi from repo"
        echo "  chezmoi-apply   - Apply chezmoi configuration"
        echo "  fish           - Test fish shell"
        echo "  mise           - Check mise runtimes"
        echo "  tools          - Check all installed tools"
        echo "  shell          - Start interactive shell as ryan"
        echo "  help           - Show this help"
        ;;
esac
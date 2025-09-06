#!/bin/bash
# Test bootstrap script as ryan user

set -euo pipefail

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

# Parse arguments
RESET=false
VERBOSE=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --reset)
            RESET=true
            shift
            ;;
        --verbose|-v)
            VERBOSE=true
            shift
            ;;
        *)
            warn "Unknown option: $1"
            shift
            ;;
    esac
done

# Ensure running as ubuntu user
if [[ "$(whoami)" != "ubuntu" ]]; then
    error "This script must be run as the ubuntu user"
fi

# Reset ryan user if requested
if [[ "$RESET" == "true" ]]; then
    log "Resetting ryan user first..."
    bash /home/ubuntu/repo/dynamo-dotfiles/test/reset-ryan.sh
fi

# Test bootstrap as ryan
log "🚀 Testing bootstrap as ryan user..."

# Download and run bootstrap
log "Running bootstrap script..."
if [[ "$VERBOSE" == "true" ]]; then
    sudo su - ryan -c "curl -fsSL https://raw.githubusercontent.com/ryanolson/dynamo-dotfiles/main/bootstrap.sh | bash -x"
else
    sudo su - ryan -c "curl -fsSL https://raw.githubusercontent.com/ryanolson/dynamo-dotfiles/main/bootstrap.sh | bash"
fi

# Verify installation
log "🔍 Verifying installation..."

# Check chezmoi
if sudo su - ryan -c "command -v chezmoi" >/dev/null 2>&1; then
    success "✓ chezmoi installed"
    sudo su - ryan -c "chezmoi --version | head -1"
else
    warn "✗ chezmoi not found"
fi

# Check mise  
if sudo su - ryan -c "command -v mise" >/dev/null 2>&1; then
    success "✓ mise installed"
    sudo su - ryan -c "mise --version"
else
    warn "✗ mise not found"
fi

# Check fish
if sudo su - ryan -c "command -v fish" >/dev/null 2>&1; then
    success "✓ fish installed"
    sudo su - ryan -c "fish --version"
else
    warn "✗ fish not found"
fi

# Check if configs were applied
if [[ -f /home/ryan/.gitconfig ]]; then
    success "✓ .gitconfig applied"
else
    warn "✗ .gitconfig not found"
fi

if [[ -d /home/ryan/.config/fish ]]; then
    success "✓ fish config applied"
else
    warn "✗ fish config not found"
fi

# Check some CLI tools
for tool in bat eza ripgrep fd zoxide; do
    if sudo su - ryan -c "command -v $tool" >/dev/null 2>&1; then
        success "✓ $tool installed"
    else
        warn "✗ $tool not found"
    fi
done

# Check language runtimes
log "Checking language runtimes..."
sudo su - ryan -c "mise ls --current 2>/dev/null || echo 'No runtimes configured yet'"

success "🎉 Bootstrap test complete!"
log ""
log "To interact as ryan: sudo su - ryan"
log "To run fish shell: sudo su - ryan -c 'fish'"
log "To test idempotency: Run this script again without --reset"
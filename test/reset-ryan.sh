#!/bin/bash
# Reset ryan user for testing bootstrap from scratch

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

# Ensure running as ubuntu user with sudo privileges
if [[ "$(whoami)" != "ubuntu" ]]; then
    error "This script must be run as the ubuntu user"
fi

log "🧹 Resetting ryan user for fresh bootstrap testing..."

# Kill any processes owned by ryan
log "Stopping ryan's processes..."
sudo pkill -u ryan 2>/dev/null || true
sleep 1

# Remove ryan user and home directory
log "Removing ryan user and home directory..."
sudo userdel -r ryan 2>/dev/null || true

# Verify removal
if getent passwd ryan >/dev/null 2>&1; then
    error "Failed to remove ryan user"
fi

# Create fresh ryan user
log "Creating fresh ryan user..."
sudo useradd -m -s /bin/bash ryan
echo "ryan:test123" | sudo chpasswd

# Add ryan to sudo group (for package installations)
log "Adding ryan to sudo group..."
sudo usermod -aG sudo ryan

# Verify user creation
if ! getent passwd ryan >/dev/null 2>&1; then
    error "Failed to create ryan user"
fi

# Set up permissions for dynamo-dotfiles repo access
log "Setting up repository permissions..."
# Make the repo readable and executable by ryan
sudo chmod -R a+rX /home/ubuntu/repo/dynamo-dotfiles

success "✅ Ryan user reset complete!"
log ""
log "User details:"
log "  Username: ryan"
log "  Password: test123"
log "  Home: /home/ryan"
log "  Groups: $(groups ryan 2>/dev/null | cut -d: -f2)"
log ""
log "Ready to test bootstrap!"
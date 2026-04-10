#!/bin/bash
set -euo pipefail

# =============================================================================
# install_aihub.sh
#
# Installs or updates the NVIDIA AIHub CLI binary.
# Re-run this script at any time to update to the latest version.
#
# Completions are written for bash and fish on every run.
# =============================================================================

AIHUB_BASE_URL="https://gitlab-master.nvidia.com/api/v4/projects/242997/packages/generic/aihub/latest"
INSTALL_DIR="$HOME/.local/bin"
BINARY_NAME="aihub"
INSTALL_PATH="$INSTALL_DIR/$BINARY_NAME"

# --- Logging ---
log()     { echo -e "\033[0;34m[INFO]\033[0m $*"; }
success() { echo -e "\033[0;32m[SUCCESS]\033[0m $*"; }
warn()    { echo -e "\033[1;33m[WARN]\033[0m $*"; }
error()   { echo -e "\033[0;31m[ERROR]\033[0m $*" >&2; exit 1; }

# --- Platform detection ---
OS="$(uname -s)"
ARCH="$(uname -m)"

case "$OS" in
    Linux)
        case "$ARCH" in
            x86_64)  PLATFORM="linux-amd64" ;;
            aarch64) PLATFORM="linux-arm64" ;;
            *) error "Unsupported architecture: $ARCH" ;;
        esac
        CHECKSUM_CMD="sha256sum"
        ;;
    Darwin)
        case "$ARCH" in
            arm64)  PLATFORM="darwin-arm64" ;;
            x86_64) PLATFORM="darwin-amd64" ;;
            *) error "Unsupported architecture: $ARCH" ;;
        esac
        CHECKSUM_CMD="shasum -a 256"
        ;;
    *)
        error "Unsupported OS: $OS"
        ;;
esac

BINARY_FILE="aihub-${PLATFORM}"
DOWNLOAD_URL="${AIHUB_BASE_URL}/${BINARY_FILE}"
SUMS_URL="${AIHUB_BASE_URL}/SHA256SUMS"

# --- Temp dir ---
TMP_DIR="$(mktemp -d /tmp/aihub-install.XXXXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT

# --- Completions ---
setup_completions() {
    # Bash — bash-completion auto-sources ~/.local/share/bash-completion/completions/
    local bash_comp_dir="$HOME/.local/share/bash-completion/completions"
    mkdir -p "$bash_comp_dir"
    "$INSTALL_PATH" completion bash > "$bash_comp_dir/aihub"
    log "Bash completion → $bash_comp_dir/aihub"

    # Fish — auto-loaded from ~/.config/fish/completions/
    if command -v fish &>/dev/null || [[ -d "$HOME/.config/fish" ]]; then
        local fish_comp_dir="$HOME/.config/fish/completions"
        mkdir -p "$fish_comp_dir"
        "$INSTALL_PATH" completion fish > "$fish_comp_dir/aihub.fish"
        log "Fish completion → $fish_comp_dir/aihub.fish"
    fi
}

# --- Download ---
log "Fetching AIHub CLI ($PLATFORM)..."
curl -fsSL "$DOWNLOAD_URL" -o "$TMP_DIR/$BINARY_FILE"
curl -fsSL "$SUMS_URL"     -o "$TMP_DIR/SHA256SUMS"

# --- Checksum verification ---
log "Verifying checksum..."
(cd "$TMP_DIR" && grep "$BINARY_FILE" SHA256SUMS | $CHECKSUM_CMD -c --quiet)

EXPECTED_SUM="$(grep "$BINARY_FILE" "$TMP_DIR/SHA256SUMS" | awk '{print $1}')"

# --- Already installed? ---
if [[ -x "$INSTALL_PATH" ]]; then
    INSTALLED_SUM="$($CHECKSUM_CMD "$INSTALL_PATH" | awk '{print $1}')"

    if [[ "$INSTALLED_SUM" == "$EXPECTED_SUM" ]]; then
        success "Already up to date ($("$INSTALL_PATH" --version 2>/dev/null || echo 'unknown version'))."
        setup_completions
        exit 0
    fi

    CURRENT_VERSION="$("$INSTALL_PATH" --version 2>/dev/null || echo 'unknown version')"
    log "Installed: $CURRENT_VERSION"
    printf "Update available. Proceed? [y/N] "
    read -r answer
    case "$answer" in
        [yY]|[yY][eE][sS]) ;;
        *)
            log "Skipping update."
            setup_completions
            exit 0
            ;;
    esac
fi

# --- Install ---
mkdir -p "$INSTALL_DIR"
install -m 755 "$TMP_DIR/$BINARY_FILE" "$INSTALL_PATH"
success "AIHub CLI installed → $INSTALL_PATH"
"$INSTALL_PATH" --version

setup_completions
success "Done. Open a new shell or reload completions."

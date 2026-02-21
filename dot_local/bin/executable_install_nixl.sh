#!/bin/bash
set -euo pipefail

# =============================================================================
# install_nixl.sh [GIT_REF]
#
# Installs or upgrades NIXL (NVIDIA Inference Xfer Library) and prerequisites.
# Linux only. GIT_REF defaults to 'main'.
#
# REFERENCE: This script mirrors the official NIXL Dockerfile build procedure:
#   https://github.com/ai-dynamo/nixl  →  contrib/Dockerfile
#
# Key NIXL build knobs (see meson_options.txt in nixl repo):
#   -Denable_plugins=UCX,POSIX,GDS   Comma-separated list of dynamic plugins
#   -Ducx_path=<prefix>              Where UCX is installed
#   -Dinstall_headers=true            Install C/C++ headers
#   -Dbuild_tests=false               Skip test targets
#
# UCX's ./contrib/configure-release-mt is a wrapper around ./configure that
# adds: --enable-mt --disable-logging --disable-debug --disable-assertions
#        --disable-params-check
#
# To update versions, change UCX_VERSION and NIXL_REF below.
# =============================================================================

# --- Configuration ---
UCX_VERSION="v1.20.0"
UCX_PREFIX="/usr/local"
NIXL_PREFIX="/opt/nvidia/nvda_nixl"
NIXL_REPO="https://github.com/ai-dynamo/nixl.git"
NIXL_REF="${1:-main}"
NIXL_REF_FILE="$NIXL_PREFIX/.nixl-git-ref"
BUILD_DIR="$(mktemp -d /tmp/nixl-build.XXXXXX)"
trap 'rm -rf "$BUILD_DIR"' EXIT

# --- Logging ---
log()     { echo -e "\033[0;34m[INFO]\033[0m $*"; }
success() { echo -e "\033[0;32m[SUCCESS]\033[0m $*"; }
warn()    { echo -e "\033[1;33m[WARN]\033[0m $*"; }
error()   { echo -e "\033[0;31m[ERROR]\033[0m $*" >&2; exit 1; }

# --- Phase 1: Linux guard + dependency check ---
[[ "$(uname -s)" == "Linux" ]] || { echo "NIXL is only supported on Linux."; exit 0; }

# Force HTTPS for github clones. User git configs (inherited via sudo -E) may
# have insteadOf rules rewriting https:// to git@github.com:, which fails
# without SSH keys under sudo. GIT_CONFIG_GLOBAL=/dev/null makes git ignore
# the user's ~/.gitconfig entirely.
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_TERMINAL_PROMPT=0

# uv is often installed in user-local paths that sudo doesn't inherit.
# Search common locations so `sudo ./install_nixl.sh` works.
if ! command -v uv &>/dev/null; then
    for p in "$HOME/.local/bin" "$HOME/.cargo/bin" /usr/local/bin /home/*/.local/bin; do
        if [[ -x "$p/uv" ]]; then
            export PATH="$p:$PATH"
            break
        fi
    done
    command -v uv &>/dev/null || error "uv is required but not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
fi

# Determine architecture
ARCH="$(uname -m)"
case "$ARCH" in
    x86_64)  ARCH_TRIPLET="x86_64-linux-gnu" ;;
    aarch64) ARCH_TRIPLET="aarch64-linux-gnu" ;;
    *) error "Unsupported architecture: $ARCH" ;;
esac

log "Installing NIXL ref='$NIXL_REF' on $ARCH ($ARCH_TRIPLET)..."

# --- Phase 2: apt system dependencies ---
install_apt_deps() {
    log "Installing system dependencies via apt..."

    local required_pkgs=(
        build-essential autoconf automake libtool pkg-config cmake git
        python3-dev meson ninja-build
    )
    local optional_pkgs=(libibverbs-dev librdmacm-dev liburing-dev)

    local to_install=()
    for pkg in "${required_pkgs[@]}"; do
        if ! dpkg -l "$pkg" 2>/dev/null | grep -q '^ii'; then
            to_install+=("$pkg")
        fi
    done

    if [[ ${#to_install[@]} -gt 0 ]]; then
        sudo apt-get update -qq
        sudo apt-get install -y "${to_install[@]}"
    fi

    # Optional InfiniBand/RDMA packages — install only if available
    for pkg in "${optional_pkgs[@]}"; do
        if ! dpkg -l "$pkg" 2>/dev/null | grep -q '^ii'; then
            if apt-cache show "$pkg" &>/dev/null; then
                sudo apt-get install -y "$pkg" || warn "Could not install optional package: $pkg"
            else
                warn "Optional package not available in apt: $pkg"
            fi
        fi
    done

    success "System dependencies ready."
}

install_apt_deps

# --- Phase 3: Python build dependencies ---
log "Installing Python build dependencies..."
uv venv "$BUILD_DIR/.venv" --quiet
uv pip install --python "$BUILD_DIR/.venv/bin/python" --quiet meson ninja pybind11 tomlkit
export PATH="$BUILD_DIR/.venv/bin:$PATH"
success "Python build dependencies ready."

# --- Phase 4: Remove conflicting system UCX plugin dirs ---
# The NIXL Dockerfile explicitly removes these to prevent old UCX transports
# from shadowing the freshly built ones (see contrib/Dockerfile):
#   RUN rm -rf /usr/lib/ucx
#   RUN rm -rf /opt/hpcx/ucx
for ucx_dir in /usr/lib/ucx /opt/hpcx/ucx; do
    if [[ -d "$ucx_dir" ]]; then
        log "Removing conflicting system UCX dir: $ucx_dir"
        sudo rm -rf "$ucx_dir"
    fi
done

# --- Phase 5: UCX ---
# Built from source with ./contrib/configure-release-mt (the official NIXL way).
# This wrapper adds --enable-mt plus release optimizations.
install_ucx() {
    log "Installing UCX $UCX_VERSION from source with configure-release-mt..."

    # CUDA detection
    CUDA_FLAGS=""
    for cuda_path in /usr/local/cuda /usr/local/cuda-12 /usr/local/cuda-13; do
        if [[ -d "$cuda_path" ]]; then
            CUDA_FLAGS="--with-cuda=$cuda_path"
            log "CUDA found at $cuda_path — enabling CUDA support in UCX"
            break
        fi
    done

    # gdrcopy detection
    GDRCOPY_FLAGS=""
    if [[ -f /usr/local/include/gdrapi.h ]] || [[ -f /usr/include/gdrapi.h ]]; then
        GDRCOPY_FLAGS="--with-gdrcopy=/usr/local"
        log "gdrcopy found — enabling in UCX"
    fi

    git clone --depth 1 -b "$UCX_VERSION" https://github.com/openucx/ucx.git "$BUILD_DIR/ucx"
    cd "$BUILD_DIR/ucx"
    ./autogen.sh

    # Use UCX's own configure-release-mt wrapper (adds --enable-mt + release flags)
    # then layer on the same flags the NIXL Dockerfile uses.
    ./contrib/configure-release-mt \
        --prefix="$UCX_PREFIX" \
        --enable-shared \
        --disable-static \
        --disable-doxygen-doc \
        --enable-optimizations \
        --enable-cma \
        --enable-devel-headers \
        --with-verbs \
        --with-dm \
        $CUDA_FLAGS \
        $GDRCOPY_FLAGS

    make -j"$(nproc)"
    sudo make -j"$(nproc)" install-strip
    sudo ldconfig

    success "UCX $UCX_VERSION installed with MT support at $UCX_PREFIX."
}

install_ucx

# --- Phase 6: NIXL ---
install_nixl() {
    local installed_ref=""
    [[ -f "$NIXL_REF_FILE" ]] && installed_ref="$(cat "$NIXL_REF_FILE")"

    if [[ -d "$NIXL_PREFIX" ]] && [[ "$installed_ref" == "$NIXL_REF" ]]; then
        log "NIXL already at ref '$NIXL_REF' — skipping"
        return
    fi

    if [[ -n "$installed_ref" ]]; then
        log "Upgrading NIXL from '$installed_ref' to '$NIXL_REF'..."
    else
        log "Installing NIXL ref='$NIXL_REF'..."
    fi

    # Clone: try --branch first (works for tags/branches), fall back for commit SHAs
    git clone --depth 1 --branch "$NIXL_REF" "$NIXL_REPO" "$BUILD_DIR/nixl" 2>/dev/null \
        || { git clone "$NIXL_REPO" "$BUILD_DIR/nixl" && git -C "$BUILD_DIR/nixl" checkout "$NIXL_REF"; }

    cd "$BUILD_DIR/nixl"

    # Plugin list: UCX and POSIX are always enabled.
    # GDS is included when CUDA/cuFile headers are available (meson skips it gracefully if not).
    PLUGIN_LIST="UCX,POSIX,GDS"

    meson setup build \
        --prefix="$NIXL_PREFIX" \
        -Ducx_path="$UCX_PREFIX" \
        -Dinstall_headers=true \
        -Denable_plugins="$PLUGIN_LIST" \
        -Dbuild_tests=false \
        -Dbuild_examples=false
    ninja -C build
    sudo ninja -C build install

    # Record installed ref for future upgrade detection
    echo "$NIXL_REF" | sudo tee "$NIXL_REF_FILE" > /dev/null

    success "NIXL installed at '$NIXL_PREFIX'."
}

install_nixl

# --- Phase 7: Post-install — ldconfig for NIXL libs ---
# Matches the Dockerfile pattern:
#   RUN echo "$NIXL_PREFIX/lib/$ARCH-linux-gnu" > /etc/ld.so.conf.d/nixl.conf && \
#       echo "$NIXL_PLUGIN_DIR" >> /etc/ld.so.conf.d/nixl.conf && \
#       ldconfig
log "Configuring dynamic linker for NIXL..."
{
    echo "$NIXL_PREFIX/lib/$ARCH_TRIPLET"
    echo "$NIXL_PREFIX/lib/$ARCH_TRIPLET/plugins"
} | sudo tee /etc/ld.so.conf.d/nixl.conf > /dev/null
sudo ldconfig
success "ldconfig updated with NIXL library paths."

# --- Phase 8: Environment file ---
log "Writing NIXL environment variables..."

NIXL_ENV_CONTENT="export NIXL_HOME=$NIXL_PREFIX
export PATH=\"\$NIXL_HOME/bin:\$PATH\"
export LD_LIBRARY_PATH=\"$UCX_PREFIX/lib:\$NIXL_HOME/lib/$ARCH_TRIPLET:\$NIXL_HOME/lib:\${LD_LIBRARY_PATH:-}\"
export NIXL_PLUGIN_DIR=\"\$NIXL_HOME/lib/$ARCH_TRIPLET/plugins\""

if sudo tee /etc/profile.d/nixl.sh <<< "$NIXL_ENV_CONTENT" > /dev/null 2>&1; then
    log "Environment written to /etc/profile.d/nixl.sh (bash/zsh)"
else
    mkdir -p "$HOME/.local/share"
    echo "$NIXL_ENV_CONTENT" > "$HOME/.local/share/nixl.sh"
    warn "Could not write /etc/profile.d/nixl.sh — source ~/.local/share/nixl.sh manually"
fi

# Fish shell config — /etc/profile.d/*.sh is ignored by fish
FISH_NIXL_CONTENT="set -gx NIXL_HOME $NIXL_PREFIX
fish_add_path \$NIXL_HOME/bin
set -gx LD_LIBRARY_PATH $UCX_PREFIX/lib \$NIXL_HOME/lib/$ARCH_TRIPLET \$NIXL_HOME/lib \$LD_LIBRARY_PATH
set -gx NIXL_PLUGIN_DIR \$NIXL_HOME/lib/$ARCH_TRIPLET/plugins"

if [[ -d /etc/fish ]] || command -v fish &>/dev/null; then
    sudo mkdir -p /etc/fish/conf.d
    sudo tee /etc/fish/conf.d/nixl.fish <<< "$FISH_NIXL_CONTENT" > /dev/null
    log "Environment written to /etc/fish/conf.d/nixl.fish (fish)"
fi

success "NIXL installation complete. Open a new shell to apply, or:"
log "  bash/zsh: source /etc/profile.d/nixl.sh"
log "  fish:     source /etc/fish/conf.d/nixl.fish"

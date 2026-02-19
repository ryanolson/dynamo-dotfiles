#!/bin/bash
set -euo pipefail

# install_nixl.sh [GIT_REF]
# Installs or upgrades NIXL (NVIDIA Inference Xfer Library) and prerequisites.
# Linux only. GIT_REF defaults to 'main'.

# --- Configuration ---
UCX_VERSION="1.20.0"
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

# --- Phase 1: Linux guard ---
[[ "$(uname -s)" == "Linux" ]] || { echo "NIXL is only supported on Linux."; exit 0; }

log "Installing NIXL ref='$NIXL_REF'..."

# --- Phase 2: apt system dependencies ---
install_apt_deps() {
    log "Installing system dependencies via apt..."

    local required_pkgs=(
        build-essential autoconf automake libtool pkg-config cmake git
        python3-pip meson ninja-build
    )
    local optional_pkgs=(libibverbs-dev librdmacm-dev)

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
pip3 install --quiet --upgrade meson ninja pybind11 tomlkit
success "Python build dependencies ready."

# --- Phase 4: UCX ---
install_ucx() {
    if pkg-config --exists ucx 2>/dev/null && \
       [[ "$(pkg-config --modversion ucx 2>/dev/null)" == "$UCX_VERSION" ]]; then
        log "UCX $UCX_VERSION already installed — skipping"
        return
    fi

    log "Installing UCX $UCX_VERSION..."

    # CUDA detection
    CUDA_FLAGS=""
    for cuda_path in /usr/local/cuda /usr/local/cuda-12 /usr/local/cuda-13; do
        if [[ -d "$cuda_path" ]]; then
            CUDA_FLAGS="--with-cuda=$cuda_path"
            log "CUDA found at $cuda_path — enabling CUDA support in UCX"
            break
        fi
    done

    curl -fsSL "https://github.com/openucx/ucx/releases/download/v${UCX_VERSION}/ucx-${UCX_VERSION}.tar.gz" \
        | tar -xz -C "$BUILD_DIR"

    cd "$BUILD_DIR/ucx-${UCX_VERSION}"
    ./configure \
        --prefix="$UCX_PREFIX" \
        --enable-shared \
        --disable-static \
        --with-verbs \
        $CUDA_FLAGS
    make -j"$(nproc)"
    sudo make install
    sudo ldconfig

    success "UCX $UCX_VERSION installed."
}

install_ucx

# --- Phase 5: NIXL ---
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
    meson setup build \
        --prefix="$NIXL_PREFIX" \
        -Ducx_path="$UCX_PREFIX" \
        -Dinstall_headers=true \
        -Denable_plugins=UCX,POSIX
    ninja -C build
    sudo ninja -C build install

    # Record installed ref for future upgrade detection
    echo "$NIXL_REF" | sudo tee "$NIXL_REF_FILE" > /dev/null

    success "NIXL installed at '$NIXL_PREFIX'."
}

install_nixl

# --- Phase 6: Python bindings ---
log "Installing NIXL Python bindings..."

cuda_major=""
if command -v nvcc &>/dev/null; then
    cuda_major=$(nvcc --version | grep -oP 'release \K\d+')
fi

if [[ "$cuda_major" == "13" ]]; then
    pip3 install --upgrade "nixl[cu13]"
else
    pip3 install --upgrade "nixl[cu12]"
fi

success "NIXL Python bindings installed."

# --- Phase 7: Environment file ---
log "Writing NIXL environment variables..."

NIXL_ENV_CONTENT='export NIXL_HOME=/opt/nvidia/nvda_nixl
export PATH="$NIXL_HOME/bin:$PATH"
export LD_LIBRARY_PATH="$NIXL_HOME/lib:${LD_LIBRARY_PATH:-}"'

if sudo tee /etc/profile.d/nixl.sh <<< "$NIXL_ENV_CONTENT" > /dev/null 2>&1; then
    log "Environment written to /etc/profile.d/nixl.sh"
else
    mkdir -p "$HOME/.local/share"
    echo "$NIXL_ENV_CONTENT" > "$HOME/.local/share/nixl.sh"
    warn "Could not write /etc/profile.d/nixl.sh — source ~/.local/share/nixl.sh manually"
fi

success "NIXL installation complete. Re-login or source the environment file to apply PATH changes."

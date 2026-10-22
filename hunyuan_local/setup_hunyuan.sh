#!/usr/bin/env bash
# One-time setup of Hunyuan3D-2GP (offline AI 3D generation) on Fedora.
#
# Runs everything inside an Ubuntu 22.04 "distrobox" container that shares your
# home folder and NVIDIA GPU. That avoids Fedora's too-new compiler, which
# NVIDIA's CUDA compiler refuses to work with.
#
# Usage:   bash setup_hunyuan.sh
# Needs:   NVIDIA driver working on the host (nvidia-smi), ~40 GB free disk,
#          internet for the one-time downloads. Takes 20-60 minutes.

set -euo pipefail

BOX="hunyuan3d"
INSTALL_DIR="$HOME/hunyuan3d"
REPO_URL="https://github.com/deepbeepmeep/Hunyuan3D-2GP.git"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------
# Part 2: runs INSIDE the Ubuntu container
# --------------------------------------------------------------------------
if [[ "${1:-}" == "--inside" ]]; then
    export DEBIAN_FRONTEND=noninteractive

    say "Installing build tools and Python 3.10"
    sudo apt-get update -y
    sudo apt-get install -y build-essential git wget ca-certificates ninja-build \
        python3.10 python3.10-venv python3.10-dev libgl1 libglib2.0-0 libegl1 libgomp1

    if [[ ! -x /usr/local/cuda-12.4/bin/nvcc ]]; then
        say "Installing NVIDIA CUDA 12.4 toolkit (compiler only, not a driver)"
        tmp="$(mktemp -d)"
        wget -q -O "$tmp/keyring.deb" \
            https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/cuda-keyring_1.1-1_all.deb
        sudo dpkg -i "$tmp/keyring.deb"
        sudo apt-get update -y
        sudo apt-get install -y cuda-toolkit-12-4
        rm -rf "$tmp"
    fi
    export CUDA_HOME=/usr/local/cuda-12.4
    export PATH="$CUDA_HOME/bin:$PATH"
    export TORCH_CUDA_ARCH_LIST="8.6"   # RTX 30-series; speeds up compiling
    nvidia-smi >/dev/null 2>&1 || die "GPU not visible inside the container. Was it created with --nvidia?"

    if [[ ! -d "$INSTALL_DIR/venv" ]]; then
        say "Creating Python environment"
        python3.10 -m venv "$INSTALL_DIR/venv"
    fi
    # shellcheck disable=SC1091
    source "$INSTALL_DIR/venv/bin/activate"
    pip install --upgrade pip wheel "setuptools<75"

    say "Installing PyTorch 2.5.1 (CUDA 12.4) - large download"
    pip install torch==2.5.1 torchvision==0.20.1 --index-url https://download.pytorch.org/whl/cu124

    if [[ ! -d "$INSTALL_DIR/Hunyuan3D-2GP/.git" ]]; then
        say "Downloading Hunyuan3D-2GP"
        git clone --depth 1 "$REPO_URL" "$INSTALL_DIR/Hunyuan3D-2GP"
    fi
    cd "$INSTALL_DIR/Hunyuan3D-2GP"

    say "Installing Python requirements"
    grep -v -E '^\s*diso\s*$' requirements.txt > /tmp/hy3d_reqs.txt
    # Hunyuan3D-2GP predates newer diffusers releases, which break its texture
    # and text models; pin the version it was built against.
    pip install -r /tmp/hy3d_reqs.txt "diffusers==0.32.2" sentencepiece protobuf
    pip install --no-build-isolation diso

    say "Compiling texture modules (a few minutes)"
    (cd hy3dgen/texgen/custom_rasterizer && python setup.py install)
    # mesh_render.py imports this from its own folder, so build it in place
    (cd hy3dgen/texgen/differentiable_renderer && python setup.py build_ext --inplace)

    cp "$HERE/hunyuan_server.py" "$INSTALL_DIR/Hunyuan3D-2GP/hunyuan_server.py"

    say "Checking the install"
    python - <<'PY'
import torch
assert torch.cuda.is_available(), "PyTorch can't see the GPU"
print("GPU:", torch.cuda.get_device_name(0))
import custom_rasterizer, mmgp, diso  # noqa: F401
from hy3dgen.texgen.differentiable_renderer import mesh_processor
assert mesh_processor.__file__.endswith(".so"), "mesh_processor did not compile"
print("Texture modules: OK")
PY
    say "Setup finished inside the container."
    exit 0
fi

# --------------------------------------------------------------------------
# Part 1: runs on Fedora
# --------------------------------------------------------------------------
command -v nvidia-smi >/dev/null || die "nvidia-smi not found. Install the NVIDIA driver first."
[[ -f "$HERE/hunyuan_server.py" ]] || die "Keep hunyuan_server.py next to this script."

free_gb=$(df -BG --output=avail "$HOME" | tail -1 | tr -dc '0-9')
(( free_gb >= 40 )) || echo "Warning: only ${free_gb} GB free in your home folder; ~40 GB is recommended."

if ! command -v distrobox >/dev/null; then
    say "Installing distrobox (asks for your password)"
    sudo dnf install -y distrobox
fi

mkdir -p "$INSTALL_DIR"
cp "$HERE/hunyuan_server.py" "$HERE/setup_hunyuan.sh" "$HERE/start_hunyuan.sh" "$INSTALL_DIR/" 2>/dev/null || true
chmod +x "$INSTALL_DIR/"*.sh

if ! distrobox list 2>/dev/null | grep -qw "$BOX"; then
    say "Creating Ubuntu 22.04 container '$BOX' with GPU access"
    distrobox create --name "$BOX" --image docker.io/library/ubuntu:22.04 --nvidia --yes
fi

say "Installing inside the container (this is the long part)"
distrobox enter "$BOX" -- bash "$INSTALL_DIR/setup_hunyuan.sh" --inside

cat <<EOF

$(printf '\033[1;32m')All set!$(printf '\033[0m')

Start the server any time with:
    bash ~/hunyuan3d/start_hunyuan.sh

The first start downloads the AI models (~20 GB) - let it finish.
When it prints "server ready", generate from Blender's Meshy tab (Local mode).
EOF

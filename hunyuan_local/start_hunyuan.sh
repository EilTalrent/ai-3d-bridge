#!/usr/bin/env bash
# Start the local Hunyuan3D server for Blender. Leave this terminal open while
# you work; press Ctrl+C to stop it and free your GPU.
#
#   bash ~/hunyuan3d/start_hunyuan.sh            # balanced (default)
#   bash ~/hunyuan3d/start_hunyuan.sh --fast     # turbo shape model, quicker
#   bash ~/hunyuan3d/start_hunyuan.sh --quality  # full-size shape model, slower
#   bash ~/hunyuan3d/start_hunyuan.sh --multiview  # build from several views (front/back/side)
#   bash ~/hunyuan3d/start_hunyuan.sh --no-text  # skip text model, saves RAM
# Any other options are passed to hunyuan_server.py (see --help there).

BOX="hunyuan3d"
INSTALL_DIR="$HOME/hunyuan3d"
TEXT=1
ARGS=(--profile 4)

for a in "$@"; do
    case "$a" in
        --fast)    ARGS+=(--turbo) ;;
        --quality) ARGS+=(--h2) ;;
        --multiview|--mv) ARGS+=(--multiview) ;;
        --no-text) TEXT=0 ;;
        *)         ARGS+=("$a") ;;
    esac
done
(( TEXT )) && ARGS+=(--enable_t23d)

exec distrobox enter "$BOX" -- bash -c '
    cd "$1/Hunyuan3D-2GP" && source ../venv/bin/activate && shift &&
    export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True &&
    exec python hunyuan_server.py "$@"
' _ "$INSTALL_DIR" "${ARGS[@]}"

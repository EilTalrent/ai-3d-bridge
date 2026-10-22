# AI 3D Bridge

Generate 3D models from text or images, and AI-texture meshes, from a sidebar
tab inside Blender.

- **Offline (default):** runs Tencent's Hunyuan3D-2GP on your own NVIDIA GPU
  (tested target: RTX 3080 10 GB on Fedora). Free, no account needed.
- **Cloud (optional):** uses the Meshy API with a paid key, and adds remesh,
  auto-rigging and animation.

## Folders

| Folder | What it is |
|---|---|
| `blender_addon/` | The Blender add-on (sidebar tab **AI 3D**) |
| `hunyuan_local/` | Setup + server that runs Hunyuan3D on your GPU (Fedora, via distrobox) |

## Quick start

1. **Install the AI (one time):** `bash hunyuan_local/setup_hunyuan.sh`
   (details in [hunyuan_local/README.md](hunyuan_local/README.md))
2. **Start it when you want to generate:** `bash ~/hunyuan3d/start_hunyuan.sh`
   and wait for `server ready`.
3. **Install the add-on:** run `bash build_addon.sh`, then in Blender go to
   Preferences > Add-ons > Install from Disk and pick `dist/ai_3d_bridge.zip`
   (Blender 4.2+) or `dist/ai_3d_bridge.py` (any version).
4. In the 3D Viewport press **N** > **AI 3D** tab.

## Known fixes already applied

- `diffusers` is pinned to 0.32.2. Newer versions break Hunyuan3D-2GP's
  texture model (`trust_remote_code`) and text model (`Qwen3ForCausalLM`).
- `sentencepiece` + `protobuf` are installed for the text-to-image model.
- `nvidia-modprobe ... GLIBC_ABI_DT_RELR` warnings when the server starts are
  harmless (a Fedora helper that can't run inside the Ubuntu container).

## Licenses

- The add-on and scripts in this repo: GPL-3.0-or-later.
- Hunyuan3D 2.0 (downloaded during setup, not included here) is under the
  Tencent Hunyuan 3D 2.0 Community License, Copyright (c) 2025 Tencent. It does
  not apply in the EU, UK or South Korea.

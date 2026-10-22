# Offline AI 3D on your own GPU (Hunyuan3D-2GP) for Blender

Generates 3D models from text or an image, and AI-textures your own meshes,
entirely on your computer. Built for NVIDIA cards with ~10 GB VRAM (RTX 3080)
on Fedora, using the "AI 3D Bridge" Blender add-on in Local mode.

## One-time setup (20-60 minutes)

```bash
cd ~/Downloads            # wherever you unzipped this folder
unzip hunyuan_local.zip
cd hunyuan_local
bash setup_hunyuan.sh
```

It asks for your password once (to install `distrobox`), then builds an Ubuntu
container with your GPU passed through, installs CUDA's compiler, PyTorch,
Hunyuan3D-2GP, and compiles the texture modules. Everything lives in
`~/hunyuan3d` and an Ubuntu container named `hunyuan3d`. Nothing changes your
Fedora system or NVIDIA driver.

Needs about 40 GB of free disk space.

## Every time you want to generate

```bash
bash ~/hunyuan3d/start_hunyuan.sh
```

The **first** start downloads the AI models (~20 GB). Wait for
`=== Hunyuan3D server ready ===`, then leave the terminal open and use the
**AI 3D** tab in Blender (press N in the 3D Viewport). Ctrl+C stops the server
and frees your GPU.

Options:

| Command | Effect |
|---|---|
| `start_hunyuan.sh` | Balanced: Hunyuan3D-2mini shapes, textures, text input |
| `start_hunyuan.sh --fast` | Turbo shape model: much quicker, a bit less detail |
| `start_hunyuan.sh --quality` | Full Hunyuan3D-2 shape model: best detail, slower |
| `start_hunyuan.sh --multiview` | Multi-view model: build from front/back/side images (character sheets) |
| `start_hunyuan.sh --no-text` | Skip the text-to-image model (saves ~6 GB RAM) |
| `start_hunyuan.sh --profile 5` | Most aggressive memory saving if you run out of memory |

## Getting models that match your art

- **One figure per image.** Don't feed a whole character sheet: the AI will
  try to build every pose, inset and label as one object.
- **Plain or transparent background**, figure centered, whole body visible.
- **Character sheets:** cut out the front, back and side views and use the
  **Views** tab with `--multiview`. "Left"/"Right" mean which edge of the
  picture the figure is facing.
- Thin things (bows, bowstrings, blades held away from the body) come out
  blobby. Generate weapons separately from their own images.

## Tips for a 10 GB card

- Close games, Steam and heavy browser tabs before generating. Your desktop
  alone uses ~2 GB of VRAM.
- Free system RAM matters too: texturing offloads to RAM. Aim for 16 GB free.
- If you get "out of memory": lower **Texture faces** (e.g. 20000) or
  **Detail** in Blender, or restart with `--profile 5`.
- Expect roughly 1-3 minutes for a shape, plus a few minutes for texturing.
- Text prompts: only the first 60 characters are used. Short and concrete
  works best ("a wooden treasure chest with iron bands").

## Uninstall

```bash
distrobox rm hunyuan3d
rm -rf ~/hunyuan3d ~/.cache/huggingface/hub/models--tencent--Hunyuan3D-2* \
       ~/.cache/huggingface/hub/models--Tencent-Hunyuan--HunyuanDiT* ~/.u2net
```

## License note

Hunyuan3D 2.0 is licensed under the Tencent Hunyuan 3D 2.0 Community License
Agreement, Copyright (c) 2025 Tencent. All Rights Reserved. It is not licensed
for use in the EU, UK or South Korea; the full terms are in
`~/hunyuan3d/Hunyuan3D-2GP/LICENSE` after setup.

# AI 3D Bridge for Blender

Generate 3D models from text or images, and AI-texture your meshes, from a
sidebar tab in Blender. Two backends:

- **Local (default):** free and offline using Hunyuan3D on your own NVIDIA
  GPU. Set it up with the separate `hunyuan_local` package, then start its
  server before generating.
- **Meshy cloud:** Meshy's API (paid key). Adds remesh, auto-rig and animation.

## Install

1. Edit > Preferences > Add-ons (Get Extensions in 4.2+) > dropdown >
   **Install from Disk...** > pick `meshy_bridge.zip` (Blender 4.2+) or
   `meshy_bridge.py` (any version).
2. Enable **AI 3D Bridge**. In its preferences pick the backend.
3. In the 3D Viewport press **N** and open the **AI 3D** tab.

## Local mode

| Tab | What it does |
|---|---|
| Text | Prompt to 3D model (prompt becomes an image first, then a model) |
| Image | PNG/JPG/WEBP to 3D model |
| Texture | AI-paints the selected mesh from a prompt or reference image. Makes new UVs and adds a textured copy |

**Detail** controls shape resolution; **Texture faces** is the face count the
mesh is reduced to before texturing (lower = faster, less GPU memory). Use the
refresh button at the top of the tab to check the server is running.

Results import at the 3D cursor and are also saved as GLB in a
`meshy_models` folder next to your .blend (or your temp folder).

## Meshy cloud mode

Needs a Meshy Pro plan or higher for API access. Paste the key from
https://www.meshy.ai/settings/api in the add-on preferences. Every generation
uses Meshy credits.

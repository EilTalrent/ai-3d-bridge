#!/usr/bin/env bash
# Builds the installable Blender add-on files into dist/:
#   dist/ai_3d_bridge.zip  (Blender 4.2+, Install from Disk)
#   dist/ai_3d_bridge.py   (any Blender version)
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p dist
rm -f dist/ai_3d_bridge.zip
(cd blender_addon && zip -q ../dist/ai_3d_bridge.zip __init__.py blender_manifest.toml README.md)
cp blender_addon/__init__.py dist/ai_3d_bridge.py
echo "Built dist/ai_3d_bridge.zip and dist/ai_3d_bridge.py"

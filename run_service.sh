#!/bin/bash
# 运行 valhalla 服务脚本

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TILES_DIR="$SCRIPT_DIR/valhalla_tiles"

echo "Starting valhalla service..."
echo "Config: $TILES_DIR/valhalla.json"

LD_LIBRARY_PATH=/usr/local/lib valhalla_service "$TILES_DIR/valhalla.json" 1

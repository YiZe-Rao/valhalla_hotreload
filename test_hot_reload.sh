#!/bin/bash
# 测试热加载功能

TILES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/valhalla_tiles"
TRAFFIC_DIR="$TILES_DIR"

echo "Testing hot reload functionality..."

# 1. 检查 traffic.tar 是否存在
if [ ! -f "$TRAFFIC_DIR/traffic_active.tar" ]; then
    echo "ERROR: traffic_active.tar not found"
    exit 1
fi

echo "Current traffic.tar size: $(du -h $TRAFFIC_DIR/traffic_active.tar | cut -f1)"

# 2. 模拟更新：创建新的 traffic.tar
echo "Creating updated traffic.tar..."
python3 realtime_traffic_daemon.py \
    --config "$TILES_DIR/valhalla.json" \
    --heartbeat /home/admin/heartbeat-2025-03-01.csv \
    --interval 60 \
    --dry-run

# 3. 检查是否生成了新文件
if [ -f "$TRAFFIC_DIR/traffic_standby.tar" ]; then
    echo "Standby traffic.tar created: $(du -h $TRAFFIC_DIR/traffic_standby.tar | cut -f1)"
else
    echo "WARNING: traffic_standby.tar not created"
fi

# 4. 测试 HTTP API (如果服务正在运行)
echo "Testing /admin/reload_traffic API..."
curl -s -X POST http://localhost:8002/admin/reload_traffic \
    -H "Content-Type: application/json" \
    -d "{\"traffic_path\": \"$TRAFFIC_DIR/traffic_standby.tar\"}" \
    || echo "Service not running or API not available"

echo ""
echo "Test complete!"

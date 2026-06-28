#!/bin/bash
# 启动带实时流量更新的 Valhalla 服务

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TILES_DIR="$SCRIPT_DIR/valhalla_tiles"
CONFIG="$TILES_DIR/valhalla.json"

echo "Starting Valhalla service with realtime traffic..."
echo "Config: $CONFIG"
echo "Traffic dir: $TILES_DIR"

# 启动 valhalla_service
LD_LIBRARY_PATH=/usr/local/lib valhalla_service "$CONFIG" 1 &
VALHALLA_PID=$!

echo "Valhalla service started (PID: $VALHALLA_PID)"

# 等待服务启动
sleep 5

# 启动实时流量守护进程
echo "Starting realtime traffic daemon..."
python3 "$SCRIPT_DIR/realtime_traffic_daemon.py" \
    --config "$CONFIG" \
    --heartbeat /home/admin/heartbeat-2025-03-01.csv \
    --interval 5 \
    --window 60 \
    &
DAEMON_PID=$!

echo "Realtime daemon started (PID: $DAEMON_PID)"
echo ""
echo "To stop: kill $VALHALLA_PID $DAEMON_PID"
echo "Logs: tail -f /var/log/valhalla*.log"

# 等待
wait

#!/bin/bash
# Valhalla 实时交通数据热更新测试脚本
# 功能：插入实时速度数据并测试热更新

set -e

CONTAINER_NAME="admiring_bartik"
CONFIG_FILE="/valhalla_tiles/valhalla.json"
TRAFFIC_TAR="/valhalla_tiles/traffic.tar"
TEST_LAT="22.2783"
TEST_LON="114.1750"

echo "=============================================="
echo "Valhalla 实时交通数据热更新测试"
echo "=============================================="

# 1. 检查服务状态
echo ""
echo "[步骤 1] 检查 Valhalla 服务状态..."
sudo docker exec $CONTAINER_NAME curl -s "http://127.0.0.1:8002/status" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"  版本：{d.get('version','N/A')}\")"

# 2. 生成 traffic.tar (使用 45 km/h)
echo ""
echo "[步骤 2] 生成 traffic.tar (45 km/h)..."
sudo docker exec $CONTAINER_NAME valhalla_traffic_demo_utils \
    --config $CONFIG_FILE \
    --generate-live-traffic "2/647736/0,45,1775370000"

# 3. 重启服务加载新数据
echo ""
echo "[步骤 3] 重启 Valhalla 服务..."
sudo docker exec $CONTAINER_NAME bash -c "pkill valhalla_service || true"
sleep 2
sudo docker exec $CONTAINER_NAME bash -c "LD_LIBRARY_PATH=/usr/local/lib nohup valhalla_service $CONFIG_FILE 1 > /tmp/valhalla.log 2>&1 &"
sleep 4

# 4. 测试实时速度
echo ""
echo "[步骤 4] 测试实时速度数据..."
SPEED=$(sudo docker exec $CONTAINER_NAME curl -s -X POST "http://127.0.0.1:8002/locate" \
    -H "Content-Type: application/json" \
    -d "{\"locations\":[{\"lat\":$TEST_LAT,\"lon\":$TEST_LON}],\"verbose\":true}" | \
    python3 -c "import sys,json; d=json.load(sys.stdin); e=d[0]['edges'][0]; print(e.get('live_speed',{}).get('overall_speed', 'None'))" 2>/dev/null)
echo "  实时速度：$SPEED (编码值，实际速度约 $(echo "scale=1; $SPEED / 2" | bc) km/h)"

# 5. 更新 traffic.tar (使用 80 km/h)
echo ""
echo "[步骤 5] 更新 traffic.tar (80 km/h)..."
sudo docker exec $CONTAINER_NAME valhalla_traffic_demo_utils \
    --config $CONFIG_FILE \
    --generate-live-traffic "2/647736/0,80,1775370000"

# 6. 重启服务并验证更新
echo ""
echo "[步骤 6] 重启服务并验证数据更新..."
sudo docker exec $CONTAINER_NAME bash -c "pkill valhalla_service || true"
sleep 2
sudo docker exec $CONTAINER_NAME bash -c "LD_LIBRARY_PATH=/usr/local/lib nohup valhalla_service $CONFIG_FILE 1 > /tmp/valhalla.log 2>&1 &"
sleep 4

NEW_SPEED=$(sudo docker exec $CONTAINER_NAME curl -s -X POST "http://127.0.0.1:8002/locate" \
    -H "Content-Type: application/json" \
    -d "{\"locations\":[{\"lat\":$TEST_LAT,\"lon\":$TEST_LON}],\"verbose\":true}" | \
    python3 -c "import sys,json; d=json.load(sys.stdin); e=d[0]['edges'][0]; print(e.get('live_speed',{}).get('overall_speed', 'None'))" 2>/dev/null)
echo "  新实时速度：$NEW_SPEED (编码值，实际速度约 $(echo "scale=1; $NEW_SPEED / 2" | bc) km/h)"

# 7. 验证结果
echo ""
echo "=============================================="
echo "测试结果:"
if [ "$SPEED" != "None" ] && [ "$NEW_SPEED" != "None" ] && [ "$SPEED" != "$NEW_SPEED" ]; then
    echo "  [成功] 实时速度数据已成功插入和更新!"
    echo "  - 第一次速度：$SPEED -> $(echo "scale=1; $SPEED / 2" | bc) km/h"
    echo "  - 第二次速度：$NEW_SPEED -> $(echo "scale=1; $NEW_SPEED / 2" | bc) km/h"
else
    echo "  [失败] 测试未通过"
fi
echo "=============================================="

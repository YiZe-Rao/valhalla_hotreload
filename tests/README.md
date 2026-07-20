# 测试文件

## data/heartbeat/ — 原始测试数据

| 文件 | 说明 |
|---|---|
| `heartbeat-2025-03-01.csv` | Heartbeat 设备上报数据（香港区域，含 lat/lon/speed/bearing, CRLF 换行） |

**格式**: `id,f0_,location(POINT lon lat),bearing,speed,device_time,server_time`
**大小**: 450MB, 2,835,790 行
**有效记录率**: ~70% (过滤掉无效 GPS、异常速度后)

## scripts/ — 测试脚本

| 脚本 | 说明 | 依赖 |
|---|---|---|
| `test_heartbeat_parse.py` | 解析 heartbeat CSV，统计速度分布 | Python3 |
| `test_realtime_traffic_update.py` | 从 heartbeat 生成 traffic.tar 并验证速度编码 | Python3 |
| `heartbeat_to_edge_csv.py` | heartbeat → edge speed CSV 转换器 (离线+在线模式) | Python3 + valhalla_service (在线模式) |
| `valhalla_hotreload_test.sh` | 完整热重载 8 步骤验证 | Docker + valhalla_service |
| `validate_per_edge_injection.sh` | 按边注入 4 阶段验证 (离线编码+在线注入) | Python3 + Docker (在线阶段) |
| `test_hot_reload.sh` | 容器内热更新简化测试 | Docker (容器内执行) |

## 使用方法

### 离线测试 (无需 Docker)

```bash
# 解析 heartbeat 数据统计
python3 tests/scripts/test_heartbeat_parse.py tests/data/heartbeat/heartbeat-2025-03-01.csv

# 离线数据格式验证
python3 tests/scripts/heartbeat_to_edge_csv.py \
    --heartbeat tests/data/heartbeat/heartbeat-2025-03-01.csv \
    --max-records 5000 --offline

# 生成 traffic.tar (demo 模式: 固定速度)
python3 tests/scripts/test_realtime_traffic_update.py \
    --heartbeat tests/data/heartbeat/heartbeat-2025-03-01.csv \
    --output /tmp/test_traffic.tar --demo

# 生成 traffic.tar (真实模式: 使用 heartbeat 速度)
python3 tests/scripts/test_realtime_traffic_update.py \
    --heartbeat tests/data/heartbeat/heartbeat-2025-03-01.csv \
    --output /tmp/test_traffic.tar --sample 500

# 验证 traffic.tar 结构
tar tvf /tmp/test_traffic.tar | head -10
```

### 在线测试 (需要 Docker + valhalla_service)

```bash
# 在线转换: heartbeat GPS → edge CSV (调用 /locate API)
python3 tests/scripts/heartbeat_to_edge_csv.py \
    --heartbeat tests/data/heartbeat/heartbeat-2025-03-01.csv \
    --max-records 500 \
    --valhalla-url http://localhost:8002 \
    --output /tmp/edge_speeds.csv

# 完整热重载测试 (8 步骤)
bash tests/scripts/valhalla_hotreload_test.sh

# Per-Edge 注入验证 (4 阶段)
bash tests/scripts/validate_per_edge_injection.sh
```

### 注入后验证 (容器内)

**关键**: `valhalla_live_traffic --update-edges` 是离线工具，修改 traffic.tar 后必须触发热加载或重启服务:

```bash
# 1. 注入速度
valhalla_live_traffic --config /valhalla_tiles/valhalla.json --update-edges /tmp/edge_speeds.csv

# 2. 使新数据生效 — 重启服务 (当前 /admin/reload_traffic HTTP handler 未编译)
pkill valhalla_service
sleep 1
LD_LIBRARY_PATH=/usr/local/lib valhalla_service /valhalla_tiles/valhalla.json 1 &

# 如需免重启热加载，参见 realtime/src/baldr/README.md 补丁指南

# 3. 验证
curl -s -X POST http://localhost:8002/locate \
    -H "Content-Type: application/json" \
    -d '{"locations":[{"lat":22.343,"lon":114.199}],"verbose":true}' \
    | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['edges'][0].get('live_speed',{}).get('overall_speed','none'))"
```

## 测试数据特点

- **数据来源**: 香港区域，2025-03-01
- **速度范围**: 0 ~ 82 km/h (城市道路)
- **GPS 范围**: lat 22.22-22.51, lon 113.93-114.29
- **平均速度**: ~18 km/h
- **过滤规则**: lat [22.0, 22.6], lon [113.8, 114.3], speed (0, 150] km/h

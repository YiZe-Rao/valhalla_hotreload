# 测试文件

## data/heartbeat/ — 原始测试数据

| 文件 | 说明 |
|---|---|
| `heartbeat-2025-03-01.csv` | Heartbeat 设备上报数据（香港区域，含 lat/lon/speed/bearing） |

**格式**: `id,f0_,location,bearing,speed,device_time,server_time`

## scripts/ — 测试脚本

| 脚本 | 说明 |
|---|---|
| `test_heartbeat_parse.py` | 解析 heartbeat CSV，统计速度分布 |
| `test_realtime_traffic_update.py` | 从 heartbeat 生成 traffic.tar 并验证实时速度 |
| `test_hot_reload.sh` | 热更新测试（容器内插入速度数据、重启验证） |
| `valhalla_hotreload_test.sh` | 完整热重载验证（基础功能 + 热重载 + Heartbeat + 一致性 + 稳定性） |

## 使用方法

```bash
# 解析 heartbeat 数据统计
python3 tests/scripts/test_heartbeat_parse.py tests/data/heartbeat/heartbeat-2025-03-01.csv

# 生成 traffic.tar 测试
python3 tests/scripts/test_realtime_traffic_update.py \
    --heartbeat tests/data/heartbeat/heartbeat-2025-03-01.csv \
    --output /tmp/test_traffic.tar \
    --sample 1000

# 完整热重载测试
bash tests/scripts/valhalla_hotreload_test.sh
```

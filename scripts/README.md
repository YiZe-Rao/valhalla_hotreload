# 工具脚本

| 脚本 | 说明 | 运行环境 |
|------|------|----------|
| `generate_traffic_from_heartbeat.py` | 从 heartbeat 真实数据调用 `valhalla_live_traffic` 生成 traffic.tar | Docker 容器内 |

## 使用方法

```bash
# 需运行在 Docker 容器内（依赖 valhalla_live_traffic 和 valhalla_service）
python3 scripts/generate_traffic_from_heartbeat.py
```

> **注意**: 容器内路径为 `/valhalla_tiles/valhalla.json` 和 `/app/heartbeat-2025-03-01.csv`。如需本地离线生成，使用 `tests/scripts/test_realtime_traffic_update.py`。

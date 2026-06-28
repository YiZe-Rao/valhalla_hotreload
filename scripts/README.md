# 工具脚本

| 脚本 | 说明 |
|---|---|
| `generate_traffic_from_heartbeat.py` | 从 heartbeat 真实数据生成 traffic.tar（调用 valhalla_traffic_demo_utils） |

## 使用方法

```bash
# 需运行在 Docker 容器内（依赖 valhalla_traffic_demo_utils）
python3 scripts/generate_traffic_from_heartbeat.py
```

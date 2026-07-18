# Valhalla 实时速度逐边注入 (Live Traffic Per-Edge Injection)

## 1. 功能简介

该功能允许向 Valhalla 路由引擎的 `traffic.tar` 文件中**按边 (per-edge) 注入实时速度**，无需重启服务即可被路由查询消费。

### 核心能力

| 能力 | 说明 |
|------|------|
| 单边注入 | CLI 直接指定一条边的速度，适合调试和测试 |
| 批量注入 | 从 CSV 文件批量注入，适合产线数据管道 |
| 从零构建 | 无需预先存在的 traffic.tar，直接从数据创建 |
| Hot Reload | 写入后立即生效，无需重启 valhalla_service |
| 速度编码 | 自动将 km/h 转换为 TrafficSpeed 位字段（2 kph 分辨率） |
| 拥堵标注 | 支持 6-bit congestion（1-63）写入每条边 |

### 数据流

```
heartbeat GPS CSV → heartbeat_to_edge_csv.py → edge CSV → valhalla_live_traffic → traffic.tar → valhalla_service (hot reload)
```

### 零核心侵入

所有修改仅涉及新增文件和 CLI 工具，**未修改任何 Valhalla 核心引擎文件** (`graphreader.h`, `graphtile.h`, `traffictile.h`, `dynamiccost.cc` 等)。

---

## 2. 环境依赖

### 编译依赖

| 依赖 | 版本 | 说明 |
|------|------|------|
| CMake | ≥ 3.12 | 构建系统 |
| C++ | ≥ 14 | 语言标准 |
| Boost | ≥ 1.71 | `property_tree`, `algorithm` |
| cxxopts | (内置) | CLI 参数解析 |
| microtar | (内置) | tar 文件读写 |
| RapidJSON | (内置) | valhalla.json 配置解析 |
| libvalhalla | (本项目) | `baldr::GraphReader`, `baldr::TrafficTile`, `baldr::GraphId` |

### 运行时依赖

| 依赖 | 说明 |
|------|------|
| `valhalla_service` | 运行中的 Valhalla HTTP 服务（端口 8002） |
| `valhalla.json` | 配置文件，需包含 `mjolnir.tile_dir` 和 `mjolnir.traffic_extract` |
| 已构建的路由 tiles | 与 traffic.tar 对应的 `.gph` 文件必须存在 |
| Python ≥ 3.6 | 仅 `heartbeat_to_edge_csv.py` 脚本需要 |

### 配置文件要求

`valhalla.json` 的 `mjolnir` 段必须包含：

```json
{
  "mjolnir": {
    "tile_dir": "/valhalla_tiles",
    "traffic_extract": "/valhalla_tiles/traffic.tar"
  }
}
```

---

## 3. CLI 命令参考

所有命令通过 `valhalla_live_traffic` 二进制执行。

### 3.1 `--set-edge-speed` — 单条边注入

**用途**：调试、测试、临时速度覆盖。

```bash
valhalla_live_traffic \
  --config <config_path> \
  --set-edge-speed "<tile_id>,<edge_idx>,<speed_kph>[,<congestion>]"
```

**参数说明**：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `tile_id` | string | 是 | — | `level/tile_index/0` 格式，由 `/locate` API 响应中的 `edge_id.{level, tile_id}` 组合 |
| `edge_idx` | uint32 | 是 | — | 边在 tile 内的偏移量，由 `/locate` API 响应中的 `edge_id.id` 提供 |
| `speed_kph` | float | 是 | — | 注入速度 (km/h)，范围 0–252，精度 2 kph |
| `congestion` | uint8 | 否 | `1` | 拥堵程度 1–63 (`1`=畅通, `31`=中度拥堵, `51`=严重拥堵, `0`=未知) |

**示例**：

```bash
# 单条边 77 km/h，畅通
valhalla_live_traffic --config /valhalla_tiles/valhalla.json \
  --set-edge-speed "2/647736/0,370769,77,6"

# 单条边 5 km/h，严重拥堵
valhalla_live_traffic --config /valhalla_tiles/valhalla.json \
  --set-edge-speed "2/647736/0,370769,5,51"

# 同时注入多条边（重复标志即可）
valhalla_live_traffic --config /valhalla_tiles/valhalla.json \
  --set-edge-speed "2/647736/0,370769,77,6" \
  --set-edge-speed "2/647736/0,371000,45,31"
```

**预期输出**：

```
Updated 2 edges in /valhalla_tiles/traffic.tar
```

> 输出 `Updated 0 edges` 表示 traffic.tar 不存在或损坏——先用 `--generate-live-traffic` 初始化。

---

### 3.2 `--update-edges` — CSV 批量注入

**用途**：产线数据管道、heartbeat 批量转换后注入。

```bash
valhalla_live_traffic \
  --config <config_path> \
  --update-edges <csv_path>
```

**CSV 格式**：

```csv
# level/tile_index/0, edge_index, speed_kph, congestion
2/647736/0,370769,77.0,6
2/647736/0,370770,55.0,16
2/647735/0,12345,32.0,31
```

- 以 `#` 开头的行视为注释，自动跳过
- 空行自动跳过
- 至少需要 3 列（tile_id, edge_idx, speed_kph），第 4 列可选（congestion，默认 1）

**示例**：

```bash
valhalla_live_traffic --config /valhalla_tiles/valhalla.json \
  --update-edges /tmp/edge_speeds.csv
```

**预期输出**：

```
Updated 1125 edges in /valhalla_tiles/traffic.tar
```

---

### 3.3 `--generate-live-traffic` — 初始化 traffic.tar

**用途**：从零生成 traffic.tar（首次部署或重建）。

```bash
valhalla_live_traffic \
  --config <config_path> \
  --generate-live-traffic "<tile_id>,<encoded_speed>,<timestamp>"
```

**参数说明**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `tile_id` | string | `level/tile_index/0` 格式的 tile 标识 |
| `encoded_speed` | uint32 | baseline 编码速度，`speed_kph / 2`。例如 60 km/h → `30` |
| `timestamp` | uint64 | epoch 秒时间戳 |

**示例**：

```bash
valhalla_live_traffic --config /valhalla_tiles/valhalla.json \
  --generate-live-traffic "2/647736/0,30,$(date +%s)"
```

**预期输出**：

```
Generated traffic.tar successfully at /valhalla_tiles/traffic.tar
```

> 注：此命令每次只生成一个 tile。多 tile 场景建议用 `build_live_traffic_from_edges()` 库函数或先创建 baseline 再用 `--update-edges` 填充。

---

### 3.4 `--update-live-traffic` — 全局覆盖

**用途**：将已有 traffic.tar 中所有 tile 的所有边统一设为同一速度。

```bash
valhalla_live_traffic \
  --config <config_path> \
  --update-live-traffic <speed_kph>
```

**示例**：

```bash
valhalla_live_traffic --config /valhalla_tiles/valhalla.json \
  --update-live-traffic 60
```

**预期输出**：

```
Updated traffic.tar successfully at /valhalla_tiles/traffic.tar
```

---

### 3.5 其他辅助命令

| 命令 | 示例 | 说明 |
|------|------|------|
| `--help` | `valhalla_live_traffic --help` | 打印所有选项 |
| `--get-tile-id` | `valhalla_live_traffic --get-tile-id 325892112389` | 将 raw GraphId 转为 `level/tile/id` 字符串 |
| `--get-traffic-dir` | `valhalla_live_traffic --get-traffic-dir 325892112389` | 获取对应 edge 的 traffic 目录 |
| `--generate-predicted-traffic` | `valhalla_live_traffic --generate-predicted-traffic 40` | 生成 predicted traffic 的 base64 编码 |

---

## 4. heartbeat_to_edge_csv.py — GPS 数据转换脚本

### 4.1 功能

将 heartbeat GPS 点数据转换为 `--update-edges` 兼容的 CSV。

### 4.2 命令行

```bash
python3 heartbeat_to_edge_csv.py \
  --heartbeat <heartbeat_csv_path> \
  [--output <output_csv_path>] \
  [--max-records <n>] \
  [--valhalla-url <url>] \
  [--speed-window <seconds>] \
  [--delay-ms <ms>] \
  [--offline]
```

### 4.3 参数说明

| 参数 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `--heartbeat` / `-i` | 是 | — | heartbeat CSV 文件路径 |
| `--output` / `-o` | 否 | `/tmp/edge_speeds.csv` | 输出 CSV 路径 |
| `--max-records` / `-n` | 否 | `0` (全部) | 处理的 heartbeat 记录上限 |
| `--valhalla-url` | 否 | `http://localhost:8002` | valhalla_service 地址 |
| `--speed-window` / `-w` | 否 | `300.0` | 速度聚合时间窗口（秒） |
| `--delay-ms` | 否 | `50` | 两次 `/locate` 调用间隔（ms） |
| `--offline` | 否 | `false` | 离线模式：仅校验 heartbeat 数据格式 |

### 4.4 示例

```bash
# 在线模式 — 完整流程
python3 heartbeat_to_edge_csv.py \
  --heartbeat tests/data/heartbeat/heartbeat-2025-03-01.csv \
  --max-records 5000 \
  --output /tmp/edge_speeds.csv

# 离线模式 — 仅校验数据
python3 heartbeat_to_edge_csv.py \
  --heartbeat tests/data/heartbeat/heartbeat-2025-03-01.csv \
  --offline
```

### 4.5 预期输出

```
============================================================
  Heartbeat → Edge CSV 转换完成
============================================================
  Heartbeat 记录:    5000
  成功映射:          4137
  映射失败:          863
  唯一边数:          2843
  输出边数:          1125 (聚合后)
  输出文件:          /tmp/edge_speeds.csv

  # 下一步 — 注入 traffic.tar:
  valhalla_live_traffic --config valhalla.json \
      --update-edges /tmp/edge_speeds.csv

  速度统计: avg=42.3 min=5.0 max=98.0 km/h

  样本输出 (前5行):
    2/647736/0,370769,77.0,6
    2/647736/0,370770,55.0,16
    2/647735/0,12345,32.0,31
============================================================
```

---

## 5. 完整操作流程

### 5.1 首次初始化

```bash
# Step 1: 生成 baseline traffic.tar（空 tar，所有边 = baseline 速度）
valhalla_live_traffic --config /valhalla_tiles/valhalla.json \
  --generate-live-traffic "2/647736/0,30,$(date +%s)"

# Step 2: 将 heartbeat 数据转成 edge CSV
python3 heartbeat_to_edge_csv.py \
  --heartbeat tests/data/heartbeat/heartbeat-2025-03-01.csv \
  --output /tmp/edge_speeds.csv

# Step 3: 批量注入
valhalla_live_traffic --config /valhalla_tiles/valhalla.json \
  --update-edges /tmp/edge_speeds.csv
```

### 5.2 增量更新（日常运行）

```bash
# 方式 A: 单条边调试
valhalla_live_traffic --config /valhalla_tiles/valhalla.json \
  --set-edge-speed "2/647736/0,370769,77,6"

# 方式 B: 批量注入新的 heartbeat 数据
python3 heartbeat_to_edge_csv.py \
  --heartbeat /data/heartbeat-2026-06-29.csv \
  --output /tmp/today_edges.csv
valhalla_live_traffic --config /valhalla_tiles/valhalla.json \
  --update-edges /tmp/today_edges.csv
```

---

## 6. 验证方法

### 6.1 `/locate` API 验证

```bash
curl -s http://localhost:8002/locate?verbose=true \
  -H "Content-Type: application/json" \
  -d '{"locations":[{"lat":22.3430,"lon":114.1986}]}' \
  | python3 -c "
import json, sys
resp = json.load(sys.stdin)
for e in resp[0].get('edges', [])[:3]:
    ei = e.get('edge_id', {})
    ls = e.get('live_speed', {})
    ps = e.get('predicted_speeds', [])
    print(f'edge[{ei.get(\"id\")}]: '
          f'live={ls.get(\"overall_speed\", \"none\")} kph, '
          f'predicted={ps[0] if ps else \"none\"} kph')
"
```

**预期**：注入过的边显示 `live=... kph`，未注入的边显示 `live=none kph`。

### 6.2 `/route` API 验证

```bash
curl -s "http://localhost:8002/route" \
  -H "Content-Type: application/json" \
  -d '{
    "locations": [{"lat": 22.30, "lon": 114.17}, {"lat": 22.32, "lon": 114.19}],
    "costing": "auto",
    "directions_options": {"units": "km"}
  }' | python3 -c "
import json, sys
s = json.load(sys.stdin)['trip']['summary']
print(f'time={s[\"time\"]}s, length={s[\"length\"]}km')
"
```

**预期**：注入更高速度 → 路由时间缩短；注入更低速度 → 路由时间增加。

### 6.3 Hot Reload 验证

```bash
# 改变速度
valhalla_live_traffic --config /valhalla_tiles/valhalla.json \
  --set-edge-speed "2/647736/0,370769,5,51"

# 立即查询（无需 restart）
curl -s http://localhost:8002/locate?verbose=true \
  -H "Content-Type: application/json" \
  -d '{"locations":[{"lat":22.3430,"lon":114.1986}]}' \
  | python3 -c "
import json, sys
e = json.load(sys.stdin)[0]['edges'][0]
print(f'live_speed={e.get(\"live_speed\",{}).get(\"overall_speed\",\"none\")} kph')
"
```

**预期**：`live_speed=4 kph`（5 / 2 × 2 = 4，2 kph 分辨率取整）。

---

## 7. 速度编码参考

### 7.1 TrafficSpeed 位字段 (8 bytes)

| 字段 | 位宽 | 编码规则 |
|------|------|----------|
| `overall_encoded_speed` | 7 bit | `floor(speed_kph / 2)`，max 126 (= 252 km/h) |
| `encoded_speed1/2/3` | 各 7 bit | 三分段速度，全边统一时与 overall 相同 |
| `breakpoint1` | 8 bit | `255` = 第一个子段覆盖 100% 边长 |
| `breakpoint2` | 8 bit | `255` = 不使用（全边统一速度时） |
| `congestion1/2/3` | 各 6 bit | `0`=未知, `1`=畅通, `31`=中度拥堵, `63`=严重拥堵 |
| `has_incidents` | 1 bit | `0`=无事故 |
| `spare` | 1 bit | 预留 |

- `UNKNOWN_TRAFFIC_SPEED_RAW = 127`：7-bit 字段的最大值，表示速度未知
- `breakpoint1 == 0`：`speed_valid()` 返回 `false`，Valhalla 回退到 predicted speed

### 7.2 常用速度 ↔ 编码值

| km/h | encoded |
|------|---------|
| 0 | 0 |
| 5 | 2 |
| 30 | 15 |
| 60 | 30 |
| 77 | 38 |
| 120 | 60 |
| 252 | 126 (max) |

### 7.3 拥堵程度建议值

| 拥堵等级 | congestion | 速度范围 |
|----------|------------|----------|
| 畅通 | 1–10 | > 40 km/h |
| 轻度拥堵 | 11–25 | 20–40 km/h |
| 中度拥堵 | 26–40 | 10–20 km/h |
| 严重拥堵 | 41–55 | < 10 km/h |
| 停滞 | 56–63 | ≈ 0 km/h |

---

## 8. 文件结构

```
poc/
├── valhalla_code_overwrites/
│   ├── CMakeLists.txt                          # 根 CMake，注册 valhalla_live_traffic 到 data_tools
│   ├── src/
│   │   ├── CMakeLists.txt                      # 子 CMake，添加 live_traffic_utils.cc 到 libvalhalla
│   │   └── mjolnir/
│   │       ├── live_traffic_utils.h            # [新建] 库头文件: EdgeSpeedMap, encode_live_speed(), update_edge_live_speeds(), build_live_traffic_from_edges()
│   │       ├── live_traffic_utils.cc           # [新建] 库实现: mmap 编辑、tar 构建、速度编码
│   │       └── valhalla_live_traffic.cc        # [重命名扩展] CLI 工具: --update-edges, --set-edge-speed
│   └── ... (其他覆写文件)
├── valhalla/                                   # Valhalla 子模块（不修改核心文件）
├── valhalla_tiles/
│   ├── valhalla.json                           # 配置文件
│   └── traffic.tar                             # 实时速度 tar 文件（运行时生成/修改）
├── update_traffic.py                           # [修改] 二进制名更新
├── build.sh                                    # [修改] 文件复制列表更新
├── Dockerfile                                  # [修改] COPY 路径更新
└── tests/
    ├── data/heartbeat/heartbeat-2025-03-01.csv  # 测试用 heartbeat 数据
    └── scripts/
        └── heartbeat_to_edge_csv.py             # [新建] heartbeat → edge CSV 转换
```

## 9. 故障排查

| 症状 | 可能原因 | 解决 |
|------|----------|------|
| `Updated 0 edges` | traffic.tar 为空/损坏 | 执行 `--generate-live-traffic` 初始化 |
| `Tile not found` 警告 | speed_map 中的 tile 在 routing tiles 中不存在 | 检查 tile_id 是否正确，确认对应 `.gph` 文件存在 |
| `Edge index ... out of bounds` | edge_idx ≥ directed_edge_count | 检查 CSV 中的 edge_idx 是否与当前 tile 版本匹配 |
| `/locate` 返回的 `live_speed` 为 null | edge 未被注入/breakpoint=0 | 确认 `--update-edges` 或 `--set-edge-speed` 返回了 `Updated N edges` (N > 0) |
| 热加载不生效 | valhalla_service 未开启 hot reload | 确认 `realtime_traffic_daemon.py` 在运行，或手动 `POST /admin/reload_traffic` |

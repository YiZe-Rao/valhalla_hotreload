# Valhalla Traffic Project — 人工测试流程

> 基于项目现有文档和测试脚本整理，覆盖 3 个核心模块的完整测试流程。
> 测试数据: `tests/data/heartbeat/heartbeat-2025-03-01.csv` (香港区域, 450MB, 283万条记录)

---

## 目录

1. [测试前快速检查（无需 Docker, < 1 分钟）](#1-测试前快速检查无需-docker--1-分钟)
2. [离线数据与编码验证（无需 Docker, ~2 分钟）](#2-离线数据与编码验证无需-docker-2-分钟)
3. [Heartbeat 解析与 traffic.tar 生成测试（无需 Docker, ~30 秒）](#3-heartbeat-解析与-traffictar-生成测试无需-docker-30-秒)
4. [Docker 完整集成测试（需要 Docker, ~45 分钟首次 / ~5 分钟后续）](#4-docker-完整集成测试需要-docker-45-分钟首次--5-分钟后续)
5. [热重载完整验证（需要 Docker + valhalla_service）](#5-热重载完整验证需要-docker--valhalla_service)
6. [Per-Edge 注入验证（需要 Docker + valhalla_service）](#6-per-edge-注入验证需要-docker--valhalla_service)
7. [Pipeline 5 阶段验证（需要 Docker 双容器）](#7-pipeline-5-阶段验证需要-docker-双容器)
8. [测试检查清单](#8-测试检查清单)

---

## 项目架构速览

```
Heartbeat GPS CSV
       │
       ▼
┌─────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│ Stage 1     │───▶│ Stage 2      │───▶│ Stage 3      │───▶│ Stage 4      │───▶│ Stage 5      │
│ Data Clean  │    │ Map Matching │    │ Speed Calc   │    │ Empty Fill   │    │ Speed Profile│
└─────────────┘    └──────────────┘    └──────────────┘    └──────────────┘    └──────────────┘
                          │                                                          │
                          ▼                                                          ▼
                   Valhalla /trace_attributes                               traffic.tar
                   (port 8002 或 8080)                              (Valhalla 格式, mmap 读取)
                                                                              │
                                                                              ▼
                                                                     valhalla_service
                                                                     (热加载 / 路由查询)
```

### 3 个活跃模块

| 模块 | 位置 | 用途 | 端口 |
|------|------|------|------|
| **POC** | `poc/` | Valhalla + Prime Server Docker 部署, 自定义 traffic 注入 | 8002 |
| **Pipeline** | `pipeline/` | 5 阶段 ETA 交通数据处理流水线 (双容器) | 8080 |
| **Realtime** | `realtime/` | 实时交通热加载扩展 (修改 GraphReader) | 8002 |

### 关键数据格式

- **Heartbeat CSV**: `id, f0_, location(POINT lon lat), bearing, speed(km/h), device_time, server_time`
- **TrafficTile**: Header(24B) + TrafficSpeed[](8B/edge), speed 用 7-bit 编码 (2kph 分辨率)
- **GraphId**: `value = level | (tile_index << 3) | (edge_id << 25)` (bits [2:0] [24:3] [45:25])

---

## 1. 测试前快速检查（无需 Docker, < 1 分钟）

### 1a. 确认测试数据存在

```bash
# 检查 heartbeat 数据文件
ls -lh tests/data/heartbeat/heartbeat-2025-03-01.csv
# 期望: 约 450MB 的文件存在

# 快速查看前 3 行
head -3 tests/data/heartbeat/heartbeat-2025-03-01.csv
# 期望: header + 2 条 POINT(lon lat) 格式的数据

# 统计总行数
wc -l tests/data/heartbeat/heartbeat-2025-03-01.csv
# 期望: 约 2,835,790 行 (含 header)
```

### 1b. 确认测试脚本齐全

```bash
ls -la tests/scripts/
# 应包含:
#   test_heartbeat_parse.py        — 解析 heartbeat 统计速度分布
#   test_realtime_traffic_update.py — 生成 traffic.tar 验证
#   test_hot_reload.sh             — 热更新测试 (容器内)
#   valhalla_hotreload_test.sh     — 完整热重载验证 (8 步骤)
#   validate_per_edge_injection.sh — 按边注入验证 (4 阶段)
#   heartbeat_to_edge_csv.py       — heartbeat→edge CSV 转换器

ls scripts/
# 应包含:
#   generate_traffic_from_heartbeat.py  — 从 heartbeat 生成 traffic.tar
```

### 1c. 确认 Python3 可用

```bash
python3 --version
# 期望: Python 3.8+ (需要 csv, json, struct, tarfile, urllib 等标准库)
```

---

## 2. 离线数据与编码验证（无需 Docker, ~2 分钟）

这些测试验证数据格式和编码逻辑的正确性，不需要任何服务运行。

### 2a. 离线验证 heartbeat 数据格式 + GraphId 位运算 + TrafficSpeed 编码

```bash
# 运行离线验证 (Phase 1+2+3, 不触发 Docker 构建)
bash tests/scripts/validate_per_edge_injection.sh
```

**人工检查点**:
- `Phase 1` 应打印 `Heartbeat 数据离线验证报告`，包含有效记录数、平均速度、GPS 范围
- `Phase 2` 应显示所有编码测试 `[PASS]`，TrafficSpeed 2kph 分辨率编码与 C++ 一致
- `Phase 2b` 应显示 GraphId 位运算与 C++ graphid.h 一致
- `Phase 3` 应确认核心文件存在且 GraphId bug fix 已应用

### 2b. 单独测试 heartbeat_to_edge_csv.py 离线模式

```bash
# 离线模式: 仅验证数据格式，不调用 valhalla API
python3 tests/scripts/heartbeat_to_edge_csv.py \
    --heartbeat tests/data/heartbeat/heartbeat-2025-03-01.csv \
    --max-records 5000 \
    --offline
```

**人工检查点**:
- 输出应显示 `Heartbeat 数据离线验证报告`
- 有效速度记录 > 0
- 经纬度范围在香港区域内 (lat 22.0-22.6, lon 113.8-114.3)
- 速度在合理范围 (0-150 km/h)

---

## 3. Heartbeat 解析与 traffic.tar 生成测试（无需 Docker, ~30 秒）

### 3a. Speed 分布统计

```bash
# 解析前 1000 条 heartbeat 记录，输出速度统计
python3 tests/scripts/test_heartbeat_parse.py \
    tests/data/heartbeat/heartbeat-2025-03-01.csv \
    1000
```

**人工检查点**:
- 输出 `HEARTBEAT_RECORDS=xxx` (应 > 0)
- 输出平均速度、最小/最大速度
- 速度范围合理 (香港城市道路: 通常 20-80 km/h)

### 3b. 从 heartbeat 生成 traffic.tar (Demo 模式)

```bash
# Demo 模式: 用固定速度生成 traffic.tar (不依赖 valhalla_service)
python3 tests/scripts/test_realtime_traffic_update.py \
    --heartbeat tests/data/heartbeat/heartbeat-2025-03-01.csv \
    --output /tmp/test_traffic.tar \
    --sample 500 \
    --demo
```

**人工检查点**:
- 输出应显示 `Successfully created /tmp/test_traffic.tar`
- 文件大小 > 0 bytes

### 3c. 从 heartbeat 真实数据生成 traffic.tar

```bash
# 真实模式: 用 heartbeat 的 GPS+speed 生成 traffic.tar
python3 tests/scripts/test_realtime_traffic_update.py \
    --heartbeat tests/data/heartbeat/heartbeat-2025-03-01.csv \
    --output /tmp/test_traffic_real.tar \
    --sample 1000
```

**人工检查点**:
- `Processed xxx valid records` (数量应 > 0)
- `Successfully created /tmp/test_traffic_real.tar`
- 文件大小应大于 demo 模式输出

### 3d. 验证生成的 traffic.tar 结构

```bash
# 列出 tar 内容
tar tvf /tmp/test_traffic.tar | head -20

# 提取一个 tile 查看二进制结构
tar xf /tmp/test_traffic.tar -O $(tar tf /tmp/test_traffic.tar | head -1) | xxd | head -20
```

**人工检查点**:
- tar 中包含 `.gph` 文件 (Valhalla graph tile 格式)
- 文件名格式: `XXXXX.gph` (5 位 tile ID)
- 文件内容以 24 字节 header 开头 (`TrafficTileHeader`)

---

## 4. Docker 完整集成测试（需要 Docker, ~45 分钟首次 / ~5 分钟后续）

> **重要**: 在执行 Docker 测试前，先运行 Section 2+3 的离线测试确保数据正确。

### 4a. 构建 Docker 镜像（仅首次）

```bash
cd pipeline

# 构建 Container 1: Valhalla map-matching 服务 (port 8080)
docker buildx build --platform linux/amd64 -t valhalla-local-test --load .

# 等待构建完成... (约 30-40 分钟首次构建)
```

**人工检查点**:
- 构建过程无 fatal error
- 最终显示 `Successfully tagged valhalla-local-test:latest`

### 4b. 启动 Valhalla 服务并提取 way_edges.txt

```bash
# 启动容器
docker run -d -p 8080:8080 --name valhalla-test valhalla-local-test

# 等待服务启动
sleep 12

# 检查服务状态
curl -s http://localhost:8080/status | python3 -m json.tool
# 期望: 返回 JSON 包含 "version" 字段

# 提取 way_edges.txt (OSM way ID → Valhalla edge ID 映射)
docker cp valhalla-test:/custom_files/tiles/way_edges.txt \
    ./traffic_pipeline/data/road_data/way_edges.txt

# 确认文件存在且有内容
wc -l ./traffic_pipeline/data/road_data/way_edges.txt
```

### 4c. 测试 Valhalla API 端点

```bash
# 1) /status
curl -s http://localhost:8080/status | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Valhalla version: {d[\"version\"]}')"

# 2) /route — 中环 → 尖沙咀
curl -s -X POST http://localhost:8080/route \
    -H "Content-Type: application/json" \
    -d '{
        "locations":[
            {"lat":22.2816,"lon":114.1585},
            {"lat":22.2988,"lon":114.1722}
        ],
        "costing":"auto",
        "directions_options":{"units":"kilometers"}
    }' | python3 -c "
import sys,json
s=json.load(sys.stdin)['trip']['summary']
print(f'Distance: {s[\"length\"]:.2f} km')
print(f'Time:     {s[\"time\"]:.0f} sec')
print(f'Status:   {json.load(sys.stdin)[\"trip\"][\"status_message\"]}')
"

# 3) /locate — GPS → edge 映射
curl -s -X POST http://localhost:8080/locate \
    -H "Content-Type: application/json" \
    -d '{"locations":[{"lat":22.2783,"lon":114.1750}],"verbose":true}' | \
    python3 -c "
import sys,json
d=json.load(sys.stdin)
edges=d[0]['edges']
print(f'Matched {len(edges)} edges')
if edges:
    e=edges[0]
    print(f'edge_id: {e[\"id\"]}')
    print(f'distance: {e[\"distance\"]:.2f}m')
    print(f'edge_info: {e.get(\"edge_info\",{})}')
"
```

**人工检查点**:
- `/status` 返回版本号
- `/route` 返回中环→尖沙咀路由 (距离约 3-5km, 有明确的 time)
- `/locate` 返回匹配的 edge 信息 (edge_id, distance 等)

### 4d. 测试 Pipeline Container 2 (可选)

```bash
cd pipeline/traffic_pipeline

# 构建 pipeline 容器
docker build -t traffic-pipeline:latest .

# 运行 pipeline
docker run -it --rm \
    -v $(pwd)/data:/app/data \
    -v $(pwd)/config.yaml:/app/config.yaml \
    -e VALHALLA_SERVICE_URL="http://host.docker.internal:8080" \
    traffic-pipeline:latest

# 检查输出
ls -la data/output/
```

**人工检查点**:
- Pipeline 5 个阶段全部执行完毕
- `data/output/` 中有 Stage 5 生成的速度剖面文件

### 4e. 清理

```bash
docker stop valhalla-test
docker rm valhalla-test
```

---

## 5. 热重载完整验证（需要 Docker + valhalla_service）

这是最全面的自动化测试脚本，覆盖 8 个验证步骤。

### 5a. 前置条件

```bash
# 确认 Docker 镜像存在
docker images | grep valhalla-hotreload
# 如果没有, 需要先构建: cd poc && docker build -t valhalla-hotreload:latest .

# 确认 heartbeat 数据在宿主机可访问
ls -lh /home/admin/heartbeat-2025-03-01.csv 2>/dev/null || \
    echo "需要将 tests/data/heartbeat/heartbeat-2025-03-01.csv 复制或链接到 /home/admin/"

# 如果文件不在 /home/admin/, 创建符号链接
ln -sf /home/admin/valhalla-project/tests/data/heartbeat/heartbeat-2025-03-01.csv \
    /home/admin/heartbeat-2025-03-01.csv
```

### 5b. 运行完整验证

```bash
# 执行 8 步骤完整验证 (需要 sudo)
bash tests/scripts/valhalla_hotreload_test.sh
```

**8 个测试步骤一览**:

| Step | 名称 | 验证内容 | 期望结果 |
|------|------|----------|----------|
| 1/8 | 环境准备 | Docker 镜像、heartbeat 挂载、容器启动、tiles 加载 | 容器正常启动, heartbeat 可读 |
| 2/8 | 基础功能 | /status, /route (两条), /locate | 路由成功返回距离和时间 |
| 3/8 | 热重载 | 注入 60→80→5→120 km/h, 每次验证 /locate live_speed | overall_speed = speed×2, ETA 方向一致 |
| 4/8 | Heartbeat 端到端 | CSV 解析 → 均速注入 → 热重载 → 路由查询 | heartbeat 均速正确反映到 live_speed |
| 5/8 | 一致性 | 10 次相同 /route 请求 | 结果一致 (无随机波动) |
| 6/8 | 稳定性 | 30 个并发请求 + 3 次热重载 | 0 错误, 延迟稳定 |
| 7/8 | 异常处理 | 无效坐标/空body/单点/起终点相同/无效costing/海上坐标 | 正确返回错误 |
| 8/8 | 总结 | 打印 PASS/FAIL/WARN 统计 | 期望: FAIL=0 |

### 5c. 关键验证指标（人工判断）

运行脚本后，检查输出中的关键数字:

```
# Step 3 热重载: 速度与 overall_speed 对照
Speed            overall_speed    Route ETA
60 km/h (init)   120             N/A
80 km/h          160             xxx sec
5 km/h           10              yyy sec (应 > 80km/h 的 ETA)
120 km/h         240             zzz sec

# Step 4 Heartbeat: 应输出有效记录数、平均速度
有效记录:  xxxx 条
均速:      xx.x km/h
```

### 5d. 手动热重载测试（简化版）

如果想手动操作而非全自动脚本:

```bash
# 1. 启动容器
CONTAINER="valhalla-manual"
docker run -d --init --name $CONTAINER \
    -p 8002:8002 \
    -v /home/admin/heartbeat-2025-03-01.csv:/data/heartbeat.csv:ro \
    valhalla-hotreload:latest tail -f /dev/null

# 2. 生成初始 traffic.tar 并启动服务
TILE_ID="2/647736/0"
TS=$(date +%s)
docker exec $CONTAINER valhalla_traffic_demo_utils \
    --config /valhalla_tiles/valhalla.json \
    --generate-live-traffic "${TILE_ID},60,${TS}"

docker exec $CONTAINER bash -c \
    "LD_LIBRARY_PATH=/usr/local/lib nohup valhalla_service /valhalla_tiles/valhalla.json 1 > /tmp/vs.log 2>&1 &"
sleep 12

# 3. 测试 baseline
curl -s -X POST http://localhost:8002/locate \
    -H "Content-Type: application/json" \
    -d '{"locations":[{"lat":22.2783,"lon":114.1750}],"verbose":true}' | \
    python3 -c "import sys,json; e=json.load(sys.stdin)[0]['edges'][0]; print(f'overall_speed={e.get(\"live_speed\",{}).get(\"overall_speed\",\"N/A\")}')"
# 期望: overall_speed=120 (60×2)

# 4. 注入 80 km/h 并验证热重载
TS=$(date +%s)
docker exec $CONTAINER valhalla_traffic_demo_utils \
    --config /valhalla_tiles/valhalla.json \
    --generate-live-traffic "${TILE_ID},80,${TS}"
sleep 6

# 再次查询 — overall_speed 应变为 160
curl -s -X POST http://localhost:8002/locate \
    -H "Content-Type: application/json" \
    -d '{"locations":[{"lat":22.2783,"lon":114.1750}],"verbose":true}' | \
    python3 -c "import sys,json; e=json.load(sys.stdin)[0]['edges'][0]; print(f'overall_speed={e.get(\"live_speed\",{}).get(\"overall_speed\",\"N/A\")}')"
# 期望: overall_speed=160 ✓ 热重载成功!

# 5. 清理
docker stop $CONTAINER && docker rm $CONTAINER
```

**人工检查点**:
- 注入 60 km/h → overall_speed = 120 ✓
- 注入 80 km/h → overall_speed = 160 ✓
- 无需重启服务 speed 就更新了 (核心热重载功能)
- 服务在整个过程中持续可用

---

## 6. Per-Edge 注入验证（需要 Docker + valhalla_service）

这个测试验证 heartbeat GPS → edge_id 映射 → 按边速度注入的完整链路。

### 6a. 在线转换: heartbeat → edge CSV

```bash
# 确认 valhalla_service 在 8002 端口运行
curl -s http://localhost:8002/status | python3 -m json.tool

# 转换前 500 条 heartbeat → edge speed CSV (调用 /locate API)
python3 tests/scripts/heartbeat_to_edge_csv.py \
    --heartbeat tests/data/heartbeat/heartbeat-2025-03-01.csv \
    --max-records 500 \
    --valhalla-url http://localhost:8002 \
    --output /tmp/edge_speeds.csv
```

**人工检查点**:
- 输出显示 `[1/500] mapped=1 unmapped=0 unique_edges=1 rate=X.X/s`
- 最终显示唯一边数 > 0
- `/tmp/edge_speeds.csv` 存在且有数据行:
  ```bash
  head -10 /tmp/edge_speeds.csv
  # 格式: level/tile_index/0,edge_index,speed_kph,congestion
  ```

### 6b. 注入按边速度到 traffic.tar

```bash
# 将 CSV 复制到容器
docker cp /tmp/edge_speeds.csv valhalla-hotreload:/data/edge_speeds.csv

# 执行注入
docker exec valhalla-hotreload valhalla_live_traffic \
    --config /valhalla_tiles/valhalla.json \
    --update-edges /data/edge_speeds.csv
```

**人工检查点**:
- 输出 `Updated N edges in /valhalla_tiles/traffic.tar`

> ⚠️ **关键步骤**: `valhalla_live_traffic --update-edges` 是离线工具——它直接修改 `traffic.tar` 文件，但 Valhalla 服务在启动时已经 mmap 了旧文件。**修改后必须触发热加载或重启服务**，否则 `/locate` 查询将始终返回 `live=none`。详见下方 6c 步骤。

### 6c. 触发热加载 (必须执行!)

```bash
# 方法 1 (推荐): 调用热加载 API
curl -s -X POST http://localhost:8002/admin/reload_traffic \
    -H "Content-Type: application/json" \
    -d '{"traffic_path": "/valhalla_tiles/traffic.tar"}'

# 方法 2 (备选): 重启服务
# docker exec valhalla-hotreload bash -c "pkill valhalla_service || true"
# sleep 2
# docker exec valhalla-hotreload bash -c \
#     "LD_LIBRARY_PATH=/usr/local/lib nohup valhalla_service /valhalla_tiles/valhalla.json 1 > /tmp/valhalla.log 2>&1 &"
# sleep 10
```

**人工检查点**:
- 热加载 API 返回 `{"success": true, ...}`
- 如果返回 `"error"` 或连接失败，说明热加载扩展未编译进此版本的 Valhalla，改用方法 2 重启服务

### 6d. 验证注入结果

> ⚠️ **重要**: 只有用 `/locate` 查询与注入的边**完全相同**的 GPS 坐标时，才能看到 live_speed。不同的 GPS 点会匹配到不同的 edge。

```bash
# 步骤 1: 先找出目标 GPS 点对应的 edge (而非猜测)
curl -s -X POST http://localhost:8002/locate \
    -H "Content-Type: application/json" \
    -d '{"locations":[{"lat":22.343,"lon":114.199}],"verbose":true}' | \
    python3 -c "
import sys,json
d=json.load(sys.stdin)
for i,e in enumerate(d[0]['edges'][:3]):
    ei=e['edge_id']
    ls=e.get('live_speed',{})
    print(f'edge[{i}]: level={ei[\"level\"]} tile_id={ei[\"tile_id\"]} edge_index={ei[\"id\"]} graphid_value={ei[\"value\"]}')
    print(f'         live_speed={ls.get(\"overall_speed\",\"none\")} name={\",\".join(e.get(\"edge_info\",{}).get(\"names\",[\"unknown\"]))}')
    print(f'         → 注入命令: --set-edge-speed \"{ei[\"level\"]}/{ei[\"tile_id\"]}/0,{ei[\"id\"]},<speed_kph>,<congestion>\"')
"

# 步骤 2: 用上面输出的 edge 信息精确注入 (示例: level=0, tile_id=3381, edge_index=15)
docker exec valhalla-hotreload valhalla_live_traffic \
    --config /valhalla_tiles/valhalla.json \
    --set-edge-speed "0/3381/0,15,45,6"

# 步骤 3: 触发热加载 (必须!)
curl -s -X POST http://localhost:8002/admin/reload_traffic \
    -H "Content-Type: application/json" \
    -d '{"traffic_path": "/valhalla_tiles/traffic.tar"}'

# 步骤 4: 再次查询同一坐标，验证 live_speed 已更新
curl -s -X POST http://localhost:8002/locate \
    -H "Content-Type: application/json" \
    -d '{"locations":[{"lat":22.343,"lon":114.199}],"verbose":true}' | \
    python3 -c "
import sys,json
d=json.load(sys.stdin)
for i,e in enumerate(d[0]['edges'][:3]):
    ls=e.get('live_speed',{})
    print(f'edge[{i}]: overall_speed={ls.get(\"overall_speed\",\"N/A\")}, '
          f'congestion={ls.get(\"congestion1\",\"N/A\")}, '
          f'name={\",\".join(e.get(\"edge_info\",{}).get(\"names\",[\"unknown\"]))}')
"
```

**人工检查点**:
- 注入前: `live=none` 或 `overall_speed=none`
- 注入后 (重启/热重载完成后): `overall_speed` = 注入的 speed_kph × 2 (如注入 45 km/h → overall_speed=90)
- 每个 edge 应返回 `overall_speed`, `congestion` 等字段
- congestion 值在 1-63 之间

### 6e. ETA 验证（带 traffic 的路由）

```bash
# 带 date_time=current 的路由 (使用 live traffic)
curl -s -X POST http://localhost:8002/route \
    -H "Content-Type: application/json" \
    -d '{
        "locations":[
            {"lat":22.343,"lon":114.199},
            {"lat":22.282,"lon":114.159}
        ],
        "costing":"auto",
        "date_time":{"type":0,"value":"current"},
        "directions_options":{"units":"kilometers"}
    }' | python3 -c "import sys,json; s=json.load(sys.stdin)['trip']['summary']; print(f'dist={s[\"length\"]:.2f}km time={s[\"time\"]:.1f}s')"

# 对比: 不带 date_time (使用默认速度, 不受 live traffic 影响)
curl -s -X POST http://localhost:8002/route \
    -H "Content-Type: application/json" \
    -d '{
        "locations":[
            {"lat":22.343,"lon":114.199},
            {"lat":22.282,"lon":114.159}
        ],
        "costing":"auto",
        "directions_options":{"units":"kilometers"}
    }' | python3 -c "import sys,json; s=json.load(sys.stdin)['trip']['summary']; print(f'dist={s[\"length\"]:.2f}km time={s[\"time\"]:.1f}s')"
```

**人工检查点**:
- 带 `date_time=current` 的路由 ETA 受 live traffic 速度影响
- 两个 ETA 可能有差异 (取决于注入的速度与默认速度的比较)
- 如果注入的速度很低 (如 5 km/h)，带 date_time 的 ETA 应该明显更大

---

## 6f. 故障排查: 注入后 `/locate` 仍返回 `live=none`

这是最常见的错误场景。以下是完整诊断流程。

### 典型错误日志

```
# 步骤 1: 注入成功
$ valhalla_live_traffic --config /valhalla_tiles/valhalla.json \
    --update-edges /tmp/edge_speeds.csv
Updated 101 edges in /valhalla_tiles/traffic.tar        ← 看起来成功了

# 步骤 2: 查询 — 竟然是 none!
$ curl -s http://localhost:8002/locate?verbose=true \
    -d '{"locations":[{"lat":22.3430,"lon":114.1986}],"verbose":true}' | ...
edge[12562]: live=none kph                             ← 失败!!
edge[51147]: live=none kph
```

### 原因 #1 (最常见): 没有触发热加载或重启服务

```
valhalla_service 启动
  └→ mmap traffic.tar (旧版本) ───── 服务持有旧文件映射 ─────────▶
                                                                    │
valhalla_live_traffic --update-edges                                │
  └→ mmap 改 traffic.tar (新版本) ── 磁盘已更新, 但服务看不到! ────▶
                                                                    │
curl /locate                                                        │
  └→ 服务从旧 mmap 读取 → live=none ◀── 服务仍在使用旧映射! ──────┘
```

**Valhalla 在启动时通过 `midgard::tar` 将 `traffic.tar` mmap 到内存中。**
`valhalla_live_traffic --update-edges` 修改的是磁盘上的文件，但服务持有的 mmap 不会自动刷新。

**解决方法**:

```bash
# 方法 A (推荐): 热加载 API
curl -X POST http://localhost:8002/admin/reload_traffic \
    -H "Content-Type: application/json" \
    -d '{"traffic_path": "/valhalla_tiles/traffic.tar"}'
# 期望响应: {"success": true, "message": "Traffic archive hot-reloaded successfully"}

# 如果热加载 API 不可用 (返回 404 或 "Unknown action"):
# 说明此 Valhalla 版本未编译实时热加载扩展 (realtime/ 模块)。
# 使用方法 B:

# 方法 B: 重启服务
pkill valhalla_service
sleep 2
LD_LIBRARY_PATH=/usr/local/lib valhalla_service /valhalla_tiles/valhalla.json 1 &
sleep 10

# 方法 C (容器环境): 重启容器
docker restart <container_name>
sleep 12
```

### 原因 #2: Edge 不匹配 — 注入的边和查询的边不同

```
注入的边:    --set-edge-speed "2/647736/0,370769,5,51"
                                     ^^^^^^
查询返回的边: edge[12562], edge[51147]
              ^^^^^^^^^^^^  ^^^^^^^^^^^
              不同的 edge_index!
```

`/locate` 将 GPS 坐标匹配到**最近的几条道路 edge**。不同的 GPS 坐标匹配到不同的 edge。
你必须在**同一条 edge** 上执行"注入"和"查询"。

**正确流程**:

```bash
# 1. 先查询目标 GPS 点对应的 edge 信息 (不要猜!)
curl -s -X POST http://localhost:8002/locate \
    -H "Content-Type: application/json" \
    -d '{"locations":[{"lat":22.3430,"lon":114.1986}],"verbose":true}' \
    | python3 -c "
import sys, json
d = json.load(sys.stdin)
for e in d[0]['edges'][:3]:
    ei = e['edge_id']
    # 关键: level, tile_id, edge_index (id) 三者唯一确定一条边
    print(f'--set-edge-speed \"{ei[\"level\"]}/{ei[\"tile_id\"]}/0,{ei[\"id\"]},45,6\"')
"

# 2. 用上面输出的精确 edge 信息注入 (复用输出的命令)
valhalla_live_traffic --config /valhalla_tiles/valhalla.json \
    --set-edge-speed "0/3381/0,15,45,6"    # ← 从步骤 1 得到的精确值!

# 3. 触发热加载或重启
curl -X POST http://localhost:8002/admin/reload_traffic \
    -H "Content-Type: application/json" \
    -d '{"traffic_path": "/valhalla_tiles/traffic.tar"}'

# 4. 用同样的 GPS 坐标再次查询 (必须用和步骤 1 相同的坐标!)
curl -s -X POST http://localhost:8002/locate \
    -H "Content-Type: application/json" \
    -d '{"locations":[{"lat":22.3430,"lon":114.1986}],"verbose":true}' \
    | python3 -c "
import sys, json
d = json.load(sys.stdin)
for e in d[0]['edges'][:3]:
    ls = e.get('live_speed', {})
    os = ls.get('overall_speed', 'none')
    print(f'overall_speed={os}')   # ← 应该不再是 none!
"
```

### 原因 #3: 路径不一致 — config 中的 traffic_extract 与实际文件不匹配

```bash
# 检查 valhalla.json 中配置的 traffic 文件路径
grep -A1 'traffic_extract' /valhalla_tiles/valhalla.json

# 检查 valhalla_live_traffic 实际写入的路径 (看工具的输出日志)
# "Updated N edges in /valhalla_tiles/traffic.tar"

# 两者必须指向同一个文件!
# 如果 config 中是 /custom_files/traffic.tar 但工具写入 /valhalla_tiles/traffic.tar
# → 服务读取的路径和工具写入的路径不一致!
```

```bash
# 修复路径不一致:
# 方案 A: 修改 valhalla.json 中的 traffic_extract 指向正确路径
# 方案 B: 用 --config 指定正确的配置文件
valhalla_live_traffic --config /custom_files/valhalla.json --update-edges /data/edge_speeds.csv
```

### 原因 #4: edge_index 越界 — 超出了 tile 的 directed_edge_count

```
如果: edge_index >= TrafficTileHeader.directed_edge_count
则: update_edge_live_speeds() 会跳过该边并输出警告
```

```bash
# 检查 tile 中有多少条边
# 方法: 用 /locate 确认 edge_index，确保 edge_index 在 tile 范围内
# 如果不确定, 用 /locate 返回的 edge 信息 (绝对在范围内)
```

### 完整诊断脚本 (一键排查)

```bash
#!/bin/bash
# 将此脚本在容器内运行，诊断注入失败原因

CONFIG="/valhalla_tiles/valhalla.json"
TEST_LAT=22.3430
TEST_LON=114.1986
SPEED_KPH=45
CONGESTION=6

echo "=== 诊断: live_speed 注入失败 ==="
echo ""

# 1. 检查配置文件路径
echo "[1] 检查 traffic_extract 配置..."
TRAFFIC_PATH=$(python3 -c "import json; print(json.load(open('$CONFIG'))['mjolnir']['traffic_extract'])")
echo "    config → $TRAFFIC_PATH"
if [ -f "$TRAFFIC_PATH" ]; then
    echo "    ✓ 文件存在: $(ls -lh $TRAFFIC_PATH | awk '{print $5}')"
else
    echo "    ✗ 文件不存在! 路径不一致!"
fi

# 2. 查询目标 GPS 对应的 edge
echo "[2] 查询目标 GPS ($TEST_LAT, $TEST_LON) 对应的 edge..."
EDGE_INFO=$(curl -s -X POST http://localhost:8002/locate \
    -H "Content-Type: application/json" \
    -d "{\"locations\":[{\"lat\":$TEST_LAT,\"lon\":$TEST_LON}],\"verbose\":true}" \
    | python3 -c "
import sys,json
d=json.load(sys.stdin)
e=d[0]['edges'][0]
ei=e['edge_id']
ls=e.get('live_speed',{})
print(f'level={ei[\"level\"]} tile_id={ei[\"tile_id\"]} edge_index={ei[\"id\"]}')
print(f'live_before={ls.get(\"overall_speed\",\"none\")}')
")

LEVEL=$(echo "$EDGE_INFO" | grep "level=" | cut -d' ' -f1 | cut -d= -f2)
TILE_ID=$(echo "$EDGE_INFO" | grep "tile_id=" | cut -d' ' -f2 | cut -d= -f2)
EDGE_IDX=$(echo "$EDGE_INFO" | grep "edge_index=" | cut -d' ' -f3 | cut -d= -f2)
LIVE_BEFORE=$(echo "$EDGE_INFO" | grep "live_before=" | cut -d= -f2)

echo "    level=$LEVEL tile_id=$TILE_ID edge_index=$EDGE_IDX"
echo "    live_speed (before) = $LIVE_BEFORE"

# 3. 精确注入该 edge
echo "[3] 注入速度 $SPEED_KPH km/h 到该 edge..."
valhalla_live_traffic --config "$CONFIG" \
    --set-edge-speed "$LEVEL/$TILE_ID/0,$EDGE_IDX,$SPEED_KPH,$CONGESTION"

# 4. 触发热加载
echo "[4] 触发热加载..."
RELOAD_RESULT=$(curl -s -X POST http://localhost:8002/admin/reload_traffic \
    -H "Content-Type: application/json" \
    -d "{\"traffic_path\": \"$TRAFFIC_PATH\"}")
echo "    $RELOAD_RESULT"

if echo "$RELOAD_RESULT" | grep -q "error\|Unknown"; then
    echo "    ✗ 热加载 API 不可用, 需要重启服务!"
fi

# 5. 等待并验证
sleep 3
echo "[5] 验证注入结果..."
LIVE_AFTER=$(curl -s -X POST http://localhost:8002/locate \
    -H "Content-Type: application/json" \
    -d "{\"locations\":[{\"lat\":$TEST_LAT,\"lon\":$TEST_LON}],\"verbose\":true}" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['edges'][0].get('live_speed',{}).get('overall_speed','none'))")

echo "    live_speed (after) = $LIVE_AFTER"

EXPECTED=$((SPEED_KPH * 2))
if [ "$LIVE_AFTER" = "$EXPECTED" ]; then
    echo "    ✓ 成功! overall_speed=$LIVE_AFTER (期望 $EXPECTED)"
elif [ "$LIVE_AFTER" = "none" ]; then
    echo "    ✗ 仍为 none!"
    echo "    → 检查: 是否成功触发了热加载或重启?"
    echo "    → 检查: config 路径是否匹配?"
    echo "    → 检查: edge_index 是否在 tile 范围内?"
else
    echo "    ⚠ 更新了但值不对: $LIVE_AFTER (期望 $EXPECTED)"
fi
```

---

## 7. Pipeline 5 阶段验证（需要 Docker 双容器）

### 7a. 启动 Container 1 (Valhalla)

```bash
cd pipeline
docker buildx build --platform linux/amd64 -t valhalla-local-test --load .
docker run -d -p 8080:8080 --name valhalla-test valhalla-local-test
sleep 12

# 验证服务
curl -s http://localhost:8080/status | python3 -c "import sys,json; print('OK v'+json.load(sys.stdin)['version'])"

# 提取 way_edges.txt
docker cp valhalla-test:/custom_files/tiles/way_edges.txt \
    ./traffic_pipeline/data/road_data/way_edges.txt
```

### 7b. 构建并运行 Container 2 (Pipeline)

```bash
cd pipeline/traffic_pipeline
docker build -t traffic-pipeline:latest .

docker run -it --rm \
    -v $(pwd)/data:/app/data \
    -v $(pwd)/config.yaml:/app/config.yaml \
    -e VALHALLA_SERVICE_URL="http://host.docker.internal:8080" \
    traffic-pipeline:latest
```

### 7c. 验证各阶段输出

```bash
# 检查 pipeline 输出目录结构
find pipeline/traffic_pipeline/data/output -type f | sort

# 预期目录结构:
# data/output/
# ├── stage1_data_clean/        — 清洗后的 GPS 数据
# ├── stage2_map_matching/      — map-matched edges
# ├── stage3_speed_calculation/ — 每条 edge 的速度
# ├── stage4_empty_slots_fill/  — 填充后的完整 edge 速度
# └── stage5_speed_profile/     — Valhalla 格式速度剖面

# 检查 Stage 5 输出
ls -la pipeline/traffic_pipeline/data/output/stage5_speed_profile/
```

### 7d. 将 Pipeline 输出注入 Valhalla

```bash
# 复制 traffic data 到项目根
cp -r pipeline/traffic_pipeline/data/output/stage5_speed_profile/traffic_data/* \
    pipeline/traffic_data/

# 复制到容器
docker cp pipeline/traffic_data/. valhalla-test:/custom_files/traffic_data

# 注入 predicted traffic 到 tiles
docker exec valhalla-test valhalla_add_predicted_traffic \
    -c /custom_files/valhalla.json \
    -t /custom_files/traffic_data

# 重启容器使新的 tiles 生效
docker restart valhalla-test
sleep 12

# 验证: 带 date_time 的路由应使用新的 traffic 数据
curl -s -X POST http://localhost:8080/route \
    -H "Content-Type: application/json" \
    -d '{
        "locations":[
            {"lat":22.2816,"lon":114.1585},
            {"lat":22.2988,"lon":114.1722}
        ],
        "costing":"auto",
        "date_time":{"type":1,"value":"2025-03-01T12:00"},
        "directions_options":{"units":"kilometers"}
    }' | python3 -c "import sys,json; s=json.load(sys.stdin)['trip']['summary']; print(f'time={s[\"time\"]:.0f}s')"
```

### 7e. 清理

```bash
docker stop valhalla-test && docker rm valhalla-test
```

---

## 8. 测试检查清单

### 离线测试 (无需 Docker)

- [ ] 1a. 测试数据 `heartbeat-2025-03-01.csv` 存在 (450MB, 2.8M+ 行)
- [ ] 1b. 所有测试脚本齐全 (6 个脚本 + 数据文件)
- [ ] 1c. Python3 可用
- [ ] 2a. `validate_per_edge_injection.sh` 离线 Phase 1/2/3 全部 PASS
- [ ] 2b. `heartbeat_to_edge_csv.py --offline` 输出有效记录
- [ ] 3a. `test_heartbeat_parse.py` 输出合理速度统计
- [ ] 3b. `test_realtime_traffic_update.py --demo` 成功生成 traffic.tar
- [ ] 3c. `test_realtime_traffic_update.py` 真实模式成功
- [ ] 3d. traffic.tar 内部结构正确 (.gph 文件, 24B header)

### Docker 集成测试

- [ ] 4a. Container 1 镜像构建成功 (`valhalla-local-test`)
- [ ] 4b. Valhalla 服务启动, `/status` 返回版本号
- [ ] 4c. `/route` 中环→尖沙咀 返回有效路由
- [ ] 4c. `/locate` 返回 edge 匹配结果
- [ ] 4d. Pipeline Container 2 5 阶段全部完成 (可选)

### 热重载测试

- [ ] 5a. heartbeat 数据可挂载到容器
- [ ] 5b. Step 2 基础功能 (/status, /route, /locate) 正常
- [ ] 5b. Step 3 热重载: 60→80→5→120 km/h 速度验证通过
- [ ] 5b. Step 3 overall_speed = speed×2 正确
- [ ] 5b. Step 4 Heartbeat 均速正确注入
- [ ] 5b. Step 5 10 次一致性测试通过
- [ ] 5b. Step 6 稳定性测试 0 错误
- [ ] 5b. Step 7 异常输入返回正确错误
- [ ] 5b. Step 8 FAIL=0

### Per-Edge 注入测试

- [ ] 6a. `heartbeat_to_edge_csv.py` 在线模式成功映射 GPS → edge
- [ ] 6b. `valhalla_live_traffic --update-edges` 注入成功
- [ ] 6c. `/locate` 返回正确的 overall_speed 和 congestion
- [ ] 6d. 带 date_time 的路由 ETA 受 live traffic 影响

### Pipeline 端到端测试

- [ ] 7a. Container 1 Valhalla 在 8080 端口运行
- [ ] 7b. Container 2 Pipeline 5 阶段全部执行
- [ ] 7c. 各阶段输出文件存在
- [ ] 7d. predicted traffic 注入后路由反映新速度

---

## API 端点速查

| 端点 | 方法 | 用途 | 示例 |
|------|------|------|------|
| `/status` | GET | 服务状态和版本 | `curl localhost:8002/status` |
| `/route` | POST | 时间相关路由 | 见 Section 4c |
| `/trace_attributes` | POST | Map matching (GPS → edges) | Pipeline Stage 2 使用 |
| `/locate` | POST | 点匹配到最近道路 edge | `verbose=true` 获取 live_speed |
| `/isochrone` | POST | 可达性区域 | 支持 traffic 参数 |
| `/admin/reload_traffic` | POST | 热加载 traffic.tar | 见 Section 5 |

## 关键约束和边界条件

- **速度编码**: 2kph 分辨率, 最大编码值 126 (252 km/h), 127=UNKNOWN
- **速度下限**: speed > 5 km/h 才被 Valhalla 采用
- **拥堵值范围**: 1-63 (1=严重拥堵, 63=畅通?... 实际代码中 51=严重, 6=畅通)
- **GPS 过滤**: lat [22.0, 22.6], lon [113.8, 114.3] (香港区域)
- **速度过滤**: (0, 150] km/h
- **GraphId**: level bits[2:0], tile_index bits[24:3], edge_id bits[45:25]
- **TrafficTile header**: 24 bytes = tile_id(8) + last_update(8) + edge_count(4) + version(4) + spare(4+4)
- **热重载机制**: 文件 mtime 变更自动检测, mutex 保护 tile_extract_ 原子替换
- **Docker 路径**: 容器内 `/valhalla_tiles/`, `/custom_files/`; 宿主机路径因环境而异

## 故障排查快速参考

| 问题 | 检查方法 |
|------|----------|
| 服务无法启动 | `docker logs <container>` 查看错误日志 |
| /locate 无返回 | 检查 tiles 是否加载: `docker exec <container> find /valhalla_tiles -name "*.gph" \| wc -l` |
| traffic.tar 未生成 | 检查守护进程日志: `tail -f /valhalla_tiles/valhalla.log` |
| 热加载不生效 | 确认文件 mtime 已变更; 检查 `ls -la /valhalla_tiles/traffic_*.tar` |
| 速度数据为 0 | 检查速度是否 >5 km/h (低于阈值被忽略) |
| heartbeat 解析失败 | 确认 CSV 格式: header + POINT(lon lat) WKT 格式 |
| Docker 构建慢 | 首次构建约 30-40 分钟, 后续利用缓存约 5-10 分钟 |

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Valhalla Traffic Project — 基于 Valhalla 路由引擎的实时交通数据处理系统，包含3个活跃模块:

| 模块 | 用途 | 技术栈 |
|------|------|--------|
| `poc/` | Valhalla + Prime Server Docker 部署，含自定义 traffic 支持 | C++ (CMake), Python, Docker |
| `pipeline/` | 5 阶段 ETA 交通数据处理流水线 | Python (Polars/Pandas), Docker |
| `realtime/` | 实时交通热加载扩展（修改 Valhalla GraphReader） | C++, Python, Bash |

`backup/` 是 `poc/` 的历史备份，日常开发不使用。

## Build & Run

### POC 模块 (poc/)

```bash
cd poc
./build.sh                           # 完整构建: prime_server → valhalla → tiles → traffic
./run_service.sh                     # 启动 valhalla_service (port 8002)
./run_realtime_service.sh            # 启动 service + realtime_traffic_daemon.py
```

Docker 构建（替代 `build.sh`）:
```bash
cd poc
docker build -t valhalla-traffic .
docker run -p 8002:8002 -it valhalla-traffic bash
# 容器内: LD_LIBRARY_PATH=/usr/local/lib valhalla_service /valhalla_tiles/valhalla.json 1
```

### Pipeline 模块 (pipeline/)

Pipeline 是双容器架构:
- **Container 1 (Valhalla)**: 在 port 8080 运行 `trace_attributes` map-matching 服务
- **Container 2 (Pipeline)**: 消费 Container 1 的 API，执行 5 阶段流水线

```bash
cd pipeline
# Container 1
docker buildx build --platform linux/amd64 -t valhalla-local-test --load .
docker run -d -p 8080:8080 --name valhalla-test valhalla-local-test
docker cp valhalla-test:/custom_files/tiles/way_edges.txt ./traffic_pipeline/data/road_data/

# Container 2
cd traffic_pipeline
docker build -t traffic-pipeline:latest .
docker run -it --rm \
  -v $(pwd)/data:/app/data \
  -e VALHALLA_SERVICE_URL="http://host.docker.internal:8080" \
  traffic-pipeline:latest
```

### Realtime 模块 (realtime/)

```bash
cd realtime
./build.sh    # 注入热加载代码到 valhalla GraphReader，编译，部署 Python daemon
```

### 运行测试

```bash
# 解析 heartbeat 数据统计
python3 tests/scripts/test_heartbeat_parse.py tests/data/heartbeat/heartbeat-2025-03-01.csv

# 生成 traffic.tar 测试
python3 tests/scripts/test_realtime_traffic_update.py \
    --heartbeat tests/data/heartbeat/heartbeat-2025-03-01.csv \
    --output /tmp/test_traffic.tar --sample 1000

# 完整热重载测试 (需要运行中的 valhalla_service)
bash tests/scripts/valhalla_hotreload_test.sh
```

## Architecture

### 数据流总览

```
Heartbeat GPS CSV → [Data Clean] → [Map Matching] → [Speed Calc] → [Empty Slots Fill] → [Speed Profile]
                                                                                            ↓
                                                                              traffic.tar (Valhalla 格式)
                                                                                            ↓
                                                                              valhalla_service (热加载)
```

### POC 模块关键架构

自定义 traffic 功能通过 **文件覆写** 方式注入 Valhalla:

1. `valhalla_code_overwrites/CMakeLists.txt` — 根 CMakeLists，将 `valhalla_traffic_demo_utils` 加入 `valhalla_data_tools`
2. `valhalla_code_overwrites/src/CMakeLists.txt` — 添加 `microtar` 库依赖到 valhalla target
3. `valhalla_code_overwrites/src/mjolnir/valhalla_traffic_demo_utils.cc` — 核心 custom utility，使用 Valhalla 内部 `baldr::GraphReader` / `mjolnir::GraphTileBuilder` 读写 traffic 数据

`build.sh` 将这些文件复制到 valhalla 源码目录后编译。

### Pipeline 5 阶段

| 阶段 | 类 | 说明 |
|------|-----|------|
| Stage 1 | `DataCleanStage` | 清洗 GPS 轨迹和 trip 数据，过滤异常点 |
| Stage 2 | `MapMatchingStage` | 调用 Valhalla `/trace_attributes` 将 GPS 匹配到道路 edge |
| Stage 3 | `SpeedCalculationStage` | 从 map-matched 点计算每条 edge 的速度 |
| Stage 4 | `EmptySlotsFillingStage` | 填充无数据 edge 的速度（缺失值填充） |
| Stage 5 | `SpeedProfileGenerationStage` | 生成 Valhalla historical traffic 格式输出 |

核心类:
- `PipelineOrchestrator` (`orchestrator.py`) — 协调所有阶段执行
- `PipelineConfig` / `DataNode` / `StageResult` (`pipeline/base.py`) — 配置和数据流抽象
- `ValhallaClient` (`clients/valhalla_client.py`) — 异步 HTTP 客户端调用 Valhalla API

### Realtime 热加载机制

双缓冲 traffic.tar 原子切换:

```
realtime_traffic_daemon.py
  ├── 读取 heartbeat CSV 流
  ├── GPS → edge_index 映射 (_map_to_edge_index)
  ├── 60s 滑动窗口时间衰减加权平均
  ├── 生成 next.tar.new → 原子 rename 为 standby.tar
  └── POST /admin/reload_traffic 通知 valhalla_service 热加载
```

Valhalla C++ 端 (`realtime/src/baldr/`): 修改 `GraphReader` 添加 `HotReloadTrafficArchive()` 方法，用 mutex 保护 `tile_extract_` 的原子替换。

## Traffic 数据格式

### Predicted Traffic (历史速度)
- CSV 格式: `edge_id, freeflow_speed, constrained_speed, historical_speeds...`
- 按 tile 目录层级存放，由 `valhalla_add_predicted_traffic` 嵌入 tiles
- 速度必须 >5 km/h 才被 Valhalla 采用

### Live Traffic (实时速度)
- `traffic.tar` 文件，由 Valhalla 通过 mmap 直接读取
- 每个 tile 一个条目: `TrafficTileHeader` (24 bytes) + `TrafficSpeed[]` (8 bytes/边)
- TrafficSpeed bitfield: speed(7bit) × 4 + breakpoint(8bit) × 2 + congestion(6bit) × 3
- Live traffic 优先级高于 predicted traffic

### Heartbeat CSV 格式
```
id,f0_,location,bearing,speed,device_time,server_time
3ae38ba2...,v6y5Uns...,POINT(114.198600738 22.343012951),2.66,4.01,2025-02-28 16:00:00,...
```
测试数据: `tests/data/heartbeat/heartbeat-2025-03-01.csv` (香港区域)

## 关键 Valhalla 工具

| 工具 | 用途 |
|------|------|
| `valhalla_build_tiles` | 从 OSM .pbf 生成路由 tiles |
| `valhalla_ways_to_edges` | 生成 OSM way ID → Valhalla edge ID 映射 (`way_edges.txt`) |
| `valhalla_add_predicted_traffic` | 将 predicted traffic CSV 嵌入 tiles |
| `valhalla_traffic_demo_utils` | 自定义工具: 生成 live traffic tar, 查询 traffic 目录等 |
| `valhalla_service` | HTTP 路由服务 |

## Valhalla API Endpoints (POC: port 8002, Pipeline: port 8080)

- `/route` — 时间相关路由 (支持 `date_time` 参数启用 traffic)
- `/trace_attributes` — Map matching，返回 edge IDs 和 matched points
- `/locate` — 点匹配到最近道路，返回 `predicted_speeds` 和 `live_speed`
- `/isochrone` — 可达性区域 (支持 traffic)
- `/admin/reload_traffic` — 热加载 traffic.tar (realtime 模块添加的扩展端点)

## 路径注意事项

- `valhalla.json` 中的路径在 Docker 容器内和物理机上不同
  - Docker 容器: `/valhalla_tiles/`, `/custom_files/`
  - 物理机: `/home/admin/valhalla_traffic_poc_/valhalla_tiles/`
- `poc/valhalla/` 和 `poc/prime_server/` 各有自己的 `.git`，是独立 git 仓库
- `valhalla-project/` 本身不是 git 仓库
- 大文件 (`.osm.pbf`, `shapefile.zip` 等) 在 `.gitignore` 中排除

## Memory Context

相关 memory 文件位于 `~/.claude/projects/-home-admin/memory/`:
- `valhalla_realtime_project.md` — GCP 部署方案
- `valhalla_eta_pipeline_structure.md` — 测试用例结构和数据格式
- `valhalla_docs.md` — Valhalla 官方文档地址
- `heartbeat_data_format.md` — heartbeat CSV 格式详细说明
- `validated/docker_build_fixes.md` — 已验证的 Docker 构建修复方案

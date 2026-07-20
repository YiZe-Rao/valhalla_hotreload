# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Valhalla Traffic Project — 基于 Valhalla 路由引擎的实时交通数据处理系统，包含 2 个活跃模块:

| 模块 | 用途 | 技术栈 |
|------|------|--------|
| `pipeline/` | 5 阶段 ETA 交通数据处理流水线 | Python (Polars/Pandas), Docker |
| `realtime/` | 实时交通热加载扩展（修改 Valhalla GraphReader） | C++, Python, Bash |

辅助目录:
| 目录 | 用途 |
|------|------|
| `tests/` | 测试脚本和数据 (heartbeat CSV, Python/Bash 测试) |
| `scripts/` | 工具脚本 (从 heartbeat 生成 traffic.tar) |
| `tiles/` | 地图瓦片测试工作目录 (空) |
| `docs/` | 项目文档 (测试指南, 技术深读, 设计文档) |

> **注意**: `poc/` (Valhalla + Prime Server Docker 部署) 和 `backup/` (历史备份) 已移除。如需 POC 完整环境，参考设计文档 `docs/superpowers/`。
> **人工验证**: 容器中执行检查项请参考 `docs/HOT_RELOAD_VERIFICATION_CHECKLIST.md`。

## Build & Run

### Pipeline 模块 (pipeline/)

Pipeline 是双容器架构:
- **Container 1 (Valhalla)**: 在 port 8080 运行 `trace_attributes` map-matching 服务
- **Container 2 (Pipeline)**: 消费 Container 1 的 API，执行 5 阶段流水线

```bash
cd pipeline

# Container 1: Valhalla map-matching service
docker buildx build --platform linux/amd64 -t valhalla-local-test --load .
docker run -d -p 8080:8080 --name valhalla-test valhalla-local-test
docker cp valhalla-test:/custom_files/tiles/way_edges.txt ./traffic_pipeline/data/road_data/

# 注意: Pipeline Container 2 的 Dockerfile 在 pipeline/ 根目录
# 其构建逻辑引用 traffic_pipeline/ 子目录

# 测试 Valhalla API
curl -s http://localhost:8080/status | python3 -m json.tool
curl -s -X POST http://localhost:8080/route \
    -H "Content-Type: application/json" \
    -d '{"locations":[{"lat":22.2816,"lon":114.1585},{"lat":22.2988,"lon":114.1722}],"costing":"auto"}'
```

### Realtime 模块 (realtime/)

```bash
cd realtime
# 需要先有 valhalla_traffic_poc_ 基础项目在 /home/admin/ 下
./build.sh    # 注入热加载代码到 valhalla GraphReader，编译，部署 Python daemon
```

热加载机制的核心文件:
- `realtime/src/baldr/graphreader_hot_reload.{h,cc}` — GraphReader 热加载扩展 (shared_ptr 原子切换)
- `realtime/src/baldr/realtime_traffic_updater.{h,cc}` — 实时速度更新器 (时间衰减加权平均 + 双缓冲)
- `realtime/scripts/realtime_traffic_daemon.py` — Python 守护进程 (heartbeat CSV → edge 映射 → tar 生成)

### 运行测试

```bash
# 离线测试 (无需 Docker) — 全部可用 ✓
python3 tests/scripts/test_heartbeat_parse.py tests/data/heartbeat/heartbeat-2025-03-01.csv
python3 tests/scripts/heartbeat_to_edge_csv.py --heartbeat tests/data/heartbeat/heartbeat-2025-03-01.csv --max-records 5000 --offline
python3 tests/scripts/test_realtime_traffic_update.py --heartbeat tests/data/heartbeat/heartbeat-2025-03-01.csv --output /tmp/test_traffic.tar --sample 500

# Docker 测试 — 需要运行中的 valhalla_service
bash tests/scripts/valhalla_hotreload_test.sh        # 8 步骤完整验证
bash tests/scripts/validate_per_edge_injection.sh     # 离线 + 在线 4 阶段验证

# 在线转换 (需要 valhalla_service 在 8002 端口运行)
python3 tests/scripts/heartbeat_to_edge_csv.py \
    --heartbeat tests/data/heartbeat/heartbeat-2025-03-01.csv \
    --max-records 500 \
    --valhalla-url http://localhost:8002 \
    --output /tmp/edge_speeds.csv
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

### Pipeline 5 阶段

| 阶段 | 类 | 说明 |
|------|-----|------|
| Stage 1 | `DataCleanStage` | 清洗 GPS 轨迹和 trip 数据，过滤异常点 |
| Stage 2 | `MapMatchingStage` | 调用 Valhalla `/trace_attributes` 将 GPS 匹配到道路 edge |
| Stage 3 | `SpeedCalculationStage` | 从 map-matched 点计算每条 edge 的速度 |
| Stage 4 | `EmptySlotsFillingStage` | 填充无数据 edge 的速度（缺失值填充） |
| Stage 5 | `SpeedProfileGenerationStage` | 生成 Valhalla historical traffic 格式输出 |

核心类:
- `PipelineOrchestrator` (`pipeline/traffic_pipeline/traffic_pipeline/orchestrator.py`) — 协调所有阶段执行
- `PipelineConfig` / `DataNode` / `StageResult` (`pipeline/traffic_pipeline/traffic_pipeline/pipeline/base.py`) — 配置和数据流抽象
- `ValhallaClient` (`pipeline/traffic_pipeline/traffic_pipeline/clients/valhalla_client.py`) — 异步 HTTP 客户端调用 Valhalla API

### Realtime 热加载机制

双缓冲 traffic.tar 原子切换:

```
realtime_traffic_daemon.py
  ├── 读取 heartbeat CSV 流
  ├── GPS → edge_index 映射 (_map_to_edge_index)
  ├── 60s 滑动窗口时间衰减加权平均
  ├── 生成 next.tar.new → 原子 rename 为 standby.tar
  └── 生成 traffic.tar 后需重启 valhalla_service（或编译 HTTP handler 后用 /admin/reload_traffic）
```

Valhalla C++ 端 (`realtime/src/baldr/`): 修改 `GraphReader` 添加 `HotReloadTrafficArchive()` 方法，用 mutex 保护 `tile_extract_` 的原子替换。

**关键**: `valhalla_live_traffic --update-edges` 是离线工具，修改 traffic.tar 后 valhalla_service **不会自动感知**。

**当前可用方案**: 修改 traffic.tar 后**重启 valhalla_service**（`pkill valhalla_service && 重新启动`），服务启动时通过 mmap 加载新的 traffic.tar。此方法已验证有效。

**`/admin/reload_traffic` 端点状态**: `HotReloadTrafficArchive()` C++ 函数已存在于 graphreader.cc，但 HTTP handler 尚未注册到 prime_server action 分发链中。需要 3 处修改才能启用:
1. `options.proto` — 在 Action 枚举中添加 `reload_traffic = 13`
2. `loki_worker.h/cc` — 注册 action + 实现 dispatch handler
3. `valhalla.json` — 在 `loki.actions` 中添加 `"reload_traffic"`

详见 `docs/TECHNICAL_DEEP_DIVE.md` §8 和 `realtime/src/baldr/README.md`。

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
测试数据: `tests/data/heartbeat/heartbeat-2025-03-01.csv` (香港区域, 450MB, 2.8M 行, CRLF 换行)

## 关键 Valhalla 工具 (需在 Docker 容器内使用)

| 工具 | 用途 |
|------|------|
| `valhalla_build_tiles` | 从 OSM .pbf 生成路由 tiles |
| `valhalla_ways_to_edges` | 生成 OSM way ID → Valhalla edge ID 映射 (`way_edges.txt`) |
| `valhalla_add_predicted_traffic` | 将 predicted traffic CSV 嵌入 tiles |
| `valhalla_live_traffic` | 按边实时速度注入 (新增工具, 替代旧的 `valhalla_traffic_demo_utils`) |

## Valhalla API Endpoints

| 端点 | 用途 | 关键参数 |
|------|------|----------|
| `/route` | 时间相关路由 | `date_time` 参数启用 traffic |
| `/trace_attributes` | Map matching | GPS → edge IDs, matched points |
| `/locate` | 点→道路匹配 | `verbose=true` 返回 `predicted_speeds` 和 `live_speed` |
| `/isochrone` | 可达性区域 | 支持 traffic |
| `/admin/reload_traffic` | 热加载 traffic.tar | ⚠️ 需编译 HTTP handler（见下方注意事项） |

## 路径注意事项

- 容器内路径与宿主机路径不同:
  - Docker 容器: `/valhalla_tiles/`, `/custom_files/`
  - 宿主机: 因环境而异，常见 `/home/admin/valhalla_traffic_poc_/valhalla_tiles/`
- `pipeline/custom_files/valhalla.json` 中 `traffic_extract` 默认为 `/custom_files/traffic.tar`
- `realtime/build.sh` 依赖 `/home/admin/valhalla_traffic_poc_/` 存在

## Memory Context

相关 design docs 位于 `docs/superpowers/`:
- `specs/2026-06-28-live-traffic-per-edge-injection-design.md` — Per-edge 注入设计
- `plans/2026-06-28-live-traffic-per-edge-injection.md` — 实施计划

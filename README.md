# Valhalla Traffic Project

基于 Valhalla 路由引擎的实时交通数据处理项目集，包含 ETA 管线、实时交通热加载等组件。

## 目录结构

```
valhalla-project/
├── pipeline/             # ETA Pipeline 框架（5 阶段交通数据处理）
│   ├── traffic_pipeline/ # 核心管线代码
│   │   ├── stages/       # 阶段 1-5: 数据清洗 → 地图匹配 → 速度计算 → 空槽填充 → 速度剖面
│   │   ├── src/          # 工具模块（speed/filling/encoding/utils）
│   │   ├── pipeline/     # Pipeline 基础设施
│   │   └── clients/      # Valhalla 客户端
│   ├── custom_files/     # 自定义配置（valhalla.json, entrypoint.sh）
│   ├── traffic_data/     # traffic 数据工作目录
│   ├── docs/             # Pipeline 文档
│   └── Dockerfile        # Valhalla 服务容器构建
│
├── realtime/             # 实时交通热加载扩展
│   ├── src/              # C++ 源码（baldr: graphreader + traffic_updater）
│   ├── scripts/          # Python daemon（realtime_traffic_daemon.py）
│   ├── config/           # 配置文件模板
│   └── build.sh          # 构建脚本（注入代码到 Valhalla 源码树）
│
├── tests/                # 测试相关文件
│   ├── scripts/          # 测试脚本
│   │   ├── test_heartbeat_parse.py           # 解析 heartbeat 统计速度分布
│   │   ├── test_realtime_traffic_update.py   # 从 heartbeat 生成 traffic.tar
│   │   ├── test_hot_reload.sh                # 热更新测试 (容器内)
│   │   ├── valhalla_hotreload_test.sh        # 完整热重载 8 步骤验证
│   │   ├── validate_per_edge_injection.sh    # 按边注入 4 阶段验证
│   │   └── heartbeat_to_edge_csv.py          # heartbeat → edge CSV 转换器
│   └── data/heartbeat/   # 测试数据
│       └── heartbeat-2025-03-01.csv          # 香港区域 GPS 数据 (450MB)
│
├── scripts/              # 工具脚本
│   └── generate_traffic_from_heartbeat.py    # 从 heartbeat 生成 traffic.tar
│
├── tiles/                # 地图瓦片工作目录
├── docs/                 # 项目文档
│   ├── MANUAL_TESTING_GUIDE.md    # 人工测试流程
│   ├── TECHNICAL_DEEP_DIVE.md     # 技术细节深读
│   └── superpowers/               # 设计文档
│       ├── specs/   # Per-edge injection 设计
│       └── plans/   # 实施计划
│
└── CLAUDE.md            # Claude Code 项目指南
```

> **历史**: 项目最初包含 `poc/` (Valhalla + Prime Server 完整 Docker 部署) 和 `backup/` (备份)，现已移除。设计文档保留在 `docs/superpowers/`。

## 各模块说明

### pipeline/ — ETA 管线框架

5 阶段交通数据处理流水线：

1. **Stage 1 (DataCleanStage)**: GPS 轨迹清洗，过滤异常点
2. **Stage 2 (MapMatchingStage)**: 调用 Valhalla `/trace_attributes` 将 GPS 匹配到道路 edge
3. **Stage 3 (SpeedCalculationStage)**: 从 map-matched 点计算每条 edge 的速度
4. **Stage 4 (EmptySlotsFillingStage)**: 填充无数据 edge 的速度（缺失值填充）
5. **Stage 5 (SpeedProfileGenerationStage)**: 生成 Valhalla historical traffic 格式输出

双容器架构: Container 1 (Valhalla map-matching) + Container 2 (Pipeline)

详见 `pipeline/README.md`。

### realtime/ — 实时交通热加载

独立构建的实时交通服务扩展：
- C++ 端: 修改 Valhalla `GraphReader` 添加 `HotReloadTrafficArchive()`
- Python 端: `realtime_traffic_daemon.py` 守护进程
- 双缓冲机制: `traffic_active.tar` ↔ `traffic_standby.tar` 原子切换

详见 `realtime/README.md`。

### tests/ — 测试

三层测试模型:
- **Layer 1 (离线)**: CSV 解析、编码验证、tar 生成 — 无需 Docker
- **Layer 2 (Docker)**: API 端点、Pipeline 阶段 — 需要 Docker
- **Layer 3 (热重载)**: 速度注入 → 查询验证 → 稳定性 — 需要 Docker + 服务

详见 `tests/README.md` 和 `docs/MANUAL_TESTING_GUIDE.md`。

## 快速开始

### 离线测试 (无需 Docker, < 2 分钟)

```bash
# 验证数据格式和编码逻辑
python3 tests/scripts/heartbeat_to_edge_csv.py \
    --heartbeat tests/data/heartbeat/heartbeat-2025-03-01.csv \
    --max-records 5000 --offline

# 解析 heartbeat 速度统计
python3 tests/scripts/test_heartbeat_parse.py \
    tests/data/heartbeat/heartbeat-2025-03-01.csv 1000

# 生成 traffic.tar (demo 模式)
python3 tests/scripts/test_realtime_traffic_update.py \
    --heartbeat tests/data/heartbeat/heartbeat-2025-03-01.csv \
    --output /tmp/test_traffic.tar --demo
```

### 完整集成测试 (需要 Docker)

详见 `docs/MANUAL_TESTING_GUIDE.md`。

## 技术要点

### 数据格式

- **TrafficSpeed**: 64-bit bitfield — speed(7bit) × 4 + breakpoint(8bit) × 2 + congestion(6bit) × 3
- **GraphId**: 64-bit — `value = level | (tile_index << 3) | (edge_id << 25)`
- **速度编码**: 2kph 分辨率，UNKNOWN_TRAFFIC_SPEED_RAW = 127

### 热重载

- `shared_ptr<midgard::tar>` 原子赋值 → 新请求用新数据，旧请求继续用旧数据
- `valhalla_live_traffic --update-edges` 是离线工具 → 修改后必须重启或调用 `/admin/reload_traffic`

详见 `docs/TECHNICAL_DEEP_DIVE.md`。

## 相关链接

- [Valhalla 官方文档](https://valhalla.readthedocs.io/)
- 测试数据: `tests/data/heartbeat/heartbeat-2025-03-01.csv` (香港区域, 450MB, 2.8M 行)

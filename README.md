# Valhalla Traffic Project

基于 Valhalla 路由引擎的实时交通数据处理项目集，包含 POC 验证、ETA 管线、实时服务等多个组件。

## 目录结构

```
valhalla-project/
├── poc/                  # 主 POC 项目（活跃开发中）
│   ├── valhalla/         # Valhalla 源码（forked + custom patches）
│   ├── prime_server/     # Prime Server 源码（Valhalla 的 companion server）
│   ├── valhalla_tiles/   # 预构建地图瓦片（hong-kong）
│   ├── valhalla_code_overwrites/  # Valhalla 自定义修改代码
│   ├── Dockerfile        # Docker 构建配置
│   ├── docker-compose.yml
│   ├── build.sh          # 构建脚本
│   ├── realtime_traffic_daemon.py  # 实时交通数据守护进程
│   ├── run_service.sh
│   └── run_realtime_service.sh
│
├── pipeline/             # ETA Pipeline 框架（5 阶段交通数据处理）
│   ├── traffic_pipeline/ # 核心管线代码
│   │   ├── stages/       # 阶段 1-5: 数据清洗 → 地图匹配 → 速度计算 → 空槽填充 → 速度剖面
│   │   ├── src/          # 工具模块（speed/filling/encoding/utils）
│   │   ├── pipeline/     # Pipeline 基础设施
│   │   └── clients/      # Valhalla 客户端
│   ├── custom_files/     # 自定义配置（valhalla.json, entrypoint.sh）
│   ├── traffic_data/     # 原始交通数据
│   ├── docs/             # Pipeline 文档
│   └── Dockerfile
│
├── realtime/             # 实时交通服务（独立构建版本）
│   ├── src/              # 源码（baldr/thor/mjolnir）
│   ├── config/           # 配置文件
│   ├── scripts/          # realtime_traffic_daemon.py
│   └── build.sh          # 独立构建脚本
│
├── backup/               # POC 项目备份
│   ├── (同 poc/ 结构)
├── tiles/                # 地图瓦片测试目录
├── scripts/              # 工具脚本
│   ├── generate_traffic_from_heartbeat.py  # 从 heartbeat 生成 traffic.tar
│   └── README.md
└── tests/                # 测试相关文件
    ├── scripts/          # 测试脚本
    │   ├── test_heartbeat_parse.py
    │   ├── test_realtime_traffic_update.py
    │   ├── test_hot_reload.sh
    │   └── valhalla_hotreload_test.sh
    └── data/heartbeat/   # 测试数据
        └── heartbeat-2025-03-01.csv
```

## 各模块说明

### poc/ — 主概念验证项目

包含完整的 Valhalla + Prime Server Docker 部署方案：
- 基于 Docker 构建部署
- 集成实时交通数据更新
- 支持热重载（hot-reload）

**关键文件**:
- `valhalla_code_overwrites/src/mjolnir/valhalla_traffic_demo_utils.cc` — 交通数据注入自定义代码
- `realtime_traffic_daemon.py` — 从 heartbeat 数据源读取并更新到 Valhalla

### pipeline/ — ETA 管线框架

5 阶段交通数据处理流水线：
1. **Stage 1**: 数据清洗
2. **Stage 2**: 地图匹配
3. **Stage 3**: 速度计算
4. **Stage 4**: 空槽填充
5. **Stage 5**: 速度剖面生成

### realtime/ — 实时交通服务

独立构建的实时交通服务代码，包含 Valhalla 源码修改（baldr/thor/mjolnir 模块）。

### backup/ — 备份

POC 项目的备份副本，保留用于恢复。

### tests/ — 测试

Heartbeat 数据解析、交通数据生成和热重载相关测试。详见 `tests/README.md`。

### scripts/ — 工具脚本

数据处理和生成工具。详见 `scripts/README.md`。

## 快速开始

```bash
# 进入 POC 项目
cd poc

# 构建 Docker 镜像
./build.sh

# 启动服务
./run_service.sh

# 启动实时交通服务
./run_realtime_service.sh
```

## 相关项目

- [Valhalla 官方文档](https://valhalla.readthedocs.io/)
- 测试数据: `tests/data/heartbeat/heartbeat-2025-03-01.csv`

## 注意事项

- `backup/` 和 `poc/` 结构相同，`backup/` 为历史备份，日常开发使用 `poc/`
- `valhalla/` 和 `prime_server/` 子目录包含各自的 `.git`，为独立 git 仓库（submodule）
- 大文件（osm.pbf、shapefile 等）请勿提交到 git

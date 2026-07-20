# Valhalla Live Traffic 按边实时速度注入 设计文档

**日期**: 2026-06-28
**状态**: 已确认

---

## 1. 背景与问题

### 1.1 当前状态

`valhalla_traffic_demo_utils.cc` 是 Valhalla traffic POC 项目中的自定义工具，负责生成和管理 Live Traffic 数据（`traffic.tar`）。它有两个核心函数：

- `build_live_traffic_data()` — 新建 `traffic.tar`，一个 tile 内**所有边**填入**同一个常量速度**
- `customize_live_traffic_data()` — 打开已有 `traffic.tar`，**所有 tile 的所有边**覆盖为**同一个常量速度**

### 1.2 问题

无法**按边分别设定不同的实时速度**。当前工具只能填入无效数据（所有边相同速度），无法实现每条街道独立的实时速度注入。

### 1.3 目标

扩展该工具，支持 per-edge 实时速度写入，同时：
- 零修改 Valhalla 核心（`graphtile.h`、`GetSpeed()`、`GraphReader` 加载逻辑）
- 完全复用原生 `TrafficTile` / `TrafficSpeed` 格式（`traffictile.h`）
- 保持现有 CLI 命令向后兼容
- 可被外部脚本/程序调用

---

## 2. 设计方案

### 2.1 分层架构

```
CLI 层:  valhalla_live_traffic.cc  (参数解析、CSV 解析、格式化输出)
         │
         ▼ 调用
库 层:   src/mjolnir/live_traffic_utils.{h,cc}
         (encode_live_speed、update_edge_live_speeds、build_live_traffic_from_edges)
         │
         ▼ 使用
原生层:  traffictile.h (TrafficSpeed, TrafficTileHeader)
         graphreader.h (GraphReader, GraphTile)
         midgard/tar.h (tar 文件读写)
```

### 2.2 文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `valhalla/src/mjolnir/live_traffic_utils.h` | 新增 | 库接口声明 |
| `valhalla/src/mjolnir/live_traffic_utils.cc` | 新增 | 库实现 |
| `valhalla_code_overwrites/src/mjolnir/valhalla_live_traffic.cc` | 重命名自 `valhalla_traffic_demo_utils.cc` + 修改 | 追加 per-edge CLI 命令 |
| `valhalla_code_overwrites/CMakeLists.txt` | 修改 | 工具名: `valhalla_traffic_demo_utils` → `valhalla_live_traffic` |
| `valhalla_code_overwrites/src/CMakeLists.txt` | 修改 | 添加 `live_traffic_utils.cc` 到 valhalla target |
| `poc/build.sh` | 修改 | 文件复制路径更新 |
| `poc/Dockerfile` | 修改 | COPY 路径更新 |

**零修改文件**: `graphtile.h`, `directededge.h`, `graphreader.h`, `graphreader.cc`, `traffictile.h`, 所有 costing 代码

### 2.3 库接口

```cpp
// live_traffic_utils.h
namespace valhalla::mjolnir {

using EdgeSpeedMap =
    std::unordered_map<uint64_t,
      std::vector<std::tuple<uint32_t, float, uint8_t>>>;
//                     edge_index  speed  congestion

// 编码 speed_kph → TrafficSpeed bitfield
baldr::TrafficSpeed encode_live_speed(float speed_kph, uint8_t congestion = 1);

// 按边原地更新已有 traffic.tar (mmap)
uint32_t update_edge_live_speeds(const boost::property_tree::ptree& mjolnir_pt,
                                 const EdgeSpeedMap& speed_map,
                                 uint64_t timestamp);

// 新建 traffic.tar，仅填充指定边（其余边 = UNKNOWN）
uint32_t build_live_traffic_from_edges(const boost::property_tree::ptree& mjolnir_pt,
                                       const EdgeSpeedMap& speed_map,
                                       uint64_t timestamp);
}
```

### 2.4 CLI 接口

```bash
# 保留：所有现有命令不变

# 新增：从 CSV 文件按边更新
valhalla_live_traffic --config valhalla.json \
    --update-edges /path/to/edge_speeds.csv

# 新增：命令行直接指定（可重复多次）
valhalla_live_traffic --config valhalla.json \
    --set-edge-speed 0/3381/0,15,45.5,1 \
    --set-edge-speed 0/3381/0,23,12.0,31
```

### 2.5 CSV 格式

```csv
# tile_id, edge_index, speed_kph, [congestion]
# tile_id = GraphId 格式 "level/tile_index/id"
0/3381/0,15,45.5,1
0/3381/0,23,12.0,31
1/47701/0,5,60.0,1
```

- `congestion` 默认 1，范围 1-63 (UNKNOWN_CONGESTION_VAL=0 表示未知，1-63 表示拥堵程度从低到高)
- 空行和 `#` 开头的注释行被忽略

---

## 3. 实现要点

### 3.1 `encode_live_speed()` — TrafficSpeed 编码

- 速度编码：`raw = speed_kph / 2`（2kph 分辨率，与 `traffictile.h` 定义一致）
- 上限检查：`raw >= UNKNOWN_TRAFFIC_SPEED_RAW` 时 clamp
- breakpoint1/2 默认 255 → 整条边只有一个速度段
- 复用 `TrafficSpeed` 构造函数，编译期验证与 `traffictile.h` 的 `static_assert(sizeof(TrafficSpeed)==8)` 一致

### 3.2 `update_edge_live_speeds()` — mmap 原地更新

1. 从 `mjolnir_pt` 读取 `traffic_extract` 路径
2. `mmap` 整个文件 (`PROT_READ | PROT_WRITE, MAP_SHARED`)
3. 用 `mtar` 遍历每个 entry
4. 从文件名解析 `tile_id` (`GraphTile::GetTileId`)
5. 只处理 `speed_map` 中存在的 tile
6. 更新 `TrafficTileHeader::last_update`
7. 逐边更新 `TrafficSpeed` bitfield（其余边保持原值）
8. `msync` 写回磁盘

### 3.3 `build_live_traffic_from_edges()` — 新建 tar + 选择性填充

1. 构造 `GraphReader(mjolnir_pt)`
2. 用 `mtar_open "w"` 创建新 tar
3. 遍历 `speed_map` 中每个 tile_id
4. 通过 `reader.GetGraphTile()` 获取 `directededgecount()`
5. 构建 tile 二进制：`TrafficTileHeader` + N × `TrafficSpeed`
6. `speed_map` 中有数据的边 → 写入编码后的速度
7. 无数据的边 → 写入 `INVALID_SPEED` (`breakpoint=0` → `speed_valid()=false`)
8. `mtar_finalize` + `mtar_close`

### 3.4 线程安全

- `update_edge_live_speeds()` 是**离线工具**，应在 valhalla_service 未运行时调用，或调用后使用 `HotReloadTrafficArchive()` 触发热加载
- `encode_live_speed()` 纯函数，线程安全

---

## 4. 使用流程

```bash
# 1. 构建
cd poc && ./build.sh

# 2. 启动 valhalla_service
./run_service.sh

# 3. 有实时速度数据后，按边更新
valhalla_live_traffic --config valhalla_tiles/valhalla.json \
    --update-edges /tmp/edge_speeds.csv

# 4. 重启 valhalla_service 使新数据生效
# (或编译 HTTP handler 后使用 /admin/reload_traffic 端点)
pkill valhalla_service; sleep 1
LD_LIBRARY_PATH=/usr/local/lib valhalla_service /valhalla_tiles/valhalla.json 1 &
# curl -X POST http://localhost:8002/admin/reload_traffic \
#     -H "Content-Type: application/json" \
#     -d '{"traffic_path":"/valhalla_tiles/traffic.tar"}'

# 5. 验证
curl http://localhost:8002/locate \
    --data '{"locations":[{"lat":22.28,"lon":114.16}],"verbose":true}' | jq '.[0].edges[0].live_speed'
```

---

## 5. 边界条件

- **边索引越界**: `edge_index >= header->directed_edge_count` 时跳过并警告
- **空 speed_map**: `update_edge_live_speeds()` 返回 0，不修改文件
- **已有 tar 不存在**: `update_edge_live_speeds()` 报错退出（不自动创建）
- **speed_kph = 0**: 编码后 `overall_encoded_speed = 0` → `closed()` 返回 true → 路由中避开该边
- **Congestion 值域**: 自动 clamp 到 `[0, 63]`，0 表示未知拥堵

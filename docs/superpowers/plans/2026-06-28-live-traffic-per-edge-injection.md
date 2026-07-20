# Live Traffic Per-Edge Speed Injection 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 扩展 `valhalla_traffic_demo_utils` 为 `valhalla_live_traffic`，支持按边独立设定实时速度（替代原有"全 tile 统一速度"的限制）。

**Architecture:** 库层 (`live_traffic_utils.{h,cc}`) 提供 `encode_live_speed` / `update_edge_live_speeds` / `build_live_traffic_from_edges` 三个可复用函数。CLI 层 (`valhalla_live_traffic.cc`) 在保留所有现有命令的基础上，追加 `--update-edges` 和 `--set-edge-speed` 两个新命令。

**Tech Stack:** C++14, Valhalla baldr/mjolnir, microtar, mmap, cxxopts

## Global Constraints

- 零修改 Valhalla 核心文件: `graphtile.h`, `graphreader.h`, `traffictile.h`, `directededge.h`, `graphreader.cc`, `GetSpeed()`, 所有 costing 代码
- 保持现有 CLI 命令 (`--get-traffic-dir`, `--get-tile-id`, `--generate-predicted-traffic`, `--generate-live-traffic`, `--update-live-traffic`) 行为完全不变
- 库文件放入 `valhalla_code_overwrites/src/mjolnir/`，通过 `build.sh` 复制到 valhalla 源码树
- 编译通过 `valhalla_code_overwrites/src/CMakeLists.txt` 管控

---

### Task 1: 创建库头文件 `live_traffic_utils.h`

**Files:**
- Create: `valhalla-project/poc/valhalla_code_overwrites/src/mjolnir/live_traffic_utils.h`

**Interfaces:**
- Produces: `encode_live_speed(float, uint8_t) → TrafficSpeed`, `update_edge_live_speeds(ptree, EdgeSpeedMap, uint64_t) → uint32_t`, `build_live_traffic_from_edges(ptree, EdgeSpeedMap, uint64_t) → uint32_t`

- [ ] **Step 1: 创建头文件**

```cpp
#ifndef VALHALLA_MJOLNIR_LIVE_TRAFFIC_UTILS_H_
#define VALHALLA_MJOLNIR_LIVE_TRAFFIC_UTILS_H_

#include <cstdint>
#include <tuple>
#include <unordered_map>
#include <vector>
#include <valhalla/baldr/traffictile.h>
#include <boost/property_tree/ptree.hpp>

namespace valhalla {
namespace mjolnir {

// tile_id → [(edge_index, speed_kph, congestion)]
using EdgeSpeedMap =
    std::unordered_map<uint64_t,
                       std::vector<std::tuple<uint32_t, float, uint8_t>>>;

/**
 * Encode (speed_kph, congestion) into a TrafficSpeed bitfield.
 * Uses the Traffictile.h bit layout: 2kph resolution, 7-bit encoded speed.
 *
 * @param speed_kph   Speed in km/h. Clamped to [0, 254].
 * @param congestion  Congestion level 1–63. 0 = unknown. Default 1 (no congestion).
 * @return TrafficSpeed with breakpoint1=255, breakpoint2=255 (full edge coverage).
 */
baldr::TrafficSpeed encode_live_speed(float speed_kph, uint8_t congestion = 1);

/**
 * Update specific edges in an existing traffic.tar via mmap in-place editing.
 * Non-specified edges retain their existing values.
 *
 * @param mjolnir_pt   The "mjolnir" subtree from valhalla.json.
 * @param speed_map    tile_id → list of (edge_index, speed_kph, congestion).
 * @param timestamp    Update timestamp (epoch seconds), written to header.last_update.
 * @return Number of edges actually updated.
 */
uint32_t update_edge_live_speeds(const boost::property_tree::ptree& mjolnir_pt,
                                 const EdgeSpeedMap& speed_map,
                                 uint64_t timestamp);

/**
 * Create a new traffic.tar, populating only edges specified in speed_map.
 * Unspecified edges are set to INVALID_SPEED (breakpoint=0 → speed_valid()=false).
 * Only tiles with data in speed_map are written to the tar.
 *
 * @param mjolnir_pt   The "mjolnir" subtree from valhalla.json.
 * @param speed_map    tile_id → list of (edge_index, speed_kph, congestion).
 * @param timestamp    Creation timestamp (epoch seconds).
 * @return Number of edges actually filled with valid speeds.
 */
uint32_t build_live_traffic_from_edges(const boost::property_tree::ptree& mjolnir_pt,
                                       const EdgeSpeedMap& speed_map,
                                       uint64_t timestamp);

} // namespace mjolnir
} // namespace valhalla

#endif
```

- [ ] **Step 2: Commit**

```bash
cd /home/admin/valhalla-project && git add poc/valhalla_code_overwrites/src/mjolnir/live_traffic_utils.h && git commit -m "feat: add live_traffic_utils.h library header"
```

---

### Task 2: 创建库实现文件 `live_traffic_utils.cc`

**Files:**
- Create: `valhalla-project/poc/valhalla_code_overwrites/src/mjolnir/live_traffic_utils.cc`

**Interfaces:**
- Consumes: `encode_live_speed` (from Task 1), `TrafficSpeed`, `TrafficTileHeader`, `TrafficTile` (from `traffictile.h`), `GraphReader`, `GraphTile` (from `graphreader.h`), `mtar_t` (from microtar)
- Produces: `update_edge_live_speeds`, `build_live_traffic_from_edges`

- [ ] **Step 1: 实现 `encode_live_speed()`**

```cpp
#include "live_traffic_utils.h"
#include "baldr/graphreader.h"
#include "config.h"
#include "filesystem.h"
#include "microtar.h"
#include "mjolnir/graphtilebuilder.h"
#include "rapidjson/document.h"

#include <algorithm>
#include <cstring>
#include <fstream>
#include <sys/mman.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>

namespace valhalla {
namespace mjolnir {

baldr::TrafficSpeed encode_live_speed(float speed_kph, uint8_t congestion) {
    uint32_t raw = static_cast<uint32_t>(speed_kph / 2.0f);
    if (raw > baldr::UNKNOWN_TRAFFIC_SPEED_RAW - 1) {
        raw = baldr::UNKNOWN_TRAFFIC_SPEED_RAW - 1;
    }
    // Clamp congestion to valid range [0, 63]
    if (congestion > baldr::MAX_CONGESTION_VAL) {
        congestion = baldr::MAX_CONGESTION_VAL;
    }

    return baldr::TrafficSpeed{
        raw,                       // overall_encoded_speed
        raw, raw, raw,             // encoded_speed1/2/3
        255,                       // breakpoint1
        255,                       // breakpoint2
        congestion, congestion, congestion,  // congestion1/2/3
        0                          // has_incidents
    };
}
```

- [ ] **Step 2: 实现 `update_edge_live_speeds()`**

```cpp
// MMap helper — replicates the pattern from existing valhalla_traffic_demo_utils.cc
struct MMap {
    MMap(const char* filename) {
        fd = open(filename, O_RDWR);
        if (fd < 0) throw std::runtime_error("Cannot open " + std::string(filename));
        struct stat s;
        fstat(fd, &s);
        data = mmap(0, s.st_size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
        if (data == MAP_FAILED) throw std::runtime_error("mmap failed");
        length = s.st_size;
    }
    ~MMap() { munmap(data, length); close(fd); }
    int fd;
    void* data;
    size_t length;
};

uint32_t update_edge_live_speeds(const boost::property_tree::ptree& mjolnir_pt,
                                 const EdgeSpeedMap& speed_map,
                                 uint64_t timestamp) {
    std::string traffic_path = mjolnir_pt.get<std::string>("traffic_extract");
    if (!filesystem::exists(traffic_path)) {
        throw std::runtime_error("traffic.tar not found: " + traffic_path);
    }

    auto memory = std::make_shared<MMap>(traffic_path.c_str());

    // Setup microtar read/write callbacks on mmap'd memory
    mtar_t tar;
    std::memset(&tar, 0, sizeof(tar));
    tar.pos = 0;
    tar.stream = memory->data;
    tar.read = [](mtar_t* t, void* buf, unsigned sz) -> int {
        std::memcpy(buf, reinterpret_cast<char*>(t->stream) + t->pos, sz);
        return MTAR_ESUCCESS;
    };
    tar.write = [](mtar_t* t, const void* buf, unsigned sz) -> int {
        std::memcpy(reinterpret_cast<char*>(t->stream) + t->pos, buf, sz);
        return MTAR_ESUCCESS;
    };
    tar.seek = [](mtar_t*, unsigned) -> int { return MTAR_ESUCCESS; };
    tar.close = [](mtar_t*) -> int { return MTAR_ESUCCESS; };

    uint32_t updated_count = 0;
    mtar_header_t tar_header;

    while ((mtar_read_header(&tar, &tar_header)) != MTAR_ENULLRECORD) {
        // Parse tile_id from filename (e.g. "0/003/015.gph")
        try {
            auto tile_id = baldr::GraphTile::GetTileId(tar_header.name);

            auto speed_it = speed_map.find(tile_id);
            if (speed_it == speed_map.end()) {
                mtar_next(&tar);
                continue;  // tile not in update list
            }

            // Construct TrafficTile pointing into mmap region
            char* tile_data = reinterpret_cast<char*>(tar.stream) + tar.pos;
            baldr::TrafficTile tile(
                std::make_unique<baldr::GraphMemory>(tile_data, tar_header.size,
                    [](const char*) { /* no-op deleter: MMap owns memory */ }));

            // Update header timestamp
            const_cast<volatile baldr::TrafficTileHeader*>(tile.header)->last_update = timestamp;

            // Update specified edges
            for (const auto& [edge_idx, speed_kph, congestion] : speed_it->second) {
                if (edge_idx >= tile.header->directed_edge_count) {
                    LOG_WARN("Edge index " + std::to_string(edge_idx) +
                             " out of bounds for tile " + std::to_string(tile_id) +
                             " (max " + std::to_string(tile.header->directed_edge_count) + ")");
                    continue;
                }
                auto* current = const_cast<baldr::TrafficSpeed*>(&tile.speeds[edge_idx]);
                *current = encode_live_speed(speed_kph, congestion);
                updated_count++;
            }
        } catch (...) {
            // skip non-tile entries
        }
        mtar_next(&tar);
    }

    msync(memory->data, memory->length, MS_SYNC);
    return updated_count;
}
```

- [ ] **Step 3: 实现 `build_live_traffic_from_edges()`**

```cpp
uint32_t build_live_traffic_from_edges(const boost::property_tree::ptree& mjolnir_pt,
                                       const EdgeSpeedMap& speed_map,
                                       uint64_t timestamp) {
    std::string tile_dir = mjolnir_pt.get<std::string>("tile_dir");
    std::string traffic_path = mjolnir_pt.get<std::string>("traffic_extract");

    // Ensure parent directory exists
    filesystem::path parent = filesystem::path(traffic_path).parent_path();
    if (!filesystem::exists(parent)) {
        throw std::runtime_error("Traffic extract directory does not exist: " + parent.string());
    }

    baldr::GraphReader reader(mjolnir_pt);
    uint32_t filled_count = 0;

    mtar_t tar;
    auto open_result = mtar_open(&tar, traffic_path.c_str(), "w");
    if (open_result != MTAR_ESUCCESS) {
        throw std::runtime_error("Could not create traffic tar: " + traffic_path);
    }

    // Walk each tile in speed_map
    for (const auto& [tile_id_raw, edge_speeds] : speed_map) {
        baldr::GraphId tile_graph_id(tile_id_raw);
        auto tile = reader.GetGraphTile(tile_graph_id);
        if (!tile) {
            LOG_WARN("Tile not found: " + std::to_string(tile_id_raw));
            continue;
        }

        uint32_t edge_count = tile->header()->directededgecount();

        // Build tile binary: header + speed array
        std::stringstream buffer;
        baldr::TrafficTileHeader header = {};
        header.tile_id = tile_graph_id.Tile_Base().value;
        header.last_update = timestamp;
        header.traffic_tile_version = baldr::TRAFFIC_TILE_VERSION;
        header.directed_edge_count = edge_count;
        buffer.write(reinterpret_cast<const char*>(&header), sizeof(header));

        // Build a lookup for fast access during the loop
        std::unordered_map<uint32_t, std::pair<float, uint8_t>> edge_lookup;
        for (const auto& [idx, spd, cong] : edge_speeds) {
            edge_lookup[idx] = {spd, cong};
        }

        for (uint32_t i = 0; i < edge_count; ++i) {
            auto it = edge_lookup.find(i);
            if (it != edge_lookup.end()) {
                auto ts = encode_live_speed(it->second.first, it->second.second);
                buffer.write(reinterpret_cast<const char*>(&ts), sizeof(ts));
                filled_count++;
            } else {
                // INVALID_SPEED: breakpoint1=0 → speed_valid() returns false
                baldr::TrafficSpeed invalid = {
                    baldr::UNKNOWN_TRAFFIC_SPEED_RAW,
                    baldr::UNKNOWN_TRAFFIC_SPEED_RAW,
                    baldr::UNKNOWN_TRAFFIC_SPEED_RAW,
                    baldr::UNKNOWN_TRAFFIC_SPEED_RAW,
                    0, 0, 0, 0, 0, 0
                };
                buffer.write(reinterpret_cast<const char*>(&invalid), sizeof(invalid));
            }
        }

        // Padding (matches existing build_live_traffic_data pattern)
        uint32_t dummy = 0;
        buffer.write(reinterpret_cast<const char*>(&dummy), sizeof(dummy));
        buffer.write(reinterpret_cast<const char*>(&dummy), sizeof(dummy));

        std::string tile_data = buffer.str();
        std::string filename = baldr::GraphTile::FileSuffix(tile_graph_id);

        if (mtar_write_file_header(&tar, filename.c_str(), tile_data.size()) != MTAR_ESUCCESS) {
            mtar_close(&tar);
            throw std::runtime_error("Could not write tar header for " + filename);
        }
        if (mtar_write_data(&tar, tile_data.c_str(), tile_data.size()) != MTAR_ESUCCESS) {
            mtar_close(&tar);
            throw std::runtime_error("Could not write tar data for " + filename);
        }
    }

    mtar_finalize(&tar);
    mtar_close(&tar);
    return filled_count;
}

} // namespace mjolnir
} // namespace valhalla
```

- [ ] **Step 4: Commit**

```bash
cd /home/admin/valhalla-project && git add poc/valhalla_code_overwrites/src/mjolnir/live_traffic_utils.cc && git commit -m "feat: add live_traffic_utils.cc library implementation"
```

---

### Task 3: 重命名 + 扩展 CLI 工具 `valhalla_live_traffic.cc`

**Files:**
- Rename: `valhalla-project/poc/valhalla_code_overwrites/src/mjolnir/valhalla_traffic_demo_utils.cc` → `valhalla_live_traffic.cc`
- Modify: (same file) — 追加新命令

**Interfaces:**
- Consumes: `encode_live_speed`, `update_edge_live_speeds`, `build_live_traffic_from_edges` (from `live_traffic_utils.h`)

- [ ] **Step 1: 重命名文件**

```bash
cd /home/admin/valhalla-project
mv poc/valhalla_code_overwrites/src/mjolnir/valhalla_traffic_demo_utils.cc \
   poc/valhalla_code_overwrites/src/mjolnir/valhalla_live_traffic.cc
```

- [ ] **Step 2: 在文件末尾（`main` 函数之前）追加 CSV 解析函数和新 CLI handler**

在 `handle_update_live_traffic` 函数之后、`main` 函数之前插入：

```cpp
#include "live_traffic_utils.h"

// --- CSV parsing (CLI-layer utility) ---

static valhalla::mjolnir::EdgeSpeedMap parse_edge_speeds_csv(const std::string& csv_path) {
    valhalla::mjolnir::EdgeSpeedMap result;
    std::ifstream f(csv_path);
    if (!f.is_open()) {
        throw std::runtime_error("Cannot open CSV file: " + csv_path);
    }

    std::string line;
    size_t line_no = 0;
    while (std::getline(f, line)) {
        line_no++;
        if (line.empty() || line[0] == '#') continue;

        // Parse: tile_id_str, edge_index, speed_kph [, congestion]
        // tile_id_str format: "0/3381/0" or "level/tile_index/id"
        std::vector<std::string> parts;
        std::stringstream ss(line);
        std::string part;
        while (std::getline(ss, part, ',')) {
            parts.push_back(part);
        }

        if (parts.size() < 3) {
            std::cerr << "Warning: skipping line " << line_no
                      << " (expected >=3 fields, got " << parts.size() << ")" << std::endl;
            continue;
        }

        try {
            // Parse tile_id: supports GraphId format "level/tile/id" or raw uint64
            uint64_t tile_id;
            if (parts[0].find('/') != std::string::npos) {
                size_t s1 = parts[0].find('/');
                size_t s2 = parts[0].find('/', s1 + 1);
                uint32_t lvl = static_cast<uint32_t>(std::stoul(parts[0].substr(0, s1)));
                uint32_t tile = static_cast<uint32_t>(std::stoul(parts[0].substr(s1 + 1, s2 - s1 - 1)));
                uint32_t id = static_cast<uint32_t>(std::stoul(parts[0].substr(s2 + 1)));
                tile_id = valhalla::baldr::GraphId(lvl, tile, id).tileid();
            } else {
                tile_id = static_cast<uint64_t>(std::stoull(parts[0]));
            }
            uint32_t edge_idx = static_cast<uint32_t>(std::stoul(parts[1]));
            float speed_kph = std::stof(parts[2]);
            uint8_t congestion = (parts.size() >= 4) ?
                static_cast<uint8_t>(std::stoul(parts[3])) : 1;

            result[tile_id].emplace_back(edge_idx, speed_kph, congestion);
        } catch (const std::exception& e) {
            std::cerr << "Warning: skipping line " << line_no
                      << " (parse error: " << e.what() << ")" << std::endl;
        }
    }
    return result;
}

// --- New CLI handlers ---

static int handle_update_edges(const std::string& csv_path,
                               const boost::property_tree::ptree& pt) {
    auto speed_map = parse_edge_speeds_csv(csv_path);
    if (speed_map.empty()) {
        std::cerr << "No valid edge speeds found in " << csv_path << std::endl;
        return EXIT_FAILURE;
    }

    uint64_t now = static_cast<uint64_t>(time(nullptr));
    uint32_t count = valhalla::mjolnir::update_edge_live_speeds(
        pt.get_child("mjolnir"), speed_map, now);

    std::cout << "Updated " << count << " edges in "
              << pt.get<std::string>("mjolnir.traffic_extract") << std::endl;
    return EXIT_SUCCESS;
}

static int handle_set_edge_speed(const std::vector<std::string>& specs,
                                 const boost::property_tree::ptree& pt) {
    valhalla::mjolnir::EdgeSpeedMap speed_map;

    for (const auto& spec : specs) {
        // Parse: tile_id,edge_idx,speed_kph[,congestion]
        // tile_id format: GraphId "level/tile/id" or raw uint64
        std::vector<std::string> parts;
        std::stringstream ss(spec);
        std::string part;
        while (std::getline(ss, part, ',')) {
            parts.push_back(part);
        }

        if (parts.size() < 3) {
            std::cerr << "Error: --set-edge-speed requires at least "
                      << "tile_id,edge_idx,speed_kph (got " << parts.size() << ")" << std::endl;
            return EXIT_FAILURE;
        }

        try {
            uint64_t tile_id;
            if (parts[0].find('/') != std::string::npos) {
                size_t s1 = parts[0].find('/');
                size_t s2 = parts[0].find('/', s1 + 1);
                uint32_t lvl = static_cast<uint32_t>(std::stoul(parts[0].substr(0, s1)));
                uint32_t tile = static_cast<uint32_t>(std::stoul(parts[0].substr(s1 + 1, s2 - s1 - 1)));
                uint32_t id = static_cast<uint32_t>(std::stoul(parts[0].substr(s2 + 1)));
                tile_id = valhalla::baldr::GraphId(lvl, tile, id).tileid();
            } else {
                tile_id = static_cast<uint64_t>(std::stoull(parts[0]));
            }
            uint32_t edge_idx = static_cast<uint32_t>(std::stoul(parts[1]));
            float speed_kph = std::stof(parts[2]);
            uint8_t congestion = (parts.size() >= 4) ?
                static_cast<uint8_t>(std::stoul(parts[3])) : 1;

            speed_map[tile_id].emplace_back(edge_idx, speed_kph, congestion);
        } catch (const std::exception& e) {
            std::cerr << "Error parsing spec '" << spec << "': " << e.what() << std::endl;
            return EXIT_FAILURE;
        }
    }

    uint64_t now = static_cast<uint64_t>(time(nullptr));
    uint32_t count = valhalla::mjolnir::update_edge_live_speeds(
        pt.get_child("mjolnir"), speed_map, now);

    std::cout << "Updated " << count << " edges in "
              << pt.get<std::string>("mjolnir.traffic_extract") << std::endl;
    return EXIT_SUCCESS;
}
```

- [ ] **Step 3: 修改 `main()` 函数，追加新选项和分发逻辑**

在 `main()` 函数内，现有 `options.add_options()` 块末尾追加两个选项：

```cpp
// 在现有 options 定义末尾（"--update-live-traffic" 之后）追加:
        ("update-edges",
         "Update specific edges in an existing traffic.tar from a CSV file. "
         "CSV format: tile_id,edge_index,speed_kph[,congestion]",
         cxxopts::value<std::string>())
        ("set-edge-speed",
         "Set speed for a specific edge. "
         "Format: tile_id,edge_idx,speed_kph[,congestion]. "
         "Can be specified multiple times.",
         cxxopts::value<std::vector<std::string>>());
```

在 `main()` 函数内，现有 `if (result.count("update-live-traffic"))` 分支之后追加：

```cpp
    std::string csv_path;
    std::vector<std::string> edge_specs;

    if (result.count("update-edges")) {
        csv_path = result["update-edges"].as<std::string>();
        // Load config
        boost::property_tree::ptree pt;
        if (result.count("config") && filesystem::is_regular_file(config_file_path)) {
            rapidjson::read_json(config_file_path, pt);
        } else {
            std::cerr << "Configuration is required for --update-edges" << std::endl;
            return EXIT_FAILURE;
        }
        return handle_update_edges(csv_path, pt);
    }

    if (result.count("set-edge-speed")) {
        edge_specs = result["set-edge-speed"].as<std::vector<std::string>>();
        boost::property_tree::ptree pt;
        if (result.count("config") && filesystem::is_regular_file(config_file_path)) {
            rapidjson::read_json(config_file_path, pt);
        } else {
            std::cerr << "Configuration is required for --set-edge-speed" << std::endl;
            return EXIT_FAILURE;
        }
        return handle_set_edge_speed(edge_specs, pt);
    }
```

注意: `csv_path` 和 `edge_specs` 变量声明需要在 `try` 块内、`cxxopts::Options options(argv[0], ...)` 附近。

- [ ] **Step 4: 修改 `handle_help()` 中的工具描述**

将 `valhalla_traffic_demo_utils` 替换为 `valhalla_live_traffic`：

```cpp
// 修改 cxxopts::Options 构造行（约 283 行）：
    cxxopts::Options options(argv[0],
                             " - Provides utilities for adding live traffic to valhalla routing tiles.");
```

- [ ] **Step 5: Commit**

```bash
cd /home/admin/valhalla-project && git add -A poc/valhalla_code_overwrites/src/mjolnir/ && git commit -m "feat: rename to valhalla_live_traffic, add --update-edges and --set-edge-speed commands"
```

---

### Task 4: 更新 `valhalla_code_overwrites/src/CMakeLists.txt`

**Files:**
- Modify: `valhalla-project/poc/valhalla_code_overwrites/src/CMakeLists.txt:179-199`

- [ ] **Step 1: 将 `live_traffic_utils.cc` 加入 valhalla 库编译**

找到 `set(valhalla_src ...)` 行（约 179 行），在 `proto_conversions.cc` 之后追加新行：

```cmake
set(valhalla_src
    worker.cc
    filesystem.cc
    proto_conversions.cc
    ${VALHALLA_SOURCE_DIR}/src/mjolnir/live_traffic_utils.cc
    ${VALHALLA_SOURCE_DIR}/valhalla/config.h
    ${valhalla_hdrs}
    ${libvalhalla_link_objects})
```

- [ ] **Step 2: Commit**

```bash
cd /home/admin/valhalla-project && git add poc/valhalla_code_overwrites/src/CMakeLists.txt && git commit -m "build: add live_traffic_utils.cc to valhalla library target"
```

---

### Task 5: 更新 `valhalla_code_overwrites/CMakeLists.txt`

**Files:**
- Modify: `valhalla-project/poc/valhalla_code_overwrites/CMakeLists.txt:300`

- [ ] **Step 1: 工具名替换**

```bash
cd /home/admin/valhalla-project
# 在 CMakeLists.txt 第 300 行，将 valhalla_traffic_demo_utils 替换为 valhalla_live_traffic
sed -i 's/valhalla_traffic_demo_utils/valhalla_live_traffic/g' poc/valhalla_code_overwrites/CMakeLists.txt
```

- [ ] **Step 2: 确认替换结果**

```bash
grep "valhalla_live_traffic" poc/valhalla_code_overwrites/CMakeLists.txt
```

期望输出包含: `valhalla_add_predicted_traffic valhalla_assign_speeds valhalla_add_elevation valhalla_live_traffic)`

- [ ] **Step 3: Commit**

```bash
cd /home/admin/valhalla-project && git add poc/valhalla_code_overwrites/CMakeLists.txt && git commit -m "build: rename valhalla_traffic_demo_utils → valhalla_live_traffic in CMakeLists"
```

---

### Task 6: 更新 `poc/build.sh`

**Files:**
- Modify: `valhalla-project/poc/build.sh:38-40`

- [ ] **Step 1: 追加库文件复制命令，更新 CLI 文件名**

在现有 3 条 `cp` 命令处，将原文件复制改为新文件名，并追加库文件复制：

```bash
# 修改后的复制段（替换 build.sh 第 38-40 行）:
# 复制自定义文件
cp "$SCRIPT_DIR/valhalla_code_overwrites/src/mjolnir/live_traffic_utils.h" "$VALHALLA_DIR/src/mjolnir/live_traffic_utils.h"
cp "$SCRIPT_DIR/valhalla_code_overwrites/src/mjolnir/live_traffic_utils.cc" "$VALHALLA_DIR/src/mjolnir/live_traffic_utils.cc"
cp "$SCRIPT_DIR/valhalla_code_overwrites/src/mjolnir/valhalla_live_traffic.cc" "$VALHALLA_DIR/src/mjolnir/valhalla_live_traffic.cc"
cp "$SCRIPT_DIR/valhalla_code_overwrites/CMakeLists.txt" "$VALHALLA_DIR/CMakeLists.txt"
cp "$SCRIPT_DIR/valhalla_code_overwrites/src/CMakeLists.txt" "$VALHALLA_DIR/src/CMakeLists.txt"
```

- [ ] **Step 2: Commit**

```bash
cd /home/admin/valhalla-project && git add poc/build.sh && git commit -m "build: update build.sh for live_traffic_utils and valhalla_live_traffic"
```

---

### Task 7: 更新 `poc/Dockerfile`

**Files:**
- Modify: `valhalla-project/poc/Dockerfile:63`

- [ ] **Step 1: 追加 COPY 指令**

在 Dockerfile 的第 63 行之后（`COPY valhalla_code_overwrites/src/mjolnir/valhalla_traffic_demo_utils.cc` 之后），追加：

```dockerfile
# New: live traffic library and per-edge utility
COPY valhalla_code_overwrites/src/mjolnir/live_traffic_utils.h valhalla/src/mjolnir/live_traffic_utils.h
COPY valhalla_code_overwrites/src/mjolnir/live_traffic_utils.cc valhalla/src/mjolnir/live_traffic_utils.cc
```

同时将原有 `valhalla_traffic_demo_utils.cc` 的行更新为：

```dockerfile
COPY valhalla_code_overwrites/src/mjolnir/valhalla_live_traffic.cc valhalla/src/mjolnir/valhalla_live_traffic.cc
```

- [ ] **Step 2: Commit**

```bash
cd /home/admin/valhalla-project && git add poc/Dockerfile && git commit -m "build: update Dockerfile for valhalla_live_traffic and library files"
```

---

### Task 8: 验证

- [ ] **Step 1: 检查所有文件存在且路径正确**

```bash
echo "=== Checking new files ==="
ls -la /home/admin/valhalla-project/poc/valhalla_code_overwrites/src/mjolnir/live_traffic_utils.h
ls -la /home/admin/valhalla-project/poc/valhalla_code_overwrites/src/mjolnir/live_traffic_utils.cc
ls -la /home/admin/valhalla-project/poc/valhalla_code_overwrites/src/mjolnir/valhalla_live_traffic.cc

echo "=== Checking old file removed ==="
! ls /home/admin/valhalla-project/poc/valhalla_code_overwrites/src/mjolnir/valhalla_traffic_demo_utils.cc 2>/dev/null && echo "OK: old file removed"

echo "=== Checking CMakeLists references ==="
grep -n "valhalla_traffic_demo_utils" /home/admin/valhalla-project/poc/valhalla_code_overwrites/CMakeLists.txt && echo "WARNING: old name still present" || echo "OK: no old references"
grep -n "valhalla_live_traffic" /home/admin/valhalla-project/poc/valhalla_code_overwrites/CMakeLists.txt
grep -n "live_traffic_utils" /home/admin/valhalla-project/poc/valhalla_code_overwrites/src/CMakeLists.txt

echo "=== Checking build.sh ==="
grep -n "live_traffic_utils\|valhalla_live_traffic" /home/admin/valhalla-project/poc/build.sh

echo "=== Checking Dockerfile ==="
grep -n "live_traffic_utils\|valhalla_live_traffic\|valhalla_traffic_demo_utils" /home/admin/valhalla-project/poc/Dockerfile
```

- [ ] **Step 2: 验证零修改核心文件**

```bash
# 确认核心文件未被修改
cd /home/admin/valhalla-project/poc/valhalla
echo "=== Checking core files untouched ==="
for f in valhalla/baldr/graphtile.h valhalla/baldr/traffictile.h valhalla/baldr/graphreader.h valhalla/baldr/directededge.h src/baldr/graphreader.cc; do
    if git diff --name-only 2>/dev/null | grep -q "$f"; then
        echo "WARNING: $f was modified"
    else
        echo "OK: $f untouched"
    fi
done
```

- [ ] **Step 3: 编译验证**（需要 Docker 或本地 Valhalla 构建环境）

```bash
cd /home/admin/valhalla-project/poc
# 如果有 Docker 环境:
# docker build -t valhalla-live-traffic-test .
# 或本地构建:
# ./build.sh
```

编译成功后验证新工具可用：

```bash
# 检查新工具是否存在
which valhalla_live_traffic || ls -la /usr/local/bin/valhalla_live_traffic

# 检查帮助输出
valhalla_live_traffic --help 2>&1 | grep -E "update-edges|set-edge-speed"
```

- [ ] **Step 4: 功能验证 — 使用现有 Andorra tiles 测试**

```bash
cd /home/admin/valhalla-project/poc/valhalla_tiles

# 查看一个 tile 的 way_edges.txt 获取实际 edge 信息
head -3 valhalla_tiles/way_edges.txt

# 创建测试 CSV（使用实际 tile 和 edge 数据）
# 这里的 tile_id 需要根据实际 tiles 调整
cat > /tmp/test_speeds.csv << 'EOF'
# tile_id,edge_index,speed_kph,congestion
0/0/0,0,45.5,1
0/0/0,1,12.0,31
EOF

# 按边更新
valhalla_live_traffic --config valhalla.json --update-edges /tmp/test_speeds.csv

# 验证 traffic.tar 已更新
ls -la traffic.tar
```

- [ ] **Step 5: Commit**

```bash
# 如有任何修正则提交
cd /home/admin/valhalla-project && git add -A && git status
```

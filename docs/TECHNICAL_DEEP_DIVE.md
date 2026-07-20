# Valhalla Traffic Project — 技术细节深读指南

> 按技术主题组织，每个主题标注关键文件和行号，适合在编辑器中边看边读。
> 建议读序：数据格式 → 编码层 → 聚合算法 → 热重载机制 → Pipeline 架构 → 构建系统。

---

## 目录

1. [数据格式: Heartbeat CSV → GPS → Edge](#1-数据格式-heartbeat-csv--gps--edge)
2. [二进制编码: TrafficSpeed & GraphId 位布局](#2-二进制编码-trafficspeed--graphid-位布局)
3. [速度聚合: 时间衰减加权平均](#3-速度聚合-时间衰减加权平均)
4. [热重载: shared_ptr 原子切换 + 双缓冲](#4-热重载-shared_ptr-原子切换--双缓冲)
5. [Pipeline: 5 阶段架构 & DataNode 数据流](#5-pipeline-5-阶段架构--datanode-数据流)
6. [构建系统: Docker + CMake + 代码注入](#6-构建系统-docker--cmake--代码注入)
7. [测试架构: 离线 → Docker → 热重载 三层验证](#7-测试架构-离线--docker--热重载-三层验证)

---

## 1. 数据格式: Heartbeat CSV → GPS → Edge

### 1.1 原始 Heartbeat CSV

**文件**: `tests/data/heartbeat/heartbeat-2025-03-01.csv` (450MB, 2,835,790 行)

```
id,f0_,location,bearing,speed,device_time,server_time
3ae38ba2...,v6y5UnsG...,POINT(114.198600738 22.343012951),2.66,4.01,2025-02-28 16:00:00,...
```

**格式要点**:
- `location` 是 WKT (Well-Known Text) 格式: `POINT(lon lat)` — **注意经度在前**
- `bearing` 是航向角 (0-360°)
- `speed` 单位是 km/h
- `device_time` 和 `server_time` 可能存在时钟偏差

**读代码入口**: `tests/scripts/test_heartbeat_parse.py:18-27`
```python
coords = location.replace('POINT(', '').replace(')', '').split()
lon, lat = float(coords[0]), float(coords[1])  # POINT(lon lat)
```

### 1.2 GPS 过滤规则

**读代码入口**: `tests/scripts/test_heartbeat_parse.py:28-32` 和 `heartbeat_to_edge_csv.py:107-113`

三条过滤规则:
```python
# 1. 地理围栏: 香港区域
if not (22.0 <= lat <= 22.6 and 113.8 <= lon <= 114.3):
    continue

# 2. 速度异常值
if speed <= 0 or speed > 150:
    continue

# 3. 零点坐标
if lon == 0 and lat == 0:
    continue
```

### 1.3 GPS → Edge 映射 (在线模式)

**核心文件**: `tests/scripts/heartbeat_to_edge_csv.py`

**完整调用链**:
```
heartbeat CSV row
  → parse_heartbeat_csv()          [line 77]  解析 CSV 行
  → call_locate()                  [line 137] 调用 /locate API
  → EdgeSpeedAggregator.add()      [line 220] 按 edge 累积样本
  → compute_average()              [line 227] 时间衰减加权平均
  → write_edge_csv()               [line 279] 输出 Valhalla 兼容格式
```

**关键函数 `call_locate()`** (`heartbeat_to_edge_csv.py:137-205`):
```python
def call_locate(lat, lon, base_url="http://localhost:8002"):
    data = json.dumps({"locations": [{"lat": lat, "lon": lon}], "verbose": True})
    # POST → /locate?verbose=true
    # 解析返回的 edge_id 信息:
    #   edge_id_info = {'level': 0, 'tile_id': 3381, 'id': 15, 'value': 27055}
    # 提取:
    #   level      = edge_id_info['level']       # hierarchy level 0-7
    #   tile_index = edge_id_info['tile_id']     # 22-bit tile index
    #   edge_idx   = edge_id_info['id']          # 21-bit edge index within tile
    #   tile_key   = tile_base_value(lvl, tile_index)  # 用于 CSV 第一列
```

**为什么需要 `tile_key` (又名 `tile_id_key`)?**
CSV 格式要求: `level/tile_index/0, edge_index, speed_kph, congestion`
第三个数 `0` 表示 edge_id=0 的 tile base GraphId，它唯一标识一个 tile。
`tile_key = level | (tile_index << 3) | (0 << 25)` = tile base 对应的 64-bit 值。

---

## 2. 二进制编码: TrafficSpeed & GraphId 位布局

### 2.1 GraphId: 64-bit 复合键

**参考规范**: Valhalla `baldr/graphid.h`

```
Bit layout (64 bits total):
  ┌───────┬──────────────────────┬───────────────────────┬─────┐
  │  bits │  [45:25]  (21 bits)  │  [24:3]  (22 bits)    │[2:0]│
  │       │  edge_id             │  tile_index           │level│
  └───────┴──────────────────────┴───────────────────────┴─────┘

value = level | (tile_index << 3) | (edge_id << 25)
```

**Python 实现**: `heartbeat_to_edge_csv.py:55-71`
```python
def graphid_value(lvl, tile_index, edge_id=0):
    return lvl | (tile_index << 3) | (edge_id << 25)

def graphid_decompose(value):
    lvl        = value & 0x7              # bits [2:0]
    tile_index = (value & 0x1fffff8) >> 3 # bits [24:3]
    edge_id    = (value & 0x3ffffe000000) >> 25  # bits [45:25]
    return lvl, tile_index, edge_id

def tile_base_value(lvl, tile_index):
    return graphid_value(lvl, tile_index, edge_id=0)
```

**位掩码验证** (`validate_per_edge_injection.sh:172-223`):
```python
# 测试用例:
(0, 3381, 0)   → value=? → decompose 应回到 (0, 3381, 0)
(2, 647736, 5) → value=? → decompose 应回到 (2, 647736, 5)
```

### 2.2 TrafficTile 二进制结构

**TrafficTile 总布局**:
```
┌──────────────────────────────────────────────────────┐
│ TrafficTileHeader  (24 bytes)                        │
│ ┌─────────────────┬────────────────────────────────┐ │
│ │ tile_id         │ uint64 (8 bytes)               │ │
│ │ last_update     │ uint64 (8 bytes, epoch secs)   │ │
│ │ edge_count      │ uint32 (4 bytes)               │ │
│ │ version         │ uint32 (4 bytes)               │ │
│ └─────────────────┴────────────────────────────────┘ │
├──────────────────────────────────────────────────────┤
│ TrafficSpeed[]  (8 bytes × edge_count)               │
│ ┌─────────────────┬────────────────────────────────┐ │
│ │ per-edge entry  │ uint64 bitfield                │ │
│ └─────────────────┴────────────────────────────────┘ │
├──────────────────────────────────────────────────────┤
│ Padding (8 bytes, 全 0)                              │
└──────────────────────────────────────────────────────┘
```

### 2.3 TrafficSpeed 64-bit Bitfield

```
Bit layout:
┌──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┐
│ bits │[6:0] │[13:7]│[20:14]│[27:21]│[35:28]│[43:36]│[49:44]│[55:50]│[61:56]│
│      │overall│ spd1 │ spd2  │ spd3  │ bp1   │ bp2   │ cong1 │ cong2 │ cong3 │
└──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┘
                                                [62]        [63]
                                              has_incidents  spare
```

- `overall_encoded_speed`: 7 bits, 分辨率 2 km/h, 范围 [0, 126], 127 = UNKNOWN
- `breakpoint1/2`: 8 bits each, 255 表示速度覆盖整条边
  - breakpoint1=127 表示前 50% 的边用 speed1，后 50% 用 speed2
- `congestion1/2/3`: 6 bits each, 范围 [1, 63]

**Python 编码模拟**: `validate_per_edge_injection.sh:105-157`
```python
UNKNOWN_TRAFFIC_SPEED_RAW = 127
MAX_CONGESTION_VAL = 63

def encode_live_speed(speed_kph):
    raw = int(speed_kph / 2.0)  # 2kph 分辨率
    if raw > UNKNOWN_TRAFFIC_SPEED_RAW - 1:
        raw = UNKNOWN_TRAFFIC_SPEED_RAW - 1  # clamp to 126 max
    return raw  # decoded = raw * 2
```

**C++ 等效实现**: `realtime/src/baldr/realtime_traffic_updater.h:92-94`
```cpp
static uint8_t EncodeSpeed(float speed_kph) {
    return static_cast<uint8_t>(std::min(speed_kph / 2.0f, 127.0f));
}
```

### 2.4 拥堵程度映射

| 速度范围 | 拥堵等级 | congestion 值 | 含义 |
|----------|----------|---------------|------|
| < 10 km/h | 严重拥堵 | 50-51 | 几乎停滞 |
| 10-20 km/h | 中度拥堵 | 30-31 | 缓行 |
| 20-40 km/h | 轻度拥堵 | 15-16 | 较慢 |
| > 40 km/h | 畅通 | 5-6 | 正常行驶 |

**读代码入口**: `realtime/src/baldr/realtime_traffic_updater.cc:137-147`

---

## 3. 速度聚合: 时间衰减加权平均

### 3.1 算法概述

同一 edge 可能有多个 heartbeat GPS 点。需要对它们做加权平均得到一个代表速度。

**核心算法** (`realtime_traffic_updater.cc:72-113`):
```
对于每条 edge 的所有样本:
  1. 丢弃超过 window 秒的旧样本
  2. 对剩余样本做时间衰减加权:
     weight = max(0.1, 1.0 - age / window)
     weighted_sum  += speed × weight
     weight_total  += weight
  3. avg_speed = weighted_sum / weight_total
```

**Python 等效**: `heartbeat_to_edge_csv.py:227-271`
```python
def compute_average(self, now=None):
    for (tile_key, edge_idx), speed_list in self.samples.items():
        weighted_sum = 0.0
        weight_total = 0.0
        for speed, ts in speed_list:
            age = now - ts
            if age > self.window:  # 超过窗口的丢弃
                continue
            weight = max(0.1, 1.0 - age / self.window)
            weighted_sum += speed * weight
            weight_total += weight

        if weight_total < 0.01:  # 权重太低则丢弃
            continue
        avg_speed = weighted_sum / weight_total
```

### 3.2 权重衰减曲线

```
weight
  1.0 ─┤***
       │    ****
       │         ****
       │              ****
  0.1 ─┤                   ********────────── (floor at 0.1)
       │
       └─────┬─────────────┬──────────────▶ age
             0           window          window+ε
```

**为什么设置 floor=0.1?**
防止窗口末端的数据权重无限趋近 0。即使是最旧的样本也保留 10% 的贡献，避免速度计算被单点数据主导。

### 3.3 滑动窗口数据清理

**C++**: `realtime_traffic_updater.cc:77-82`
```cpp
int64_t cutoff = now - speed_window_seconds_;
samples.erase(
    std::remove_if(samples.begin(), samples.end(),
        [cutoff](const SpeedSample& s) { return s.timestamp < cutoff; }),
    samples.end()
);
```

---

## 4. 热重载: shared_ptr 原子切换 + 双缓冲

这是整个项目最核心的技术设计。目标是**在不重启 Valhalla 服务、不中断正在处理的请求的情况下**，切换到新的 traffic 数据。

### 4.1 整体架构

```
Python Daemon (realtime_traffic_daemon.py)
  │
  ├── 1. 读取 heartbeat CSV 流
  ├── 2. GPS → edge 映射 (调用 /locate API)
  ├── 3. 速度聚合 (60s 滑动窗口)
  │
  ├── 4. 生成 next.tar.new
  │    └── BuildTrafficTar()
  │
  ├── 5. 原子 rename: next.tar.new → standby.tar
  │    └── filesystem::rename() (原子操作, 同文件系统)
  │
  └── 6. 通知 valhalla_service: POST /admin/reload_traffic
       │
       ▼
C++ GraphReader (graphreader_hot_reload.cc)
  │
  ├── 1. 验证新文件存在且大小 > 512 bytes
  ├── 2. 创建 new midgard::tar 对象并解析 tile 索引
  ├── 3. 在 mutex 保护下替换 tile_extract_->traffic_archive (shared_ptr 赋值)
  └── 4. Trim() 清理旧缓存
```

### 4.2 关键 C++ 代码

**读代码入口**: `realtime/src/baldr/graphreader_hot_reload.cc:21-106`

**步骤 1: 文件验证** (lines 23-39)
```cpp
// 检查文件存在且大小合理 (> 512 bytes)
if (!filesystem::exists(new_traffic_path)) { return false; }
auto file_size = filesystem::file_size(new_traffic_path);
if (file_size < 512) { return false; }
```

**步骤 2: 加载新 archive 到临时对象** (lines 42-73)
```cpp
new_archive = std::make_shared<midgard::tar>(new_traffic_path, true);
// 遍历 tar 内容, 构建 tile_id → data 索引
for (auto& c : new_archive->contents) {
    auto id = GraphTile::GetTileId(c.first);        // 从文件名提取 tile_id
    new_traffic_tiles[id] = std::make_pair(
        const_cast<char*>(c.second.first),          // mmap 数据指针
        c.second.second                              // 数据大小
    );
}
```

**步骤 3: 原子切换** (lines 76-97) — **这是核心**
```cpp
{
    std::lock_guard<std::mutex> lock(tile_extract_mutex_);
    // tile_extract_->traffic_archive 是 shared_ptr<midgard::tar>
    // shared_ptr 赋值本身是原子操作 (引用计数安全)
    tile_extract_->traffic_archive = new_archive;
    tile_extract_->traffic_tiles  = new_traffic_tiles;
}
// 此时:
//   - 新请求 → 看到 new_archive (新数据)
//   - 正在处理的请求 → 仍持有旧 shared_ptr → 继续使用旧数据
//   - 旧数据在最后一个旧请求完成后自动释放 (shared_ptr 引用计数归零)
```

**步骤 4: 清理缓存** (line 103)
```cpp
Trim();  // 清空已加载的 GraphTile 缓存, 强制下次请求加载新 traffic 数据
```

### 4.3 为什么不需要暂停服务? (以及为什么有时需要!)

**理想情况 (热加载代码已编译进 Valhalla)**:

```
时间线:
  t0: Request A 开始处理 (持有旧 shared_ptr, refcount=2)
  t1: 热加载触发, traffic_archive 被替换 (旧 refcount=1, 新 refcount=1)
  t2: Request B 到达 → 拿到新 shared_ptr → 用新数据
  t3: Request A 完成 → 旧 shared_ptr refcount=0 → 旧内存自动释放
  t4: Request C 到达 → 拿到新 shared_ptr → 用新数据

  ✓ Request A 全程使用旧数据 — 无数据竞争
  ✓ Request B, C 使用新数据 — 即时切换
  ✓ 无服务中断, 无请求排队
```

**实际情况 (如果没有热加载代码)**:

```
valhalla_service 在启动时:
  1. midgard::tar 打开 traffic.tar 文件
  2. mmap(文件, PROT_READ) → 映射到进程地址空间
  3. 后续所有 GetSpeed() 调用从 mmap 内存中读取

valhalla_live_traffic --update-edges:
  1. mmap(文件, PROT_READ | PROT_WRITE, MAP_SHARED)
  2. 直接修改文件内容 → 磁盘已更新
  3. msync → 刷到磁盘

问题: valhalla_service 的 mmap 是独立的!
  - 服务有自己的 mmap 映射，不会自动感知底层文件变化
  - midgard::tar 没有文件变更监听机制
  - 服务必须通过 HotReloadTrafficArchive() 重新加载

┌─────────────────────────────────────────────────────────┐
│  磁盘 traffic.tar                                        │
│  ┌──────────────────────────────────────────────────┐   │
│  │ [旧数据] → valhalla_live_traffic 修改 → [新数据] │   │
│  └──────────────────────────────────────────────────┘   │
│       ▲ mmap (服务)          ▲ mmap (工具写入)           │
│       │                      │                           │
│  valhalla_service       valhalla_live_traffic            │
│  (读旧数据!)             (写新数据)                       │
│                                                          │
│  服务必须重新 mmap 才能看到新数据!                        │
└─────────────────────────────────────────────────────────┘
```

**所以关键规则是**: `valhalla_live_traffic --update-edges` (离线工具) 修改文件后，必须通过以下方式之一让服务感知:

1. `POST /admin/reload_traffic` → 触发 `HotReloadTrafficArchive()` (如果已编译)
2. 重启 valhalla_service → 重新 mmap traffic.tar
3. 重启 Docker 容器

### 4.4 检测热加载是否可用

```bash
# 方法 1: 直接调用热加载 API
curl -s -X POST http://localhost:8002/admin/reload_traffic \
    -H "Content-Type: application/json" \
    -d '{"traffic_path": "/valhalla_tiles/traffic.tar"}'

# 如果返回: {"success": true, ...} → 热加载可用 ✓
# 如果返回: {"error": "Unknown action"} 或 404 → 热加载未编译
# 如果返回: Connection refused → 服务未启动
```

```bash
# 方法 2: 检查 binary 是否包含热加载符号
strings /usr/local/bin/valhalla_service | grep -i "HotReload\|reload_traffic"
# 有输出 → 热加载已编译 ✓
# 无输出 → 热加载未编译, 需要重启服务
```

### 4.5 双缓冲文件命名

```
traffic_dir/
├── traffic_active.tar    ← 当前活跃 (valhalla_service mmap 读取)
├── traffic_standby.tar   ← 预备 (上次更新生成)
├── traffic_next.tar.new  ← 构建中 (本次更新正在写入)
└── traffic_current.tar   ← 符号链接 → active (用于引用)
```

**切换序** (`realtime_traffic_updater.cc:260-267`):
```
1. BuildTrafficTar() → next.tar.new  (构建中)
2. filesystem::rename(next.tar.new → standby.tar)  (原子 rename)
3. 通知 GraphReader 从 standby.tar 加载
```

**读代码入口**: `realtime/src/baldr/realtime_traffic_updater.cc:258-287`

### 4.6 注入 GraphReader 的方式

**build.sh 使用 sed 执行代码注入** (`realtime/build.sh:65-161`):
```bash
# 1. 追加 HotReloadTrafficArchive 实现到 graphreader.cc 末尾
cat >> "$VALHALLA_SRC/graphreader.cc" << 'EOF'
bool GraphReader::HotReloadTrafficArchive(const std::string& new_traffic_path) {
    // ... 完整实现 ...
}
EOF

# 2. 在 graphreader.h 中注入方法声明
sed -i '/virtual void Trim() {/i\
  bool HotReloadTrafficArchive(const std::string& new_traffic_path);\
' "$GRAPHREADER_H"

# 3. 注入 mutex 成员变量
sed -i '/^private:/i\
  mutable std::mutex tile_extract_mutex_;' "$GRAPHREADER_H"
```

---

## 5. Pipeline: 5 阶段架构 & DataNode 数据流

### 5.1 架构概览

```
PipelineConfig ──→ PipelineOrchestrator ──→ 5 × BaseStage
                      │
                      ├── DataCleanStage
                      ├── MapMatchingStage  ← ValhallaClient
                      ├── SpeedCalculationStage
                      ├── EmptySlotsFillingStage
                      └── SpeedProfileGenerationStage
                            │
                            ▼
                       DataNode (贯穿所有阶段)
```

**读代码入口**:
- `pipeline/traffic_pipeline/traffic_pipeline/pipeline/base.py` — 基类
- `pipeline/traffic_pipeline/traffic_pipeline/orchestrator.py` — 编排器
- `pipeline/traffic_pipeline/traffic_pipeline/stages/` — 5 个阶段实现

### 5.2 PipelineConfig

**文件**: `pipeline/traffic_pipeline/traffic_pipeline/pipeline/base.py:18-48`

```python
@dataclass
class PipelineConfig:
    valhalla_service_url: str = "http://localhost:8002"
    workers: int = 4
    batch_size: int = 100

    # 每个阶段可以独立开关
    enable_data_clean: bool = True
    enable_map_matching: bool = True
    enable_speed_calculation: bool = True
    enable_empty_slots_filling: bool = True
    enable_speed_profile_generation: bool = True
```

### 5.3 DataNode — 阶段间的数据载体

**文件**: `pipeline/traffic_pipeline/traffic_pipeline/pipeline/base.py:50-84`

```python
@dataclass
class DataNode:
    # 输入
    trajectories: Optional[List[Dict]] = None
    trips: Optional[List[Dict]] = None

    # 各阶段输出 (累加, 不覆盖)
    cleaned_trajectories: Optional[pd.DataFrame] = None    # Stage 1 输出
    cleaned_trips: Optional[pd.DataFrame] = None           # Stage 1 输出
    map_matched_points: pl.DataFrame = None                # Stage 2 输出
    raw_speeds: Optional[Dict] = None                      # Stage 3 输出
    filled_speeds: Optional[Dict] = None                   # Stage 4 输出
    speed_profiles: Dict[str, NDArray] = {}                # Stage 5 输出

    metadata: Dict[str, Any] = {}  # 元数据贯穿
    errors: List[str] = []         # 错误收集
```

**数据流**: 每个阶段的 `process()` 返回新的 DataNode，携带着该阶段的输出。Orchestrator 将它传给下一个阶段。

### 5.4 BaseStage.run() 模板方法

**文件**: `pipeline/traffic_pipeline/traffic_pipeline/pipeline/base.py:166-201`

```python
def run(self, data: DataNode, output_dir=None) -> StageResult:
    # 1. 验证输入
    if not self.validate_input(data):
        return StageResult.fail(...)

    # 2. 处理 (子类实现)
    result_data = self.process(data)

    # 3. 保存输出 (子类可选实现)
    if output_dir:
        self.save_output(result_data, output_dir)

    return StageResult.ok(data=result_data)
```

### 5.5 Orchestrator 运行循环

**文件**: `pipeline/traffic_pipeline/traffic_pipeline/orchestrator.py:122-172`

```python
def run(self):
    data = self.load_input()             # 加载原始数据

    for stage in self.stages:            # 顺序执行 5 个阶段
        result = stage.run(data, output_dir)
        if not result.success:           # 任一阶段失败则终止
            return False
        data = result.data               # 传递到下一阶段

    self.save_output(data, output_dir)   # 保存最终结果
```

### 5.6 Stage 2 MapMatching 的 HTTP 连接池

**文件**: `pipeline/traffic_pipeline/traffic_pipeline/stages/stage2_map_matching.py:84-111`

```python
def get_session(self):
    """每个线程一个 HTTP Session (连接复用, 减少 TCP 握手)"""
    thread_local = threading.local()
    sess = getattr(thread_local, "session", None)
    if sess is None:
        sess = requests.Session()
        pool_size = 50
        adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
        sess.mount("http://", adapter)
        thread_local.session = sess
    return sess

def match_single_trip(self, item):
    """单个 trip 的 map matching, 在线程池中并发执行"""
    trip_id, gps_trace = item
    # 构造 trace_attributes 请求
    shape_for_api = [{"lat": p["lat"], "lon": p["lon"]} for p in gps_trace]
    payload = {"shape": shape_for_api, "costing": "auto", "shape_match": "map_snap"}
    r = sess.post(f"{base_url}/trace_attributes", json=payload, timeout=30)
    return r.json()
```

### 5.7 各阶段职责与输出格式

| Stage | 类名 | 输入 | 输出 | 输出格式 |
|-------|------|------|------|----------|
| 1 | `DataCleanStage` | GPS raw CSV | `cleaned_trajectories` | Parquet (按文件) |
| 2 | `MapMatchingStage` | cleaned trajectories | `map_matched_points` | Polars DataFrame (trip_id, edges, matched_points) |
| 3 | `SpeedCalculationStage` | matched edges | `raw_speeds` | `Dict[edge_id, Dict[time_bucket, List[speed]]]` |
| 4 | `EmptySlotsFillingStage` | raw_speeds (有空洞) | `filled_speeds` | 同上 (空洞被填充) |
| 5 | `SpeedProfileGenerationStage` | filled_speeds | `speed_profiles` | Valhalla CSV: `edge_id, freeflow_speed, constrained_speed, historical_speeds...` |

---

## 6. 构建系统: Docker + CMake + 代码注入

### 6.1 realtime/build.sh 的整体流程

**文件**: `realtime/build.sh` (334 行)

```
build.sh 执行流程:
  ┌──────────────────────────────────────────────────────────┐
  │ Step 1: 检查基础项目 /home/admin/valhalla_traffic_poc_  │
  │ Step 2: 复制扩展文件到基础项目的 valhalla/src/baldr/    │
  │ Step 3: 备份原始 graphreader.cc / graphreader.h          │
  │ Step 4: 用 sed + cat 注入热加载代码到 graphreader.{h,cc}│
  │         - 在 .cc 末尾追加 HotReloadTrafficArchive()     │
  │         - 在 .h 中添加方法声明 + mutex 成员变量         │
  │ Step 5: 编译 valhalla (cmake + make)                     │
  │ Step 6: 复制 Python daemon 到项目目录                    │
  │ Step 7: 生成启动脚本 run_realtime_service.sh            │
  │ Step 8: 生成测试脚本 test_hot_reload.sh                  │
  └──────────────────────────────────────────────────────────┘
```

### 6.2 关键: 对 Valhalla 核心文件的零修改策略

**文件**: `tests/scripts/validate_per_edge_injection.sh:228-261` (Phase 3)

设计原则是**新增文件, 不修改 Valhalla 核心代码**:
```
valhalla_code_overwrites/src/mjolnir/
├── live_traffic_utils.h       ← 新增: TrafficSpeed 编码工具函数
├── live_traffic_utils.cc      ← 新增: 编码实现
└── valhalla_live_traffic.cc   ← 新增: CLI 工具入口 (替代 valhalla_traffic_demo_utils)
```

验证脚本会检查:
- 核心文件 (`graphtile.h`, `traffictile.h`, `graphreader.h`, `directededge.h`) 存在且未被修改
- 新增文件 (`live_traffic_utils.h/.cc`, `valhalla_live_traffic.cc`) 存在
- GraphId bug fix 已应用: `GraphId(tile, lvl, 0).value` ← 参数顺序正确

### 6.3 Docker 双容器架构 (Pipeline)

```
Container 1: valhalla-local-test (port 8080)
  ┌────────────────────────────────────────┐
  │ valhalla_service                       │
  │ /trace_attributes ← map matching API   │
  │ /custom_files/tiles/way_edges.txt      │
  └────────────────────────────────────────┘
              ▲
              │ HTTP (trace_attributes)
              │
Container 2: traffic-pipeline (无端口)
  ┌────────────────────────────────────────┐
  │ traffic_pipeline/                      │
  │ ├── Stage 1-5                          │
  │ ├── clients/valhalla_client.py  ← 调用 │
  │ └── data/road_data/way_edges.txt ← 映射│
  └────────────────────────────────────────┘
```

**way_edges.txt 的作用**: OSM way ID → Valhalla edge ID 映射
- 在 Container 1 构建时由 `valhalla_ways_to_edges` 生成
- 必须与 Container 2 中的副本一致
- 通过在 Dockerfile 中运行 `valhalla_ways_to_edges` 确保一致性

### 6.4 CMake 集成 (POC 模块)

**`poc/valhalla_code_overwrites/CMakeLists.txt`** 在 valhalla 根 CMakeLists 中:
- 将 `valhalla_traffic_demo_utils` 加入 `valhalla_data_tools` 构建目标
- 为 valhalla target 添加 `microtar` 库依赖 (用于在 C++ 中构建 tar 文件)
- 新增 `valhalla_live_traffic` CLI 工具 (替代旧的 demo utils)

---

## 7. 测试架构: 离线 → Docker → 热重载 三层验证

### 7.1 三层测试模型

```
Layer 1: 离线验证 (无 Docker, < 2 min)
├── CSV 格式解析
├── TrafficSpeed 编码 (Python vs C++ 对比)
├── GraphId 位运算验证
└── 源代码完整性 (核心文件未修改)

Layer 2: Docker 集成 (需要 Docker, ~5 min)
├── 双容器启动
├── API 端点 (/status, /route, /locate)
├── Pipeline 5 阶段
└── way_edges.txt 一致性

Layer 3: 热重载验证 (需要 Docker + 服务, ~3 min)
├── 速度注入 → /locate 验证
├── ETA 方向验证 (低速 → 高 ETA)
├── Heartbeat E2E 验证
├── 10 次一致性测试
├── 30 并发 + 3 次热重载 稳定性测试
└── 异常输入测试 (6 种异常场景)
```

### 7.2 关键测试脚本映射

| 脚本 | 对应层级 | 覆盖范围 |
|------|----------|----------|
| `test_heartbeat_parse.py` | Layer 1 | CSV 解析 → 速度统计 |
| `test_realtime_traffic_update.py` | Layer 1 | heartbeat → traffic.tar 生成 |
| `heartbeat_to_edge_csv.py --offline` | Layer 1 | 数据格式 + GPS 范围验证 |
| `validate_per_edge_injection.sh` | Layer 1+2 | 离线验证 + Docker 在线验证 |
| `valhalla_hotreload_test.sh` | Layer 2+3 | 8 步骤全覆盖验证 |
| `test_hot_reload.sh` | Layer 3 | 热重载快速测试 (简化版) |

### 7.3 validate_per_edge_injection.sh 的 4 阶段设计

**文件**: `tests/scripts/validate_per_edge_injection.sh` (463 行)

```
Phase 1 (离线): 数据格式验证
  └── CSV header, GPS WKT, 速度统计, GPS 范围

Phase 2 (离线): 编码验证
  ├── TrafficSpeed 2kph 分辨率: 8 个测试用例 (0 → 300 km/h)
  └── GraphId 位运算: 5 个 (level, tile, edge_id) 往返测试

Phase 3 (离线): 源代码完整性
  ├── 核心文件存在且未修改
  ├── 新增文件存在
  └── GraphId bug fix 已应用 (参数顺序检查)

Phase 4 (在线): Docker 构建 + 注入验证
  ├── valhalla_live_traffic --update-edges
  ├── /locate 返回 injected live_speed
  ├── /route ETA 验证
  └── Hot Reload 自动感知
```

### 7.4 valhalla_hotreload_test.sh 的 8 步骤设计

**文件**: `tests/scripts/valhalla_hotreload_test.sh` (799 行)

| Step | 检查函数 | 验证内容 |
|------|----------|----------|
| 1/8 | `log_info` / `log_pass` | Docker 镜像、heartbeat 挂载、tile 数量、服务启动 |
| 2/8 | `check_not_empty` / `check_numeric_gt` | `/status`, `/route` (2 条), `/locate` |
| 3/8 | `check_result` / `check_numeric_ge` | 速度注入 60→80→5→120 km/h, 验证 overall_speed=2×speed |
| 4/8 | Python inline | Heartbeat CSV 解析 → 均速注入 → 热重载 → 路由查询 |
| 5/8 | Bash 循环 | 10 次相同 /route 请求, 断言结果一致 |
| 6/8 | Python inline | 30 个并发请求 + 3 次热重载, 统计错误率+延迟 |
| 7/8 | curl + grep | 6 种异常: 南极/空/单点/起终点相同/无效costing/海上 |
| 8/8 | PASS/FAIL 汇总 | 所有测试统计, exit code |

---

## 8. 常见问题诊断: live_speed 注入后仍为 none

### 问题现象

```bash
$ valhalla_live_traffic --config /valhalla_tiles/valhalla.json --update-edges /tmp/edge_speeds.csv
Updated 101 edges in /valhalla_tiles/traffic.tar    ← 工具报告成功

$ curl -s http://localhost:8002/locate?verbose=true \
    -d '{"locations":[{"lat":22.3430,"lon":114.1986}],"verbose":true}' | ...
edge[12562]: live=none kph    ← 查询不到!!
```

### 三个根因 + 排查顺序

```
优先级:  #1 > #2 > #3
```

**根因 #1 (概率 90%): 未触发热加载/重启**

`valhalla_live_traffic` 修改磁盘文件，但 valhalla_service 在启动时 mmap 了旧版本，不会自动感知变更。

```bash
# 检查
strings /usr/local/bin/valhalla_service | grep -i HotReload
# 有输出→ 调用热加载API
# 无输出→ 重启服务
```

**根因 #2 (概率 7%): edge 不匹配**

注入的 edge_index 和 `/locate` 查询返回的 edge_index 不同。`--set-edge-speed "2/647736/0,370769,5,51"` 中的 `370769` 必须等于 `/locate` 返回的 `edge_id.id`。

```bash
# 先查 edge，再注入 (使用同一个 GPS 坐标!)
curl .../locate -d '{"locations":[{"lat":22.343,"lon":114.199}],"verbose":true}' \
  | python3 -c "print edge level/tile_id/edge_index"
```

**根因 #3 (概率 3%): 路径不一致**

`valhalla.json` 中 `mjolnir.traffic_extract` 的路径 ≠ `valhalla_live_traffic` 写入的文件路径。

```bash
python3 -c "import json; print(json.load(open('valhalla.json'))['mjolnir']['traffic_extract'])"
# 必须与工具输出的 "Updated N edges in XXX" 中的 XXX 一致
```

### 正确流程

```
1. /locate 查询目标 GPS → 拿到 {level, tile_id, edge_index}
2. --set-edge-speed 用步骤 1 的精确值注入
3. POST /admin/reload_traffic (或重启服务)
4. /locate 用步骤 1 相同的 GPS 坐标再次查询
5. overall_speed 应 = 注入的 speed_kph × 2
```

---

## 9. 读代码建议路线

### 路线 A: 理解数据流 (推荐起点)

```
1. tests/scripts/heartbeat_to_edge_csv.py   ← 从 CSV 到 edge 的完整链路
   ├── parse_heartbeat_csv()     [line 77]   解析 CSV
   ├── call_locate()             [line 137]  API 调用
   ├── EdgeSpeedAggregator       [line 212]  聚合器
   └── write_edge_csv()          [line 279]  输出格式

2. tests/scripts/test_realtime_traffic_update.py  ← traffic.tar 生成
   ├── HeartbeatRecord           [line 34]   数据类
   ├── TrafficTarGenerator       [line 77]   生成器
   └── _build_traffic_tile()     [line 132]  二进制格式
```

### 路线 B: 理解热重载

```
1. realtime/src/baldr/graphreader_hot_reload.h    ← 接口声明
2. realtime/src/baldr/graphreader_hot_reload.cc   ← 实现
   └── HotReloadTrafficArchive()  [line 21]  原子切换核心

3. realtime/src/baldr/realtime_traffic_updater.h   ← 更新器接口
   ├── HeartbeatRecord            [line 19]   数据结构
   ├── RealtimeTrafficUpdater     [line 49]   类定义
   └── EncodeSpeed()              [line 92]   编码器

4. realtime/src/baldr/realtime_traffic_updater.cc  ← 更新器实现
   ├── AggregateSpeedsByTile()    [line 46]   聚合算法
   ├── BuildTrafficTar()          [line 164]  tar 构建
   ├── UpdateFromHeartbeats()     [line 235]  主循环
   └── SwitchTrafficArchive()     [line 289]  切换逻辑
```

### 路线 C: 理解 Pipeline 架构

```
1. pipeline/traffic_pipeline/traffic_pipeline/pipeline/base.py
   ├── PipelineConfig             [line 18]   配置
   ├── DataNode                   [line 50]   数据载体
   ├── StageResult                [line 87]   阶段结果
   └── BaseStage.run()            [line 166]  模板方法

2. pipeline/traffic_pipeline/traffic_pipeline/orchestrator.py
   └── PipelineOrchestrator.run() [line 122]  编排循环

3. pipeline/traffic_pipeline/traffic_pipeline/clients/valhalla_client.py
   └── ValhallaClient             HTTP 客户端

4. pipeline/traffic_pipeline/traffic_pipeline/stages/stage2_map_matching.py
   ├── match_with_session()       [line 98]   HTTP 连接池
   └── get_matched_points_threaded() [line 127] 并发匹配
```

### 路线 D: 理解构建和部署

```
1. realtime/build.sh              ← 完整构建流程 (334 行)
   ├── Step 2: 文件复制           [line 44]
   ├── Step 4: sed 代码注入       [line 65-161]
   ├── Step 5: cmake 编译         [line 164-202]
   └── Step 7: 启动脚本生成       [line 223-263]

2. pipeline/Dockerfile            ← Container 1 构建
3. pipeline/traffic_pipeline/Dockerfile  ← Container 2 构建
```

---

## 10. 关键常量和边界值速查

| 常量 | 值 | 位置 |
|------|-----|------|
| `UNKNOWN_TRAFFIC_SPEED_RAW` | 127 (7-bit max) | `realtime_traffic_updater.h:94` |
| 速度编码分辨率 | 2 km/h | TrafficSpeed 格式 |
| 最大有效编码速度 | 126 → 252 km/h | `UNKNOWN_TRAFFIC_SPEED_RAW - 1` |
| 最小有效速度 | > 5 km/h (Valhalla 阈值) | — |
| 速度有效范围 | (0, 150] km/h | `test_heartbeat_parse.py:28` |
| 香港纬度范围 | [22.0, 22.6] | `test_heartbeat_parse.py:26` |
| 香港经度范围 | [113.8, 114.3] | `test_heartbeat_parse.py:26` |
| 速度聚合窗口 | 60s (默认) | `RealtimeTrafficUpdater` 构造 |
| 更新间隔 | 5s (默认) | `RealtimeTrafficUpdater` 构造 |
| 时间衰减 floor | 0.1 | `realtime_traffic_updater.cc:95` |
| TrafficTileHeader | 24 bytes | `traffictile.h` |
| TrafficSpeed | 8 bytes/edge | `traffictile.h` |
| GraphId level bits | [2:0] (3 bits) | `graphid.h` |
| GraphId tile bits | [24:3] (22 bits) | `graphid.h` |
| GraphId edge bits | [45:25] (21 bits) | `graphid.h` |
| 最小 traffic.tar 大小 | 512 bytes | `graphreader_hot_reload.cc:31` |

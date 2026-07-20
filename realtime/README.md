# Valhalla Realtime Traffic

基于 Valhalla 路由引擎的实时交通速度更新系统。

**运行环境**: Docker 容器

## 功能特性

- **不重启服务的热更新**: 通过双缓冲机制实现 traffic.tar 的原子切换
- **实时速度聚合**: 从 GPS heartbeat 数据流计算道路实时速度
- **滑动窗口算法**: 60 秒时间衰减加权平均，平滑瞬时波动
- **5 秒更新间隔**: 支持高频数据更新

## 架构设计

```
┌─────────────────────────────────────────────────────────────┐
│                    valhalla_service                          │
│  ┌──────────────────────────────────────────────────────┐   │
│  │                   GraphReader                         │   │
│  │  ┌────────────────────────────────────────────────┐  │   │
│  │  │            tile_extract_t                      │  │   │
│  │  │  traffic_archive: shared_ptr<midgard::tar>     │  │   │
│  │  └────────────────────────────────────────────────┘  │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
                              ▲
                              │ 热加载通知
                              │
┌─────────────────────────────────────────────────────────────┐
│         realtime_traffic_daemon.py (Python 守护进程)         │
│  ┌────────────┐   ┌────────────┐   ┌────────────────────┐  │
│  │ heartbeat  │──►│  map-match │──►│  speed_aggregator  │  │
│  │   stream   │   │  edge_id   │   │  60s sliding window│  │
│  └────────────┘   └────────────┘   └────────────────────┘  │
│                                         │                   │
│  ┌──────────────────────────────────────┴───────────────┐  │
│  │              traffic.tar 生成器                       │  │
│  │  active.tar ← standby.tar ← next.tar.new             │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

## 快速开始

### 1. 构建 Docker 镜像

```bash
cd valhalla_traffic_poc_
docker build -t valhalla-traffic .
```

**构建时间**: 约 10 分钟（首次构建）

### 2. 构建并启动 Docker 容器

```bash
# 构建镜像 (在 poc/ 或 valhalla_traffic_poc_/ 中)
cd /home/admin/valhalla_traffic_poc_
docker build -t valhalla-traffic .

# 运行容器（端口转发 8002, 挂载 heartbeat 数据）
docker run -p 8002:8002 \
  -v /home/admin/valhalla-project/tests/data/heartbeat/heartbeat-2025-03-01.csv:/data/heartbeat.csv \
  -it valhalla-traffic bash
```

### 3. 容器内启动服务

```bash
# 在容器内先执行 build.sh 注入代码 (首次)
cd /root/valhalla_traffic_realtime && ./build.sh

# 启动 valhalla_service
LD_LIBRARY_PATH=/usr/local/lib valhalla_service /valhalla_tiles/valhalla.json 1 &

# 启动守护进程
python3 /root/valhalla_traffic_realtime/scripts/realtime_traffic_daemon.py \
    --config /valhalla_tiles/valhalla.json \
    --heartbeat /data/heartbeat.csv \
    --interval 5 \
    --window 60
```

### 4. 验证热加载

```bash
# 在容器内查看日志
tail -f /valhalla_tiles/valhalla.log

# 检查 traffic 文件
ls -la /valhalla_tiles/traffic_*.tar

# 测试 API
curl -X POST http://localhost:8002/admin/reload_traffic \
    -H "Content-Type: application/json" \
    -d '{"traffic_path": "/valhalla_tiles/traffic_standby.tar"}'
```

## 配置文件

### valhalla.json 配置项

**Docker 容器内路径**:

```json
{
    "mjolnir": {
        "traffic_extract": "/valhalla_tiles/traffic_current.tar",
        "tile_dir": "/valhalla_tiles",
        ...
    }
}
```

### 守护进程参数

**Docker 容器内启动**:

```bash
python3 realtime_traffic_daemon.py \
    --config /valhalla_tiles/valhalla.json \
    --heartbeat /data/heartbeat.csv \
    --interval 5 \
    --window 60
```

| 参数 | 说明 | 默认值 |
|------|------|--------|
| --config | valhalla.json 路径 | `/valhalla_tiles/valhalla.json` |
| --heartbeat | heartbeat CSV 文件路径 | 必需 |
| --interval | 更新间隔 (秒) | 5 |
| --window | 速度聚合窗口 (秒) | 60 |

**注意**: 容器内路径使用 `/valhalla_tiles/` 而非 `/workspace/valhalla_tiles/`

## 数据格式

### Heartbeat CSV

```csv
id,f0_,location,bearing,speed,device_time,server_time
3ae38ba2-7eee-44e7-b102-72f8c6026ec2,v6y5UnsGYC1ZichOse2NSg==,POINT(114.198600738 22.343012951),2.66,4.01,2025-02-28 16:00:00,2025-02-28 16:00:01.541 UTC
```

### TrafficTile 结构

```
TrafficTileHeader (24 字节)
├── tile_id (8 bytes)
├── last_update (8 bytes, epoch seconds)
├── directed_edge_count (4 bytes)
├── traffic_tile_version (4 bytes)
└── spare2, spare3 (8 bytes)

TrafficSpeed[] (8 字节/边)
├── overall_encoded_speed: 7 bits (2kph 分辨率)
├── encoded_speed1-3: 7 bits each
├── breakpoint1-2: 8 bits each
├── congestion1-3: 6 bits each
├── has_incidents: 1 bit
└── spare: 1 bit
```

## API 端点

### POST /admin/reload_traffic

触发热加载 traffic.tar

**请求体:**
```json
{
    "traffic_path": "/valhalla_tiles/traffic_standby.tar"
}
```

**响应:**
```json
{
    "success": true,
    "message": "Traffic archive hot-reloaded successfully",
    "tiles_loaded": 123
}
```

## 性能指标

| 指标 | 目标值 |
|------|--------|
| 更新延迟 | < 5 秒 |
| 内存开销 | < 100MB |
| 并发查询影响 | 无 |
| 服务中断时间 | 0 秒 |

## 故障排查

### traffic.tar 未生成

检查守护进程日志:
```bash
# 查看 Docker 容器日志
docker logs <container_id> 2>&1 | tail -50

# 或在容器内检查
docker exec -it <container_id> bash
tail -f /valhalla_tiles/valhalla.log
```

### 热加载失败

1. 验证文件存在：`ls -la /valhalla_tiles/traffic_*.tar`
2. 检查文件大小：`du -h /valhalla_tiles/traffic_*.tar`
3. 查看 Valhalla 日志：`tail -f /valhalla_tiles/valhalla.log`

### 速度数据异常

调整聚合窗口:
```bash
# 增加窗口以平滑数据
--window 120

# 减少窗口以更快响应
--window 30
```

## 扩展开发

### 添加自定义速度源

实现 `RealtimeTrafficUpdater` 的 `_map_to_edge_index` 方法:

```python
def _map_to_edge_index(self, lat, lon):
    # 调用实际的 map-matching API
    resp = requests.post('http://localhost:8002/locate', json={
        'lat': lat, 'lon': lon
    })
    return resp.json()['edges'][0]['edge_index']
```

### 集成外部交通数据

修改 `process_heartbeat_batch` 方法融合多源数据:

```python
def process_heartbeat_batch(self, records):
    # 1. GPS 速度
    gps_speeds = self._aggregate_gps(records)

    # 2. 融合外部数据
    external_speeds = self._fetch_external_data()
    fused_speeds = self._fuse_sources(gps_speeds, external_speeds)

    # 3. 生成 tar
    self._build_traffic_tar(fused_speeds)
```

## 备份与恢复

### 备份原始配置

**在构建 Docker 镜像前**:

```bash
# 备份原始文件
cd valhalla_traffic_poc_/valhalla
cp src/baldr/graphreader.cc src/baldr/graphreader.cc.orig
cp valhalla/baldr/graphreader.h valhalla/baldr/graphreader.h.orig
```

### 恢复到原始版本

**方法 1: 恢复 Docker 镜像**
```bash
# 重新构建 Docker 镜像
docker build -t valhalla-traffic .
```

**方法 2: 在容器内恢复**
```bash
# 进入容器
docker exec -it <container_id> bash

# 恢复原始文件
cd /valhalla
git checkout src/baldr/graphreader.cc
git checkout valhalla/baldr/graphreader.h
```

## 许可证

与 Valhalla 主项目保持一致 (BSD 2-Clause)。

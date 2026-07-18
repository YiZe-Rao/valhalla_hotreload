# Valhalla Live Traffic 逐边注入 — 完整人工测试流程

> **测试环境**: Docker 容器 `valhalla-live-traffic:v1`  
> **测试数据**: heartbeat-2025-03-01.csv (2,835,790 条记录, 香港区域)  
> **测试日期**: 2026-06-30

---

## 阶段 0：启动容器

### Step 0.1 — 启动容器

```bash
/usr/bin/sudo docker run -d \
  --name valhalla-live-test \
  -p 8002:8002 \
  valhalla-live-traffic:v1 \
  sleep infinity
```

**预期**: 返回容器 ID (64 位十六进制字符串)

### Step 0.2 — 进入容器

```bash
/usr/bin/sudo docker exec -it valhalla-live-test bash
```

**预期**: 进入容器 shell，提示符变为 `root@<container_id>:/#`

### Step 0.3 — 确认环境

```bash
# 检查二进制
which valhalla_live_traffic
valhalla_live_traffic --help | head -5

# 检查 tiles
ls /valhalla_tiles/valhalla_tiles/2/
ls /valhalla_tiles/valhalla.json

# 检查 traffic.tar 状态
ls -la /valhalla_tiles/traffic.tar
```

**预期**:
- `which` 输出 `/usr/local/bin/valhalla_live_traffic`
- `--help` 显示包含 `--update-edges`、`--set-edge-speed` 的选项列表
- tiles 目录存在 `2/` 子目录 (香港 tile level=2)
- `traffic.tar` 不存在或大小为 0

---

## 阶段 1：生成 Baseline traffic.tar

### Step 1.1 — 确定需要生成哪些 tile

```bash
# 查看 heartbeat 覆盖哪些 tile（稍后会用到）
# 先确认可用的 tile 列表
ls /valhalla_tiles/valhalla_tiles/2/
```

**预期**: 看到 `000/` 到 `999/` 范围的目录，其中香港 tile 的 tile_index 约在 647xxx 范围

### Step 1.2 — 为覆盖区域的主要 tile 生成 baseline tar

> `valhalla_live_traffic` 用容器内的路径：`/valhalla_tiles/valhalla.json`

```bash
valhalla_live_traffic \
  --config /valhalla_tiles/valhalla.json \
  --generate-live-traffic "2/647736/0,30,$(date +%s)"
```

**参数说明**:
| 值 | 含义 |
|----|------|
| `2/647736/0` | tile 坐标 (level=2, tile_index=647736, id=0) |
| `30` | 编码 baseline 速度 = 60 km/h (`30 * 2 = 60`) |
| `$(date +%s)` | 当前 epoch 时间戳 |

**预期输出**:
```
Generated traffic.tar successfully at /valhalla_tiles/traffic.tar
```

### Step 1.3 — 确认 tar 已生成

```bash
ls -la /valhalla_tiles/traffic.tar
```

**预期**: 文件大小 > 0 (约几十 KB，取决于 tile 的 directed_edge_count)

---

## 阶段 2：启动 valhalla_service

### Step 2.1 — 后台启动服务

```bash
LD_LIBRARY_PATH=/usr/local/lib valhalla_service /valhalla_tiles/valhalla.json 1 \
  > /tmp/valhalla.log 2>&1 &
```

### Step 2.2 — 等待服务就绪

```bash
sleep 3
curl -s http://localhost:8002/status | head -c 200
```

**预期**: 返回 JSON 状态信息 (含 `version` 字段)

### Step 2.3 — 确认 service 已加载 traffic.tar

```bash
grep -i "traffic" /tmp/valhalla.log | tail -5
```

**预期**: 日志包含流量文件加载信息（或无明显错误）

---

## 阶段 3：Heartbeat → Edge CSV 转换

### Step 3.1 — 复制 heartbeat 数据到容器

> **在宿主机另外开一个终端执行**:

```bash
# 先复制 heartbeat CSV 到容器
/usr/bin/sudo docker cp \
  /home/admin/valhalla-project/tests/data/heartbeat/heartbeat-2025-03-01.csv \
  valhalla-live-test:/tmp/heartbeat.csv

# 确认复制完成
/usr/bin/sudo docker exec valhalla-live-test ls -lh /tmp/heartbeat.csv
```

**预期**: 显示文件大小约 450 MB

### Step 3.2 — 复制转换脚本到容器

```bash
/usr/bin/sudo docker cp \
  /home/admin/valhalla-project/tests/scripts/heartbeat_to_edge_csv.py \
  valhalla-live-test:/tmp/heartbeat_to_edge_csv.py
```

### Step 3.3 — 运行转换（小样本先验证）

> **回到容器内执行**:

```bash
python3 /tmp/heartbeat_to_edge_csv.py \
  --heartbeat /tmp/heartbeat.csv \
  --max-records 500 \
  --valhalla-url http://localhost:8002 \
  --delay-ms 50 \
  --output /tmp/edge_speeds.csv
```

**核心观察指标**:

```
[INFO] CSV header: ['id', 'f0_', 'location', 'bearing', 'speed', ...]
[INFO] Parsed XXX valid records from /tmp/heartbeat.csv
[INFO] Mapping XXX GPS points via http://localhost:8002/locate ...
[INFO]   [100/XXX] mapped=YY unmapped=ZZ unique_edges=WW rate=X.X/s
[INFO]   [200/XXX] mapped=YY unmapped=ZZ unique_edges=WW rate=X.X/s
...
[INFO] Mapping complete: XXX mapped, YY unmapped, ZZ unique edges in XX.Xs
```

**关键判定**:
- `mapped > 0` — GPS 点成功映射到 edge ✅
- `unique_edges > 0` — 至少覆盖了部分唯一边 ✅
- 如果 `mapped = 0`，检查 service 是否正常运行、tiles 是否正确

**预期输出**:

```
============================================================
  Heartbeat → Edge CSV 转换完成
============================================================
  Heartbeat 记录:    500
  成功映射:          XXX
  映射失败:          YYY
  唯一边数:          ZZZ
  输出边数:          WWW (聚合后)
  输出文件:          /tmp/edge_speeds.csv

  速度统计: avg=XX.X min=X.X max=XX.X km/h

  样本输出 (前5行):
    2/XXXXXX/0,XXXXXX,XX.X,X
    2/XXXXXX/0,XXXXXX,XX.X,X
============================================================
```

### Step 3.4 — 查看转换结果

```bash
head -20 /tmp/edge_speeds.csv
```

**预期**: 看到 CSV 格式的行，非注释行格式为 `level/tile_index/0,edge_idx,speed_kph,congestion`

---

## 阶段 4：注入实时速度

### Step 4.1 — 用 `--update-edges` 批量注入

```bash
valhalla_live_traffic \
  --config /valhalla_tiles/valhalla.json \
  --update-edges /tmp/edge_speeds.csv
```

**预期**: `Updated N edges in /valhalla_tiles/traffic.tar` (N > 0)

**故障排查**:
- 如果输出 `Updated 0 edges`: traffic.tar 不存在或 tile 不在 tar 中 → 回到 Step 1.2 重新生成
- 如果输出 `Tile not found`: CSV 中的 tile_id 在 routing tiles 中不存在 → 检查 tile 坐标是否正确

### Step 4.2 — 用 `--set-edge-speed` 注入单条指定边（精确控制）

从 CSV 输出中选取一条边手动测试：

```bash
# 假设 CSV 输出第一条是 2/647736/0,370769,77.0,6
# 注入为 88 km/h (明显不同于批量值)
valhalla_live_traffic \
  --config /valhalla_tiles/valhalla.json \
  --set-edge-speed "2/647736/0,370769,88,6"
```

**预期**: `Updated 1 edges in /valhalla_tiles/traffic.tar`

### Step 4.3 — 再注入一条严重拥堵的边做对比

```bash
# 同 tile 下另一条边，注入 5 km/h 严重拥堵
valhalla_live_traffic \
  --config /valhalla_tiles/valhalla.json \
  --set-edge-speed "2/647736/0,370770,5,51"
```

**预期**: `Updated 1 edges in /valhalla_tiles/traffic.tar`

---

## 阶段 5：验证注入效果

### 测试 5A — `/locate` API 验证

### Step 5A.1 — 查询注入过的 edge

```bash
curl -s http://localhost:8002/locate?verbose=true \
  -H "Content-Type: application/json" \
  -d '{"locations":[{"lat":22.3430,"lon":114.1986}]}' \
  | python3 -m json.tool | head -60
```

**人工观察**:
- 找到 `"edges"` 数组的第一个元素
- 查看 `"edge_id"` 中的 `"id"` 和 `"tile_id"` 值
- 查看 `"live_speed"` 对象中的 `"overall_speed"` — **这是注入的速度**

### Step 5A.2 — 用脚本提取关键信息

```bash
curl -s http://localhost:8002/locate?verbose=true \
  -H "Content-Type: application/json" \
  -d '{"locations":[{"lat":22.3430,"lon":114.1986}]}' \
  | python3 -c "
import json, sys
resp = json.load(sys.stdin)
for e in resp[0].get('edges', [])[:5]:
    ei = e.get('edge_id', {})
    ls = e.get('live_speed', {})
    ps = e.get('predicted_speeds', [])
    edge_id = ei.get('id', '?')
    live = ls.get('overall_speed', 'none') if ls else 'none'
    pred = ps[0] if ps else 'none'
    print(f'edge[{edge_id}]: live={live} kph, predicted={pred} kph')
"
```

**判定规则**:

| 观察 | 结论 |
|------|------|
| 注入边 `live=N` (N > 0), N ≤ predicted | ✅ 实时速度已生效 |
| 未注入边 `live=none` | ✅ 未注入边正确保留 baseline |
| 所有边 `live=none` | ❌ traffic.tar 未被加载或有编码错误 |

### 测试 5B — Hot Reload 验证

### Step 5B.1 — 记录当前速度

```bash
# 查询当前速度
curl -s http://localhost:8002/locate?verbose=true \
  -H "Content-Type: application/json" \
  -d '{"locations":[{"lat":22.3430,"lon":114.1986}]}' \
  | python3 -c "
import json, sys
e = json.load(sys.stdin)[0]['edges'][0]
ls = e.get('live_speed', {})
print(f'Before: edge[{e.get(\"edge_id\",{}).get(\"id\",\"?\")}] live={ls.get(\"overall_speed\",\"none\")} kph')
"
```

### Step 5B.2 — 修改同一条边的速度（极端值便于观察）

```bash
# 改成 3 km/h — 按 2kph 编码后为 2 km/h
valhalla_live_traffic \
  --config /valhalla_tiles/valhalla.json \
  --set-edge-speed "2/647736/0,370769,3,63"
```

### Step 5B.3 — 立即查询（无需重启 valhalla_service）

```bash
curl -s http://localhost:8002/locate?verbose=true \
  -H "Content-Type: application/json" \
  -d '{"locations":[{"lat":22.3430,"lon":114.1986}]}' \
  | python3 -c "
import json, sys
e = json.load(sys.stdin)[0]['edges'][0]
ls = e.get('live_speed', {})
print(f'After:  edge[{e.get(\"edge_id\",{}).get(\"id\",\"?\")}] live={ls.get(\"overall_speed\",\"none\")} kph')
"
```

**判定**:
- `Before` 显示原速度 (88 km/h → 编码为 88)
- `After` 显示新速度 (3 km/h → 编码为 2)
- **无需重启 valhalla_service 即生效** ✅
- 如果 `After` 仍显示旧值，检查 `valhalla_live_traffic` 是否输出了 `Updated 1 edges`

### 测试 5C — `/route` API 验证

### Step 5C.1 — 路由查询

```bash
curl -s "http://localhost:8002/route" \
  -H "Content-Type: application/json" \
  -d '{
    "locations": [
      {"lat": 22.280, "lon": 114.160},
      {"lat": 22.320, "lon": 114.190}
    ],
    "costing": "auto",
    "directions_options": {"units": "km"}
  }' | python3 -c "
import json, sys
s = json.load(sys.stdin)['trip']['summary']
print(f'time={s[\"time\"]}s, length={s[\"length\"]}km')
"
```

**预期**: 返回合理路由时间（不应为 0 或报错）

---

## 阶段 6：数据完整性验证

### Step 6.1 — 确认 traffic.tar 结构正确

```bash
# 用 Python 快速检查 traffic.tar 内容
python3 -c "
import struct

with open('/valhalla_tiles/traffic.tar', 'rb') as f:
    data = f.read()

# 跳过 tar header (512 bytes)
TAR_HEADER_SIZE = 512
TRAFFIC_HEADER_SIZE = 32  # sizeof(TrafficTileHeader)
TRAFFIC_SPEED_SIZE = 8     # sizeof(TrafficSpeed)

offset = TAR_HEADER_SIZE
while offset < len(data) - TRAFFIC_HEADER_SIZE:
    tile_id, last_update, edge_count, version = struct.unpack_from('<QQII', data, offset)
    print(f'  Tile: {tile_id & 0xffffffff:08x}')
    print(f'    Last update: {last_update}')
    print(f'    Edge count:  {edge_count}')
    print(f'    Version:     {version}')
    
    # 检查前几条边的速度
    for i in range(min(3, edge_count)):
        speed_offset = offset + TRAFFIC_HEADER_SIZE + i * TRAFFIC_SPEED_SIZE
        raw = struct.unpack_from('<Q', data, speed_offset)[0]
        overall = raw & 0x7f
        bp1 = (raw >> 28) & 0xff
        valid = 'VALID' if bp1 != 0 and overall != 127 else 'INVALID'
        print(f'    edge[{i}]: encoded={overall} ({overall*2} kph) bp1={bp1} [{valid}]')
    
    # 跳到下一个 tile entry (每个 tar entry 有 512-byte 头部)
    entry_size = TRAFFIC_HEADER_SIZE + edge_count * TRAFFIC_SPEED_SIZE + 8
    padded_size = ((entry_size + 511) // 512) * 512  # tar 块对齐
    offset += TAR_HEADER_SIZE + padded_size
    
    # 安全限制：只检查第一个 tile
    break
"
```

**预期输出示例**:

```
  Tile: 00edc5d0
    Last update: 1719000000
    Edge count:  610
    Version:     3
    edge[0]: encoded=30 (60 kph) bp1=255 [VALID]
    edge[1]: encoded=30 (60 kph) bp1=255 [VALID]
    edge[2]: encoded=30 (60 kph) bp1=255 [VALID]
```

**人工检查要点**:
- `Version` 必须是 `3` (TRAFFIC_TILE_VERSION)
- 注入过的边 `overall` 值与预期一致
- 未注入的边 `bp1=255` (如果是 baseline) 或 `bp1=0` (如果是从零构建的)

### Step 6.2 — 确认服务日志无错误

```bash
tail -30 /tmp/valhalla.log | grep -i -E "error|warn|traffic|fail"
```

**预期**: 无严重错误 (少量 WARN 可接受)

---

## 阶段 7：测试完成检查清单

| # | 验证项 | 通过标准 | 你的结果 |
|---|--------|----------|----------|
| 1 | `valhalla_live_traffic --help` | 包含 `--update-edges`, `--set-edge-speed` | |
| 2 | `--generate-live-traffic` | 生成 traffic.tar (size > 0) | |
| 3 | `valhalla_service` 启动 | `curl /status` 返回 200 | |
| 4 | `heartbeat_to_edge_csv.py` | `mapped > 0`, 输出 CSV 格式正确 | |
| 5 | `--update-edges <csv>` | `Updated N edges` (N > 0) | |
| 6 | `--set-edge-speed` 单边 | `Updated 1 edges` | |
| 7 | `/locate` 返回注入速度 | `live_speed.overall_speed = floor(speed/2)*2` | |
| 8 | `/locate` 未注入边 | `live_speed = null` 或 baseline | |
| 9 | Hot Reload | 修改后立即查询结果变化，无需重启 | |
| 10 | `/route` 正常返回 | 时间 + 距离合理 | |
| 11 | 服务日志无异常 | 无 FATAL/ERROR 关于 traffic | |

---

## 阶段 8：收尾

### Step 8.1 — 停止并清理容器

```bash
# 退出容器
exit

# 停止并删除容器
/usr/bin/sudo docker stop valhalla-live-test
/usr/bin/sudo docker rm valhalla-live-test

# （可选）保留镜像以便下次测试
# /usr/bin/sudo docker rmi valhalla-live-traffic:v1
```

---

## 附录 A：常用调试命令

```bash
# 查看所有 valhalla_live_traffic 选项
valhalla_live_traffic --help

# 将 raw GraphId 转为 level/tile/id
valhalla_live_traffic --get-tile-id 325892112389

# 查看 traffic.tar 的修改时间
stat /valhalla_tiles/traffic.tar | grep Modify

# 用 hexdump 检查 traffic.tar 前几条边
xxd /valhalla_tiles/traffic.tar | head -40

# 查看 service 的完整日志
less /tmp/valhalla.log
```

## 附录 B：编码对照速查

| km/h | encoded (overall_speed) | `/locate` 返回 |
|------|------------------------|----------------|
| 3 | 1 | 2 |
| 5 | 2 | 4 |
| 30 | 15 | 30 |
| 60 | 30 | 60 |
| 77 | 38 | 76 |
| 88 | 44 | 88 |
| 120 | 60 | 120 |
| 252 | 126 | 252 |
| ≥254 | 127 (= UNKNOWN) | `null` |

> `/locate` 返回的 `overall_speed` = `encoded * 2`。77 km/h → `floor(77/2)=38` → `38*2=76`。

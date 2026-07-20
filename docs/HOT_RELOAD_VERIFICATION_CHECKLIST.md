# /admin/reload_traffic 人工检查验证清单

> 基于 2026-07-20 `valhalla-live-test` 容器 (valhalla-live-traffic:v1, Valhalla v3.1.4) 实测结果

## 前置条件

- Docker 容器运行中: `valhalla-live-test` (image: `valhalla-live-traffic:v1`)
- valhalla_service 在 8002 端口运行
- `valhalla_live_traffic` CLI 工具可用

---

## 检查项 1: 确认 valhalla_service 运行状态

```bash
# 进入容器
docker exec -it valhalla-live-test bash

# 检查进程
ps aux | grep valhalla_service | grep -v grep

# 期望输出: valhalla_service /valhalla_tiles/valhalla.json 1
```

**✓ 实测结果**: PID 400, 正常运行于 8002 端口

---

## 检查项 2: 确认 HotReloadTrafficArchive 函数已编译

```bash
strings /usr/local/bin/valhalla_service | grep "HotReload"
```

**期望输出** (2 行):
```
_ZN8valhalla5baldr11GraphReader23HotReloadTrafficArchiveE...cold
_ZN8valhalla5baldr11GraphReader23HotReloadTrafficArchiveE...
```

**解读**:
| 结果 | 含义 |
|------|------|
| 有输出 (>=1 行) | `HotReloadTrafficArchive()` 已编译进 binary ✅ |
| 无输出 | 函数未编译，确认需重启方案 |

**✓ 实测结果**: 2 行输出，函数已编译

> ⚠️ **关键**: 函数已编译 **≠** HTTP 端点可用。函数只是被注入到了 `graphreader.cc`，但 prime_server HTTP handler 需要单独注册。

---

## 检查项 3: 确认 /admin/reload_traffic 返回 404

```bash
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST http://localhost:8002/admin/reload_traffic \
    -H "Content-Type: application/json" \
    -d '{"traffic_path": "/valhalla_tiles/traffic.tar"}')
echo "HTTP Status: $HTTP_CODE"
```

**期望**: `HTTP Status: 404`

**✓ 实测结果**: 404, 返回 error_code=106, 可用端点列表中无 `reload_traffic`

---

## 检查项 4: 确认 loki.actions 中缺少 reload_traffic

```bash
python3 -c "
import json
with open('/valhalla_tiles/valhalla.json') as f:
    config = json.load(f)
actions = config.get('loki',{}).get('actions',[])
print(f'已注册 ({len(actions)}):')
for a in sorted(actions):
    print(f'  - {a}')
print(f'reload_traffic 在配置中: {\"是\" if \"reload_traffic\" in actions else \"否 ✗\"}')
"
```

**期望**: 12 个 actions，**不含** `reload_traffic`

**✓ 实测结果**:
```
  - locate, route, height, sources_to_targets, optimized_route,
    isochrone, trace_route, trace_attributes, transit_available,
    expansion, centroid, status
reload_traffic 在配置中: 否 ✗
```

---

## 检查项 5: 确认 protobuf Action 枚举不含 reload_traffic

```bash
strings /usr/local/lib/libvalhalla_loki.so | grep "reload"
```

**期望**: 无输出（枚举中不含 `reload_traffic`）

**✓ 实测结果**: 无输出

---

## 检查项 6: 验证 traffic.tar 结构和 tile 匹配

```bash
python3 -c "
import struct, json, subprocess

# traffic.tar 信息
with open('/valhalla_tiles/traffic.tar', 'rb') as f:
    data = f.read()
offset = 512
tile_id = struct.unpack('<Q', data[offset:offset+8])[0]
cnt = struct.unpack('<I', data[offset+16:offset+20])[0]
level = tile_id & 0x7
tile_idx = (tile_id >> 3) & 0x3FFFFF
print(f'traffic.tar: tile_id={tile_id} level={level} tile_index={tile_idx} edges={cnt}')

# /locate 信息
result = subprocess.run([
    'curl', '-s', '-X', 'POST', 'http://localhost:8002/locate',
    '-H', 'Content-Type: application/json',
    '-d', '{\"locations\":[{\"lat\":22.2816,\"lon\":114.1585}],\"verbose\":true}'
], capture_output=True, text=True)
d = json.loads(result.stdout)
e = d[0]['edges'][0]
ei = e['edge_id']
val = ei['value']
lvl2 = val & 0x7
tile2 = (val >> 3) & 0x3FFFFF
edg2 = (val >> 25) & 0x1FFFFF
base = lvl2 | (tile2 << 3)
print(f'/locate:   tile_base={base} level={lvl2} tile_index={tile2} edge_index={edg2}')

# 匹配检查
if base == tile_id:
    print('MATCH: /locate tile == traffic.tar tile ✓')
    print(f'  注入命令格式: --set-edge-speed \"{level}/{tile_idx}/0,<speed>,<congestion>\"')
else:
    print(f'MISMATCH: /locate tile={base} != traffic.tar tile={tile_id} ✗')
    print('  需要: --generate-live-traffic 重建 traffic.tar 匹配 GPS 的 tile')
"
```

**✓ 实测结果**:
```
traffic.tar: tile_id=5181890 level=2 tile_index=647736 edges=416153
/locate:   tile_base=5181890 level=2 tile_index=647736 edge_index=349381
MATCH: /locate tile == traffic.tar tile ✓
```

---

## 检查项 7: 执行完整的 注入→重启→验证 流程

### Step 1: 查询当前速度

```bash
curl -s -X POST http://localhost:8002/locate \
    -H "Content-Type: application/json" \
    -d '{"locations":[{"lat":22.2816,"lon":114.1585}],"verbose":true}' | \
python3 -c "import sys,json; d=json.load(sys.stdin); e=d[0]['edges'][0]; \
  print(f'overall_speed(before)={e.get(\"overall_speed\",\"N/A\")}'); \
  print(f'live_speed={e.get(\"live_speed\",{})}')"
```

### Step 2: 重建 traffic.tar（匹配 GPS 的 tile）

```bash
# ⚠️ 必须用数字时间戳，TS 令牌会导致 crash (stoull 异常)
LEVEL=2; TILE=647736; TS=$(date +%s)
valhalla_live_traffic --config /valhalla_tiles/valhalla.json \
    --generate-live-traffic "$LEVEL/$TILE/0,44,$TS"
```

**✓ 实测结果**: `Generated traffic.tar succesfully at /valhalla_tiles/traffic.tar`

### Step 3: 注入新速度

```bash
# 注入 5 km/h 到 edge_index=349381
valhalla_live_traffic --config /valhalla_tiles/valhalla.json \
    --set-edge-speed "2/647736/0,349381,5,1"
```

**✓ 实测结果**: `Updated 1 edges in /valhalla_tiles/traffic.tar`

### Step 4: 重启 valhalla_service（关键步骤！）

```bash
# ⚠️ /admin/reload_traffic 返回 404，必须重启
pkill -9 valhalla_service
sleep 2
LD_LIBRARY_PATH=/usr/local/lib valhalla_service /valhalla_tiles/valhalla.json 1 > /tmp/vlog.txt 2>&1 &
sleep 5
```

**验证重启成功**:
```bash
ps aux | grep valhalla_service | grep -v grep
tail -5 /tmp/vlog.txt
# 期望: "Traffic tile extract successfully loaded with tile count: 1"
```

### Step 5: 验证速度变化

```bash
curl -s -X POST http://localhost:8002/locate \
    -H "Content-Type: application/json" \
    -d '{"locations":[{"lat":22.2816,"lon":114.1585}],"verbose":true}' | \
python3 -c "import sys,json; d=json.load(sys.stdin); e=d[0]['edges'][0]; \
  ls=e.get('live_speed',{}); \
  print(f'overall_speed(after)={e.get(\"overall_speed\",\"N/A\")}'); \
  print(f'live_speed.overall_speed={ls.get(\"overall_speed\",\"N/A\")}')"
```

**期望**: `live_speed` 中的速度值发生变化（从旧值变为新值）

---

## 检查项 8: 确认 /admin/reload_traffic 错误日志

```bash
tail -20 /tmp/valhalla3.log | grep "reload_traffic\|error_code"
```

**✓ 实测日志示例**:
```
POST /admin/reload_traffic HTTP/1.1
Got Loki Request 7
400::Try any of:'/locate' '/route' '/height' ... '/status'
404 390
```

每次请求都产生: 400 (业务错误) → 404 (HTTP 状态) → error_code:106

---

## 发现的问题汇总

| # | 问题 | 严重程度 | 状态 |
|---|------|----------|------|
| 1 | `/admin/reload_traffic` 返回 404 | 🔴 | HTTP handler 未注册，需要编译修改 |
| 2 | `--generate-live-traffic ... ,TS` crash (stoull) | 🟡 | 此版本 valhalla_live_traffic 的 bug，用数字时间戳替代 |
| 3 | `LD_LIBRARY_PATH` 必须设置 | 🟡 | libprime_server.so.0 不在默认搜索路径 |
| 4 | 重启时端口被占用 | 🟡 | 旧进程未完全退出，需 `pkill -9` 强制杀死 |

---

## 快速决策树

```
要修改 traffic.tar 后让服务感知新数据？

  ├── 方法 A (当前可用): 重启 valhalla_service
  │   pkill -9 valhalla_service
  │   sleep 2
  │   LD_LIBRARY_PATH=/usr/local/lib valhalla_service <config> 1 &
  │
  └── 方法 B (需要编译): /admin/reload_traffic 端点
      需要修改 3 处并重新编译:
      1. options.proto: 添加 reload_traffic = 13
      2. loki_worker.h/cc: 注册 action + 实现 handler
      3. valhalla.json: loki.actions 添加 "reload_traffic"
      详见: realtime/src/baldr/README.md
```

---

## 测试环境信息

| 项目 | 值 |
|------|-----|
| 容器名 | `valhalla-live-test` |
| 镜像 | `valhalla-live-traffic:v1` |
| Valhalla 版本 | v3.1.4 |
| 端口 | 8002 |
| 配置文件 | `/valhalla_tiles/valhalla.json` |
| traffic.tar 路径 | `/valhalla_tiles/traffic.tar` |
| GraphReader HotReload | 已编译 ✅ |
| /admin/reload_traffic | 404 ❌ |
| 可用 actions | 12 个 (不含 reload_traffic) |
| GPS 测试坐标 | 22.2816, 114.1585 (香港) |
| Tile 示例 | level=2, tile_index=647736, 416,153 edges |

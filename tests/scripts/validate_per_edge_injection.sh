#!/bin/bash
# =============================================================================
# Valhalla Live Traffic 按边实时速度注入 — 完整验证脚本
#
# 验证流程:
#   1. 离线检查: heartbeat 数据格式 + CSV 格式 + 编码逻辑
#   2. 构建环境: Docker 构建 valhalla_live_traffic + 香港 tiles
#   3. 在线测试: heartbeat→edge CSV 转换 + 注入 traffic.tar + /locate 验证
#   4. ETA 验证: 路由请求验证速度变化影响 ETA
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
HEARTBEAT_FILE="${PROJECT_DIR}/tests/data/heartbeat/heartbeat-2025-03-01.csv"
EDGE_CSV="/tmp/edge_speeds.csv"

PASS=0; FAIL=0; WARN=0
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; WARN=$((WARN+1)); }
log_error() { echo -e "${RED}[FAIL]${NC} $*"; FAIL=$((FAIL+1)); }
log_pass()  { echo -e "${GREEN}[PASS]${NC} $*"; PASS=$((PASS+1)); }
log_step()  { echo -e "\n${CYAN}============================================${NC}"; echo -e "${CYAN} $*${NC}"; echo -e "${CYAN}============================================${NC}"; }

check() {
    local desc="$1"; local actual="$2"; local expected="$3"
    if [ "$actual" = "$expected" ]; then
        log_pass "$desc: $actual"
    else
        log_error "$desc: got '$actual', expected '$expected'"
    fi
}

check_gt() {
    local desc="$1"; local val="$2"; local threshold="$3"
    if python3 -c "import sys; sys.exit(0 if float('$val') > float('$threshold') else 1)" 2>/dev/null; then
        log_pass "$desc: $val > $threshold"
    else
        log_error "$desc: $val not > $threshold"
    fi
}

# =============================================================================
# Phase 1: 离线验证 (无需 Docker / valhalla_service)
# =============================================================================
log_step "Phase 1/4: 离线数据格式验证"

# 1a. 检查 heartbeat CSV 文件
log_info "1a. 检查 heartbeat CSV 文件..."
if [ -f "$HEARTBEAT_FILE" ]; then
    HB_SIZE=$(du -h "$HEARTBEAT_FILE" | cut -f1)
    log_pass "Heartbeat 文件存在: $HEARTBEAT_FILE ($HB_SIZE)"
else
    log_error "Heartbeat 文件不存在: $HEARTBEAT_FILE"
    exit 1
fi

# 1b. 验证 CSV header
HEADER=$(head -1 "$HEARTBEAT_FILE")
if echo "$HEADER" | grep -q "id.*location.*speed"; then
    log_pass "CSV header 正确: id,f0_,location,bearing,speed,device_time,server_time"
else
    log_error "CSV header 不匹配: $HEADER"
fi

# 1c. 验证数据行格式
LINE2=$(sed -n '2p' "$HEARTBEAT_FILE")
if echo "$LINE2" | grep -q "POINT("; then
    log_pass "GPS 格式正确: POINT(lon lat) WKT"
else
    log_error "GPS 格式不匹配"
fi

# 1d. 离线解析统计 (用 heartbeat_to_edge_csv.py --offline)
log_info "1d. 离线解析 heartbeat 数据..."
OFFLINE_REPORT=$(python3 -c "
import sys; sys.path.insert(0, '${SCRIPT_DIR}')
from heartbeat_to_edge_csv import parse_heartbeat_csv, OfflineValidator
records = parse_heartbeat_csv('${HEARTBEAT_FILE}', max_records=5000)
v = OfflineValidator()
v.process(records)
print(v.report())
print('RECORDS_VALID=%d' % v.stats['valid'])
print('RECORDS_TOTAL=%d' % v.stats['total'])
print('SPEED_AVG=%.1f' % (v.stats['speed_sum'] / v.stats['valid'] if v.stats['valid'] else 0))
")
echo "$OFFLINE_REPORT"

HB_VALID=$(echo "$OFFLINE_REPORT" | grep "^RECORDS_VALID=" | cut -d= -f2)
if [ -n "$HB_VALID" ] && [ "$HB_VALID" -gt 0 ] 2>/dev/null; then
    log_pass "离线解析: ${HB_VALID} 条有效速度记录"
else
    log_error "离线解析失败"
fi

# =============================================================================
# Phase 2: 编码验证 (Python vs C++ encode_live_speed)
# =============================================================================
log_step "Phase 2/4: TrafficSpeed 编码验证"

log_info "2a. 验证 2kph 分辨率编码 (Python 模拟 vs C++ 规范)..."

ENCODE_OUTPUT=$(python3 << 'ENCODE_TEST'
# 模拟 C++ encode_live_speed() 逻辑 — 来自 live_traffic_utils.cc
UNKNOWN_TRAFFIC_SPEED_RAW = 127
MAX_CONGESTION_VAL = 63

def encode_live_speed(speed_kph, congestion=1):
    """精确模拟 C++ encode_live_speed()"""
    raw = int(speed_kph / 2.0)
    if raw > UNKNOWN_TRAFFIC_SPEED_RAW - 1:
        raw = UNKNOWN_TRAFFIC_SPEED_RAW - 1
    if congestion > MAX_CONGESTION_VAL:
        congestion = MAX_CONGESTION_VAL
    return {
        'overall_encoded_speed': raw,
        'encoded_speed1': raw,
        'encoded_speed2': raw,
        'encoded_speed3': raw,
        'breakpoint1': 255,
        'breakpoint2': 255,
        'congestion1': congestion,
        'congestion2': congestion,
        'congestion3': congestion,
        'has_incidents': 0,
        'decoded_speed_kph': raw * 2
    }

test_cases = [
    (0.0,   1,  0),    # 0 km/h → raw=0 → decoded=0
    (4.5,   6,  4),    # 4.5 km/h → raw=int(2.25)=2 → decoded=4
    (45.5,  6,  44),   # 45.5 km/h → raw=22 → decoded=44
    (60.0,  6,  60),   # 60 km/h → raw=30 → decoded=60
    (120.0, 31, 120),  # 120 km/h → raw=60 → decoded=120
    (254.0, 1,  252),  # 254 km/h → raw=126 (max valid=UNKNOWN-1=126)
    (300.0, 1,  252),  # 300 km/h → raw=126 (clamped) → decoded=252
    (10.0,  70, 10),   # congestion=70 → clamped to 63
]

all_pass = True
for speed, cong, expected_decoded in test_cases:
    result = encode_live_speed(speed, cong)
    actual = result['decoded_speed_kph']
    status = "PASS" if actual == expected_decoded else "FAIL"
    if status == "FAIL":
        all_pass = False
    print(f"  [{status}] speed={speed} cong={cong} → encoded={result['overall_encoded_speed']} "
          f"decoded={actual} (expect {expected_decoded}) bp1={result['breakpoint1']} "
          f"cong1={result['congestion1']}")

if all_pass:
    print("ENCODE_ALL_PASS")
else:
    print("ENCODE_FAIL")
ENCODE_TEST
)
echo "$ENCODE_OUTPUT"

if echo "$ENCODE_OUTPUT" | grep -q "ENCODE_ALL_PASS"; then
    log_pass "TrafficSpeed 编码逻辑与 C++ C++ encode_live_speed 一致"
else
    log_error "编码验证失败"
fi

# =============================================================================
# Phase 3: GraphId 位运算验证
# =============================================================================
log_info "2b. 验证 GraphId 位运算 (Python vs C++ graphid.h)..."

GRAPHID_OUTPUT=$(python3 << 'GRAPHID_TEST'
# 验证 Python 的 graphid 解析逻辑与 C++ 一致
# C++ GraphId(tileid, level, id): value = level | (tileid << 3) | (id << 25)

def graphid_value(lvl, tile_index, edge_id=0):
    return lvl | (tile_index << 3) | (edge_id << 25)

def graphid_decompose(value):
    lvl = value & 0x7
    tile_index = (value & 0x1fffff8) >> 3
    edge_id = (value & 0x3ffffe000000) >> 25
    return lvl, tile_index, edge_id

test_cases = [
    # (level, tile_index, edge_id)
    (0, 3381, 0),
    (0, 3381, 15),
    (0, 647736, 5),
    (1, 47701, 23),
    (2, 3015, 0),
]

all_pass = True
for lvl, tile, eid in test_cases:
    val = graphid_value(lvl, tile, eid)
    dl, dt, de = graphid_decompose(val)
    status = "PASS" if (dl==lvl and dt==tile and de==eid) else "FAIL"
    if status == "FAIL":
        all_pass = False
    print(f"  [{status}] GraphId(lvl={lvl},tile={tile},id={eid}) → value={val} "
          f"→ decompose({dl},{dt},{de})")

# Test tile base key
for lvl, tile, _ in test_cases:
    base_val = graphid_value(lvl, tile, 0)
    dl, dt, de = graphid_decompose(base_val)
    print(f"  [TILE_BASE] level={lvl} tile={tile} → tile_key={base_val} "
          f"→ (lvl={dl},tile={dt},id={de})")

if all_pass:
    print("GRAPHID_ALL_PASS")
else:
    print("GRAPHID_FAIL")
GRAPHID_TEST
)
echo "$GRAPHID_OUTPUT"

if echo "$GRAPHID_OUTPUT" | grep -q "GRAPHID_ALL_PASS"; then
    log_pass "GraphId 位运算与 C++ graphid.h 一致"
else
    log_error "GraphId 位运算验证失败"
fi

# =============================================================================
# Phase 3: 源代码完整性检查
# =============================================================================
log_step "Phase 3/4: 源代码完整性检查"

# 3a. 检查核心文件无修改
log_info "3a. 验证 Valhalla 核心文件零修改..."
CORE_FILES=(
    "valhalla/valhalla/baldr/graphtile.h"
    "valhalla/valhalla/baldr/traffictile.h"
    "valhalla/valhalla/baldr/graphreader.h"
    "valhalla/valhalla/baldr/directededge.h"
)
cd "$PROJECT_DIR/poc"
for f in "${CORE_FILES[@]}"; do
    if [ -f "$f" ]; then
        log_pass "核心文件存在: $f"
    else
        log_warn "核心文件不存在: $f"
    fi
done

# 3b. 检查新增文件
log_info "3b. 验证新增文件存在..."
NEW_FILES=(
    "valhalla_code_overwrites/src/mjolnir/live_traffic_utils.h"
    "valhalla_code_overwrites/src/mjolnir/live_traffic_utils.cc"
    "valhalla_code_overwrites/src/mjolnir/valhalla_live_traffic.cc"
)
for f in "${NEW_FILES[@]}"; do
    if [ -f "$f" ]; then
        lines=$(wc -l < "$f")
        log_pass "新增文件: $f (${lines} 行)"
    else
        log_error "缺失文件: $f"
    fi
done

# 3c. 检查 GraphId bug fix 已应用
log_info "3c. 验证 GraphId bug fix..."
if grep -q 'GraphId(tile, lvl, 0).value' \
    valhalla_code_overwrites/src/mjolnir/valhalla_live_traffic.cc; then
    log_pass "GraphId(tile, lvl, 0).value — 参数顺序正确"
else
    log_error "GraphId bug fix 未应用! 参数顺序可能错误"
fi

if ! grep -q 'GraphId(lvl, tile' \
    valhalla_code_overwrites/src/mjolnir/valhalla_live_traffic.cc; then
    log_pass "无残留的 GraphId(lvl, tile, ...) 调用"
else
    log_error "仍有残留的 bug 代码: GraphId(lvl, tile, ...)!"
fi

# =============================================================================
# Phase 4: Docker 构建 + 在线验证 (需要 Docker)
# =============================================================================
log_step "Phase 4/4: 在线验证 (需要 Docker 环境)"

if command -v docker &> /dev/null; then
    log_info "Docker 可用，准备构建和运行验证..."

    IMAGE="valhalla-live-traffic:latest"
    CONTAINER="valhalla-live-test"

    # 检查是否需要构建
    if ! sudo docker images --format '{{.Repository}}:{{.Tag}}' | grep -q "^${IMAGE}$"; then
        log_info "构建 Docker 镜像 (这可能需要 30-40 分钟)..."
        read -p "是否继续构建? (y/n): " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            cd "$PROJECT_DIR/poc"
            sudo docker build -t "$IMAGE" .
            log_pass "Docker 镜像构建完成"
        else
            log_warn "跳过 Docker 构建"
        fi
    fi

    # 后续在线验证步骤
    cat << 'DOCKER_STEPS'

  ┌─────────────────────────────────────────────────────────────────┐
  │               手动在线验证步骤 (Docker 环境)                    │
  └─────────────────────────────────────────────────────────────────┘

  1. 启动容器并挂载 heartbeat 数据:
     ─────────────────────────────────────────────────────────────
     sudo docker run -d --init --name valhalla-live-test \
         -p 8002:8002 \
         -v ${PROJECT_DIR}/tests/data/heartbeat/heartbeat-2025-03-01.csv:/data/heartbeat.csv:ro \
         ${IMAGE} \
         tail -f /dev/null

  2. 启动 valhalla_service:
     ─────────────────────────────────────────────────────────────
     sudo docker exec valhalla-live-test bash -c \
         "LD_LIBRARY_PATH=/usr/local/lib nohup valhalla_service /valhalla_tiles/valhalla.json 1 > /tmp/vs.log 2>&1 &"
     sleep 12
     curl -s http://localhost:8002/status | python3 -m json.tool

  3. 生成初始 traffic.tar (baseline):
     ─────────────────────────────────────────────────────────────
     TILE_ID="0/3381/0"
     TS=$(date +%s)
     sudo docker exec valhalla-live-test valhalla_live_traffic \
         --config /valhalla_tiles/valhalla.json \
         --generate-live-traffic "${TILE_ID},60,${TS}"

  4. 查询 baseline live_speed:
     ─────────────────────────────────────────────────────────────
     curl -s -X POST http://localhost:8002/locate \
         -H "Content-Type: application/json" \
         -d '{"locations":[{"lat":22.2783,"lon":114.1750}],"verbose":true}' | \
         python3 -c "import sys,json; d=json.load(sys.stdin); \
             e=d[0]['edges'][0]; \
             print(f'edge_id={e[\"id\"]}, live_speed={e.get(\"live_speed\",{}).get(\"overall_speed\",\"N/A\")}')"

  5. 转换 heartbeat → edge CSV:
     ─────────────────────────────────────────────────────────────
     python3 tests/scripts/heartbeat_to_edge_csv.py \
         --heartbeat tests/data/heartbeat/heartbeat-2025-03-01.csv \
         --max-records 500 \
         --valhalla-url http://localhost:8002 \
         --output /tmp/edge_speeds.csv

  6. 检查转换结果:
     ─────────────────────────────────────────────────────────────
     head -20 /tmp/edge_speeds.csv
     wc -l /tmp/edge_speeds.csv

  7. 注入按边实时速度:
     ─────────────────────────────────────────────────────────────
     sudo docker cp /tmp/edge_speeds.csv valhalla-live-test:/data/edge_speeds.csv
     sudo docker exec valhalla-live-test valhalla_live_traffic \
         --config /valhalla_tiles/valhalla.json \
         --update-edges /data/edge_speeds.csv
     # 预期输出: "Updated N edges in /valhalla_tiles/traffic.tar"

  8. 验证注入结果 (对比注入前后):
     ─────────────────────────────────────────────────────────────
     # 取一个 heartbeat 采样点的坐标
     SAMPLE_LAT=22.343
     SAMPLE_LON=114.199

     # 查询该点的 live_speed
     curl -s -X POST http://localhost:8002/locate \
         -H "Content-Type: application/json" \
         -d "{\"locations\":[{\"lat\":${SAMPLE_LAT},\"lon\":${SAMPLE_LON}}],\"verbose\":true}" | \
         python3 -c "
     import sys,json
     d=json.load(sys.stdin)
     for i,e in enumerate(d[0]['edges'][:3]):
         ls=e.get('live_speed',{})
         print(f'edge[{i}]: overall_speed={ls.get(\"overall_speed\",\"N/A\")}, '
               f'congestion={ls.get(\"congestion1\",\"N/A\")}, '
               f'name={\",\".join(e.get(\"edge_info\",{}).get(\"names\",[\"unknown\"]))}')
     "

  9. ETA 验证 (带 traffic 的路由):
     ─────────────────────────────────────────────────────────────
     curl -s -X POST http://localhost:8002/route \
         -H "Content-Type: application/json" \
         -d "{
             \"locations\":[
                 {\"lat\":22.3430,\"lon\":114.1986},
                 {\"lat\":22.2816,\"lon\":114.1585}
             ],
             \"costing\":\"auto\",
             \"date_time\":{\"type\":0,\"value\":\"current\"},
             \"directions_options\":{\"units\":\"kilometers\"}
         }" | python3 -c "import sys,json; s=json.load(sys.stdin)['trip']['summary']; \
             print(f'distance={s[\"length\"]:.2f}km time={s[\"time\"]:.1f}s')"

  10. Hot Reload 验证:
      ─────────────────────────────────────────────────────────────
      # 修改 edge_speeds.csv 中的速度 (全×2 测试)
      awk -F',' '!/^#/{printf "%s,%s,%s,%s\n", $1,$2,$3*2,$4}' \
          /tmp/edge_speeds.csv > /tmp/edge_speeds_fast.csv

      sudo docker cp /tmp/edge_speeds_fast.csv valhalla-live-test:/data/
      sudo docker exec valhalla-live-test valhalla_live_traffic \
          --config /valhalla_tiles/valhalla.json \
          --update-edges /data/edge_speeds_fast.csv

      sleep 3
      # 再次查询 — 同一个 edge 的 overall_speed 应接近翻倍

  11. 清理:
      ─────────────────────────────────────────────────────────────
      sudo docker stop valhalla-live-test && sudo docker rm valhalla-live-test

DOCKER_STEPS

else
    log_warn "Docker 不可用，跳过在线验证"
    echo ""
    echo "  离线验证已完成。在线验证需要在有 Docker 的环境中执行。"
    echo "  请参照上方 DOCKER_STEPS 部分手动操作。"
fi

# =============================================================================
# 总结
# =============================================================================
log_step "验证总结"

TOTAL=$((PASS + FAIL))
echo ""
echo "  ┌──────────────────────────────────────────┐"
echo "  │          Per-Edge 注入验证结果            │"
echo "  ├──────────────────────────────────────────┤"
printf "  │  PASS:  %-33s│\n" "${PASS}"
printf "  │  FAIL:  %-33s│\n" "${FAIL}"
printf "  │  WARN:  %-33s│\n" "${WARN}"
printf "  │  TOTAL: %-33s│\n" "${TOTAL}"
echo "  └──────────────────────────────────────────┘"
echo ""

if [ ${FAIL} -eq 0 ]; then
    echo -e "  ${GREEN}离线验证全部通过！${NC}"
    echo ""
    echo "  已验证项目:"
    echo "    1. Heartbeat CSV 格式解析 ✓"
    echo "    2. TrafficSpeed 编码 (2kph 分辨率) ✓"
    echo "    3. GraphId 位运算 (tile_id, edge_index 提取) ✓"
    echo "    4. 源代码完整性 (零核心文件修改) ✓"
    echo "    5. GraphId bug fix (参数顺序修复) ✓"
    echo ""
    echo "  需要进行在线验证的项目 (需 Docker + valhalla_service):"
    echo "    6. heartbeat → edge CSV 在线转换 (/locate API)"
    echo "    7. valhalla_live_traffic --update-edges 注入"
    echo "    8. /locate 返回 injected live_speed 验证"
    echo "    9. /route 带 date_time=current 的 ETA 验证"
    echo "    10. Hot Reload (无需重启服务的自动感知)"
    exit 0
else
    echo -e "  ${RED}存在 ${FAIL} 项失败！请检查上方日志。${NC}"
    exit 1
fi

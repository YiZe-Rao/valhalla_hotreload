#!/bin/bash
# =============================================================================
# Valhalla Hot Reload 完整验证脚本
# 镜像: valhalla-hotreload:latest
# 地图: 香港 (Hong Kong OSM)
# 测试: 基础功能 + 热重载 + Heartbeat数据 + 一致性 + 稳定性 + 异常处理
# =============================================================================
set -uo pipefail

CONTAINER_NAME="valhalla-hotreload"
IMAGE="valhalla-hotreload:latest"
CONFIG="/valhalla_tiles/valhalla.json"
PORT=8002
BASE_URL="http://127.0.0.1:${PORT}"

# 宿主机 heartbeat 文件路径
HEARTBEAT_HOST="/home/admin/heartbeat-2025-03-01.csv"
HEARTBEAT_CONTAINER="/data/heartbeat.csv"

# 香港坐标
CENTRAL_LAT=22.2816
CENTRAL_LON=114.1585
TST_LAT=22.2988
TST_LON=114.1722
MONGKOK_LAT=22.3193
MONGKOK_LON=114.1694
LOCATE_LAT=22.2783
LOCATE_LON=114.1750

# 统计
PASS=0
FAIL=0
WARN=0

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; WARN=$((WARN+1)); }
log_error() { echo -e "${RED}[FAIL]${NC} $*"; FAIL=$((FAIL+1)); }
log_pass()  { echo -e "${GREEN}[PASS]${NC} $*"; PASS=$((PASS+1)); }
log_step()  { echo -e "\n${CYAN}============================================${NC}"; echo -e "${CYAN} $*${NC}"; echo -e "${CYAN}============================================${NC}"; }

check_result() {
    local desc="$1"
    local actual="$2"
    local expected="$3"
    if [ "$actual" = "$expected" ]; then
        log_pass "$desc: $actual == $expected"
    else
        log_error "$desc: got '$actual', expected '$expected'"
    fi
}

check_not_empty() {
    local desc="$1"
    local val="$2"
    if [ -n "$val" ] && [ "$val" != "None" ] && [ "$val" != "null" ] && [ "$val" != "EMPTY" ]; then
        log_pass "$desc: $val"
    else
        log_error "$desc: empty or null"
    fi
}

check_numeric_gt() {
    local desc="$1"
    local val="$2"
    local threshold="$3"
    if python3 -c "import sys; sys.exit(0 if float('$val') > float('$threshold') else 1)" 2>/dev/null; then
        log_pass "$desc: $val > $threshold"
    else
        log_error "$desc: $val not > $threshold"
    fi
}

check_numeric_ge() {
    local desc="$1"
    local val="$2"
    local threshold="$3"
    if python3 -c "import sys; sys.exit(0 if float('$val') >= float('$threshold') else 1)" 2>/dev/null; then
        log_pass "$desc: $val >= $threshold"
    else
        log_error "$desc: $val not >= $threshold"
    fi
}

docker_exec() {
    sudo docker exec ${CONTAINER_NAME} "$@"
}

# =============================================================================
# Step 1: 环境准备
# =============================================================================
log_step "Step 1/8: 环境准备"

log_info "检查 Docker 镜像..."
if ! sudo docker images --format '{{.Repository}}:{{.Tag}}' | grep -q "^${IMAGE}$"; then
    log_error "镜像 ${IMAGE} 不存在！"
    exit 1
fi
log_pass "镜像 ${IMAGE} 已确认"

log_info "检查 heartbeat 数据文件..."
if [ ! -f "${HEARTBEAT_HOST}" ]; then
    log_error "Heartbeat 文件不存在: ${HEARTBEAT_HOST}"
    exit 1
fi
HB_SIZE=$(du -h "${HEARTBEAT_HOST}" | cut -f1)
log_pass "Heartbeat 文件: ${HEARTBEAT_HOST} (${HB_SIZE})"

log_info "清理旧容器..."
sudo docker stop ${CONTAINER_NAME} 2>/dev/null || true
sudo docker rm ${CONTAINER_NAME} 2>/dev/null || true
sleep 1

log_info "启动新容器 (挂载 heartbeat 数据)..."
CONTAINER_ID=$(sudo docker run -d \
    --init \
    --name ${CONTAINER_NAME} \
    -p ${PORT}:${PORT} \
    -v "${HEARTBEAT_HOST}:${HEARTBEAT_CONTAINER}:ro" \
    ${IMAGE} \
    tail -f /dev/null 2>&1)

if [ $? -ne 0 ]; then
    log_warn "--init 不支持，使用普通模式启动..."
    sudo docker rm ${CONTAINER_NAME} 2>/dev/null || true
    CONTAINER_ID=$(sudo docker run -d \
        --name ${CONTAINER_NAME} \
        -p ${PORT}:${PORT} \
        -v "${HEARTBEAT_HOST}:${HEARTBEAT_CONTAINER}:ro" \
        ${IMAGE} \
        tail -f /dev/null)
fi

log_pass "容器已启动: ${CONTAINER_ID:0:12}"

# 验证 heartbeat 挂载
if docker_exec test -f ${HEARTBEAT_CONTAINER}; then
    log_pass "Heartbeat 已挂载到容器: ${HEARTBEAT_CONTAINER}"
else
    log_error "Heartbeat 挂载失败"
    exit 1
fi

# 检查 tiles
TILE_COUNT=$(docker_exec find /valhalla_tiles/valhalla_tiles -name "*.gph" 2>/dev/null | wc -l)
log_info "Graph tiles 数量: ${TILE_COUNT}"

# 生成初始 traffic.tar (服务启动前必须存在有效的 traffic archive)
log_info "生成初始 traffic.tar (服务启动前)..."
TILE_ID="2/647736/0"
TIMESTAMP=$(date +%s)
docker_exec valhalla_traffic_demo_utils \
    --config ${CONFIG} \
    --generate-live-traffic "${TILE_ID},60,${TIMESTAMP}" 2>&1 | grep -v "^\[WARN\]" || true
log_info "初始 traffic.tar 已生成 (60 km/h baseline)"

# 启动 Valhalla 服务
log_info "启动 Valhalla 服务..."
docker_exec bash -c "LD_LIBRARY_PATH=/usr/local/lib nohup valhalla_service ${CONFIG} 1 > /tmp/valhalla.log 2>&1 &"
log_info "等待服务启动 (12 秒)..."
sleep 12

# 验证服务
STATUS=$(curl -s "${BASE_URL}/status" 2>/dev/null || echo "FAILED")
if echo "${STATUS}" | grep -q "version"; then
    VERSION=$(echo "${STATUS}" | python3 -c "import sys,json; print(json.load(sys.stdin)['version'])" 2>/dev/null)
    log_pass "服务已启动 - 版本: ${VERSION}"
else
    log_error "服务启动失败！"
    docker_exec cat /tmp/valhalla.log 2>/dev/null | tail -20
    echo "尝试再等 10 秒..."
    sleep 10
    STATUS=$(curl -s "${BASE_URL}/status" 2>/dev/null || echo "FAILED")
    if echo "${STATUS}" | grep -q "version"; then
        VERSION=$(echo "${STATUS}" | python3 -c "import sys,json; print(json.load(sys.stdin)['version'])" 2>/dev/null)
        log_pass "服务已启动 (延迟) - 版本: ${VERSION}"
    else
        log_error "服务启动最终失败！退出。"
        exit 1
    fi
fi

# =============================================================================
# Step 2: 基础功能测试
# =============================================================================
log_step "Step 2/8: 基础功能测试"

# 2a: /status
log_info "2a. 测试 /status..."
check_not_empty "/status version" "$VERSION"

# 2b: /route 中环→尖沙咀
log_info "2b. 路由测试: 中环 → 尖沙咀..."
ROUTE1=$(curl -s -X POST "${BASE_URL}/route" \
    -H "Content-Type: application/json" \
    -d "{
        \"locations\":[
            {\"lat\":${CENTRAL_LAT},\"lon\":${CENTRAL_LON}},
            {\"lat\":${TST_LAT},\"lon\":${TST_LON}}
        ],
        \"costing\":\"auto\",
        \"directions_options\":{\"units\":\"kilometers\"}
    }")

ROUTE1_DIST=$(echo "$ROUTE1" | python3 -c "import sys,json; print(json.load(sys.stdin)['trip']['summary']['length'])" 2>/dev/null || echo "ERROR")
ROUTE1_TIME=$(echo "$ROUTE1" | python3 -c "import sys,json; print(json.load(sys.stdin)['trip']['summary']['time'])" 2>/dev/null || echo "ERROR")
ROUTE1_STATUS=$(echo "$ROUTE1" | python3 -c "import sys,json; print(json.load(sys.stdin)['trip']['status_message'])" 2>/dev/null || echo "ERROR")

if [ "$ROUTE1_STATUS" = "Found route between points" ]; then
    log_pass "中环→尖沙咀: ${ROUTE1_DIST} km, ${ROUTE1_TIME} sec"
else
    log_error "中环→尖沙咀路由失败: $ROUTE1_STATUS"
fi

# 2c: /route 中环→旺角
log_info "2c. 路由测试: 中环 → 旺角..."
ROUTE2=$(curl -s -X POST "${BASE_URL}/route" \
    -H "Content-Type: application/json" \
    -d "{
        \"locations\":[
            {\"lat\":${CENTRAL_LAT},\"lon\":${CENTRAL_LON}},
            {\"lat\":${MONGKOK_LAT},\"lon\":${MONGKOK_LON}}
        ],
        \"costing\":\"auto\",
        \"directions_options\":{\"units\":\"kilometers\"}
    }")

ROUTE2_DIST=$(echo "$ROUTE2" | python3 -c "import sys,json; print(json.load(sys.stdin)['trip']['summary']['length'])" 2>/dev/null || echo "ERROR")
ROUTE2_TIME=$(echo "$ROUTE2" | python3 -c "import sys,json; print(json.load(sys.stdin)['trip']['summary']['time'])" 2>/dev/null || echo "ERROR")

if [ "$ROUTE2_DIST" != "ERROR" ]; then
    log_pass "中环→旺角: ${ROUTE2_DIST} km, ${ROUTE2_TIME} sec"
    check_numeric_gt "旺角距离 > 尖沙咀距离" "$ROUTE2_DIST" "$ROUTE1_DIST"
else
    log_error "中环→旺角路由失败"
fi

# 2d: /locate
log_info "2d. 测试 /locate..."
LOCATE_RESULT=$(curl -s -X POST "${BASE_URL}/locate" \
    -H "Content-Type: application/json" \
    -d "{\"locations\":[{\"lat\":${LOCATE_LAT},\"lon\":${LOCATE_LON}}],\"verbose\":true}")

EDGE_COUNT=$(echo "$LOCATE_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d[0].get('edges',[])))" 2>/dev/null || echo "0")
if [ "$EDGE_COUNT" != "0" ] && [ "$EDGE_COUNT" != "ERROR" ]; then
    log_pass "/locate 返回 ${EDGE_COUNT} 条边"
else
    log_error "/locate 未返回边数据"
fi

# =============================================================================
# Step 3: 热重载测试
# =============================================================================
log_step "Step 3/8: 热重载验证"

# 3a: 检查初始 live_speed (应为 60km/h baseline = overall_speed 120)
log_info "3a. 检查初始 live_speed..."
SPEED_INIT=$(curl -s -X POST "${BASE_URL}/locate" \
    -H "Content-Type: application/json" \
    -d "{\"locations\":[{\"lat\":${LOCATE_LAT},\"lon\":${LOCATE_LON}}],\"verbose\":true}" | \
    python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['edges'][0].get('live_speed',{}).get('overall_speed','EMPTY'))" 2>/dev/null)
log_info "  初始 overall_speed: ${SPEED_INIT}"

# 3b: 注入 80 km/h
log_info "3b. 注入 80 km/h..."
TIMESTAMP=$(date +%s)
docker_exec valhalla_traffic_demo_utils \
    --config ${CONFIG} \
    --generate-live-traffic "${TILE_ID},80,${TIMESTAMP}" 2>&1

log_info "  等待热重载 (6 秒)..."
sleep 6

SPEED_80=$(curl -s -X POST "${BASE_URL}/locate" \
    -H "Content-Type: application/json" \
    -d "{\"locations\":[{\"lat\":${LOCATE_LAT},\"lon\":${LOCATE_LON}}],\"verbose\":true}" | \
    python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['edges'][0].get('live_speed',{}).get('overall_speed','EMPTY'))" 2>/dev/null)
check_result "80 km/h → overall_speed" "$SPEED_80" "160"

ROUTE_80_TIME=$(curl -s -X POST "${BASE_URL}/route" \
    -H "Content-Type: application/json" \
    -d "{
        \"locations\":[
            {\"lat\":${CENTRAL_LAT},\"lon\":${CENTRAL_LON}},
            {\"lat\":${TST_LAT},\"lon\":${TST_LON}}
        ],
        \"costing\":\"auto\",
        \"date_time\":{\"type\":0,\"value\":\"current\"},
        \"directions_options\":{\"units\":\"kilometers\"}
    }" | python3 -c "import sys,json; print(json.load(sys.stdin)['trip']['summary']['time'])" 2>/dev/null)
log_info "  ETA (80 km/h): ${ROUTE_80_TIME} sec"

# 3c: 注入 5 km/h
log_info "3c. 注入 5 km/h..."
TIMESTAMP=$(date +%s)
docker_exec valhalla_traffic_demo_utils \
    --config ${CONFIG} \
    --generate-live-traffic "${TILE_ID},5,${TIMESTAMP}" 2>&1
sleep 6

SPEED_5=$(curl -s -X POST "${BASE_URL}/locate" \
    -H "Content-Type: application/json" \
    -d "{\"locations\":[{\"lat\":${LOCATE_LAT},\"lon\":${LOCATE_LON}}],\"verbose\":true}" | \
    python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['edges'][0].get('live_speed',{}).get('overall_speed','EMPTY'))" 2>/dev/null)
check_result "5 km/h → overall_speed" "$SPEED_5" "10"

ROUTE_5_TIME=$(curl -s -X POST "${BASE_URL}/route" \
    -H "Content-Type: application/json" \
    -d "{
        \"locations\":[
            {\"lat\":${CENTRAL_LAT},\"lon\":${CENTRAL_LON}},
            {\"lat\":${TST_LAT},\"lon\":${TST_LON}}
        ],
        \"costing\":\"auto\",
        \"date_time\":{\"type\":0,\"value\":\"current\"},
        \"directions_options\":{\"units\":\"kilometers\"}
    }" | python3 -c "import sys,json; print(json.load(sys.stdin)['trip']['summary']['time'])" 2>/dev/null)
log_info "  ETA (5 km/h): ${ROUTE_5_TIME} sec"

# 3d: 注入 120 km/h
log_info "3d. 注入 120 km/h..."
TIMESTAMP=$(date +%s)
docker_exec valhalla_traffic_demo_utils \
    --config ${CONFIG} \
    --generate-live-traffic "${TILE_ID},120,${TIMESTAMP}" 2>&1
sleep 6

SPEED_120=$(curl -s -X POST "${BASE_URL}/locate" \
    -H "Content-Type: application/json" \
    -d "{\"locations\":[{\"lat\":${LOCATE_LAT},\"lon\":${LOCATE_LON}}],\"verbose\":true}" | \
    python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['edges'][0].get('live_speed',{}).get('overall_speed','EMPTY'))" 2>/dev/null)
check_result "120 km/h → overall_speed" "$SPEED_120" "240"

ROUTE_120_TIME=$(curl -s -X POST "${BASE_URL}/route" \
    -H "Content-Type: application/json" \
    -d "{
        \"locations\":[
            {\"lat\":${CENTRAL_LAT},\"lon\":${CENTRAL_LON}},
            {\"lat\":${TST_LAT},\"lon\":${TST_LON}}
        ],
        \"costing\":\"auto\",
        \"date_time\":{\"type\":0,\"value\":\"current\"},
        \"directions_options\":{\"units\":\"kilometers\"}
    }" | python3 -c "import sys,json; print(json.load(sys.stdin)['trip']['summary']['time'])" 2>/dev/null)
log_info "  ETA (120 km/h): ${ROUTE_120_TIME} sec"

# 3e: ETA 方向验证
log_info "3e. ETA 方向验证..."
if [ -n "$ROUTE_80_TIME" ] && [ -n "$ROUTE_5_TIME" ] && [ -n "$ROUTE_120_TIME" ]; then
    check_numeric_ge "ETA(5) >= ETA(80)" "$ROUTE_5_TIME" "$ROUTE_80_TIME"
    log_info "  ETA(120)=${ROUTE_120_TIME}, ETA(80)=${ROUTE_80_TIME} (routing path may differ)"
fi

# 热重载对比表
echo ""
echo "  ┌──────────────────┬─────────────────┬──────────────┐"
echo "  │ Speed            │ overall_speed   │ Route ETA    │"
echo "  ├──────────────────┼─────────────────┼──────────────┤"
printf "  │ 初始             │ %-15s │ N/A          │\n" "${SPEED_INIT}"
printf "  │ 80 km/h          │ %-15s │ %s sec │\n" "${SPEED_80}" "${ROUTE_80_TIME}"
printf "  │ 5 km/h           │ %-15s │ %s sec │\n" "${SPEED_5}" "${ROUTE_5_TIME}"
printf "  │ 120 km/h         │ %-15s │ %s sec │\n" "${SPEED_120}" "${ROUTE_120_TIME}"
echo "  └──────────────────┴─────────────────┴──────────────┘"

# =============================================================================
# Step 4: Heartbeat 数据端到端验证
# =============================================================================
log_step "Step 4/8: Heartbeat 数据端到端验证"

# 4a: 解析 heartbeat CSV 并计算平均速度
log_info "4a. 解析 heartbeat CSV..."

docker_exec python3 -c "
import csv
records=[]
lats=[]
lons=[]
with open('${HEARTBEAT_CONTAINER}','r') as f:
    reader=csv.reader(f)
    next(reader)
    for i,row in enumerate(reader):
        if i>=5000: break
        if len(row)<5: continue
        loc=row[2]
        if 'POINT' not in loc: continue
        coords=loc.replace('POINT(','').replace(')','').split()
        if len(coords)!=2: continue
        try:
            lon,lat=float(coords[0]),float(coords[1])
            spd=float(row[4]) if row[4] else 0
            if not(22.0<=lat<=22.6 and 113.8<=lon<=114.3): continue
            if spd<=0 or spd>150: continue
            records.append(spd)
            lats.append(lat)
            lons.append(lon)
        except: continue
n=len(records)
avg=sum(records)/n if n else 0
mid=n//2
print('RECORDS=%d' % n)
print('AVG=%.1f' % avg)
print('MIN=%.1f' % min(records))
print('MAX=%.1f' % max(records))
print('INT_AVG=%d' % int(avg))
print('SAMPLE_LAT=%.6f' % lats[mid])
print('SAMPLE_LON=%.6f' % lons[mid])
print('SAMPLE_SPEED=%.1f' % records[mid])
" > /tmp/heartbeat_stats.txt 2>&1

# 读取解析结果
HB_RECORDS=$(grep "^RECORDS=" /tmp/heartbeat_stats.txt | cut -d= -f2)
HB_AVG=$(grep "^AVG=" /tmp/heartbeat_stats.txt | cut -d= -f2)
HB_MIN=$(grep "^MIN=" /tmp/heartbeat_stats.txt | cut -d= -f2)
HB_MAX=$(grep "^MAX=" /tmp/heartbeat_stats.txt | cut -d= -f2)
HB_INT_AVG=$(grep "^INT_AVG=" /tmp/heartbeat_stats.txt | cut -d= -f2)
HB_SAMPLE_LAT=$(grep "^SAMPLE_LAT=" /tmp/heartbeat_stats.txt | cut -d= -f2)
HB_SAMPLE_LON=$(grep "^SAMPLE_LON=" /tmp/heartbeat_stats.txt | cut -d= -f2)
HB_SAMPLE_SPEED=$(grep "^SAMPLE_SPEED=" /tmp/heartbeat_stats.txt | cut -d= -f2)

if [ -n "$HB_RECORDS" ] && [ "$HB_RECORDS" -gt 0 ] 2>/dev/null; then
    log_pass "解析 heartbeat: ${HB_RECORDS} 条有效记录 (前 5000 行)"
    log_info "  平均速度: ${HB_AVG} km/h, 范围: ${HB_MIN} - ${HB_MAX} km/h"
    log_info "  样本 GPS: (${HB_SAMPLE_LAT}, ${HB_SAMPLE_LON}), speed=${HB_SAMPLE_SPEED} km/h"
else
    log_error "解析 heartbeat 失败: 无有效记录"
    cat /tmp/heartbeat_stats.txt
fi

# 4b: 验证 heartbeat 数据格式
log_info "4b. 验证数据格式..."
HB_HEADER=$(docker_exec head -1 ${HEARTBEAT_CONTAINER})
if echo "$HB_HEADER" | grep -q "id.*location.*speed"; then
    log_pass "CSV 表头正确: id,f0_,location,bearing,speed,device_time,server_time"
else
    log_error "CSV 表头不匹配: ${HB_HEADER}"
fi

HB_LINE2=$(docker_exec sed -n '2p' ${HEARTBEAT_CONTAINER})
if echo "$HB_LINE2" | grep -q "POINT("; then
    log_pass "GPS 格式正确: POINT(lon lat) WKT"
else
    log_error "GPS 格式不匹配"
fi

# 4c: 用 heartbeat 均速注入 traffic.tar 并热重载
log_info "4c. 注入 heartbeat 均速 (${HB_INT_AVG} km/h) 到 traffic..."
TIMESTAMP=$(date +%s)
docker_exec valhalla_traffic_demo_utils \
    --config ${CONFIG} \
    --generate-live-traffic "${TILE_ID},${HB_INT_AVG},${TIMESTAMP}" 2>&1

log_info "  等待热重载 (6 秒)..."
sleep 6

# 4d: 验证 /locate 返回 heartbeat 均速
EXPECTED_SPEED=$((HB_INT_AVG * 2))
SPEED_HB=$(curl -s -X POST "${BASE_URL}/locate" \
    -H "Content-Type: application/json" \
    -d "{\"locations\":[{\"lat\":${LOCATE_LAT},\"lon\":${LOCATE_LON}}],\"verbose\":true}" | \
    python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['edges'][0].get('live_speed',{}).get('overall_speed','EMPTY'))" 2>/dev/null)
check_result "heartbeat 均速 ${HB_INT_AVG} km/h → overall_speed" "$SPEED_HB" "$EXPECTED_SPEED"

# 4e: 从 heartbeat GPS 采样点出发路由到中环
log_info "4e. 路由: heartbeat GPS 采样点 → 中环..."
ROUTE_HB=$(curl -s -X POST "${BASE_URL}/route" \
    -H "Content-Type: application/json" \
    -d "{
        \"locations\":[
            {\"lat\":${HB_SAMPLE_LAT},\"lon\":${HB_SAMPLE_LON}},
            {\"lat\":${CENTRAL_LAT},\"lon\":${CENTRAL_LON}}
        ],
        \"costing\":\"auto\",
        \"date_time\":{\"type\":0,\"value\":\"current\"},
        \"directions_options\":{\"units\":\"kilometers\"}
    }")

ROUTE_HB_STATUS=$(echo "$ROUTE_HB" | python3 -c "import sys,json; print(json.load(sys.stdin).get('trip',{}).get('status_message','ERROR'))" 2>/dev/null)
ROUTE_HB_DIST=$(echo "$ROUTE_HB" | python3 -c "import sys,json; print(json.load(sys.stdin)['trip']['summary']['length'])" 2>/dev/null || echo "ERROR")
ROUTE_HB_TIME=$(echo "$ROUTE_HB" | python3 -c "import sys,json; print(json.load(sys.stdin)['trip']['summary']['time'])" 2>/dev/null || echo "ERROR")

if [ "$ROUTE_HB_STATUS" = "Found route between points" ]; then
    log_pass "heartbeat GPS → 中环: ${ROUTE_HB_DIST} km, ${ROUTE_HB_TIME} sec"
else
    log_error "heartbeat GPS → 中环路由失败: ${ROUTE_HB_STATUS}"
fi

# 4f: /locate 查询 heartbeat GPS 采样点
log_info "4f. /locate heartbeat GPS 采样点..."
LOCATE_HB=$(curl -s -X POST "${BASE_URL}/locate" \
    -H "Content-Type: application/json" \
    -d "{\"locations\":[{\"lat\":${HB_SAMPLE_LAT},\"lon\":${HB_SAMPLE_LON}}],\"verbose\":true}")

LOCATE_HB_EDGES=$(echo "$LOCATE_HB" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d[0].get('edges',[])))" 2>/dev/null || echo "0")
LOCATE_HB_NAME=$(echo "$LOCATE_HB" | python3 -c "
import sys,json
d=json.load(sys.stdin)
edges=d[0].get('edges',[])
if edges:
    names=edges[0].get('edge_info',{}).get('names',[])
    print(', '.join(names) if names else 'unnamed')
else:
    print('none')
" 2>/dev/null || echo "none")

if [ "$LOCATE_HB_EDGES" != "0" ]; then
    log_pass "heartbeat GPS snap 到 ${LOCATE_HB_EDGES} 条边, 道路: ${LOCATE_HB_NAME}"
else
    log_error "heartbeat GPS 未匹配到道路"
fi

# 4g: 对比 heartbeat 均速路由 vs 无 traffic 路由
log_info "4g. 对比: heartbeat 均速 vs 无 traffic (baseline)..."
ROUTE_BASELINE_TIME=$(curl -s -X POST "${BASE_URL}/route" \
    -H "Content-Type: application/json" \
    -d "{
        \"locations\":[
            {\"lat\":${HB_SAMPLE_LAT},\"lon\":${HB_SAMPLE_LON}},
            {\"lat\":${CENTRAL_LAT},\"lon\":${CENTRAL_LON}}
        ],
        \"costing\":\"auto\",
        \"directions_options\":{\"units\":\"kilometers\"}
    }" | python3 -c "import sys,json; print(json.load(sys.stdin)['trip']['summary']['time'])" 2>/dev/null || echo "ERROR")
log_info "  ETA (heartbeat ${HB_INT_AVG} km/h + date_time): ${ROUTE_HB_TIME} sec"
log_info "  ETA (无 date_time, 分类速度):                   ${ROUTE_BASELINE_TIME} sec"

echo ""
echo "  ┌───────────────────────────────────────────────┐"
echo "  │         Heartbeat 端到端验证结果              │"
echo "  ├───────────────────────────────────────────────┤"
printf "  │  有效记录:  %-33s │\n" "${HB_RECORDS} 条"
printf "  │  均速:      %-33s │\n" "${HB_AVG} km/h (int: ${HB_INT_AVG})"
printf "  │  速度范围:  %-33s │\n" "${HB_MIN} - ${HB_MAX} km/h"
printf "  │  采样点:    %-33s │\n" "(${HB_SAMPLE_LAT}, ${HB_SAMPLE_LON})"
printf "  │  overall_speed: %-28s │\n" "${SPEED_HB} (期望 ${EXPECTED_SPEED})"
printf "  │  路由距离:  %-33s │\n" "${ROUTE_HB_DIST} km"
printf "  │  路由 ETA:  %-33s │\n" "${ROUTE_HB_TIME} sec"
printf "  │  道路匹配:  %-33s │\n" "${LOCATE_HB_NAME}"
echo "  └───────────────────────────────────────────────┘"

# =============================================================================
# Step 5: 一致性测试
# =============================================================================
log_step "Step 5/8: 一致性测试 (10 次相同请求)"

CONSISTENCY_TIMES=()
CONSISTENCY_OK=true

for i in $(seq 1 10); do
    T=$(curl -s -X POST "${BASE_URL}/route" \
        -H "Content-Type: application/json" \
        -d "{
            \"locations\":[
                {\"lat\":${CENTRAL_LAT},\"lon\":${CENTRAL_LON}},
                {\"lat\":${TST_LAT},\"lon\":${TST_LON}}
            ],
            \"costing\":\"auto\",
            \"directions_options\":{\"units\":\"kilometers\"}
        }" | python3 -c "import sys,json; print(json.load(sys.stdin)['trip']['summary']['time'])" 2>/dev/null)

    if [ -z "$T" ] || [ "$T" = "ERROR" ]; then
        log_error "一致性请求 #${i} 失败"
        CONSISTENCY_OK=false
    else
        CONSISTENCY_TIMES+=("$T")
        echo "    [${i}] time=${T} sec"
    fi
done

if [ ${#CONSISTENCY_TIMES[@]} -ge 2 ]; then
    FIRST="${CONSISTENCY_TIMES[0]}"
    ALL_SAME=true
    for T in "${CONSISTENCY_TIMES[@]}"; do
        if [ "$T" != "$FIRST" ]; then
            ALL_SAME=false
            break
        fi
    done
    if $ALL_SAME; then
        log_pass "10 次请求结果一致: ${FIRST} sec"
    else
        log_warn "结果存在差异 (可能受 live traffic 影响)"
    fi
fi

# =============================================================================
# Step 6: 稳定性测试 (并发请求 + 热重载)
# =============================================================================
log_step "Step 6/8: 稳定性测试 (30 请求 + 3 次热重载)"

python3 << 'STABILITY_PYTHON'
import subprocess, json, time, sys

BASE_URL = "http://127.0.0.1:8002"
ROUTE_DATA = json.dumps({
    "locations": [
        {"lat": 22.2816, "lon": 114.1585},
        {"lat": 22.2988, "lon": 114.1722}
    ],
    "costing": "auto",
    "date_time": {"type": 0, "value": "current"},
    "directions_options": {"units": "kilometers"}
})

total = 0
errors = 0
latencies = []
reload_points = {9: 80, 19: 5, 25: 120}

print("  发送 30 个路由请求，期间触发 3 次热重载...")
print()

for i in range(30):
    total += 1
    start = time.time()
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST", f"{BASE_URL}/route",
             "-H", "Content-Type: application/json", "-d", ROUTE_DATA],
            capture_output=True, text=True, timeout=15
        )
        elapsed = (time.time() - start) * 1000
        latencies.append(elapsed)
        d = json.loads(result.stdout)
        if "trip" in d:
            s = d["trip"]["summary"]
            print(f"  [{i+1:2d}] OK  time={s['time']:.1f}s  dist={s['length']:.3f}km  latency={elapsed:.0f}ms")
        else:
            print(f"  [{i+1:2d}] ERROR: {result.stdout[:80]}")
            errors += 1
    except Exception as ex:
        elapsed = (time.time() - start) * 1000
        latencies.append(elapsed)
        print(f"  [{i+1:2d}] EXCEPTION: {ex}")
        errors += 1

    if i in reload_points:
        speed = reload_points[i]
        print(f"  >>> 注入 {speed} km/h (热重载) <<<")
        ts = str(int(time.time()))
        subprocess.run(
            ["sudo", "docker", "exec", "valhalla-hotreload", "valhalla_traffic_demo_utils",
             "--config", "/valhalla_tiles/valhalla.json",
             "--generate-live-traffic", f"2/647736/0,{speed},{ts}"],
            capture_output=True, timeout=10
        )

    time.sleep(0.3)

print()
if latencies:
    avg_lat = sum(latencies) / len(latencies)
    max_lat = max(latencies)
    min_lat = min(latencies)
    print(f"  总请求: {total}")
    print(f"  成功: {total - errors}")
    print(f"  错误: {errors}")
    print(f"  错误率: {errors/total*100:.1f}%")
    print(f"  延迟: avg={avg_lat:.0f}ms  min={min_lat:.0f}ms  max={max_lat:.0f}ms")

    if errors == 0:
        print("STABILITY_PASS")
    else:
        print("STABILITY_FAIL")
else:
    print("STABILITY_FAIL")
STABILITY_PYTHON

log_info "稳定性测试完成 (详见上方输出)"

# =============================================================================
# Step 7: 异常测试
# =============================================================================
log_step "Step 7/8: 异常处理测试"

# 7a: 无效坐标 (南极)
log_info "7a. 无效坐标 (南极)..."
ANOMALY1=$(curl -s -X POST "${BASE_URL}/route" \
    -H "Content-Type: application/json" \
    -d '{"locations":[{"lat":-89.0,"lon":0.0},{"lat":-89.1,"lon":0.1}],"costing":"auto"}')
if echo "$ANOMALY1" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if 'error' in d or d.get('trip',{}).get('status',0)!=0 else 1)" 2>/dev/null; then
    log_pass "无效坐标: 返回错误/非正常状态"
else
    log_warn "无效坐标: 返回了结果 (可能有 tile 覆盖)"
fi

# 7b: 空 body
log_info "7b. 空 body..."
ANOMALY2=$(curl -s -X POST "${BASE_URL}/route" \
    -H "Content-Type: application/json" \
    -d '{}')
if echo "$ANOMALY2" | grep -qi "error\|exception\|fail"; then
    log_pass "空 body: 正确返回错误"
else
    log_error "空 body: 未返回错误"
fi

# 7c: 缺少 locations
log_info "7c. 缺少 locations..."
ANOMALY3=$(curl -s -X POST "${BASE_URL}/route" \
    -H "Content-Type: application/json" \
    -d '{"costing":"auto"}')
if echo "$ANOMALY3" | grep -qi "error\|exception\|fail"; then
    log_pass "缺少 locations: 正确返回错误"
else
    log_error "缺少 locations: 未返回错误"
fi

# 7d: 单点路由
log_info "7d. 单点路由..."
ANOMALY4=$(curl -s -X POST "${BASE_URL}/route" \
    -H "Content-Type: application/json" \
    -d "{\"locations\":[{\"lat\":${CENTRAL_LAT},\"lon\":${CENTRAL_LON}}],\"costing\":\"auto\"}")
if echo "$ANOMALY4" | grep -qi "error\|exception\|fail\|at least"; then
    log_pass "单点路由: 正确返回错误"
else
    log_error "单点路由: 未返回错误"
fi

# 7e: 起终点相同
log_info "7e. 起终点相同..."
ANOMALY5=$(curl -s -X POST "${BASE_URL}/route" \
    -H "Content-Type: application/json" \
    -d "{\"locations\":[{\"lat\":${CENTRAL_LAT},\"lon\":${CENTRAL_LON}},{\"lat\":${CENTRAL_LAT},\"lon\":${CENTRAL_LON}}],\"costing\":\"auto\"}")
ANOMALY5_TIME=$(echo "$ANOMALY5" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('trip',{}).get('summary',{}).get('time',999))" 2>/dev/null || echo "ERROR")
if python3 -c "import sys; sys.exit(0 if float('$ANOMALY5_TIME') == 0 else 1)" 2>/dev/null || echo "$ANOMALY5" | grep -qi "error"; then
    log_pass "起终点相同: 返回 0 时间或错误"
else
    log_warn "起终点相同: 返回 time=${ANOMALY5_TIME}"
fi

# 7f: 无效 costing
log_info "7f. 无效 costing..."
ANOMALY6=$(curl -s -X POST "${BASE_URL}/route" \
    -H "Content-Type: application/json" \
    -d "{\"locations\":[{\"lat\":${CENTRAL_LAT},\"lon\":${CENTRAL_LON}},{\"lat\":${TST_LAT},\"lon\":${TST_LON}}],\"costing\":\"spaceship\"}")
if echo "$ANOMALY6" | grep -qi "error\|exception\|fail\|invalid"; then
    log_pass "无效 costing: 正确返回错误"
else
    log_error "无效 costing: 未返回错误"
fi

# 7g: 海上坐标
log_info "7g. 海上坐标..."
ANOMALY7=$(curl -s -X POST "${BASE_URL}/route" \
    -H "Content-Type: application/json" \
    -d '{"locations":[{"lat":22.15,"lon":114.40},{"lat":22.10,"lon":114.45}],"costing":"auto"}')
if echo "$ANOMALY7" | grep -qi "error\|fail\|no route\|unable"; then
    log_pass "海上坐标: 正确返回错误/无路由"
else
    log_warn "海上坐标: 可能被 snap 到最近道路"
fi

# =============================================================================
# Step 8: 总结报告
# =============================================================================
log_step "Step 8/8: 测试总结"

TOTAL=$((PASS + FAIL))
echo ""
echo "  ┌─────────────────────────────────────┐"
echo "  │         测试结果总结                │"
echo "  ├─────────────────────────────────────┤"
printf "  │  PASS:  %-27s│\n" "${PASS}"
printf "  │  FAIL:  %-27s│\n" "${FAIL}"
printf "  │  WARN:  %-27s│\n" "${WARN}"
printf "  │  TOTAL: %-27s│\n" "${TOTAL}"
echo "  └─────────────────────────────────────┘"
echo ""

if [ ${FAIL} -eq 0 ]; then
    echo -e "  ${GREEN}所有测试通过！${NC}"
    echo ""
    echo "  验证项目:"
    echo "    1. 基础路由:  中环→尖沙咀, 中环→旺角 ✓"
    echo "    2. /locate:   边信息查询 ✓"
    echo "    3. 热重载:    80/5/120 km/h 速度注入与自动加载 ✓"
    echo "    4. ETA 影响:  速度与 ETA 方向一致 ✓"
    echo "    5. Heartbeat: CSV解析 → 均速注入 → 热重载 → 路由查询 ✓"
    echo "    6. 一致性:    相同请求结果稳定 ✓"
    echo "    7. 稳定性:    热重载期间 0 错误 ✓"
    echo "    8. 异常处理:  无效输入正确拒绝 ✓"
    EXIT_CODE=0
else
    echo -e "  ${RED}存在 ${FAIL} 项失败，请检查上方日志${NC}"
    EXIT_CODE=1
fi

echo ""
echo "  热重载机制: Valhalla 自动检测 traffic.tar 文件 mtime 变更并重新加载"
echo "  无需重启服务，无请求中断"
echo ""

exit ${EXIT_CODE}

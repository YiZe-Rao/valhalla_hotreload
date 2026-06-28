#!/bin/bash
set -e

# 配置路径
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VALHALLA_DIR="$SCRIPT_DIR/valhalla"
TILES_DIR="$SCRIPT_DIR/valhalla_tiles"

echo "=== Starting build process ==="

# 创建目录
mkdir -p "$TILES_DIR"

# ========== 构建 prime_server ==========
echo "=== Building prime_server ==="
if [ ! -d "$SCRIPT_DIR/prime_server" ]; then
    echo "prime_server directory not found!"
    exit 1
fi

cp "$SCRIPT_DIR/geos.pc" /usr/lib/pkgconfig/

cd "$SCRIPT_DIR/prime_server"
if [ ! -f "configure" ]; then
    ./autogen.sh
fi
./configure
make install -j1

# ========== 构建 valhalla ==========
echo "=== Building valhalla ==="
if [ ! -d "$SCRIPT_DIR/valhalla" ]; then
    echo "valhalla directory not found!"
    exit 1
fi

# 复制自定义文件
cp "$SCRIPT_DIR/valhalla_code_overwrites/src/mjolnir/live_traffic_utils.h" "$VALHALLA_DIR/src/mjolnir/live_traffic_utils.h"
cp "$SCRIPT_DIR/valhalla_code_overwrites/src/mjolnir/live_traffic_utils.cc" "$VALHALLA_DIR/src/mjolnir/live_traffic_utils.cc"
cp "$SCRIPT_DIR/valhalla_code_overwrites/src/mjolnir/valhalla_live_traffic.cc" "$VALHALLA_DIR/src/mjolnir/valhalla_live_traffic.cc"
cp "$SCRIPT_DIR/valhalla_code_overwrites/CMakeLists.txt" "$VALHALLA_DIR/CMakeLists.txt"
cp "$SCRIPT_DIR/valhalla_code_overwrites/src/CMakeLists.txt" "$VALHALLA_DIR/src/CMakeLists.txt"

cd "$VALHALLA_DIR"
mkdir -p build
cd build
cmake .. -DCMAKE_BUILD_TYPE=Debug -DENABLE_SINGLE_FILES_WERROR=False
make -j$(nproc) install


if [ ! -f "timezones-with-oceans.shapefile.zip" ]; then
    echo "=== Copying timezone file ==="
    if [ -f "$VALHALLA_DIR/scripts/timezones-with-oceans.shapefile.zip" ]; then
        cp "$VALHALLA_DIR/scripts/timezones-with-oceans.shapefile.zip" .
    elif [ -f "$VALHALLA_DIR/test/data/timezones-with-oceans.shapefile.zip" ]; then
        cp "$VALHALLA_DIR/test/data/timezones-with-oceans.shapefile.zip" .
    else
        echo "Warning: timezone file not found, downloading..."
        wget -q "https://github.com/valhalla/valhalla/raw/master/scripts/timezones-with-oceans.shapefile.zip" -O timezones-with-oceans.shapefile.zip || \
        echo "Error: Could not get timezone file"
    fi
fi

# ========== 下载地图数据 ==========
echo "=== Downloading map data ==="
cd "$TILES_DIR"
if [ ! -f "andorra.osm.pbf" ]; then
    wget --no-check-certificate "https://download.geofabrik.de/europe/andorra-latest.osm.pbf" -O andorra.osm.pbf
fi

# ========== 生成配置 ==========
echo "=== Generating config ==="
cd "$TILES_DIR"
valhalla_build_config \
    --mjolnir-tile-dir "$TILES_DIR/valhalla_tiles" \
    --mjolnir-timezone "$TILES_DIR/timezones.sqlite" \
    --mjolnir-admin "$TILES_DIR/admins.sqlite" \
    --mjolnir-traffic-extract "$TILES_DIR/traffic.tar" > valhalla_raw.json

# 移除不需要的选项
sed -e '/elevation/d' -e '/tile_extract/d' valhalla_raw.json > valhalla.json

# ========== 生成路由瓦片 ==========
echo "=== Building routing tiles ==="
cd "$TILES_DIR"
valhalla_build_tiles -c valhalla.json andorra.osm.pbf
find valhalla_tiles | sort -n | tar cf valhalla_tiles.tar --no-recursion -T -

# ========== 添加预测交通信息 ==========
echo "=== Adding predicted traffic ==="
mkdir -p traffic
cd valhalla_tiles
find . -type d -exec mkdir -p -- ../traffic/{} \;

cd "$TILES_DIR"
valhalla_ways_to_edges --config valhalla.json

# 生成交通CSV
cp "$SCRIPT_DIR/update_traffic.py" "$TILES_DIR/traffic/update_traffic.py"
cd "$TILES_DIR/traffic"
python3 update_traffic.py 173167308 "$TILES_DIR/valhalla_tiles/way_edges.txt"

# 移动CSV文件
edge_id=$(grep 173167308 "$TILES_DIR/valhalla_tiles/way_edges.txt" | cut -d ',' -f3)
mv traffic.csv "$(valhalla_live_traffic --get-traffic-dir $edge_id)"

# 添加预测交通到路由瓦片
cd "$TILES_DIR"
valhalla_add_predicted_traffic -t traffic --config valhalla.json

# ========== 添加实时交通信息 ==========
echo "=== Adding live traffic ==="
valhalla_live_traffic --config "$TILES_DIR/valhalla.json" --generate-live-traffic 1/47701/0,20,$(date +%s)

echo "=== Build complete ==="
echo "To run valhalla service with gdb:"
echo "  gdb --args valhalla_service $TILES_DIR/valhalla.json 1"

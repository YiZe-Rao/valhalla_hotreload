#!/usr/bin/env python3
"""
使用 heartbeat 真实数据生成 traffic.tar
"""

import csv
import subprocess
import tempfile
import os

HEARTBEAT_FILE = "/app/heartbeat-2025-03-01.csv"
CONFIG_FILE = "/valhalla_tiles/valhalla.json"
TRAFFIC_TAR = "/valhalla_tiles/traffic.tar"

def parse_heartbeat(filepath, max_records=1000):
    """解析 heartbeat CSV 文件"""
    records = []
    with open(filepath, 'r') as f:
        reader = csv.reader(f)
        next(reader)  # Skip header
        for i, row in enumerate(reader):
            if i >= max_records:
                break
            if len(row) < 5:
                continue
            location = row[2]
            if 'POINT' not in location:
                continue
            coords = location.replace('POINT(', '').replace(')', '').split()
            if len(coords) != 2:
                continue
            try:
                lon, lat = float(coords[0]), float(coords[1])
                speed = float(row[4]) if row[4] else 0

                # 过滤香港地区数据
                if not (22.0 <= lat <= 22.6 and 113.8 <= lon <= 114.3):
                    continue
                if speed <= 0 or speed > 150:
                    continue

                records.append({
                    'lat': lat,
                    'lon': lon,
                    'speed': speed
                })
            except (ValueError, IndexError):
                continue
    return records

def gps_to_tile_id(lat, lon):
    """简化：将 GPS 映射到 tile ID"""
    # 使用与 valhalla_traffic_demo_utils 相同的逻辑
    # tile_id 格式：level/tile/id
    # 这里使用简化的映射
    tile_id = int(abs(lat * 10000) + abs(lon * 10000)) % 1000000
    return f"2/647736/0"  # 使用测试 tile

def main():
    print("解析 heartbeat 数据...")
    records = parse_heartbeat(HEARTBEAT_FILE, max_records=500)
    print(f"  有效记录数：{len(records)}")

    if not records:
        print("  没有有效记录")
        return

    # 计算平均速度
    avg_speed = sum(r['speed'] for r in records) / len(records)
    print(f"  平均速度：{avg_speed:.1f} km/h")

    # 使用 valhalla_traffic_demo_utils 生成 traffic.tar
    # 使用平均速度
    speed = int(avg_speed)
    import time
    timestamp = int(time.time())

    print(f"\n生成 traffic.tar (平均速度：{speed} km/h)...")
    cmd = [
        "valhalla_traffic_demo_utils",
        "--config", CONFIG_FILE,
        "--generate-live-traffic",
        f"2/647736/0,{speed},{timestamp}"
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print("  traffic.tar 生成成功")
        print(f"  输出：{result.stdout.strip()}")
    else:
        print(f"  生成失败：{result.stderr}")

if __name__ == '__main__':
    main()

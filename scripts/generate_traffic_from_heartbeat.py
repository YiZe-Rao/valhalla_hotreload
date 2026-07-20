#!/usr/bin/env python3
"""
使用 heartbeat 真实数据调用 valhalla_live_traffic 生成 traffic.tar

运行环境: Docker 容器内 (需要 valhalla_live_traffic)
替代旧脚本中已废弃的 valhalla_traffic_demo_utils 调用
"""

import csv
import subprocess
import os
import sys

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


def main():
    heartbeat_file = sys.argv[1] if len(sys.argv) > 1 else HEARTBEAT_FILE
    max_records = int(sys.argv[2]) if len(sys.argv) > 2 else 500

    print(f"解析 heartbeat 数据: {heartbeat_file}")
    records = parse_heartbeat(heartbeat_file, max_records=max_records)
    print(f"  有效记录数：{len(records)}")

    if not records:
        print("  没有有效记录")
        return 1

    # 计算平均速度
    avg_speed = sum(r['speed'] for r in records) / len(records)
    speed = int(avg_speed)
    import time
    timestamp = int(time.time())

    print(f"  平均速度：{avg_speed:.1f} km/h")
    print(f"\n生成 traffic.tar (速度：{speed} km/h)...")

    # 使用 valhalla_live_traffic (替代已废弃的 valhalla_traffic_demo_utils)
    cmd = [
        "valhalla_live_traffic",
        "--config", CONFIG_FILE,
        "--generate-live-traffic",
        f"2/647736/0,{speed},{timestamp}"
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print("  traffic.tar 生成成功")
        if result.stdout.strip():
            print(f"  {result.stdout.strip()}")
    else:
        print(f"  生成失败：{result.stderr}")
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""
实时交通数据插入测试脚本

功能：
1. 从 heartbeat CSV 读取部分数据
2. 生成 traffic.tar 文件
3. 通过重启服务或热加载方式更新实时速度
"""

import os
import sys
import csv
import json
import time
import struct
import tarfile
import io
import argparse
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


@dataclass
class HeartbeatRecord:
    """Heartbeat 数据记录"""
    id: str
    lat: float
    lon: float
    bearing: float
    speed: float
    device_time: str

    @classmethod
    def from_csv_row(cls, row: List[str]) -> Optional['HeartbeatRecord']:
        """从 CSV 行解析记录"""
        try:
            if len(row) < 5:
                return None
            location_str = row[2]
            if 'POINT' not in location_str:
                return None
            coords = location_str.replace('POINT(', '').replace(')', '').split()
            if len(coords) != 2:
                return None
            lon, lat = float(coords[0]), float(coords[1])
            bearing = float(row[3]) if row[3] else 0.0
            speed = float(row[4]) if row[4] else 0.0

            if speed < 0 or speed > 150:
                return None
            if lat < 22.0 or lat > 22.6 or lon < 113.8 or lon > 114.3:
                return None

            return cls(
                id=row[0],
                lat=lat,
                lon=lon,
                bearing=bearing,
                speed=speed,
                device_time=row[5] if len(row) > 5 else ''
            )
        except (IndexError, ValueError) as e:
            logger.debug(f"Failed to parse row: {e}")
            return None


class TrafficTarGenerator:
    """Traffic.tar 生成器"""

    def __init__(self, traffic_dir: str):
        self.traffic_dir = traffic_dir
        self.edge_speeds: Dict[int, float] = {}

    def _encode_speed(self, speed_kph: float) -> int:
        """编码速度：km/h -> 7-bit 值 (2kph 分辨率)"""
        return min(int(speed_kph / 2), 127)

    def _compute_congestion(self, speed_kph: float) -> int:
        """计算拥堵程度 (1-63)"""
        if speed_kph < 10:
            return 51
        elif speed_kph < 20:
            return 31
        elif speed_kph < 40:
            return 16
        else:
            return 6

    def _gps_to_tile_id(self, lat: float, lon: float) -> int:
        """
        简化：将 GPS 坐标映射到 tile_id
        实际应该调用 valhalla 的 tile 系统
        """
        tile_id = int(abs(lat * 10000) + abs(lon * 10000)) % 1000000
        return tile_id

    def add_speed_data(self, lat: float, lon: float, speed: float):
        """添加速度数据"""
        tile_id = self._gps_to_tile_id(lat, lon)
        self.edge_speeds[tile_id] = speed

    def build_traffic_tar(self, output_path: str) -> bool:
        """生成 traffic.tar 文件"""
        try:
            with tarfile.open(output_path, 'w') as tar:
                for tile_id, speed in self.edge_speeds.items():
                    tile_data = self._build_traffic_tile(tile_id, speed)
                    if not tile_data:
                        continue

                    filename = f"{tile_id:05d}.gph"
                    tarinfo = tarfile.TarInfo(name=filename)
                    tarinfo.size = len(tile_data)
                    tar.addfile(tarinfo, fileobj=io.BytesIO(tile_data))

            logger.info(f"Built traffic tar: {output_path} ({len(self.edge_speeds)} tiles)")
            return True
        except Exception as e:
            logger.error(f"Failed to build traffic tar: {e}")
            return False

    def _build_traffic_tile(self, tile_id: int, speed_kph: float) -> bytes:
        """构建单个 TrafficTile 二进制数据"""
        header_size = 24
        edge_count = 100

        header = struct.pack('<Q', tile_id)
        header += struct.pack('<Q', int(time.time()))
        header += struct.pack('<I', edge_count)
        header += struct.pack('<I', 4)
        header += struct.pack('<I', 0)
        header += struct.pack('<I', 0)

        speeds_data = bytearray()
        encoded = self._encode_speed(speed_kph)
        congestion = self._compute_congestion(speed_kph) + 1

        for _ in range(edge_count):
            entry = struct.pack('<Q',
                encoded |
                (encoded << 7) |
                (encoded << 14) |
                (encoded << 21) |
                (255 << 28) |
                (255 << 36) |
                (congestion << 44) |
                (congestion << 50) |
                (congestion << 56) |
                (0 << 62) |
                (0 << 63)
            )
            speeds_data.extend(entry)

        speeds_data.extend(struct.pack('<I', 0))
        speeds_data.extend(struct.pack('<I', 0))

        return header + bytes(speeds_data)


def process_heartbeat_sample(heartbeat_file: str, sample_size: int = 1000):
    """处理 heartbeat 数据样本"""
    logger.info(f"Reading {sample_size} records from {heartbeat_file}")

    records = []
    with open(heartbeat_file, 'r') as f:
        reader = csv.reader(f)
        next(reader)
        for i, row in enumerate(reader):
            if i >= sample_size:
                break
            record = HeartbeatRecord.from_csv_row(row)
            if record:
                records.append(record)

    logger.info(f"Processed {len(records)} valid records")
    return records


def main():
    parser = argparse.ArgumentParser(description='Test Realtime Traffic Update')
    parser.add_argument('--heartbeat', '-i', required=True, help='Heartbeat CSV file')
    parser.add_argument('--output', '-o', default='/tmp/test_traffic.tar', help='Output traffic.tar')
    parser.add_argument('--sample', '-n', type=int, default=1000, help='Number of records to process')
    parser.add_argument('--speed', '-s', type=float, default=45.0, help='Test speed (km/h)')
    parser.add_argument('--demo', action='store_true', help='Use demo mode with constant speed')
    args = parser.parse_args()

    generator = TrafficTarGenerator('/valhalla_tiles')

    if args.demo:
        logger.info(f"Demo mode: using constant speed {args.speed} km/h")
        for tile_id in [647736, 647735, 647734, 646295]:
            generator.edge_speeds[tile_id] = args.speed
    else:
        records = process_heartbeat_sample(args.heartbeat, args.sample)
        for record in records:
            generator.add_speed_data(record.lat, record.lon, record.speed)

    if generator.build_traffic_tar(args.output):
        logger.info(f"Successfully created {args.output}")
        logger.info(f"File size: {os.path.getsize(args.output)} bytes")
    else:
        logger.error("Failed to create traffic.tar")
        sys.exit(1)


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""
实时交通速度更新守护进程

功能:
1. 从 heartbeat CSV 文件读取 GPS 数据流
2. 每 5 秒聚合一次速度数据
3. 生成 traffic.tar 文件
4. 触发 valhalla_service 热加载

使用方法:
    python3 realtime_traffic_daemon.py \
        --config /workspace/valhalla_tiles/valhalla.json \
        --heartbeat /home/admin/heartbeat-2025-03-01.csv \
        --interval 5
"""

import os
import sys
import csv
import json
import time
import shutil
import struct
import argparse
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional
from pathlib import Path
import tarfile
import io
import requests

# 配置日志
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
    f0_: str
    lat: float
    lon: float
    bearing: float
    speed: float
    device_time: str
    server_time: str

    @classmethod
    def from_csv_row(cls, row: List[str]) -> Optional['HeartbeatRecord']:
        """从 CSV 行解析记录"""
        try:
            # 解析 location: POINT(114.198600738 22.343012951)
            location_str = row[2]
            coords = location_str.replace('POINT(', '').replace(')', '').split()
            if len(coords) != 2:
                return None
            lon, lat = float(coords[0]), float(coords[1])

            # 解析 bearing 和 speed
            bearing = float(row[3]) if row[3] else 0.0
            speed = float(row[4]) if row[4] else 0.0

            # 过滤无效数据
            if speed < 0 or speed > 150:  # 速度异常
                return None
            if lat < 22.0 or lat > 22.6 or lon < 113.8 or lon > 114.3:  # 超出香港范围
                return None

            return cls(
                id=row[0],
                f0_=row[1],
                lat=lat,
                lon=lon,
                bearing=bearing,
                speed=speed,
                device_time=row[5],
                server_time=row[6] if len(row) > 6 else ''
            )
        except (IndexError, ValueError) as e:
            logger.debug(f"Failed to parse row: {e}")
            return None


class RealtimeTrafficUpdater:
    """实时交通速度更新器"""

    def __init__(self, config_path: str, update_interval: int = 5,
                 speed_window: int = 60):
        self.config_path = config_path
        self.update_interval = update_interval
        self.speed_window = speed_window

        # 从配置读取 traffic 目录
        self.traffic_dir = self._get_traffic_dir()
        self.active_tar = os.path.join(self.traffic_dir, "traffic_active.tar")
        self.standby_tar = os.path.join(self.traffic_dir, "traffic_standby.tar")
        self.next_tar = os.path.join(self.traffic_dir, "traffic_next.tar.new")

        # 边速度缓存：edge_index -> [(speed, timestamp), ...]
        self.edge_speeds: Dict[int, List[Tuple[float, float]]] = defaultdict(list)

        # hot-reload endpoint 状态（避免重复告警）
        self._hot_reload_available: Optional[bool] = None

        # 统计信息
        self.stats = {
            'total_records': 0,
            'valid_records': 0,
            'edges_updated': 0,
            'tiles_updated': 0,
            'updates_count': 0
        }

        logger.info(f"RealtimeTrafficUpdater initialized:")
        logger.info(f"  traffic_dir: {self.traffic_dir}")
        logger.info(f"  update_interval: {update_interval}s")
        logger.info(f"  speed_window: {speed_window}s")

    def _get_traffic_dir(self) -> str:
        """从 valhalla.json 读取 traffic_extract 目录"""
        try:
            with open(self.config_path) as f:
                config = json.load(f)
            traffic_extract = config.get('mjolnir', {}).get('traffic_extract', '')
            return os.path.dirname(traffic_extract) or '/workspace/valhalla_tiles'
        except Exception as e:
            logger.warning(f"Failed to read config, using default: {e}")
            return '/workspace/valhalla_tiles'

    def _encode_speed(self, speed_kph: float) -> int:
        """编码速度：km/h -> 7-bit 值 (2kph 分辨率)"""
        return min(int(speed_kph / 2), 127)

    def _compute_congestion(self, speed_kph: float) -> int:
        """计算拥堵程度 (1-63)"""
        if speed_kph < 10:
            return 51  # 严重拥堵
        elif speed_kph < 20:
            return 31  # 中度拥堵
        elif speed_kph < 40:
            return 16  # 轻度拥堵
        else:
            return 6   # 畅通

    def _build_traffic_tile(self, tile_id: int,
                            edge_speeds: Dict[int, float]) -> bytes:
        """构建单个 TrafficTile 二进制数据"""
        header_size = 24  # TrafficTileHeader 大小
        speed_entry_size = 8  # TrafficSpeed 大小

        if not edge_speeds:
            return b''

        # 构建 header
        max_edge_index = max(edge_speeds.keys()) if edge_speeds else 0
        edge_count = max_edge_index + 1  # 包含所有边

        header = struct.pack('<Q', tile_id)  # tile_id (8 bytes)
        header += struct.pack('<Q', int(time.time()))  # last_update (8 bytes)
        header += struct.pack('<I', edge_count)  # directed_edge_count (4 bytes)
        header += struct.pack('<I', 4)  # traffic_tile_version (4 bytes)
        header += struct.pack('<I', 0)  # spare2 (4 bytes)
        header += struct.pack('<I', 0)  # spare3 (4 bytes)

        # 构建所有边的速度数据
        speeds_data = bytearray()
        for edge_idx in range(edge_count):
            speed = edge_speeds.get(edge_idx, 127)  # 默认 UNKNOWN

            encoded = self._encode_speed(speed)
            congestion = self._compute_congestion(speed) + 1  # 1-63 范围

            # TrafficSpeed bitfield (64 bits = 8 bytes)
            # 布局：speed(7) | speed1(7) | speed2(7) | speed3(7) |
            #      bp1(8) | bp2(8) | cong1(6) | cong2(6) | cong3(6) |
            #      incidents(1) | spare(1)
            entry = struct.pack('<Q',
                encoded |                      # overall_encoded_speed (bits 0-6)
                (encoded << 7) |               # encoded_speed1 (bits 7-13)
                (encoded << 14) |              # encoded_speed2 (bits 14-20)
                (encoded << 21) |              # encoded_speed3 (bits 21-27)
                (255 << 28) |                  # breakpoint1 (bits 28-35)
                (255 << 36) |                  # breakpoint2 (bits 36-43)
                (congestion << 44) |           # congestion1 (bits 44-49)
                (congestion << 50) |           # congestion2 (bits 50-55)
                (congestion << 56) |           # congestion3 (bits 56-61)
                (0 << 62) |                    # has_incidents (bit 62)
                (0 << 63)                      # spare (bit 63)
            )
            speeds_data.extend(entry)

        # 填充数据（与原有格式兼容）
        speeds_data.extend(struct.pack('<I', 0))
        speeds_data.extend(struct.pack('<I', 0))

        return header + bytes(speeds_data)

    def _build_traffic_tar(self,
                           tile_speeds: Dict[int, Dict[int, float]],
                           output_path: str) -> bool:
        """生成 traffic.tar 文件"""
        try:
            with tarfile.open(output_path, 'w') as tar:
                for tile_id, edge_speeds in tile_speeds.items():
                    tile_data = self._build_traffic_tile(tile_id, edge_speeds)
                    if not tile_data:
                        continue

                    # 生成文件名
                    filename = f"{tile_id:05d}.gph"

                    # 添加到 tar
                    tarinfo = tarfile.TarInfo(name=filename)
                    tarinfo.size = len(tile_data)
                    tar.addfile(tarinfo, fileobj=io.BytesIO(tile_data))

            logger.info(f"Built traffic tar: {output_path} "
                       f"({len(tile_speeds)} tiles)")
            return True

        except Exception as e:
            logger.error(f"Failed to build traffic tar: {e}")
            return False

    def _map_to_edge_index(self, lat: float, lon: float) -> Optional[int]:
        """
        将 GPS 坐标映射到 edge_index

        简化实现：使用位置 hash 模拟
        实际应调用 valhalla /locate API 或内部 map-matcher
        """
        # 简化：基于位置生成伪 edge_index
        # 实际应使用：requests.post('http://localhost:8002/locate', ...)
        pseudo_index = int(abs(lat * 100000) + abs(lon * 100000)) % 10000
        return pseudo_index

    def process_heartbeat_batch(self, records: List[HeartbeatRecord]) -> int:
        """
        处理一批 heartbeat 记录
        返回更新的边数量
        """
        now = time.time()
        self.stats['total_records'] += len(records)

        # 1. 将记录映射到边并更新缓存
        edge_updates = defaultdict(list)

        for record in records:
            edge_idx = self._map_to_edge_index(record.lat, record.lon)
            if edge_idx is not None:
                edge_updates[edge_idx].append((record.speed, now))
                self.stats['valid_records'] += 1

        # 2. 更新边速度缓存（滑动窗口）
        cutoff = now - self.speed_window

        for edge_idx, speed_ts_list in edge_updates.items():
            self.edge_speeds[edge_idx].extend(speed_ts_list)
            # 保留窗口内的数据
            self.edge_speeds[edge_idx] = [
                (s, t) for s, t in self.edge_speeds[edge_idx]
                if t > cutoff
            ]

        # 3. 计算每条边的最终速度（时间衰减加权平均）
        tile_speeds = defaultdict(dict)
        half_window = self.speed_window / 2

        for edge_idx, speed_ts_list in self.edge_speeds.items():
            if not speed_ts_list:
                continue

            weighted_sum = 0.0
            weight_total = 0.0

            for speed, ts in speed_ts_list:
                age = now - ts
                # 线性衰减权重
                weight = max(0.1, 1.0 - age / self.speed_window)
                weighted_sum += speed * weight
                weight_total += weight

            if weight_total > 0:
                avg_speed = weighted_sum / weight_total
                tile_id = edge_idx // 1000  # 简化 tile 分组

                # 过滤异常速度
                if 1.0 <= avg_speed <= 150.0:
                    tile_speeds[tile_id][edge_idx % 1000] = avg_speed

        # 4. 生成 traffic.tar
        if not self._build_traffic_tar(tile_speeds, self.next_tar):
            return 0

        # 5. 原子重命名：next → standby
        try:
            if os.path.exists(self.standby_tar):
                os.remove(self.standby_tar)
            shutil.move(self.next_tar, self.standby_tar)
        except Exception as e:
            logger.error(f"Failed to rename traffic tar: {e}")
            return 0

        # 6. 更新统计
        edge_count = sum(len(edges) for edges in tile_speeds.values())
        self.stats['edges_updated'] = edge_count
        self.stats['tiles_updated'] = len(tile_speeds)
        self.stats['updates_count'] += 1

        logger.info(f"Updated {edge_count} edges across "
                   f"{len(tile_speeds)} tiles")

        return edge_count

    def switch_archive(self) -> bool:
        """
        切换 traffic archive
        1. standby → active
        2. 通知 valhalla_service 热加载
        """
        if not os.path.exists(self.standby_tar):
            logger.error("Standby tar does not exist")
            return False

        try:
            # 原子切换符号链接
            active_link = os.path.join(self.traffic_dir, "traffic_current.tar")

            if os.path.islink(active_link):
                os.unlink(active_link)
            os.symlink(self.standby_tar, active_link)

            # 通知 valhalla_service 热加载
            self._notify_hot_reload()

            logger.info(f"Switched traffic archive to {self.standby_tar}")
            return True

        except Exception as e:
            logger.error(f"Failed to switch archive: {e}")
            return False

    def _notify_hot_reload(self) -> bool:
        """
        通知 valhalla_service 热加载 traffic.tar

        注意: /admin/reload_traffic 端点需要编译 HTTP handler 到 valhalla_service 中。
        当前大部分 Docker 镜像未包含此 handler，返回 404。
        此时需重启 valhalla_service 使新 traffic.tar 生效。
        """
        try:
            resp = requests.post(
                'http://localhost:8002/admin/reload_traffic',
                json={'traffic_path': self.standby_tar},
                timeout=5
            )
            if resp.status_code == 200:
                logger.info("valhalla_service acknowledged hot reload")
                self._hot_reload_available = True
                return True
            elif resp.status_code == 404:
                if self._hot_reload_available is None:
                    logger.warning(
                        "/admin/reload_traffic 返回 404 — HTTP handler 未编译到 valhalla_service 中。\n"
                        "    traffic.tar 已更新，但需重启 valhalla_service 使新数据生效:\n"
                        "    pkill valhalla_service && LD_LIBRARY_PATH=/usr/local/lib "
                        "valhalla_service /valhalla_tiles/valhalla.json 1 &"
                    )
                    self._hot_reload_available = False
                return False
            else:
                logger.warning(f"Hot reload returned unexpected status: {resp.status_code}")
                return False
        except requests.exceptions.RequestException:
            if self._hot_reload_available is None:
                logger.debug("valhalla_service not reachable, continuing anyway")
            return False

    def get_stats(self) -> dict:
        """获取统计信息"""
        return {
            **self.stats,
            'edge_cache_size': len(self.edge_speeds),
            'active_tar': os.path.exists(self.active_tar),
            'standby_tar': os.path.exists(self.standby_tar),
        }


def stream_heartbeat_csv(filepath: str):
    """流式读取 heartbeat CSV 文件"""
    logger.info(f"Streaming heartbeat data from {filepath}")

    with open(filepath, 'r') as f:
        reader = csv.reader(f)
        header = next(reader)  # 跳过 header
        logger.info(f"CSV columns: {header}")

        for row in reader:
            record = HeartbeatRecord.from_csv_row(row)
            if record:
                yield record


def main():
    parser = argparse.ArgumentParser(
        description='Realtime Traffic Update Daemon'
    )
    parser.add_argument(
        '--config', '-c',
        default='/workspace/valhalla_tiles/valhalla.json',
        help='Path to valhalla.json config'
    )
    parser.add_argument(
        '--heartbeat', '-i',
        required=True,
        help='Path to heartbeat CSV file'
    )
    parser.add_argument(
        '--interval', '-n',
        type=int,
        default=5,
        help='Update interval in seconds (default: 5)'
    )
    parser.add_argument(
        '--window', '-w',
        type=int,
        default=60,
        help='Speed aggregation window in seconds (default: 60)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Do not actually update traffic files'
    )

    args = parser.parse_args()

    # 检查文件是否存在
    if not os.path.exists(args.heartbeat):
        logger.error(f"Heartbeat file not found: {args.heartbeat}")
        sys.exit(1)

    if not os.path.exists(args.config):
        logger.warning(f"Config file not found: {args.config}")

    # 创建更新器
    updater = RealtimeTrafficUpdater(
        config_path=args.config,
        update_interval=args.interval,
        speed_window=args.window
    )

    # 主循环
    logger.info(f"Starting realtime traffic daemon (interval={args.interval}s)")
    logger.info(f"Reading from: {args.heartbeat}")

    batch = []
    batch_start = time.time()

    try:
        for record in stream_heartbeat_csv(args.heartbeat):
            batch.append(record)

            # 检查是否达到更新间隔
            if time.time() - batch_start >= args.interval:
                logger.info(f"Processing batch of {len(batch)} records")

                if not args.dry_run:
                    updated = updater.process_heartbeat_batch(batch)
                    if updated > 0:
                        updater.switch_archive()

                # 打印统计
                stats = updater.get_stats()
                logger.info(f"Stats: {json.dumps(stats, indent=2)}")

                # 重置 batch
                batch = []
                batch_start = time.time()

    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.error(f"Error: {e}")
        raise

    # 处理剩余数据
    if batch:
        logger.info(f"Processing final batch of {len(batch)} records")
        if not args.dry_run:
            updater.process_heartbeat_batch(batch)
            updater.switch_archive()

    logger.info(f"Final stats: {json.dumps(updater.get_stats(), indent=2)}")


if __name__ == '__main__':
    main()

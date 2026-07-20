#!/usr/bin/env python3
"""
heartbeat_to_edge_csv.py — 将 heartbeat CSV 数据转换为 valhalla_live_traffic 所需的按边实时速度 CSV

功能:
  1. 解析 heartbeat CSV (GPS 点 + 速度)
  2. 调用 valhalla /locate API 将 GPS 坐标映射到真实 edge
  3. 提取 tile_id (GraphId 格式 "level/tile_index/0") 和 edge_index
  4. 同一 edge 多个 heartbeat 点 → 时间衰减加权平均
  5. 输出 --update-edges 兼容的 CSV: level/tile_index/0,edge_index,speed_kph,congestion

依赖: valhalla_service 必须在 http://localhost:8002 运行，且已加载香港 tiles

用法:
  # 处理前 5000 条 heartbeat 记录
  python3 heartbeat_to_edge_csv.py \
      --heartbeat tests/data/heartbeat/heartbeat-2025-03-01.csv \
      --max-records 5000 \
      --output /tmp/edge_speeds.csv

  # 注入到 traffic.tar
  valhalla_live_traffic --config valhalla.json --update-edges /tmp/edge_speeds.csv
"""

import csv
import json
import sys
import time
import struct
import argparse
import logging
import math
from collections import defaultdict
from typing import Dict, List, Tuple, Optional, Set
from urllib.request import Request, urlopen
from urllib.error import URLError

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Valhalla GraphId 位布局 (来自 valhalla/baldr/graphid.h)
#
#   value = level | (tileid << 3) | (id << 25)
#
#   level:  bits [2:0]    (3 bits,  hierarchy level 0-7)
#   tileid: bits [24:3]   (22 bits, tile index)
#   id:     bits [45:25]  (21 bits, edge index within tile)
# ---------------------------------------------------------------------------

def graphid_value(lvl: int, tile_index: int, edge_id: int = 0) -> int:
    """构造 GraphId 64-bit 原始值 — 匹配 C++ GraphId(tileid, level, id).value"""
    return lvl | (tile_index << 3) | (edge_id << 25)


def graphid_decompose(value: int) -> Tuple[int, int, int]:
    """分解 64-bit GraphId → (level, tile_index, edge_id)"""
    lvl = value & 0x7
    tile_index = (value & 0x1fffff8) >> 3
    edge_id = (value & 0x3ffffe000000) >> 25
    return lvl, tile_index, edge_id


def tile_base_value(lvl: int, tile_index: int) -> int:
    """TILE BASE GraphId.value 用于 CSV 的第一列 tile_id 标识"""
    return graphid_value(lvl, tile_index, edge_id=0)


# ---------------------------------------------------------------------------
# heartbeat CSV 解析
# ---------------------------------------------------------------------------

def parse_heartbeat_csv(filepath: str, max_records: int = 0) -> List[dict]:
    """
    解析 heartbeat CSV，返回有效记录列表。
    每条记录: {lat, lon, speed, bearing, server_time}
    """
    records = []
    with open(filepath, 'r') as f:
        reader = csv.reader(f)
        header = next(reader)
        logger.info(f"CSV header: {header}")

        for i, row in enumerate(reader):
            if max_records and i >= max_records:
                break
            try:
                if len(row) < 5:
                    continue
                # 解析 location: POINT(lon lat)
                loc = row[2]
                if 'POINT' not in loc:
                    continue
                coords = loc.replace('POINT(', '').replace(')', '').split()
                if len(coords) != 2:
                    continue
                lon, lat = float(coords[0]), float(coords[1])

                # 过滤无效 GPS
                if lon == 0 and lat == 0:
                    continue
                # 香港范围
                if not (22.0 <= lat <= 22.6 and 113.8 <= lon <= 114.3):
                    continue

                speed = float(row[4]) if row[4] else 0.0
                # 过滤异常速度
                if speed <= 0 or speed > 150:
                    continue

                bearing = float(row[3]) if row[3] else 0.0
                server_time = row[6] if len(row) > 6 else ''

                records.append({
                    'lat': lat,
                    'lon': lon,
                    'speed': speed,
                    'bearing': bearing,
                    'server_time': server_time
                })
            except (ValueError, IndexError) as e:
                logger.debug(f"Row {i}: {e}")
                continue

    logger.info(f"Parsed {len(records)} valid records from {filepath}")
    return records


# ---------------------------------------------------------------------------
# valhalla /locate API — GPS → edge 映射
# ---------------------------------------------------------------------------

def call_locate(lat: float, lon: float,
                base_url: str = "http://localhost:8002",
                timeout: float = 10.0) -> Optional[dict]:
    """
    调用 valhalla /locate?verbose=true，返回第一个匹配 edge 的信息。
    返回: {graphid_value, edge_index, tile_index, level, tile_id_key}
    或 None (无匹配)
    """
    data = json.dumps({
        "locations": [{"lat": lat, "lon": lon}],
        "verbose": True
    }).encode('utf-8')

    req = Request(
        f"{base_url}/locate?verbose=true",
        data=data,
        headers={"Content-Type": "application/json"}
    )

    try:
        with urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode('utf-8'))
    except URLError as e:
        logger.debug(f"/locate error ({lat:.4f},{lon:.4f}): {e}")
        return None
    except json.JSONDecodeError:
        logger.debug(f"/locate invalid JSON ({lat:.4f},{lon:.4f})")
        return None

    if not result or not isinstance(result, list) or len(result) == 0:
        return None

    loc = result[0]
    edges = loc.get('edges', [])
    if not edges:
        return None

    # 取第一个匹配的 edge
    edge = edges[0]

    # Valhalla /locate 响应结构: edge.edge_id = {id, value, tile_id, level}
    # edge_id.id    = GraphId::id() (21-bit edge index within tile)
    # edge_id.value = full 64-bit GraphId raw value
    # edge_id.tile_id = 22-bit tile index
    # edge_id.level   = hierarchy level (0-7)
    edge_id_info = edge.get('edge_id', {})
    if edge_id_info:
        lvl = edge_id_info.get('level', 0)
        tile_index = edge_id_info.get('tile_id', 0)
        edge_idx = edge_id_info.get('id', 0)
        graphid_raw = edge_id_info.get('value', 0)
    else:
        # Fallback for older valhalla versions
        graphid_raw = edge.get('edge', {}).get('id') or edge.get('id')
        if graphid_raw is None:
            return None
        lvl, tile_index, edge_idx = graphid_decompose(int(graphid_raw))

    tile_key = tile_base_value(lvl, tile_index)

    return {
        'graphid_value': int(graphid_raw) if graphid_raw else 0,
        'level': lvl,
        'tile_index': tile_index,
        'edge_index': edge_idx,
        'tile_id_key': tile_key,
        'percent_along': edge.get('percent_along', 0.0),
        'distance': edge.get('distance', 0.0),
    }


# ---------------------------------------------------------------------------
# 速度聚合
# ---------------------------------------------------------------------------

class EdgeSpeedAggregator:
    """按 edge 聚合 heartbeat 速度，时间衰减加权平均"""

    def __init__(self, speed_window_seconds: float = 300.0):
        self.window = speed_window_seconds
        # edge_key (tile_id_key, edge_index) → [(speed, timestamp), ...]
        self.samples: Dict[Tuple[int, int], List[Tuple[float, float]]] = defaultdict(list)

    def add(self, tile_id_key: int, edge_index: int, speed_kph: float,
            timestamp: Optional[float] = None):
        if timestamp is None:
            timestamp = time.time()
        key = (tile_id_key, edge_index)
        self.samples[key].append((speed_kph, timestamp))

    def compute_average(self, now: Optional[float] = None) -> List[Tuple[int, int, float, int]]:
        """
        计算每条边的加权平均速度和拥堵程度。
        返回: [(tile_id_key, edge_index, avg_speed_kph, congestion), ...]
        """
        if now is None:
            now = time.time()
        half_window = self.window / 2.0

        results = []
        for (tile_key, edge_idx), speed_list in self.samples.items():
            if not speed_list:
                continue

            # 时间衰减加权平均
            weighted_sum = 0.0
            weight_total = 0.0
            for speed, ts in speed_list:
                age = now - ts
                if age > self.window:
                    continue
                weight = max(0.1, 1.0 - age / self.window)
                weighted_sum += speed * weight
                weight_total += weight

            if weight_total < 0.01:
                continue

            avg_speed = weighted_sum / weight_total

            # 拥堵程度 (1-63)
            if avg_speed < 10:
                congestion = 51   # 严重拥堵
            elif avg_speed < 20:
                congestion = 31   # 中度拥堵
            elif avg_speed < 40:
                congestion = 16   # 轻度拥堵
            else:
                congestion = 6    # 畅通

            # 约束 congestion 到 [1, 63]
            congestion = max(1, min(63, congestion))

            results.append((tile_key, edge_idx, round(avg_speed, 1), congestion))

        return results


# ---------------------------------------------------------------------------
# CSV 输出
# ---------------------------------------------------------------------------

def write_edge_csv(results: List[Tuple[int, int, float, int]],
                   output_path: str):
    """
    写入 valhalla_live_traffic --update-edges 兼容的 CSV。
    格式: level/tile_index/0, edge_index, speed_kph, congestion
    """
    with open(output_path, 'w') as f:
        f.write("# level/tile_index/0, edge_index, speed_kph, congestion\n")
        f.write("# Generated by heartbeat_to_edge_csv.py\n")
        f.write(f"# Records: {len(results)}\n")
        f.write(f"# Timestamp: {int(time.time())}\n")

        for tile_key, edge_idx, speed_kph, congestion in results:
            lvl, tile_index, _ = graphid_decompose(tile_key)
            # 输出格式: level/tile_index/0 (0 表示 tile base)
            f.write(f"{lvl}/{tile_index}/0,{edge_idx},{speed_kph},{congestion}\n")

    logger.info(f"Wrote {len(results)} edge speeds to {output_path}")


# ---------------------------------------------------------------------------
# offline 模式 — 无 valhalla_service 时的数据验证
# ---------------------------------------------------------------------------

class OfflineValidator:
    """离线模式：验证 heartbeat 数据格式 + 模拟 CSV 生成 (无真实 edge 映射)"""

    def __init__(self):
        self.stats = {
            'total': 0,
            'valid': 0,
            'speed_sum': 0.0,
            'speed_min': 999,
            'speed_max': 0,
            'lat_min': 90, 'lat_max': -90,
            'lon_min': 180, 'lon_max': -180,
        }

    def process(self, records: List[dict]):
        for r in records:
            self.stats['total'] += 1
            s = r['speed']
            if s > 0:
                self.stats['valid'] += 1
                self.stats['speed_sum'] += s
                self.stats['speed_min'] = min(self.stats['speed_min'], s)
                self.stats['speed_max'] = max(self.stats['speed_max'], s)
            self.stats['lat_min'] = min(self.stats['lat_min'], r['lat'])
            self.stats['lat_max'] = max(self.stats['lat_max'], r['lat'])
            self.stats['lon_min'] = min(self.stats['lon_min'], r['lon'])
            self.stats['lon_max'] = max(self.stats['lon_max'], r['lon'])

    def report(self) -> str:
        s = self.stats
        avg = s['speed_sum'] / s['valid'] if s['valid'] > 0 else 0
        lines = [
            "=" * 60,
            "  Heartbeat 数据离线验证报告",
            "=" * 60,
            f"  总记录数:       {s['total']}",
            f"  有效速度记录:   {s['valid']}",
            f"  平均速度:       {avg:.1f} km/h",
            f"  速度范围:       {s['speed_min']:.1f} ~ {s['speed_max']:.1f} km/h",
            f"  纬度范围:       {s['lat_min']:.4f} ~ {s['lat_max']:.4f}",
            f"  经度范围:       {s['lon_min']:.4f} ~ {s['lon_max']:.4f}",
            "",
            f"  TrafficSpeed 编码 (2kph 分辨率):",
            f"    avg → encoded = {int(avg/2)} → decoded = {int(avg/2)*2} km/h",
            f"    (与 C++ encode_live_speed() 一致)",
            "",
            f"  下一步 — 启动 valhalla_service 后运行在线转换:",
            f"    python3 heartbeat_to_edge_csv.py \\",
            f"        --heartbeat tests/data/heartbeat/heartbeat-2025-03-01.csv \\",
            f"        --output /tmp/edge_speeds.csv",
            f"    valhalla_live_traffic --config valhalla.json --update-edges /tmp/edge_speeds.csv",
            "=" * 60,
        ]
        return '\n'.join(lines)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='heartbeat → valhalla edge speed CSV converter'
    )
    parser.add_argument('--heartbeat', '-i', required=True,
                        help='Heartbeat CSV file path')
    parser.add_argument('--output', '-o', default='/tmp/edge_speeds.csv',
                        help='Output edge speed CSV (default: /tmp/edge_speeds.csv)')
    parser.add_argument('--max-records', '-n', type=int, default=0,
                        help='Max heartbeat records to process (0 = all)')
    parser.add_argument('--valhalla-url', default='http://localhost:8002',
                        help='Valhalla service URL (default: http://localhost:8002)')
    parser.add_argument('--offline', action='store_true',
                        help='Offline mode: validate data format only (no valhalla API)')
    parser.add_argument('--speed-window', '-w', type=float, default=300.0,
                        help='Speed aggregation window in seconds (default: 300)')
    parser.add_argument('--delay-ms', type=int, default=50,
                        help='Delay between /locate calls in ms (default: 50)')

    args = parser.parse_args()

    # 1. Parse heartbeat CSV
    records = parse_heartbeat_csv(args.heartbeat, args.max_records)
    if not records:
        logger.error("No valid heartbeat records found")
        sys.exit(1)

    # 2. 离线模式
    if args.offline:
        validator = OfflineValidator()
        validator.process(records)
        print(validator.report())
        return

    # 3. 在线模式 — 调用 valhalla /locate 映射
    logger.info(f"Mapping {len(records)} GPS points via {args.valhalla_url}/locate ...")

    aggregator = EdgeSpeedAggregator(speed_window_seconds=args.speed_window)
    mapped = 0
    unmapped = 0
    nodes = set()  # track unique edges

    start_time = time.time()
    for i, r in enumerate(records):
        result = call_locate(r['lat'], r['lon'], args.valhalla_url)
        if result:
            aggregator.add(
                result['tile_id_key'],
                result['edge_index'],
                r['speed']
            )
            nodes.add((result['tile_id_key'], result['edge_index']))
            mapped += 1
        else:
            unmapped += 1

        # Progress
        if (i + 1) % 100 == 0 or i == 0:
            elapsed = time.time() - start_time
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            logger.info(f"  [{i+1}/{len(records)}] mapped={mapped} unmapped={unmapped} "
                       f"unique_edges={len(nodes)} rate={rate:.1f}/s")

        # Rate limiting
        if args.delay_ms > 0:
            time.sleep(args.delay_ms / 1000.0)

    logger.info(f"Mapping complete: {mapped} mapped, {unmapped} unmapped, "
                f"{len(nodes)} unique edges in {time.time()-start_time:.1f}s")

    if mapped == 0:
        logger.error("No GPS points could be mapped to edges. "
                     "Is valhalla_service running with Hong Kong tiles?")
        sys.exit(1)

    # 4. Aggregate & write CSV
    results = aggregator.compute_average()
    write_edge_csv(results, args.output)

    # 5. Summary
    print()
    print("=" * 60)
    print("  Heartbeat → Edge CSV 转换完成")
    print("=" * 60)
    print(f"  Heartbeat 记录:    {len(records)}")
    print(f"  成功映射:          {mapped}")
    print(f"  映射失败:          {unmapped}")
    print(f"  唯一边数:          {len(nodes)}")
    print(f"  输出边数:          {len(results)} (聚合后)")
    print(f"  输出文件:          {args.output}")
    print()
    print(f"  # 下一步 — 注入 traffic.tar:")
    print(f"  valhalla_live_traffic --config valhalla.json \\")
    print(f"      --update-edges {args.output}")
    print()
    if results:
        speeds = [r[2] for r in results]
        print(f"  速度统计: avg={sum(speeds)/len(speeds):.1f} "
              f"min={min(speeds):.1f} max={max(speeds):.1f} km/h")
        # Show a few sample lines
        print()
        print("  样本输出 (前5行):")
        with open(args.output) as f:
            for line in f:
                if not line.startswith('#'):
                    print(f"    {line.rstrip()}")
    print("=" * 60)


if __name__ == '__main__':
    main()

"""Stage 5: Speed Profile Generation Framework.

This module provides the framework for generating speed profiles.
Outputs Valhalla historical traffic format CSV files.
TODO: Implement actual speed profile generation logic (colleagues will implement)
"""

from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple, Any
import logging

import concurrent.futures
import numpy as np
import pandas as pd
from tqdm import tqdm
import os
from numpy.typing import NDArray

from traffic_pipeline.pipeline.base import BaseStage, DataNode, PipelineConfig
from traffic_pipeline.src.encoding.compress import compress_speed_buckets, encode_compressed_speeds
from traffic_pipeline.src.encoding.smoothing import halman_filter

def get_tile_path_from_graph_id(graph_id: int) -> Tuple[str, str]:
    """
    Generates the Valhalla tile file path from a Graph ID.
    
    Valhalla packs graph_id as:
        [higher bits: id_in_tile] [22 bits: tile_id] [3 bits: level]
    
    So we extract:
        level     = lowest 3 bits
        tile_id   = next 22 bits
    """
    # Extract level (lowest 3 bits)
    level = graph_id & 0x7          # 0b00000111 mask → keeps bits 0-2
    
    # Extract tile_id: shift right 3 bits (remove level), then mask next 22 bits
    tile_id = (graph_id >> 3) & 0x3FFFFF        # 0x3FFFFF = 2^22 - 1
    
    tile_id_str = str(tile_id)
    # Pad to multiple of 3 digits (Valhalla directory layout requirement)
    pad_length = (len(tile_id_str) + 2) // 3 * 3
    tile_id_str = tile_id_str.zfill(pad_length)

    dir_parts = [str(level)]
    for i in range(0, len(tile_id_str) - 3, 3):
        dir_parts.append(tile_id_str[i:i+3])
    
    dir_path = os.path.join(*dir_parts)
    filename = tile_id_str[-3:] + ".csv"
    
    return dir_path, filename

class SpeedProfileGenerationStage(BaseStage):
    """Stage 5: Generate final speed profiles and output CSV files.

    Output format (Valhalla historical traffic):
        edge_id, freeflow_speed, constrained_speed, historical_speeds
        1/47701/130,50,40,AQ0AAAAAAA...

    Where:
        - edge_id: Internal graph_id (level/tile_id/id)
        - freeflow_speed: Typical night speed (km/h)
        - constrained_speed: Typical day speed (km/h)
        - historical_speeds: DCT-II encoded 2016 speed values

    Input:
        - filled_speeds: Complete speed list per edge

    Output:
        - speed_profiles: Final speed profiles per edge
        - CSV files in tile hierarchy
    """

    def __init__(self, config: PipelineConfig) -> None:
        """Initialize speed profile generation stage.

        Args:
            config: Pipeline configuration
        """
        super().__init__(config, "speed_profile_generation")
        self.gen_speed_config: Dict[str, Any] = config.processing.get("generate_speeds", {})
        self.input_config: Dict[str, Any] = config.input
        self.output_config: Dict[str, Any] = config.output
        self.way_edges_path: str = self.input_config.get(
            "local_csv", {}
        ).get("way_edges_path", "data/road_data/way_edges.txt")
        self.roads_path: str = self.input_config.get("local_csv", {}).get(
            "roads_path", "data/road_data/hong_kong_roads_with_centroids.csv"
        )
        self._edge_to_way: Dict[int, int] = self.build_edge_to_way_map(self.way_edges_path)
        self._way_to_type: Dict[int, str] = pd.read_csv(self.roads_path).set_index("way_id")[
            "highway_type"
        ].to_dict()
        
        smoothing_config = self.gen_speed_config.get("smoothing", {})
        self.halman_window: int = smoothing_config.get("halman_window", 15)
        self.halman_n_sigmas: float = smoothing_config.get("halman_n_sigmas", 2.5)
        
        self.buckets_per_week: int = self.gen_speed_config.get("buckets_per_week", 2016)
        self.slots_per_day: int = self.gen_speed_config.get("slots_per_day", 288)
        
        self._tunnel_exclusions: set[int] | None = None

    def validate_input(self, data: DataNode) -> bool:
        """Validate input data has required fields.

        Args:
            data: Input data node

        Returns:
            True if input is valid
        """
        return (
            data.filled_speeds is not None
            and len(data.filled_speeds) > 0
        )
        
    def build_edge_to_way_map(self, path: str) -> Dict[int, int]:
        edge_to_way = {}

        with open(path, "r") as f:
            for line in f:
                parts = line.strip().split(",")
                if not parts:
                    continue

                way_id = int(parts[0])

                # Remaining values are in groups: direction, edge_id
                vals = parts[1:]
                for i in range(0, len(vals), 2):
                    direction = int(vals[i])          # not used here, but available
                    edge_id = int(vals[i + 1])
                    edge_to_way[edge_id] = way_id

        return edge_to_way
        
    def infer_road_type(self, edge_id: int) -> str:
        """
        Infers a road type based on OSM way_id from pre-loaded mappings.
        Returns 'unknown' silently for unmapped edges or way_ids.
        """
        way_id = self._edge_to_way.get(int(edge_id))
        if way_id is None:
            raise ValueError(f"Missing way_id for edge_id: {edge_id}")

        return self._way_to_type.get(way_id, 'unknown')
        
    def _get_derived_speeds(
        self, way_id: int, time_slots: Dict[Any, List[float]]
    ) -> Tuple[float, float]:
        
        """Derives freeflow and constrained speeds based on the configured strategy."""
        strategy = self.gen_speed_config.get('speed_derivation_strategy', 'rule_based')
        road_type = self.infer_road_type(way_id)

        if strategy == 'data_driven':
            data_driven_config = self.gen_speed_config.get('data_driven_speeds', {})
            off_peak_hours = data_driven_config.get('off_peak_hours', [])
            percentile = data_driven_config.get('percentile', 95)

            off_peak_speeds = []
            for slot, speeds in time_slots.items():
                hour = (slot % self.slots_per_day) // 12
                if hour in off_peak_hours:
                    off_peak_speeds.extend(speeds)
            
            if off_peak_speeds:
                freeflow_speed = np.percentile(off_peak_speeds, percentile)
                # For constrained speed, we can use the same value as a reasonable fallback
                constrained_speed = freeflow_speed
                return freeflow_speed, constrained_speed

        # Fallback to rule_based if data_driven fails or is not selected
        rule_config = self.gen_speed_config.get('rule_based_speeds', {})
        freeflow_speeds = rule_config.get('freeflow', {})
        constrained_speeds = rule_config.get('constrained', {})
        
        freeflow_speed = freeflow_speeds.get(road_type, freeflow_speeds.get('default', 50.0))
        constrained_speed = constrained_speeds.get(road_type, constrained_speeds.get('default', 40.0))
        
        return freeflow_speed, constrained_speed
    
    def _load_tunnel_exclusions(self) -> set[int]:
        roads_path = Path(self.roads_path)
        way_edges_path = Path(self.way_edges_path)

        df = pd.read_csv(roads_path)
        tunnel_way_ids = set(df[df["name"].str.contains("tunnel", case=False, na=False)]["way_id"])

        tunnel_edges: set[int] = set()
        with way_edges_path.open() as f:
            for line in f:
                parts = line.strip().split(",")
                if len(parts) < 3:
                    continue
                try:
                    way_id = int(parts[0])
                except ValueError:
                    continue
                if way_id not in tunnel_way_ids:
                    continue
                for i in range(1, len(parts), 2):
                    if i + 1 >= len(parts):
                        break
                    try:
                        tunnel_edges.add(int(parts[i + 1]))
                    except ValueError:
                        pass

        self._tunnel_exclusions = tunnel_edges
    
    def _process_edge(self, item: Tuple[int, Dict[Any, List[float]]]) -> NDArray[np.float32]:
        graph_id, time_slots = item
        freeflow, constrained = self._get_derived_speeds(graph_id, time_slots)
        default_speed = freeflow * 0.8

        hist_speeds = np.full(self.buckets_per_week, default_speed, dtype=np.float32)
        for slot_str, speeds in time_slots.items():
            try:
                slot = int(slot_str)
            except ValueError:
                continue
            if 0 <= slot < self.buckets_per_week:
                
                # Uncomment for speed outlier removal # change
                # if graph_id in abnormal_graph_ids:
                speeds_arr = np.array(speeds)

                q = np.percentile(speeds_arr, [25, 75])
                iqr = q[1] - q[0]
                lower = q[0] - 1.5 * iqr
                upper = q[1] + 1.5 * iqr

                speeds_arr = speeds_arr[(speeds_arr >= lower) & (speeds_arr <= upper)]

                hist_speeds[slot] = np.mean(speeds_arr) if speeds_arr.size > 0 else default_speed

        hist_speeds = halman_filter(hist_speeds, self.halman_window, self.halman_n_sigmas)

        return hist_speeds
    
    def _export_speeds(
        self, item: Tuple[int, Dict[Any, List[float]]]
    ) -> List[Tuple[str, Dict[str, Any]]]:
        graph_id, time_slots = item
        
        freeflow_speed, constrained_speed = self._get_derived_speeds(graph_id, time_slots)
        hist_speeds = time_slots
        
        encoding_method = self.gen_speed_config.get('encoding_method', 'dct')
        hist_encoded = ""
        if encoding_method == 'new_dct':
            coefficients = compress_speed_buckets(hist_speeds)          # From 2016 to 200 values
            hist_encoded = encode_compressed_speeds(coefficients)

        graph_id = int(graph_id)
        
        # --- Bitwise unpacking of Valhalla graph_id ---
        level = graph_id & 0x7                  # lowest 3 bits  → level
        tile_id = (graph_id >> 3) & 0x3FFFFF    # next 22 bits   → tile_id
        id_in_tile = graph_id >> 25             # all remaining higher bits → id inside tile
        
        edge_id = f"{level}/{tile_id}/{id_in_tile}"
        
        dir_path, filename = get_tile_path_from_graph_id(graph_id)
        file_path = os.path.join(dir_path, filename)
        
        data = {
            'edge_id': edge_id,
            'freeflow_speed': freeflow_speed,
            'constrained_speed': constrained_speed,
            'historical_speeds': hist_encoded
        }
        
        return [(file_path, data)]
    
    def process(self, data: DataNode) -> DataNode:
        """Generate speed profiles with EMA smoothing and DCT encoding.

        Args:
            data: Input data node with filled speeds

        Returns:
            DataNode with final speed profiles

        TODO: Implement actual profile generation:
            1. Apply Halman-MA smoothing (span=24) to reduce noise
            2. Fill any remaining missing speed values
            3. Generate complete 2016-bucket speed profile per edge
            4. Compute freeflow_speed (night) and constrained_speed (day)
            5. DCT-II encode 2016 speed values to string
            6. Create tile hierarchy folder structure
            7. Write CSV files per Valhalla format
        """
        self.logger.info(f"Processing speed profile generation for {len(data.filled_speeds) if data.filled_speeds is not None else 0} edges")
        
        # Remove tunnel edge IDs
        # if self._tunnel_exclusions is None:
        #     self._load_tunnel_exclusions()
        self._tunnel_exclusions: set[int] = set()
        edges_to_process = {
            gid: ts for gid, ts in data.filled_speeds.items() if gid not in self._tunnel_exclusions
        }
        
        final_speed: Dict[str, NDArray[np.float32]] = {}
        keys = list(edges_to_process.keys())
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            for graph_id, hist_speeds in tqdm(
                zip(keys, executor.map(self._process_edge, edges_to_process.items())),
                total=len(edges_to_process)
            ):
                final_speed[graph_id] = hist_speeds
        
        result_data = DataNode(
            trajectories=data.trajectories,
            trips=data.trips,
            cleaned_trajectories=data.cleaned_trajectories,
            cleaned_trips=data.cleaned_trips,
            map_matched_points=data.map_matched_points,
            raw_speeds=data.raw_speeds,
            filled_speeds=data.filled_speeds,
            speed_profiles=final_speed,
            metadata={
                **data.metadata,
                "stage": "speed_profile_generation",
                "filled_edge_count": len(data.filled_speeds) if data.filled_speeds else 0,
                "profile_edge_count": len(final_speed),
            }
        )
        
        self.save_output(result_data, self.output_config.get('output_dir', "data/output"))

        return result_data

    def save_output(self, data: DataNode, output_dir: Path) -> bool:
        """Save speed profiles to CSV files in tile hierarchy.

        Args:
            tiled_data: Dictionary mapping file paths to lists of data dictionaries
            output_dir: Output directory (should be traffic_data folder)

        Returns:
            True if save successful
        """
        
        # Implement actual CSV output in Valhalla tile hierarchy:
        #   traffic_data/
        #   ├── 0/
        #   │   └── 003/
        #   │       └── 015.csv
        #   ├── 1/
        #   │   └── 047/
        #   │       └── 701.csv
        #
        # CSV format:
        #   edge_id,freeflow_speed,constrained_speed,historical_speeds
        #   1/47701/130,50,40,AQ0AAAAAAA...
        
        output_dir = Path(output_dir) / "stage5_speed_profile" / "traffic_data"
        if os.path.exists(output_dir):
            import shutil
            shutil.rmtree(output_dir)
        os.makedirs(output_dir)
        
        tiled_data = defaultdict(list)
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            for results in tqdm(executor.map(self._export_speeds, data.speed_profiles.items()),
                total=len(data.speed_profiles)
            ):
                for file_path, out in results:
                    tiled_data[file_path].append(out)
                    
                    
        logging.info(f"Writing {len(tiled_data)} tile traffic files to '{output_dir}'/ directory...")
        
        for tile_path, records in tiled_data.items():
            full_path = os.path.join(output_dir, tile_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            df = pd.DataFrame(records)
            df.to_csv(full_path, index=False, header=False, lineterminator='\n')
            
        logging.info(f"Encoding complete! Data written to {output_dir}")

        return True

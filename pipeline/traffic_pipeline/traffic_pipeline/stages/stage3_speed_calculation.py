"""Stage 3: Speed Calculation Framework.

This module provides the framework for calculating raw speeds per edge.
TODO: Implement actual speed calculation logic (colleagues will implement)
"""

from pathlib import Path
from typing import Any, Dict, List
import logging

# Core data processing
import polars as pl
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# File I/O and utilities
import os
import json
import gzip
import tempfile
import glob
import logging
import random
import time

# Concurrency and progress
from concurrent.futures import ThreadPoolExecutor
from tqdm import tqdm

from traffic_pipeline.pipeline.base import BaseStage, DataNode, PipelineConfig
from traffic_pipeline.src.speed.calculation import calculate_speeds_for_trace
from traffic_pipeline.src.utils.tools import parse_start_ts, get_time_slot, get_filename, load_matched_data_streaming

# Type aliases
TripGPSPoint = Dict[str, Any]
TripTrace = List[TripGPSPoint]
ValhallaResults = Dict[str, Any]
WayTimeSpeeds = Dict[Any, Dict[Any, List[float]]]
SpeedRecord = Dict[str, Any]

class SpeedCalculationStage(BaseStage):
    """Stage 3: Calculate raw speeds per edge per time bucket.

    Computes speeds from GPS trace timestamps and distances.

    Input:
        - map_matched_points: GPS points with edge IDs

    Output:
        - raw_speeds: List of {edge_id, slot, speed_kph} records
    """

    def __init__(self, config: PipelineConfig) -> None:
        """Initialize speed calculation stage.

        Args:
            config: Pipeline configuration
        """
        super().__init__(config, "speed_calculation")
        self.calc_config: Dict[str, Any] = config.processing.get('speed', {}) 
        

    def validate_input(self, data: DataNode) -> bool:
        """Validate input data has required fields.

        Args:
            data: Input data node

        Returns:
            True if input is valid
        """
        logging.info(f"{len(data.map_matched_points)} Map Matched Points")
        return (
            data.map_matched_points is not None
            and len(data.map_matched_points) > 0
        )
                
    def prepare_results_for_trip(
        self, 
        gps_trace: TripTrace, 
        valhalla_results: ValhallaResults, 
        min_speed: float = 5.0, 
        max_speed: float = 150.0
    ) -> List[SpeedRecord]:
        results = []
        
        edge_to_speed_length_dict = {}
        for idx, _ in enumerate(valhalla_results['matched_points']):
            if idx == 0:
                continue
            
            # Get the speed from the gps trace
            speed_kph = gps_trace[idx]['speed']
            if not (min_speed < speed_kph < max_speed) or valhalla_results['matched_points'][idx]['type'] == "unmatched":
                continue
            
            # Get the corresponding edge['way_id']
            if valhalla_results['matched_points'][idx]['edge_index'] >= len(valhalla_results['edges']):
                continue
            else:
                index_of_edge = valhalla_results['matched_points'][idx]['edge_index']
                way_id = valhalla_results['edges'][index_of_edge]['id']     # valhalla_results['edges'][index_of_edge]['way_id']   # change to id instead of way_id

                if index_of_edge not in edge_to_speed_length_dict:
                    edge_to_speed_length_dict[index_of_edge] = {'speeds': [], 'length': valhalla_results['edges'][index_of_edge]['length']}
                edge_to_speed_length_dict[index_of_edge]['speeds'].append(speed_kph)
                # edge_to_speed_length_dict[index_of_edge] = [speed_kph, valhalla_results['edges'][index_of_edge]['length']]

            # Get the corresponding slot
            slot = get_time_slot(gps_trace[idx]['time'])
                    
            results.append({"way_id": way_id, "slot": slot, "speed_kph": speed_kph})
            
        return results
    
    def process_single_trip(self, item: Tuple[Any, TripTrace]) -> List[SpeedRecord]:
        trip_id, gps_trace = item
        if len(gps_trace) < self.calc_config.get("min_gps_points", 5):
            return []
        
        if trip_id in self.map_matched_points["trip_id"]:
            valhalla_results = self.map_matched_points.filter(pl.col("trip_id") == trip_id)
            valhalla_results = valhalla_results.select("matched_points", "edges").to_dicts()[0]
        else:
            return []
        
        if not valhalla_results or "edges" not in valhalla_results:
            return []
        start_ts = parse_start_ts(gps_trace[0]["time"])
        if start_ts is None:
            return []

        min_speed = self.calc_config.get("speed_limits", {}).get("min_speed_kph", 5)
        max_speed = self.calc_config.get("speed_limits", {}).get("max_speed_kph", 150)
        
        # Calculate speed
        if gps_trace:
            gps_trace, valhalla_results = calculate_speeds_for_trace(gps_trace, valhalla_results)

        results = self.prepare_results_for_trip(gps_trace, valhalla_results, min_speed=min_speed, max_speed=max_speed)

        return results
    
    def process_all_trips_threaded(
        self,
        batch: Dict[Any, TripTrace],
        max_workers: int = 8,
    ) -> pl.DataFrame:
        all_results: List[SpeedRecord] = []
        items = list(batch.items())
        if not items:               # empty polars frame with schema to keep downstream code stable
            return pl.DataFrame(schema={"way_id": pl.Int64, "slot": pl.Int64, "speed_kph": pl.Float64})

        # Uncomment for debugging purposes only # hardcoded # changed
        # for i in tqdm(range(len(items))): self.process_single_trip(items[i])
        
        # Use map to avoid holding many Future objects and to stream results through tqdm
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for trip_results in tqdm(executor.map(self.process_single_trip, items), total=len(items), desc="Processing trips", unit="trip"):
                if trip_results:
                    all_results.extend(trip_results)

        if all_results:
            return pl.DataFrame(all_results)
        else:
            return pl.DataFrame(schema={"way_id": pl.Int64, "slot": pl.Int64, "speed_kph": pl.Float64})
                  
    def calculate_speeds(self) -> WayTimeSpeeds:
        """
        Calculate speeds on road segments (ways) for each time bucket based on matched trip data.

        Args:
            matched_trips (dict): Dictionary of trip_id -> list of GPS points (with 'time').

        Returns:
            dict: Nested dictionary mapping way_id -> time_slot -> list of speeds (km/h).
        """
        logging.info("Start calculating speed by time bucket...")
        
        # Dictionary structure: way_id -> time_slot -> list of speeds
        way_time_speeds = defaultdict(lambda: defaultdict(list))
        
        # Apply sampling if sampling_rate < 1 to reduce computation on large datasets
        sampling_rate = self.calc_config.get('sampling_rate', 1.0)
        if sampling_rate < 1.0:
            trip_ids = list(self.map_matched_points.keys())
            sampled_trip_ids = random.sample(trip_ids, int(len(trip_ids) * sampling_rate))
            self.map_matched_points = {tid: self.map_matched_points[tid] for tid in sampled_trip_ids}
            logging.info(f"After sampling, processing {len(self.map_matched_points)} trips")

        intermediate_dir = self.config.output_dir / "stage1_data_clean"
        parquet_files = glob.glob(os.path.join(intermediate_dir, f'*.parquet'))
        
        if not parquet_files:
            raise ValueError("No Parquet files found in stage1_data_clean directory")
        
        
        if len(parquet_files) == 1:
            input_file = parquet_files[0]
            self.logger.info(f"Processing single file: {input_file}")
            partial_results_list = []
            for batch in tqdm(load_matched_data_streaming(input_file, batch_size=5000), desc="Batches"):
                df = self.process_all_trips_threaded(batch)
                partial_result = self.aggregate_way_slot_speeds(df)
                partial_results_list.append(partial_result)

            self.file_name = get_filename(input_file)
            way_time_speeds = self.merge_partial_results_polars(partial_results_list)
            # self.save_speed_data_gzip(way_time_speeds, self.file_name)
            
            return way_time_speeds
        else:
            # way_time_speeds_list = []
            # for input_file in tqdm(parquet_files, desc="Processing files"):
            raise NotImplementedError("Multiple Parquet files processing not implemented yet. Please ensure only one Parquet file is present in stage1_data_clean directory.")
          
    def aggregate_way_slot_speeds(self, df: pl.DataFrame) -> WayTimeSpeeds:
        # Convert pandas → Polars only if necessary
        if not isinstance(df, pl.DataFrame):
            df = pl.from_pandas(df)

        # Version-agnostic approach using implode() which works across Polars versions
        grouped = (
            df.group_by(["way_id", "slot"])
            .agg(pl.col("speed_kph").implode())  # Use implode() instead of .list
        )

        # Convert efficiently to nested dict (way_id → slot → list of speeds)
        result = {}
        for way_id, slot, speeds in tqdm(grouped.iter_rows()):
            if way_id not in result:
                result[way_id] = {}
            result[way_id][slot] = speeds

        return result
    
    def merge_partial_results_polars(
        self,
        partials: List[WayTimeSpeeds],
        temp_dir: Optional[str] = None,
    ) -> WayTimeSpeeds:
        """
        Merge multiple partial results ({way_id: {slot: [speeds...]}})
        efficiently using Polars, ensuring way_id and slot are strings.

        Args:
            partials: iterable of dicts (each partial result)
            temp_dir: optional directory for temporary Parquet storage

        Returns:
            dict: merged {way_id: {slot: [speeds...]}}
        """
        temp_dir = temp_dir or tempfile.mkdtemp(prefix="way_merge_")
        parquet_files = []

        # Step 1: Write each partial result to disk as Parquet
        for i, partial in enumerate(partials):
            rows = []
            for way_id, slots in partial.items():
                way_id_str = str(way_id)
                for slot, speeds in slots.items():
                    slot_str = str(slot)
                    for speed in speeds:
                        rows.append((way_id_str, slot_str, float(speed)))

            if not rows:
                continue

            # Explicit schema: Utf8 for strings, Float64 for speeds
            df = pl.DataFrame(
                rows,
                schema={"way_id": pl.Utf8, "slot": pl.Utf8, "speed_kph": pl.Float64}
            )
            parquet_path = os.path.join(temp_dir, f"partial_{i}.parquet")
            df.write_parquet(parquet_path)
            parquet_files.append(parquet_path)

        if not parquet_files:
            return {}

        # Step 2: Lazy concatenate and group_by
        lazy_frames = [pl.scan_parquet(f) for f in parquet_files]
        merged_lazy = pl.concat(lazy_frames)

        merged_df = (
            merged_lazy
            .group_by(["way_id", "slot"])
            .agg(pl.col("speed_kph").implode())
            .collect()
        )

        # Step 3: Convert back to nested dict
        result = {}
        for row in merged_df.iter_rows():
            way_id, slot, speeds = row
            result.setdefault(way_id, {})[slot] = speeds

        return result

    def process(self, data: DataNode) -> DataNode:
        """Calculate raw speeds from map-matched traces.

        Args:
            data: Input data node with matched points

        Returns:
            DataNode with raw speed records

        Implement actual speed calculation:
            1. Parse GPS trace into lat, lon, time arrays
            2. Compute distances between consecutive points
            3. Compute time differences in hours
            4. Calculate raw speeds (km/h)
            5. Apply smoothing for GPS jumps
            6. Filter elapsed time > 10 seconds
            7. Filter speeds outside bounds (min_speed, max_speed)
            8. Map edge_index to edge_id
            9. Accumulate speeds into edge_to_speed_length_dict
            10. Convert timestamps to 5-minute slots (0-2015)
            11. Create speed records {id, slot, speed_kph}
        """
        self.logger.info(f"Processing speed calculation for {len(data.map_matched_points) if data.map_matched_points is not None else 0} points")
        # Placeholder: pass through matched points
        self.map_matched_points = data.map_matched_points
        raw_speeds = self.calculate_speeds()
        
        result_data = DataNode(
            trajectories=data.trajectories,
            trips=data.trips,
            cleaned_trajectories=data.cleaned_trajectories,
            cleaned_trips=data.cleaned_trips,
            map_matched_points=data.map_matched_points,
            raw_speeds=raw_speeds,
            metadata={
                **data.metadata,
                "stage": "speed_calculation",
                "matched_point_count": len(data.map_matched_points) if data.map_matched_points is not None else 0,
                "raw_speed_count": len(raw_speeds),
            }
        )
        
        self.save_output(result_data, self.config.output_dir)

        return result_data
    
    def save_output(self, data: DataNode, output_dir: Path) -> bool:
        """Save raw speeds to output directory."""
        stage_dir = output_dir / "stage3_speed_calculation"
        stage_dir.mkdir(parents=True, exist_ok=True)
        
        out_path = stage_dir / f'speed_data_{self.file_name}.json.gz'
        
        tmp_path = out_path.with_suffix('.tmp.json.gz')
        try:
            with gzip.open(tmp_path, 'wt', encoding='utf-8') as f:
                json.dump(data.raw_speeds, f, separators=(',', ':'))
            
            tmp_path.replace(out_path)
            self.logger.info(f"Speed data saved to {out_path}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to save speed data: {e}")
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            return False


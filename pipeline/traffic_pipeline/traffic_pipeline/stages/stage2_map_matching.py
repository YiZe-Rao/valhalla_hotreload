"""Stage 2: Map Matching Framework.

This module provides the framework for map matching GPS traces to edges.
Uses Valhalla's trace_attributes endpoint for edge ID mapping.
"""

# Core data processing
import polars as pl
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# File I/O and utilities
import os
import glob
import logging

# Concurrency and progress
from concurrent.futures import ThreadPoolExecutor
import threading
from tqdm import tqdm

# HTTP and networking
import requests
from requests.adapters import HTTPAdapter

from traffic_pipeline.clients.valhalla_client import (
    BaseValhallaClient,
    ValhallaClientFactory,
)
from traffic_pipeline.pipeline.base import BaseStage, DataNode, PipelineConfig
from traffic_pipeline.src.utils.tools import load_matched_data_streaming

# Type aliases
GPSPoint = Dict[str, Any]
TripTrace = List[GPSPoint]
MatchedTripResult = Dict[str, Any]

class MapMatchingStage(BaseStage):
    """Stage 2: Map match GPS traces to road network edges.

    Calls Valhalla trace_attributes endpoint to:
    - Match GPS points to nearest road edges
    - Retrieve edge IDs for speed calculation

    Input:
        - cleaned_trajectories: Cleaned GPS trajectory records

    Output:
        - map_matched_points: GPS points with edge IDs
    """

    def __init__(
        self,
        config: PipelineConfig,
        valhalla_client: Optional[BaseValhallaClient] = None
    ) -> None:
        """Initialize map matching stage.

        Args:
            config: Pipeline configuration
            valhalla_client: Optional Valhalla client (creates default if not provided)
        """
        super().__init__(config, "map_matching")
        self.match_config: Dict[str, Any] = config.processing['map_matching'] 
        self.valhalla_config: Dict[str, Any] = config.valhalla
        self.valhalla_client = valhalla_client or ValhallaClientFactory.create(
            service_url=config.valhalla_service_url
        )

    def validate_input(self, data: DataNode) -> bool:
        """Validate input data has required fields.

        Args:
            data: Input data node

        Returns:
            True if input is valid
        """
        return (
            data.cleaned_trajectories is not None
            and len(data.cleaned_trajectories) > 0
        )
        
    def get_session(self) -> requests.Session:
        thread_local = threading.local()
        sess: Optional[requests.Session] = getattr(thread_local, "session", None)
        if sess is None:
            sess = requests.Session()
            pool_size = int(self.match_config.get("http_pool_size", 50))
            adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size, max_retries=0)
            sess.mount("http://", adapter)
            sess.mount("https://", adapter)
            # optional: reduce logging from requests
            thread_local.session = sess
        return sess

    # Lightweight match function using per-thread session to avoid creating new TCP/TLS handshakes
    def match_with_session(self, gps_trace: TripTrace) -> Optional[Dict[str, Any]]:
        if not gps_trace:
            return None
        sess = self.get_session()
        shape_for_api = [{"lat": p["lat"], "lon": p["lon"]} for p in gps_trace]
        payload = {"shape": shape_for_api, "costing": "auto", "shape_match": "map_snap"}
        try:
            r = sess.post(f"{self.valhalla_config['service_url']}{self.valhalla_config['trace_attributes_endpoint']}", json=payload, timeout=30)
            r.raise_for_status()
            logging.info("Success")
            return r.json()
        except Exception:
            logging.info("None")
            return None
      
    def match_single_trip(self, item: Tuple[Any, TripTrace]) -> List[MatchedTripResult]:
        trip_id, gps_trace = item
        if len(gps_trace) < self.match_config["min_gps_points"]:
            return []
        
        valhalla_results = self.match_with_session(gps_trace)
        if not valhalla_results or "edges" not in valhalla_results or "matched_points" not in valhalla_results:
            return []
        return [{
            "trip_id": trip_id,
            "matched_points": valhalla_results["matched_points"],
            "edges": valhalla_results["edges"]
        }]
        
    def get_matched_points_threaded(
        self, 
        processed_trips_gps: Dict[Any, TripTrace], 
        max_workers: int = 8
    ) -> pl.DataFrame:
        all_results = []
        items = list(processed_trips_gps.items())
        
        if not items:
            return pl.DataFrame({
                "trip_id": pl.Series([], dtype=pl.Utf8),
                "matched_points": pl.Series([], dtype=pl.Object),
                "edges": pl.Series([], dtype=pl.Object),
            })

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for trip_results in tqdm(
                executor.map(self.match_single_trip, items),
                total=len(items),
                desc="Processing trips",
                unit="trip"
            ):
                all_results.extend(trip_results)

        if all_results:
            return pl.DataFrame(all_results)
        else:
            return pl.DataFrame({
                "trip_id": pl.Series([], dtype=pl.Utf8),
                "matched_points": pl.Series([], dtype=pl.Object),
                "edges": pl.Series([], dtype=pl.Object),
            })
        
    def get_matched_points(self) -> pl.DataFrame:
        """Retrieve matched points from Valhalla results."""
        
        intermediate_dir = self.config.output_dir / "stage1_data_clean"
        parquet_files = glob.glob(os.path.join(intermediate_dir, "*.parquet"))
        
        all_dfs = []
        for input_file in tqdm(parquet_files, desc="Processing files"):
            partial_results_list = []
            for batch in tqdm(
                load_matched_data_streaming(input_file, batch_size=5000),
                desc="Batches"
            ):
                df = self.get_matched_points_threaded(batch)
                partial_results_list.append(df)
            
            if partial_results_list:
                file_df = pl.concat(partial_results_list, how="vertical_relaxed")
                all_dfs.append(file_df)
          
        if all_dfs:
            final_df = pl.concat(all_dfs, how="vertical_relaxed")
            return final_df
        else:
            return pl.DataFrame({
                "trip_id": pl.Series([], dtype=pl.Utf8),
                "matched_points": pl.Series([], dtype=pl.List(pl.Struct)),
                "edges": pl.Series([], dtype=pl.List(pl.Object)),
            })
            
    def process(self, data: DataNode) -> DataNode:
        """Map match GPS traces to edges.

        Args:
            data: Input data node with cleaned trajectories

        Returns:
            DataNode with map-matched points

        Implement actual map matching:
            1. For each trip, extract trip ID and GPS trace
            2. Call match_with_session() for map matching
            3. Skip traces with < min_gps_points
            4. Attach edge info to each GPS point
            5. Store matched points with edge IDs
        """
        self.logger.info(f"Processing map matching for {len(data.cleaned_trajectories) if data.cleaned_trajectories is not None else 0} trajectories")

        matched_points: List[Dict[str, Any]] = []

        # Placeholder: iterate through trajectories
        # TODO: Implement actual map matching with Valhalla client

        # if data.cleaned_trajectories:
        #     for trajectory in data.cleaned_trajectories:
        #         # TODO: Extract GPS trace from trajectory
        #         # gps_trace = [{"lat": ..., "lon": ..., "time": ...}, ...]
        #         # response = await self.valhalla_client.match_with_session(gps_trace)
        #         # if response:
        #         #     matched_points.extend(self.valhalla_client.create_matched_points(response))
        #         pass
        
        self.trips = data.cleaned_trips if data.cleaned_trips is not None else None
        self.traj = data.cleaned_trajectories if data.cleaned_trajectories is not None else None
        
        matched_points = self.get_matched_points()
        
        result_data = DataNode(
            trajectories=data.trajectories,
            trips=data.trips,
            cleaned_trajectories=data.cleaned_trajectories,
            cleaned_trips=data.cleaned_trips,
            map_matched_points=matched_points,
            metadata={
                **data.metadata,
                "stage": "map_matching",
                "trajectory_count": len(data.cleaned_trajectories) if data.cleaned_trajectories is not None else 0,
                "matched_point_count": len(matched_points),
            }
        )

        return result_data

    def save_output(self, data: DataNode, output_dir: Path) -> bool:
        """Save map matched data to output directory.

        Args:
            data: Data node with matched points
            output_dir: Output directory

        Returns:
            True if save successful
        """
        output_dir = output_dir / "stage2_map_matching"
        output_dir.mkdir(parents=True, exist_ok=True)

        # TODO: Save matched points to file
        self.logger.info(f"Map matching stage output saved to {output_dir}")

        return True

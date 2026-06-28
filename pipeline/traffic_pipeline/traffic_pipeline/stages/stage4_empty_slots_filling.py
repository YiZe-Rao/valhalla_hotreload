"""Stage 4: Empty Slots Filling Framework.

This module provides the framework for filling missing speed values.
"""

import copy
from pathlib import Path
from typing import Any, Dict
import logging

from traffic_pipeline.pipeline.base import BaseStage, DataNode, PipelineConfig
from traffic_pipeline.src.filling.fill_missing import _fill_missing_time_slots_temporal

class EmptySlotsFillingStage(BaseStage):
    """Stage 4: Fill missing speed values in time slots.

    Methods:
        - Temporal neighborhood filling (default)
        - Day-of-week pattern filling

    Input:
        - raw_speeds: Incomplete speed list per edge

    Output:
        - filled_speeds: Complete speed list per edge
    """

    def __init__(self, config: PipelineConfig) -> None:
        """Initialize empty slots filling stage.

        Args:
            config: Pipeline configuration
        """
        super().__init__(config, "empty_slots_filling")
        self.empty_fill_config: Dict[str, Any] = config.processing.get('empty_slots', {}) 

    def validate_input(self, data: DataNode) -> bool:
        """Validate input data has required fields.

        Args:
            data: Input data node

        Returns:
            True if input is valid
        """
        return (
            data.raw_speeds is not None
            and len(data.raw_speeds) > 0
        )
    
    
    def process(self, data: DataNode) -> DataNode:
        """Fill missing speed values for each edge.

        Args:
            data: Input data node with raw speeds

        Returns:
            DataNode with filled speed values

        Implement actual filling logic:
            1. Group raw speeds by edge_id
            2. For each edge, build time_dict mapping slot -> [speeds]
            3. Apply temporal neighborhood filling:
                - For missing slot t, look at [t-neighbor_size, t+neighbor_size]
                - Compute average of existing speeds
            4. Apply day-of-week pattern filling (alternative):
                - For missing slot t, look at same time on other days
                - Compute average of speeds from same time slot
            5. Update way_time_speeds with filled values
        """
        self.logger.info(f"Processing empty slots filling for {len(data.raw_speeds) if data.raw_speeds else 0} speed records")

        filled_speeds: Dict[str, Any] = {}
        logging.info("Starting aggregation and encoding of historical speed data...")
        
        filled_speeds = _fill_missing_time_slots_temporal(copy.deepcopy(data.raw_speeds), self.empty_fill_config.get("neighbor_size", 3))
        
        result_data = DataNode(
            trajectories=data.trajectories,
            trips=data.trips,
            cleaned_trajectories=data.cleaned_trajectories,
            cleaned_trips=data.cleaned_trips,
            map_matched_points=data.map_matched_points,
            raw_speeds=data.raw_speeds,
            filled_speeds=filled_speeds,
            metadata={
                **data.metadata,
                "stage": "empty_slots_filling",
                "raw_speed_count": len(data.raw_speeds) if data.raw_speeds else 0,
                "filled_edge_count": len(filled_speeds),
            }
        )

        return result_data

    def save_output(self, data: DataNode, output_dir: Path) -> bool:
        """Save filled speeds to output directory.

        Args:
            data: Data node with filled speeds
            output_dir: Output directory

        Returns:
            True if save successful
        """
        output_dir = output_dir / "stage4_empty_slots_filling"
        output_dir.mkdir(parents=True, exist_ok=True)

        # TODO: Save filled speeds to file
        self.logger.info(f"Empty slots filling stage output saved to {output_dir}")

        return True

"""Pipeline Orchestrator.

Coordinates execution of all 5 pipeline stages.
"""

from pathlib import Path
from typing import Optional
import logging

from traffic_pipeline.clients.valhalla_client import ValhallaClientFactory
from traffic_pipeline.pipeline.data import load_csv
from traffic_pipeline.pipeline.base import DataNode, PipelineConfig, StageResult
from traffic_pipeline.stages import (
    DataCleanStage,
    EmptySlotsFillingStage,
    MapMatchingStage,
    SpeedCalculationStage,
    SpeedProfileGenerationStage,
)


class PipelineOrchestrator:
    """Orchestrates the complete traffic data processing pipeline.

    Pipeline Flow:
        1. Data Clean -> 2. Map Matching -> 3. Speed Calculation ->
        4. Empty Slots Filling -> 5. Speed Profile Generation

    Input:
        - Local CSV files or Firestore

    Output:
        - Speed files in Valhalla historical traffic format
    """

    def __init__(self, config: PipelineConfig):
        """Initialize the orchestrator.

        Args:
            config: Pipeline configuration
        """
        self.config = config
        self.logger = logging.getLogger("orchestrator")
        self._setup_stages()

    def _setup_stages(self) -> None:
        """Initialize all pipeline stages."""
        self.stages = []

        # Stage 1: Data Clean
        if self.config.enable_data_clean:
            self.stages.append(DataCleanStage(self.config))

        # Stage 2: Map Matching
        if self.config.enable_map_matching:
            valhalla_client = ValhallaClientFactory.create(
                service_url=self.config.valhalla_service_url
            )
            self.stages.append(MapMatchingStage(self.config, valhalla_client))

        # Stage 3: Speed Calculation
        if self.config.enable_speed_calculation:
            self.stages.append(SpeedCalculationStage(self.config))

        # Stage 4: Empty Slots Filling
        if self.config.enable_empty_slots_filling:
            self.stages.append(EmptySlotsFillingStage(self.config))

        # Stage 5: Speed Profile Generation
        if self.config.enable_speed_profile_generation:
            self.stages.append(SpeedProfileGenerationStage(self.config))

        self.logger.info(f"Initialized {len(self.stages)} pipeline stages")

    def load_input(self) -> DataNode:
        """Load input data from configured source.

        Returns:
            DataNode with input data

        TODO: Implement actual data loading from CSV or Firestore
        """
        self.logger.info("Loading input data...")

        # TODO: Implement data loading:
        if self.config.input['source'] == "local_csv":
            trajectories = load_csv(self.config.input['local_csv']['trajectories_path'])
            trips = load_csv(self.config.input['local_csv']['trips_path'])
        #   elif config.input.source == "firestore":
        #       trajectories = load_from_firestore(config.input.firestore)
        #       trips = load_from_firestore(config.input.firestore)

        data = DataNode(
            trajectories=trajectories,
            trips=trips,
            metadata={"source": self.config}
        )

        self.logger.info("Input data loaded")
        return data

    def save_output(self, data: DataNode, output_dir: Path) -> bool:
        """Save final output (speed files) to directory.

        Args:
            data: Data node with final results
            output_dir: Output directory

        Returns:
            True if save successful
        """
        self.logger.info(f"Saving output to {output_dir}")

        # TODO: Implement actual output saving:
        #   - Create tile hierarchy folder structure
        #   - Write CSV files per Valhalla historical traffic format
        #   - Format: edge_id, freeflow_speed, constrained_speed, historical_speeds

        self.logger.info("Output saved successfully")
        return True

    def run(self) -> bool:
        """Execute the complete pipeline.

        Returns:
            True if pipeline completed successfully
        """
        self.logger.info("=" * 60)
        self.logger.info("Starting Traffic Pipeline")
        self.logger.info("=" * 60)

        # Create output directory
        self.config.output_dir.mkdir(parents=True, exist_ok=True)

        # Load input data
        self.logger.info("Loading input data...")
        data = self.load_input()
        if not data:
            self.logger.error("Failed to load input data")
            return False

        # Run each stage in sequence
        for stage in self.stages:
            self.logger.info(f"\n{'='*40}")
            self.logger.info(f"Running Stage: {stage.name}")
            self.logger.info(f"{'='*40}")

            result = stage.run(data, self.config.output_dir)

            if not result.success:
                self.logger.error(f"Stage {stage.name} failed: {result.error}")
                return False

            # Update data with stage output
            if result.data:
                data = result.data

            # Log metrics
            if result.metrics:
                self.logger.info(f"Stage metrics: {result.metrics}")

        # Save final output
        self.logger.info("\nSaving final output...")
        if not self.save_output(data, self.config.output_dir):
            self.logger.error("Failed to save output")
            return False

        self.logger.info("=" * 60)
        self.logger.info("Pipeline completed successfully")
        self.logger.info("=" * 60)

        return True

    def get_stage_names(self) -> list:
        """Get list of stage names.

        Returns:
            List of stage names in order
        """
        return [stage.name for stage in self.stages]

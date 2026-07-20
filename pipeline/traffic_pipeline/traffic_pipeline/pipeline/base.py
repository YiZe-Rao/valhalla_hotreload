"""Base classes and interfaces for the traffic pipeline."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
import logging
import polars as pl
import pandas as pd
import numpy as np
from numpy.typing import NDArray



logger = logging.getLogger(__name__)


@dataclass
class PipelineConfig:
    """Main pipeline settings + full raw config access"""
    config_path: Optional[Path] = None
    valhalla_service_url: str = "http://localhost:8002"
    workers: int = 4
    batch_size: int = 100
    output_dir: Path = Path("data/output")
    temp_dir: Path = Path("/tmp/traffic_pipeline")
    log_level: str = "INFO"
    
    # Stage flags
    enable_data_clean: bool = True
    enable_map_matching: bool = True
    enable_speed_calculation: bool = True
    enable_empty_slots_filling: bool = True
    enable_speed_profile_generation: bool = True
    
    # Keep the complete config as attribute
    _config: dict = None   # private

    @property
    def config(self) -> dict:
        return self._config

    # Optional: allow dot access directly on the object
    def __getattr__(self, name):
        if self._config and name in self._config:
            return self._config[name]
        raise AttributeError(f"No attribute '{name}'")


@dataclass
class DataNode:
    """Represents data flowing through the pipeline."""

    # Input data reference
    trajectories: Optional[List[Dict[str, Any]]] = None
    trips: Optional[List[Dict[str, Any]]] = None

    # Stage outputs
    cleaned_trajectories: Optional[pd.DataFrame] = None
    cleaned_trips: Optional[pd.DataFrame] = None
    map_matched_points: pl.DataFrame = None
    raw_speeds: Optional[Dict[str, Dict[str, List[float]]]] = None
    filled_speeds: Optional[Dict[str, Dict[str, List[float]]]] = None
    speed_profiles: Dict[str, NDArray[np.float32]] = field(default_factory=dict)
    
    # Metadata
    metadata: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    def get_trajectory_count(self) -> int:
        """Get number of trajectories."""
        if self.cleaned_trajectories:
            return len(self.cleaned_trajectories)
        return len(self.trajectories) if self.trajectories else 0

    def get_edge_count(self) -> int:
        """Get number of unique edges."""
        if self.raw_speeds:
            edges = set()
            for record in self.raw_speeds:
                if "edge_id" in record:
                    edges.add(record["edge_id"])
            return len(edges)
        return 0


@dataclass
class StageResult:
    """Result of a pipeline stage execution."""

    success: bool
    stage_name: str
    message: str
    data: Optional[DataNode] = None
    error: Optional[str] = None
    metrics: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, stage_name: str, message: str, data: Optional[DataNode] = None) -> "StageResult":
        """Create a successful result."""
        return cls(success=True, stage_name=stage_name, message=message, data=data)

    @classmethod
    def fail(cls, stage_name: str, error: str) -> "StageResult":
        """Create a failed result."""
        return cls(success=False, stage_name=stage_name, message="", error=error)


class BaseStage(ABC):
    """Abstract base class for all pipeline stages.

    Each stage must implement:
    - validate_input(): Check if input data is valid
    - process(): Process the data
    - save_output(): Save results (optional)
    """

    def __init__(self, config: PipelineConfig, name: str):
        """Initialize the stage.

        Args:
            config: Pipeline configuration
            name: Stage name for logging
        """
        self.config = config
        self.name = name
        self.logger = logging.getLogger(f"pipeline.stages.{name}")

    @abstractmethod
    def validate_input(self, data: DataNode) -> bool:
        """Validate input data for this stage.

        Args:
            data: Input data node

        Returns:
            True if input is valid
        """
        pass

    @abstractmethod
    def process(self, data: DataNode) -> DataNode:
        """Process the data.

        Args:
            data: Input data node

        Returns:
            Processed data node
        """
        pass

    def save_output(self, data: DataNode, output_dir: Path) -> bool:
        """Save stage output to files.

        Args:
            data: Data node with output
            output_dir: Output directory

        Returns:
            True if save successful
        """
        self.logger.info(f"Stage {self.name}: save_output not implemented")
        return True

    def run(self, data: DataNode, output_dir: Optional[Path] = None) -> StageResult:
        """Run the complete stage.

        Args:
            data: Input data node
            output_dir: Optional output directory

        Returns:
            StageResult with success status and data
        """
        self.logger.info(f"Starting stage: {self.name}")

        # Validate input
        if not self.validate_input(data):
            error = f"Stage {self.name}: Invalid input data"
            self.logger.error(error)
            return StageResult.fail(self.name, error)

        # Process data
        try:
            result_data = self.process(data)
            self.logger.info(f"Stage {self.name}: Processing complete")
        except Exception as e:
            error = f"Stage {self.name}: Processing failed - {str(e)}"
            self.logger.exception(error)
            return StageResult.fail(self.name, error)

        # Save output
        if output_dir and not self.save_output(result_data, output_dir):
            return StageResult.fail(self.name, f"Stage {self.name}: Failed to save output")

        return StageResult.ok(
            stage_name=self.name,
            message=f"Stage {self.name} completed successfully",
            data=result_data
        )


class BasePipeline(ABC):
    """Abstract base class for the complete pipeline.

    Pipeline flow:
    1. Data Clean -> 2. Map Matching -> 3. Speed Calculation ->
    4. Empty Slots Filling -> 5. Speed Profile Generation
    """

    def __init__(self, config: PipelineConfig):
        """Initialize the pipeline.

        Args:
            config: Pipeline configuration
        """
        self.config = config
        self.logger = logging.getLogger("pipeline")
        self._setup_logging()

    def _setup_logging(self) -> None:
        """Configure logging."""
        logging.basicConfig(
            level=getattr(logging, self.config.log_level),
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )

    @abstractmethod
    def load_input(self) -> DataNode:
        """Load input data from source (CSV or Firestore).

        Returns:
            DataNode with input data
        """
        pass

    @abstractmethod
    def save_output(self, data: DataNode, output_dir: Path) -> bool:
        """Save final output (speed files) to directory.

        Args:
            data: Data node with final results
            output_dir: Output directory

        Returns:
            True if save successful
        """
        pass

    def run(self) -> bool:
        """Execute the complete pipeline.

        Returns:
            True if pipeline completed successfully
        """
        self.logger.info("=" * 60)
        self.logger.info("Starting Traffic Pipeline")
        self.logger.info("=" * 60)

        # Load input data
        self.logger.info("Loading input data...")
        data = self.load_input()
        if not data:
            self.logger.error("Failed to load input data")
            return False

        self.logger.info(f"Loaded {data.get_trajectory_count()} trajectories")

        # Stage 1: Data Clean
        if self.config.enable_data_clean:
            self.logger.info("Stage 1: Data Clean")
            # TODO: Implement data cleaning logic
            pass

        # Stage 2: Map Matching
        if self.config.enable_map_matching:
            self.logger.info("Stage 2: Map Matching")
            # TODO: Implement map matching logic
            pass

        # Stage 3: Speed Calculation
        if self.config.enable_speed_calculation:
            self.logger.info("Stage 3: Speed Calculation")
            # TODO: Implement speed calculation logic
            pass

        # Stage 4: Empty Slots Filling
        if self.config.enable_empty_slots_filling:
            self.logger.info("Stage 4: Empty Slots Filling")
            # TODO: Implement empty slots filling logic
            pass

        # Stage 5: Speed Profile Generation
        if self.config.enable_speed_profile_generation:
            self.logger.info("Stage 5: Speed Profile Generation")
            # TODO: Implement speed profile generation logic
            pass

        # Save output
        self.logger.info("Saving output...")
        if not self.save_output(data, self.config.output_dir):
            self.logger.error("Failed to save output")
            return False

        self.logger.info("=" * 60)
        self.logger.info("Pipeline completed successfully")
        self.logger.info("=" * 60)

        return True

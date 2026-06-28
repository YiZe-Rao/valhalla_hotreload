"""Valhalla API client for trace_attributes service."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import asyncio
import logging

import httpx


logger = logging.getLogger(__name__)


@dataclass
class TracePoint:
    """GPS trace point for map matching."""

    lat: float
    lon: float
    time: int  # Unix timestamp


@dataclass
class MatchedPoint:
    """Map-matched point with edge information."""

    lat: float
    lon: float
    edge_index: int
    edge_id: str
    distance: float


@dataclass
class TraceAttributesRequest:
    """Request to trace_attributes endpoint."""

    shape: List[Dict[str, Any]]
    costing: str = "auto"
    shape_match: str = "map_snap"
    directions_options: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "shape": self.shape,
            "costing": self.costing,
            "shape_match": self.shape_match,
            "directions_options": self.directions_options or {}
        }


@dataclass
class TraceAttributesResponse:
    """Response from trace_attributes endpoint."""

    edges: List[Dict[str, Any]]
    matched_points: List[Dict[str, Any]]
    raw_score: float
    match_score: float

    @classmethod
    def from_response(cls, response: Dict[str, Any]) -> "TraceAttributesResponse":
        """Parse response from Valhalla API."""
        return cls(
            edges=response.get("edges", []),
            matched_points=response.get("matched_points", []),
            raw_score=response.get("raw_score", 0.0),
            match_score=response.get("match_score", 0.0)
        )


class BaseValhallaClient(ABC):
    """Abstract base class for Valhalla API client."""

    @abstractmethod
    async def trace_attributes(
        self,
        trace_points: List[TracePoint]
    ) -> Optional[TraceAttributesResponse]:
        """Send trace for map matching.

        Args:
            trace_points: List of GPS trace points

        Returns:
            TraceAttributesResponse or None if failed
        """
        pass


class ValhallaClient(BaseValhallaClient):
    """Client for Valhalla trace_attributes API.

    Communicates with Container 1's Valhalla service to perform
    map matching and retrieve edge IDs for GPS traces.
    """

    def __init__(
        self,
        service_url: str = "http://localhost:8080",
        timeout: float = 60.0,
        max_retries: int = 3,
        retry_backoff: float = 1.0
    ):
        """Initialize the client.

        Args:
            service_url: Valhalla service URL
            timeout: Request timeout in seconds
            max_retries: Maximum retry attempts
            retry_backoff: Retry backoff factor
        """
        self.service_url = service_url.rstrip("/")
        self.endpoint = f"{self.service_url}/trace_attributes"
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.logger = logging.getLogger("valhalla_client")

    async def trace_attributes(
        self,
        trace_points: List[TracePoint]
    ) -> Optional[TraceAttributesResponse]:
        """Send trace for map matching.

        Args:
            trace_points: List of GPS trace points

        Returns:
            TraceAttributesResponse or None if failed
        """
        # Convert trace points to shape format
        shape = [
            {"lat": p.lat, "lon": p.lon}
            for p in trace_points
        ]

        request = TraceAttributesRequest(shape=shape)

        self.logger.info(f"Sending {len(trace_points)} points to trace_attributes")

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(self.max_retries):
                try:
                    response = await client.post(
                        self.endpoint,
                        json=request.to_dict()
                    )
                    response.raise_for_status()
                    data = response.json()
                    self.logger.info("trace_attributes request successful")
                    return TraceAttributesResponse.from_response(data)

                except httpx.HTTPStatusError as e:
                    self.logger.warning(
                        f"Attempt {attempt + 1}/{self.max_retries}: "
                        f"HTTP error {e.response.status_code}"
                    )
                except httpx.RequestError as e:
                    self.logger.warning(
                        f"Attempt {attempt + 1}/{self.max_retries}: "
                        f"Request error: {str(e)}"
                    )

                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self.retry_backoff * (attempt + 1))

        self.logger.error(f"trace_attributes failed after {self.max_retries} attempts")
        return None

    async def match_with_session(
        self,
        gps_trace: List[Dict[str, Any]]
    ) -> Optional[TraceAttributesResponse]:
        """Match GPS trace using trace_attributes.

        This is a convenience method that accepts GPS traces in dict format.

        Args:
            gps_trace: List of dicts with lat, lon, and time keys

        Returns:
            TraceAttributesResponse or None if failed
        """
        trace_points = [
            TracePoint(
                lat=point["lat"],
                lon=point["lon"],
                time=point.get("time", 0)
            )
            for point in gps_trace
        ]

        return await self.trace_attributes(trace_points)

    def extract_edge_ids(
        self,
        response: TraceAttributesResponse
    ) -> List[str]:
        """Extract edge IDs from trace_attributes response.

        Args:
            response: TraceAttributesResponse

        Returns:
            List of edge IDs
        """
        edge_ids = []
        for edge in response.edges:
            if "id" in edge:
                edge_ids.append(str(edge["id"]))
        return edge_ids

    def create_matched_points(
        self,
        response: TraceAttributesResponse
    ) -> List[MatchedPoint]:
        """Create MatchedPoint objects from response.

        Args:
            response: TraceAttributesResponse

        Returns:
            List of MatchedPoint objects
        """
        matched = []
        for i, point in enumerate(response.matched_points):
            edge_index = point.get("edge_index", -1)
            edge_id = ""

            if 0 <= edge_index < len(response.edges):
                edge = response.edges[edge_index]
                edge_id = str(edge.get("id", ""))

            matched.append(MatchedPoint(
                lat=point.get("lat", 0.0),
                lon=point.get("lon", 0.0),
                edge_index=edge_index,
                edge_id=edge_id,
                distance=point.get("distance_along_edge", 0.0)
            ))

        return matched


class ValhallaClientFactory:
    """Factory for creating Valhalla clients."""

    @staticmethod
    def create(
        service_url: Optional[str] = None,
        timeout: float = 60.0,
        max_retries: int = 3
    ) -> ValhallaClient:
        """Create a Valhalla client.

        Args:
            service_url: Service URL (defaults to env var VALHALLA_SERVICE_URL)
            timeout: Request timeout
            max_retries: Maximum retry attempts

        Returns:
            Configured ValhallaClient
        """
        import os

        url = service_url or os.getenv("VALHALLA_SERVICE_URL", "http://localhost:8080")

        return ValhallaClient(
            service_url=url,
            timeout=timeout,
            max_retries=max_retries
        )

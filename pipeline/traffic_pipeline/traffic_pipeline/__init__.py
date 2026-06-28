"""Traffic Pipeline Framework.

This package provides a framework for processing GPS data and generating
speed files for Valhalla routing engine.

Pipeline Stages:
    1. Data Clean - Clean raw GPS data
    2. Map Matching - Call trace_attributes for edge ID mapping
    3. Speed Calculation - Calculate raw speeds per edge per time bucket
    4. Empty Slots Filling - Fill missing speed values
    5. Speed Profile Generation - EMA smoothing, DCT encoding
"""

__version__ = "0.1.0"

# Core data processing
import json
import logging
from pathlib import Path
import pandas as pd
from datetime import datetime, timezone
import numpy as np
import math
import os

def parse_start_ts(point_time):
    # very fast ISO8601 parse for typical "YYYY-MM-DDTHH:MM:SS[.mmm][Z]" formats
    try:
        # handle trailing Z (UTC) quickly
        if point_time.endswith("Z"):
            # fromisoformat doesn't accept 'Z', so replace with +00:00
            return datetime.fromisoformat(point_time[:-1] + "+00:00").timestamp()
        return datetime.fromisoformat(point_time).timestamp()
    except Exception:
        # fallback to pandas (slower) for weird formats
        try:
            return pd.to_datetime(point_time).timestamp()
        except Exception:
            return None
        
def get_time_slot(timestamp_str):
    edge_time = parse_start_ts(timestamp_str)      # start_ts +  gps_trace[idx]['time_ela']
    edge_dt = datetime.fromtimestamp(edge_time)
    weekday = (edge_dt.weekday() + 1) % 7
    slot = (weekday * 288) + (edge_dt.hour * 12) + (edge_dt.minute // 5)  
    
    return slot  

def get_filename(path):
    filename = os.path.basename(path)
    name_without_ext = os.path.splitext(filename)[0]
    parts = name_without_ext.split('_')
    month_monthpart = '_'.join(parts[-3:])
    
    return month_monthpart

def is_nan_value(value):
    """
    Checks for various NaN representations in Python.

    Args:
        value: The value to check.

    Returns:
        True if the value is NaN, False otherwise.
    """
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, str) and value.lower() in ('nan', 'none', 'inf', '-inf', '+inf', 'na', ''):
        return True
    if not pd.notna(value):
        return True
    if isinstance(value, np.floating) and np.isnan(value):
        return True
    return False

def _load_json(path) -> dict:
    # Accept both str and Path
    path = Path(path)

    try:
        with path.open("r") as file:
            return json.load(file)
    except FileNotFoundError as e:
        raise FileNotFoundError(f"JSON file not found at {path}") from e
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON format in {path}") from e
    
    
    
def load_matched_data_streaming(file_path, batch_size=1000):
    """
    Stream matched-trip data and yield batches mapping trip_id -> list(points).
    Supports old JSON (ijson), gzipped NDJSON, and parquet written as rows
    (columns: trip_id, t, lat, lon). Converts epoch 't' -> ISO timestamp strings.

    Assumes parquet was written grouped by trip_id (rows for a trip are contiguous).
    """
    batch = {}
    count = 0
    logging.info(f"Streaming from {file_path}")

    # helper to push a completed trip into current batch and yield when full
    def _add_trip(batch_dict, tid, pts):
        nonlocal count
        if not pts or len(pts) < 10:  # Skip trips with <10 points
            return
        batch_dict[tid] = pts
        count += 1

    # parquet path: stream row-groups and detect trip_id boundaries (fast, low-mem)
    if file_path.lower().endswith(('.parquet', '.parq')):
        try:
            import pyarrow.parquet as pq
            pf = pq.ParquetFile(file_path)
            current_tid = None
            current_pts = []

            for rg in range(pf.num_row_groups):
                table = pf.read_row_group(rg, columns=['trip_id', 't', 'lat', 'lon'])
                df = table.to_pandas()
                # iterate rows in file order
                for _, row in df.iterrows():
                    tid = str(row['trip_id'])
                    # convert epoch seconds -> ISO8601 UTC string
                    tval = row['t']
                    try:
                        iso_t = datetime.fromtimestamp(int(tval), tz=timezone.utc).isoformat()
                    except Exception:
                        iso_t = None

                    pt = {'lat': float(row['lat']), 'lon': float(row['lon']), 'time': iso_t}

                    if current_tid is None:
                        current_tid = tid
                        current_pts = [pt]
                        continue

                    if tid == current_tid:
                        current_pts.append(pt)
                    else:
                        # trip boundary -> add previous trip
                        _add_trip(batch, current_tid, current_pts)
                        current_tid = tid
                        current_pts = [pt]

                    if count >= batch_size:
                        yield batch
                        batch = {}
                        count = 0

            # finish last trip
            if current_tid is not None and current_pts:
                _add_trip(batch, current_tid, current_pts)

            if batch:
                yield batch
            return
        except Exception:
            logging.exception("Parquet streaming failed, falling back to pandas/parquet full-read.")
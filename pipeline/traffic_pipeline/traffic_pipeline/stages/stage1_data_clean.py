"""Stage 1: Data Cleaning Framework.

This module provides the framework for cleaning raw GPS data.
Implement actual data cleaning logic.
"""

from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional
import logging
import os
from pathlib import Path
import logging
import pandas as pd
from pandas import DataFrame, Series
import re
import json
import numpy as np
from typing import Dict, List, Optional, Tuple, DefaultDict
from tqdm import tqdm
import glob
import io
from datetime import datetime

from traffic_pipeline.pipeline.base import BaseStage, DataNode, PipelineConfig
from traffic_pipeline.src.utils.tools import is_nan_value, _load_json

class DataCleanStage(BaseStage):
    """Stage 1: Clean raw GPS trajectory and trip data.

    Validates and cleans:
    - Missing timestamps
    - Invalid coordinates
    - Duplicate records
    - Outlier values

    Input:
        - trajectories: Raw trajectory records
        - trips: Raw trip records

    Output:
        - cleaned_trajectories: Cleaned trajectory records
        - cleaned_trips: Cleaned trip records
    """
    def __init__(self, config: PipelineConfig):
        """Initialize data cleaning stage.

        Args:
            config: Pipeline configuration
        """
        super().__init__(config, "data_clean")
        self.clean_config: Dict[str, Any] = self.config.processing.get('clean', {})
        
        self.traj: Optional[pd.DataFrame] = None
        self.trip: Optional[pd.DataFrame] = None
        
        self.numerical_cols: List[str] = ['fare', 'distance']
        
        self.taxi_dict_path: Path = Path(
            self.clean_config.get('taxi_dict_path', "data/road_data/taxi_to_number_dict.json")
        )
        self.taxi_type_path: Path = Path(
            self.clean_config.get('taxi_type_path', "data/road_data/taxi_type_dict.json")
        )
        
        self.start_reference: pd.Timestamp = pd.Timestamp(
            self.clean_config.get("start_reference", "2025-02-03 00:00:00")
        ).tz_localize(None)
        
        self.hk_utc_adjustment: int = self.clean_config.get("hk_utc_adjustment", 28800)
        self.fare_upper_bound: float = float(self.clean_config.get("fare_upper_bound", 700))
        
    def validate_input(self, data: DataNode) -> bool:
        """Validate input data has required fields.

        Args:
            data: Input data node

        Returns:
            True if input is valid
        """
        has_trajectories = data.trajectories is not None and len(data.trajectories) > 0
        has_trips = data.trips is not None and len(data.trips) > 0

        return has_trajectories or has_trips
    
    def _extract_coordinates(self, df: DataFrame, column: str, lat_col: str, lon_col: str) -> DataFrame:
        """Extract latitude and longitude from a POINT column."""
        try:
            coords = df[column].str.extract(r'POINT\((\S+) (\S+)\)')
            df[lat_col] = coords[1].astype(float)
            df[lon_col] = coords[0].astype(float)
        except Exception as e:
            raise ValueError(f"Failed to extract coordinates from '{column}': {e}") from e
        
        return df
    
    def _remove_outliers(self, df: pd.DataFrame, col: str) -> DataFrame:
        """Remove outliers from a DataFrame column using IQR method with custom fare cap."""
        Q1, Q99 = df[col].quantile([0.01, 0.99])
        IQR = Q99 - Q1
        upper_bound = self.fare_upper_bound if col == 'fare' else Q99 + IQR
        return df[df[col].le(upper_bound)]              # df[(df[col] >= lower_bound) & (df[col] <= upper_bound)]
    
    def fix_milliseconds(self, s: str) -> str:
        # If no milliseconds, add .000 before ' UTC'
        if re.match(r'.*\d{2}:\d{2}:\d{2} UTC$', s):
            return s.replace(' UTC', '.000 UTC')
        # If milliseconds present but less than 3 digits, pad zeros
        m = re.match(r'(.*\d{2}:\d{2}:\d{2}\.)(\d{1,2})( UTC)', s)
        if m:
            prefix, ms, suffix = m.groups()
            ms_padded = ms.ljust(3, '0')  # pad to length 3
            return f"{prefix}{ms_padded}{suffix}"
        # Otherwise, return as is
        return s
    
    def _convert_time(self, df: DataFrame, column: str) -> Series:
        """Convert a DataFrame column to datetime, handling timezone issues."""
        # Fix the string format first
        df[f'{column}'] = df[f'{column}'].apply(self.fix_milliseconds)
        df_clone = df.copy()
        df[f'{column}'] = pd.to_datetime(df[f'{column}'], errors='coerce')
        null_decimal_index = df[f'{column}'].isnull()

        # This should not be necessary after we fix the string format above
        if null_decimal_index.any():  # Check if there are any True values
            df.loc[null_decimal_index, f'{column}'] = df_clone.loc[null_decimal_index, f'{column}'].str.replace(' UTC', '.000 UTC').str.replace(r'(\d{2}:\d{2}:\d{2})\.(\d)', r'\1.\200')

        return df[column]
    
    def _validate_no_nan(self, df: DataFrame, columns: List[str]) -> None:
        """Assert no NaN-like values in specified DataFrame columns."""
        for column in columns:
            nan_count = df[column].apply(is_nan_value).sum()
            if nan_count > 0:
                raise ValueError(f"Found {nan_count} NaN-like values in '{column}'")
            
    def _get_max_car_number(self, license_series: Series) -> Optional[int]:
        """Extract the car numbers from the license column"""
        car_numbers = license_series.str.extract(r'(\d+)')[0].astype(int)
        return car_numbers.max() if not car_numbers.empty else None
    
    def _get_car_time_ranges(self, trip: DataFrame, car_label: str) -> Optional[List[Tuple[float, float]]]:
        """Extract time ranges for a specific car from trip data."""
        car_data = trip[trip['license_index'] == car_label]
        if car_data.empty:
            return None
        return car_data[['start_time_value', 'end_time_value']].values.tolist()
    
    def _process_car_trajectory(
        self, 
        traj: DataFrame, 
        car_label: str, 
        time_ranges: List[Tuple[float, float]], 
        save_path: str
    ) -> None:
        """Process trajectory data for a car, determine hire status, and save to CSV."""
        car_trajectory = traj[traj['license_index'] == car_label].copy()
        if car_trajectory.empty:
            return

        # Determine hire status based on time ranges
        car_trajectory['hire_status'] = car_trajectory['time_value'].apply(
            lambda x: 'TRUE' if any(start <= x <= end for start, end in time_ranges) else 'FALSE'
        )

        # Save to CSV
        output_file = os.path.join(save_path, f'filtered_trajectory_{car_label}.csv')
        car_trajectory.to_csv(output_file, index=False)
        print(f"Saved {car_label} trajectory to {output_file}")
        
    def _combine_car_files(self, max_car_number: int, save_path: str, combined_path: str) -> None:
        """Combine individual car trajectory files into a single CSV."""
        # Remove existing combined file if it exists
        if os.path.exists(combined_path):
            os.remove(combined_path)

        header_written = False
        with open(combined_path, 'w', encoding='utf-8') as combined_file:
            for i in range(1, max_car_number + 1):
                car_label = f"Car{i}"
                car_file = os.path.join(save_path, f'filtered_trajectory_{car_label}.csv')
                if not os.path.exists(car_file):
                    continue

                for chunk in pd.read_csv(car_file, chunksize=self.clean_config.get('chunk_size', 100000), encoding='utf-8'):
                    chunk.to_csv(
                        combined_file,
                        header=not header_written,
                        index=False
                    )
                    header_written = True

        print(f"Combined CSV saved to {combined_path}")
            
    def indicate_hire_status(self, determine_hiring_status: bool = False, combine_car_data: bool = True) -> None:
        """
        Determine hire status for each car and optionally combine car-specific data into a single CSV.

        Args:
            determine_hiring_status: If True, calculate and save hire status for each car.
            combine_car_data: If True, combine individual car files into a single CSV.

        Raises:
            ValueError: If input DataFrames are not initialized or processing fails.
            FileNotFoundError: If input files are missing.
        """
        
        if self.traj is None or self.trip is None:
            raise ValueError("Trajectory or trip data not initialized")
        
        # Load the Trajectory CSV file and Trip CSV file
        traj = self.traj.copy()
        trip = self.trip.copy()

        # Get the maximum car number
        max_car_number = self._get_max_car_number(traj['license_index'])
        if max_car_number == 0:
            raise ValueError("No valid car numbers found in trajectory data")

        # Define output directory
        save_path = os.path.join(self.config.output_dir, "stage1_data_clean", "by_car")
        os.makedirs(save_path, exist_ok=True)

        if determine_hiring_status:
            print("\nDetermining hiring status...")
            car_time_ranges: Dict[str, Optional[List[Tuple[float, float]]]] = {}

            # Process each car
            with tqdm(total=max_car_number, desc="Processing cars") as pbar:
                for i in range(1, max_car_number + 1):
                    car_label = f"Car{i}"
                    time_ranges = self._get_car_time_ranges(trip, car_label)
                    car_time_ranges[car_label] = time_ranges

                    if time_ranges:
                        self._process_car_trajectory(traj, car_label, time_ranges, save_path)
                    pbar.update(1)

            print("All cars processed")

        if combine_car_data:
            combined_path = os.path.join(
                save_path,
                "combined_heartbeat_sort_timeval_hiringstat.csv"
            )
            self._combine_car_files(max_car_number, save_path, combined_path)
            for f in glob.glob(f"{save_path}/filtered_trajectory*.csv"):
                os.remove(f)

            self.traj = pd.read_csv(combined_path)
            self.traj = self.traj[self.traj['hire_status'] == True].copy()
                   
    def clean_data(self) -> None:
        traj = self.traj.copy() if self.traj is not None else None
        trip = self.trip.copy() if self.trip is not None else None
        
        traj.rename(columns={'f0_': 'meter_id'}, inplace=True)
        if not (len(traj[traj['device_time'].isnull() & traj['server_time'].notnull()]) == 0 and \
            len(traj[traj['device_time'].isna() & traj['server_time'].notna()]) == 0):
            mask = traj['device_time'].isnull()
            traj.loc[mask, 'device_time'] = traj.loc[mask, 'server_time']     

        # Drop rows with NaN in critical columns
        traj_cols = ['device_time', 'meter_id', 'location']
        trip_cols = ['trip_start', 'trip_end', 'start_location', 'end_location']
        if all(col in traj for col in traj_cols):
            traj.dropna(subset=traj_cols, inplace=True)
        if all(col in trip for col in trip_cols):
            trip.dropna(subset=trip_cols, inplace=True)

        # Filter out invalid device_time rows
        traj = traj[traj['device_time'] != 'device_time']

        # Convert time columns to datetime
        traj['device_time'] = self._convert_time(traj, 'device_time')
        trip['trip_start'] = self._convert_time(trip, 'trip_start')
        trip['trip_end'] = self._convert_time(trip, 'trip_end')

        # Load mapping dictionaries
        meter_mapping = _load_json(self.taxi_dict_path)
        taxi_type_dict = _load_json(self.taxi_type_path)
        
        # Update taxi type dictionary to avoid error in the database
        # Get unique meter IDs and convert to set for efficiency
        unique_meter_ids = set(traj['meter_id'].unique())
        # Combine all existing IDs (excluding 'Other') into a set
        existing_ids = set().union(*(ids for key, ids in taxi_type_dict.items()))
        # Find new IDs not in existing_ids
        new_ids = unique_meter_ids - existing_ids
        # Update 'Other' key with new IDs
        taxi_type_dict['Other'] = taxi_type_dict.get('Other', []) + list(new_ids)
        with open(self.taxi_dict_path, 'w') as f: 
            json.dump(taxi_type_dict, f, indent=4, sort_keys=True)

        # Update meter mapping for new meter IDs
        unique_meter_ids = traj['meter_id'].unique()            # Use the traj here instead of the trip because the traj has more unique meter IDs
        unique_meter_ids = np.unique(np.concatenate([unique_meter_ids, trip['license_plate'].unique()])) # change
        next_id = len(meter_mapping.values()) + 1
        for meter_id in unique_meter_ids:
            if meter_id not in meter_mapping:
                meter_mapping[meter_id] = f'Car{next_id}'
                next_id += 1

        # Save updated meter mapping
        with self.taxi_dict_path.open('w') as file:
            json.dump(meter_mapping, file, indent=2)

        # Map meter IDs and extract license numbers
        traj['license_index'] = traj['meter_id'].map(meter_mapping)
        traj['license_number'] = traj['license_index'].str.extract(r'(\d+)').astype(int)

        # Map license plates # change
        # unique_license_plates = trip['license_plate'].unique()
        # license_mapping = {
        #     plate: meter_mapping.get(plate, f'Car{i+1}')
        #     for i, plate in enumerate(unique_license_plates)
        # }
        trip['license_index'] = trip['license_plate'].map(meter_mapping)
        trip['license_number'] = trip['license_index'].str.extract(r'(\d+)').astype(int)

        # Validate no NaN-like values
        self._validate_no_nan(trip, ['start_location', 'end_location', 'trip_start', 'trip_end'])

        # This is time-consuming, so we can comment it out for now
        # self._validate_no_nan(traj, ['location', 'device_time', 'meter_id'])

        trip = self._extract_coordinates(trip, 'start_location', 'lat', 'long')
        trip = self._extract_coordinates(trip, 'end_location', 'lat_end', 'long_end')
        traj = self._extract_coordinates(traj, 'location', 'lat', 'long')
        
        # Remove invalid trips based on coordinate conditions
        # Convert coordinate columns to float
        coords = ['lat', 'long', 'lat_end', 'long_end']
        trip[coords] = trip[coords].astype(float)
        
        # Define invalid conditions
        same_point = (trip['lat'] == trip['lat_end']) & (trip['long'] == trip['long_end'])
        near_same_point = (abs(trip['lat'] - trip['lat_end']) < 0.001) & (abs(trip['long'] - trip['long_end']) < 0.001)
        zero_start = (trip['lat'] == 0) & (trip['long'] == 0)
        zero_end = (trip['lat_end'] == 0) & (trip['long_end'] == 0)
        same_lat_long = (trip['lat'] == trip['long']) | (trip['lat_end'] == trip['long_end'])
        
        # Filter out invalid trips
        trip = trip[~(same_point | near_same_point | zero_start | zero_end | same_lat_long)]
        
        # Create DataFrame with rows having non-outlier values in all numerical columns.
        cleaned_trips = [self._remove_outliers(trip, col) for col in self.numerical_cols]
        common_indices = list(set.intersection(*[set(df.index) for df in cleaned_trips]))
        trip = trip.loc[common_indices].copy()
        
        # change
        # Remove the trips and trajectory data for license plates that appear less than or equal to 10 times
        # plate_counts = trip['license_plate'].value_counts()
        # trip = trip[~trip['license_plate'].isin(plate_counts[plate_counts <= 10].index)]
        # traj = traj[~traj['meter_id'].isin(plate_counts[plate_counts <= 10].index)]
        
        # Select relevant columns
        traj = traj[[
            'meter_id', 'speed', 'device_time', 'server_time',
            'license_index', 'license_number', 'lat', 'long'
        ]]          # Drop
        trip = trip[[
            'extra', 'fare', 'tip', 'discount', 'payment_type', 'distance',
            'wait_time', 'license_plate', 'trip_start', 'trip_end',
            'license_index', 'license_number', 'lat', 'long', 'lat_end', 'long_end', 'make', 'model', 'id'
        ]]          # Drop 'id', 'createdAt', 'start_location', 'end_location', 'session_id', 'make', 'model'

        
        # Sort DataFrames
        traj.sort_values(by=['license_number', 'device_time'], inplace=True)
        trip.sort_values(by=['license_number', 'trip_start'], inplace=True)
        
        # # Calculate time values
        # try:
        #     traj['time_value'] = (
        #         (traj['device_time'].astype('datetime64[ns]') - self.start_reference.tz_localize(None)).dt.total_seconds() +
        #         self.hk_utc_adjustment
        #     ).round()
        #     trip['start_time_value'] = (
        #         (trip['trip_start'].astype('datetime64[ns]') - self.start_reference.tz_localize(None)).dt.total_seconds() +
        #         self.hk_utc_adjustment
        #     ).round()
        #     trip['end_time_value'] = (
        #         (trip['trip_end'].dt.tz_convert(None).astype('datetime64[ns]')  - self.start_reference.tz_localize(None)).dt.total_seconds() +
        #         self.hk_utc_adjustment
        #     ).round()
        # except Exception as e:
        #     raise ValueError(f"Failed to calculate time values: {e}") from e
        
        # Calculate time values
        try:
            start_ref = self.start_reference.tz_localize(None)

            traj['time_value'] = (
                (traj['device_time'].dt.tz_localize(None) - start_ref).dt.total_seconds() +
                self.hk_utc_adjustment
            ).round()

            trip['start_time_value'] = (
                (trip['trip_start'].dt.tz_localize(None) - start_ref).dt.total_seconds() +
                self.hk_utc_adjustment
            ).round()

            trip['end_time_value'] = (
                (trip['trip_end'].dt.tz_localize(None) - start_ref).dt.total_seconds() +
                self.hk_utc_adjustment
            ).round()
        except Exception as e:
            raise ValueError(f"Failed to calculate time values: {e}") from e

        # Remove duplicated values
        duplicates = traj.duplicated(subset=['meter_id', 'time_value'], keep=False)
        if duplicates.any():
            traj = traj.drop_duplicates(subset=['meter_id', 'time_value'], keep='first')
        duplicates = trip.duplicated(subset=['license_plate', 'start_time_value'], keep=False)
        if duplicates.any():
            trip = trip.drop_duplicates(subset=['license_plate', 'start_time_value'], keep='first')
        
        self.trip = trip
        self.traj = traj
        
        self.indicate_hire_status(determine_hiring_status=True, combine_car_data=True)
    
            
    def build_trip_lookup(self):
        """Build trip lookup table"""
        logging.info(f"Building trip lookup table from ...")
        try:
            df_trips = self.trip
            df_trips['trip_start'] = pd.to_datetime(df_trips['trip_start'], format='mixed', utc=True)
            df_trips['trip_end'] = pd.to_datetime(df_trips['trip_end'], format='mixed', utc=True)
            
            trips_lookup = defaultdict(list)
            for _, row in df_trips.iterrows():
                trips_lookup[row['license_plate']].append((
                    row['trip_start'], row['trip_end'], row['id']
                ))
            
            logging.info(f"Trip lookup table built, containing {len(trips_lookup)} unique vehicles.")
            return trips_lookup
        except FileNotFoundError:
            logging.error(f"Error: File  not found.")
            return None
        
    def process_gps_data(
        self, 
        trips_lookup: Optional[Dict[str, List[Tuple[pd.Timestamp, pd.Timestamp, Any]]]]
    ) -> DefaultDict[Any, List[Dict[str, Any]]]:
        """Process GPS data and match to trips"""
        
        if trips_lookup is None:
            return defaultdict(list)
                    
        logging.info(f"Processing GPS data ...")
        
        processed_trips_gps: DefaultDict[Any, List[Dict[str, Any]]] = defaultdict(list)
        
        csv_buffer = io.StringIO()
        self.traj.to_csv(csv_buffer, index=False)
        csv_buffer.seek(0)

        try:
            gps_iterator = pd.read_csv(
                csv_buffer,                   
                chunksize=self.clean_config.get('chunk_size', 100000),
                iterator=True,
                usecols=['meter_id', 'long', 'lat', 'device_time', 'hire_status']
            )
            
            for i, chunk in enumerate(gps_iterator):
                logging.info(f"  - Processing GPS data chunk {i+1}...")
                chunk = chunk[chunk['hire_status']==True].copy()
                chunk['device_time'] = pd.to_datetime(chunk['device_time'], format='mixed', utc=True)
                chunk['location'] = 'POINT(' + chunk['long'].astype(str) + ' ' + chunk['lat'].astype(str) + ')'
                
                for _, row in chunk.iterrows():
                    # vehicle_id = row['f0_']
                    vehicle_id = row['meter_id']
                    gps_time = row['device_time']
                    
                    if vehicle_id in trips_lookup:
                        for trip_start, trip_end, trip_id in trips_lookup[vehicle_id]:
                            if trip_start <= gps_time <= trip_end:
                                # match = re.search(r"POINT\((.+?)\s(.+?)\)", row['location'])
                                # if match:
                                lon, lat = row['long'], row['lat']  # float(match.group(1)), float(match.group(2))
                                processed_trips_gps[trip_id].append({
                                    "lat": lat, 
                                    "lon": lon, 
                                    "time": gps_time,
                                    "vehicle_id": vehicle_id
                                })
        
        except FileNotFoundError:
            logging.error(f"Error.")
            return defaultdict(list)
        
        # Sort by time
        for trip_id in processed_trips_gps:
            processed_trips_gps[trip_id].sort(key=lambda p: p['time'])
            
        logging.info(f"Processing completed. Matched GPS data for {len(processed_trips_gps)} trips.")
        
        return processed_trips_gps
    
    def to_epoch(self, t: Any) -> Optional[int]:
        if t is None:
            return None
        if isinstance(t, (int, float)):
            return int(t)
        if isinstance(t, datetime):
            return int(t.timestamp())
        if isinstance(t, str):
            try:
                if t.endswith('Z'):
                    return int(datetime.fromisoformat(t[:-1] + "+00:00").timestamp())
                return int(datetime.fromisoformat(t).timestamp())
            except Exception:
                try:
                    return int(pd.to_datetime(t).timestamp())
                except Exception:
                    return None
        return None
    
    def save_matched_data_zip(self, processed_trips_gps: DefaultDict[Any, List[Dict[str, Any]]]) -> None:
        """
        Save matched trip data compactly and quickly.

        Preferred output: Parquet (fast, small). If pyarrow is not installed, write
        gzipped NDJSON as a fallback. Timestamps -> epoch seconds (int), lat/lon
        rounded to configurable precision to reduce size.
        """
        os.makedirs(self.config.output_dir / "stage1_data_clean", exist_ok=True)
        precision = 5
        parquet_path = os.path.join(self.config.output_dir, "stage1_data_clean", f"processed_trips_gps.parquet")
        ndjson_gz_path = os.path.join(self.config.output_dir, "stage1_data_clean", f"processed_trips_gps.ndjson.gz")

        # Try Parquet (batched) for smallest+fastest I/O
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq

            batch_size = self.clean_config.get('parquet_batch_size', 100000)
            trip_col = []
            t_col = []
            lat_col = []
            lon_col = []
            writer = None

            for trip_id, points in processed_trips_gps.items():
                tid = str(trip_id)
                for p in points:
                    ts = self.to_epoch(p.get('time'))
                    if ts is None:
                        continue
                    try:
                        lat = round(float(p.get('lat')), precision)
                        lon = round(float(p.get('lon')), precision)
                    except Exception:
                        continue
                    trip_col.append(tid)
                    t_col.append(ts)
                    lat_col.append(lat)
                    lon_col.append(lon)

                    if len(t_col) >= batch_size:
                        table = pa.Table.from_pydict({
                            'trip_id': trip_col,
                            't': t_col,
                            'lat': lat_col,
                            'lon': lon_col
                        })
                        if writer is None:
                            writer = pq.ParquetWriter(parquet_path, table.schema, compression='snappy')
                        writer.write_table(table)
                        trip_col.clear(); t_col.clear(); lat_col.clear(); lon_col.clear()

            if t_col:
                table = pa.Table.from_pydict({
                    'trip_id': trip_col,
                    't': t_col,
                    'lat': lat_col,
                    'lon': lon_col
                })
                if writer is None:
                    writer = pq.ParquetWriter(parquet_path, table.schema, compression='snappy')
                writer.write_table(table)
                trip_col.clear(); t_col.clear(); lat_col.clear(); lon_col.clear()

            if writer is not None:
                writer.close()
            logging.info(f"Matched data saved compactly to {parquet_path}")
            return
        except Exception:
            # Fallback: write compact gzipped NDJSON (streaming)
            import gzip
            import json
            with gzip.open(ndjson_gz_path, 'wt', encoding='utf-8') as out:
                for trip_id, points in processed_trips_gps.items():
                    compact_pts = []
                    for p in points:
                        ts = self.to_epoch(p.get('time'))
                        if ts is None:
                            continue
                        try:
                            lat = round(float(p.get('lat')), precision)
                            lon = round(float(p.get('lon')), precision)
                        except Exception:
                            continue
                        compact_pts.append({'t': ts, 'lat': lat, 'lon': lon})
                    if not compact_pts:
                        continue
                    rec = {'trip_id': str(trip_id), 'points': compact_pts}
                    out.write(json.dumps(rec, separators=(',', ':')) + '\n')
            logging.info(f"Matched data saved compacted (gzipped NDJSON) to {ndjson_gz_path}")

    def process(self, data: DataNode) -> DataNode:
        """Clean raw GPS data.

        Args:
            data: Input data node with raw trajectories and trips

        Returns:
            DataNode with cleaned trajectories and trips

        TODO: Implement actual cleaning logic:
            1. Fill missing device_time from server_time
            2. Drop rows with NaN in critical columns
            3. Convert time columns to datetime
            4. Parse JSON location strings to lat/lon
            5. Filter invalid trips (same start/end, zero coords)
            6. Remove numerical outliers
            7. Sort by vehicle ID and time
            8. Compute time_value fields
            9. Drop duplicates
        """
        self.logger.info("Processing data cleaning stage")

        self.traj = data.trajectories.copy() if data.trajectories is not None else None
        self.trip = data.trips.copy() if data.trips is not None else None
        
        unclean_trips_len = len(self.trip) if self.trip is not None else 0
        unclean_traj_len = len(self.traj) if self.traj is not None else 0
        self.clean_data()
        print(f"Cleaned trajectory: {unclean_traj_len} -> {len(self.traj) if self.traj is not None else 0}")
        print(f"Cleaned trips: {unclean_trips_len} -> {len(self.trip) if self.trip is not None else 0}")
        
        trips_lookup = self.build_trip_lookup()
        processed_trips_gps = self.process_gps_data(trips_lookup)
        self.save_matched_data_zip(processed_trips_gps)
        
        cleaned_data = DataNode(
            trajectories=data.trajectories,
            trips=data.trips,
            cleaned_trajectories=self.traj.copy() if self.traj is not None else None,
            cleaned_trips=self.trip.copy() if self.trip is not None else None,
            metadata={
                **data.metadata,
                "stage": "data_clean",
                "original_trajectory_count": len(data.trajectories) if data.trajectories is not None else 0,
                "original_trip_count": len(data.trips) if data.trips is not None else 0,
            }
        )
        
        return cleaned_data

    def save_output(self, data: DataNode, output_dir: Path) -> bool:
        """Save cleaned data to output directory.

        Args:
            data: Data node with cleaned data
            output_dir: Output directory

        Returns:
            True if save successful
        """
        output_dir = output_dir / "stage1_data_clean"
        output_dir.mkdir(parents=True, exist_ok=True)

        # TODO: Save cleaned data to CSV files
        self.logger.info(f"Data clean stage output saved to {output_dir}")

        return True
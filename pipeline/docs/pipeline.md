
### 1️⃣ System Architecture Overview

#### 1.1 Data Flow Pipeline

Heartbeat GPS Data → Data Clean → Map Matching → Speed Calculation → Missing Speed Filling → Speed Smoothing (EMA) → DCT Encoding


### 2️⃣ Data Processing Pipeline (Step-by-Step)

#### Stage 1: Data Clean

**Input**: Raw heartbeat GPS data (taxi traces)  

**Output**: Clean GPS data

I**nvalid Data Patterns Need to Be Clean:**

1. Rows where `device_time` is missing but `server_time` is present (imputed from `server_time`).
2. Rows with `NaN` in critical columns (`device_time`, `meter_id`, `location` for trajectories and `trip_start`, `trip_end`, `start_location`, `end_location` for trips).
3. Trips with identical or near‑identical start/end coordinates, zero coordinates, or swapped latitude/longitude (e.g., `lat == long`).
4. Outlier trips in numerical columns (e.g., fare, distance) that fall outside plausible ranges.
5. Duplicated trajectory or trip records for the same vehicle and time (same `meter_id`/`license_plate` and `time_value`/`start_time_value`).

**Steps:**

1. Fill missing `device_time` values with `server_time` where `device_time` is null but `server_time` is not.
2. Drop rows where any of the critical trajectory or trip columns are `NaN`.
3. Convert `device_time`, `trip_start`, and `trip_end` to proper datetime objects using a helper `_convert_time`.
4. Parse JSON‑style location strings into numeric `lat`/`long` columns for both trajectories and trips using `_extract_coordinates`.
5. Filter out invalid trips based on coordinate conditions:
    - Start and end at the same point or very close points.
    - Zero coordinates at start or end.
    - `lat == long` at either end.
    - ***Note**: For Dec 1, 2025, 1,207 out of 15,823 trips were removed (about 7.63%). For Dec 2, 1,501 out of 15,970 were removed (9.40%), and for Dec 3, 1,331 out of 16,182 were removed (8.22%).*
6. Remove outliers in numerical trip columns (`extra`, `fare`, `tip`, `discount`, `distance`, `wait_time`) by iteratively applying `_remove_outliers` and keeping only rows that survive all filters.
7. Sort trajectories by `license_number` and `device_time`, and trips by `license_number` and `trip_start`.
8. Compute `time_value`‑style seconds‑since‑reference fields for both trajectories and trips, adjusted for Hong Kong time.
9. Drop duplicates based on `(meter_id, time_value)` for trajectories and `(license_plate, start_time_value)` for trips, keeping the first occurrence.
10. Write cleaned trajectories and trips to CSV files (`combined_heartbeat_sort_timeval.csv` and `combined_trip_sort_timeval.csv`).

**Cleaning Noise in Speed**

- **Addressing the jumping GPS point issue**: smoothing "dips" in the speeds list.
    - For every **inner point,** if the value at position i is **smaller than both neighbors** → it's a sudden dip. We replace that value with the **average** of the left and right neighbors.
- GPS data points linked to road segments that are fully covered (like tunnels) with no satellite reception or exhibit extreme speed fluctuations are filtered out. (see [(Cleansing) Identify Roads with Abnormal GPS](https://www.notion.so/Cleansing-Identify-Roads-with-Abnormal-GPS-2f63e751dc9c8042b115dae955daa596?pvs=21))
- **Speed outliers** for each timeslot for other roads (with *moderate* speed fluctuations (only 5-29 timeslots out of 2016)) are filtered out using IQR-based bounds.

**Key Considerations:**

- The cleaning pipeline prioritizes consistency between trajectory and trip data (same vehicle IDs, time alignment, coordinate validity) so downstream map‑matching and speed‑calculation stages receive coherent input.
- Coordinate‑based trip filters (same/near‑same points, zero coordinates, swapped lat/lon) help remove obviously corrupted records that would distort speed and route estimates.
- Cleaning speed noise preserves overall trajectory while removing implausible drops in speeds

---

#### Stage 2: Map Matching

**Input:**  GPS data (lists of traces)

**Output:** Map-matched GPS points with device edge IDs

**Steps:**

1. For each trip, extract the trip ID and the raw GPS trace (list of points with `lat`, `lon`, and `time`).
2. Call `match_with_session(gps_trace)` to obtain Valhalla map‑matching results, which include `edges` and `matched_points`.
    - This method performs **Valhalla map matching** on GPS traces using the `trace_attributes` endpoint at `http://localhost:8002/trace_attributes`. It converts a GPS trace (list of lat/lon dicts) into Valhalla's required `shape` format, sends a POST request with `"costing": "auto"` (auto vehicle routing) and `"shape_match": "map_snap"` (snaps GPS points to nearest road edges even if noisy).
3. If the trace has fewer than `min_gps_points` or the map‑matching result is missing or lacks `edges`, skip the trip and return an empty list.
4. Attach edge information to each GPS point via `matched_points`, so every point is associated with a Valhalla edge index and, ultimately, an `id.`

**Key Considerations:**

1. Map matching must be robust to noisy GPS traces. Very short traces are discarded early. See [(Map Matching) Heartbeat Data → Edge Id Tag](https://www.notion.so/Map-Matching-Heartbeat-Data-Edge-Id-Tag-de43e751dc9c828fae3d01cd84f124e6?pvs=21) for comparison of the trace_attributes (for map matching) and locate point.
2. The `matched_points` array must remain aligned with the filtered GPS trace so that each GPS point still correctly references its edge.

---

#### Stage 3: Speed Calculation

**Input**: Map-matched GPS points with timestamps  

**Output**: Raw speed values per edge per time bucket  

**Steps:**

1. Parse the GPS trace into arrays of `lat`, `lon`, and `time.`
2. If the trace has more than one point, compute distances between consecutive points using `accurate_distance_np` and time differences in hours via `times.diff()[1:].total_seconds() / 3600.0`.
    - The `accurate_distance_np` function computes **precise geodesic distances** between GPS coordinate pairs using Astropy's astronomical coordinate system. It converts input lat/lon values to `Quantity` objects with degree units, transforms them into 3D Earth locations at sea level (`height=0`) via `EarthLocation.from_geodetic`, creates `SkyCoord` objects in the ITRS frame, calculates the **angular separation** between points using `separation()`, converts this to radians, multiplies by Earth's radius (`R_earth.value`) for arc length in meters, and returns the final distance in **kilometers.**
    - This method provides sub-meter accuracy for GPS trajectory analysis by properly accounting for Earth's curvature, unlike simple Euclidean or Haversine approximations.
3. Compute raw speeds in km/h as `dists / time_diffs`, setting the first speed to 0.
4. Apply a simple smoothing pass over the speed array to fix the jumping issue: for interior points, if a speed is lower than both neighbors, replace it with the average of those neighbors.
5. Filter out GPS points where the elapsed time to the previous point exceeds 10 seconds.
6. For each matched point (except the first), extract the GPS‑computed `speed_kph` and check that it falls within configured bounds (`min_speed < speed_kph < max_speed`) and that the point is not `"unmatched"` and has a valid edge index.
7. For valid points, map the Valhalla `edge_index` to the corresponding `id` and accumulate the observed `speed_kph` into `edge_to_speed_length_dict` along with the edge length.
8. Convert the GPS timestamp of each valid point into a 5‑minute time slot within the week (slot values range from 0 to 2015) using `weekday`, `hour`, and `minute // 5`, yielding a `slot` index. `0` means Sunday 12AM-12:05AM, `1` means Sunday 12:05AM-12:10AM, etc.
9. Finally, append a record of the form `{"id": id, "slot": slot, "speed_kph": speed_kph}` to the `results` list.

**Key Considerations:**

- The 10‑second elapsed‑time filter and speed‑bounds checks help remove implausible speeds before aggregation.
- The 5‑minute weekly time slot (`slot`) allows aggregation into per‑edge, per‑time‑bucket speed statistics (e.g., average speed per road segment and time bin).

---

#### Stage 4: Empty Slots Filling

**Input**: Incomplete Speed List 

**Output:** Full Speed List

**Steps:**

- The input `way_time_speeds` is a dictionary keyed by `edge_id`, where each value is a `time_dict` mapping time slots `(0–2015)` to lists of observed speeds.
- For each `edge_id`, a deep copy of its `time_dict` is made so that the original structure is not mutated during processing.
- Method 1 – **Temporal neighborhood filling**:
    - For each time slot `t` in `TIMESLOTS` (0 to 2015), if `t` is missing in `time_dict`, define a window `[t - neighbor_size, t + neighbor_size]` clipped to `[0, 2015]`. By efault, `neighbor_size` is set to be 3.
    - Collect all speed values from all slots within this window that exist in `time_dict`.
    - If any speeds are found, compute their average and store it as a singleton list `time_dict[t] = [avg]`.
- Method 2 – **Day‑of‑week pattern filling**:
    - For each missing slot `t`, decompose it into `time_of_day = t % SLOTS_PER_DAY` and `current_day = t // SLOTS_PER_DAY`. Note that `SLOTS_PER_DAY = 2016/7`.
    - For each of the other 6 days of the week, compute the corresponding slot `other_t = time_of_day + day * SLOTS_PER_DAY`.
    - If `other_t` exists in `time_dict`, collect its speed values.
    - If any speeds are found across the other days, compute their average and store it as `time_dict[t] = [avg]`.
- Then the updated `time_dict` is written back into `way_time_speeds[edge_id]`.
- By default, we use Method 1 for the temporal speed filling see [(Performance) Measure ETA performance of different versions](https://www.notion.so/Performance-Measure-ETA-performance-of-different-versions-2f53e751dc9c80e0afbff95bf08c97b3?pvs=21).

**Key Considerations:**

- The 2016 time slots represent one full week of 5‑minute intervals (288 per day × 7 days), so the filling logic preserves weekly periodicity.
- Method 1 assumes that nearby time slots are similar. Method 2 assumes that the same time‑of‑day on different days is similar.

---

#### Stage 5: Speed Profile Generation

**Input**: Raw speed values per edge  

**Output**: Smoothed 2016-bucket speed profile per edge  

**Steps:**

1. Apply EMA smoothing (span=24) to reduce noise
2. Fill missing speed values for incomplete profiles
3. Generate complete 2016-bucket speed profile per edge

**Key Considerations:**

- EMA span=24 is validated optimal parameter
- Missing value strategy affects profile quality
- Verify profile completeness before encoding



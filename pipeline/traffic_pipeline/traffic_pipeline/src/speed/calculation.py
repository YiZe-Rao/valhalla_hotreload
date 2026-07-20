
# Specialized libraries
from astropy.units import Quantity
from astropy.coordinates import SkyCoord, EarthLocation
from astropy.constants import R_earth

import numpy as np
import pandas as pd

def accurate_distance_np(lat1, lon1, lat2, lon2):
    lon1 = Quantity(lon1, unit='deg')
    lat1 = Quantity(lat1, unit='deg')
    lon2 = Quantity(lon2, unit='deg')
    lat2 = Quantity(lat2, unit='deg')
    pts1 = SkyCoord(EarthLocation.from_geodetic(lon1, lat1, height=0).itrs, frame='itrs')
    pts2 = SkyCoord(EarthLocation.from_geodetic(lon2, lat2, height=0).itrs, frame='itrs')
    sep = pts2.separation(pts1)
    dist_m = np.deg2rad(sep) * R_earth.value
    return dist_m.value / 1000.0  # in km

def fix_speed_jumping(speeds):
    s = speeds.copy()
    for i in range(1, len(s)-1):
        if speeds[i] < speeds[i-1] and speeds[i] < speeds[i+1]:
            s[i] = (speeds[i-1] + speeds[i+1]) / 2
    assert len(s) == len(speeds)
    speeds = s
    
    return speeds
            
def calculate_speeds_for_trace(gps_trace, valhalla_results):
    lats = np.array([point['lat'] for point in gps_trace])
    lons = np.array([point['lon'] for point in gps_trace])
    times = pd.to_datetime([point['time'] for point in gps_trace])
    
    # Compute distances and time diffs vectorized
    if len(gps_trace) > 1:
        dists = accurate_distance_np(lats[:-1], lons[:-1], lats[1:], lons[1:])
        time_diffs = times.diff()[1:].total_seconds() / 3600.0
        speeds = np.zeros(len(gps_trace))
        speeds[1:] = dists / time_diffs.where(time_diffs > 0, np.nan)  # Avoid div by zero, use nan or 0
        time_diffs = [0] + time_diffs.tolist()
        
        # Addressing the jumping issue
        speeds = fix_speed_jumping(speeds)
        
        kept = [i for i in range(len(gps_trace)) if i == 0 or time_diffs[i] * 3600 <= 10]

        # Apply the same filter to everything
        gps_trace = [gps_trace[i] for i in kept]
        speeds = [speeds[i] for i in kept]
        time_diffs = [time_diffs[i] for i in kept]
        valhalla_results['matched_points'] = [valhalla_results['matched_points'][i] for i in kept]

        # Update dicts (fast loop, no heavy ops)
        for i in range(len(gps_trace)):
            gps_trace[i]['speed'] = speeds[i]                   # First is 0
            gps_trace[i]['time_ela'] = time_diffs[i]*3600       # First is 0
    else:
        gps_trace[0]['speed'] = 0.0
        
    return gps_trace, valhalla_results
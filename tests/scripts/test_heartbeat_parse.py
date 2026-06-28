#!/usr/bin/env python3
import csv
import sys

HEARTBEAT_FILE = sys.argv[1] if len(sys.argv) > 1 else '/app_heartbeat.csv'
MAX_RECORDS = int(sys.argv[2]) if len(sys.argv) > 2 else 500

records = []
with open(HEARTBEAT_FILE, 'r') as f:
    reader = csv.reader(f)
    next(reader)
    for i, row in enumerate(reader):
        if i >= MAX_RECORDS:
            break
        if len(row) < 5:
            continue
        location = row[2]
        if 'POINT' not in location:
            continue
        coords = location.replace('POINT(', '').replace(')', '').split()
        if len(coords) != 2:
            continue
        try:
            lon, lat = float(coords[0]), float(coords[1])
            speed = float(row[4]) if row[4] else 0
            if not (22.0 <= lat <= 22.6 and 113.8 <= lon <= 114.3):
                continue
            if speed <= 0 or speed > 150:
                continue
            records.append(speed)
        except (ValueError, IndexError):
            continue

count = len(records)
if count > 0:
    avg = sum(records) / count
    mn = min(records)
    mx = max(records)
    print("HEARTBEAT_RECORDS=%d" % count)
    print("HEARTBEAT_AVG=%.1f" % avg)
    print("HEARTBEAT_MIN=%.1f" % mn)
    print("HEARTBEAT_MAX=%.1f" % mx)
    print("HEARTBEAT_INT_AVG=%d" % int(avg))
else:
    print("HEARTBEAT_RECORDS=0")
    print("HEARTBEAT_AVG=0")
    sys.exit(1)

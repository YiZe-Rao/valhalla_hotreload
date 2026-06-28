# Speed File Format Specification

## Valhalla Historical Traffic Format

Speed files must follow Valhalla's historical traffic CSV format:

```csv
edge_id,freeflow_speed,constrained_speed,historical_speeds
1/47701/130,50,40,AQ0AAAAAAA...
1/47701/131,50,40,AQ0AAAAAAA...
```

## Column Specifications

### edge_id

Internal graph ID in format: `level/tile_id/id`

Examples:
- `0/003/015`
- `1/47701/130`

This maps to Valhalla's tile hierarchy.

### freeflow_speed

Typical nighttime speed in km/h.

- Represents uncongested traffic conditions
- Used when no historical traffic data is available

### constrained_speed

Typical daytime speed in km/h.

- Represents normal traffic conditions
- Used as baseline for traffic calculations

### historical_speeds

DCT-II encoded string of 2016 speed values.

- 2016 values = 288 slots/day × 7 days
- 5-minute intervals starting Sunday 00:00
- Encoded as base64 string

## Encoding Details

### DCT-II Encoding

The 2016 speed values are compressed using Discrete Cosine Transform:

```python
# Pseudocode for encoding
coefficients = dct(speed_values)           # Apply DCT-II
encoded = encode_compressed(coefficients)  # Convert to base64 string
```

### Speed Buckets

| Bucket | Description |
|--------|-------------|
| 0-287 | Sunday (5-min intervals) |
| 288-575 | Monday |
| 576-863 | Tuesday |
| 864-1151 | Wednesday |
| 1152-1439 | Thursday |
| 1440-1727 | Friday |
| 1728-2015 | Saturday |

## Tile Hierarchy

Files are organized in Valhalla's tile hierarchy:

```
traffic_data/
├── 0/
│   ├── 000/
│   │   └── 001.csv
│   └── 003/
│       └── 015.csv
└── 1/
    ├── 047/
    │   └── 701.csv
    └── 120/
        └── 342.csv
```

### Path Resolution

To find the path for an edge:

1. Get `graph_id` from Valhalla
2. Use `GraphTile::FileSuffix(graph_id)` to get path
3. Create CSV file at that path

## Example Output

### Sample CSV Content

```csv
edge_id,freeflow_speed,constrained_speed,historical_speeds
0/000/001,60,45,AQ0AAAAAAAAC4AAAA...
0/000/002,55,40,AQ0AAAAAAC3AAAA...
1/120/342,70,55,AQ0AAAAAAB7AAAA...
```

### Decoding Example

```python
import base64
import numpy as np
from scipy.fft import dct

def decode_historical_speeds(encoded_string):
    """Decode DCT-encoded speed values."""
    # Decode base64 to coefficients
    coefficients = np.frombuffer(
        base64.b64decode(encoded_string),
        dtype=np.int16
    )

    # Apply inverse DCT
    speed_values = idct(coefficients, norm='ortho')

    return speed_values

# Usage
speeds = decode_historical_speeds("AQ0AAAAAAA...")
print(f"Speed at slot 0: {speeds[0]:.2f} km/h")
print(f"Speed at slot 1000: {speeds[1000]:.2f} km/h")
```

## Validation

Use Valhalla's `valhalla_add_predicted_traffic` tool to validate:

```bash
valhalla_add_predicted_traffic -c valhalla.json -t traffic_data/
```

## References

- [Valhalla Historical Traffic Documentation](https://valhalla.github.io/valhalla/mjolnir/historical_traffic/)
- [Valhalla Source: compress_speed_buckets](https://github.com/valhalla/valhalla/blob/master/scripts/)
- [Valhalla Source: encode_compressed_speeds](https://github.com/valhalla/valhalla/blob/master/scripts/)

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This repository demonstrates how to add traffic support to the Valhalla routing engine. It includes:

- **valhalla/** - The Valhalla routing engine (submodule with custom modifications)
- **prime_server/** - HTTP server dependency for Valhalla services
- **valhalla_code_overwrites/** - Custom CMakeLists and `valhalla_traffic_demo_utils.cc` for traffic features
- **valhalla_tiles/** - Generated routing tiles and configuration (created during build)

## Build Commands

### Full build via build script
```bash
./build.sh
```

### Start the service
```bash
./run_service.sh
```

### Docker build (alternative)
```bash
docker build -t valhalla-traffic .
docker run -p 8002:8002 -it valhalla-traffic bash
```

### Manual build steps
```bash
# Build prime_server
cd prime_server
./autogen.sh && ./configure && make install -j1

# Build valhalla
cd valhalla
mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Debug -DENABLE_SINGLE_FILES_WERROR=False
make -j$(nproc) install
```

### Debugging with GDB
```bash
gdb --args valhalla_service valhalla_tiles/valhalla.json 1
```

## Key Tools

- `valhalla_traffic_demo_utils` - Custom utility for generating/managing traffic data
- `valhalla_build_tiles` - Build routing tiles from OSM data
- `valhalla_ways_to_edges` - Generate OSM way to Valhalla edge mappings
- `valhalla_add_predicted_traffic` - Add predicted traffic to tiles
- `valhalla_service` - Start the HTTP routing service

## Traffic Data Types

1. **Predicted traffic** - Time-based speeds via CSV files in tile hierarchy
2. **Live traffic** - Real-time speeds via `traffic.tar` memory-mapped file

## Architecture

The custom traffic functionality is implemented in `valhalla_traffic_demo_utils.cc` which:
- Uses Valhalla's internal `baldr::GraphReader` and `mjolnir::GraphTileBuilder`
- Links against `microtar` library for `.tar` file manipulation
- Reads/writes traffic data to Valhalla tile directories

Key CMake modifications in `valhalla_code_overwrites/`:
- `CMakeLists.txt` - Adds `valhalla_traffic_demo_utils` to `valhalla_data_tools`
- `src/CMakeLists.txt` - Adds `microtar` library dependency to valhalla target

## Workflow

1. Build generates map tiles from OSM data (Andorra by default)
2. `valhalla_ways_to_edges` creates `way_edges.txt` mapping OSM IDs to Valhalla edge IDs
3. `update_traffic.py` generates traffic CSV for specific OSM ways
4. `valhalla_add_predicted_traffic` embeds CSV data into tiles
5. `valhalla_traffic_demo_utils --generate-live-traffic` creates `traffic.tar`

## API Endpoints (port 8002)

- `/route` - Time-dependent routing (supports traffic via `date_time` parameter)
- `/isochrone` - Reachability areas (supports traffic)
- `/locate` - Match point to nearest road, returns edge info with `predicted_speeds` and `live_speed`
- `/trace_attributes` - Map matching with edge details

## Notes

- Predicted traffic speeds must be >5 km/h to be considered
- Live traffic overrides predicted traffic
- The `traffic.tar` file must be regenerated before each service start for live updates to be picked up

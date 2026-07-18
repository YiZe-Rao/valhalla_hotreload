# Third-Party Dependencies

This project depends on the Valhalla routing engine and its dependencies.
All dependencies are managed through the Valhalla build system.

## Core Dependencies (provided by Valhalla)

| Dependency | Version | Purpose |
|-----------|---------|---------|
| Valhalla | upstream | Routing engine (git submodule at `../valhalla/`) |
| prime_server | 0.6.3+ | HTTP server for Valhalla services |
| Boost | 1.71+ | C++ utility libraries |
| Protobuf | 3.x | Protocol buffer support |
| CMake | 3.12+ | Build system |
| cxxopts | (bundled) | CLI argument parsing |
| microtar | (bundled in Valhalla) | tar file read/write |
| RapidJSON | (bundled in Valhalla) | JSON config parsing |

## Bundled Libraries (in Valhalla source)

- `microtar` — Minimal tar library used for `traffic.tar` manipulation
  - Path in Valhalla: `third_party/microtar/`
- `cxxopts` — Lightweight C++ command line option parser
  - Path in Valhalla: `third_party/cxxopts/`
- `rapidjson` — Fast JSON parser/generator
  - Path in Valhalla: `third_party/rapidjson/`

## Installation

All dependencies are installed automatically when building Valhalla.
See `scripts/build.sh` for the complete build pipeline.

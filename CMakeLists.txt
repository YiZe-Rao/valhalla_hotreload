# valhalla_hotreload — Valhalla Live Traffic Per-Edge Real-time Speed Injection
#
# This project is a set of overlay files that add live traffic (real-time speed)
# injection capabilities to the Valhalla routing engine.
#
# Build instructions:
#   1. Build prime_server first (see scripts/build.sh)
#   2. Copy the overlay CMake files into the Valhalla source tree:
#        cp cmake/CMakeLists.txt      <valhalla_src>/
#        cp cmake/src_CMakeLists.txt  <valhalla_src>/src/CMakeLists.txt
#   3. Copy source files into Valhalla:
#        cp include/valhalla_hotreload/live_traffic_utils.h <valhalla_src>/src/mjolnir/
#        cp src/live_traffic_utils.cc                       <valhalla_src>/src/mjolnir/
#        cp src/valhalla_live_traffic.cc                    <valhalla_src>/src/mjolnir/
#   4. Build Valhalla with cmake + make
#
# Or simply run: bash scripts/build.sh
#
# See docs/ for full documentation.

cmake_minimum_required(VERSION 3.12)
project(valhalla_hotreload
  VERSION 1.0.0
  DESCRIPTION "Valhalla Live Traffic Per-Edge Real-time Speed Injection"
  LANGUAGES CXX C
)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# This is an overlay project — it does not build standalone.
# The source files in src/ and include/ are designed to be compiled
# as part of the Valhalla build system (see cmake/ for overlay CMake files).
#
# To verify headers are well-formed:
#   find include/ -name '*.h' -exec g++ -std=c++17 -fsyntax-only {} \;

message(STATUS "valhalla_hotreload v${PROJECT_VERSION}")
message(STATUS "This is an overlay project for Valhalla — see README.md and docs/ for build instructions.")

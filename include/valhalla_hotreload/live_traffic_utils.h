#ifndef VALHALLA_MJOLNIR_LIVE_TRAFFIC_UTILS_H_
#define VALHALLA_MJOLNIR_LIVE_TRAFFIC_UTILS_H_

#include <cstdint>
#include <tuple>
#include <unordered_map>
#include <vector>
#include <valhalla/baldr/traffictile.h>
#include <boost/property_tree/ptree.hpp>

namespace valhalla {
namespace mjolnir {

// tile_id → [(edge_index, speed_kph, congestion)]
using EdgeSpeedMap =
    std::unordered_map<uint64_t,
                       std::vector<std::tuple<uint32_t, float, uint8_t>>>;

/**
 * Encode (speed_kph, congestion) into a TrafficSpeed bitfield.
 * Uses the Traffictile.h bit layout: 2kph resolution, 7-bit encoded speed.
 *
 * @param speed_kph   Speed in km/h. Clamped to [0, 254].
 * @param congestion  Congestion level 1–63. 0 = unknown. Default 1 (no congestion).
 * @return TrafficSpeed with breakpoint1=255, breakpoint2=255 (full edge coverage).
 */
baldr::TrafficSpeed encode_live_speed(float speed_kph, uint8_t congestion = 1);

/**
 * Update specific edges in an existing traffic.tar via mmap in-place editing.
 * Non-specified edges retain their existing values.
 *
 * @param mjolnir_pt   The "mjolnir" subtree from valhalla.json.
 * @param speed_map    tile_id → list of (edge_index, speed_kph, congestion).
 * @param timestamp    Update timestamp (epoch seconds), written to header.last_update.
 * @return Number of edges actually updated.
 */
uint32_t update_edge_live_speeds(const boost::property_tree::ptree& mjolnir_pt,
                                 const EdgeSpeedMap& speed_map,
                                 uint64_t timestamp);

/**
 * Create a new traffic.tar, populating only edges specified in speed_map.
 * Unspecified edges are set to INVALID_SPEED (breakpoint=0 → speed_valid()=false).
 * Only tiles with data in speed_map are written to the tar.
 *
 * @param mjolnir_pt   The "mjolnir" subtree from valhalla.json.
 * @param speed_map    tile_id → list of (edge_index, speed_kph, congestion).
 * @param timestamp    Creation timestamp (epoch seconds).
 * @return Number of edges actually filled with valid speeds.
 */
uint32_t build_live_traffic_from_edges(const boost::property_tree::ptree& mjolnir_pt,
                                       const EdgeSpeedMap& speed_map,
                                       uint64_t timestamp);

} // namespace mjolnir
} // namespace valhalla

#endif

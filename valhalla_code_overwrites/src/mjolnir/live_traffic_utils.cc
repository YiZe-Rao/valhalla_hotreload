#include "live_traffic_utils.h"
#include "baldr/graphreader.h"
#include "config.h"
#include "filesystem.h"
#include "microtar.h"
#include "mjolnir/graphtilebuilder.h"
#include "rapidjson/document.h"

#include <algorithm>
#include <cstring>
#include <fstream>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

namespace valhalla {
namespace mjolnir {

// ---------------------------------------------------------------------------
// MMap helper — replicates the pattern from valhalla_traffic_demo_utils.cc
// ---------------------------------------------------------------------------
struct MMap {
  MMap(const char* filename) {
    fd = open(filename, O_RDWR);
    if (fd < 0)
      throw std::runtime_error("Cannot open " + std::string(filename));
    struct stat s;
    fstat(fd, &s);
    data = mmap(nullptr, s.st_size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (data == MAP_FAILED)
      throw std::runtime_error("mmap failed");
    length = s.st_size;
  }
  ~MMap() {
    munmap(data, length);
    close(fd);
  }
  int fd;
  void* data;
  size_t length;
};

// ---------------------------------------------------------------------------
// Copy of raw tar header for offset calculation (matches valhalla_traffic_demo_utils.cc)
// ---------------------------------------------------------------------------
typedef struct {
  char name[100];
  char mode[8];
  char owner[8];
  char group[8];
  char size[12];
  char mtime[12];
  char checksum[8];
  char type;
  char linkname[100];
  char _padding[255];
} mtar_raw_header_t_;

// ---------------------------------------------------------------------------
// MMapGraphMemory — bridges mmap'd memory to baldr::GraphMemory for TrafficTile
// ---------------------------------------------------------------------------
class MMapGraphMemory final : public baldr::GraphMemory {
public:
  MMapGraphMemory(std::shared_ptr<MMap> mmap, char* data_, size_t size_)
      : mmap_(std::move(mmap)) {
    data = data_;
    size = size_;
  }

private:
  const std::shared_ptr<MMap> mmap_;
};

// ===========================================================================
//  Step 1: encode_live_speed
// ===========================================================================
baldr::TrafficSpeed encode_live_speed(float speed_kph, uint8_t congestion) {
  // Convert speed to 2-kph-resolution encoded value
  uint32_t raw = static_cast<uint32_t>(speed_kph / 2.0f);
  // Clamp to valid range (max is UNKNOWN_TRAFFIC_SPEED_RAW - 1, since UNKNOWN is reserved)
  if (raw > baldr::UNKNOWN_TRAFFIC_SPEED_RAW - 1) {
    raw = baldr::UNKNOWN_TRAFFIC_SPEED_RAW - 1;
  }
  // Clamp congestion to valid range [0, 63]
  if (congestion > baldr::MAX_CONGESTION_VAL) {
    congestion = baldr::MAX_CONGESTION_VAL;
  }

  // Return full-edge coverage: breakpoint1=255 means the first sub-segment covers
  // the entire edge; breakpoint2=255 is unused (no third sub-segment).
  return baldr::TrafficSpeed{
      raw,                          // overall_encoded_speed
      raw, raw, raw,                // encoded_speed1/2/3
      255,                          // breakpoint1  -> full edge
      255,                          // breakpoint2  -> unused
      congestion, congestion, congestion,  // congestion1/2/3
      0                             // has_incidents
  };
}

// ===========================================================================
//  Step 2: update_edge_live_speeds — in-place mmap editing of traffic.tar
// ===========================================================================
uint32_t update_edge_live_speeds(const boost::property_tree::ptree& mjolnir_pt,
                                 const EdgeSpeedMap& speed_map,
                                 uint64_t timestamp) {
  std::string traffic_path = mjolnir_pt.get<std::string>("traffic_extract");
  if (!filesystem::exists(traffic_path)) {
    throw std::runtime_error("traffic.tar not found: " + traffic_path);
  }

  // mmap the whole tar into memory for in-place editing
  auto memory = std::make_shared<MMap>(traffic_path.c_str());

  // Setup microtar callbacks operating on the mmap'd region
  mtar_t tar;
  std::memset(&tar, 0, sizeof(tar));
  tar.pos = 0;
  tar.stream = memory->data;
  tar.read = [](mtar_t* t, void* buf, unsigned sz) -> int {
    std::memcpy(buf, reinterpret_cast<char*>(t->stream) + t->pos, sz);
    return MTAR_ESUCCESS;
  };
  tar.write = [](mtar_t* t, const void* buf, unsigned sz) -> int {
    std::memcpy(reinterpret_cast<char*>(t->stream) + t->pos, buf, sz);
    return MTAR_ESUCCESS;
  };
  tar.seek = [](mtar_t*, unsigned) -> int { return MTAR_ESUCCESS; };
  tar.close = [](mtar_t*) -> int { return MTAR_ESUCCESS; };

  uint32_t updated_count = 0;
  mtar_header_t tar_header;

  while ((mtar_read_header(&tar, &tar_header)) != MTAR_ENULLRECORD) {
    // Parse tile_id from filename (e.g. "0/003/015.gph")
    try {
      auto tile_id = baldr::GraphTile::GetTileId(tar_header.name);

      auto speed_it = speed_map.find(tile_id.value);
      if (speed_it == speed_map.end()) {
        mtar_next(&tar);
        continue; // tile not in update list
      }

      // Construct TrafficTile pointing into the mmap'd region,
      // accounting for the tar header prefix before the tile data.
      char* tile_data =
          reinterpret_cast<char*>(tar.stream) + tar.pos + sizeof(mtar_raw_header_t_);
      baldr::TrafficTile tile(
          std::make_unique<MMapGraphMemory>(memory, tile_data, tar_header.size));

      // Update header timestamp
      const_cast<volatile baldr::TrafficTileHeader*>(tile.header)->last_update = timestamp;

      // Update specified edges
      for (const auto& entry : speed_it->second) {
        uint32_t edge_idx = std::get<0>(entry);
        float speed_kph = std::get<1>(entry);
        uint8_t congestion = std::get<2>(entry);

        if (edge_idx >= tile.header->directed_edge_count) {
          LOG_WARN("Edge index " + std::to_string(edge_idx) +
                   " out of bounds for tile " + std::to_string(tile_id.value) +
                   " (max " + std::to_string(tile.header->directed_edge_count) + ")");
          continue;
        }
        auto* current = const_cast<baldr::TrafficSpeed*>(&tile.speeds[edge_idx]);
        *current = encode_live_speed(speed_kph, congestion);
        updated_count++;
      }
    } catch (...) {
      // skip non-tile entries (e.g. directory entries)
    }
    mtar_next(&tar);
  }

  msync(memory->data, memory->length, MS_SYNC);
  return updated_count;
}

// ===========================================================================
//  Step 3: build_live_traffic_from_edges — create a fresh traffic.tar from
//  a speed_map, using the actual GraphReader tile metadata for edge counts.
// ===========================================================================
uint32_t build_live_traffic_from_edges(const boost::property_tree::ptree& mjolnir_pt,
                                       const EdgeSpeedMap& speed_map,
                                       uint64_t timestamp) {
  std::string traffic_path = mjolnir_pt.get<std::string>("traffic_extract");

  // Ensure parent directory exists
  filesystem::path parent = filesystem::path(traffic_path).parent_path();
  if (!filesystem::exists(parent)) {
    throw std::runtime_error("Traffic extract directory does not exist: " +
                             parent.string());
  }

  baldr::GraphReader reader(mjolnir_pt);
  uint32_t filled_count = 0;

  mtar_t tar;
  auto open_result = mtar_open(&tar, traffic_path.c_str(), "w");
  if (open_result != MTAR_ESUCCESS) {
    throw std::runtime_error("Could not create traffic tar: " + traffic_path);
  }

  // Walk each tile in speed_map
  for (const auto& kv : speed_map) {
    uint64_t tile_id_raw = kv.first;
    const auto& edge_speeds = kv.second;

    baldr::GraphId tile_graph_id(tile_id_raw);
    auto tile = reader.GetGraphTile(tile_graph_id);
    if (!tile) {
      LOG_WARN("Tile not found: " + std::to_string(tile_id_raw));
      continue;
    }

    uint32_t edge_count = tile->header()->directededgecount();

    // Build tile binary: header + speed array
    std::stringstream buffer;
    baldr::TrafficTileHeader header = {};
    header.tile_id = tile_graph_id.Tile_Base().value;
    header.last_update = timestamp;
    header.traffic_tile_version = baldr::TRAFFIC_TILE_VERSION;
    header.directed_edge_count = edge_count;
    buffer.write(reinterpret_cast<const char*>(&header), sizeof(header));

    // Build a lookup for fast access during the edge loop
    std::unordered_map<uint32_t, std::pair<float, uint8_t>> edge_lookup;
    for (const auto& entry : edge_speeds) {
      edge_lookup[std::get<0>(entry)] = {std::get<1>(entry), std::get<2>(entry)};
    }

    for (uint32_t i = 0; i < edge_count; ++i) {
      auto it = edge_lookup.find(i);
      if (it != edge_lookup.end()) {
        auto ts = encode_live_speed(it->second.first, it->second.second);
        buffer.write(reinterpret_cast<const char*>(&ts), sizeof(ts));
        filled_count++;
      } else {
        // INVALID_SPEED: breakpoint1=0 => speed_valid() returns false
        baldr::TrafficSpeed invalid = {
            baldr::UNKNOWN_TRAFFIC_SPEED_RAW,
            baldr::UNKNOWN_TRAFFIC_SPEED_RAW,
            baldr::UNKNOWN_TRAFFIC_SPEED_RAW,
            baldr::UNKNOWN_TRAFFIC_SPEED_RAW,
            0, 0, 0, 0, 0, 0  // breakpoints=0 => invalid
        };
        buffer.write(reinterpret_cast<const char*>(&invalid), sizeof(invalid));
      }
    }

    // Padding (matches existing build_live_traffic_data pattern)
    uint32_t dummy = 0;
    buffer.write(reinterpret_cast<const char*>(&dummy), sizeof(dummy));
    buffer.write(reinterpret_cast<const char*>(&dummy), sizeof(dummy));

    std::string tile_data = buffer.str();
    std::string filename = baldr::GraphTile::FileSuffix(tile_graph_id);

    if (mtar_write_file_header(&tar, filename.c_str(), tile_data.size()) !=
        MTAR_ESUCCESS) {
      mtar_close(&tar);
      throw std::runtime_error("Could not write tar header for " + filename);
    }
    if (mtar_write_data(&tar, tile_data.c_str(), tile_data.size()) != MTAR_ESUCCESS) {
      mtar_close(&tar);
      throw std::runtime_error("Could not write tar data for " + filename);
    }
  }

  mtar_finalize(&tar);
  mtar_close(&tar);
  return filled_count;
}

} // namespace mjolnir
} // namespace valhalla

#include "realtime_traffic_updater.h"

#include <valhalla/baldr/graphreader.h>
#include <valhalla/baldr/graphtile.h>
#include <valhalla/midgard/logging.h>

#include <fstream>
#include <algorithm>
#include <cmath>
#include <ctime>
#include <tar.h>
#include <sstream>
#include <cstring>

namespace valhalla {
namespace baldr {

RealtimeTrafficUpdater::RealtimeTrafficUpdater(
    GraphReader& reader,
    const std::string& traffic_dir,
    uint32_t update_interval_seconds,
    uint32_t speed_window_seconds)
    : reader_(reader)
    , traffic_dir_(traffic_dir)
    , update_interval_seconds_(update_interval_seconds)
    , speed_window_seconds_(speed_window_seconds) {

    // 初始化文件路径
    active_traffic_path_ = traffic_dir + "/traffic_active.tar";
    standby_traffic_path_ = traffic_dir + "/traffic_standby.tar";
    next_traffic_path_ = traffic_dir + "/traffic_next.tar.new";

    // 检查 traffic 目录是否存在
    if (!filesystem::exists(traffic_dir)) {
        LOG_WARN("Traffic directory does not exist, will create: " + traffic_dir);
        filesystem::create_directories(traffic_dir);
    }

    LOG_INFO("RealtimeTrafficUpdater initialized:");
    LOG_INFO("  traffic_dir: " + traffic_dir);
    LOG_INFO("  update_interval: " + std::to_string(update_interval_seconds) + "s");
    LOG_INFO("  speed_window: " + std::to_string(speed_window_seconds) + "s");
}

std::unordered_map<uint64_t, std::unordered_map<uint32_t, float>>
RealtimeTrafficUpdater::AggregateSpeedsByTile(const std::vector<HeartbeatRecord>& records) {
    int64_t now = std::time(nullptr);

    // 1. 将 records 按 edge 分组并更新缓存
    // 注意：这里需要 edge_index，但 heartbeat 只有 location
    // 实际使用时需要通过 map-matching 获取 edge_index
    // 这里简化处理，假设 records 已包含 edge_index

    // 按 tile 聚合速度
    std::unordered_map<uint64_t, std::unordered_map<uint32_t, float>> tile_speeds;

    for (const auto& record : records) {
        // 计算 edge_index (简化：使用 location hash 模拟)
        // 实际应调用 map-matcher
        uint32_t pseudo_edge_index = static_cast<uint32_t>(
            std::abs(record.lat * 100000) + std::abs(record.lon * 100000)) % 100000;

        // 计算 tile_id
        // Valhalla tile 编码：tile_id = (level << 22) | tile_index
        // 这里使用简化计算
        uint64_t tile_id = pseudo_edge_index / 1000;  // 简化：每 tile 约 1000 条边

        // 添加到缓存
        edge_speed_cache_[pseudo_edge_index].push_back({record.speed, now});
    }

    // 2. 计算每条边的最终速度（时间衰减加权平均）
    int64_t cutoff = now - speed_window_seconds_;
    int64_t half_window = speed_window_seconds_ / 2;

    for (auto& [edge_index, samples] : edge_speed_cache_) {
        // 清理过期数据
        samples.erase(
            std::remove_if(samples.begin(), samples.end(),
                [cutoff](const SpeedSample& s) { return s.timestamp < cutoff; }),
            samples.end()
        );

        if (samples.empty()) {
            continue;
        }

        // 时间衰减加权平均：越近的数据权重越高
        float weighted_sum = 0.0f;
        float weight_total = 0.0f;

        for (const auto& sample : samples) {
            int64_t age = now - sample.timestamp;
            // 线性衰减：0 秒时权重 1.0，window 秒时权重 0.1
            float weight = std::max(0.1f, 1.0f - static_cast<float>(age) / speed_window_seconds_);
            weighted_sum += sample.speed * weight;
            weight_total += weight;
        }

        if (weight_total > 0.0f) {
            float avg_speed = weighted_sum / weight_total;

            // 确定 tile_id
            uint64_t tile_id = edge_index / 1000;

            // 过滤异常速度 (<1 km/h 或 >150 km/h)
            if (avg_speed >= 1.0f && avg_speed <= 150.0f) {
                tile_speeds[tile_id][edge_index % 1000] = avg_speed;
            }
        }
    }

    return tile_speeds;
}

TrafficSpeed RealtimeTrafficUpdater::CreateTrafficSpeed(float speed_kph, bool valid) const {
    TrafficSpeed speed = {};

    if (!valid) {
        // 无效速度：使用 UNKNOWN 标记
        speed.overall_encoded_speed = UNKNOWN_TRAFFIC_SPEED_RAW;
        speed.encoded_speed1 = UNKNOWN_TRAFFIC_SPEED_RAW;
        speed.encoded_speed2 = UNKNOWN_TRAFFIC_SPEED_RAW;
        speed.encoded_speed3 = UNKNOWN_TRAFFIC_SPEED_RAW;
        speed.breakpoint1 = 0;
        speed.breakpoint2 = 0;
        speed.congestion1 = 0;
        speed.congestion2 = 0;
        speed.congestion3 = 0;
        speed.has_incidents = 0;
        speed.spare = 0;
        return speed;
    }

    uint8_t encoded = EncodeSpeed(speed_kph);

    // 计算拥堵程度（简化：基于速度分段）
    uint8_t congestion = 0;
    if (speed_kph < 10) {
        congestion = 50;  // 严重拥堵
    } else if (speed_kph < 20) {
        congestion = 30;  // 中度拥堵
    } else if (speed_kph < 40) {
        congestion = 15;  // 轻度拥堵
    } else {
        congestion = 5;   // 畅通
    }

    speed.overall_encoded_speed = encoded;
    speed.encoded_speed1 = encoded;
    speed.encoded_speed2 = encoded;
    speed.encoded_speed3 = encoded;
    speed.breakpoint1 = 255;  // 速度覆盖整条边
    speed.breakpoint2 = 255;
    speed.congestion1 = congestion + 1;  // 1-63 范围
    speed.congestion2 = congestion + 1;
    speed.congestion3 = congestion + 1;
    speed.has_incidents = 0;
    speed.spare = 0;

    return speed;
}

bool RealtimeTrafficUpdater::BuildTrafficTar(
    const std::string& output_path,
    const std::unordered_map<uint64_t, std::unordered_map<uint32_t, float>>& tile_speeds) {

    if (tile_speeds.empty()) {
        LOG_WARN("No tile speeds to write");
        return false;
    }

    // 使用 microtar 库写入 tar 文件
    mtar_t tar;
    int ret = mtar_open(&tar, output_path.c_str(), "w");
    if (ret != MTAR_ESUCCESS) {
        LOG_ERROR("Failed to create traffic tar file: " + output_path);
        return false;
    }

    for (const auto& [tile_id, edge_speeds] : tile_speeds) {
        // 构建 TrafficTile buffer
        std::stringstream buffer;

        // 写入 header (24 字节)
        TrafficTileHeader header = {};
        header.tile_id = tile_id;
        header.last_update = std::time(nullptr);
        header.traffic_tile_version = TRAFFIC_TILE_VERSION;
        header.directed_edge_count = static_cast<uint32_t>(edge_speeds.size());
        header.spare2 = 0;
        header.spare3 = 0;

        buffer.write(reinterpret_cast<char*>(&header), sizeof(header));

        // 写入每条边的 TrafficSpeed (8 字节/边)
        for (const auto& [edge_index, speed_kph] : edge_speeds) {
            TrafficSpeed speed = CreateTrafficSpeed(speed_kph);
            buffer.write(reinterpret_cast<char*>(&speed), sizeof(speed));
        }

        // 写入填充数据（与原有格式兼容）
        uint32_t dummy = 0;
        buffer.write(reinterpret_cast<char*>(&dummy), sizeof(dummy));
        buffer.write(reinterpret_cast<char*>(&dummy), sizeof(dummy));

        // 写入 tar
        std::string filename = GraphTile::FileSuffix(GraphId(tile_id, 0, 0));
        std::string data = buffer.str();

        ret = mtar_write_file_header(&tar, filename.c_str(), data.size());
        if (ret != MTAR_ESUCCESS) {
            LOG_ERROR("Failed to write tar file header for tile " + std::to_string(tile_id));
            mtar_close(&tar);
            return false;
        }

        ret = mtar_write_data(&tar, data.c_str(), data.size());
        if (ret != MTAR_ESUCCESS) {
            LOG_ERROR("Failed to write tar data for tile " + std::to_string(tile_id));
            mtar_close(&tar);
            return false;
        }
    }

    mtar_finalize(&tar);
    mtar_close(&tar);

    LOG_INFO("Built traffic tar: " + output_path +
             " with " + std::to_string(tile_speeds.size()) + " tiles");

    return true;
}

uint32_t RealtimeTrafficUpdater::UpdateFromHeartbeats(
    const std::vector<HeartbeatRecord>& records) {

    if (records.empty()) {
        return 0;
    }

    std::lock_guard<std::mutex> lock(update_mutex_);
    updating_.store(true);

    // 1. 聚合速度
    auto tile_speeds = AggregateSpeedsByTile(records);

    if (tile_speeds.empty()) {
        updating_.store(false);
        return 0;
    }

    // 2. 构建新的 traffic.tar
    if (!BuildTrafficTar(next_traffic_path_, tile_speeds)) {
        updating_.store(false);
        return 0;
    }

    // 3. 原子重命名：next → standby
    try {
        if (filesystem::exists(standby_traffic_path_)) {
            filesystem::remove(standby_traffic_path_);
        }
        filesystem::rename(next_traffic_path_, standby_traffic_path_);
    } catch (const std::exception& e) {
        LOG_ERROR("Failed to rename traffic tar: " + std::string(e.what()));
        updating_.store(false);
        return 0;
    }

    // 4. 计算更新的边数量
    uint32_t edge_count = 0;
    for (const auto& [tile_id, edge_speeds] : tile_speeds) {
        edge_count += static_cast<uint32_t>(edge_speeds.size());
    }
    last_updated_edge_count_.store(edge_count);

    // 5. 更新时间戳
    last_update_epoch_.store(static_cast<uint64_t>(std::time(nullptr)));

    updating_.store(false);

    LOG_INFO("Updated " + std::to_string(edge_count) + " edges across " +
             std::to_string(tile_speeds.size()) + " tiles");

    return edge_count;
}

bool RealtimeTrafficUpdater::SwitchTrafficArchive() {
    // 检查 standby 文件是否存在
    if (!filesystem::exists(standby_traffic_path_)) {
        LOG_ERROR("Standby traffic file does not exist: " + standby_traffic_path_);
        return false;
    }

    // 调用 GraphReader 的热加载方法
    // 注意：需要 GraphReader 支持 HotReloadTrafficArchive
    // 这里使用动态配置方式，通过修改配置文件触发重新加载

    try {
        // 原子切换符号链接
        std::string active_link = traffic_dir_ + "/traffic_current.tar";
        std::string temp_link = traffic_dir_ + "/traffic_temp.tar";

        // 创建新链接指向 standby
        if (filesystem::exists(active_link)) {
            filesystem::rename(active_link, temp_link);
        }
        filesystem::rename(standby_traffic_path_, active_link);

        if (filesystem::exists(temp_link)) {
            filesystem::remove(temp_link);
        }

        LOG_INFO("Switched traffic archive: " + standby_traffic_path_ + " -> " + active_link);

        // 通知 GraphReader 重新加载
        // 实际实现需要扩展 GraphReader 接口
        // 这里通过日志记录，由外部服务监听并触发重载
        LOG_INFO("Traffic archive switch complete. GraphReader reload pending.");

        return true;

    } catch (const std::exception& e) {
        LOG_ERROR("Failed to switch traffic archive: " + std::string(e.what()));
        return false;
    }
}

} // namespace baldr
} // namespace valhalla

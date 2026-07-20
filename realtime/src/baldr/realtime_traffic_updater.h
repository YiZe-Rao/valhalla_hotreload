#pragma once

#include <valhalla/baldr/graphreader.h>
#include <valhalla/baldr/traffictile.h>
#include <valhalla/baldr/graphid.h>
#include <atomic>
#include <mutex>
#include <unordered_map>
#include <vector>
#include <string>
#include <chrono>

namespace valhalla {
namespace baldr {

/**
 * Heartbeat 记录结构
 */
struct HeartbeatRecord {
    std::string id;           // UUID 设备标识
    std::string f0_;          // Base64 加密设备 ID
    float lat;                // 纬度
    float lon;                // 经度
    float bearing;            // 航向角 (度)
    float speed;              // 速度 (km/h)
    int64_t device_time;      // 设备时间 (epoch seconds)
    int64_t server_time;      // 服务器接收时间 (epoch seconds)
};

/**
 * 边速度聚合结果
 */
struct EdgeSpeed {
    uint32_t edge_index;
    float speed_kph;
    float confidence;         // 0.0 - 1.0
    int64_t last_update;      // epoch seconds
};

/**
 * 实时交通速度更新器
 * 支持不重启服务的情况下热更新 traffic.tar
 *
 * 架构设计:
 * 1. 双缓冲机制：active_tar_ ↔ standby_tar_ 交替切换
 * 2. 原子切换：利用 shared_ptr<midgard::tar> 的写时复制语义
 * 3. 滑动窗口速度聚合：保留最近 60 秒数据，时间衰减加权平均
 */
class RealtimeTrafficUpdater {
public:
    /**
     * 构造函数
     * @param reader GraphReader 引用
     * @param traffic_dir traffic.tar 所在目录
     * @param update_interval_seconds 更新间隔 (默认 5 秒)
     * @param speed_window_seconds 速度聚合窗口 (默认 60 秒)
     */
    RealtimeTrafficUpdater(GraphReader& reader,
                          const std::string& traffic_dir,
                          uint32_t update_interval_seconds = 5,
                          uint32_t speed_window_seconds = 60);

    /**
     * 从 heartbeat 数据流更新交通速度
     * @param records 新的 GPS 点集合
     * @return 成功更新的边数量
     */
    uint32_t UpdateFromHeartbeats(const std::vector<HeartbeatRecord>& records);

    /**
     * 触发 traffic.tar 切换（双缓冲）
     * @return 是否成功
     */
    bool SwitchTrafficArchive();

    /**
     * 获取当前状态
     */
    bool IsUpdating() const { return updating_.load(); }
    uint64_t LastUpdateTime() const { return last_update_epoch_; }
    uint32_t GetLastUpdatedEdgeCount() const { return last_updated_edge_count_; }

    /**
     * 获取活跃的交通文件路径
     */
    std::string GetActiveTrafficPath() const { return active_traffic_path_; }

    /**
     * 速度编码器：km/h → 7-bit 编码值
     * TrafficSpeed 使用 2kph 分辨率
     */
    static uint8_t EncodeSpeed(float speed_kph) {
        return static_cast<uint8_t>(std::min(speed_kph / 2.0f, 127.0f));
    }

    /**
     * 速度解码器：7-bit 编码值 → km/h
     */
    static float DecodeSpeed(uint8_t encoded) {
        return static_cast<float>(encoded) * 2.0f;
    }

private:
    GraphReader& reader_;
    std::string traffic_dir_;
    std::string active_traffic_path_;
    std::string standby_traffic_path_;
    std::string next_traffic_path_;

    uint32_t update_interval_seconds_;
    uint32_t speed_window_seconds_;

    std::atomic<bool> updating_{false};
    std::atomic<uint64_t> last_update_epoch_{0};
    std::atomic<uint32_t> last_updated_edge_count_{0};

    std::mutex update_mutex_;

    // 边速度缓存：edge_index -> [(speed, timestamp), ...]
    struct SpeedSample {
        float speed;
        int64_t timestamp;
    };
    std::unordered_map<uint32_t, std::vector<SpeedSample>> edge_speed_cache_;

    // 内部方法
    std::unordered_map<uint64_t, std::unordered_map<uint32_t, float>>
    AggregateSpeedsByTile(const std::vector<HeartbeatRecord>& records);

    bool BuildTrafficTar(
        const std::string& output_path,
        const std::unordered_map<uint64_t, std::unordered_map<uint32_t, float>>& tile_speeds
    );

    TrafficSpeed CreateTrafficSpeed(float speed_kph, bool valid = true) const;
};

} // namespace baldr
} // namespace valhalla

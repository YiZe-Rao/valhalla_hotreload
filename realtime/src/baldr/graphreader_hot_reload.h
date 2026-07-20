// GraphReader 热加载扩展
// 将此文件内容合并到 valhalla/valhalla/baldr/graphreader.h

#pragma once

// 在 GraphReader 类中添加以下方法（约第 520 行后插入）:

/*
 * ============================================================================
 * Realtime Traffic Hot Reload Support
 * ============================================================================
 */

/**
 * 热加载新的 traffic.tar 文件
 * 不中断现有查询的情况下切换交通数据源
 *
 * @param new_traffic_path 新 traffic.tar 路径
 * @return 成功返回 true
 *
 * 线程安全：使用写时复制 (copy-on-write) 语义
 * - 新的 GraphTile 请求会使用新 archive
 * - 旧的 GraphTile 仍持有旧 mmap 引用，直到引用计数归零
 *
 * 使用示例:
 *   GraphReader reader(config);
 *   RealtimeTrafficUpdater updater(reader, "/data/valhalla");
 *
 *   // 在后台线程中定期调用
 *   updater.UpdateFromHeartbeats(records);
 *   updater.SwitchTrafficArchive();  // 触发热加载
 */
bool HotReloadTrafficArchive(const std::string& new_traffic_path);

/**
 * 获取当前 traffic archive 的只读引用
 * 用于外部 updater 访问
 */
std::shared_ptr<midgard::tar> GetTrafficArchive() const {
    return tile_extract_ ? tile_extract_->traffic_archive : nullptr;
}

/**
 * 获取 traffic tiles 的只读引用
 */
const std::unordered_map<uint32_t, std::pair<char*, size_t>>&
GetTrafficTiles() const {
    static std::unordered_map<uint32_t, std::pair<char*, size_t>> empty;
    return tile_extract_ ? tile_extract_->traffic_tiles : empty;
}

/*
 * ============================================================================
 * 需要添加的成员变量 (在 GraphReader 类的 private 部分)
 * ============================================================================
 */

// std::mutex tile_extract_mutex_;  // 保护 tile_extract_ 的并发访问

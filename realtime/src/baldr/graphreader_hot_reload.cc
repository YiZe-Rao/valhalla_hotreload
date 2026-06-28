// GraphReader 热加载实现
// 将此文件内容合并到 valhalla/src/baldr/graphreader.cc

#include <valhalla/baldr/graphreader.h>
#include <valhalla/midgard/logging.h>
#include <valhalla/midgard/tar.h>
#include <mutex>

namespace valhalla {
namespace baldr {

/**
 * 热加载 traffic.tar 实现
 *
 * 设计要点:
 * 1. 使用 shared_ptr 的原子赋值实现原子切换
 * 2. 旧 tile 保留旧数据引用直到请求完成
 * 3. 新 tile 请求使用新数据
 * 4. 清理缓存迫使下次请求加载新数据
 */
bool GraphReader::HotReloadTrafficArchive(const std::string& new_traffic_path) {
    // 1. 验证新文件存在且有效
    if (!filesystem::exists(new_traffic_path)) {
        LOG_ERROR("Traffic archive file does not exist: " + new_traffic_path);
        return false;
    }

    // 检查文件大小
    try {
        auto file_size = filesystem::file_size(new_traffic_path);
        if (file_size < 512) {  // 最小合理大小
            LOG_ERROR("Traffic archive file too small: " + std::to_string(file_size) + " bytes");
            return false;
        }
        LOG_INFO("Traffic archive file size: " + std::to_string(file_size) + " bytes");
    } catch (const std::exception& e) {
        LOG_ERROR("Failed to check traffic archive size: " + std::string(e.what()));
        return false;
    }

    // 2. 加载新的 traffic archive 到临时对象
    std::shared_ptr<midgard::tar> new_archive;
    std::unordered_map<uint32_t, std::pair<char*, size_t>> new_traffic_tiles;

    try {
        LOG_INFO("Loading new traffic archive: " + new_traffic_path);
        new_archive = std::make_shared<midgard::tar>(new_traffic_path, true);

        // 解析索引，建立 tile_id -> data 映射
        for (auto& c : new_archive->contents) {
            try {
                auto id = GraphTile::GetTileId(c.first);
                new_traffic_tiles[id] = std::make_pair(
                    const_cast<char*>(c.second.first),
                    c.second.second
                );
            } catch (const std::exception& e) {
                // 跳过非 tile 文件
                LOG_TRACE("Skipping non-tile file in traffic archive: " + c.first);
            }
        }

        if (new_traffic_tiles.empty()) {
            LOG_ERROR("No valid traffic tiles found in new archive");
            return false;
        }

        LOG_INFO("Loaded " + std::to_string(new_traffic_tiles.size()) +
                 " traffic tiles from " + new_traffic_path);

    } catch (const std::exception& e) {
        LOG_ERROR("Failed to load new traffic archive: " + std::string(e.what()));
        return false;
    }

    // 3. 原子切换（关键点：traffic_archive 是 shared_ptr）
    // 使用内存序确保可见性
    {
        // 注意：需要在 GraphReader 中添加 tile_extract_mutex_
        // 如果不存在，可以使用 atomic 操作或省略锁（shared_ptr 赋值本身是原子的）
        std::lock_guard<std::mutex> lock(tile_extract_mutex_);

        if (tile_extract_) {
            // 保存旧的用于日志
            auto old_tile_count = tile_extract_->traffic_tiles.size();

            // 原子切换引用
            tile_extract_->traffic_archive = new_archive;
            tile_extract_->traffic_tiles = new_traffic_tiles;

            LOG_INFO("Hot-reloaded traffic archive: " + new_traffic_path +
                     " (old tiles: " + std::to_string(old_tile_count) +
                     ", new tiles: " + std::to_string(new_traffic_tiles.size()) + ")");
        } else {
            LOG_ERROR("tile_extract_ is null, cannot hot-reload traffic archive");
            return false;
        }
    }

    // 4. 清理缓存的 traffic tiles（强制下次请求重新加载）
    // 注意：已加载的 GraphTile 对象继续使用旧数据直到被释放
    LOG_INFO("Trimming tile cache to force reload of traffic tiles");
    Trim();

    return true;
}

} // namespace baldr
} // namespace valhalla

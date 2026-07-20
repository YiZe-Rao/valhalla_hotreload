# 实现 /admin/reload_traffic HTTP Endpoint — 补丁指南

## 状态

| 组件 | 状态 |
|------|------|
| `GraphReader::HotReloadTrafficArchive()` | ✅ 已编译到 valhalla_service |
| `Options::Action` 枚举中有 reload_traffic | ❌ 缺失 |
| loki_worker 注册 action + dispatch | ❌ 缺失 |
| valhalla.json 配置 loki.actions | ❌ 缺失 |

**验证方法**: `strings /usr/local/bin/valhalla_service | grep HotReload` 返回符号 = 函数已编译。

## 需要修改的文件

共 3 个文件 + 1 个配置文件:

### 1. `valhalla/proto/options.proto` — 添加 Action 枚举值

在 `enum Action` 末尾（`status = 12;` 之后）添加:

```protobuf
  enum Action {
    no_action = 0;
    route = 1;
    locate = 2;
    sources_to_targets = 3;
    optimized_route = 4;
    isochrone = 5;
    trace_route = 6;
    trace_attributes = 7;
    height = 8;
    transit_available = 9;
    expansion = 10;
    centroid = 11;
    status = 12;
+   reload_traffic = 13;  // ← 添加此行
  }
```

### 2. `valhalla/valhalla/loki/worker.h` — 添加方法声明

在 `void status(Api& request) const;` 之后添加:

```cpp
  void status(Api& request) const;
+ void reload_traffic(Api& request) const;  // ← 添加此行
```

### 3. `valhalla/src/loki/worker.cc` — 注册 action + 实现 handler

#### 3a. 在构造函数中添加 action 处理（可选，仅在 path != action name 时需要）

构造函数中已有 action 注册逻辑:
```cpp
  for (const auto& kv : config.get_child("loki.actions")) {
    auto path = kv.second.get_value<std::string>();
    if (!Options_Action_Enum_Parse(path, &action)) {
      throw std::runtime_error("Action not supported " + path);
    }
    actions.insert(action);
    action_str.append("'/" + path + "' ");
  }
```
`reload_traffic` 路径名与枚举名相同 (= "reload_traffic")，所以 `Options_Action_Enum_Parse` 会自动匹配。

#### 3b. 在 `work()` 方法的 switch 中添加 case

在 `switch (options.action())` 中，`case Options::status:` 之后添加:

```cpp
      case Options::status:
        status(request);
        result.messages.emplace_back(request.SerializeAsString());
        break;
+     case Options::reload_traffic:
+       reload_traffic(request);
+       result.messages.emplace_back(request.SerializeAsString());
+       break;
      case Options::expansion:
```

#### 3c. 实现 handler 方法

在文件末尾（`cleanup()` 之后）添加:

```cpp
void loki_worker_t::reload_traffic(Api& request) const {
  auto* reload_response = request.mutable_reload_traffic_response();

  // 从请求中读取 traffic_path
  const auto& options = request.options();
  std::string traffic_path = options.has_reload_traffic_path()
      ? options.reload_traffic_path()
      : config.get<std::string>("mjolnir.traffic_extract");

  LOG_INFO("Hot-reloading traffic archive: " + traffic_path);

  // 调用 HotReloadTrafficArchive（已在 graphreader.cc 中实现）
  if (reader->OverCommitted()) {
    reader->Trim();
  }

  bool success = reader->HotReloadTrafficArchive(traffic_path);

  reload_response->set_success(success);
  if (success) {
    reload_response->set_message("Traffic archive hot-reloaded successfully");
    reload_response->set_tiles_loaded(1);  // TODO: 从 reader 获取实际 tile 数量
    LOG_INFO("Hot-reload successful: " + traffic_path);
  } else {
    reload_response->set_message("Failed to hot-reload traffic archive");
    LOG_ERROR("Hot-reload failed: " + traffic_path);
  }
}
```

**简化版本**（如果不想修改 .proto 来添加 reload_traffic 字段，直接从 options 中解析 path）:

```cpp
void loki_worker_t::reload_traffic(Api& request) const {
  auto* status = request.mutable_status();

  // 从 valhalla.json 读取 traffic_extract 路径（默认值）
  std::string traffic_path = config.get<std::string>("mjolnir.traffic_extract");

  // 如果 request 中有 path 参数，则覆盖
  // 注意：需要在 options.proto 中添加 reload_traffic_path 字段到 Options message

  LOG_INFO("Hot-reloading traffic archive: " + traffic_path);

  bool success = reader->HotReloadTrafficArchive(traffic_path);

  status->set_has_live_traffic(success);
  if (!success) {
    throw valhalla_exception_t{150, "Failed to hot-reload traffic archive"};  // 自定义错误码
  }
}
```

### 4. `valhalla_tiles/valhalla.json` — 配置 loki.actions

在 `loki.actions` 列表中添加 `"reload_traffic"`:

```json
{
  "loki": {
    "actions": [
      "locate",
      "route",
      "sources_to_targets",
      "optimized_route",
      "isochrone",
      "trace_route",
      "trace_attributes",
      "height",
      "transit_available",
      "expansion",
      "centroid",
      "status",
      "reload_traffic"     ← 添加此行
    ]
  }
}
```

## 编译步骤

修改完成后，重新编译 valhalla:

```bash
cd /valhalla

# 重新生成 proto（如果有 proto 修改）
protoc --cpp_out=valhalla/proto proto/options.proto

# 重新编译
mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release -DENABLE_SINGLE_FILES_WERROR=False
make -j$(nproc) valhalla_service

# 安装
make install

# 重启服务
pkill valhalla_service
LD_LIBRARY_PATH=/usr/local/lib valhalla_service /valhalla_tiles/valhalla.json 1 &
```

## 测试

```bash
# 验证端点可用
curl -s -X POST http://localhost:8002/admin/reload_traffic \
    -H "Content-Type: application/json" \
    -d '{"traffic_path": "/valhalla_tiles/traffic.tar"}'

# 预期返回: {"success":true,"message":"Traffic archive hot-reloaded successfully","tiles_loaded":1}
```

## 备选方案（无需重新编译）

如果不方便重新编译 valhalla_service，当前已验证可用的方案是**重启服务**:

```bash
# 1. 修改 traffic.tar
valhalla_live_traffic --config /valhalla_tiles/valhalla.json \
    --set-edge-speed "1/40614/0,44,10,TS"

# 2. 重启 valhalla_service
pkill valhalla_service
sleep 1
LD_LIBRARY_PATH=/usr/local/lib valhalla_service /valhalla_tiles/valhalla.json 1 &

# 3. 验证（服务启动后通过 mmap 自动加载新的 traffic.tar）
curl -s -X POST http://localhost:8002/locate \
    -H "Content-Type: application/json" \
    -d '{"locations":[{"lat":22.2816,"lon":114.1585}],"verbose":true}'
# 确认 overall_speed 已更新
```

重启方案已在 valhalla-live-test 容器中验证通过（v3.1.4, 2026-07-20）。

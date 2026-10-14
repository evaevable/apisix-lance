# 源码第 2 章：配置热更新是怎么实现的

[上一章：一次 HTTP 请求如何穿过 APISIX](01-http-request-lifecycle.md) · [返回学习目录](../../README.md)

## 1. 本章要回答的问题

上一章结尾留下一个说法：路由树只在本地配置版本变化时才重建。
本章沿源码回答三个问题：

- 写配置时，校验和落库分别发生在哪里？
- 网关进程怎么知道 etcd 变了？
- 版本号是谁在什么条件下推进的？

阅读版本与上一章一致，固定到提交 `2f8ebf8527cdc878afe30100077c471879a5c133`（tag 3.14.1）。
本章只读源码，不修改上游代码，也不声称做过源码级动态调试。
说明文字是学习笔记；标记为“原文摘录”的代码来自 Apache APISIX。
上游许可证与署名见 [APISIX-LICENSE.txt](APISIX-LICENSE.txt)、[APISIX-NOTICE.txt](APISIX-NOTICE.txt)。

## 2. 先画清两条通道

热更新最容易被讲错的地方，是把「写」和「读」混成一条线。

```mermaid
flowchart TB
    O[客户端或 Dashboard] -->|PUT /apisix/admin/routes/orders| A[Admin API]
    A --> V[check_conf 校验]
    V -->|core.etcd.set 写入| E[(etcd)]
    E -. main watcher 订阅| W[本地配置同步器]
    W --> L[内存 values 与 conf_version]
    L -. 版本变化时懒重建| T[路由匹配结构]
    R[业务请求] --> T
    T --> U[后端服务]
```

> 简化模型：不展开多 worker、多网关实例、Standalone 模式与各类专用代理分支。

关键区别：

- **写通道**：Admin API 做校验，然后把配置写入 etcd。
- **读通道**：每个 worker 自己后台同步 etcd，把结果放进内存。
- **业务请求**：只读内存里的匹配结构，不参与上面两条通道。

## 3. 写通道：校验发生在写入之前

[apisix/admin/routes.lua:54–76](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/admin/routes.lua#L54-L76) 的 `check_conf` 先做结构校验：

```lua
    if conf.host and conf.hosts then
        return nil, {error_msg = "only one of host or hosts is allowed"}
    end
...
    local ok, err = core.schema.check(schema, conf)
    if not ok then
        return nil, {error_msg = "invalid configuration: " .. err}
    end

    local upstream_conf = conf.upstream
    if upstream_conf then
        local ok, err = apisix_upstream.check_upstream_conf(upstream_conf)
```

写路由时还会检查引用是否真的存在。
[78–127 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/admin/routes.lua#L78-L127) 分别对 `upstream_id`、`service_id`、`plugin_config_id` 调用 `core.etcd.get`。
[129–141 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/admin/routes.lua#L129-L141) 校验插件配置和 `vars` 表达式。

**注意这里的 etcd 读取属于写通道。**
看到 `core.etcd.get` 不能立刻推断「业务请求也在查 etcd」。

校验通过后进入真正落库。[apisix/admin/resource.lua:301](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/admin/resource.lua#L301) 是关键调用：

```lua
    local res, err = core.etcd.set(key, conf, ttl)
    if not res then
        core.log.error("failed to put ", self.kind, " [", key, "] to etcd: ", err)
        return 503, {error_msg = err}
    end
```

所以实验中 `PUT /apisix/admin/routes/orders` 返回 200，只说明**校验通过且 etcd 写入成功**，不代表所有网关已切换到新规则。

## 4. 读通道的起点：init_worker 挂载自动同步

路由配置对象在 worker 初始化时创建。
[apisix/http/route.lua:138–150](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/http/route.lua#L138-L150)：

```lua
function _M.init_worker(filter)
    local user_routes, err = core.config.new("/routes", {
            automatic = true,
            item_schema = core.schema.route,
            checker = check_route,
            filter = filter,
        })
```

三个参数值得记住：

- `automatic = true`：由后台定时器持续同步，而不是每次请求现取。
- `item_schema` 与 `checker`：读进来的每条配置也要校验。
- `/routes`：key 会被拼上前缀，最终是 `/apisix/routes`。

`core.config.new` 最终落在
[apisix/core/config_etcd.lua:1004–1079](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/core/config_etcd.lua#L1004-L1079)。
其中 [1050–1065 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/core/config_etcd.lua#L1050-L1065) 是关键：

```lua
    if automatic then
        if not key then
            return nil, "missing `key` argument"
        end
...
        ngx_timer_at(0, _automatic_fetch, obj)
```

`automatic` 为真时，注册一个定时器启动后台同步循环，而不是同步阻塞 worker 启动。
对象初始状态里 [1035–1037 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/core/config_etcd.lua#L1035-L1037) 设置 `conf_version = 0`、`need_reload = true`。

## 5. 后台循环：出错后的退避与重连

[874–972 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/core/config_etcd.lua#L874-L972) 是 `_automatic_fetch`。以下几点是上一章问题「etcd 挂了会怎样」的源码依据：

- 每轮调用 `sync_data(self)`；出错时根据错误类型决定是否等待。
- [914–931 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/core/config_etcd.lua#L914-L931)：出现「所有 etcd 端点不健康」时进入指数退避重连（1 秒起步、逐步翻倍）。
- [950–953 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/core/config_etcd.lua#L950-L953)：`timeout` 属于预期错误，不加额外的重同步延迟。
- [969–971 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/core/config_etcd.lua#L969-L971)：只要进程没退出、对象还在运行，就再次注册定时器继续下一轮。

**这解释了上一章的结论**：同步循环停摆时，网关内存里已有配置仍然存在，已有请求可以按旧规则继续代理；但新配置拿不进来，冷启动可能受阻。

## 6. 两种同步路径：全量重载与增量事件

[665–710 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/core/config_etcd.lua#L665-L710) 是 `sync_data` 的分叉点：

```lua
    if self.need_reload then
        local res, err = readdir(self.etcd_cli, self.key)
...
        load_full_data(self, dir_res, headers)

        return true
    end

    local dir_res, err = waitdir(self)
```

- `need_reload = true`：走 `readdir` 全量读取，由 `load_full_data` 重建整份数据。
- 否则：走 `waitdir`，只处理增量事件。

[701–707 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/core/config_etcd.lua#L701-L707) 说明了全量重载的触发条件：

```lua
        if err == "compacted" or err == "restarted" then
            self.need_reload = true
            log.error("waitdir [", self.key, "] err: ", err,
                     ", will read the configuration again via readdir")
```

也就是：**增量历史不可用或 etcd 重启时，退回全量重载。**
这与基础第 2 章讲的「历史被压缩后不能无限回放，需要重新同步」正好对上。

## 7. 谁在真正订阅 etcd？

这一版除了每个 key 的 `waitdir`，还有一个**主监听器**。
[apisix/core/config_etcd.lua:129–207](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/core/config_etcd.lua#L129-L207) 的 `do_run_watch`：

```lua
        watch_ctx.prefix = local_conf.etcd.prefix .. "/"
        watch_ctx.timeout = local_conf.etcd.watch_timeout
...
        watch_ctx.rev = rev + 1
...
        local res_func, err, http_cli = watch_ctx.cli:watchdir(watch_ctx.prefix, opts)
```

它用 `watchdir` 订阅整个前缀，并从 revision 位置继续。
[183–207 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/core/config_etcd.lua#L183-L207) 设置超时与起始 revision，其中起始点来自已加载配置的 `X-Etcd-Index` 或一次查询得到的最新修订号。

各 key 的 `waitdir` 并不是自己再发一个 watch，而是消费主监听器收到的事件。
[423–481 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/core/config_etcd.lua#L423-L481) 的 `http_waitdir`：

```lua
        if tonumber(res.result.header.revision) > self.prev_index then
            local res2
            for _, evt in ipairs(res.result.events) do
                if core_str.find(evt.kv.key, key) == 1 then
```

它按 key 前缀过滤事件，并用信号量等待新事件：

```lua
    watch_ctx.sema[key] = self.watch_sema
    local ok, err = self.watch_sema:wait(timeout or 60)
```

所以准确说法是：**一个主 watch 订阅前缀，各配置对象消费自己关心的那部分事件。**
这比「每个 key 各自独立长轮询」更贴近 3.14.1 的实际实现。

## 8. 数据落地与版本推进

收到事件后，[740–806 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/core/config_etcd.lua#L740-L806) 处理单条配置：

```lua
        if data_valid and res.value and self.item_schema then
            data_valid, err = check_schema(self.item_schema, res.value)
...
        if not data_valid then
            goto CONTINUE
```

**这里有一个很实用的结论：数据面自己也会校验。**
即使有人绕过 Admin API 直接往 etcd 写不合规内容，这一层校验不通过就不会更新内存缓存，事件被跳过，每条事件独立处理而不是整批丢弃。

随后按「已存在 / 新增」分别更新 `values` 与 `values_hash`，删除时把位置置为 `false`。
版本推进在两处：

- 增量路径：[837 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/core/config_etcd.lua#L837) 每成功应用一条事件 `conf_version = conf_version + 1`。
- 全量路径：[639–641 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/core/config_etcd.lua#L639-L641) 只在确有变化时 `+1`。

[648–662 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/core/config_etcd.lua#L648-L662) 的 `upgrade_version` 只允许修订号单调向前，用于过滤旧事件：

```lua
    if new_ver <= pre_index then
        return
    end

    self.prev_index = new_ver
```

## 9. 把两章接起来

完整的热更新链路是：

```text
Admin API 校验 → 写入 etcd → 主 watch 收到事件并推进 revision
   → 各配置对象按 key 过滤并校验事件 → 更新内存 values
   → conf_version 递增 → 后续请求发现版本变化 → 重建路由结构
```

两个容易混淆但必须分清的点：

- **写入成功 ≠ 全局瞬间生效**。同步在后台进行，不同 worker、不同网关实例的生效时刻可能短暂不一致。
- **版本变化 ≠ 一定重建**。是全量还是增量、是否真的改变数据，都会影响最终结果。

另外，[1195–1217 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/core/config_etcd.lua#L1195-L1217) 说明 worker 启动时如果与 master 加载配置的时间相差过大，会重新拉取全量配置。
这说明「新 worker 一定继承现有内存」也不是无条件成立的。

## 10. 先读这五处

| 顺序 | 文件与定位 | 要回答的问题 |
| --- | --- | --- |
| 1 | `apisix/admin/routes.lua`：54、78、129 | 写入前校验了什么？ |
| 2 | `apisix/admin/resource.lua`：301 | 配置最终写到哪里？ |
| 3 | `apisix/http/route.lua`：138 | 谁创建了路由配置对象？ |
| 4 | `apisix/core/config_etcd.lua`：1004、874、665 | 同步循环如何启动、如何分叉？ |
| 5 | `apisix/core/config_etcd.lua`：129、423、740、837 | 事件如何分发、校验与落地？ |

## 11. 小练习与后续节奏

1. `PUT` 返回 200 之后立刻请求，为什么仍可能短暂拿到旧规则？
2. 有人直接往 etcd 写入不合格的路由 JSON，为什么网关可能保持旧配置不变？
3. `compacted` 为什么必须触发全量重载，而不能强行回放增量？
4. 为什么说「每个业务请求都会查一次 etcd 检查配置」是错的？

本章只做静态源码阅读，未修改上游代码，也未执行源码级单元测试。
此前 Docker 实验中的热更新观察属于功能层面证据，不是本章所有内部分支的动态追踪证据。

后续按「继续」推进，每次先讲解再沉淀：

- 源码第 3 章：`key-auth` 如何识别 Consumer，插件阶段与提前拒绝。
- 源码第 4 章：轮询、健康检查与重试的具体实现。

这些是待学习主题，不是已完成的章节。

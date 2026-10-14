# 源码第 7 章：自己写一个插件

[上一章：上游 TLS 与 mTLS](06-upstream-tls-mtls.md) · [返回学习目录](../../README.md)

## 1. 本章和其他章不一样

前面六章都是读别人的代码。这一章要**真的写一个插件，让 APISIX 加载它，并观察它生效**。

配套代码已经在本仓库里：

```text
labs/01-gateway/plugins/apisix/plugins/lance-request-id.lua   插件本体
labs/01-gateway/routes/custom-plugin.json                     使用它的路由
```

阅读版本固定到提交 `2f8ebf8527cdc878afe30100077c471879a5c133`（tag 3.14.1）。
标记为“原文摘录”的代码来自 Apache APISIX；上游许可证与署名见
[APISIX-LICENSE.txt](APISIX-LICENSE.txt)、[APISIX-NOTICE.txt](APISIX-NOTICE.txt)。

## 2. 一个插件必须提供什么

插件是**普通 Lua 模块**，但要被加载器接受，必须满足几个硬性条件。
[apisix/plugin.lua:132–166](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/plugin.lua#L132-L166)：

```lua
local function load_plugin(name, plugins_list, plugin_type)
    ...
        local pkg_name = "apisix.plugins." .. name
        ...
        ok, plugin = pcall(require, pkg_name)
    end
...
    if not plugin.priority then
        core.log.error("invalid plugin [", name,
                        "], missing field: priority")
        return
    end

    if not plugin.version then
        core.log.error("invalid plugin [", name, "] missing field: version")
        return
    end

    if type(plugin.schema) ~= "table" then
        core.log.error("invalid plugin [", name, "] schema field")
        return
    end
```

三条结论：

1. **模块路径决定了插件名**：插件 `foo` 必须能被 `require("apisix.plugins.foo")` 找到。
2. **`priority`、`version`、`schema` 缺一不可**，缺了就只打一行错误日志然后**跳过这个插件**——不会让 APISIX 启动失败，所以不查日志会以为加载成功了。
3. 缺字段是**静默降级**，这是排查插件问题时的第一个检查点。

## 3. `_meta` 是自动注入的，你别自己写

[172–188 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/plugin.lua#L172-L188)：

```lua
    if plugin.schema['$comment'] ~= plugin_injected_schema['$comment'] then
        if properties._meta then
            core.log.error("invalid plugin [", name,
                           "]: found forbidden '_meta' field in the schema")
            return
        end

        properties._meta = plugin_injected_schema._meta
```

加载器会**往你的 schema 里注入 `_meta`**。
如果你自己在 schema 里定义了 `_meta`，插件会被拒绝。

### `_meta` 到底有哪些字段

定义在 [schema_def.lua:1058–1090](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/schema_def.lua#L1058-L1090)：

| 字段 | 作用 |
| --- | --- |
| `disable` | 在具体这份配置上关掉这个插件 |
| `priority` | 覆盖插件默认优先级，只影响本次配置 |
| `filter` | 决定运行时是否执行 |
| `error_response` | 自定义错误响应内容，避免泄露真实错误 |
| `pre_function` | 每个阶段执行前先跑的函数 |

它在哪里被用上？两处：

- `disable`：[plugin.lua:94–107](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/plugin.lua#L94-L107) 的 `check_disable`，在筛选插件时直接跳过。
- `priority`：[512–545 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/plugin.lua#L512-L545) 发现有人用了 `_meta.priority` 就置 `custom_sort = true`，改用 `custom_sort_plugin` 重新排序。

**所以 `_meta` 是"每份配置的调节开关"，和插件的 `priority`（全局默认值）是两层。**

## 4. 有哪些阶段可以实现

官方自带的 [apisix/plugins/example-plugin.lua](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/plugins/example-plugin.lua) 就是一个完整样板：
它把 `init`、`destroy`、`rewrite`、`access`、`header_filter`、`body_filter`、`delayed_body_filter`、`log` 全部实现了一遍，还演示了 `control_api`。

需要记住的边界（第 5 章讲过原因）：

- `log` 阶段响应已经发出，**不能改响应**。
- 要改响应头或响应体，必须在 `header_filter` / `body_filter` 或更早。

## 5. 我们的插件：lance-request-id

完整源码在
[`labs/01-gateway/plugins/apisix/plugins/lance-request-id.lua`](../../labs/01-gateway/plugins/apisix/plugins/lance-request-id.lua)。
它刻意只做两件小事，用来演示两个阶段：

```lua
local _M = {
    version = 0.1,
    priority = 1200,
    name = plugin_name,
    schema = schema,
}
```

`priority = 1200` 是有意选的：比 `key-auth`（2500）低、比 `proxy-rewrite`（1008）高。
在同一个 `rewrite` 阶段里，它会在认证之后、路径改写之前执行。
**这正好复习第 3 章的结论：同阶段看 priority，跨阶段看阶段顺序。**

```lua
function _M.rewrite(conf, ctx)
    local request_id = conf.prefix .. ngx.now() .. "-" .. math.random(100000, 999999)

    -- 存进请求上下文，供后续阶段（这里是 header_filter）使用。
    ctx.lance_request_id = request_id

    -- 写入转发给上游的请求头。
    core.request.set_header(ctx, conf.upstream_header, request_id)
end


function _M.header_filter(conf, ctx)
    -- 响应头必须在这个阶段（或更早）设置；到 log 阶段就晚了。
    core.response.set_header(conf.header, ctx.lance_request_id or "unset")
end
```

三个值得注意的设计点：

- **用 `ctx` 在阶段之间传值**：`rewrite` 生成，`header_filter` 消费。
  这和第 3 章 `attach_consumer` 把 Consumer 写进 `ctx` 是同一个套路。
- **`core.request.set_header` / `core.response.set_header` 而不是直接碰 `ngx`**：与内置插件保持一致。
- `check_schema` 只做一件事：`core.schema.check(schema, conf)`。
  路由里的 `{}` 会因此拿到 schema 中定义的默认值。

## 6. 怎么让 APISIX 认识它

两步，缺一不可。

**第一步：让 `require("apisix.plugins.lance-request-id")` 能找到文件。**
`extra_lua_path` 会被拼进 Nginx 的 `lua_package_path`
（[config.lua:30](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/cli/config.lua#L30) 定义，
[ngx_tpl.lua:84](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/cli/ngx_tpl.lua#L84) 使用）：

```yaml
apisix:
  extra_lua_path: "/opt/apisix-plugins/?.lua"
```

配合目录结构 `plugins/apisix/plugins/lance-request-id.lua`，
模块名就被解析为 `apisix.plugins.lance-request-id`。

**第二步：把插件名写进 `plugins` 列表**，否则它根本不会被加载：

```yaml
plugins:
  - key-auth
  - limit-count
  - proxy-rewrite
  - prometheus
  - lance-request-id
```

容器里的挂载（本仓库 `compose.yaml`）：

```yaml
    volumes:
      - ./apisix/config.yaml:/usr/local/apisix/conf/config.yaml:ro
      - ./plugins:/opt/apisix-plugins:ro
```

## 7. 实测结果

**插件确实被加载**，启动日志的 HTTP 插件列表是：

```text
[lua] plugin.lua:219: load(): new plugins:
      {"key-auth":true,"prometheus":true,"proxy-rewrite":true,"limit-count":true,"lance-request-id":true}
```

**请求头与响应头都生效**（真实 curl 输出）：

```text
HTTP/1.1 200 OK
X-Lance-Request-Id: lance-1791975207.577-717072
```

**和另一个插件共存正常**：后端收到的路径仍是 `/orders/123`，
说明 `proxy-rewrite`（1008）在我们的插件（1200）之后也执行了——两个 `rewrite` 插件按 priority 依次运行。

**`_meta` 也实测了**（同一路由，只改 `_meta`）：

| 配置 | HTTP | 自定义响应头 |
| --- | --- | --- |
| `lance-request-id: {}` | 200 | **存在** |
| `{ "_meta": { "disable": true } }` | 200 | **不存在** |
| `{ "_meta": { "priority": 3000 } }` | 200 | 存在 |

`disable = true` 时请求仍然成功，但插件被跳过——**这正是 `check_disable` 的作用**。
`priority` 覆盖没有报错，字段可用（本次没有构造出能观测到排序变化的场景，所以只验证了"不报错"）。

`scripts/lab.py smoke` 也把自定义插件纳入了验证，共 14 项检查全部通过，其中两项是：

```text
PASS custom plugin route: HTTP 200
PASS custom plugin: rewrite and header_filter both applied
```

## 8. 实测中踩到的坑（这部分比源码更有用）

### 坑一：显式声明 `plugins` 会关掉所有没列出的插件

原来是靠默认列表加载全部插件。一旦自己写了 `plugins:`，
**没列出的插件就不存在了**，引用它的路由会被拒绝。

### 坑二：移除 `prometheus` 会连带破坏 stream 插件加载

我第一版 `plugins` 列表里没有 `prometheus`，结果启动日志出现：

```text
failed to load plugin [syslog]
err: prometheus/exporter.lua:54: lua_shared_dict "prometheus-cache" not configured
```

原因是两个机制的组合：

- `prometheus-cache` 共享字典**按条件创建**
  （[ngx_tpl.lua:70–72](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/cli/ngx_tpl.lua#L70-L72)）：

  ```nginx
      {% if enabled_plugins["prometheus"] or enabled_stream_plugins["prometheus"] then %}
      lua_shared_dict prometheus-cache {* meta.lua_shared_dict["prometheus-cache"] *};
      {% end %}
  ```

- 而 **stream 插件列表是独立的一份**，不受 `plugins` 控制
  （[plugin.lua:269](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/plugin.lua#L269) 的 `load_stream`）：

  ```text
  load_stream(): new plugins: {"mqtt-proxy":true,"syslog":true,"limit-conn":true,"traffic-split":true,"ip-restriction":true}
  ```

stream 里的 `syslog` 加载时依赖 prometheus 的 exporter，共享字典不存在就报错。

**把 `prometheus` 加回列表后：`prometheus-cache` 出现 1 次，`failed to load plugin` 出现 0 次，配置自检通过。**

这个坑的教训是：**`plugins` 列表不是"我允许路由用哪些插件"这么简单**，
它还会影响共享字典等基础设施的创建条件。

### 坑三：改挂载的配置文件后，容器不会自动重建

`config.yaml` 是挂载进去的，改文件不会触发容器重建，**Nginx 配置仍是旧的**。
必须重启或强制重建容器才会重新生成 `nginx.conf`。
我第一次验证时就被这个骗到过：`grep prometheus-cache` 返回 0，以为配置没生效。

## 9. 本章没有验证什么

- **没有验证 `_meta.filter` 和 `_meta.pre_function`**，只从 schema 得知字段存在。
- **没有验证 `_meta.priority` 真的改变了执行顺序**，只确认不报错。
- **没有验证 `metadata_schema`（插件级元数据）** 与 `control_api`。
- **没有做性能测试**，也没有评估插件在每请求路径上的开销。
- 插件是**面向学习的最小实现**，没有错误处理、没有幂等考虑、不适合生产。

## 10. 先读这五处

| 顺序 | 文件与定位 | 要回答的问题 |
| --- | --- | --- |
| 1 | `apisix/plugin.lua`：132、152、163 | 加载插件有哪些硬性条件？ |
| 2 | `apisix/plugin.lua`：175 | `_meta` 为什么不能自己写在 schema 里？ |
| 3 | `apisix/schema_def.lua`：1058 | `_meta` 有哪些字段，各自什么用？ |
| 4 | `apisix/plugins/example-plugin.lua`：45、76、110 | 官方样板如何组织各阶段？ |
| 5 | `apisix/cli/config.lua`：30 + `ngx_tpl.lua`：84 | 自定义插件靠什么被找到？ |
| 6 | 本仓库 `plugins/apisix/plugins/lance-request-id.lua` | 最小可用插件长什么样？ |

## 11. 小练习与后续节奏

1. 插件只写 `name` 和 `schema`、忘了 `priority`，会发生什么？为什么不容易被发现？
2. 为什么 `_meta` 是"每份配置的开关"，而不是插件自己的属性？
3. 想在 `log` 阶段给响应加一个头，为什么做不到？应该改到哪个阶段？
4. 为什么从 `plugins` 列表里删掉一个插件，会影响另一个看似无关的插件？

本章真的写了并跑通了一个插件，同时实测了 `_meta.disable` 与共享字典的连带影响。
仓库代码有改动（新增插件与路由、更新配置与脚本），已在同一提交中说明。
未做性能测试，未验证 `_meta.filter` / `pre_function` / `metadata_schema`。

**这是三个主题中的第二个。** 继续时讲：

- 第三个主题：多网关高可用——控制面/数据面分离下的配置分发。

这是一项待学习主题，不是已完成章节。

# 源码第 5 章：可观测性——怎么定位一次线上故障

[上一章：选节点、健康检查与重试](04-load-balancing-and-retries.md) · [返回学习目录](../../README.md)

## 1. 为什么把可观测性放在这一章

前四章里最有说服力的证据，全部来自日志：

- 第 1 章确认请求确实没到后端，靠的是后端访问日志。
- 第 3 章证明 401 未触达后端，靠的是对比后端日志条数。
- 第 4 章证明重试真的发生了，靠的是 APISIX 访问日志里的 `502, 200`。

所以这一章不再是"顺便看看日志"，而是把**观测手段本身**讲清楚：
它们各自能回答什么问题，以及**什么时候会骗你**。

阅读版本固定到提交 `2f8ebf8527cdc878afe30100077c471879a5c133`（tag 3.14.1）。
说明文字是学习笔记；标记为“原文摘录”的代码来自 Apache APISIX。
上游许可证与署名见 [APISIX-LICENSE.txt](APISIX-LICENSE.txt)、[APISIX-NOTICE.txt](APISIX-NOTICE.txt)。

## 2. 三种手段，回答三种问题

| 手段 | 粒度 | 回答什么 | 典型缺陷 |
| --- | --- | --- | --- |
| 访问日志 | 每个请求一行 | 这次请求发生了什么？ | 量大、字段多，只看状态码会漏 |
| 错误日志 | 发生异常时 | 哪一步报错了？ | 不表示每个请求的结果 |
| 指标 | 聚合值 | 整体趋势是否异常？ | 掩盖单次请求的细节 |

**它们不是三个可以互相替代的选项，而是三层递进：指标发现异常，日志定位原因，错误日志给出根因。**

## 3. 访问日志的默认格式

[apisix/cli/config.lua:134–141](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/cli/config.lua#L134-L141)：

```lua
    http = {
      enable_access_log = true,
      access_log = "logs/access.log",
      access_log_buffer = 16384,
      -- luacheck: push max code line length 300
      access_log_format =
      '$remote_addr - $remote_user [$time_local] $http_host "$request" $status $body_bytes_sent $request_time "$http_referer" "$http_user_agent" $upstream_addr $upstream_status $upstream_response_time "$upstream_scheme://$upstream_host$upstream_uri"',
```

这个格式在模板里被渲染成 Nginx 的 `log_format`
（[ngx_tpl.lua:415](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/cli/ngx_tpl.lua#L415)）：

```nginx
    log_format main escape=... '{* http.access_log_format *}';
```

而写入日志的时机由 `log_by_lua_block` 触发
（[ngx_tpl.lua:881–883](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/cli/ngx_tpl.lua#L881-L883)）。
**注意：这仍然是 Nginx 自己的 access_log 机制，不是 APISIX 重新发明了一套。**

## 4. 用上一章那条日志逐字段验证

第 4 章出现过这样一行（为了读起来清楚，做了换行）：

```text
172.19.0.1 - - [14/Oct/2026:07:39:15 +0000] 127.0.0.1:9080 "GET /orders/123 HTTP/1.1"
200 85 3.096 "-" "curl/8.7.1" 172.19.0.5:8080, 172.19.0.4:8080 502, 200 3.095, 0.001 "http://127.0.0.1:9080"
```

按默认格式对齐，每个字段的含义是：

| 字段 | 值 | 说明 |
| --- | --- | --- |
| `$remote_addr` | `172.19.0.1` | 客户端地址（这里是 Docker 网桥） |
| `$http_host` | `127.0.0.1:9080` | 请求的 Host |
| `$request` | `GET /orders/123 HTTP/1.1` | 请求行 |
| `$status` | `200` | **客户端最终看到的状态** |
| `$body_bytes_sent` | `85` | 响应体字节数 |
| `$request_time` | `3.096` | 整个请求耗时（秒） |
| `$upstream_addr` | `172.19.0.5:8080, 172.19.0.4:8080` | **本次请求访问过的所有后端** |
| `$upstream_status` | `502, 200` | **每个后端的返回状态** |
| `$upstream_response_time` | `3.095, 0.001` | 每个后端的耗时 |
| `$upstream_scheme://$upstream_host$upstream_uri` | `http://127.0.0.1:9080` | 实际转发目标 |

**这行日志的重点不是 200，而是它右边那些字段。**

`$upstream_addr` 有两个地址、`$upstream_status` 是 `502, 200`——
一次请求访问了两个后端、第一个失败第二个成功。这正是第 4 章"重试在兜底"的证据来源。

## 5. 状态码会骗你，延迟不会

同一批日志里，把故障期间和正常时期放在一起对比：

| 时期 | `$status` | `$upstream_addr` | `$upstream_status` | `$request_time` |
| --- | --- | --- | --- | --- |
| 故障期间（首次） | `200` | 两个地址 | `502, 200` | **`39.000`** |
| 故障期间（后续） | `200` | 两个地址 | `502, 200` | **`3.096`～`3.112`** |
| 正常时期 | `200` | 单个地址 | `200` | **`0.001`～`0.007`** |

**`$status` 全是 200，完全看不出问题。**

但延迟放大了三个数量级：从毫秒级涨到 **3.1 秒**，首次甚至到 **39 秒**。
客户端确实拿到了正确结果，只是**慢了上千倍**。

这就是"状态码会骗你，延迟不会"。
如果监控只统计 `$status` 的 2xx/5xx 比例，这次故障会**完全隐形**。

顺带一个实操结论：我们的实验没有配置上游 `timeout`，所以这次延迟由 Nginx 默认超时行为决定。
**生产环境应显式配置上游超时和 `retry_timeout`，否则故障时的延迟放大不受控。**
（本章只观测到该现象，没有深入内核或 Docker 网络层面去定位这 39 秒/3.1 秒的具体来源。）

## 6. log 阶段到底做了什么

日志能写出来，是因为请求走完了 `http_log_phase`。
[apisix/init.lua:1029–1059](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/init.lua#L1029-L1059)：

```lua
function _M.http_log_phase()
    local api_ctx = ngx.ctx.api_ctx
    if not api_ctx then
        return
    end

    if not api_ctx.var.apisix_upstream_response_time or
    api_ctx.var.apisix_upstream_response_time == "" then
        api_ctx.var.apisix_upstream_response_time = ngx.var.upstream_response_time
    end
    local api_ctx = common_phase("log")
```

这里做四件事，每一件都有实际意义：

1. **补齐上游响应时间变量**，供日志格式和插件读取。
2. **执行插件的 `log` 阶段**——指标、日志类插件都挂在这里（下面第 7 节）。
3. **执行被动健康检查**：`healthcheck_passive(api_ctx)`，根据本次请求结果上报成功或失败。
   但它是**有前提**的
   （[init.lua:912–922](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/init.lua#L912-L922)）：

   ```lua
   local function healthcheck_passive(api_ctx)
       local checker = api_ctx.up_checker
       if not checker then
           return
       end
   ...
       local passive = up_conf.checks and up_conf.checks.passive
       if not passive then
           return
       end
   ```

   没有 checker、或没配 `checks.passive`，它就直接返回。
   **我们的实验两者都没有，所以这里其实是空操作**——这和第 4 章的结论是一致的。

4. **回收资源**，回到 table pool：

```lua
    if api_ctx.server_picker and api_ctx.server_picker.after_balance then
        api_ctx.server_picker.after_balance(api_ctx, false)
    end

    core.ctx.release_vars(api_ctx)
    if api_ctx.plugins then
        core.tablepool.release("plugins", api_ctx.plugins)
    end
    ...
    core.tablepool.release("api_ctx", api_ctx)
```

**第 4 点带来一个容易踩的坑**：log 阶段结束后，`api_ctx` 会被归还并**复用**。

- 不要写"把 `api_ctx` 存到某个全局变量里，之后再读"的插件。
- 需要长期保存的信息，必须在 log 阶段结束前拷成自己的数据。

另外要区分：log 阶段是**请求结束后**才执行，此时响应已经发出，**不能再用它修改响应**。
修改响应要放在 `header_filter` 或 `body_filter`。

## 7. 指标：采集在 log 阶段

指标插件本身很薄
（[apisix/plugins/prometheus.lua:32–40](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/plugins/prometheus.lua#L32-L40)）：

```lua
local _M = {
    version = 0.2,
    priority = 500,
    name = plugin_name,
    log  = exporter.http_log,
    destroy = exporter.destroy,
    schema = schema,
    run_policy = "prefer_route",
}
```

三个要点：

- `priority = 500`，且**它实现的是 `log` 方法**，所以采集发生在第 6 节的 log 阶段。
- 真正的采集与导出逻辑在 `apisix.plugins.prometheus.exporter` 里。
- `run_policy = "prefer_route"` 表示可以在路由级启用，按路由维度区分。

指标端点由 exporter 注册
（[53–62 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/plugins/prometheus.lua#L53-L62)）：

```lua
function _M.api()
    return exporter.get_api(true)
end

function _M.init()
    local local_conf = core.config.local_conf()
    local enabled_in_stream = core.table.array_find(local_conf.stream_plugins, "prometheus")
    exporter.http_init(enabled_in_stream)
end
```

默认导出路径和地址在配置示例里可以确认
（[conf/config.yaml.example:621–622](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/conf/config.yaml.example#L621-L622)）：

```yaml
  prometheus:                               # Plugin: prometheus
    export_uri: /apisix/prometheus/metrics  # Set the URI for the Prometheus metrics endpoint.
```

**这一节是本章唯一没有实测的部分，原因见下。**

## 8. 本实验验证了什么，没验证什么

在同一台实验环境上实测：

| 项目 | 结果 |
| --- | --- |
| 访问日志格式与字段 | 与源码默认格式逐字段吻合（第 4 节表格） |
| 故障期间延迟放大 | 从毫秒级到 3.1 秒，首次 39 秒（第 5 节表格） |
| `GET http://127.0.0.1:9080/apisix/prometheus/metrics` | **HTTP 404** |
| `GET http://127.0.0.1:9180/apisix/prometheus/metrics` | **HTTP 404** |
| `http://127.0.0.1:9091/apisix/prometheus/metrics` | **不可达** |

**我们的实验没有启用指标导出**，所以指标数量和内容**没有验证过**。
这是一个诚实的边界，不是"指标不重要"。

想在本地试指标，需要按官方文档配置 `plugin_attr.prometheus` 的导出地址或导出端口，
并让 Prometheus 能访问该端点。这些步骤本章没有执行。

## 9. 一次故障排查的实操顺序

把四章的内容串成可执行的动作：

```text
1. 监控告警：先看聚合指标或错误率，发现"有问题"
2. 顺序反了？先确认客户端状态码分布 → 全是 2xx 时不能收工
3. 看 $upstream_status：是否出现非 2xx？几次？
4. 看 $upstream_addr：是否出现多个地址？说明发生了重试
5. 看 $request_time / $upstream_response_time：延迟是否被放大？
6. 看错误日志：连接被拒绝？超时？DNS 失败？
7. 回到源码：重试次数、健康检查是否配置（第 4 章）
```

第 2 步是本章最想强调的一点：
**"客户端 200"从来不是排查终点。**

## 10. 先读这五处

| 顺序 | 文件与定位 | 要回答的问题 |
| --- | --- | --- |
| 1 | `apisix/cli/config.lua`：134 | 默认访问日志格式有哪些字段？ |
| 2 | `apisix/cli/ngx_tpl.lua`：415、881 | 格式如何渲染，日志在哪个钩子写出？ |
| 3 | `apisix/init.lua`：1029、1044、1050 | log 阶段做了哪几件事？ |
| 4 | `apisix/plugins/prometheus.lua`：32、58 | 指标在哪个阶段采集，端点从哪来？ |
| 5 | `conf/config.yaml.example`：230、621 | 可配置项分别在什么位置？ |

## 11. 小练习与后续节奏

1. `$status` 是 200 而 `$upstream_status` 是 `502, 200`，客户端的这次请求算成功还是失败？
2. 为什么在 log 阶段插件不能修改响应内容？
3. 如果把 `api_ctx` 缓存到插件模块级的变量里，可能会出现什么问题？
4. 监控只看状态码时，第 5 节那次故障会以什么形式暴露（或者完全不暴露）？

本章做了一次可观测性专项实测（访问日志字段对齐、故障延迟对比、指标端点探测）。
未修改仓库代码，未修改上游源码，未启用指标导出。

到这里源码系列已有五章，覆盖：请求生命周期、配置热更新、认证插件、负载均衡与容错、可观测性。
后续按「继续」推进的方向（待定，不是已完成章节）：

- 上游 TLS 与 mTLS：证书如何加载与校验。
- 插件开发：写一个插件要满足哪些约束，`_meta` 怎么用。
- 多网关高可用：控制面/数据面分离部署下的配置分发。

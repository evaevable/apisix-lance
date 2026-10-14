# 源码第 4 章：选节点、健康检查与重试

[上一章：key-auth 如何识别调用方](03-key-auth-and-plugin-phases.md) · [返回学习目录](../../README.md)

## 1. 本章要回答的问题

上一章结尾的问题是：认证失败时 `proxy-rewrite` 会不会执行。
答案是不执行——`key-auth`（priority 2500）先于 `proxy-rewrite`（priority 1008）在同一个 `rewrite` 阶段运行，认证返回 401 后请求就结束了。

本章追另一个日常问题：

> Upstream 配了两个实例，其中一个挂了，请求还能成功吗？是谁在兜底？

阅读版本固定到提交 `2f8ebf8527cdc878afe30100077c471879a5c133`（tag 3.14.1）。
说明文字是学习笔记；标记为“原文摘录”的代码来自 Apache APISIX。
上游许可证与署名见 [APISIX-LICENSE.txt](APISIX-LICENSE.txt)、[APISIX-NOTICE.txt](APISIX-NOTICE.txt)。

## 2. 先记住：这是两件不同的事

新手最容易把这两个机制混成一件：

| 机制 | 回答的问题 |
| --- | --- |
| 健康检查 | **哪些节点有资格被选中？** |
| 重试 | **选中之后失败了，要不要再试一个？** |

它们的配置项不同、生效时机不同，兜底效果也完全不同。
本章会看到：**我们的实验环境没有配健康检查，却能靠重试扛住一个实例挂掉。**

## 3. 节点从哪来，picker 怎么建

第 1 章讲过：`pick_server` 在 access 阶段被调用一次，重试时再次调用。
[balancer.lua:192–195](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/balancer.lua#L192-L195) 的注释把这件事写得很直白：

```lua
-- pick_server will be called:
-- 1. in the access phase so that we can set headers according to the picked server
-- 2. each time we need to retry upstream
```

真正选择节点的是 **server picker**，按 `upstream.type` 加载对应模块
（[99–132 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/balancer.lua#L99-L132)）：

```lua
local function create_server_picker(upstream, checker)
    local picker = pickers[upstream.type]
    if not picker then
        pickers[upstream.type] = require("apisix.balancer." .. upstream.type)
```

我们的实验用的是 `"type": "roundrobin"`，所以加载 `apisix/balancer/roundrobin.lua`。

节点会先按 `priority` 分组（[49–61 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/balancer.lua#L49-L61)）：

```lua
    if not new_nodes[node.priority] then
        new_nodes[node.priority] = {}
        core.table.insert(new_nodes._priority_index, node.priority)
    end
```

如果出现多个优先级层次，[118–123 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/balancer.lua#L118-L123) 会用 `priority_balancer` 包一层，实现「主节点优先、主节点不可用才用备用节点」。
我们的实验所有节点权重都是 1、优先级相同，走的是单层分支。

## 4. 哪些节点有资格被选中

[64–95 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/balancer.lua#L64-L95) 的 `fetch_health_nodes` 决定候选集合。

**没有 checker 时，所有节点都进候选：**

```lua
    if not checker then
        local new_nodes = core.table.new(0, #nodes)
        for _, node in ipairs(nodes) do
            new_nodes = transform_node(new_nodes, node)
        end
        return new_nodes
    end
```

**有 checker 时才按健康状态过滤**，而且这里有一个反直觉的兜底：

```lua
    if core.table.nkeys(up_nodes) == 0 then
        core.log.warn("all upstream nodes is unhealthy, use default")
        for _, node in ipairs(nodes) do
            up_nodes = transform_node(up_nodes, node)
        end
    end
```

**全部节点都不健康时，APISIX 反而把全部节点放回候选。**
这是「失败开放」而不是「失败关闭」：宁可尝试可能失败的节点，也不让服务彻底不可用。
理解这一点很重要——**健康检查不是硬熔断**。

那什么时候才有 checker？看
[apisix/healthcheck_manager.lua:52–55](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/healthcheck_manager.lua#L52-L55)：

```lua
local function create_checker(up_conf)
    if not up_conf.checks then
        return nil
    end
```

**没配 `checks`，就不会创建 checker。**
我们的实验路由里确实没有 `checks`，所以 checker 为空。

再看 [115–121 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/healthcheck_manager.lua#L115-L121)：

```lua
function _M.fetch_node_status(checker, ip, port, hostname)
    -- check if the checker is valid
    if not checker or checker.dead then
        return true
    end
```

**没有 checker 时，每个节点都被当成健康的。**
所以结论是：我们的实验里，挂掉的节点仍然留在轮询池里，照样会被选中。

## 5. roundrobin 怎么挑，怎么避开刚失败的节点

[apisix/balancer/roundrobin.lua:27–59](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/balancer/roundrobin.lua#L27-L59)：

```lua
    local picker = roundrobin:new(up_nodes)
    local nodes_count = nkeys(up_nodes)
    return {
        upstream = upstream,
        get = function (ctx)
            if ctx.balancer_tried_servers and ctx.balancer_tried_servers_count == nodes_count then
                return nil, "all upstream servers tried"
            end
```

两个关键点：

- `roundrobin:new(up_nodes)` 把**权重**交给底层轮询实现，权重为 1 的节点被均匀轮询。
- `ctx.balancer_tried_servers` 记录本次请求已经试过的节点，`get` 会跳过它们，避免重试时又打回同一个坏节点。

记录动作在 [60–76 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/balancer/roundrobin.lua#L60-L76)：

```lua
        after_balance = function (ctx, before_retry)
            if not before_retry then
                ...
                return nil
            end

            if not ctx.balancer_tried_servers then
                ctx.balancer_tried_servers = core.tablepool.fetch("balancer_tried_servers", 0, 2)
            end

            ctx.balancer_tried_servers[ctx.balancer_server] = true
```

而调用它的正是重试分支
（[balancer.lua:217–221](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/balancer.lua#L217-L221)）：

```lua
    ctx.balancer_try_count = (ctx.balancer_try_count or 0) + 1
    if ctx.balancer_try_count > 1 then
        if ctx.server_picker and ctx.server_picker.after_balance then
            ctx.server_picker.after_balance(ctx, true)
        end
```

还有一个省事的优化（[243–248 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/balancer.lua#L243-L248)）：

```lua
    -- the same picker will be used in the whole request, especially during the retry
    local server_picker = ctx.server_picker
    if not server_picker then
        server_picker = lrucache_server_picker(key, version,
                                               create_server_picker, up_conf, checker)
    end
```

同一个请求内复用 picker，缓存键里带了 upstream 版本和健康检查 status 版本。
所以锁定的节点列表在一次请求的重试过程中是一致的。

## 6. 默认重试几次？答案可能出乎意料

[143–178 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/balancer.lua#L143-L178) 的 `set_balancer_opts`：

```lua
    local retries = up_conf.retries
    if not retries or retries < 0 then
        retries = #up_conf.nodes - 1
    end

    if retries > 0 then
        if up_conf.retry_timeout and up_conf.retry_timeout > 0 then
            ctx.proxy_retry_deadline = ngx_now() + up_conf.retry_timeout
        end
        local ok, err = set_more_tries(retries)
```

**没配置 `retries` 时，默认值不是 0，而是「节点数 − 1」。**

而 `retries` 的 schema 本身没有默认值
（[schema_def.lua:401–404](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/schema_def.lua#L401-L404)）：

```lua
        retries = {
            type = "integer",
            minimum = 0,
        },
```

所以对我们的两节点订单上游，**默认就有 1 次重试**。

由此得到两个实用结论：

- 两节点上游什么都不配，也能靠默认重试扛住一个节点故障。
- **单节点上游默认 `retries = 0`**，节点挂了就是挂了，没有重试余地。

## 7. 本机实测：停掉一个实例会怎样

实验环境本来就有两个订单实例。做法是记录基线、临时停掉 `orders-b`、持续发请求、再恢复。

| 阶段 | 6 次请求的状态码 | 实例分布 |
| --- | --- | --- |
| 基线（两实例都在） | `200 200 200 200 200 200` | `orders-b orders-a orders-a orders-a orders-b orders-b` |
| **停掉 `orders-b`** | `200 200 200 200 200 200` | **全部 `orders-a`** |
| 恢复后 | `200 200 200 200 200 200` | 恢复正常 |

**一个实例挂掉，6 次请求全部成功，没有出现 502。**

但只看客户端结果是看不出原因的：可能是健康检查把它摘掉了，也可能是重试兜住了。
于是去查 APISIX 访问日志，找到了直接证据：

```text
[error] connect() failed (113: No route to host) while connecting to upstream,
        upstream: "http://172.19.0.5:8080/orders/123"
"GET /orders/123 HTTP/1.1" 200 ... 172.19.0.5:8080, 172.19.0.4:8080 502, 200 ...
```

把这段日志拆开读：

- `upstream_addr` 是 `172.19.0.5:8080, 172.19.0.4:8080`——**同一个请求访问了两个后端**。
- `upstream_status` 是 `502, 200`——**第一个返回 502，第二个返回 200**。
- `connect() failed (113: No route to host)`——第一次连接就是失败的。

结论非常明确：

1. 挂掉的节点**没有**被健康检查排除，轮询仍然选中了它。
2. 第一次尝试失败，触发重试，换到另一个节点。
3. **最终客户端看到的是 200。**

这正是第 6 节源码推理的实测印证：
**在没有配健康检查的情况下，是重试在兜底，不是健康检查在兜底。**

实验已恢复原状（`orders-b` 重新健康）。本次只临时停止并重启了自己实验室的容器，未修改仓库代码。

## 8. 三个容易踩的坑

**坑一：把「客户端 200」当成「后端都健康」。**
日志里的 `502, 200` 说明失败确实发生了，只是被重试掩盖。监控只看客户端状态码会漏报。

**坑二：以为健康检查一定会摘掉坏节点。**
没配 `checks` 就没有 checker；而且即使有 checker，全部不健康时也会回退到「用全部节点」。

**坑三：以为重试可以无限兜底。**
重试次数受 `retries` 限制，默认等于节点数减一；配了 `retry_timeout` 还会加总时限。
节点数越多，重试预算越大，但**写请求重试可能造成重复提交**，需要业务幂等配合。

## 9. 先读这五处

| 顺序 | 文件与定位 | 要回答的问题 |
| --- | --- | --- |
| 1 | `apisix/balancer.lua`：99、49、118 | picker 怎么按类型加载，节点怎么按优先级分组？ |
| 2 | `apisix/balancer.lua`：64、88 | 谁决定候选节点？全不健康时会怎样？ |
| 3 | `apisix/balancer/roundrobin.lua`：27、39、60 | 怎么轮询，怎么跳过已试过的节点？ |
| 4 | `apisix/balancer.lua`：143、163 | 默认重试几次，超时怎么算？ |
| 5 | `apisix/healthcheck_manager.lua`：52、115 | 什么时候有 checker，没有时节点算什么状态？ |

## 10. 小练习与后续节奏

1. 单节点上游不配 `retries`，节点故障时客户端会看到什么？
2. 为什么「全部节点不健康就回退到全部节点」比直接返回 502 更合理？在什么场景下反而危险？
3. 访问日志里只有 `200`，能证明后端没有发生过失败吗？
4. 为什么重试对幂等的查询接口更安全，而对创建订单要谨慎？

本章做了静态源码阅读，并用自己实验室的容器补充了一项可观测验证（临时停止并已恢复一个后端实例）。
未修改上游代码，未执行源码级单元测试，也未验证配置了 `checks` 时的健康检查行为。

到这里，请求生命周期、配置热更新、认证插件、负载均衡与容错四条主线已经走完。
后续按「继续」推进的方向（待定，不是已完成章节）：

- 可观测性：访问日志字段、指标与链路追踪。
- 上游 TLS 与 mTLS。
- 插件开发：自己写一个插件要满足哪些约束。

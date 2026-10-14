# 源码第 3 章：key-auth 如何识别调用方

[上一章：配置热更新是怎么实现的](02-config-hot-reload.md) · [返回学习目录](../../README.md)

## 1. 本章要回答的问题

上一章结尾的问题是：绕过 Admin API 直接往 etcd 写不合格数据会怎样。
答案在源码里已经清楚：数据面读书时也校验，不合格的条目会被逐条跳过，**旧配置继续生效**。

本章换一个角度，追一个更贴近日常的场景：

```http
GET /protected/orders/123
```

它为什么有时返回 401、有时返回 200？以及：**401 的时候，后端到底有没有被打到？**

阅读版本固定到提交 `2f8ebf8527cdc878afe30100077c471879a5c133`（tag 3.14.1），与实验版本一致。
说明文字是学习笔记；标记为“原文摘录”的代码来自 Apache APISIX。
上游许可证与署名见 [APISIX-LICENSE.txt](APISIX-LICENSE.txt)、[APISIX-NOTICE.txt](APISIX-NOTICE.txt)。

## 2. 先分清两处配置

这是最容易混淆的一点：**插件开关在路由上，凭证在 Consumer 上。**

`labs/01-gateway/routes/protected-orders.json` 里插件配置是空的：

```json
  "plugins": {
    "key-auth": {},
    "proxy-rewrite": {
      "regex_uri": ["^/protected/(.*)$", "/$1"]
    }
  },
```

`{}` 的意思是「这条路由启用 key-auth，使用默认参数」。
真正被校验的 API Key 并不在这里，而在 Consumer 上。
所以 `scripts/lab.py` 必须**先创建 Consumer，再提交路由**。

## 3. 插件元信息说明了很多事

[apisix/plugins/key-auth.lua:51–58](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/plugins/key-auth.lua#L51-L58)：

```lua
local _M = {
    version = 0.1,
    priority = 2500,
    type = 'auth',
    name = plugin_name,
    schema = schema,
    consumer_schema = consumer_schema,
}
```

三个字段值得注意：

- `priority = 2500`：在同一阶段内决定执行顺序。
- `type = 'auth'`：标明这是认证类插件。
- 同时有 `schema` 和 `consumer_schema`：**路由级参数**和**Consumer 级凭证**用两套不同的校验规则。

路由级参数 [22–39 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/plugins/key-auth.lua#L22-L39)：

```lua
        header = {
            type = "string",
            default = "apikey",
        },
        query = {
            type = "string",
            default = "apikey",
        },
        hide_credentials = {
            type = "boolean",
            default = false,
        },
```

默认既接受名为 `apikey` 的请求头，也接受同名查询参数。
这两个默认值，正是我们的实验里 `-H 'apikey: ...'` 和 `?apikey=...` 都能通过的原因。

Consumer 级凭证 [41–48 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/plugins/key-auth.lua#L41-L48)：

```lua
local consumer_schema = {
    type = "object",
    properties = {
        key = { type = "string" },
    },
    encrypt_fields = {"key"},
    required = {"key"},
}
```

`encrypt_fields` 表示这个字段按加密字段处理，属于密钥管理机制的一部分。
这解释了为什么配置里的 Key 不应该被当成普通明文对待。

## 4. 它运行在哪个阶段？

[103 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/plugins/key-auth.lua#L103) 的实现函数是：

```lua
function _M.rewrite(conf, ctx)
```

**它实现的是 `rewrite` 阶段。**

结合第 1 章的结论：插件 `rewrite` 回调由 `http_access_phase()` 调用，而不是 Nginx 的 rewrite 钩子。
所以「认证插件在 access 之前」这种说法要小心：这里的分界是 APISIX 的插件阶段，不是 Nginx 的钩子名字。

## 5. 找凭证的顺序

[69–87 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/plugins/key-auth.lua#L69-L87)：

```lua
    local from_header = true
    local key = core.request.header(ctx, conf.header)

    if not key then
        local uri_args = core.request.get_uri_args(ctx) or {}
        key = uri_args[conf.query]
        from_header = false
    end

    if not key then
        return nil, nil, "Missing API key in request"
    end
```

顺序是：**先查请求头，没有才查查询参数。**

`from_header` 记下来源，因为后面 `hide_credentials` 要按来源决定删哪一个：

```lua
    if conf.hide_credentials then
        if from_header then
            core.request.set_header(ctx, conf.header, nil)
        else
            local args = core.request.get_uri_args(ctx)
            args[conf.query] = nil
            core.request.set_uri_args(ctx, args)
        end
    end
```

继续往下，[83–87 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/plugins/key-auth.lua#L83-L87) 才真正查调用方：

```lua
    local consumer, consumer_conf, err = consumer_mod.find_consumer(plugin_name, "key", key)
    if not consumer then
        core.log.warn("failed to find consumer: ", err or "invalid api key")
        return nil, nil, "Invalid API key in request"
    end
```

于是有了两种不同含义的失败：

| 情况 | 错误信息 |
| --- | --- |
| 完全没带凭证 | `Missing API key in request` |
| 带了但匹配不到 | `Invalid API key in request` |

## 6. 401 是怎么产生的

[103–118 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/plugins/key-auth.lua#L103-L118)：

```lua
function _M.rewrite(conf, ctx)
    local consumer, consumer_conf, err = find_consumer(ctx, conf)
    if not consumer then
        if not conf.anonymous_consumer then
            return 401, { message = err}
        end
        consumer, consumer_conf, err = consumer_mod.get_anonymous_consumer(conf.anonymous_consumer)
        if not consumer then
            err = "key-auth failed to authenticate the request, code: 401. error: " .. err
            core.log.error(err)
            return 401, { message = "Invalid user authorization"}
        end
    end
    consumer_mod.attach_consumer(ctx, consumer, consumer_conf)
    core.log.info("hit key-auth rewrite")
end
```

插件**只是返回 `401` 和响应体**，并不直接写网络响应。
框架在第 1 章讲过的地方处理返回值：看到 code 就 `core.response.exit`，请求不再继续。

一个容易忽略的分支：如果配了 `anonymous_consumer`，缺少或错误的凭证**不会**直接 401，
而是挂上一个匿名 Consumer 继续处理。
这个选项适合「必须登录才能下单，但匿名也能浏览」的场景。

## 7. 识别成功后，调用方信息去哪了

`attach_consumer` 在 [apisix/consumer.lua:212–221](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/consumer.lua#L212-L221)：

```lua
function _M.attach_consumer(ctx, consumer, conf)
    ctx.consumer = consumer
    ctx.consumer_name = consumer.consumer_name
    ctx.consumer_group_id = consumer.group_id
    ctx.consumer_ver = conf.conf_version

    core.request.set_header(ctx, "X-Consumer-Username", consumer.username)
    core.request.set_header(ctx, "X-Credential-Identifier", consumer.credential_id)
    core.request.set_header(ctx, "X-Consumer-Custom-ID", consumer.custom_id)
end
```

两件事同时发生：

1. 调用方信息写进 `ctx`，后续插件可以读，也可以按 Consumer 做差异化策略。
2. 向上游注入三个 `X-Consumer-*` / `X-Credential-Identifier` 请求头，后端可以直接知道是谁在调用。

**我们的演示后端只回显路径和方法，不打印请求头，所以这三个头没有在实验里被验证过。**
本章只做静态阅读，不对它们的实际到达情况下结论。

## 8. 凭证查询为什么不是每次查 etcd

[consumer.lua:261–279](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/consumer.lua#L261-L279)：

```lua
function _M.consumers_kv(plugin_name, consumer_conf, key_attr)
    local consumers = lrucache("consumers_key#" .. plugin_name, consumer_conf.conf_version,
        create_consume_cache, consumer_conf, key_attr)

    return consumers
end

function _M.find_consumer(plugin_name, key, key_value)
    ...
    local consumers = _M.consumers_kv(plugin_name, consumer_conf, key)
    consumer = consumers[key_value]
```

关键点：

- 缓存键里带了 `consumer_conf.conf_version`。
- `create_consume_cache` 把凭证映射成一张「值 → Consumer」的表。

所以每次请求的凭证校验是**内存哈希查找**。
当 Consumer 配置版本变化时，缓存按新版本重建——这和第 2 章的机制是同一套思路。

Consumer 配置对象本身也是在 [298–310 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/consumer.lua#L298-L310) 用 `automatic = true` 创建的：

```lua
    local cfg = {
        automatic = true,
        checker = check_consumer,
    }

    consumers, err = core.config.new("/consumers", cfg)
```

## 9. 一个反直觉的对比：优先级不等于一切

这是我们实验里两条路由涉及插件的阶段与优先级：

| 插件 | 实现阶段 | priority |
| --- | --- | --- |
| `key-auth` | `rewrite` | 2500 |
| `proxy-rewrite` | `rewrite` | 1008 |
| `limit-count` | `access` | 1002 |

由此得到两个结论：

- **同阶段内**，priority 大的先执行。所以 `protected` 路由上 `key-auth` 先于 `proxy-rewrite`：认证不过，路径改写也没机会执行。
- **跨阶段时**，阶段顺序优先于数值。所以 `limited` 路由上，`proxy-rewrite`（rewrite 阶段）**先于** `limit-count`（access 阶段）执行，尽管 1008 > 1002 只是巧合，真正原因是阶段不同。

这就是「插件按优先级执行」这个简略说法容易出错的边界。

限流的拒绝路径可以在 [limit-count 的 rate_limit](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/plugins/limit-count/init.lua#L301-L314) 看到，机制与认证完全一致：

```lua
        if err == "rejected" then
            ...
            return conf.rejected_code
        end
```

同样是「插件返回状态码，框架提前结束请求」。

## 10. 本机实测：401 的请求有没有打到后端？

源码只能证明逻辑，行为还要实测。用一个能观测的方式验证：**数后端访问日志的条数。**

做法是记录后端收到的 `/orders` 请求数，然后依次发起四种请求，再对比增量。

| 请求 | 结果 |
| --- | --- |
| 不带凭证 | HTTP 401 |
| `apikey: wrong-key` | HTTP 401 |
| `apikey: 正确值`（请求头） | HTTP 200，后端返回 `orders-b` |
| `?apikey=正确值`（查询参数） | HTTP 200，后端返回 `orders-a` |

后端收到的订单请求数 **从 11 变成 13，只增加了 2**。
四次请求里有两次 401，**它们没有出现在后端日志里**。

这就把源码结论变成了可观测事实：

> 认证失败发生在转发之前，后端根本没有收到请求。

同时也验证了第 5 节的结论：请求头和查询参数两种来源都能通过。

复现方式（在仓库根目录）：

```bash
python3 scripts/lab.py up
python3 scripts/lab.py init
```

实验仍需自行按第 4 章文档发起请求；上限与风险说明见 [README 的安全与范围](../../README.md#安全与范围)。
本次没有修改仓库代码，只执行了已有的实验环境。

## 11. 先读这五处

| 顺序 | 文件与定位 | 要回答的问题 |
| --- | --- | --- |
| 1 | `apisix/plugins/key-auth.lua`：51、22、41 | 插件元信息、路由级参数、Consumer 级凭证 |
| 2 | `apisix/plugins/key-auth.lua`：69、103 | 凭证从哪里取，失败时怎么返回？ |
| 3 | `apisix/consumer.lua`：261、269 | 凭证校验为什么是内存查找？ |
| 4 | `apisix/consumer.lua`：212、298 | 调用方信息去哪里，配置怎么同步？ |
| 5 | `apisix/plugins/limit-count.lua`：36 | 对比：另一个插件为什么在 access 阶段？ |

## 12. 小练习与后续节奏

1. 如果只配了 `anonymous_consumer` 而匿名 Consumer 又不存在，会返回什么？
2. 为什么「插件配置写在路由上、凭证写在 Consumer 上」这种拆分是有意义的？
3. `protected` 路由上如果认证失败，`proxy-rewrite` 的路径改写会不会执行？
4. 为什么不能简单地说「priority 越大越先执行」？

本章做了静态源码阅读，并用已有实验环境补充了一项可观测验证。
未修改上游代码，未执行源码级单元测试。

后续按「继续」推进：

- 源码第 4 章：轮询、健康检查与重试的具体实现。

这些是待学习主题，不是已完成的章节。

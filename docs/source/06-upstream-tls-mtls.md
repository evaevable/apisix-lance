# 源码第 6 章：上游 TLS 与 mTLS

[上一章：可观测性](05-observability.md) · [返回学习目录](../../README.md)

## 1. 本章要回答的问题

网关到后端这一段，什么时候加密？凭什么信任对方？如果后端要求客户端证书，证书从哪来？

这三个问题分别对应：**scheme、证书校验、mTLS 客户端证书。**
其中第二个问题，本章会给出一个容易被忽略的结论。

阅读版本固定到提交 `2f8ebf8527cdc878afe30100077c471879a5c133`（tag 3.14.1）。
说明文字是学习笔记；标记为“原文摘录”的代码来自 Apache APISIX。
上游许可证与署名见 [APISIX-LICENSE.txt](APISIX-LICENSE.txt)、[APISIX-NOTICE.txt](APISIX-NOTICE.txt)。

## 2. scheme 决定要不要加密

[apisix/upstream.lua:83–92](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/upstream.lua#L83-L92)：

```lua
local function set_upstream_scheme(ctx, upstream)
    -- plugins like proxy-rewrite may already set ctx.upstream_scheme
    if not ctx.upstream_scheme then
        -- the old configuration doesn't have scheme field, so fallback to "http"
        ctx.upstream_scheme = upstream.scheme or "http"
    end

    ctx.var["upstream_scheme"] = ctx.upstream_scheme
end
```

三个要点：

- **默认是 `http`**。老配置没有 `scheme` 字段也能运行。
- **插件可以先改写 scheme**，一旦被预设，这里就不再覆盖。所以"路由上写了 https"不一定等于最终就是 https。
- 这个值最终进入 Nginx 变量，被 `proxy_pass` 使用（第 1 章的 `$upstream_scheme://...`）。

配套还有一张端口表
（[94–102 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/upstream.lua#L94-L102)）：

```lua
local scheme_to_port = {
    http = 80,
    https = 443,
    grpc = 80,
    grpcs = 443,
}
```

它的用处是判断"要不要把端口拼进 Host"，见第 4 章 `parse_server_for_upstream_host`：
**端口等于该 scheme 的默认端口时不拼接。**

## 3. 上游 TLS 的 SNI 是谁

生成的 Nginx 配置里有这么两行
（[ngx_tpl.lua:781–784](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/cli/ngx_tpl.lua#L781-L784)）：

```nginx
        {% if http.proxy_ssl_server_name then %}
        proxy_ssl_name $upstream_host;
        proxy_ssl_server_name on;
        {% end %}
```

含义是：**开启后，向上游发起 TLS 时使用的 SNI 取 `$upstream_host`。**

而这个开关本身是启动配置，默认开启
（[cli/config.lua:151](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/cli/config.lua#L151)）：

```lua
      proxy_ssl_server_name = true,
```

两点提醒：

- 它是**静态渲染进配置的**，不是按路由动态切换的。要变更需改配置并生效。
- 既然 SNI 取自 `upstream_host`，那 `pass_host` 和 `upstream_host` 怎么配，会直接影响上游看到什么 SNI。

## 4. mTLS：客户端证书从哪来

如果后端要求网关出示客户端证书，有两种配置方式，**互斥**。

### 方式一：引用 SSL 资源（推荐）

配置 `tls.client_cert_id`，引用一个 SSL 资源。
[apisix/init.lua:527–547](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/init.lua#L527-L547)：

```lua
    if api_ctx.matched_upstream and api_ctx.matched_upstream.tls and
        api_ctx.matched_upstream.tls.client_cert_id then

        local cert_id = api_ctx.matched_upstream.tls.client_cert_id
        local upstream_ssl = router.router_ssl.get_by_id(cert_id)
        if not upstream_ssl or upstream_ssl.type ~= "client" then
```

**这里有个硬性约束：SSL 资源的 `type` 必须是 `client`**，否则直接返回 502。
写成 server 类型、或 ID 不存在，都会导致请求失败。

### 方式二：内联 PEM

直接在配置里写 `tls.client_cert` 与 `client_key`。
写配置时就会校验
（[upstream.lua:409–416](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/upstream.lua#L409-L416)）：

```lua
    if conf.tls and conf.tls.client_cert then
        local cert = conf.tls.client_cert
        local key = conf.tls.client_key
        local ok, err = apisix_ssl.validate(cert, key)
```

### 为什么说互斥

在 schema 里表达得很清楚
（[schema_def.lua:423–429](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/schema_def.lua#L423-L429)）：

```lua
            dependencies = {
                client_cert = {required = {"client_key"}},
                client_key = {required = {"client_cert"}},
                client_cert_id = {
                    ["not"] = {required = {"client_cert", "client_key"}}
                },
```

- `client_cert` 必须和 `client_key` 一起出现——只给证书不给私钥没有意义。
- `client_cert_id` **不能**和它们同时出现。

**实践上更推荐方式一**：私钥集中存在 SSL 资源里，不用散落在每条路由的 JSON 中。

## 5. 证书怎么被加载和应用

拿到 PEM 之后要解析成 Nginx 能用的形式
（[upstream.lua:269–300](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/upstream.lua#L269-L300)）：

```lua
    local tls_has_cert = up_conf.tls and (up_conf.tls.client_cert or up_conf.tls.client_cert_id)
    if (scheme == "https" or scheme == "grpcs") and tls_has_cert then
        local client_cert, client_key
        if up_conf.tls.client_cert_id then
            client_cert = api_ctx.upstream_ssl.cert
            client_key = api_ctx.upstream_ssl.key
        else
            client_cert = up_conf.tls.client_cert
            client_key = up_conf.tls.client_key
        end
        ...
        local cert, err = apisix_ssl.fetch_cert(sni, client_cert)
        ...
        local key, err = apisix_ssl.fetch_pkey(sni, client_key)
```

解析本身带缓存
（[ssl.lua:221–228](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/ssl.lua#L221-L228)）：

```lua
function _M.fetch_cert(sni, cert)
    local parsed_cert, err = cert_cache(cert, nil, parse_pem_cert, sni, cert)
```

**解析结果走 LRU 缓存，不是每个请求重新解析 PEM。**

最后应用给 Nginx，`grpcs` 走一条单独的路
（[292–316 行](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/upstream.lua#L292-L316)）：
普通 https 立即调用 `set_upstream_tls_client_param(cert, key)`；
grpcs 先把证书存进 `ctx`，稍后由 `set_grpcs_upstream_param` 应用。

## 6. 本章最关键的一点：上游证书默认不被校验

这部分是安全上最容易误判的地方，分三步看证据。

**第一步：CA 包是配好的。** 默认配置里
（[cli/config.lua:71](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/cli/config.lua#L71)）：

```lua
      ssl_trusted_certificate = "system",
```

它被渲染成
（[ngx_tpl.lua:740–742](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/cli/ngx_tpl.lua#L740-L742)）：

```nginx
        {% if ssl.ssl_trusted_certificate ~= nil then %}
        proxy_ssl_trusted_certificate {* ssl.ssl_trusted_certificate *};
        {% end %}
```

**第二步：但没有开启校验。** 实测我们这台网关生成的实际配置文件：

```text
300:        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
330:        proxy_ssl_name $upstream_host;
331:        proxy_ssl_server_name on;
--- 是否存在 proxy_ssl_verify ---
0
```

**`proxy_ssl_verify` 的出现次数是 0。**
也就是说：CA 包路径配好了，SNI 也发了，唯独"校验对端证书"这个开关没打开。

**第三步：源码也印证了能力边界。** `tls.verify` 的 schema 描述写得很直接
（[schema_def.lua:416–420](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/schema_def.lua#L416-L420)）：

```lua
                verify = {
                    type = "boolean",
                    description = "Turn on server certificate verification, "..
                    "currently only kafka upstream is supported",
                    default = false,
                },
```

**默认 false，且描述说明该字段目前只有 kafka 上游支持。**

把三步合起来，结论是：

> **在 3.14.1 的这一默认配置下，网关到上游的连接会加密，但不会验证对方证书链。**
> 这能防被动窃听，**不能防主动中间人**。

这是一个必须显式评估的边界，不能把"配了 https"当成"已经验证了对端身份"。

## 7. 实测：https 上游对接明文后端

为了确认 TLS 真的会被执行，我临时建了一条把上游 scheme 改成 `https`、但后端仍是明文 HTTP 的路由：

| 步骤 | 结果 |
| --- | --- |
| 创建临时路由（`scheme: https` → `products:8080`） | HTTP 201 |
| 请求该路由 | **HTTP 502** |

APISIX 错误日志给出了原因：

```text
[crit] SSL_do_handshake() failed (SSL: error:0A0000C6:SSL routines::packet length too long
       ...:record layer failure) while SSL handshaking to upstream,
       upstream: "https://172.19.0.2:8080/tlsprobe/1"
```

信息量很大：

- `SSL_do_handshake() failed` —— **网关确实发起了 TLS 握手**，`scheme` 不是装饰。
- `packet length too long` / `record layer failure` —— 对端回的是明文 HTTP，不符合 TLS 记录格式。
- 日志里的 `upstream` 前缀是 `https://`，再次确认 scheme 生效。

清理也做了验证：删除临时路由返回 200，之后该路径恢复 404，**订单路由仍为 200**，实验环境未受影响。
本次只通过 Admin API 临时增删了一条路由，**未修改仓库代码**。

## 8. 本章没有验证什么

- **没有跑通一次成功的 mTLS 链路**：需要一个要求客户端证书的 TLS 后端，本章没有搭建。
  因此 `client_cert_id` 的端到端效果、以及"资源 type 不是 client 会 502"这条路径，**只有源码依据，没有实测**。
- **没有验证 `tls.verify` 的实际行为**，只引用了 schema 描述。
- **没有测试自定义 CA**（即把 `ssl_trusted_certificate` 改成自己的 CA 包）的效果。

这三项都是真实存在的空白，需要时应单独搭环境验证。

## 9. 先读这五处

| 顺序 | 文件与定位 | 要回答的问题 |
| --- | --- | --- |
| 1 | `apisix/upstream.lua`：83、94 | scheme 从哪来，默认值是什么？ |
| 2 | `apisix/cli/ngx_tpl.lua`：740、781 | CA 包与 SNI 是怎么写进配置的？ |
| 3 | `apisix/init.lua`：527 | client_cert_id 引用有什么硬性约束？ |
| 4 | `apisix/upstream.lua`：269、292 | 证书怎么加载、解析、应用？ |
| 5 | `apisix/schema_def.lua`：416、423 | verify 的能力边界与互斥规则？ |

## 10. 小练习与后续节奏

1. 路由上写了 `scheme: https`，为什么最终不一定走 TLS？
2. 上游看到什么 SNI，由哪个变量决定？
3. "连接加密了"和"验证了对方身份"为什么是两件事？本章的证据支持哪个结论？
4. 为什么更推荐用 `client_cert_id` 而不是内联 PEM？

本章做了静态源码阅读，并补充两项实测（https 上游握手失败、实际 nginx.conf 指令检查）。
未修改上游代码，未搭建 TLS 后端，未验证成功的 mTLS 链路。

**这是三个主题中的第一个。** 按你的要求一个一个来，继续时讲：

- 第二个主题：插件开发——写一个插件要满足哪些约束，`_meta` 怎么用。
- 第三个主题：多网关高可用——控制面/数据面分离下的配置分发。

这两项是待学习主题，不是已完成章节。

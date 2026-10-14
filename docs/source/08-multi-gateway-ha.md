# 源码第 8 章：多网关高可用与配置分发

[上一章：自己写一个插件](07-plugin-development.md) · [返回学习目录](../../README.md)

## 1. 单网关的问题

到这里为止，我们的实验只有**一个**网关实例。它能处理请求、能热更新配置、能限流认证。

但它有一个致命弱点：**这一个进程挂了，整个入口就没了。**

所以真实系统会部署多个网关实例。而多实例立刻带来新问题：

- 配置怎么保证每个实例都一样？
- 谁来写配置，谁来读配置？
- 一个实例刚启动，怎么拿到已有规则？

阅读版本固定到提交 `2f8ebf8527cdc878afe30100077c471879a5c133`（tag 3.14.1）。
标记为“原文摘录”的代码来自 Apache APISIX；上游许可证与署名见
[APISIX-LICENSE.txt](APISIX-LICENSE.txt)、[APISIX-NOTICE.txt](APISIX-NOTICE.txt)。

## 2. 三种部署角色

默认配置里就写着角色
（[cli/config.lua:359–363](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/cli/config.lua#L359-L363)）：

```lua
  deployment = {
    role = "traditional",
    role_traditional = {
      config_provider = "etcd"
    },
```

| 角色 | 干什么 | 配置来源 |
| --- | --- | --- |
| `traditional` | 同一个实例既提供管理接口又代理业务 | etcd（默认） |
| `control_plane` | 只提供管理接口，通常不代理业务 | etcd |
| `data_plane` | 只代理业务，**不接收配置写入** | etcd / xds / yaml |

后两种角色的配置键在官方示例里可以确认
（[config.yaml.example:681–685](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/conf/config.yaml.example#L681-L685)）：

```yaml
  role_data_plane:            # Set data plane details if role is data_plane.
    config_provider: etcd     # Set the configuration center: etcd, xds, or yaml.

  role_control_plane:         # Set control plane details if role is control_plane.
    config_provider: etcd     # Set the configuration center.
```

我们前七章的实验就是 `traditional`，所以管理接口和业务接口在同一个容器里。

## 3. 角色带来的三个实际差别

### 差别一：数据面不做 etcd 初始化

[cli/ops.lua:884–886](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/cli/ops.lua#L884-L886)：

```lua
    if env.deployment_role ~= "data_plane" then
        init_etcd(env, args)
    end
```

`init_etcd` 只在非数据面执行。
**初始化 etcd 是控制面的职责**，数据面只负责读。

### 差别二：管理端口由 `enable_admin` 决定

[ngx_tpl.lua:600](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/cli/ngx_tpl.lua#L600)：

```lua
    {% if enable_admin then %}
    server {
```

而 `enable_admin` 默认是 `true`
（[cli/config.lua:23](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/cli/config.lua#L23)）。
**要在数据面关掉管理接口，必须显式设为 false。**

### 差别三：控制面不渲染业务 server

[ngx_tpl.lua:674](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/cli/ngx_tpl.lua#L674)：

```lua
    {% if deployment_role ~= "control_plane" then %}
```

这一整块（包括承载代理逻辑的 `location /`）在 `control_plane` 角色下**不会被渲染**。

反过来，控制面的管理接口地址有一个便利默认值
（[ops.lua:543–550](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/cli/ops.lua#L543-L550)）：
不单独配管理监听时，取业务监听的地址。所以控制面访问管理接口用的就是它的主端口。

## 4. 我们的实验：两个网关共享一份 etcd

本节是真的搭起来并测过的。新增的实例配置在
[`labs/01-gateway/apisix/config-data-plane.yaml`](../../labs/01-gateway/apisix/config-data-plane.yaml)，关键内容：

```yaml
deployment:
  # 数据面角色：只处理业务请求，从 etcd 读取配置，不写配置。
  role: data_plane
  role_data_plane:
    config_provider: etcd
  etcd:
    host:
      - http://etcd:2379
    prefix: /apisix
```

外加 `apisix.enable_admin: false` 显式关闭管理接口。

现在实验环境里有：

| 容器 | 角色 | 对外端口 |
| --- | --- | --- |
| `apisix` | traditional | 业务 `127.0.0.1:9080`、管理 `127.0.0.1:9180` |
| `apisix-dp` | **data_plane** | 只有业务 `127.0.0.1:9081` |

两个实例连的是**同一个 etcd**，插件列表也保持一致
——列表不一致会让同一份路由在两个实例上行为不同。

### 实测一：监听端口的差别

直接对比两个容器生成的 `nginx.conf`：

```text
=== 数据面 ===
        listen 127.0.0.1:9090;
        listen 127.0.0.1:9091 reuseport;
        listen 0.0.0.0:9080 default_server reuseport;
        listen 0.0.0.0:9443 ssl default_server reuseport;

=== 主网关 ===
        listen 127.0.0.1:9090;
        listen 127.0.0.1:9091 reuseport;
        listen 0.0.0.0:9180;                          <-- 管理接口
        listen 0.0.0.0:9080 default_server reuseport;
        listen 0.0.0.0:9443 ssl default_server reuseport;
```

**数据面完全没有 `9180`**——管理接口不是"被拒绝访问"，而是**根本没有监听**。

### 实测二：配置分发（本章最关键的一次验证）

做法：**不重启任何容器**，只在主网关的管理接口创建一条新路由，然后观察数据面多久能用到它。

| 步骤 | 主网关 | 数据面 |
| --- | --- | --- |
| 创建前访问新路径 | 404 | 404 |
| 经主网关管理接口创建路由 | HTTP 201 | — |
| 访问新路径 | **200（0.01s）** | **200（0.02s）** |
| 删除该路由后 | — | **404（0.00s）** |

**数据面从来没有人碰过它，却自动获得了新路由，并且删除后也同步失效。**

这就是第 2 章那套机制的实战价值：数据面同样在后台订阅 etcd（
[`core/config_etcd.lua` 的 `_automatic_fetch`](https://github.com/apache/apisix/blob/2f8ebf8527cdc878afe30100077c471879a5c133/apisix/core/config_etcd.lua#L874-L972)），
配置一变就更新本地内存。

本地环境的分发延迟是**几十毫秒级**。但这个数字只代表本机同网环境，**不能外推到生产**。

### 实测三：冒烟检查扩展到双网关

`scripts/lab.py smoke` 增加了一项，直接验证两个网关服务同一份路由：

```text
PASS second gateway (data_plane): HTTP 200
PASS config distribution: both gateways serve the same route
```

完整冒烟现在是 **16 项检查全部通过**。

## 5. 这个实验离"生产高可用"还有多远

这一节很重要，避免把刚才的结果误读成"我已经有高可用架构了"。

**第一，前面还没有负载均衡。**
两个网关只是同时存在，没有任何东西在它们之间分流量。
真实部署需要在网关前面放 L4/L7 负载均衡（或者用 DNS/Anycast），这部分**本章没有做**。

**第二，etcd 仍然是单点。**
我们的 etcd 是**单节点、无认证的 HTTP**。
它挂了之后改不了配置——第 2 章的结论在这里同样适用：已加载配置还能继续代理，但配置更新会停摆。

**第三，限流在多网关下会"额度放大"。**
第 3 章提过我们的实验用的是 `policy: local`。它的含义是**每个实例各自计数**。
两台网关各自允许 2 次，客户端实际就能过 4 次。
**要跨实例共享额度，必须改用集中式策略（如 redis），这是配置层面的选择，不是自动生效的。**

**第四，真实机器故障没有演练。**
两个实例都在同一台机器上，宿主机故障时一起没了。

**因此本节验证的是"配置分发机制"，不是"高可用能力"。**

## 6. 本章没有验证什么

- **没有部署 `control_plane` 角色**：控制面不渲染业务 server 这一条**只有源码依据**。
- **没有验证 xds / yaml 作为数据面配置中心**，只用了 etcd。
- **没有搭 etcd 集群**，也没有做 etcd 故障演练。
- **没有做网关实例故障切换**：停掉一个实例后，请求不会自动被导到另一个。
- 没有做跨机器部署和网络分区测试。

## 7. 先读这五处

| 顺序 | 文件与定位 | 要回答的问题 |
| --- | --- | --- |
| 1 | `apisix/cli/config.lua`：359 | 默认角色是什么，配置来源是什么？ |
| 2 | `apisix/cli/ops.lua`：884 | 为什么数据面不初始化 etcd？ |
| 3 | `apisix/cli/ngx_tpl.lua`：600、674 | 管理接口与业务 server 各由什么控制？ |
| 4 | `apisix/cli/ops.lua`：543 | 控制面的管理地址默认取自哪里？ |
| 5 | 本仓库 `labs/01-gateway/apisix/config-data-plane.yaml` | 数据面最小配置长什么样？ |

## 8. 小练习与后续节奏

1. 数据面为什么不初始化 etcd？如果它先启动、控制面后启动，会怎样？
2. 数据面完全没有监听 9180，和"监听了但拒绝你"有什么本质区别？
3. 两个网关都用 `policy: local` 限流，客户端实际能发多少次请求？
4. 为什么"配置能秒级分发"不等于"我已经高可用了"？

---

## 三个主题到此收尾

按你的要求，三个主题一个一个讲完了：

| 章节 | 主题 |
| --- | --- |
| 第 6 章 | 上游 TLS 与 mTLS |
| 第 7 章 | 自己写一个插件 |
| 第 8 章 | 多网关高可用与配置分发 |

源码系列现在共 **8 章**，覆盖：请求生命周期、配置热更新、认证插件、负载均衡与容错、可观测性、上游 TLS、插件开发、多网关与配置分发。

后续可以继续的方向（**都是待学习主题，不是已完成章节**）：

- 网关前的负载均衡与实例级故障切换演练。
- 用 Redis 实现跨实例共享限流。
- etcd 集群部署与故障演练。
- 数据可观测性进阶：OpenTelemetry 链路追踪。
- APISIX 与 Kubernetes Ingress 的集成。

本章真的起了第二个网关并实测了配置分发；同时明确划出了"这还不是高可用"的边界。
未部署控制面角色，未搭 etcd 集群，未做实例故障切换。

# 05：验证记录与边界

[上一章：动手实验](04-quickstart.md) · [返回目录](../README.md)

## 本地实测结果

验证日期：2026-10-13。
环境：macOS / Apple Silicon，Docker Server 27.0.3（Colima、linux/arm64），独立 Docker Compose 2.29.1，宿主 Python 3.13.12。
容器中的 APISIX 实测版本为 3.14.1。

| 检查 | 结果 |
| --- | --- |
| 无第三方依赖单元测试 | 14 项全部通过 |
| 4 份 Route JSON 的语法和基础结构 | 通过（后扩展为 5 份，见下） |
| Docker Compose 配置解析 | 通过 |
| etcd 和 3 个 Python 后端健康检查 | 通过 |
| APISIX `apisix test` | `configuration test is successful` |
| Consumer 和 4 条 Route 经 Admin API 写入 | 全部成功 |
| 订单路由、商品路由 | HTTP 200，响应内容符合预期 |
| 40 次订单请求覆盖两个实例 | 观察到 `orders-a` 与 `orders-b` |
| 未匹配路径 | HTTP 404 |
| Admin API 不带密钥 | HTTP 401 |
| 认证路由无密钥或错误密钥 | HTTP 401 |
| 认证路由正确 Consumer 密钥 | HTTP 200 |
| 路径改写 | 后端收到 `/orders/123` |
| 同一计数键连续 3 次限流请求 | `200, 200, 429` |
| 同一运行环境再次执行完整冒烟测试 | 通过，新的演示计数键避免旧额度干扰 |
| etcd 路由键查询 | 找到实验的 4 条 Route 键 |

## 镜像复现信息

镜像采用固定版本标签，不使用 `latest`。本次拉取报告的摘要为：

```text
apache/apisix:3.14.1-debian
sha256:c228717165ecf4c0055818c9e2d9843f7b303f17a6df9e5fbaf8eafeb5007ae4

quay.io/coreos/etcd:v3.5.21
sha256:a3ccd299377af4b9968d8cbaaf9722fe354297f11908f74a846075e1e5f4ee89

python:3.13.7-slim
sha256:5f55cdf0c5d9dc1a415637a5ccc4a9e18663ad203673173b8cda8f8dcacef689
```

Compose 当前引用固定版本标签，未以摘要锁定；标签的不可变性仍由镜像发布者决定。
上述摘要是本次 Docker 拉取输出，用于记录，不代表完成了镜像签名或漏洞审计。

## 自动验证

`.github/workflows/verify.yml` 配置了 GitHub Actions：

1. 运行单元测试。
2. 检查 Route JSON 和 Compose 配置。
3. 启动 APISIX、etcd 和后端，初始化路由并执行冒烟测试。
4. 失败时输出容器日志，最后停止实验。

本记录的“通过”指本机实际执行结果，不等于已查看或确认远程 GitHub Actions 运行状态。

## 后续补充验证

- 源码第 5 章：比对访问日志字段，并实测故障期间延迟从毫秒级放大到 3.1 秒（首次 39 秒）；指标端点在本实验未启用。
- 源码第 6 章：实测 https 上游对接明文后端返回 502 与 TLS 握手失败；确认生成的 nginx.conf 中无 `proxy_ssl_verify`。
- 源码第 7 章：新增自定义插件 `lance-request-id`，冒烟检查扩展为 14 项全部通过；实测 `_meta.disable` 生效；修复了移除 `prometheus` 导致 stream 插件加载失败的配置问题。

上述补充验证的细节在各章文档中记录。这些验证同样只针对本机实验环境。

## 没有验证什么

- 多节点 etcd 容错、备份恢复和磁盘损坏恢复。
- 多 APISIX 节点下的全局限流和配置同步时延。
- 生产身份授权、TLS/mTLS、网关高可用和公网暴露防护。
- 压力测试、性能指标、完整插件覆盖和安全漏洞审计。
- Windows 原生运行、Linux/amd64 的本机实测；CI 配置不等于已验证这些环境。

这是可运行的入门练习，不应据此推断生产可用性。

# APISIX Lance：边学边练 API 网关

这是一个中文 APISIX 学习仓库，承接「APISIX 是什么」「etcd 是什么」的入门讲解。
内容是原创学习笔记与小型实验，不是 Apache APISIX 官方源码镜像，也不是生产部署方案。

## 先读什么

1. [APISIX：统一入口与核心概念](docs/01-apisix-basics.md)
2. [etcd：分布式配置账本](docs/02-etcd-basics.md)
3. [配置流与请求流：为什么请求不逐次查询 etcd](docs/03-request-and-config-flow.md)
4. [动手实验：路由、负载均衡、认证与限流](docs/04-quickstart.md)
5. [验证记录与已知边界](docs/05-verification.md)

## 源码学习：逐章看实现

基础课程讲“怎么用”，源码课程讲“为什么能这样工作”。每次先在对话中讲解一章，再保存经过源码核对的笔记；不会把未来章节一次性堆进仓库。

1. [源码第 1 章：一次 HTTP 请求如何穿过 APISIX](docs/source/01-http-request-lifecycle.md)
2. [源码第 2 章：配置热更新是怎么实现的](docs/source/02-config-hot-reload.md)
3. [源码第 3 章：key-auth 如何识别调用方](docs/source/03-key-auth-and-plugin-phases.md)

源码阅读固定在官方 APISIX **3.14.1**，提交 `2f8ebf8527cdc878afe30100077c471879a5c133`，与实验的 APISIX 版本对应。笔记使用固定提交的源码链接；少量原文摘录的上游许可证与署名随文保留，不把整个上游仓库复制进学习仓库。

后续再逐章追踪负载均衡、健康检查与重试；尚未完成的主题不作为已交付章节。

## 仓库内容

```text
docs/                         中文学习笔记与实验步骤
labs/01-gateway/
  compose.yaml                APISIX + etcd + 3 个演示后端
  apisix/config.yaml          本地传统 etcd 模式配置
  backend/server.py           无第三方依赖的 Python JSON 后端
  routes/*.json               4 条网关路由配置
scripts/lab.py                密钥生成、启停、路由初始化、冒烟验证
tests/test_lab.py             无 Docker 依赖的单元测试
.github/workflows/verify.yml  GitHub Actions 单元测试与集成验证
```

## 快速开始

前提：Python 3.10+、运行中的 Docker、Docker Compose v2；首次启动需要访问镜像仓库。
以下命令在**仓库根目录**运行，macOS/Linux 通常使用 `python3`，Windows 可使用 `python`。

```bash
python3 -m unittest discover -s tests -v
python3 scripts/lab.py prepare
python3 scripts/lab.py up
python3 scripts/lab.py init
python3 scripts/lab.py smoke
```

先读文档、不启动 Docker 也完全可以。`validate` 只检查 JSON 语法和基本结构，不等于网关运行通过。

```bash
python3 scripts/lab.py validate
curl http://127.0.0.1:9080/orders/123
curl http://127.0.0.1:9080/products/789
python3 scripts/lab.py down
```

停止实验不会删除 etcd 数据卷，也不会删除本地密钥。

## 实验能看到什么

| 请求 | 预期行为 |
| --- | --- |
| `GET /orders/123` | 请求发往两个订单实例，可在 JSON 中看到 `orders-a` 或 `orders-b` |
| `GET /products/789` | 请求发往商品实例 |
| `GET /missing` | 无匹配路由，返回 404 |
| `GET /protected/orders/123` | 无凭证或错误凭证返回 401；正确 `apikey` 返回 200 |
| `GET /limited/orders/123` | 同一个演示计数键 60 秒内前 2 次返回 200，第 3 次返回 429 |

认证路由和限流路由会把额外的路径前缀去掉，后端仍收到 `/orders/123`。

## 安全与范围

- 宿主机只发布 `127.0.0.1:9080` 和 `127.0.0.1:9180`，不直接发布 etcd 或后端端口。
- 随机 `ADMIN_KEY`、`CONSUMER_KEY` 存在 `labs/01-gateway/.env`，默认权限 0600，已被 Git 忽略。
- etcd 是单节点、无认证 HTTP 的本地实验环境，没有生产级高可用和安全防护。
- 为接受 Docker bridge 的管理请求，容器内 `allow_admin` 较宽；不能复制到公网部署。
- 限流使用调用方可控的 `X-Lab-Run` 请求头，方便重复测试；可以换头绕过，**不是安全限流方案**。
- 限流 `policy: local` 是单网关本地计数，不能理解为多个网关共享的全局额度。
- Python 后端用于回显，不存订单、不实现业务权限、不适合生产。
- 镜像固定为 APISIX `3.14.1-debian`、etcd `v3.5.21`、Python `3.13.7-slim`，是实验版本，不声称为最新版；生产需审查受支持版本、安全公告与镜像摘要。

## 继续学习路线

Route/Upstream 分离 → Service 配置复用 → Consumer 与认证 → 可信身份限流 → 健康检查与重试 → 灰度发布 → 可观测性 → 高可用与安全加固。

当前已落地的是第一个实验；后续主题不是已完成的功能。

官方参考：[Apache APISIX](https://apisix.apache.org/docs/apisix/) · [etcd](https://etcd.io/docs/)

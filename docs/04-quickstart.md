# 04：动手跑通一个 API 网关

[上一章：配置流与请求流](03-request-and-config-flow.md) · [返回目录](../README.md) · [下一章：验证记录](05-verification.md)

## 目标和前提

用 APISIX 代理订单、商品两个后端，观察负载均衡，再增加认证、路径改写和限流。
前提是 Python 3.10+、Docker 守护进程、Compose v2，以及首次拉取镜像需要的网络。
本实验不安装第三方 Python 包，不复制 Apache APISIX 官方源码。
所有命令从仓库根目录运行。macOS/Linux 使用 `python3`，Windows 可以改为 `python`。

## 1. 先运行不依赖 Docker 的测试

```bash
python3 -m unittest discover -s tests -v
python3 scripts/lab.py validate
```

单元测试验证后端和辅助脚本；JSON 检查验证语法和基本结构。它们都不能证明 APISIX 真的可运行。

## 2. 生成本地密钥并启动

```bash
python3 scripts/lab.py prepare
python3 scripts/lab.py up
python3 scripts/lab.py status
```

`prepare` 生成随机管理密钥和调用方密钥，写入 `labs/01-gateway/.env`。文件已被 Git 忽略；存在时不覆盖。
不要把两个密钥混用：管理密钥用于 Admin API 的 `X-API-KEY`，调用方密钥用于认证插件的 `apikey`。
启动命令自动选择 `docker compose` 或独立的 `docker-compose`，但都要求 Compose v2。

宿主机只开放两个回环地址：

- `http://127.0.0.1:9080`：业务请求入口。
- `http://127.0.0.1:9180/apisix/admin`：管理入口，需要管理密钥。

etcd 和后端不发布到宿主机；容器仍需在 Docker bridge 网络中相互通信。不是生产级网络隔离。

## 3. 配置调用方和四条路由

```bash
python3 scripts/lab.py init
```

脚本通过 Admin API 写入 Consumer 和 Route，不直接修改 etcd。
同一路由 ID 使用 PUT 更新，多次运行不会创建重复路由。
注意 `init` 会覆盖实验同名路由，所以自行修改实验路由后需要同步修改 JSON 文件。

| JSON 文件 | 实验目的 |
| --- | --- |
| `orders.json` | `/orders/*` 轮询两个订单实例 |
| `products.json` | `/products/*` 转发到商品实例 |
| `protected-orders.json` | `key-auth` 认证，去掉 `/protected` 前缀 |
| `limited-orders.json` | 限流，去掉 `/limited` 前缀 |
| `custom-plugin.json` | 自定义插件 `lance-request-id`，去掉 `/custom` 前缀 |

这些例子把 Upstream 内联在 Route 中，减少首次学习概念数量；后续可拆成独立 Upstream 和 Service。

## 4. 观察路由与负载均衡

```bash
curl http://127.0.0.1:9080/orders/123
curl http://127.0.0.1:9080/orders/123
curl http://127.0.0.1:9080/products/789
curl -i http://127.0.0.1:9080/missing
```

订单请求返回类似下面的 JSON；多次访问应能看到两个实例，不保证两个独立连接严格交替。

```json
{"service":"orders","instance":"orders-a","path":"/orders/123","method":"GET"}
```

如果请求 `/protected/orders/123`，后端收到的是 `/orders/123`。
这是 `proxy-rewrite` 的效果；路由匹配本身不会自动删除路径前缀。

## 5. 一次验证认证、限流和路径改写

```bash
python3 scripts/lab.py smoke
```

脚本直接读取本地调用方密钥，不在输出中打印密钥。检查以下行为：

- 订单和商品路由返回 200，订单请求覆盖两个实例。
- 无匹配路径返回 404。
- Admin API 不带管理密钥返回 401。
- 认证路由无凭证、错误凭证返回 401；正确凭证返回 200。
- 认证后的后端路径是 `/orders/123`，不是 `/protected/orders/123`。
- 同一个实验计数键发出三次限流请求，状态码是 `200, 200, 429`。

限流测试每次生成不同的 `X-Lab-Run`，避免上一次测试占用额度。
但调用方可随意改这个头绕过限制，所以**只能演示，不能用于生产身份识别**。
`policy: local` 也不是跨网关共享计数。

## 6. 看看 etcd 中的配置

以下命令使用 `docker compose`；本机只有独立命令时改为 `docker-compose`。

```bash
docker compose --env-file labs/01-gateway/.env -f labs/01-gateway/compose.yaml exec etcd \
  /usr/local/bin/etcdctl --endpoints=http://127.0.0.1:2379 \
  get /apisix/routes --prefix --keys-only
```

这里只列键名，避免输出 Consumer 等配置中的凭证。
你会发现 etcd 保存路由配置，而不是后端响应 JSON 和订单数据。

## 7. 停止实验

```bash
python3 scripts/lab.py down
```

保留 etcd 数据卷和 `.env`，下次启动可复用。脚本不提供自动删除数据功能。
同一台机器不要同时启动两个使用相同项目名和端口的实验副本。

## 常见故障与排查

- **Docker 不可用**：启动 Docker Desktop 或已有 Colima；不要仅凭 Docker 客户端已安装判断守护进程正常。
- **找不到 Compose**：安装 Compose v2；独立 `docker-compose` v2 也可以。
- **镜像拉取超时/EOF**：检查镜像仓库网络；这是下载失败，不代表路由代码失败。
- **9080/9180 已占用**：先确认占用进程，不要直接杀其他项目；调整端口时同步修改脚本 URL。
- **Admin 401/403**：检查本地 `.env`、环境变量是否冲突、管理 IP 白名单；不要通过关闭认证修复。
- **502**：检查后端健康状态与 Docker 网络名称解析。
- **JSON 合法但 Admin API 拒绝**：JSON 语法检查不是 APISIX 插件 schema 校验，查看报错中的具体字段。
- **限流没有 429**：确认三次请求使用相同的 `X-Lab-Run`；不带固定计数头不是本实验的验证方式。

查看状态和日志：

```bash
docker compose --env-file labs/01-gateway/.env -f labs/01-gateway/compose.yaml ps
docker compose --env-file labs/01-gateway/.env -f labs/01-gateway/compose.yaml logs --tail=100 apisix
```

官方参考：[Admin API](https://apisix.apache.org/docs/apisix/admin-api/) · [key-auth](https://apisix.apache.org/docs/apisix/plugins/key-auth/) · [limit-count](https://apisix.apache.org/docs/apisix/plugins/limit-count/) · [proxy-rewrite](https://apisix.apache.org/docs/apisix/plugins/proxy-rewrite/)

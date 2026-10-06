# Extore · 兑所

一个商家的卡密兑换与交付网站。**支付与订单管理在另一平台**；Extore 验证卡密、收集参数、创建处理任务、交付内容或展示服务结果。

支持人工队列、商品员工链接、Python 发货脚本、签名 webhook / 回调。SQLite 持久化任务、事件和投递重试。包含顾客界面、商家后台、Python SDK 与自动化测试。

## 本地运行

需要 Python 3.12+、[uv](https://docs.astral.sh/uv/) 和 Linux（worker 使用文件锁与进程组）。

```sh
uv sync --frozen
uv run python -m extore.cli init
# 可选：仅在空数据库创建一个本地演示商品，输出演示卡密
uv run python -m extore.cli demo
uv run uvicorn extore.app:app --host 127.0.0.1 --port 8000
# 另一个终端；自动发货和 webhook 投递需要 worker
uv run python -m extore.worker
```

打开 `http://localhost:8000`。连续点击左上角 Logo 5 次（2.5 秒内）进入后台，也可访问 `/admin`。输入服务器初始化时设置的密码，注册 Passkey 后获得后台权限，**密码立即失效**。密码会话只能注册 Passkey，不能管理商品。支持多个 Passkey；添加或移除前需要最近 10 分钟内登录。

Passkey 需要 HTTPS 或 localhost。更换域名 / RP ID 后，旧 Passkey 无法用于新域名；须先规划固定域名。

## 基本流程

1. 商家创建商品，设置参数、公开性、处理方式及交付规则。
2. 在后台生成卡密（只显示一次），或支付平台通过制卡接口领取卡密。
3. 顾客输入卡密。非公开商品仅在验证成功后显示。
4. 顾客填写参数，卡密锁定到唯一任务，重复提交返回同一个任务。
5. 人工处理、脚本或外部平台完成任务。顾客保存领取链接，查看状态并领取内容。

成功后卡密不能再次创建任务。可重复查看商品可用原卡密重新获得领取链接；仅一次领取商品须保存原领取链接。领取链接有效 30 天，放在 URL 片段中，HTTP 请求与访问日志不包含凭证。

人工员工链接有效期 1–90 天，限一个商品。员工只能查看该商品任务，领取后才能完成自己领取的任务，支持批量操作与进度更新。链接可以撤销；撤销会立即使会话失效，并释放该员工尚在处理中的任务。员工到期或人工任务长时间无人更新时，商家可核实后放行重试。

自动处理有两种：

- **Webhook**：发送 `redemption.requested`，外部平台按任务 ID 去重，再签名回调状态、进度或内容。
- **Python**：服务器的 `scripts/<name>.py` 接收 JSON，使用 `extore.sdk` 输出进度与最终结果。默认示例 `welcome.py` 不调用外部服务。

内容交付可设置重复查看或仅一次领取；服务交付只显示处理结果。顾客可立即销毁完成的交付：删除应用内内容及参数，后续链接无法查看。**这不会撤回已下载的副本、外部平台内容、已经发出的事件或历史备份，也不承诺物理介质的安全擦除。**一次领取的内容在成功返回后即从数据库中移除；网络中断时无法再次领取，应由商家另行处理。

## 认证恢复

只能在服务器上操作，没有网页密码重置入口：

```sh
uv run python -m extore.cli reset-auth
```

输入 `RESET` 确认，然后在终端私密输入新密码。所有 Passkey、登录会话和认证挑战被撤销。重新进入后台，完成新 Passkey 注册。商品、卡密与任务保留。员工授权链接仍存在；需要撤销时在后台处理。

## 生产运行

配置环境变量（`.env.example` 是示例，CLI/worker 不自动读取 `.env`；须由进程管理器加载）：

```sh
export EXTORE_ORIGIN=https://redeem.example.com
export EXTORE_DATA=/var/lib/extore
export EXTORE_SCRIPTS=/opt/extore/scripts
```

使用反向代理提供 HTTPS。API 与 worker 必须共享同一个数据目录及配置。一个数据库运行一个 worker；API 可运行多个进程，但建议单进程起步。数据目录权限为 0700，部署前 `umask 077`，专用低权限用户运行。数据库、WAL 和 `issuance.key` 都是敏感文件。平台制卡响应为实现幂等重试采用加密存储，密钥独立保存在该文件，必须一并备份。

```sh
uv run uvicorn extore.app:app --host 127.0.0.1 --port 8000 --forwarded-allow-ips=127.0.0.1
uv run python -m extore.worker
```

仅信任实际反向代理的地址。限流以 ASGI 的客户端地址为准；不要将可信代理地址设为 `*`。定期监控 worker 和 `/health`；健康检查仅证明 API / SQLite 可读写，不证明 worker 或外部发货平台可用。备份使用 SQLite 在线备份接口或停服务后复制，不能在写入时单独复制主数据库。

也提供 `Dockerfile` 与 `compose.yaml`。修改 Compose 的 `EXTORE_ORIGIN` 后，在 HTTPS 反向代理后运行：

```sh
mkdir -p data
chmod 700 data
# 容器默认 uid 10001，须保证其可以写入 data；脚本目录只读挂载
sudo chown 10001:10001 data
docker compose build
docker compose run --rm -it api python -m extore.cli init
docker compose up -d
```

脚本目录仅由商家通过 SSH 安装可信代码，**不是沙箱**，会拥有 worker 用户权限；后台不能上传或执行任意脚本文本。仅 `EXTORE_SCRIPT_*` 环境变量会被传给子进程，可用于提供商凭证。脚本超时 120 秒；输出最多 1 MB，单条结果最多 100 KB。

未知、超时、崩溃进入“待核实”，默认不能重试。仅明确未交付的失败允许按商品配置重试；商家可以在核实外部结果后放行。下游仍必须以**稳定任务 ID**避免重复发货，无法跨第三方事务承诺绝对只执行一次。

Webhook 地址须使用 HTTPS 公网 IP；DNS 校验后固定连接 IP、保留域名的 TLS 验证，不跟随重定向，不使用环境代理。默认不接入内网地址。最多自动投递 8 次，指数退避；后台可重新投递停止的事件。

## 开发文档与验证

- [接口、状态与完整事件定义](docs/protocol.md)
- [Python 脚本及回调 SDK](docs/python-sdk.md)
- [验收记录与限制](docs/acceptance.md)
- 服务运行后 `/docs` 提供交互式 OpenAPI 文档。

```sh
uv run pytest -q
uv run python -m compileall -q extore scripts
node --check extore/static/app.js
```

数据格式版本 1；后续修改数据库结构需要显式迁移。本项目没有支付收款、支付订单管理、多商家或员工全系统权限。

## Arch Linux 原生部署

`deploy/arch/PKGBUILD` 在目标机用锁定依赖构建运行环境，以 pacman 包管理文件；不复制本地虚拟环境。API 与 worker 使用独立 systemd 服务、低权限 `extore` 用户、只写 `/var/lib/extore`。脚本由 root 安装在 `/etc/extore/scripts`。

首次无人值守部署可执行 `sudo -u extore extore-admin bootstrap`：生成随机首次密码，存于 `/var/lib/extore/bootstrap-password.txt`，权限 0600，不输出到日志。由服务器操作人员私密读取并完成 Passkey 注册；注册成功后密码文件也会删除。

```sh
# 服务器终端
sudo -u extore extore-admin reset-auth
sudo -u extore extore-admin integration-key
```

`deploy/nginx.conf` 是 `extore.lmm.best` 的反向代理示例，依赖当前服务器的 `msg_safe` 日志格式。通用环境应先定义该格式或使用不含 URL / 查询参数的日志格式。证书须先通过 ACME webroot 签发，再启用 443 配置。

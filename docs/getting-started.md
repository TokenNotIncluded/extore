# 快速入门与运行指南

[返回项目首页](../README.md) · [接口与事件](protocol.md) · [CLI 指南](cli.md) · [原生 WebMCP](webmcp.md) · [Python SDK](python-sdk.md)

Extore 负责卡密验证、信息收集、任务处理与交付，支持隔离的多家店铺。平台管理员维护店铺、注册和 SMTP；店主只管理自己的商品与任务。支付和支付订单管理仍由另一平台承担，通过制卡接口、Webhook 和签名回调对接。付款适配器默认关闭，尚未选定提供商；内置预设不会发起付款。

- [从 PyPI 安装](#从-pypi-安装)
- [本地运行](#本地运行)
- [显示偏好](#显示偏好)
- [基本流程](#基本流程)
- [商品管理链接](#商品管理链接)
- [远程命令行](#远程命令行)
- [自动处理与领取](#自动处理与领取)
- [文件上传与存储](#文件上传与存储)
- [认证恢复](#认证恢复)
- [生产运行](#生产运行)
- [Arch Linux 原生部署](#arch-linux-原生部署)

首次密码、平台制卡密钥、卡密、管理链接和领取链接属于凭证，请勿提交到 Git 或公开反馈中。

## 从 PyPI 安装

需要 Linux、Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)。安装包已包含网页、Python SDK 与固定版本的预设处理器，无需另行克隆 Git 子模块：

```sh
uv tool install extore
export EXTORE_DATA="$HOME/.local/share/extore"
extore init
extore serve
```

另一个终端设置相同的 `EXTORE_DATA` 后执行 `extore worker`。默认网站地址为 `http://localhost:8000`；`extore serve --host 127.0.0.1 --port 8000` 可显式指定监听地址，更换端口或域名时需同步设置 `EXTORE_ORIGIN`。API 与 worker 在前台独立运行，生产环境应交给进程管理器。

`extore --help` 列出全部命令，`extore --version` 输出安装版本。命令包括 `init`、`bootstrap`、`reset-auth`、`integration-key`、`demo`、`serve`、`worker` 与远程 `manage`、`customer`、`admin`；原有 `python -m extore.cli` 管理命令和 `python -m extore.worker` 保持兼容。查看帮助、版本或输入无效参数不会初始化数据库。

从源码构建发行包：

```sh
uv build -q
```

## 本地运行

需要 Python 3.12+、[uv](https://docs.astral.sh/uv/) 和 Linux（worker 使用文件锁与进程组）。

以下源码运行命令在项目根目录执行。

```sh
git clone --recurse-submodules https://github.com/TokenNotIncluded/extore.git
cd extore
# 已有工作目录可先取得主仓库记录的预设处理器版本
git submodule update --init processors/official
uv sync --frozen
uv run python -m extore.cli init
# 可选：仅在空数据库创建一个本地演示商品，输出演示卡密
uv run python -m extore.cli demo
uv run uvicorn extore.app:app --host 127.0.0.1 --port 8000
```

另开一个终端，在项目根目录运行 worker；自动发货和 Webhook 投递需要它：

```sh
uv run python -m extore.worker
```

打开 `http://localhost:8000`。连续点击左上角 Logo 5 次（2.5 秒内）进入后台，也可访问 `/admin`。平台管理员输入服务器初始化密码，注册 Passkey 后获得后台权限，**这套首次密码立即失效**；首次密码会话只能注册 Passkey，不能管理商品。支持多个 Passkey；从浏览器添加或移除前需要最近 10 分钟内登录。[店主 CLI](cli-owner.md#passkey-注册)使用设备签名，注册仍需真实 WebAuthn 结果。

公众注册默认关闭。平台管理员配置加密保存的 SMTP 后，可创建店铺并发送店主邀请。店主从 `/account/login` 使用验证过的邮箱与密码登录，可启用 TOTP 和注册多个 Passkey；店主添加 Passkey 不会禁用邮箱密码。账号、恢复及 CLI 命令见[多店与账号](shops.md)。

Passkey 需要 HTTPS 或 localhost。更换域名 / RP ID 后，旧 Passkey 无法用于新域名；须先规划固定域名。

## 显示偏好

页头可选择自动、浅色或深色主题，以及自动、中文或英文语言，默认均为自动。自动主题实时跟随系统明暗变化；自动语言按浏览器语言偏好顺序选择支持的中文或英文，没有匹配时使用中文。手动选择只保存在当前浏览器。

## 基本流程

1. 商家创建商品，设置公开性、处理方式及交付规则。队列商品自定义顾客输入和交付输出；自动商品选择预设处理器，由处理器定义输入与输出。
2. 在后台生成卡密（只显示一次），或支付平台通过制卡接口领取卡密。
3. 顾客输入卡密。非公开商品仅在验证成功后显示。
4. 顾客填写参数，卡密锁定到唯一任务，重复提交返回同一个任务。
5. 队列管理者、预设处理器或外部平台完成任务。顾客保存领取链接，查看状态并领取内容。队列管理者也可说明原因，要求「修改后重提」或「原资料重试」，或拒绝任务。

成功后卡密不能再次创建任务。可重复查看商品可用原卡密重新获得领取链接；仅一次领取商品须保存原领取链接。领取链接有效 30 天，放在 URL 片段中，HTTP 请求与访问日志不包含凭证。

每个商品有独立的队列，查看、筛选和批处理都限定当前商品。人或 AI 领取任务后，才能更新、完成或标记自己领取的队列任务；自动任务由预设处理器或外部平台处理。

队列默认显示「待处理」，包含排队、处理中和失败待核实任务；成功交付及已销毁任务收进「已处理」，可主动切换查看。原生 AI 工具使用同样的默认范围，避免把历史任务反复带进上下文。

同商品的多张卡密可一起粘贴验证，最多 30 张，共用一个领取链接。每张分别填写启动参数和上传文件，非文件参数可从第一张复制到其余卡密；之后可查看整体进度或逐张进入领取、重试和销毁。

内容交付支持多个输出字段，如资源链接、账户邮箱、说明文本和文件；服务商品不定义输出字段，只显示处理状态。输入和输出均可使用中英文名称与 Markdown 教程。队列商品可继续修改未来任务的输入输出，已有任务保存各自的结构；自动商品发行卡密后，输入输出结构及处理器不能改变。

商品可定义规格档位、参考价和属性，按档位发行卡密并统计库存。`variants.price` 仅供外部商城配置参考；Extore 只负责兑换与交付，不收款。商家在支付平台设置实际售价、收款和销售库存。商品信息可一键复制为供 AI 创建上游商品的提示词。队列支持步骤、已完成步骤、给顾客的消息和商品内排位，顾客等待页显示循环动画与商家邮箱快捷链接。

卡密按商品和批次管理，可设置到期时间，查看剩余库存、使用、验码、领取与处理统计。查询用内部 ID 或尾号定位，不重新显示原卡密；失败可重试卡密单独统计；需要重试的卡密计回剩余，但仍绑定原顾客任务，不能当作从未使用的卡密再次销售。原因可分为顾客资料、外部服务或处理程序问题；原资料重试不会要求顾客重新上传材料。已拒绝卡密不能重提，独立计数。

## 商品管理链接

商家为某个商品创建管理链接，按需选择权限：查看队列、处理队列、核实后放行重试、编辑商品、配置自动发货、管理卡密、管理事件，以及向下生成管理链接。链接持有人无需获得全店后台权限。

商品编辑和自动发货配置分别授权。只编辑展示信息和顾客参数的管理者不能读取签名密钥或修改发货、查看与重试规则；「配置自动发货」可以控制该商品的外部发货，不应授予只负责展示编辑的人。

店长可获得该商品的全部权限，其中「生成管理链接」允许继续委派。子链接必须属于同一个商品，权限必须比父链接少，期限不得超过父链接。例如，店长可以给处理人员生成只有「查看队列、处理队列」的链接；处理人员没有委派权限，就不能继续生成链接。

链接有独立的浏览器登录和 CLI 设备绑定额度，`max_uses` 与 `max_cli_uses` 默认均为 1。一次成功的新浏览器登录消耗浏览器额度，同一个有效会话继续使用不重复扣减；新 CLI 设备绑定消耗 CLI 额度，已有设备签名续签不重复消费。链接最长有效 90 天，可随时撤销。撤销或过期会使该链接及全部后代失效；撤销也会释放这些管理者正在处理的任务。升级前已有的链接保留权限和有效会话，已有会话计入使用次数。

后台可按 browser / cli 渠道查看登录会话的来源 IP、客户端、设备、创建及最近活动时间，撤销会话并查询登录、链接使用和撤销审计。店长仅能查看和撤销本商品自己的会话及下级链接会话，不能操作上级、同级或全店管理者。会话过期后保留 90 天供审计；撤销处理者最后一个有效会话，会将未完成任务退回商品队列并保留步骤进度。具体接口和约束见[协议文档](protocol.md#商品管理链接)。

管理链接界面还可生成 CLI 专用的机器人接入提示词；其中票据有效 5 分钟，只用于设备绑定，不占用浏览器登录额度。`extore manage` 支持在客户端聚合多个独立商品授权，并通过同一套按商品管理接口操作任务和附件。使用命令见[远程 CLI](cli.md)，协议见[设备授权](protocol.md#cli-设备授权)。

## 远程命令行

0.6.0 及以上提供三种独立入口，首次密码和服务器恢复仍使用本机管理命令：

- [`extore manage`](cli.md)：商品配置、卡密、队列、链接、事件和会话，授权仍限单个商品；多个授权可在客户端聚合。
- [`extore customer`](cli-customer.md)：从私密标准输入验码，按卡密快照填参、上传材料、重提、领取和下载；与管理凭证分开。
- [`extore admin`](cli-owner.md)：店主可使用邮箱密码及已启用的第二因素登录，或用真实 Passkey 批准设备；平台管理员使用 Passkey 批准。设备身份固定到所属店铺或平台，全店操作使用设备私钥签名。设备最多 30 天，会话最多 8 小时，后续可签名续签。

默认列表返回摘要，任务输入、教程和完整配置按需获取。制卡、创建授权等结果保存到 0600 文件；上传不代表已提交或已交付。[AI 提示词](ai-prompts.md)提供可复制的操作说明。完整 CLI 安装要求为 `extore>=0.6.0`，也可在该版本源码目录用 `uv run extore …`。

## 自动处理与领取

自动处理有两种：

- **Webhook**：发送 `redemption.requested`，独立外部服务按任务 ID 去重，再签名回调状态、进度或结构化交付结果。
- **预设处理器**：只能选择 [TokenNotIncluded/extore-processors](https://github.com/TokenNotIncluded/extore-processors) 中的白名单预设。`processors/official` 是固定到主仓库记录提交的 Git 子模块，输入、输出和店铺配置结构由代码定义；店主在本店创建加密配置档案并绑定商品，不能上传程序、指定文件名或 Git 地址。发行卡密会保存配置档案的版本，改配置并更新商品绑定只影响后续发行的卡密；撤销档案会阻止旧版本继续执行。

内置预设 `resource_link` 交付已配置的 HTTPS 资源链接与可选说明；`personalized_text` 根据顾客姓名生成模板文本。预设能从代码初始化空步骤计划，再逐步更新已完成项和一句话进度；已有商家计划不会被替换。升级处理器需审核代码与预设结构后更新主仓库的固定提交。已审核的固定代码仍按 worker 用户权限运行，不是任意恶意代码的沙箱；其他自动化可放在独立的 HTTPS 公网 Webhook 服务。

内容交付可设置重复查看或仅一次领取；服务交付只显示处理结果。顾客可立即销毁完成的交付：删除应用内内容及参数，后续链接无法查看。**这不会撤回已下载的副本、外部平台内容、已经发出的事件或历史备份，也不承诺物理介质的安全擦除。**一次领取的内容在成功返回后即从数据库中移除；网络中断时无法再次领取，应由商家另行处理。

## 认证恢复

平台管理员丢失全部 Passkey 时，只能在服务器恢复首次密码：

```sh
uv run python -m extore.cli reset-auth
```

输入 `RESET` 确认，然后在终端私密输入新密码。平台管理员的 Passkey、登录会话及平台 CLI 设备被撤销，认证挑战被清除；重新进入后台注册新 Passkey。各店账号、Passkey、商品管理设备和业务数据保留。

店主使用 `/account/reset` 发送一次性邮件链接重置密码，已启用 TOTP 时还须提供验证码或恢复码。改密码或重置密码会撤销本店账号会话、店主 CLI 设备及商品处理设备；商品、卡密、任务与管理链接配置保留。设备管理见[商品 CLI](cli.md#配置输出与撤销)、[店主 CLI](cli-owner.md#配置与退出)及[多店与账号](shops.md)。

## 生产运行

配置环境变量（[`.env.example`](../.env.example) 是示例，CLI/worker 不自动读取 `.env`；须由进程管理器加载）：

```sh
export EXTORE_ORIGIN=https://redeem.example.com
export EXTORE_DATA=/var/lib/extore
```

使用反向代理提供 HTTPS。API 与 worker 必须共享同一个数据目录及配置。一个数据库运行一个 worker；API 可运行多个进程，但建议单进程起步。数据目录权限为 0700，部署前 `umask 077`，专用低权限用户运行。数据库、WAL、`issuance.key` 和 `master-secrets.key` 都是敏感文件，必须一并备份。前者保护平台制卡幂等响应，后者保护 SMTP、邮件队列、TOTP 和处理器配置档案；丢失密钥后不能解密既有秘密。

```sh
uv run uvicorn extore.app:app --host 127.0.0.1 --port 8000 --forwarded-allow-ips=127.0.0.1
uv run python -m extore.worker
```

文件上传默认每个文件 20 MiB、每张卡密 100 MiB；反向代理须为两个上传接口放行至少 21 MiB 请求，其余接口仍限制 256 KiB。下载只以附件返回，系统不会打开或执行文件。具体额度和清理方式见下文[文件上传与存储](#文件上传与存储)。

仅信任实际反向代理的地址。限流以 ASGI 的客户端地址为准；不要将可信代理地址设为 `*`。定期监控 worker 和 `/health`；健康检查仅证明 API / SQLite 可读写，不证明 worker 或外部发货平台可用。备份使用 SQLite 在线备份接口或停服务后复制，不能在写入时单独复制主数据库。

也提供 [`Dockerfile`](../Dockerfile) 与 [`compose.yaml`](../compose.yaml)。修改 Compose 的 `EXTORE_ORIGIN` 后，在 HTTPS 反向代理后运行：

```sh
mkdir -p data
chmod 700 data
# 容器默认 uid 10001，须保证其可以写入 data
sudo chown 10001:10001 data
docker compose build
docker compose run --rm -it api python -m extore.cli init
docker compose up -d
```

预设处理器随程序包安装，执行超时 120 秒；输出最多 1 MB，单条结果最多 100 KB。顾客接口不返回处理器配置中的秘密。

未知、超时、崩溃进入“待核实”，默认不能重试。仅明确未交付的失败允许按商品配置重试；商家可以在核实外部结果后放行。下游仍必须以**稳定任务 ID**避免重复发货，无法跨第三方事务承诺绝对只执行一次。

Webhook 须使用 HTTPS 公网地址；DNS 校验后固定连接 IP、保留域名的 TLS 验证，不跟随重定向，不使用环境代理。默认不接入内网地址。最多自动投递 8 次，指数退避；后台可重新投递停止的事件。

## 文件上传与存储

以下环境变量由进程管理器传给 API 和 worker；修改后须重启二者，保持配置一致。容量单位为字节，时间单位为秒。

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `EXTORE_UPLOAD_FILE_BYTES` | `20971520`（20 MiB） | 单文件上限，可降低；当前最高 20 MiB |
| `EXTORE_UPLOAD_JOB_BYTES` | `104857600`（100 MiB） | 每张卡密现存附件总容量 |
| `EXTORE_UPLOAD_JOB_FILES` | `100` | 每张卡密现存附件数量 |
| `EXTORE_UPLOAD_SHOP_BYTES` | `1073741824`（1 GiB） | 新店铺的默认附件额度；平台管理员可分别调整 |
| `EXTORE_UPLOAD_TOTAL_BYTES` | `5368709120`（5 GiB） | 全站保留附件及正在接收文件的逻辑容量 |
| `EXTORE_UPLOAD_DISK_RESERVE_BYTES` | `536870912`（512 MiB） | 实际磁盘剩余空间保留量，另外预留写入副本空间 |
| `EXTORE_UPLOAD_CONCURRENCY` | `4` | 同时上传数量，最高 64 |
| `EXTORE_UPLOAD_TIMEOUT_SECONDS` | `300`（5 分钟） | 一次上传的接收期限 |
| `EXTORE_UPLOAD_DRAFT_TTL_SECONDS` | `86400`（24 小时） | 未绑定附件草稿保留时间 |

配置须满足单文件容量 ≤ 单卡容量 ≤ 全站容量。`GET /api/upload-limits` 只公开 `max_file_bytes`、`max_card_bytes`、`max_card_files`，供顾客表单显示限制；不公开磁盘用量。`GET /api/admin/storage` 对店主只返回本店 `stored_bytes`、`uploading_bytes`、`limit_bytes`、`active_uploads` 和 `upload_concurrency`，不泄露其他店的用量；平台管理员另外能查看全站额度与实际磁盘余量。超出文件或卡密额度返回 413，同时上传过多返回 429，超时返回 408，全站额度或实际磁盘空间不足返回 507。

附件内容保存在 SQLite BLOB 中，上传过程中实际接收的字节占用本店与全站额度，临时文件与数据库使用同一受检查的文件系统。worker 每 60 秒执行一次维护，每轮最多清理 100 条、20 MiB 的过期草稿，积压逐步回收；清理时也检查 WAL 写入所需的磁盘空间。仅删除过期的未绑定草稿和已中断上传占用，不按年龄删除已绑定材料，也保留当前处理中尝试的交付草稿。任务提交、重新提交、一次下载及销毁仍按各自规则绑定或清除内容。

每次维护在清理事务结束后，用独立连接尝试截断 WAL，最多等待 100 毫秒；有活跃读者或锁冲突时跳过，下轮再试。删除 BLOB 会释放逻辑额度，SQLite 可以复用空闲页；数据库文件本身不会因此自动缩小。数据库与 WAL 的物理峰值可能高于附件逻辑容量，512 MiB 保留量也不是可用存储额度。部署时按实际磁盘余量安排备份和维护，不把逻辑清理当作磁盘文件已收缩。

## 记录保留与清理

记录维护默认启用，按店铺分别应用策略。已投递或已取消事件默认保留 30 天；停止投递的 dead 事件保留 90 天；登录与权限审计保留 180 天，最少可设为 90 天。失效管理链接保留 90 天后归档，保留祖先关系墓碑，不能通过删除父链接让后代权限重新有效。待投递事件、有效链接、卡密、任务、附件和业务快照不属于这项清理。

链接列表默认只显示 active，history 查看失效链接，all 也包含归档墓碑。CLI 清理默认预览，明确传 `--apply` 才实际执行：

```sh
extore admin maintenance status
extore admin maintenance cleanup --area events --limit 100
extore admin maintenance cleanup --area events --limit 100 --apply
extore manage links cleanup --product PRODUCT_ID
extore manage links cleanup --product PRODUCT_ID --apply
```

店主只能清理本店记录，平台管理员可显式选 `--shop SHOP_ID`；商品管理链接必须有委派权限且只清理自己的失效后代，不能清理自己的链接、同级或上级。维护分批执行，不因单次预览或 SQLite 删除声称磁盘文件已经缩小。保留策略与接口见[协议文档](protocol.md#记录保留与清理)。

## 开发文档与检查

- [原生 WebMCP 工具、权限与浏览器支持](webmcp.md)
- [接口、状态与完整事件定义](protocol.md)
- [Python 处理器及回调 SDK](python-sdk.md)
- [验收记录与限制](acceptance.md)
- 服务运行后 `/docs` 提供交互式 OpenAPI 文档。

```sh
uv run pytest -q
uv run python -m compileall -q extore processors/official
node --check extore/static/app.js
```

启动时迁移已有数据库，保留商品、卡密、任务和管理链接的权限范围；旧业务归入默认店铺。事件与回调协议仍为版本 1。多店数据与配置相互隔离，商品管理链接只授予指定商品的权限。支付收款与支付订单管理由上游平台负责，本项目不以预设演示结果证明实际付款。

## Arch Linux 原生部署

[`deploy/arch/PKGBUILD`](../deploy/arch/PKGBUILD) 在目标机用锁定依赖构建运行环境，以 pacman 包管理文件；不复制本地虚拟环境。API 与 worker 使用独立 systemd 服务、低权限 `extore` 用户、只写 `/var/lib/extore`。程序包包含固定版本的预设处理器，不从商家指定的目录加载程序。

首次无人值守部署可执行 `sudo -u extore extore-admin bootstrap`：生成随机首次密码，存于 `/var/lib/extore/bootstrap-password.txt`，权限 0600，不输出到日志。由服务器操作人员私密读取并完成 Passkey 注册；注册成功后密码文件也会删除。

```sh
# 服务器终端
sudo -u extore extore-admin reset-auth
sudo -u extore extore-admin integration-key
```

[`deploy/nginx.conf`](../deploy/nginx.conf) 是 `extore.lmm.best` 的反向代理示例，依赖当前服务器的 `msg_safe` 日志格式。通用环境应先定义该格式或使用不含 URL / 查询参数的日志格式。证书须先通过 ACME webroot 签发，再启用 443 配置。

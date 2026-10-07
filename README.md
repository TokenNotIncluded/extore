<p align="center">
  <a href="https://extore.lmm.best">
    <img src="https://raw.githubusercontent.com/TokenNotIncluded/extore/main/extore/static/logo.svg" width="88" height="88" alt="Extore Logo">
  </a>
</p>

<h1 align="center">兑所 · Extore</h1>

<p align="center">
  多店隔离的卡密兑换与交付网站。<br>
  输入卡密，填写信息，领取商品或查看服务结果。
</p>

<p align="center">
  <a href="https://github.com/TokenNotIncluded/extore/actions/workflows/ci.yml"><img src="https://github.com/TokenNotIncluded/extore/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="https://pypi.org/project/extore/"><img src="https://img.shields.io/pypi/v/extore?color=245449" alt="PyPI version"></a>
  <a href="https://github.com/TokenNotIncluded/extore/blob/main/pyproject.toml"><img src="https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&amp;logoColor=white" alt="Python 3.12+"></a>
  <a href="https://github.com/TokenNotIncluded/extore/blob/main/docs/webmcp.md"><img src="https://img.shields.io/badge/WebMCP-native-245449" alt="Native WebMCP"></a>
  <a href="https://github.com/TokenNotIncluded/extore/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="MIT License"></a>
</p>

<p align="center">
  <a href="https://extore.lmm.best">在线站点</a> ·
  <a href="https://github.com/TokenNotIncluded/extore/blob/main/docs/getting-started.md">使用文档</a> ·
  <a href="https://github.com/TokenNotIncluded/extore/issues">问题反馈</a> ·
  <a href="https://donate.lmm.best/?project=extore">赞助项目</a>
</p>

---

Extore 接手支付之后的兑换与交付：验证卡密，将顾客带到对应商品，收集必要信息，再交给商品队列、预设处理器或外部服务。支付与支付订单管理留在上游平台，两边通过制卡接口、Webhook 和签名回调通信。

一个实例可以管理多个独立店铺。平台管理员管理店铺、注册与邮件服务，店主管理自己的商品、队列、配置和附件；商品管理链接继续只授权指定商品。已有单店数据迁移到默认店铺。

## 能做什么

| 能力 | 说明 |
| --- | --- |
| 商品与规格 | 多商品、SKU 档位、参考价与属性；卡密绑定商品和规格。支持私有商品、快速模板、中英文参数名称与 Markdown 教程。 |
| 卡密管理 | 密码学安全随机卡密，按商品、规格、批次和状态跟踪。文本库存可按非空行生成一卡一文本，复制或下载新卡及对应表。原卡密只在发行时显示。 |
| 卡密属性与修改权益 | 规格属性可在制卡时按批次覆盖，并在发行时固定。商品可选择任意整数属性作为交付后的修改额度；同一领取链接提交建议、查看历史版本，技术失败重试不重复扣次数。 |
| 独立商品队列 | 每个商品独立处理，显示真实步骤、消息与排位。获授权的 AI 可用 `next` 原子领取当前任务，`--watch` 在客户端等待，空队列不反复送进模型。 |
| 分步任务流程 | 商品可定义输入、处理、展示和结束节点，引用前一步结果并配置分支与超时。先准备任务，再逐卡明确开始；已有卡密固定发行时的流程与处理配置。 |
| 灵活交付 | 文本、链接、文件、图片与有序图片集合，支持下拉选项和是／否字段，也可只返回服务状态。一次领取、重复查看、需要重试、附原因的拒绝及主动销毁沿用同一套规则。 |
| 批量兑换与路由 | 网页与 CLI 校验多张卡密，按商品、规格及兼容的冻结输入分组；坏码不挡有效卡，附件每卡独立。签名路由卡先本地验签，再交给固定发行站。 |
| 权限与认证 | 平台管理员使用多个 Passkey；店主可用邮箱密码、可选 TOTP 或多个 Passkey 登录。注册默认关闭，由平台管理员邀请店主。商品链接默认 1 次浏览器登录与 1 个 CLI 绑定，支持委派和会话审计。 |
| 自动化与二次开发 | 开源商品处理器、店铺加密配置、只写秘密与冻结运行限制；Python SDK、兼容的 Webhook v1 和按节点授权的私有 Worker v2，原生 WebMCP 工具按页面、身份与权限动态提供。 |
| 命令行操作 | `manage` 管理商品授权，`customer` 兑换与领取，`admin` 用固定账号的设备管理本店或平台；默认精简输出，凭证保存到私密文件。AI 主动申请单商品或当前整店队列权限，店长用设备码审批，可追加权限、集中审计与撤销。 |
| 界面与存储 | SQLite 持久化任务、事件和投递重试；单文件、单卡、单店与全站附件额度，磁盘余量保护；撕纸与分段虚线界面，明暗和语言默认自动。 |

顾客上传的是兑换材料，管理者上传的是交付文件。预设处理器来自审核后固定版本的[开源子模块](https://github.com/TokenNotIncluded/extore-processors)，商家在预设中选择并填写配置。浏览器 AI 需要原生 WebMCP 支持及实际授权，具体兼容性见[工具文档](https://github.com/TokenNotIncluded/extore/blob/main/docs/webmcp.md#浏览器兼容性)。

## 兑换流程

```text
卡密 → 对应商品与规格 → 填写参数 / 分步提交 → 商品队列或处理器 → 领取内容 / 查看结果
```

1. 商家创建商品，配置输入、输出、处理方式和查看规则，按规格发行卡密。
2. 顾客输入有效卡密。非公开商品在验证成功后才显示。
3. 提交参数后创建唯一任务，重复提交返回同一个任务。
4. 队列管理者、预设处理器或外部平台完成任务，顾客通过领取链接查看进度和结果。

重复领取的内容商品可以配置交付后的修改权益，例如把自定义属性 `included_revisions` 设为各规格的 0、1、3。服务端接受修改请求时原子扣一次额度，同一请求重发不会重复扣减；修改排队或处理中，顾客仍能领取之前成功的版本。修改权益与技术重试分开，主动销毁会清除所有版本。

[进度看板](https://github.com/TokenNotIncluded/extore/blob/main/docs/progress-board.md)把店铺作为工厂、商品流水线作为电子车间，汇总工人和任务的实际进度，不读取需求或交付内容。管理者可编辑工厂和车间标语，AI 领取任务时会读取当前提示词。可用独立的 `queue.monitor` 权限授权进度观察者，网页、CLI 和 WebMCP 使用同一个只读接口。

队列商品可修改未来任务的输入输出，已有任务保留各自的结构快照。自动商品发行卡密后锁定处理器和输入输出结构。SKU 的 `price` 字段是参考价，仅供外部商城配置参考。Extore 只负责兑换与交付，不收款；实际售价、收款和销售库存由商家在上游平台管理。

可选的[任务流程](https://github.com/TokenNotIncluded/extore/blob/main/docs/task-flow.md)把顾客输入、处理和展示串起来。验证或批量准备不会替顾客开始计时；顾客只看到当前允许的题目和明确展示的结果。流程在发行卡密时固定，旧卡没有流程快照时继续走原来的兑换方式。短期敏感输入有有效期，只传给当前获授权的处理节点，不能放到展示、普通事件或最终交付中。

[一卡一文本](https://github.com/TokenNotIncluded/extore/blob/main/docs/text-stock.md)适合已有交付内容的库存；[签名兑换路由](https://github.com/TokenNotIncluded/extore/blob/main/docs/proxy-routing.md)适合多个 Extore 站点共用入口。普通旧卡密不会被拿去逐个试探其他站点，入口站后端不接收下游路由卡密。

需要重试时，处理者须说明原因并选择「修改后重提」或「原资料重试」；外部故障也能作为原因。拒绝处理会禁用卡密。原任务、规格和步骤计划保留，重新开始后进度归零。

## 快速运行

需要 **Python 3.12+、[uv](https://docs.astral.sh/uv/) 和 Linux**。完整环境、部署和备份说明见[运行指南](https://github.com/TokenNotIncluded/extore/blob/main/docs/getting-started.md)。内置自动处理器还需要系统安装 bubblewrap 0.12 及以上和 libseccomp；Python 包安装不会提供这些依赖。Arch Linux 可执行：

```sh
sudo pacman -S --needed bubblewrap libseccomp
```

从 [PyPI](https://pypi.org/project/extore/) 安装后，可直接使用 `extore` 命令：

```sh
uv tool install extore
export EXTORE_DATA="$HOME/.local/share/extore"
extore init
extore serve
```

另开一个终端，设置相同的 `EXTORE_DATA` 后运行 `extore worker`。`extore --help` 查看全部命令，`extore --version` 查看版本。生产环境还须配置 `EXTORE_ORIGIN` 并提供 HTTPS，见运行指南。

也可以从源码运行：

```sh
git clone --recurse-submodules https://github.com/TokenNotIncluded/extore.git
cd extore
uv sync --frozen
uv run extore init
uv run extore serve
```

另开一个终端，在项目目录启动 worker，处理自动发货和 Webhook 投递：

```sh
uv run extore worker
```

打开 <http://localhost:8000>。在 2.5 秒内点击左上角 Logo 5 次，或直接访问 `/admin`。平台管理员首次使用初始化密码注册 Passkey，之后关闭这套首次密码登录；店主账号使用独立的邮箱密码和可选 TOTP，也可添加多个 Passkey。平台管理员先配置 SMTP，再邀请店主。邀请、邮箱确认和密码重置邮件采用统一响应式排版，并保留纯文本版本；详见[多店与账号](https://github.com/TokenNotIncluded/extore/blob/main/docs/shops.md)。

<details>
<summary>首次登录、认证恢复与运行边界</summary>

- Passkey 需要 HTTPS 或 localhost；部署前确定固定域名。平台管理员首次密码会话只能注册 Passkey，不能管理商品。
- 平台管理员丢失全部 Passkey 时，在服务器执行 `uv run extore reset-auth` 恢复首次密码登录；店主使用邮件重置自己的密码，开启 TOTP 时还须提供第二因素或恢复码。两者均保留业务数据。详见[认证恢复](https://github.com/TokenNotIncluded/extore/blob/main/docs/getting-started.md#认证恢复)。
- API 与 worker 共享数据目录和配置，一个数据库只运行一个 worker。数据目录、数据库、`issuance.key` 和 `master-secrets.key` 都要妥善保护和备份。详见[生产运行](https://github.com/TokenNotIncluded/extore/blob/main/docs/getting-started.md#生产运行)。
- 领取链接是凭证。一次领取会消耗查看机会；销毁不能撤回已下载的副本、外部内容或历史备份。未知、超时或崩溃的交付默认等待核实，下游须用稳定任务 ID 去重。详见[自动处理与领取](https://github.com/TokenNotIncluded/extore/blob/main/docs/getting-started.md#自动处理与领取)。

</details>

## 命令行与 AI 接入

完整 CLI 要求 **0.6.0 及以上**；也可在该版本源码目录用 `uv run extore …` 执行。安装与完整操作见 [CLI 指南](https://github.com/TokenNotIncluded/extore/blob/main/docs/cli.md)。

```sh
# 商品授权：CLI 显示网址和短码，由你在浏览器确认权限
extore manage login --device-code --origin https://extore.example.com --product PRODUCT_ID
extore manage queues --all

# 原子领取；持续等待由 CLI 完成，不反复把空队列交给模型
extore manage next --all --origin https://extore.example.com --watch --limit 1

# 本店当前全部队列商品：先申请，再由店长核对商品清单和权限
extore manage login --device-code --origin https://extore.example.com --shop SHOP_ID --pipelines-all --no-wait

# 顾客：卡密从私密文件标准输入读取
extore customer exchange --origin https://extore.example.com --codes-stdin < /path/to/private-codes.txt

# 店主：首次在浏览器用真实 Passkey 批准此设备
extore admin login --origin https://extore.example.com

# 已有邮箱账号的店主：隐藏输入密码及已开启的第二因素
extore admin login --origin https://extore.example.com --email owner@example.com
```

本店队列授权只覆盖批准清单内的队列商品，之后新建的商品需要再次申请。AI 可提出追加商品或权限，店长可减少申请范围再批准；拒绝、取消或到期的申请不会改变原授权。成功追加保留原任务处理者身份；每次写入仍使用一个商品的独立授权，客户端可以聚合查看多个队列。后台会话管理可查看、撤销整份流水线授权并释放其处理中任务。店主设备授权最多 30 天，8 小时会话由本机私钥续签；后续 JSON 写操作另用一次性设备签名。列表默认摘要，任务详情和教程按需读取，制卡与新授权链接保存为 0600 文件。

SMTP 凭据、TOTP 密钥与店铺处理器配置加密保存。店主可以回读、编辑明确声明为普通文本的交付模板与说明；账号密钥和未声明类型的配置只返回是否已设置。配置档案还支持普通工作流变量、只写秘密与运行资源上限，网页和店主 CLI 均可管理。已有卡密冻结整份档案修订，修改变量、秘密或资源上限不改变旧卡；处理器通过只读环境取得对应版本。[配置与工作流说明](https://github.com/TokenNotIncluded/extore/blob/main/docs/shops.md#工作流变量秘密与运行限制)

内置商品处理器采用固定代码的离线运行环境，需要 Linux、bubblewrap 0.12 及以上（`bwrap`）、libseccomp 和可用的内核命名空间。当前只支持 stdin/stdout 文本与资源链接预设，执行时间、进程地址空间、CPU 与输出上限；禁止联网、fork、外部程序及创建文件。隔离不可用时拒绝执行，不降级为宿主直接运行。复杂文档和 PPT 仍由外部 AI 通过 CLI 队列处理并上传文件。商家不能增加命令、挂载或联网能力；处理器可读取授予的秘密，代码仍须审核，秘密不会自动进入交付模板。付款适配和受控支付服务尚未实现，现有预设不代表已完成真实付款。

设备码登录要求 **0.7.0 及以上**。网页可一键复制不含凭证的商品机器人提示词：机器人申请设备码，你在 `/cli/device` 输入短码，核对商品、权限、设备指纹与期限，再明确批准。主动商品申请不要求先创建管理链接；本店流水线只给队列权限，商品管理的额外权限须单独明确申请。既有商品管理链接通过 `--existing-link` 设备码流程绑定，浏览器与 CLI 绑定次数独立。[文档里的提示词](https://github.com/TokenNotIncluded/extore/blob/main/docs/ai-prompts.md)也可直接复制，配合已授权的 CLI 使用。上传只保存材料，提交或交付需明确执行下一步；持续运行机器人由接入方安排。

任务流程、富类型字段、文本库存、签名兑换路由和 `next --watch` 是 **0.8.0** 的接口能力。`next` 返回当前任务、当前节点和应使用的授权；断线后先恢复同一请求，不能直接另领一单。详见[AI 队列处理](https://github.com/TokenNotIncluded/extore/blob/main/docs/automation-cli.md)。Extore 不内置持续运行的通用 AI 模型；复杂文档或 PPT 的制作质量、持续运行、外部付款和真实提供商交付需要分别验收，接口可用不等于这些工作已经完成。

## 文档

| 文档 | 内容 |
| --- | --- |
| [快速入门与运行指南](https://github.com/TokenNotIncluded/extore/blob/main/docs/getting-started.md) | 本地运行、管理权限、认证恢复、生产配置、Docker 与 Arch Linux 部署 |
| [接口与事件协议](https://github.com/TokenNotIncluded/extore/blob/main/docs/protocol.md) | 商品、SKU、卡密、队列、附件、完整事件定义与签名回调 |
| [CLI 指南](https://github.com/TokenNotIncluded/extore/blob/main/docs/cli.md) | 商品完整管理、卡密、授权、队列与私密输出 |
| [顾客 CLI](https://github.com/TokenNotIncluded/extore/blob/main/docs/cli-customer.md) · [店主 CLI](https://github.com/TokenNotIncluded/extore/blob/main/docs/cli-owner.md) | 批量兑换、材料与交付；Passkey 设备批准和全店操作 |
| [多店与账号](https://github.com/TokenNotIncluded/extore/blob/main/docs/shops.md) | 平台与店主边界、邮箱邀请、TOTP、SMTP、加密处理器配置与存储额度 |
| [可复制 AI 提示词](https://github.com/TokenNotIncluded/extore/blob/main/docs/ai-prompts.md) | 商品处理、店主运营与顾客领取的操作模板 |
| [任务流程](https://github.com/TokenNotIncluded/extore/blob/main/docs/task-flow.md) · [一卡一文本](https://github.com/TokenNotIncluded/extore/blob/main/docs/text-stock.md) | 冻结的分步流程、当前题目、计时与敏感输入；按规格导入交付库存 |
| [AI 队列处理](https://github.com/TokenNotIncluded/extore/blob/main/docs/automation-cli.md) | 原子领取、等待、断线恢复与按当前节点交付 |
| [兑换路由协议](https://github.com/TokenNotIncluded/extore/blob/main/docs/proxy-routing.md) · [路由配置](https://github.com/TokenNotIncluded/extore/blob/main/docs/proxy-config.md) | 本地验签、公开路由配置和固定发行站 |
| [原生 WebMCP](https://github.com/TokenNotIncluded/extore/blob/main/docs/webmcp.md) | 原生工具、浏览器支持、权限范围与确认要求 |
| [Python SDK](https://github.com/TokenNotIncluded/extore/blob/main/docs/python-sdk.md) | 预设处理器协议、任务结果、外部验签与回调示例 |
| [私有 Worker v2](https://github.com/TokenNotIncluded/extore/blob/main/docs/private-worker.md) · [SDK v2](https://github.com/TokenNotIncluded/extore/blob/main/docs/python-sdk-v2.md) | 按店铺、任务、尝试和节点签名派发，结果与附件幂等回调 |
| [0.8.0 更新说明](https://github.com/TokenNotIncluded/extore/blob/main/docs/releases/0.8.0.md) | 本轮能力、兼容性与仍需单独验收的边界 |
| [0.9.0 更新说明](https://github.com/TokenNotIncluded/extore/blob/main/docs/releases/0.9.0.md) | 只读进度看板、工人身份与卡通形象、CLI 网络代理和更清晰的队列页面 |
| [0.9.1 更新说明](https://github.com/TokenNotIncluded/extore/blob/main/docs/releases/0.9.1.md) | 工厂和车间标语、卡密批次文件夹、发行标识替换与清理、首页统一兑换入口 |
| [0.10.0 更新说明](https://github.com/TokenNotIncluded/extore/blob/main/docs/releases/0.10.0.md) | 自定义卡密属性、交付后修改权益、历史版本领取与轮次保护 |
| [0.8.2 更新说明](https://github.com/TokenNotIncluded/extore/blob/main/docs/releases/0.8.2.md) | 清空回收站与彻底删除商品；保留已有卡密、任务和交付 |
| [0.8.1 更新说明](https://github.com/TokenNotIncluded/extore/blob/main/docs/releases/0.8.1.md) | 商品删除、回收站恢复与独立删除权限；保留旧卡密和任务 |
| [验收记录](https://github.com/TokenNotIncluded/extore/blob/main/docs/acceptance.md) | 已记录的验证结果、对接边界与限制 |
| [产品定义](https://github.com/TokenNotIncluded/extore/blob/main/PRODUCT.md) · [设计说明](https://github.com/TokenNotIncluded/extore/blob/main/DESIGN.md) | 项目范围、交互和视觉原则 |

服务运行后，`/docs` 提供交互式 OpenAPI 文档。普通事件和既有回调保持 **v1**；流程私有 Worker 使用独立的 **v2** 签名与节点范围。

## 贡献

欢迎通过 [Issues](https://github.com/TokenNotIncluded/extore/issues) 提交可复现的问题或功能建议，也欢迎提交 Pull Request。修改前请阅读[产品定义](https://github.com/TokenNotIncluded/extore/blob/main/PRODUCT.md)与[设计说明](https://github.com/TokenNotIncluded/extore/blob/main/DESIGN.md)，保持店铺与商品权限隔离和既有视觉语言。

常用检查如下；完整检查项以 [CI 工作流](https://github.com/TokenNotIncluded/extore/blob/main/.github/workflows/ci.yml)为准：

```sh
uv run ruff check extore scripts tests
uv run ruff format --check extore scripts tests
uv run pytest -q
node --test tests/*.test.cjs
```

Pull Request 请说明行为变化和验证结果。内置预设处理器在[独立仓库](https://github.com/TokenNotIncluded/extore-processors)维护，主项目通过子模块固定版本。反馈或示例中请删除卡密、管理链接、领取凭证、密钥和顾客资料。

## 赞助

如果 Extore 帮到了你，可以通过 [donate.lmm.best](https://donate.lmm.best/?project=extore) 支持项目维护。

<p align="center">
  <a href="https://donate.lmm.best/?project=extore">
    <img src="https://donate.lmm.best/badge.svg?project=extore&amp;currency=CNY&amp;lang=zh-CN&amp;period=all&amp;layout=compact&amp;theme=dark&amp;width=440&amp;title=Extore" width="440" alt="Extore 项目累计赞助">
  </a>
</p>

## 许可证

Extore 使用 [MIT License](https://github.com/TokenNotIncluded/extore/blob/main/LICENSE)。预设处理器子模块的许可见[其仓库](https://github.com/TokenNotIncluded/extore-processors/blob/main/LICENSE)。

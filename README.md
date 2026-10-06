<p align="center">
  <a href="https://extore.lmm.best">
    <img src="https://raw.githubusercontent.com/TokenNotIncluded/extore/main/extore/static/logo.svg" width="88" height="88" alt="Extore Logo">
  </a>
</p>

<h1 align="center">兑所 · Extore</h1>

<p align="center">
  一个商家的卡密兑换与交付网站。<br>
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

Extore 接手支付之后的兑换与交付：验证卡密，将顾客带到对应商品，收集必要信息，再交给商品队列、官方处理器或外部服务。支付与支付订单管理留在上游平台，两边通过制卡接口、Webhook 和签名回调通信。

## 能做什么

| 能力 | 说明 |
| --- | --- |
| 商品与规格 | 多商品、SKU 档位、价格与属性；卡密绑定商品和规格。支持私有商品、快速模板、中英文参数名称与 Markdown 教程。 |
| 卡密管理 | 使用密码学安全随机数生成卡密，批量发行、复制与下载；按商品、规格、批次和状态跟踪库存与生命周期。原卡密只在发行时显示。 |
| 独立商品队列 | 每个商品独立领取、筛选和批处理任务，显示真实步骤、处理消息与商品内排位。人或获授权的 AI 使用同一套接口。 |
| 灵活交付 | 链接、文本、账户信息或文件，也可只返回服务状态。支持一次领取、重复查看、明确失败后的重试，以及顾客主动销毁内容。 |
| 权限与认证 | 商家后台支持多个 Passkey。商品管理链接默认可登录 1 次，按权限授权、向下委派，并记录会话与操作审计。 |
| 自动化与二次开发 | 官方开源预设处理器、Python SDK、签名事件与回调；原生定义 **47 个 WebMCP 工具**，按页面、身份与权限动态提供。 |
| 界面与存储 | SQLite 持久化任务、事件和投递重试；撕纸与分段虚线界面，灰黑暗色主题、中英文语言，默认跟随系统与浏览器。 |

顾客上传的是兑换材料，管理者上传的是交付文件。官方处理器来自审核后固定版本的[开源子模块](https://github.com/TokenNotIncluded/extore-processors)，商家在预设中选择并填写配置。浏览器 AI 需要原生 WebMCP 支持及实际授权，具体兼容性见[工具文档](https://github.com/TokenNotIncluded/extore/blob/main/docs/webmcp.md#浏览器兼容性)。

## 兑换流程

```text
卡密 → 对应商品与规格 → 填写参数 / 上传材料 → 处理任务 → 领取内容 / 查看结果
```

1. 商家创建商品，配置输入、输出、处理方式和查看规则，按规格发行卡密。
2. 顾客输入有效卡密。非公开商品在验证成功后才显示。
3. 提交参数后创建唯一任务，重复提交返回同一个任务。
4. 队列管理者、官方处理器或外部平台完成任务，顾客通过领取链接查看进度和结果。

队列商品可修改未来任务的输入输出，已有任务保留各自的结构快照。自动商品发行卡密后锁定处理器和输入输出结构。SKU 价格用于商品定义，上游平台负责实际定价、收款和销售库存。

## 快速运行

从 [PyPI](https://pypi.org/project/extore/) 安装后，可直接使用 `extore` 命令：

```sh
uv tool install extore
export EXTORE_DATA="$HOME/.local/share/extore"
extore init
extore serve
```

另开一个终端，设置相同的 `EXTORE_DATA` 后运行 `extore worker`。`extore --help` 查看全部命令，`extore --version` 查看版本。生产环境还须配置 `EXTORE_ORIGIN` 并提供 HTTPS，见运行指南。

也可以从源码运行：

需要 **Python 3.12+、[uv](https://docs.astral.sh/uv/) 和 Linux**。完整环境、部署和备份说明见[运行指南](https://github.com/TokenNotIncluded/extore/blob/main/docs/getting-started.md)。

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

打开 <http://localhost:8000>。在 2.5 秒内点击左上角 Logo 5 次，或直接访问 `/admin`。首次使用初始化密码注册 Passkey，注册成功后密码登录立即禁用；后续可添加多个 Passkey。

<details>
<summary>首次登录、认证恢复与运行边界</summary>

- Passkey 需要 HTTPS 或 localhost；部署前确定固定域名。首次密码会话只能注册 Passkey，不能管理商品。
- 丢失全部 Passkey 时，须在服务器终端执行 `uv run python -m extore.cli reset-auth`。它撤销全部 Passkey 和登录会话，保留商品、卡密与任务；商品管理链接须另行撤销。详见[认证恢复](https://github.com/TokenNotIncluded/extore/blob/main/docs/getting-started.md#认证恢复)。
- API 与 worker 共享数据目录和配置，一个数据库只运行一个 worker。数据目录、数据库和 `issuance.key` 都要妥善保护和备份。详见[生产运行](https://github.com/TokenNotIncluded/extore/blob/main/docs/getting-started.md#生产运行)。
- 领取链接是凭证。一次领取会消耗查看机会；销毁不能撤回已下载的副本、外部内容或历史备份。未知、超时或崩溃的交付默认等待核实，下游须用稳定任务 ID 去重。详见[自动处理与领取](https://github.com/TokenNotIncluded/extore/blob/main/docs/getting-started.md#自动处理与领取)。

</details>

## 文档

| 文档 | 内容 |
| --- | --- |
| [快速入门与运行指南](https://github.com/TokenNotIncluded/extore/blob/main/docs/getting-started.md) | 本地运行、管理权限、认证恢复、生产配置、Docker 与 Arch Linux 部署 |
| [接口与事件协议](https://github.com/TokenNotIncluded/extore/blob/main/docs/protocol.md) | 商品、SKU、卡密、队列、附件、完整事件定义与签名回调 |
| [原生 WebMCP](https://github.com/TokenNotIncluded/extore/blob/main/docs/webmcp.md) | 47 个工具、浏览器支持、权限范围与确认要求 |
| [Python SDK](https://github.com/TokenNotIncluded/extore/blob/main/docs/python-sdk.md) | 官方处理器协议、任务结果、外部验签与回调示例 |
| [验收记录](https://github.com/TokenNotIncluded/extore/blob/main/docs/acceptance.md) | 已记录的验证结果、对接边界与限制 |
| [产品定义](https://github.com/TokenNotIncluded/extore/blob/main/PRODUCT.md) · [设计说明](https://github.com/TokenNotIncluded/extore/blob/main/DESIGN.md) | 项目范围、交互和视觉原则 |

服务运行后，`/docs` 提供交互式 OpenAPI 文档。事件与回调协议版本为 **v1**。

## 贡献

欢迎通过 [Issues](https://github.com/TokenNotIncluded/extore/issues) 提交可复现的问题或功能建议，也欢迎提交 Pull Request。修改前请阅读[产品定义](https://github.com/TokenNotIncluded/extore/blob/main/PRODUCT.md)与[设计说明](https://github.com/TokenNotIncluded/extore/blob/main/DESIGN.md)，保持单商家范围、商品权限隔离和既有视觉语言。

常用检查如下；完整检查项以 [CI 工作流](https://github.com/TokenNotIncluded/extore/blob/main/.github/workflows/ci.yml)为准：

```sh
uv run ruff check extore scripts tests
uv run ruff format --check extore scripts tests
uv run pytest -q
node --test tests/*.test.cjs
```

Pull Request 请说明行为变化和验证结果。官方预设处理器在[独立仓库](https://github.com/TokenNotIncluded/extore-processors)维护，主项目通过子模块固定版本。反馈或示例中请删除卡密、管理链接、领取凭证、密钥和顾客资料。

## 赞助

如果 Extore 帮到了你，可以通过 [donate.lmm.best](https://donate.lmm.best/?project=extore) 支持项目维护。

<p align="center">
  <a href="https://donate.lmm.best/?project=extore">
    <img src="https://donate.lmm.best/badge.svg?project=extore&amp;currency=CNY&amp;lang=zh-CN&amp;period=all&amp;layout=compact&amp;theme=dark&amp;width=440&amp;title=Extore" width="440" alt="Extore 项目累计赞助">
  </a>
</p>

## 许可证

Extore 使用 [MIT License](https://github.com/TokenNotIncluded/extore/blob/main/LICENSE)。官方处理器子模块的许可见[其仓库](https://github.com/TokenNotIncluded/extore-processors/blob/main/LICENSE)。

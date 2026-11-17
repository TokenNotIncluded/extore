# Extore 1.0 Rust 迁移与 LMM 商店接入

Extore 的 HTTP 服务、认证、商城协议、队列和后台任务由 Rust 程序运行。Axum 接收请求，SQLite 保存业务状态，原生 worker 处理任务、通知和邮件；已有商品处理器通过审核过的 Python SDK 在独立的离线沙箱中运行。

## 运行范围

| 部分 | 运行方式 | 保留的行为 |
| --- | --- | --- |
| HTTP 与静态页面 | Rust / Axum | 既有 HTTP 数据契约、严格校验、上传限制、来源校验和安全响应头。 |
| 认证与授权 | Rust | 平台 Passkey、店主密码与 TOTP、CLI 签名、管理链接的逐级权限和撤销。 |
| 商品与卡密 | Rust / SQLite | SKU 快照、库存、批次、文本库存、修改权益、回收站和已受理任务。 |
| 兑换与任务流 | Rust | 一次领取、批量兑换、重试、修改、流程快照、输入有效期和签名回调。 |
| 后台处理 | Rust worker | 数据库独占 worker 锁、租约、通知重试、邮件和附件维护。 |
| 已审核商品处理器 | Python 标准库 SDK / bubblewrap / seccomp | 原有处理器与加密配置。沙箱无网络，无法读取站点数据目录，执行时间、内存和输出有上限。 |
| 客户端 SDK 与 CLI | 保留 Python 客户端 | 通过公开 HTTP 协议使用 Rust 服务；开发与兼容测试仍使用 `uv`。 |

生产安装包使用 `/usr/lib/extore/bin/extore`，系统入口为 `/usr/bin/extore`。API 和 worker 的 systemd 单元均启动该原生程序。包中保留静态资源、审核过的处理器和必要 SDK 文件；Python Web 服务及其虚拟环境不进入新运行包。

服务端命令为 `init`、`bootstrap`、`serve`、`worker`、`reset-auth`、`integration-key` 和 `version`。`bootstrap` 保留为初始化兼容入口；`integration-key` 仅在操作人员明确执行时轮换平台制卡密钥。仓库中的 `uv run extore manage …`、`uv run extore commerce …` 等命令仍属于客户端工具；它们与服务端二进制的入口分别使用。

## 已有数据如何迁移

本次生产迁移从 schema 22 升级到 schema 23。原生 `init` 在一个 `BEGIN IMMEDIATE` 事务中添加 `commerce_bindings` 表、`commerce_bindings_shop` 索引并推进版本号。它不重建原表，不重新生成业务标识，也不批量改写已有卡密、任务、凭据或密文。

已有主密钥必须与数据库指纹匹配。缺失或不匹配的 `master-secrets.key` 会阻止启动；历史发行恢复记录所需的 `issuance.key` 也须保留。新空库可以初始化到 schema 23，其他未知旧版本会被拒绝，应先使用对应旧版的正式迁移路径。

| 输入状态 | 原生初始化结果 |
| --- | --- |
| 新空库 | 创建 schema 23 和私密初始化材料。 |
| schema 22，原密钥有效 | 只执行已定义的两项新增结构及版本升级。 |
| schema 22，同名新结构冲突 | 整个迁移事务回滚。 |
| schema 23 | 验证现有状态并按现有配置启动。 |
| 未知旧版本或密钥不匹配 | 停止并返回错误。 |

迁移验收使用独立恢复的数据副本，比较迁移前后每张原表的结构、行和关键文件哈希，再检查 SQLite 完整性及外键。生产备份保存在开发机的 `Backups/servers/` 下；服务器临时快照只用于传输，完成校验后清理。

部署沿用现有 `prepare → snapshot → backup acknowledgement → activate → verify → confirm` 流程。回滚恢复旧程序、配置和包状态，保留切换后产生的业务数据。只有经核实兼容的 schema 差异才允许程序回滚；不能用旧数据库覆盖已经受理的新交易或任务。

## 与 LMM 新架构商店的对应关系

商家从 LMM 的 `/store/manage` 发起 Extore 连接，浏览器到 Extore 选择店铺、商品和授权范围，再返回 LMM。内置 `lmm-best-store` 客户端登记了 `https://native.api.lmm.best/store/manage` 和已有域名的回调。授权码使用 S256 PKCE；刷新令牌轮换，旧令牌重放会撤销对应授权。

新商店连接申请 `products.read` 和 `marketplace.sync`。前者读取商品资料，后者允许把商家明确授权的私有 SKU 模板映射到 LMM 后端。LMM 的实际同步函数消费 Extore 的 `extore.product-listing.v1` 契约，保留来源连接、商品和规格标识，并将首次导入内容保存为草稿。LMM 按当前商店策略固定公开可见、余额支付、登录取货和顺序发货；商品继续经过既有的草稿与发布流程。

| Extore 内容 | LMM 行为 |
| --- | --- |
| 商品、规格、说明和图片 | 导入或更新同一个来源商品，保留本地商品和规格身份。 |
| `price_quota` | 按整数 Credits 价格处理，避免经过浮点价格换算。 |
| 私有发货模板与 `fixed_content` | 仅经 `marketplace.sync` 授权进入 LMM 后端的发货配置。 |
| `sync_revision` | 判断需要更新的商店配置，包括私有发货内容变化。 |
| `card_summary` | 更新来源库存摘要；库存变化本身不改变 `sync_revision`。 |
| 批量同步 | 每个连接只拉取一次目录，各来源商品分别报告成功或失败。 |
| 到期或需要重新授权 | 显示连接状态，并使用新的授权流程重新连接。 |

本次 LMM 商店连接没有申请 `cards.issue`。自动发行卡密需要独立的规格额度授权；同步商品资料不会获得补卡权限。支付、余额、购买限制和销售订单继续由 LMM 的既有商店与 Rust core 处理。

## 验证方法与证据

验证分为 Rust 单元测试、实际 HTTP 兼容测试、跨项目联调和生产恢复演练。`tests/native_http_plugin.py` 让原有测试客户端通过 loopback HTTP 连接独立 Rust 进程；Python 仅用于准备测试数据和检查数据库，没有运行 Python ASGI 服务。

| 检查 | 证据入口 | 证明范围 |
| --- | --- | --- |
| 密钥与认证兼容、事务和流程边界 | `cargo test --locked` | 历史 AES-GCM、COSE Passkey、签名重放、权限与冻结数据。 |
| 原有商城和商品协议 | `scripts/check_native_contracts.py` | OAuth、目录、库存、卡密、回收站、管理链接、附件、配置和队列的实际 HTTP 行为。 |
| 原生长轮询 | `tests/test_native_automation_poll.py` | 等待期间释放写锁，及时处理新任务、撤销和过期。 |
| 原生 Worker V2 | `tests/test_native_private_worker.py` | 实际输入过期、处理截止时间、幂等确认及签名文件传输。 |
| 原生自动处理器 | `tests/test_native_processor_worker.py` | Rust worker 通过真实离线沙箱执行审核过的 SDK，并拒绝第二个 worker。 |
| LMM 跨项目联调 | `tests/native_lmm_sdk_probe.py`、`tests/native_lmm_sdk_probe.go` | 冻结版本 Go SDK 与实际 `syncExtoreListing` 直接消费 Rust 服务。 |
| 数据迁移 | `src/catalog/migration.rs` 与部署恢复报告 | 原有结构和数据保全、冲突回滚、原密钥可用。 |

跨项目探针使用临时数据库和 HTTPS 桥，校验冻结源码文件哈希后运行真实 LMM 同步函数。它覆盖 Native 回调、PKCE、目录、私有模板、整数售价、来源身份、库存与配置版本的区别、刷新和撤销，不创建生产账户或订单。

部分旧测试通过修改 Python 进程的时钟、常量、内部函数或 spy 来观察同进程服务。这些修改不会作用于独立 Rust 程序。原生检查明确列出此类排除项，并用真实 HTTP、实际经过的时间或测试数据库状态来验证对应行为；未把旧 Python 内部测试的通过当成 Rust 兼容证明。

## 验收复核

| 问题 | 核对方式 |
| --- | --- |
| Rust 覆盖哪些执行路径？ | 以 systemd 启动命令、安装包清单、`/health` 的 runtime 和原生进程为准。 |
| 文档中的命令能否复现？ | 服务端使用 Cargo 构建的二进制；客户端与兼容测试命令明确使用 `uv`。 |
| 是否声称未经测量的性能提升？ | 没有。当前证据证明行为与迁移兼容，不提供未做的吞吐量对比。 |
| 是否只验证理想路径？ | 覆盖撤销、重放、过期、配额、跨店越权、重复领取和迁移失败。 |
| 数据或权限是否因接入而扩大？ | 保留原数据与密钥，商店只读取获批商品；补卡、支付和销售权限分别授权。 |

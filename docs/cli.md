# 命令行指南

[返回项目首页](../README.md) · [运行指南](getting-started.md) · [顾客 CLI](cli-customer.md) · [店主 CLI](cli-owner.md) · [AI 接入提示词](ai-prompts.md) · [接口协议](protocol.md)

完整 CLI 要求 **0.6.0 及以上**；也可在该版本源码目录用 `uv run extore …` 运行。查看本机版本可用 `extore --version`。

| 入口 | 用途 | 授权 |
| --- | --- | --- |
| `extore manage` | 商品配置、卡密、队列、附件、管理链接、事件与会话 | 单个商品管理链接绑定的设备，可保存多个独立授权 |
| [`extore customer`](cli-customer.md) | 验码、填参、上传材料、跟踪状态、领取和销毁 | 卡密或已有领取链接，不使用商家权限 |
| [`extore admin`](cli-owner.md) | 本店商品、队列、卡密、安全；平台账号可维护店铺和 SMTP | 固定到账号的 CLI 设备；店主支持邮箱密码及第二因素或真实 Passkey 批准，平台管理员使用 Passkey |
| `extore init / serve / worker …` | 本机初始化、运行与服务器恢复 | 服务器用户，见[运行指南](getting-started.md) |

下面介绍 `manage`。一个授权只管理一个商品；客户端可以保存多个授权并聚合查看，每次写入仍使用其中一个授权，不合并权限。示例中的大写 ID 与路径是占位符，请用当前操作返回的实际值替换。

## 安装与登录

需要 Linux、Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)：

```sh
uv tool install --upgrade 'extore>=0.6.0'
extore manage --help
extore manage login
```

最后一条命令会隐藏输入，粘贴完整管理链接即可。需要自动接入时，把链接从标准输入交给客户端：

```sh
extore manage login --link-stdin < /path/to/private-link.txt
```

支持完整的 `/staff#…` 商品管理链接或 `/cli#…` 专用票据链接；不接受裸 token，也不通过命令行参数传递链接。生产服务器必须使用 HTTPS，本机回环地址可用 HTTP。`--client-name` 可设置后台审计中显示的设备名称。

登录时客户端生成 Ed25519 设备密钥并证明持有私钥，服务器绑定设备和该商品授权。默认每条管理链接允许 1 次浏览器登录及 1 个 CLI 设备绑定，两种额度独立；已有 CLI 设备续签不再次消耗额度。

CLI Bearer 会话有效 8 小时，到期前或失效后客户端通过设备签名自动续签。能否续签仍取决于设备、管理链接及全部祖先是否有效；设备或授权撤销、授权过期后不能继续使用。

## 复制机器人接入提示词

管理链接界面可生成机器人接入提示词，包含操作范围、安装与登录方式，以及 **5 分钟有效、仅用于 CLI 首次绑定**的票据。把提示词交给需要处理该商品的机器人即可；票据不能用于浏览器登录。

提示词和票据只能由已登录的浏览器会话生成，CLI Bearer 不能生成新的绑定票据。票据完成设备绑定后不需要反复生成，后续使用该设备的签名续签。复制提示词不会自动启动常驻机器人；接入方决定何时读取队列、如何处理并提交结果。

## 查看多个商品

重复登录不同商品的授权，客户端会保存在同一个配置中：

```sh
extore manage products
extore manage queues --all
extore manage jobs --product PRODUCT_ID
extore manage job JOB_ID --product PRODUCT_ID
```

`queues --all` 按各授权分别请求商品队列，在客户端组合结果；它不是服务端的全商品管理权限。队列列表默认 `--view active`，以服务端 compact 视图只带处理摘要及附件数量/大小；不默认重复输出长描述、Markdown、顾客参数或字段结构。完整顾客参数、输入输出快照、步骤 ID 和文件 ID 通过 `job` 或 `files` 单独读取；`products --detail` 则按需获取完整商品元数据。

`--view processed` 查看已完成、已销毁及已拒绝任务，`--view all` 查看全部记录。`--state` 精确筛选状态，`--limit` 为 1–500。退回补充的 `needs_input` 在 active 中，等待顾客重提，不应再次领取。

`--origin https://example.com` 选择服务器；`--grant DEVICE_ID` 选择某个设备授权。对单商品操作必须传 `--product`。如果同一商品有多个可用授权而无法确定使用哪一个，应明确传入 `--origin` 或 `--grant`；权限不会取多个授权的并集。

## 处理一个任务

先读取任务自己的快照，再领取和交付：

```sh
extore manage job JOB_ID --product PRODUCT_ID
extore manage claim JOB_ID --product PRODUCT_ID
extore manage progress JOB_ID --product PRODUCT_ID --progress 30 --message "正在核实资料"
extore manage complete JOB_ID --product PRODUCT_ID --output-file result.json --message "已完成"
```

`result.json` 为任务输出字段代码到字符串值的对象，例如：

```json
{"resource_url":"https://example.com/resource","note":"请按商品说明领取。"}
```

字段必须符合目标任务的 `outputs` 快照，不能照搬商品后来修改的表单。服务商品完成时省略输出；默认单个 `content` 文本任务也可用 `--content-file` 读取 UTF-8 文件。`succeed` 是 `complete` 的别名。

有步骤计划时，用可重复的 `--completed-step STEP_ID` 传入完整已完成集合，进度由服务端计算，不能撤回当前尝试已经完成的步骤。`claim` 和 `progress` 可用 `--steps-file steps.json` 为尚无计划的任务绑定一次 1–30 项步骤计划，格式为 `[{"id":"verify","label":{"zh-CN":"核实资料"}}]`，已有计划不能覆盖。`claim` 可接受多个同商品任务 ID；其他操作逐任务执行，避免把一位顾客的结果交给另一位顾客。

### 要求重试、拒绝与失败

```sh
extore manage request-retry JOB_ID --product PRODUCT_ID --reason "请补充账户邮箱截图。" --reason-type customer_input --retry-mode revise
extore manage request-retry JOB_ID --product PRODUCT_ID --reason "外部服务恢复后重试，无需修改资料。" --reason-type external --retry-mode reuse
extore manage reject JOB_ID --product PRODUCT_ID --reason "提供的账户不符合商品条件。"
extore manage fail JOB_ID --product PRODUCT_ID --message "确认未交付" --retryable
extore manage retry JOB_ID --product PRODUCT_ID
```

- `request-retry`：任务变为 `needs_input`，附顾客可见原因。`--reason-type` 为 `customer_input`、`external` 或 `processor`；`--retry-mode revise` 要求修改后重提，`reuse` 允许顾客原资料重试。默认是 `customer_input/revise`。沿用原任务和快照，顾客实际重试时才增加 `attempt`；不受发货失败的重试开关或次数限制，过期、撤销仍阻止重提。
- `request-changes`：兼容旧命令，默认仍是修改后重提；新接入优先使用 `request-retry` 明确表达原因和方式。
- `reject`：任务和卡密进入拒绝终态，顾客在原领取链接看到原因，不能重新提交或领取内容。
- `fail`：真正的处理失败；只有确认未交付时才传 `--retryable`，顾客重试还须符合商品规则。
- `retry`：具有 `queue.retry` 的管理者核实后放行失败重试，实际新尝试由顾客提交。

要求重试与拒绝必须提供非空白、最多 1000 字符的原因；这段文字会显示给顾客。处理、审核和交付都限于自己已领取的 `processing` 队列任务，不能覆盖 Webhook 或预设处理器的自动任务。原因不应包含令牌或原始异常；外部故障用 `external/reuse` 即可，不必让顾客重复上传材料。

## 附件

```sh
extore manage files JOB_ID --product PRODUCT_ID
extore manage download JOB_ID --product PRODUCT_ID --file-id FILE_ID --output ./material.pdf
extore manage upload JOB_ID --product PRODUCT_ID --field deliverable --file ./result.pdf
```

上传返回文件 ID，把它填入 `complete --output-file` 使用的对应输出字段。文件上传是交付材料，不是安装处理器代码。下载要求新的目标文件，客户端不覆盖已有文件；下载文件使用私有权限保存。服务端的单文件、单卡密和全站存储限制仍适用，默认值见[运行指南](getting-started.md#文件上传与存储)。

## 商品、卡密与授权管理

这些命令的作用范围仍由当前商品授权决定。通用选项 `--product PRODUCT_ID`、`--origin`、`--grant`、`--detail` 与 `--output NEWFILE` 放在具体操作之后，例如 `extore manage cards stats --product PRODUCT_ID --detail`。`--detail` 增加业务元数据，不在普通输出中显示认证凭证。

| 命令 | 输入与作用 |
| --- | --- |
| `product get` | 默认摘要；`--detail` 读取完整业务配置，秘密字段脱敏；实际秘密配置须 `--include-secrets --output NEWFILE` |
| `product update` | `--json-file PATCH.json` 或 `--json-stdin`，按顶层字段合并修改，保留未提供配置 |
| `product schema` | `--language zh-CN`（默认）或 `en`；默认字段代码、类型、必填与名称，`--detail` 附完整教程 |
| `product prompt` | `--language`；显式生成供上游商城创建商品的完整介绍与规格提示词 |
| `cards list` | `--limit`，默认 50，内部 ID 与安全状态，不恢复原卡密 |
| `cards inventory` | `--variant`、`--status`、`--batch`、`--search`、`--offset`、`--limit`；分页查询 |
| `cards batch` | 同库存过滤，但必须传 `--batch BATCH_ID` |
| `cards stats` | 商品与规格统计；`--detail` 包含全部统计口径 |
| `cards history CARD_ID` | 单张卡密的安全时间线 |
| `cards issue` | `--count`（1–1000，默认 1）、`--variant`（默认 default）、`--label`、`--expires FUTURE_UNIX`；卡密保存到私密 JSON 文件 |
| `cards revoke CARD_ID` | 撤销仍符合服务端规则的卡密 |
| `links list` | 当前商品有权查看的授权范围与两类额度；`--view active` 默认，history 看失效记录，all 含归档墓碑 |
| `links create` | `--json-file LINK.json` 或 `--json-stdin`；新链接保存到私密 JSON 文件 |
| `links revoke LINK_ID` | 撤销有权管理的链接分支及后代 |
| `links cleanup` | 默认预览可归档的失效链接；`--apply` 才执行，`--limit` 为 1–500 |
| `events list` | `--limit`，默认 50；默认投递摘要，`--detail` 读取安全事件元数据 |
| `events retry EVENT_ID` | 重新投递已停止的事件 |
| `sessions list / revoke SESSION_ID` | 查看或撤销当前授权可管理的会话 |
| `devices list / revoke DEVICE_ID` | 查看或撤销商品 CLI 设备；撤销设备阻止继续续签 |
| `audit` | `--limit`，默认 50，服务端最高 200；登录、设备和授权审计 |
| `processors` | 内置预设摘要；`--detail` 查询配置与输入输出定义 |
| `source` | 当前服务器的源码信息；可用 `--origin` 选择服务器，不要求商品 ID |

列表 `--limit` 为 1–500；商品管理权限仍由服务器检查。卡密 `--status` 支持 `unused`、`needs_input`、`queued`、`processing`、`succeeded`、`failed_retryable`、`failed_terminal`、`destroyed`、`revoked`、`expired`、`rejected`。`remaining` 包含未到期的未提交与退回补充卡密，退回补充仍归原顾客任务使用。

```sh
extore manage product schema --product PRODUCT_ID --language zh-CN
extore manage product update --product PRODUCT_ID --json-file patch.json
extore manage cards stats --product PRODUCT_ID
extore manage cards inventory --product PRODUCT_ID --status needs_input --limit 50
extore manage cards issue --product PRODUCT_ID --variant standard --count 10 --label "第一批"
extore manage links create --product PRODUCT_ID --json-file link.json
```

`product get --include-secrets --output NEWFILE` 必须由同一个授权同时具备 `product.edit` 与 `fulfillment.configure`，获授权的 Webhook 等配置只保存到新 0600 文件，禁止输出到终端。处理器配置档案的模板、资源地址等秘密不会出现在商品导出中；它们由店主通过独立的[配置档案命令](shops.md#处理器配置档案)更新，读取只返回元数据。

`patch.json` 只填写需要改动的顶层字段，例如 `{"name":"新名称","description":"新的领取说明"}`。数组或嵌套对象在显式提供时整体替换，不是递归合并；修改未来任务的表单仍遵守快照和已发卡冻结规则。

`link.json` 示例：

```json
{"name":"资料处理","days":1,"permissions":["queue.view","queue.process"],"max_uses":1,"max_cli_uses":1}
```

创建子链接需要 `links.delegate`，权限必须严格少于父链接，同商品且期限、浏览器/CLI 额度均不得超过父链接。只有 `product.edit` 的管理者不能修改发货配置；配置权限、卡密权限和队列权限分别判断。

### 同源 API 命令

```sh
extore manage api GET /api/manage/jobs --product PRODUCT_ID --query view=active --query limit=20
extore manage api PUT /api/manage/product --product PRODUCT_ID --json-file product.json
```

`api` 接受 `GET / POST / PUT / PATCH / DELETE` 和已支持的相对 `/api/manage/…` 路径，用可重复的 `--query KEY=VALUE` 传查询，JSON 用 `--json-file` 或 `--json-stdin`。它不会接受外部 URL、跟随重定向或扩大权限。商品写入的底层 API 使用完整 Product；需要局部改动时优先用 `product update`。队列 GET 默认 compact 与 active，`--detail` 按需读取完整详情。制卡与创建管理链接的 API 响应仍自动保存到私密文件。

## 输出与本地配置

默认列表为摘要，使用 `job`、`schema`、`--detail` 或显式 `prompt` 读取当前任务所需内容。`--output NEWFILE` 可将支持该选项的命令结果写入新 0600 文件；已存在的目标会被拒绝。

制卡、创建管理链接等一次性凭证结果会在服务端操作前预留输出文件：未传 `--output` 时自动放入配置目录的 `exports/`，目录 0700、文件 0600。标准输出仅显示文件路径、数量、批次等摘要，不打印完整卡密或新授权链接。请保存这些文件；库存和历史接口不能找回原卡密。

## 配置、输出与撤销

默认配置为 `${XDG_CONFIG_HOME:-~/.config}/extore/cli.json`，也可用 `EXTORE_CLI_CONFIG` 或 `extore manage --profile PATH …` 指定。配置目录要求当前用户所有、权限 0700，配置与锁文件要求 0600；其中保存设备私钥和当前会话，普通命令输出不返回这些凭证。

命令输出 JSON。成功用 `ok:true`，失败用 `ok:false` 与 `error`、`code`，HTTP 错误另带 `status`；失败退出码非零。读取列表不代表发货成功，任务成功以服务端状态为准。

```sh
extore manage logout --product PRODUCT_ID
extore manage logout --all
```

`logout` 结束所选当前 CLI 会话并删除本地设备密钥记录，服务器仍保留设备审计记录。需要彻底阻止设备以后续签时，在后台撤销设备；撤销管理链接同时停止其设备、会话及下级授权。会话界面区分 browser / cli 渠道，并显示绑定设备信息。

# 命令行指南

[返回项目首页](../README.md) · [运行指南](getting-started.md) · [顾客 CLI](cli-customer.md) · [店主 CLI](cli-owner.md) · [AI 接入提示词](ai-prompts.md) · [接口协议](protocol.md)

完整 CLI 要求 **0.6.0 及以上**，本文设备码登录要求 **0.7.0 及以上**；也可在该版本源码目录用 `uv run extore …` 运行。查看本机版本可用 `extore --version`。

| 入口 | 用途 | 授权 |
| --- | --- | --- |
| `extore manage` | 商品配置、卡密、队列、附件、管理链接、事件与会话 | 主动设备码申请，店主明确批准商品和权限；兼容已有商品管理链接，可保存多个独立授权 |
| [`extore customer`](cli-customer.md) | 验码、填参、上传材料、跟踪状态、领取和销毁 | 卡密或已有领取链接，不使用商家权限 |
| [`extore admin`](cli-owner.md) | 本店商品、队列、卡密、安全；平台账号可维护店铺和 SMTP | 固定到账号的 CLI 设备；店主支持邮箱密码及第二因素或真实 Passkey 批准，平台管理员使用 Passkey |
| `extore init / serve / worker …` | 本机初始化、运行与服务器恢复 | 服务器用户，见[运行指南](getting-started.md) |

下面介绍 `manage`。每个商品有独立设备授权；店铺流水线申请可一次批准多个商品，客户端逐商品保存并聚合查看，每次操作仍使用其中一个授权，不合并权限。示例中的大写 ID 与路径是占位符，请用当前操作返回的实际值替换。

## 安装与登录

需要 Linux、Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)：

```sh
uv tool install --upgrade 'extore>=0.7.0'
extore manage --help
extore manage login --device-code --origin https://extore.lmm.best --product PRODUCT_ID --client-name '我的 AI Bot'
```

CLI 显示公开授权地址、短设备码和设备指纹，最多等待 10 分钟。本人在浏览器登录店主账号、输入设备码，核对名称、指纹、商品和权限后批准。不需要预先创建管理链接，也不用将访问密钥交给 AI；审批后用本机保存的设备密钥续签。

单商品默认申请 `queue.view,queue.process,queue.retry`，可用 `--permissions` 显式请求该商品所需的其他权限，店主看到完整范围后批准。需要某店的全部队列商品可执行：

```sh
extore manage login --device-code --origin https://extore.lmm.best --shop SHOP_ID --pipelines-all --client-name '店铺 Bot' --no-wait
```

这是申请时该店当前队列商品的快照，仅允许三个队列权限；不会获得店主账号管理、SMTP、其他店铺或未来新商品的权限。店主可在首次批准时缩小商品清单与权限，成功结果中的 `authorization` 和 `grants` 是实际有效范围。

提示写到 stderr；成功结果写到 stdout 的一行 JSON，包含商品授权摘要，不包含私钥、Bearer、签名或服务器挑战。`--no-wait` 的 stdout 为 `{"ok":true,"pending":true,"authorization":{"approval_url":"…","user_code":"…","fingerprint":"…","expires":…}}`，可以将其中公开字段转告本人，无需让本人访问 AI 的云端终端。

如果 AI 的工具不能一直等待，先加 `--no-wait` 取得公开地址和设备码，交给本人批准后，再运行相同命令并去掉 `--no-wait`，保持相同 profile、目标、权限、原因和 client-name。未过期的申请会继续使用，网络中断或 Ctrl+C 后也可重复该命令。超过 10 分钟重新申请。拒绝或过期不会创建可用的商品授权。

成功后保存 `authorization.id` 和各商品 `grants[].id`。后者可作为业务命令的 `--grant`，分别指向商品设备，权限不会相互叠加。

## 申请追加商品或权限

需要更多权限时主动申请，由店主再次批准。`--permissions` 是完整期望集合，必须包含已有权限；不写则保留已有权限。

```sh
extore manage authorize --authorization AUTHORIZATION_ID --permissions queue.view,queue.process,queue.retry,product.edit --reason '需要调整该商品的输入参数' --no-wait
extore manage authorize --authorization SHOP_AUTHORIZATION_ID --product NEW_PRODUCT_ID --reason '接管新商品队列' --no-wait
extore manage authorize --authorization SHOP_AUTHORIZATION_ID --pipelines-all --reason '申请当前新增的队列商品' --no-wait
```

第一条适用于单商品授权。单商品授权保持一个商品；处理另一个商品需另执行 `login --product NEW_PRODUCT_ID`。店铺流水线授权可重复 `--product` 追加商品，或用 `--pipelines-all` 重新申请当前快照，两种选择不能同时使用；它仍只允许三个队列权限。

本人批准后，重复相同申请并去掉 `--no-wait` 完成。待批准、拒绝或申请过期都不会改变原有权限；批准并领取后，原商品设备 ID 和任务处理者保持稳定，正在处理的任务可继续交付。领取后网络中断可重复原命令续签；若未收到领取结果且设备码已过期，需重新核对批准，仍恢复原授权与任务身份。追加授权不会延长原到期时间。

也可用 `--grant DEVICE_ID` 选择保存的授权。对旧管理链接设备申请更大权限会新建独立商品授权，保留旧链接权限；旧设备已领取的任务仍需使用原 `--grant ORIGINAL_DEVICE_ID` 处理，不将任务移交给新身份。

## 兼容已有管理链接

已有管理链接的浏览器会话可以使用兼容设备码入口，不申请新的店主授权：

```sh
extore manage login --device-code --existing-link --origin https://extore.lmm.best --product PRODUCT_ID --no-wait
```

省略商品和店铺目标的设备码命令也保留原兼容方式。管理链接留在本人浏览器，CLI 不接收它；浏览器与 CLI 的授权次数分别计算，批准不会再次消费浏览器次数。这个方式使用原链接既有权限，不接受新的 `--permissions`。

原有私密链接登录继续可用：`extore manage login` 会隐藏输入，粘贴完整管理链接即可。需要自动接入时，把链接从私密文件交给客户端：

```sh
extore manage login --link-stdin < /path/to/private-link.txt
```

支持完整的 `/staff#…` 商品管理链接或 `/cli#…` 专用票据链接；不接受裸 token，也不通过命令行参数传递链接。生产服务器必须使用 HTTPS，本机回环地址可用 HTTP。`--client-name` 可设置后台审计中显示的设备名称。

登录时客户端在本机生成并私密保存 Ed25519 设备密钥，用签名证明持有私钥；服务器绑定设备和浏览器明确批准的商品授权。兼容入口中，默认每条管理链接允许 1 次浏览器登录及 1 个 CLI 设备绑定，两种额度独立；同一设备重复批准相同授权或后续续签不再次消耗 CLI 额度。不同商品的授权可保存到同一配置，操作时仍逐个检查权限。

CLI Bearer 会话有效 8 小时，到期前或失效后客户端通过设备签名自动续签。主动授权还检查店铺、商品和授权版本，兼容入口还检查管理链接及全部祖先；设备或授权撤销、授权过期后不能继续使用。

## 复制机器人接入提示词

机器人接入时优先让它执行设备码登录，把公开授权地址和设备码交给你核对批准，不需把管理链接传给机器人。管理链接界面也兼容旧的接入提示词与 **5 分钟有效、仅用于 CLI 首次绑定**的票据；票据不能用于浏览器登录。

提示词和票据只能由已登录的浏览器会话生成，CLI Bearer 不能生成新的绑定票据。票据完成设备绑定后不需要反复生成，后续使用该设备的签名续签。复制提示词不会自动启动常驻机器人；接入方决定何时读取队列、如何处理并提交结果。

## 查看多个商品

对不同商品分别运行设备码登录并在浏览器批准，客户端会保存在同一个配置中：

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
| `product prompt` | `--language`；显式生成供外部商城创建商品的完整介绍、规格与参考价提示词；Extore 不收款 |
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

`product get --include-secrets --output NEWFILE` 必须由同一个授权同时具备 `product.edit` 与 `fulfillment.configure`，获授权的 Webhook 等配置只保存到新 0600 文件，禁止输出到终端。商品管理链接的 `product get` 仍不导出处理器配置。店主和平台管理员通过独立的[配置档案命令](shops.md#处理器配置档案)更新、绑定配置；档案响应的 `configuration` 可读回处理器字段定义中严格标为 `secret:false` 的模板、说明等内容，`configured_fields` 只表示哪些字段已配置。资源 URL、密钥及未知字段默认隐藏，不回读；缺少 `secret` 或其值不是严格 `false` 的字段也不会回读。

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

## 用 CLI 配置商品处理器工作流

工作流配置要求 **0.7.0 及以上**，通过已授权的 `extore admin` 店主设备操作，商品队列的 `extore manage` 授权不能读取店铺配置档案。每个档案独立保存普通变量、写入后不可读回的密钥和运行限制，再绑定到同店、同处理器的商品。

先创建权限为 0600 的 JSON 文件，在自己的编辑器中填写；密钥不放进命令行参数、聊天或日志。

```sh
umask 077
touch ./processor-profile.json
chmod 600 ./processor-profile.json
```

文件格式如下。`API_KEY` 的值在私密文件中填写，示例中的处理器模板可按需修改：

```json
{
  "name": "文档流水线配置",
  "processor_id": "personalized_text",
  "configuration": {"template": "Hello $name"},
  "workflow": {
    "variables": {"MODEL": "example-model", "LANGUAGE": "zh-CN"},
    "secrets": {"API_KEY": "在私密文件中填写"},
    "runtime": {
      "timeout_seconds": 120,
      "memory_mb": 256,
      "cpu_seconds": 120,
      "max_output_bytes": 1000000
    }
  }
}
```

创建返回档案 ID，随后查看、编辑并绑定商品：

```sh
extore admin processor-profiles create --grant OWNER_DEVICE_ID --json-file ./processor-profile.json
extore admin processor-profiles list --grant OWNER_DEVICE_ID
extore admin processor-profiles get PROFILE_ID --grant OWNER_DEVICE_ID
extore admin processor-profiles update PROFILE_ID --grant OWNER_DEVICE_ID --json-file ./workflow-patch.json
extore admin processor-profiles bind PROFILE_ID --product PRODUCT_ID --grant OWNER_DEVICE_ID
extore admin processor-profiles binding --product PRODUCT_ID --grant OWNER_DEVICE_ID
extore admin processor-profiles unbind --product PRODUCT_ID --grant OWNER_DEVICE_ID
extore admin processor-profiles revoke PROFILE_ID --grant OWNER_DEVICE_ID
```

平台管理员创建、列出某店档案时显式加 `--shop SHOP_ID`。商家设备只能操作本店档案，不能跨店绑定。撤销档案会阻止依赖它的处理任务继续运行。

`workflow-patch.json` 也必须是 0600 私密文件。更新逐键合并：未填写的普通变量、密钥和运行限制保留；普通变量的空字符串会保存，密钥的空字符串保留原值。删除必须用明确的数组，同一名称不能同时设置和删除：

```json
{
  "workflow": {
    "variables": {"LANGUAGE": "en", "NOTE": ""},
    "secrets": {"API_KEY": ""},
    "delete_variables": ["OLD_MODEL"],
    "delete_secrets": ["OLD_API_KEY"],
    "runtime": {"timeout_seconds": 60}
  }
}
```

变量和密钥名称必须符合 `[A-Z][A-Z0-9_]{0,63}`，不能以 `EXTORE_` 开头，两类名称不能相同。每类最多 64 项，单值最多 8 KiB，总值最多 64 KiB。运行限制只接受整数：超时 10–120 秒、内存 64–512 MiB、CPU 时间 1–120 秒、输出 65536–1000000 字节；不能通过此配置改运行命令、镜像、网络或挂载。

所有读取、创建、更新和绑定结果都只返回安全配置：`configuration` 中严格标为 `secret:false` 的模板或说明，以及 `workflow.variables`、`workflow.runtime`、`workflow.configured_secret_names`。密钥只显示已配置的名称；真实值、资源 URL 和加密内容不会出现在 stdout 或 `--output` 导出中。普通模板和变量可读回并编辑，不会再次被全部打码。输出文件仍为新的 0600 文件。

更新档案只生成新修订，商品仍保留原绑定；显式执行 `bind` 后，之后新发的卡密才使用新修订。已经发出的卡密和任务保留原模板、变量、密钥和运行限制，即使商品后来解绑。

同样的操作可通过店主的通用 API 命令完成，私密 JSON 文件和安全返回规则一致：

```sh
extore admin api POST /api/admin/processor-profiles --grant OWNER_DEVICE_ID --json-file ./processor-profile.json
extore admin api GET /api/admin/processor-profiles/PROFILE_ID --grant OWNER_DEVICE_ID
extore admin api PUT /api/admin/processor-profiles/PROFILE_ID --grant OWNER_DEVICE_ID --json-file ./workflow-patch.json
extore admin api PUT /api/admin/processor-profiles/bindings/PRODUCT_ID --grant OWNER_DEVICE_ID --json-file ./binding.json
extore admin api DELETE /api/admin/processor-profiles/bindings/PRODUCT_ID --grant OWNER_DEVICE_ID
extore admin api DELETE /api/admin/processor-profiles/PROFILE_ID --grant OWNER_DEVICE_ID
```

`binding.json` 的内容是 `{"profile_id":"PROFILE_ID"}`。平台管理员的通用列表命令可加 `--query shop_id=SHOP_ID`，创建文件则填写 `shop_id`。

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

`logout` 结束所选当前 CLI 会话，清理相应待审批申请与不再使用的本地恢复密钥，保留其他商品的授权。店铺快照中多个商品共用授权密钥，退出一个商品会保留其余成员所需的密钥。`logout --all` 同时清理所有本地授权、设备密钥和待审批申请，服务器仍保留审计记录。

需要彻底阻止设备续签时，在后台撤销流水线授权或设备；撤销管理链接同时停止其设备、会话及下级授权。主动授权被撤销后，先退出相应范围，再申请新设备码，由店主重新批准；整组授权可用 `logout --origin SERVER` 清理该服务器所有本地记录后重新申请。旧链接兼容入口须提供仍有效且有额度的新授权，不能绕过撤销或绑定次数。会话界面区分 browser / cli 渠道，并显示绑定设备信息。

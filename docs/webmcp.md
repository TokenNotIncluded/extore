# 原生 WebMCP

Extore 把顾客兑换、领取和商家日常处理操作注册成浏览器原生工具。工具调用沿用页面的 API、Cookie 会话和服务端权限；没有独立管理员凭证，也不增加远程 MCP 服务端。

命令行接入使用独立的[商品管理、顾客和店主 CLI](cli.md)，浏览器原生工具仍为下文的 49 个。店主 CLI 首次设备批准必须通过正常页面和真实 Passkey，原生工具不代办 WebAuthn；后续设备续签与操作签名由 CLI 完成。商品管理的短期 CLI 票据也只能由已登录浏览器生成，CLI Bearer 不能生成新的绑定票据。

实现位于 [`extore/static/webmcp.js`](../extore/static/webmcp.js)，通过 `window.ExtoreWebMCP` 与页面连接。没有 WebMCP 的浏览器继续使用普通界面，不安装 polyfill，不伪造 `document.modelContext` 或 `navigator.modelContext`。

## 浏览器兼容性

优先检测 `document.modelContext.registerTool`，以 `registerTool(tool, {signal})` 注册，用注册生命周期的 `AbortController` 撤回工具。仅当浏览器实际提供旧的 `navigator.modelContext` 时才使用它；旧接口如提供 `unregisterTool`，撤回时同时调用。不会用旧教程中的 `provideContext` 覆盖整个页面工具集。[WebMCP 草案，2026-10-02](https://webmachinelearning.github.io/webmcp/)、[Chrome Imperative API](https://developer.chrome.com/docs/ai/webmcp/imperative-api)

WebMCP 仍是实验中的社区草案，浏览器版本、实验开关和 API 可能变化。支持状态以当前页面的功能检测为准；加载了本站脚本不等于浏览器原生工具已可用。[草案状态](https://webmachinelearning.github.io/webmcp/#sotd)

## 接入页面

先加载 `webmcp.js`，再由应用配置已有操作：

```js
await window.ExtoreWebMCP.configure({
  api, // api(path, body?, method?, options?)，path 相对 /api
  getContext: () => ({
    page: "receipt", // home | receipt | admin | staff
    role: null, // admin | staff（商品管理链接会话）| bootstrap | null
    permissions: [], // 商品管理链接的实际权限
    productId: null, // /auth/status 返回的 product_id，链接只绑定一个商品
    linkExpires: null, // 可选，/auth/status 返回的 link_expires，Unix 秒
    product: currentProduct,
    currentToken,
    cardId, // 批量领取页当前在界面选中的 card_id
    queueProductId, // 管理任务页当前选择的商品 ID
    queueProduct, // 已授权商品队列的安全元数据，含 mode、outputs 与 progress_steps
    tab,
  }),
  actions: {
    exchange, // exchange(code, options?)：验证并更新页面
    redeem,   // redeem(params, options?)：提交兑换并更新页面
    receipt,  // receipt(options?)：读取领取状态
    reveal,   // reveal(options?)：显式读取内容
    destroy,  // destroy(options?)：永久销毁本站交付内容
    navigate, // navigate(path, tab?)：本站允许的路由
    selectQueue, // selectQueue(product_id, options?)：选择商品队列并更新页面
    uploadFile, // uploadFile(definition, options?)：固定同源文件上传，scope 为 customer / job
    readFile, // readFile({product_id,job_id,file_id,max_bytes}, options?)：授权下载并返回限量 base64
    refreshUI,
  },
}); // configure() 已异步完成首次 refresh()
```

`options` 可携带 `signal`，应传给 `fetch`。`api` 必须继续使用同源请求和原有会话，不能接受任意外部 URL、替换身份或绕过服务端验证。`actions` 与人工点击共用业务逻辑，因此工具操作后，页面也显示新状态。

页面、登录身份、授权权限/商品范围、后台标签、商品、所选队列或领取凭证变化后调用 `refresh()`。退出页面或卸载集成时调用 `dispose()`。不要把 `currentToken`、原始卡密或管理链接放入工具名称、描述或日志。

```js
window.ExtoreWebMCP.capabilities();
// {
//   supported: true,
//   apiSurface: "document.modelContext", // 或 navigator.modelContext / null
//   registeredTools: ["extore_context", ...],
//   lastError: null
// }

await window.ExtoreWebMCP.refresh();
window.ExtoreWebMCP.dispose();
```

`registeredTools` 是本集成当前注册的工具名；它不包含其他脚本注册的工具。`lastError` 便于查看注册失败，不能代替一次真实的浏览器工具执行。

## 工具范围

工具使用 `extore_` 前缀。下表名称均包含这个前缀。工具仅在对应页面、角色、权限和标签下提供；Bootstrap 身份不能操作商品和任务，Passkey 认证仍由正常界面完成。`staff` 是商品管理链接会话沿用的内部角色与路由名；它按授权管理一个商品，没有全站管理员权限。

商品管理链接权限：

| 权限 | 允许的范围 |
|---|---|
| `queue.view` | 查看该商品队列 |
| `queue.process` | 领取队列任务、更新进度、提交结果、标记失败；人或获授权的 AI 都可处理 |
| `queue.retry` | 核实后放行该商品失败任务重试 |
| `product.edit` | 查看并修改该商品基本配置，如名称、说明、图片、参数和队列输出展示文本 |
| `fulfillment.configure` | 配置发货方式、交付/查看规则、官方处理器、Webhook、重试规则，以及队列输出字段结构 |
| `cards.manage` | 查询、发行、撤销该商品卡密 |
| `events.manage` | 查看与重投该商品事件 |
| `links.delegate` | 创建严格更小权限的子管理链接、管理下级授权 |

`/staff` 的 `products` / `jobs` / `cards` / `staff` / `events` 标签根据实际权限提供，`sessions` 允许查看自己的登录状态，不能进入 `security`。`queue.process` 和 `queue.retry` 都必须同时授予 `queue.view`；`fulfillment.configure` 必须同时授予 `product.edit`。商家创建的完整商品管理链接可以授予全部 8 项权限；前端显示标签不代表持有权限。

权限依赖同时写入授权工具 `inputSchema` 的 `allOf` 条件，并由执行函数和服务端再次验证，不能仅依赖浏览器校验 Schema。

| 范围 | 工具 | 用途 |
|---|---|---|
| 当前页面 | `extore_context` | 获取页面、身份和可用能力，不返回领取凭证 |
| 公开商品 | `extore_products_list`、`extore_product_get` | 只查公开商品；非公开商品须先验证有效卡密 |
| 首页或领取页 | `extore_code_verify` | 验证顾客提供的卡密，进入对应商品 |
| 页面导航 | `extore_ui_navigate` | 在本站允许的页面、后台标签之间切换 |
| 当前领取页 | `extore_product_parameters` | 读取当前商品的参数定义与教程 |
| 当前领取页 | `extore_receipt_status` | 查询当前兑换与队列状态，不领取内容 |
| 当前领取页 | `extore_redemption_submit`、`extore_redemption_retry` | 提交当前商品参数，或在规则允许时重试 |
| 当前领取页：包含文件输入 | `extore_redemption_file_upload` | 为当前有效卡密上传文件，返回用于兑换参数的文件 ID |
| 当前领取页 | `extore_receipt_reveal`、`extore_receipt_destroy` | 明确领取内容，或永久销毁本站交付内容 |
| 商家商品标签 | `extore_product_create` | 创建商品，商品管理链接不能新增商品 |
| 商家商品标签 | `extore_product_templates`、`extore_product_quick_create` | 查询快速创建模板，创建私有草稿商品与配置用管理链接 |
| 商品标签：商家或 `product.edit` | `extore_products_admin_list` | 列出当前身份可管理的商品，商品管理链接只返回自身商品 |
| 商品标签：商家或 `product.edit` | `extore_product_admin_get`、`extore_product_update` | 读取、更新商品配置，秘密字段脱敏；管理链接限于自身商品，没有商品删除接口 |
| 商品标签：商家或 `product.edit` | `extore_product_export_prompt` | 导出跨平台创建商品用的安全文本提示词；不写剪贴板，不创建外部商品 |
| 商品标签：商家或 `product.edit` | `extore_processors_list` | 获取官方处理器的配置、输入与输出 Schema，不返回实际配置秘密 |
| 卡密标签：商家或 `cards.manage` | `extore_cards_list`、`extore_cards_issue`、`extore_card_revoke` | 查询、批量发行、撤销卡密 |
| 卡密标签：商家或 `cards.manage` | `extore_card_stats`、`extore_card_inventory`、`extore_card_history` | 只读统计、分页库存与卡密生命周期，均不返回完整卡密 |
| 管理链接标签：商家或 `links.delegate` | `extore_staff_list`、`extore_staff_authorize`、`extore_staff_revoke` | 查询、创建或撤销商品管理链接；委托限于下级授权 |
| 任务标签：商家或 `queue.view` | `extore_queue_products`、`extore_queue_select`、`extore_jobs_list` | 选择和查看商品独立队列 |
| 任务标签：商家或 `queue.view` | `extore_jobs_files_list`、`extore_jobs_file_read` | 查看任务附件元数据、受限读取文件 |
| 任务标签：商家或 `queue.process` | `extore_jobs_claim`、`extore_jobs_progress`、`extore_jobs_complete`、`extore_jobs_fail`、`extore_jobs_request_changes`、`extore_jobs_reject` | 批量处理队列任务，退回补充或拒绝须说明原因，仍限于授权商品 |
| 任务标签：商家或 `queue.process` | `extore_jobs_file_upload` | 为自己领取的任务上传交付文件 |
| 任务标签：商家或 `queue.retry` | `extore_jobs_allow_retry` | 核实失败任务后放行顾客重试 |
| 事件标签：商家或 `events.manage` | `extore_events_list`、`extore_event_retry` | 查看事件，重投已停止投递的 Webhook |
| 商家安全标签 `security` | `extore_passkeys_list` | 只读 Passkey 列表 |
| 会话标签 `sessions`：商家或已登录管理链接 | `extore_sessions_list`、`extore_session_revoke`、`extore_audit_list` | 查看安全会话与登录审计元数据，撤销授权范围内的会话 |
| 已登录商家后台或管理链接页 | `extore_session_logout` | 退出当前会话 |

共有 49 个工具定义，按页面、标签和权限动态注册，不会同时全部暴露。实际工具的 `inputSchema` 是调用参数的权威说明。每次切换页面后重新发现工具，不能缓存之前的工具对象继续操作。

## 输入与确认

兑换提交、领取/销毁、管理写入和退出会话要求 `confirm: true`。Schema 将它限制为 `true`，执行函数也再次检查。它表示用户已经明确同意具体操作；代理不能为了通过校验自行补上确认。卡密验证、页面导航与队列选择不要求该字段，它们不提交兑换或交付。浏览器可根据 `consequentialHint` 再提供自己的确认交互。[Chrome 工具注解](https://developer.chrome.com/docs/ai/webmcp/imperative-api#tool-annotations-optional)

领取内容也有副作用：`view_policy=once` 商品会消耗唯一一次查看机会。销毁不可恢复，商品管理链接授权会产生可访问链接，发行卡密会生成真实兑换凭据；代理应先向用户说明数量、目标和影响，再提交确认。

参数值使用商品的 `key`，不是页面展示的多语言 `label`。例如：

```json
{
  "params": {"account_email": "customer@example.com"},
  "confirm": true
}
```

当前商品会决定参数结构和必填项，最终仍由服务端验证。商品配置字段、参数类型和状态机见 [`protocol.md`](protocol.md)。任务批处理一次最多 100 个 ID，整批事务成功或回滚；管理者只可处理自己领取的队列任务，不能覆盖自动发货任务。界面中的“队列”沿用协议值 `mode=manual`，不要求必须由人手动处理，获授权的 AI 同样可以领取并提交结果。

调用参数如下。表中 `?` 为可选字段，`+ confirm` 表示还必须传 `confirm: true`。所有对象拒绝未知字段，ID 是 1–100 字符的字母、数字、`_` 或 `-`。

| 工具（省略 `extore_`） | 输入 |
|---|---|
| `context`、`products_list`、`product_parameters`、`receipt_status` | `{}` |
| `product_get`、`product_admin_get` | `product_id` |
| `code_verify` | `code`，1–8000 字符，不能全是空白；支持最多 30 张同商品卡密，用换行、空格、逗号或分号分隔 |
| `ui_navigate` | `page`: `home` / `admin` / `staff`；`tab?` 允许后台页，值为 `products` / `jobs` / `cards` / `staff` / `events` / `security` / `sessions`，管理链接只可打开已授权标签与自身 sessions，security 仅商家 |
| `redemption_submit`、`redemption_retry` | 恰好一种：单卡 `params`，或批量 `items:[{card_id,params}]`（1–30 项，ID 不重复）+ confirm；参数值为字符串，单项最多 10000 字符；逐卡按输入快照验证 |
| `redemption_file_upload` | `field_key`、`filename`、`base64`、`content_type?` + confirm；限当前已验证卡密的文件参数 |
| `receipt_reveal`、`receipt_destroy`、`session_logout` | confirm |
| `products_admin_list`、`processors_list`、`staff_list`、`events_list`、`passkeys_list`、`queue_products`、`sessions_list` | `{}` |
| `session_revoke` | `session_id`（UUID）+ confirm；仅撤销服务端授权范围内的会话 |
| `audit_list` | `limit?`，整数 1–200，使用服务端限定的登录/授权审计范围 |
| `queue_select` | `product_id`，必须是当前身份可处理的商品 |
| `product_create` | `product` + confirm；`product.name` 必填，其他字段使用服务端默认值 |
| `product_templates` | `{}`，仅商家商品标签；返回两个内置模板的安全元数据 |
| `product_quick_create` | `template_id`、`from_product_id?`、`name?` + confirm；template_id 为 `manual_content` / `manual_service` / `existing_product`；existing_product 必须提供 from_product_id，内置模板禁止该字段；name 可选，提供时须为 1–120 字符且不能全为空白 |
| `product_update` | `product_id`、`changes` + confirm；`changes` 至少一项，未提供字段从现有配置保留 |
| `product_export_prompt` | `product_id`、`lang?`（`zh-CN` / `en`）、`include_inventory?`；返回纯文本提示词，默认不查询库存 |
| `cards_list` | `product_id?`、`limit?`，limit 为 1–500 |
| `cards_issue` | `product_id`、`count`、`variant_id?`、`label?`、`expires?` + confirm；variant_id 默认 `default`，count 为 1–1000，label 最多 100 字符且可空，expires 为未来有限 Unix 秒时间戳或 null（无截止时间） |
| `card_stats` | `product_id?`，管理链接仍只能查询自己的商品 |
| `card_inventory` | `product_id?`、`variant_id?`（规格 ID，空串为全部规格）、`status?`（下列 11 状态或空串）、`batch_id?`（最多 100 字符，只含字母/数字/`_`/`-`，可空）、`search?`（最多 100 字符）、`offset?`（整数 ≥0）、`limit?`（整数 1–500），按页查询安全库存记录 |
| `card_history` | `card_id`、`product_id?`，读取单张卡密的安全生命周期 |
| `card_revoke` | `card_id` + confirm |
| `staff_authorize` | `product_id`、`name`、`days`、`permissions`、`max_uses?`、`max_cli_uses?` + confirm；name 为 1–100 字符且非空白，days 为大于 0 且不超过 90 的有限数字，可用小数；permissions 为上述 8 个权限中的非空、不重复数组，并满足权限依赖；max_uses 与 max_cli_uses 分别为浏览器登录和 CLI 设备绑定额度，均为 1–1000 的整数，默认 1 |
| `staff_revoke` | `staff_id` + confirm |
| `jobs_list` | `product_id`、`view?`、`state?`、`limit?`；view 为 `active`（默认）/ `processed` / `all`；state 为 `queued` / `processing` / `succeeded` / `failed` / `needs_input` / `rejected` / `destroyed`，limit 为 1–500 |
| `jobs_files_list` | `product_id`、`job_id`；任务必须属于当前选择的商品队列 |
| `jobs_file_read` | `product_id`、`job_id`、`file_id`；先验证任务和附件所属范围，再读取限量内容 |
| `jobs_file_upload` | `product_id`、`job_id`、`field_key`、`filename`、`base64`、`content_type?` + confirm；必须为自己领取任务的输出快照中的文件字段 |
| `jobs_claim` | `product_id`、`ids`、`progress_steps?` + confirm；ids 为 1–100 个不重复任务 ID，步骤计划只能为尚无计划的任务绑定一次 |
| `jobs_allow_retry` | `product_id`、`ids` + confirm；放行重试，不修改步骤 |
| `jobs_progress` | `product_id`、`ids`、`progress?`、`progress_steps?`、`completed_steps?`、`message?` + confirm；progress 为 0–99 的整数，步骤型任务由服务端计算百分比；completed_steps 是任务快照中的完整已完成 ID 集合，省略则保留 |
| `jobs_complete` | `product_id`、`ids`、`message?`、`progress_steps?`、`output?`、`content?` + confirm；成功会自动完成全部步骤；执行时读取目标任务的输出快照严格验证，批内交付类型、字段代码、类型和必填规则必须相同；仅默认单 content 快照兼容 content，服务型任务只提交成功状态 |
| `jobs_fail` | `product_id`、`ids`、`message?`、`progress_steps?`、`retryable?` + confirm；仅确认未交付时设置 retryable=true |
| `jobs_request_changes`、`jobs_reject` | `product_id`、`ids`、`reason` + confirm；reason 为非空白、最多 1000 字符的顾客可见原因；只处理自己领取的 processing 队列任务 |
| `event_retry` | `event_id` + confirm |

任务 `message` 最多 1000 字符。未提供列表 `limit` 时使用后端默认值；不是无限查询。

`jobs_list` 默认使用 `view=active`，返回排队、处理中、失败待核实和退回补充任务；已完成、已销毁和已拒绝任务放在 `view=processed`，主动传 `view=all` 才读取全部历史。显式 `state` 优先，因此 `state=succeeded` 仍能精确查询已完成任务。文件工具通过 `job_id` 精确定位历史任务，仍可在相同商品权限下读取其安全附件信息。

队列按商品独立。先调用 `queue_products` 查询当前身份允许处理的商品，再用 `queue_select` 选择队列。`jobs_list` 和所有任务批处理必须传入与当前 `queueProductId` 相同的 `product_id`；不能把不同商品的任务 ID 混入一批，也没有“所有商品混合队列”。`/api/manage/products` 只返回可处理的商品，`/api/manage/jobs` 与 `/api/manage/batch` 同样限定商品范围；管理链接即使伪造参数，也不能访问授权商品之外的队列。

商品的 `progress_steps` 为新任务的默认步骤，最多 30 项，顺序就是处理计划顺序。每项只能含 `id` 和多语言 `label`；ID 使用与规格相同的 40 字符稳定 slug 且不可重复。label 最多 20 种语言，语言键须非空且最多 40 字符，每段显示文本须非空且最多 200 字符。`support_email` 为可选催办邮箱，最多 254 字符，空串表示不提供联系邮箱。这两项属于 `product.edit`，可以修改未来任务的默认计划；已有任务保留自己的计划快照。

`jobs_list` 返回每个任务的 `steps:[{id,label,done}]` 与 `completed_steps`。填写已完成步骤时必须使用这个任务的快照 ID，不应根据商品当前默认计划猜测。`completed_steps` 只接受最多 30 个不重复的 slug；省略保留现有集合，明确传入时须包含当前尝试已经完成的步骤，服务端拒绝未知步骤和撤回。步骤型任务的百分比由服务端计算，处理过程中最高 99，成功后自动完成全部步骤并显示 100；没有计划的旧任务仍支持百分比进度。

`jobs_request_changes` 与 `jobs_reject` 调用 `/api/manage/batch`，分别使用 `action=request_changes` / `reject`，把 `reason` 映射为 API 的 `message`。退回补充不等于发货失败：顾客可以沿用原任务重新提交，attempt 增加，不受商品失败重试次数限制；过期或撤销仍阻止重提。拒绝为终态，卡密同时禁用，原领取链接显示原因。两个工具都要求当前所选商品、最新权限和任务领取者匹配，且不能覆盖自动处理任务。

对于尚无计划、无已完成步骤且处于排队或处理状态的任务，`jobs_claim`、`jobs_progress`、`jobs_complete`、`jobs_fail` 可附带 `progress_steps`，一次绑定 1–30 项自定义步骤；计划一经绑定便不能替换。整批仍由服务端验证和原子更新。放行重试不清空完成集合；顾客真正提交新一次尝试时才重置步骤完成情况和进度，保留计划快照。`receipt_status` 使用字段白名单返回步骤、完成标记、`queue_position`、`support_email` 等公开状态，交付结果仍只通过明确的领取工具返回。

管理链接不能复制自己的全部权限。创建子链接时，`permissions` 必须是当前有效权限的**严格子集**，子链接至少少一个权限，并且有效期不能超过父链接的 `link_expires`。同商品、相同权限的子链接也会被拒绝。管理链接只能查询、撤销自己的下级，不能撤销父级或同级。撤销上级会同时撤销全部下级、结束相关会话并释放未完成的队列任务。普通查询不返回子链接凭证；新建时按需返回新授权链接。

新建管理链接的 `max_uses` 与 `max_cli_uses` 独立，分别限制新浏览器登录与新 CLI 设备绑定，默认均为 1，原生 Schema 与执行函数限制为 1–1000；子链接两项都不能高于最新父链接的对应上限。浏览器次数只在成功建立新会话时消耗，CLI 次数只在成功绑定新设备时消耗。页面 GET、已有会话查询和设备签名续签不会消费额度，上限耗尽也不结束已有效的会话或设备。安全元数据可显示两组上限、已用和剩余额度，没有重新取回链接凭据的工具。CLI 绑定、短期票据与撤销见[接口协议](protocol.md#cli-设备授权)。

`sessions_list` 使用 `/api/admin/sessions` 或 `/api/manage/sessions`；`session_revoke` 使用对应 `/sessions/{id}` 的 DELETE。返回字段只包含 UUID 会话标识、角色、`channel`（browser / cli）、`device_id`、`client_name`、管理链接/商品安全名称与 ID、创建/最近活动/截止时间、当前/撤销/有效标记、IP 和最多 300 字符的 User-Agent，不包含 Cookie、摘要或凭据。商品管理链接默认只能管理自己的会话；`links.delegate` 才将范围扩到同商品的下级，仍不能访问商家、父级或同级会话。

`audit_list` 使用 `/api/admin/audit` 或 `/api/manage/audit`，最多 200 条，只返回 `id`、`actor`、`action`、`target`、`created` 的安全登录与授权事件，包括分渠道消费、设备绑定/撤销及 CLI 票据创建。范围由服务端限定，不能查询全站任意订单、商品交付结果或请求 payload。撤销当前会话仍返回成功和已结束状态；随后界面需要登录或刷新返回 401，不会把已经提交的撤销误报为失败，旧的原生工具会被撤回。

商家工具继续使用 `/api/admin/*`；商品管理链接按权限使用下列同源接口：

| 权限 | 管理链接 API |
|---|---|
| `product.edit` | `GET /api/manage/product`、`PUT /api/manage/product`、`GET /api/manage/processors` |
| `cards.manage` | `GET/POST /api/manage/cards`、`POST /api/manage/cards/{id}/revoke` |
| `cards.manage` | `GET /api/manage/card-stats`、`GET /api/manage/card-inventory`、`GET /api/manage/cards/{id}/history` |
| `events.manage` | `GET /api/manage/events`、`POST /api/manage/events/{id}/retry` |
| `links.delegate` | `GET/POST /api/manage/links`、`POST /api/manage/links/{id}/revoke` |

这些 API 的商品范围由会话限定。`product_update` 会在内部读取现有配置后合并 `changes`，再提交完整 Product，以保留未传字段；不会为了编辑商品向代理返回 Webhook 密钥。

基本编辑与发货配置分开授权。只有 `product.edit` 时，管理 API 返回的 `webhook_secret` 为空；提交空密钥会保留原密钥，不会将其清除。`fulfillment.configure` 保护 `mode`、`delivery`、`view_policy`、`webhook_url`、`webhook_secret`、`allow_retry`、`max_attempts`、`processor_id` 和 `processor_config`，以及输出字段的代码名、类型与必填结构。重试规则也受保护，因为它可能改变外部再发货次数。

管理链接调用 `product_update` 时，只要 `changes` 明确包含任一受保护发货配置字段，就额外检查当前页面和最新会话中的 `fulfillment.configure`，即使传入值与旧值相同也需要权限。队列输出字段的展示名称、教程、折叠状态可以凭 `product.edit` 修改；仅调整字段数组顺序也不改变权限结构。改变代码名、类型或必填规则则需要 `fulfillment.configure`。只更新基本字段时，工具允许保留服务端脱敏的空密钥，再由后端恢复原值验证。后端同样拒绝未授权的发货配置变更；即使拥有这项权限，WebMCP 的普通返回结果仍脱敏密钥。

已知为 Webhook 或官方处理器自动处理的任务不会提供队列领取、进度、完成和失败工具；具有相应权限的管理者仍可查询状态，并在核实未交付后放行失败任务重试。

原生工具的 `product` 和 `changes` 接受以下字段：`name`、`description`、`logo`、`image`、`public`、`variants`、`progress_steps`、`support_email`、`mode`、`delivery`、`view_policy`、`allow_retry`、`max_attempts`、`parameters`、`outputs`、`webhook_url`、`webhook_secret`、`processor_id`、`processor_config`。原生 Schema 拒绝 `script` 字段，包括空字符串；协议商品查询中的 `script:""` 只用于兼容旧客户端，不能执行任意脚本。输入/输出字段支持 `key`、`label`、`description`、`collapsed`、`required`、`type`，类型均可使用 `text` / `email` / `url` / `textarea` / `number` / `file`。商品图片与 Webhook URL 必须为 HTTPS，Webhook 需明确提供至少 32 字符的密钥。更新已有 Webhook 商品时，未传新密钥会内部保留旧密钥，不把它返回代理。

## 多张卡密兑换

`code_verify` 共用公开兑换接口，支持同商品最多 30 张卡密；批内混入无效、不可兑换或其他商品卡密时整批拒绝。验证只建立页面上下文，不提交兑换，工具返回不包含领取 token。`receipt_status` 与 `product_parameters` 可返回批内 `card_id`、尾号、规格和各卡的商品输入快照；不返回顾客参数、交付内容或领取凭证。

批量提交用 `items:[{card_id,params}]`，只接受当前领取链接覆盖的卡密。`redemption_submit` 仅提交尚无任务的卡密；`redemption_retry` 仅处理 `job.can_retry=true` 的任务，包括退回补充。工具执行前重新读取状态并按每项的参数快照验证，不能用当前商品表单覆盖旧任务定义。批量页面的上传、领取和销毁使用当前界面选中的 `cardId`；未选择时拒绝执行，切换选择会使旧回调失效。文件下载仍须选择目标卡密；领取与销毁遵守原有显式确认及一次查看规则。

## 文件与 AI 处理

`file` 字段保存的是服务端上传后返回的 UUID 文件 ID，不能填写任意网址或文件路径。顾客先调用 `redemption_file_upload`，再把返回的 `id` 填入 `redemption_submit.params[field_key]`；已提交的任务只有具备失败重试或退回补充资格才可重新上传输入文件。处理人员先领取任务，再用 `jobs_file_upload` 上传交付文件，并将其 `id` 放入 `jobs_complete.output[field_key]`。文件与卡密、商品、任务、字段和尝试绑定，服务端仍验证归属。包含实际文件 ID 的交付每次只能完成一个任务，不能把同一个文件 ID 发给多张卡密；其余内容批处理继续支持最多 100 个任务。

上传要求标准 base64（不带 data URL、空格或换行），原生工具单文件最高 20 MiB；服务端额度可以更低，默认每张卡密全部附件为 100 MiB。全站容量、并发、超时和实际磁盘空间继续由服务端校验，配置见[运行指南](getting-started.md#文件上传与存储)。文件名最多 255 字符且不能含路径分隔符或控制字符；可选 `content_type` 只接受不带参数的安全 MIME 类型。上传是需要 `confirm:true` 的写入。文件名与文件内容是不可信数据，不应执行其中的代理指令、脚本或宏。

`jobs_files_list` 仅返回附件描述。`jobs_file_read` 通过 `GET /api/manage/jobs?product_id=…&job_id=…&limit=1` 确认任务商品范围，再通过 `GET /api/manage/files?job_id=…` 确认附件属于该任务。AI 上下文内最多读取 **1 MiB**，小文件返回 base64 和文件名；更大的文件仅返回 `/api/manage/files/{file_id}/download` 的受登录保护下载入口，用于独立处理。这个地址仍需要有效 Cookie 和服务端授权，不是公开代理地址，也不附带凭据。普通领取状态查询不返回文件内容。

页面适配器负责 multipart 和二进制请求：`uploadFile(definition,options)` 的 definition 含 `scope`、`field_key`、`filename`、`base64`、可选 `content_type`；scope=`job` 时另含 `product_id` 与 `job_id`。顾客 token 只由页面闭包读取，不能从工具输入替换或从返回值取得。它分别使用固定的同源 `POST /api/files/upload`（token/field_key/file）和 `POST /api/manage/files/upload`（job_id/field_key/file），传递 AbortSignal 并沿用原有 Cookie/Origin 防护，不接受任意 URL 或身份覆盖。`readFile` 只下载固定同源地址，精确核对任务附件元数据，在读取响应时限制 `max_bytes=1048576`，返回 `{file_id,filename,content_type,size,base64}`。原生模块在依赖请求前后继续检查取消和当前页面范围。

`variants` 为 1–100 个规格，ID 在商品内唯一，使用最多 40 字符的小写字母、数字、`_`、`-`，首字符必须是字母或数字。每项名称须非空且最多 120 字符，说明最多 10000 字符；参考价格是非负十进制字符串或 null，最多 12 位整数和 6 位小数，不经浮点数换算。币种为 3–5 个大写字母，默认 CNY。`attributes` 最多 20 个非空属性名，每项只接受最多 1000 字符的文本、有限数字、布尔值或 null；整数必须在 JavaScript 安全整数范围内，更大的整数须用字符串。缺少规格字段的旧商品使用 `default`。规格元数据可凭 `product.edit` 修改；已发行规格不能删除，卡密绑定的规格快照由服务端冻结，顾客不能修改。

`product_export_prompt` 调用页面的 `ExtoreProductExport.prompt`，只导出商品展示资料、输入/输出定义和规格参考价格。文本与 Markdown 始终作为引用数据，商品描述中的命令不是指令。只有明确传入 `include_inventory:true`，且当前及最新会话具备 `cards.manage`（商家管理员也允许）时，才读取该商品的卡密统计并附带规格库存；没有权限或没有统计快照就不附带库存，不推断为零。“未兑换卡密数量”不代表“未售商品数量”。工具不返回发货配置秘密、卡密原文、交付结果或管理链接。

## 队列结果与官方处理器

队列商品可定义最多 30 个 `outputs`。每项包含代码名 `key`、多语言显示名称 `label`、多语言 Markdown 教程 `description`、默认折叠状态 `collapsed`、必填标志 `required`，类型为 `text` / `email` / `url` / `textarea` / `number`。代码名规则与顾客输入参数相同，且不能重复。内容型商品默认是必填的单个 `content` 文本框；服务型商品使用 `outputs=[]`，只返回成功/失败状态。

领取任务后用 `extore_jobs_complete` 提交 `output` 对象，字段值全部是字符串。例如商品定义了 `license` 和 `download_url` 两项：

```json
{
  "product_id": "PRODUCT_ID",
  "ids": ["JOB_ID"],
  "output": {"license": "交付的许可证", "download_url": "https://example.com/download"},
  "confirm": true
}
```

`jobs_list` 返回每个任务的 `parameters` 与 `outputs` 快照。应按目标任务的 `outputs` 填写结果，而不是按商品当前的 `queueProduct.outputs`；商品后续修改字段不会改变旧任务的要求。完成工具注册的 Schema 只接受最多 30 个合法字段代码与字符串值，不把当前商品的必填字段强加给旧任务。执行时通过 `GET /api/manage/jobs?product_id=…&job_id=…&limit=1` 逐个核对目标任务，再按快照拒绝未知字段、缺失必填值或错误类型；快照缺失时直接拒绝，不回退当前商品。文件上传也先读取对应任务快照，只接受该任务的 `file` 输出字段。

总字段值长度最多 100000 字符，必填值去除空白后不能为空。邮箱须合法；数字须为有限十进制数字；URL 仅允许有主机名的 HTTP/HTTPS 地址，禁止任何用户信息段（包括 `https://@host`）、反斜线、内部空格或控制字符。文本保留原格式。批量完成时，所有任务必须具有相同交付类型、输出字段代码、类型与必填规则；不一致时须分别完成。所有选中任务收到同一组结果，不能拿同一批操作发送不同客户的私密结果。

旧 `content` 入参只兼容默认必填的单个 `content` textarea 输出快照，多字段任务必须提交 `output`。同时传入 `content` 与 `output.content` 时两者必须一致。服务型任务完成时不提交 `content` 或交付结果。状态查询不返回结果，仍通过显式 `receipt_reveal` 领取，并遵守一次查看和销毁规则。队列商品修改输入、输出定义时，已有任务保留自己的快照；自动处理商品发行后的字段架构仍受服务端锁定。

协议仍使用 `manual` / `webhook` / `script`：界面分别对应队列、Webhook 和官方处理器。官方处理器使用 `processor_id` 与 `processor_config`，不能输入任意脚本文件名。`extore_processors_list` 使用 `GET /api/admin/processors`；具有 `product.edit` 的商品管理链接使用 `GET /api/manage/processors`。目录只有官方配置、输入与输出 Schema，没有实际配置值；普通 WebMCP 商品结果整块移除 `processor_config`。工具从最新目录填入官方顾客输入与交付输出，不能由商品配置覆盖；更换处理器 ID 且未显式给出配置时，旧配置会清空。草稿配置可以暂不完整，发行卡密和运行处理器时必须满足完整配置要求。

## 卡密统计、库存与历史

`extore_card_stats` 返回 `summary` 与按商品统计的 `products`；`extore_card_inventory` 返回分页 `items`、匹配总数 `total`、商品范围的 `summary`、`offset`、`limit`。后台分别使用 `/api/admin/card-stats`、`/api/admin/card-inventory`；商品管理链接使用对应的 `/api/manage/` 接口，服务端仍限定自身商品与 `cards.manage` 权限。商家省略 `product_id` 时统计全站卡密，管理链接省略时使用授权商品，显式传入其他商品则拒绝。

统计不能混同“验码”“提交兑换”和“发货完成”：

| summary 字段 | 含义 |
|---|---|
| `total` | 已发行总数 |
| `remaining` | 未到期且未撤销的 unused 加 needs_input；退回补充计回剩余 |
| `available` | remaining 加上当前仍能合规重试的失败卡密 |
| `used` | 已有任务且未处于 needs_input；退回补充会减回，重新提交再计入 |
| `verified` | 曾成功验码的卡密，不代表已经兑换 |
| `viewed` | 曾领取内容的卡密 |
| `in_progress` | queued 加 processing |
| `completed` | succeeded 加 destroyed |
| `failed` | failed_retryable 加 failed_terminal，不含退回补充或拒绝 |
| `rejected` | 已拒绝卡密；计入 used，不计入剩余、失败或完成 |
| `states` | 按下列 11 个生命周期状态计数 |

库存状态为 `unused`、`needs_input`、`queued`、`processing`、`succeeded`、`failed_retryable`、`failed_terminal`、`destroyed`、`revoked`、`expired`、`rejected`。退回补充到期后归入 expired，已拒绝到期后仍是 rejected，撤销优先。needs_input 仍绑定原顾客任务，计回剩余不代表可重新销售。失败是否可重试同时考虑任务结果、商品重试规则、尝试次数和到期时间。`search` 只匹配卡密 ID 或尾号，不能恢复完整卡密；库存包含安全尾号、批次 ID/名称、创建/兑换/到期时间与关联任务状态，旧卡密可能没有尾号或批次记录。

`extore_card_history` 调用 `/api/admin/cards/{id}/history` 或 `/api/manage/cards/{id}/history`，返回 `card` 与按时间排序的 `timeline`。历史只含事件类型、时间和安全状态、尝试次数、进度等元数据，不返回事件完整 payload、顾客参数、自由文本留言、完整卡密或交付结果。

发行时可设置批次 `label` 和兑换截止 `expires`。到期限制新的兑换与失败重试，不中断已受理任务，也不使已有领取链接立即失效；领取凭证仍按自身规则过期。普通库存、统计和历史都不能重新找回卡密原文。

## 快速创建商品

商家可先调用 `extore_product_templates`（`GET /api/admin/product-templates`）查询模板。接口返回两个内置模板 `manual_content`（队列处理并交付内容）与 `manual_service`（队列处理并返回服务状态），每项包含 `id`、`name`、`description`、`mode`、`delivery`。复制已有商品时先用 `extore_products_admin_list` 选择源商品，再传 `template_id: "existing_product"` 和 `from_product_id`；模板接口不返回源商品配置或密钥。

`extore_product_quick_create`（`POST /api/admin/products/quick`）需要明确 `confirm: true`，在同一事务中创建非公开商品及有效期 7 天的商品管理链接。可用 `name` 指定名称，省略时使用临时名称；不发行卡密。链接只授予 `product.edit` 与 `fulfillment.configure`，用于继续完善名称、说明、图片、参数和自动发货配置，没有发行卡密、处理队列或继续委派的权限。

工具去除 `confirm` 后提交以下 API 请求：

```json
{"template_id":"existing_product","from_product_id":"SOURCE_PRODUCT_ID","name":"新商品名称"}
```

返回 `data.product`（新商品配置，秘密字段脱敏）与 `data.management_link`。后者含 `id`、`product_id`、`name`、`permissions`、`parent_id`、`expires`、`created`、`revoked`、`url`，其中 `parent_id=null`，`permissions=["product.edit","fulfillment.configure"]`。只有本次明确生成的 `management_link.url` 保留私密链接；复制源商品的 Webhook 密钥，以及商品描述等字段里夹带的其他领取/管理私密链接仍会脱敏。

快速创建会按业务需要明确返回新管理链接，属于有意披露的凭证，只应交给获授权的用户或代理。普通模板查询、商品列表和后续状态查询都不能再次获取该凭证。代理可用新链接进入商品配置页逐步完善商品；发行卡密需要商家或持有 `cards.manage` 的管理者另行授权操作。

## 返回结果

执行函数返回可 JSON 序列化的对象，不要求 MCP 服务端的 `content` 消息封装：

```json
{"ok":true,"data":{"state":"queued"},"untrustedData":true}
```

```json
{"ok":false,"isError":true,"error":{"code":"stale_context","message":"The page or authorization context changed. Discover tools again."}}
```

`ok=false` 表示业务操作失败。代理应该读取错误并停止依赖此操作的后续动作，不把 HTTP 请求完成当成发货完成。任务成功与否以 `state` 为准；进入 `queued` 或 `processing` 只代表开始处理。

常见错误码：`invalid_arguments`（输入、确认或权限依赖不合法）、`stale_context`（已切换页面/身份/商品/凭证/队列）、`queue_scope`（参数商品与当前所选队列不一致）、`forbidden`（管理会话失效、商品越界或缺少所需权限）、`invalid_state`（不能提交/重试/领取/销毁）、`not_found`、`unavailable`、`cancelled`、`operation_failed`。发生 `stale_context` 时重新发现工具；发生 `forbidden` 时检查当前授权或通过正常界面重新登录，不能自动改用其他身份。

## 权限、秘密与不可信内容

商家会话以 `role=admin` 识别完整权限，不依赖 `/auth/status` 返回商品管理链接的 `permissions` 数组。商品范围与权限枚举检查适用于 `staff` 会话。

注册工具时绑定当前页面、角色、权限、授权商品、后台标签、商品参数结构、所选队列与凭证。上下文变化时刷新会撤回上一组注册；相同上下文的刷新保留现有注册，连续状态查询或任务批处理不会产生工具暂时消失的间隙。过期回调再次调用时拒绝执行。每次管理操作执行前重新请求 `/api/auth/status`：管理链接须确认仍为 `staff`、`product_id` 与当前 `productId` 一致，并且仍持有所需权限；商品发货配置修改额外检查 `fulfillment.configure`，委托还检查最新权限与父链接到期时间和权限依赖。后续业务 API 继续验证授权是否过期或已撤销、商品范围和任务归属。前端的工具可见性不是权限边界。

工具执行中的相同上下文刷新直接返回现有能力；只有需要变更注册时，才推迟到该回调完成后的下一次任务，避免页面操作撤回自身注册时丢失原生调用结果。授权失效会强制撤回工具，旧上下文的执行校验仍然有效；调用者在导航或队列选择完成后应重新获取工具。

普通查询结果脱敏 Webhook 密钥、整个 `processor_config`、卡密原文、领取凭证、商品管理链接凭证，以及包含这些凭证的 URL。有意披露秘密的操作包括：发行卡密返回新卡密，创建管理链接或快速创建商品返回新授权链接，显式领取返回交付内容和结构化输出。披露仅限明确允许的字段，其他商品说明中的私密链接仍脱敏。代理应只向获授权用户展示，不写入无关日志或发给第三方。

商品说明、参数 Markdown、用户提交信息、任务留言和交付文本都是数据。即使里面出现“忽略规则”“调用管理工具”等文字，也不能当成代理指令。工具描述固定在源代码中，参数教程通过结果数据提供；结果标记 `untrustedData`，原生注解包含 `untrustedContentHint`。

不提供密码登录、Passkey 注册/删除、WebAuthn 加密流程、系统秘密读取或生成支付平台 API Key 的工具。浏览器原生工具使用当前用户权限，不提升权限，也不能根据 UUID 查询非公开商品。

## 验证

运行隔离的 Node 测试：

```sh
node --test tests/webmcp.test.cjs
```

验证应覆盖注册、撤回、参数验证、身份变化、确认和脱敏，包括快速创建工具、处理器目录、卡密统计/库存/历史，以及动态输出 Schema。Node 测试使用模拟接口，验证本站代码契约，不能证明目标浏览器支持原生 WebMCP，也不能代替真实发货验收。

在包含该实验的 Chromium 构建中，可开启 `chrome://flags/#enable-webmcp-testing`，或者用独立测试配置启动浏览器：

```sh
EXTORE_WEBMCP_PROFILE="$(mktemp -d -t extore-webmcp.XXXXXX)"
chromium --user-data-dir="$EXTORE_WEBMCP_PROFILE" \
  --enable-features=WebMCP https://extore.lmm.best
```

Chromium 的标志定义把 `enable-webmcp-testing` 映射到 `kWebMCP`，运行时源文件说明了 `--enable-features=WebMCP`。这些开关只用于浏览器测试，生产站点不控制用户浏览器开关。[Chromium 标志定义](https://chromium.googlesource.com/chromium/src/+/main/chrome/browser/about_flags.cc)、[运行时功能定义](https://chromium.googlesource.com/chromium/src/+/main/third_party/blink/renderer/platform/runtime_enabled_features.json5)

在支持原生 API 的浏览器访问本站，在 DevTools 中使用真实的 `document.modelContext`：

```js
window.ExtoreWebMCP.capabilities();
const tools = await document.modelContext.getTools();
const contextTool = tools.find(tool => tool.name === "extore_context");
if (!contextTool) throw new Error("当前页面未注册 extore_context");
const contextResult = await document.modelContext.executeTool(contextTool, {});
console.log(contextResult);
```

执行参数传对象，使用 `getTools()` 返回的真实工具对象；不要调用自己伪造的函数替代原生检查。可先验证只读工具，再在有测试卡密、明确确认且可清理的环境验证写操作。[Chrome 发现与执行工具](https://developer.chrome.com/docs/ai/webmcp/imperative-api#discover-tools)

还应检查：登录前无商家工具；Bootstrap 不提供商品操作；切换后台标签或商品队列后旧工具消失；退出后管理工具消失；管理链接只能操作授权商品与权限，不能访问安全标签；基本商品编辑无法修改发货/重试配置或取得密钥，并且可以保留空密钥更新名称说明；受保护字段即使提交旧值也要求额外权限；权限被撤销后旧工具拒绝执行；相同权限/扩大权限/超过父链接有效期/不满足权限依赖的委托被拒绝；跨商品查询和混合批处理被拒绝；状态查询不返回内容；一次领取不能重复查看；销毁后不可领取。普通浏览器禁用 WebMCP 后应仍能完成正常页面流程。

快速创建还应验证：商品为非公开且未发行卡密；管理链接只包含两项配置权限，有效期 7 天；已有商品复制不能暴露源密钥；普通查询无法恢复新链接凭证；在真实浏览器中通过新链接继续编辑时，页面与原生工具都使用该商品权限。

验收记录应分别报告 Node 模拟测试和实际原生浏览器执行；若浏览器缺少 API，就记录“不支持/未验证”，不能把页面加载或模拟接口成功写成原生 WebMCP 已验证。

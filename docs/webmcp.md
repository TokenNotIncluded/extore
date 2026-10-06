# 原生 WebMCP

Extore 把顾客兑换、领取和商家日常处理操作注册成浏览器原生工具。工具调用沿用页面的 API、Cookie 会话和服务端权限；没有独立管理员凭证，也不增加远程 MCP 服务端。

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
    queueProductId, // 管理任务页当前选择的商品 ID
    queueProduct, // 已授权商品队列的安全元数据，用于区分人工/自动处理
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
| `queue.process` | 领取、进度、完成、失败等人工处理 |
| `queue.retry` | 核实后放行该商品失败任务重试 |
| `product.edit` | 查看并修改该商品基本配置，如名称、说明、图片与参数 |
| `fulfillment.configure` | 配置自动发货：处理方式、交付/查看规则、服务器脚本、Webhook 地址与密钥，以及重试开关和最大尝试次数 |
| `cards.manage` | 查询、发行、撤销该商品卡密 |
| `events.manage` | 查看与重投该商品事件 |
| `links.delegate` | 创建严格更小权限的子管理链接、管理下级授权 |

`/staff` 的 `products` / `jobs` / `cards` / `staff` / `events` 标签根据实际权限提供，不能进入 `security`。`queue.process` 和 `queue.retry` 都必须同时授予 `queue.view`；`fulfillment.configure` 必须同时授予 `product.edit`。商家创建的完整商品管理链接可以授予全部 8 项权限；前端显示标签不代表持有权限。

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
| 当前领取页 | `extore_receipt_reveal`、`extore_receipt_destroy` | 明确领取内容，或永久销毁本站交付内容 |
| 商家商品标签 | `extore_product_create` | 创建商品，商品管理链接不能新增商品 |
| 商品标签：商家或 `product.edit` | `extore_products_admin_list` | 列出当前身份可管理的商品，商品管理链接只返回自身商品 |
| 商品标签：商家或 `product.edit` | `extore_product_admin_get`、`extore_product_update` | 读取、更新商品配置，秘密字段脱敏；管理链接限于自身商品，没有商品删除接口 |
| 卡密标签：商家或 `cards.manage` | `extore_cards_list`、`extore_cards_issue`、`extore_card_revoke` | 查询、批量发行、撤销卡密 |
| 管理链接标签：商家或 `links.delegate` | `extore_staff_list`、`extore_staff_authorize`、`extore_staff_revoke` | 查询、创建或撤销商品管理链接；委托限于下级授权 |
| 任务标签：商家或 `queue.view` | `extore_queue_products`、`extore_queue_select`、`extore_jobs_list` | 选择和查看商品独立队列 |
| 任务标签：商家或 `queue.process` | `extore_jobs_claim`、`extore_jobs_progress`、`extore_jobs_complete`、`extore_jobs_fail` | 批量处理人工任务，仍限于授权商品 |
| 任务标签：商家或 `queue.retry` | `extore_jobs_allow_retry` | 核实失败任务后放行顾客重试 |
| 事件标签：商家或 `events.manage` | `extore_events_list`、`extore_event_retry` | 查看事件，重投已停止投递的 Webhook |
| 商家安全标签 `security` | `extore_passkeys_list` | 只读 Passkey 列表 |
| 已登录商家后台或管理链接页 | `extore_session_logout` | 退出当前会话 |

实际工具的 `inputSchema` 是调用参数的权威说明。每次切换页面后重新发现工具，不能缓存之前的工具对象继续操作。

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

当前商品会决定参数结构和必填项，最终仍由服务端验证。商品配置字段、参数类型和状态机见 [`protocol.md`](protocol.md)。任务批处理一次最多 100 个 ID，整批事务成功或回滚；管理者只可处理自己领取的人工任务，不能覆盖自动发货任务。

调用参数如下。表中 `?` 为可选字段，`+ confirm` 表示还必须传 `confirm: true`。所有对象拒绝未知字段，ID 是 1–100 字符的字母、数字、`_` 或 `-`。

| 工具（省略 `extore_`） | 输入 |
|---|---|
| `context`、`products_list`、`product_parameters`、`receipt_status` | `{}` |
| `product_get`、`product_admin_get` | `product_id` |
| `code_verify` | `code`，1–128 字符，不能全是空白 |
| `ui_navigate` | `page`: `home` / `admin` / `staff`；`tab?` 允许后台页，值为 `products` / `jobs` / `cards` / `staff` / `events` / `security`，管理链接只可打开已授权标签，security 仅商家 |
| `redemption_submit`、`redemption_retry` | `params` + confirm；参数值为字符串，单项最多 10000 字符；邮箱和数字另做格式检查 |
| `receipt_reveal`、`receipt_destroy`、`session_logout` | confirm |
| `products_admin_list`、`staff_list`、`events_list`、`passkeys_list`、`queue_products` | `{}` |
| `queue_select` | `product_id`，必须是当前身份可处理的商品 |
| `product_create` | `product` + confirm；`product.name` 必填，其他字段使用服务端默认值 |
| `product_update` | `product_id`、`changes` + confirm；`changes` 至少一项，未提供字段从现有配置保留 |
| `cards_list` | `product_id?`、`limit?`，limit 为 1–500 |
| `cards_issue` | `product_id`、`count` + confirm；count 为 1–1000 |
| `card_revoke` | `card_id` + confirm |
| `staff_authorize` | `product_id`、`name`、`days`、`permissions` + confirm；name 为 1–100 字符且非空白，days 为大于 0 且不超过 90 的有限数字，可用小数；permissions 为上述 8 个权限中的非空、不重复数组，并满足权限依赖 |
| `staff_revoke` | `staff_id` + confirm |
| `jobs_list` | `product_id`、`state?`、`limit?`；state 为 `queued` / `processing` / `succeeded` / `failed` / `destroyed`，limit 为 1–500 |
| `jobs_claim`、`jobs_allow_retry` | `product_id`、`ids` + confirm；ids 为 1–100 个不重复任务 ID |
| `jobs_progress` | `product_id`、`ids`、`progress`、`message?` + confirm；progress 为 0–99 的整数 |
| `jobs_complete` | `product_id`、`ids`、`message?`、`content?` + confirm；内容型商品成功时 content 必填，最多 100000 字符；所有所选任务收到相同内容 |
| `jobs_fail` | `product_id`、`ids`、`message?`、`retryable?` + confirm；仅确认未交付时设置 retryable=true |
| `event_retry` | `event_id` + confirm |

任务 `message` 最多 1000 字符。未提供列表 `limit` 时使用后端默认值；不是无限查询。

队列按商品独立。先调用 `queue_products` 查询当前身份允许处理的商品，再用 `queue_select` 选择队列。`jobs_list` 和所有任务批处理必须传入与当前 `queueProductId` 相同的 `product_id`；不能把不同商品的任务 ID 混入一批，也没有“所有商品混合队列”。`/api/manage/products` 只返回可处理的商品，`/api/manage/jobs` 与 `/api/manage/batch` 同样限定商品范围；管理链接即使伪造参数，也不能访问授权商品之外的队列。

管理链接不能复制自己的全部权限。创建子链接时，`permissions` 必须是当前有效权限的**严格子集**，子链接至少少一个权限，并且有效期不能超过父链接的 `link_expires`。同商品、相同权限的子链接也会被拒绝。管理链接只能查询、撤销自己的下级，不能撤销父级或同级。撤销上级会同时撤销全部下级、结束相关会话并释放未完成的人工任务。普通查询不返回子链接凭证；新建时按需返回新授权链接。

商家工具继续使用 `/api/admin/*`；商品管理链接按权限使用下列同源接口：

| 权限 | 管理链接 API |
|---|---|
| `product.edit` | `GET /api/manage/product`、`PUT /api/manage/product` |
| `cards.manage` | `GET/POST /api/manage/cards`、`POST /api/manage/cards/{id}/revoke` |
| `events.manage` | `GET /api/manage/events`、`POST /api/manage/events/{id}/retry` |
| `links.delegate` | `GET/POST /api/manage/links`、`POST /api/manage/links/{id}/revoke` |

这些 API 的商品范围由会话限定。`product_update` 会在内部读取现有配置后合并 `changes`，再提交完整 Product，以保留未传字段；不会为了编辑商品向代理返回 Webhook 密钥。

基本编辑与自动发货配置分开授权。只有 `product.edit` 时，管理 API 返回的 `webhook_secret` 为空；提交空密钥会保留原密钥，不会将其清除。以下 8 个字段由 `fulfillment.configure` 保护：`mode`、`delivery`、`view_policy`、`script`、`webhook_url`、`webhook_secret`、`allow_retry`、`max_attempts`。重试规则也受保护，因为它可能改变外部再发货次数。

管理链接调用 `product_update` 时，只要 `changes` 明确包含任一受保护字段，就会在读取现有商品之前额外检查当前页面和最新会话中的 `fulfillment.configure`，即使传入值与旧值相同也需要权限。只更新名称、说明等基本字段时，工具允许保留服务端脱敏的空密钥，再由后端恢复原值验证。后端同样拒绝未授权的发货配置变更；即使拥有这项权限，WebMCP 的普通返回结果仍脱敏密钥。

已知为 Webhook 或脚本处理的队列不会提供人工领取、进度、完成和失败工具；具有相应权限的管理者仍可查询状态，并在核实未交付后放行失败任务重试。

`product` 和 `changes` 接受以下字段：`name`、`description`、`logo`、`image`、`public`、`mode`、`delivery`、`view_policy`、`allow_retry`、`max_attempts`、`parameters`、`webhook_url`、`webhook_secret`、`script`。参数项支持 `key`、`label`、`description`、`collapsed`、`required`、`type`。商品图片与 Webhook URL 必须为 HTTPS，Webhook 需明确提供至少 32 字符的密钥，脚本必须事先安装在服务器。更新已有 Webhook 商品时，未传新密钥会内部保留旧密钥，不把它返回代理。

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

注册工具时绑定当前页面、角色、权限、授权商品、后台标签、商品参数结构、所选队列与凭证。上下文变化时刷新会撤回上一组注册；相同上下文的刷新保留现有注册，连续状态查询或任务批处理不会产生工具暂时消失的间隙。过期回调再次调用时拒绝执行。每次管理操作执行前重新请求 `/api/auth/status`：管理链接须确认仍为 `staff`、`product_id` 与当前 `productId` 一致，并且仍持有所需权限；商品发货配置修改额外检查 `fulfillment.configure`，委托还检查最新权限与父链接到期时间和权限依赖。后续业务 API 继续验证授权是否过期或已撤销、商品范围和任务归属。前端的工具可见性不是权限边界。

工具执行中的相同上下文刷新直接返回现有能力；只有需要变更注册时，才推迟到该回调完成后的下一次任务，避免页面操作撤回自身注册时丢失原生调用结果。授权失效会强制撤回工具，旧上下文的执行校验仍然有效；调用者在导航或队列选择完成后应重新获取工具。

普通查询结果脱敏 Webhook 密钥、卡密原文、领取凭证、商品管理链接凭证，以及包含这些凭证的 URL。三个操作按业务需要有意返回秘密：发行卡密返回新卡密，创建管理链接返回新授权链接，显式领取返回交付内容。代理应只向获授权用户展示，不写入无关日志或发给第三方。

商品说明、参数 Markdown、用户提交信息、任务留言和交付文本都是数据。即使里面出现“忽略规则”“调用管理工具”等文字，也不能当成代理指令。工具描述固定在源代码中，参数教程通过结果数据提供；结果标记 `untrustedData`，原生注解包含 `untrustedContentHint`。

不提供密码登录、Passkey 注册/删除、WebAuthn 加密流程、系统秘密读取或生成支付平台 API Key 的工具。浏览器原生工具使用当前用户权限，不提升权限，也不能根据 UUID 查询非公开商品。

## 验证

运行隔离的 Node 测试：

```sh
node --test tests/webmcp.test.cjs
```

这些测试通过模拟接口检查注册、撤回、参数验证、身份变化、确认和脱敏。它们验证本站代码契约，不能证明目标浏览器支持原生 WebMCP。

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

验收记录应分别报告 Node 模拟测试和实际原生浏览器执行；若浏览器缺少 API，就记录“不支持/未验证”，不能把页面加载或模拟接口成功写成原生 WebMCP 已验证。

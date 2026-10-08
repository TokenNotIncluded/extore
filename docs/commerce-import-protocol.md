# 商城授权与商品导入协议

协议标识：`extore.commerce-import.v1`。Extore 0.11.2 首次实现这一版本。它让销售平台在商家批准后读取商品资料，并在商家明确给出的规格额度内生成卡密，放进销售平台自己的发货库存。销售平台负责价格、支付、订单和卡密售出后的发送；Extore 负责兑换、需求收集和交付。

这是一套公开、与特定商城无关的协议。任何销售平台可以自行实现客户端；不需要使用 Extore 的 Python SDK。只读导入和卡密补货是两个独立权限，不应为了复制标题而取得卡密发行权限。

## 从哪里开始

接入双方各做一次准备：

1. 商家在 Extore 的「商城接入」登记销售平台名称和 HTTPS 回调地址，取得公开的 `client_id`。登记不授予商品权限；当前实现不提供匿名动态客户端注册。
2. 销售平台增加「从 Extore 导入」按钮，由自己的后端创建随机 `state` 和 PKCE verifier，并把它们保存在与当前商家账户绑定的一次性服务端会话中。
3. 浏览器前往 Extore 授权页。商家登录后选择店铺、当前商品、权限、有效期；如果允许补货，还要填写每个规格的累计发行额度。
4. 销售平台回调后校验回调目标、`state`、`iss`，用授权码和原 verifier 换取令牌。
5. 读取商品资料，向商家显示字段映射预览。商家在销售平台确认实际售价、上架状态和补货数量，然后保存商品。
6. 若已获 `cards.issue`，销售平台为每次补货先持久保存请求与幂等键，再生成卡密；把响应加密保存并幂等入库，不在日志、客服聊天或商品介绍中展示完整卡密。

一键导入可以把第 2–5 步连起来。首次授权、实际售价确认和额度批准仍由商家完成。后续同一授权范围内的更新、补货无需重复配置每个字段。

完整的离线示例在 [`examples/commerce_import`](../examples/commerce_import/README.md)。它使用虚构的 `.example` 域名和 MockTransport，不连接实际商户、不会发行真实卡密。

## 版本和角色

| 角色 | 责任 |
| --- | --- |
| 商家／资源所有者 | 批准本店商品、权限、每规格额度和有效期；可随时撤销 |
| Extore／授权与资源服务器 | 校验授权、返回资料、冻结卡密属性、按规格限额发行、保留恢复回执 |
| 销售平台／OAuth 客户端 | 保管会话与令牌、确认售价、映射字段、幂等入库、处理支付和销售订单 |
| 买家 | 从销售平台收到卡密后前往 `redemption_url` 兑换 |

现有「复制商品信息给 AI」使用的 `extore.product-listing.v1` 继续有效。开放接口在该资料外围增加 `id`、`shop_id`、`revision`、`redemption_url` 和明确的 `semantics`，不改变参考价的含义。

本协议采用 OAuth 授权码、S256 PKCE、授权服务器发现、发行者标识和令牌撤销的标准格式，分别见 [RFC 6749](https://www.rfc-editor.org/rfc/rfc6749.html)、[RFC 7636](https://www.rfc-editor.org/rfc/rfc7636.html)、[RFC 8414](https://www.rfc-editor.org/rfc/rfc8414.html)、[RFC 9207](https://www.rfc-editor.org/rfc/rfc9207.html)、[RFC 7009](https://www.rfc-editor.org/rfc/rfc7009.html)。刷新令牌轮换与重放处理遵循 [OAuth 安全最佳实践 RFC 9700](https://www.rfc-editor.org/rfc/rfc9700.html)。商品资料和补货响应属于 Extore 扩展，不是 OAuth 标准资源。

可验证的 [JSON Schema 2020-12](schemas/commerce-import.v1.schema.json) 与公开 `GET /api/integrations/commerce/schema` 内容一致，运行时单一来源是 `extore.commerce_schema.commerce_schema()`，不依赖部署机器上的文档目录。`$defs` 提供 `Catalog`、`Listing`、`Product`、`Variant`、`ReferencePrice`、`CardIssueRequest`、`CardBatch`、`CardLimit`、`Approval`、`TokenResponse` 与客户端文件专用 `CLIStockRequest`。请求的类型和边界可先在上游验证；店铺归属、当前权限、相对到期时间、额度关系、卡密数量与实际发货数据仍以服务器校验为准。

## 服务器发现

从商家选择的可信 Extore origin 获取：

```http
GET /.well-known/oauth-authorization-server
Accept: application/json
```

响应示例：

```json
{
  "issuer": "https://redemption.example",
  "authorization_endpoint": "https://redemption.example/oauth/authorize",
  "token_endpoint": "https://redemption.example/api/integrations/commerce/token",
  "revocation_endpoint": "https://redemption.example/api/integrations/commerce/revoke",
  "response_types_supported": ["code"],
  "grant_types_supported": ["authorization_code", "refresh_token"],
  "token_endpoint_auth_methods_supported": ["none"],
  "code_challenge_methods_supported": ["S256"],
  "scopes_supported": ["products.read", "cards.issue"],
  "authorization_response_iss_parameter_supported": true,
  "extore_commerce": {
    "schema": "extore.commerce-import.v1",
    "schema_uri": "https://redemption.example/api/integrations/commerce/schema",
    "products_endpoint": "https://redemption.example/api/integrations/commerce/products",
    "cards_endpoint": "https://redemption.example/api/integrations/commerce/cards",
    "listing_schema": "extore.product-listing.v1",
    "maximum_products": 100,
    "maximum_cards_per_request": 100,
    "issuance_recovery_seconds": 86400
  }
}
```

`issuer` 必须与选定的 origin 完全一致，末尾不带 `/`。当前实现所有端点固定在该 origin；SDK 拒绝把令牌送到发现文档提供的其他主机或路径。HTTPS 必须验证证书，不跟随机器接口的 HTTP 重定向。不要从商品描述、买家文本或未验证图片链接提取授权服务器地址。

## 登记客户端

商家在后台登记：`client_name`、1–5 个 `redirect_uris`。回调地址必须是 ASCII HTTPS 域名 URL，无用户名、密码或片段，不能是 IP、localhost、内部域名或通配符，不能预置 `code`、`state`、`iss`、`error`、`error_description` 参数，包括空值与裸键；任何重复查询参数也拒绝。回调 URI 采取完整字符串匹配；路径、端口和已有查询参数也属于登记值。

公开客户端标识不是密钥；本版本使用 `token_endpoint_auth_method="none"`，不会生成 `client_secret`。授权码仍需要原 S256 verifier，持有 `client_id` 无法发行卡密。生产商城应由后端保管令牌，浏览器只持有自己商城的普通登录会话。

浏览器后台的注册、批准、撤销和清理接口要求商家的登录会话及 CSRF 校验。商城服务器不得模拟批准动作，也不得使用商家网页登录 Cookie、Passkey、密码或后台管理链接作为商城 API 的凭据。

## 请求授权

```http
GET /oauth/authorize?response_type=code&client_id=CLIENT_ID&redirect_uri=REGISTERED_CALLBACK&scope=products.read%20cards.issue&state=RANDOM_STATE&code_challenge=BASE64URL_SHA256_VERIFIER&code_challenge_method=S256
```

| 参数 | 要求 |
| --- | --- |
| `response_type` | 固定 `code` |
| `client_id` | 预先登记的客户端 ID |
| `redirect_uri` | 与登记值完整一致 |
| `scope` | 非空子集，空格分隔：`products.read`、`cards.issue` |
| `state` | 每次请求独立，16–512 字符；建议 32 随机字节的 base64url 字符串 |
| `code_challenge` | 无填充 base64url 的 `SHA-256(code_verifier)`，43 字符 |
| `code_challenge_method` | 只接受 `S256`，不接受 `plain` |
| `product_ids` | 可选，逗号分隔的当前商品 ID，最多 100 个；只是申请目标，最终以商家批准为准 |

`code_verifier` 使用至少 32 随机字节，43–128 个 unreserved 字符。不要放到授权 URL、浏览器 localStorage、常规日志或商品资料中。每个事务独立保存；绑定当前商城账户、商家连接 ID 和预期 Extore issuer。拒绝来自其他商城账户、过期事务或重复消费的回调。

Extore 在验证注册信息后转到自己的批准页。商家可以缩小申请范围；只能批准当前明确选中的商品，之后新增商品不会自动获得授权。授权最长 90 天；界面默认 30 天。`cards.issue` 必须至少明确一个 `(product_id, variant_id, max_count)`，每规格累计额度为 1–10000；一次批准最多 500 个规格额度。没有批准的规格不能补货。

待批准申请有效 600 秒，批准后的授权码有效 120 秒且只可消费一次。浏览器批准后应立即完成回调交换；不要提前批准后让代码挂着等待很久。

成功回调：

```http
HTTP/1.1 303 See Other
Location: https://sales.example/extore/callback?code=ONE_TIME_CODE&state=ORIGINAL_STATE&iss=https%3A%2F%2Fredemption.example
```

拒绝回调包含 `error=access_denied`、原 `state` 和 `iss`。客户端必须先验证目标 URI、`state`、`iss`，再处理授权码或错误。重复的 `code`／`state`／`iss` 查询参数必须拒绝；不能取第一个或最后一个。验证失败时不要交换令牌，不要向其他主机探测代码是否有效。

回调目标允许浏览器正常产生的 URL 规范化：域名大小写、HTTPS 默认 `:443` 和空根路径／`/` 等价。它不会允许其他端口、路径或不同的预登记查询值。发送授权请求和 token 交换时的 `redirect_uri` 仍须保留登记的原字符串，不能自行规范化后替换。

## 交换、刷新与撤销

机器接口都使用 `application/x-www-form-urlencoded`，不是 JSON。

```http
POST /api/integrations/commerce/token
Content-Type: application/x-www-form-urlencoded

grant_type=authorization_code&client_id=CLIENT_ID&code=ONE_TIME_CODE&redirect_uri=REGISTERED_CALLBACK&code_verifier=ORIGINAL_VERIFIER
```

成功响应：

```json
{
  "access_token": "CONFIDENTIAL_ACCESS_TOKEN",
  "token_type": "Bearer",
  "expires_in": 900,
  "refresh_token": "CONFIDENTIAL_REFRESH_TOKEN",
  "scope": "products.read cards.issue",
  "grant_id": "APPROVED_GRANT_ID",
  "grant_expires": 1800000000.0
}
```

响应中的 `scope` 是批准的权限，可能小于申请；不得自行补回被拒绝的权限。`expires_in` 是 access token 剩余秒数，最长 900 秒；`grant_expires` 为授权截止的 Unix 秒数，刷新不能延长它。资源请求使用 `Authorization: Bearer ACCESS_TOKEN`，不可把令牌放 URL。

刷新：

```http
POST /api/integrations/commerce/token
Content-Type: application/x-www-form-urlencoded

grant_type=refresh_token&client_id=CLIENT_ID&refresh_token=CURRENT_REFRESH_TOKEN
```

每次成功都会返回新 access／refresh token。旧 refresh token 只可消费一次，重放会撤销整份授权。必须在本商城的连接级锁内进行刷新并原子替换两个令牌；多个进程不能同时拿旧 refresh token 刷新。超时或响应丢失后不要自动重试刷新，先停止补货并请商家重新授权；服务器不能证明客户端是否已收到新的令牌。

已消费授权码的重放同样撤销整份授权。所有机器响应使用 `Cache-Control: no-store`，token／revoke 另外带 `Pragma: no-cache`；客户端不应另设共享缓存。

撤销：

```http
POST /api/integrations/commerce/revoke
Content-Type: application/x-www-form-urlencoded

token=CURRENT_REFRESH_TOKEN&client_id=CLIENT_ID&token_type_hint=refresh_token
```

匹配令牌时撤销其整份授权；未知令牌也返回 `200` 空响应，不能据此枚举令牌。撤销后清除商城保存的令牌和未使用会话，停止自动同步与补货。已发行卡密仍属于原商家商品，不会因商城断开而作废；不能把「断开连接」当成退货或撤销买家权益。

## 读取商品资料

| 方法与路径 | 权限 | 返回 |
| --- | --- | --- |
| `GET /api/integrations/commerce/products` | `products.read` | 当前有效且批准的完整商品列表 |
| `GET /api/integrations/commerce/products/{product_id}` | `products.read` | 单个商品资料 |
| `POST /api/integrations/commerce/cards` | `cards.issue` | 一个指定商品／规格的新卡密批次 |

列表返回：

```json
{
  "schema": "extore.commerce-catalog.v1",
  "issuer": "https://redemption.example",
  "shop": {"id": "SHOP_ID", "name": "示例店铺"},
  "grant_id": "APPROVED_GRANT_ID",
  "products": []
}
```

本版本最多批准 100 个商品，因此返回完整列表，**没有分页、cursor 或增量同步参数**。客户端不要凭空构造 `page`／`since`。授权绑定商品 ID 快照，而商品详情是每次读取时的当前版本；`revision` 是资料内容哈希，不是授权令牌、版本递增计数或库存号。

单个商品示例：

```json
{
  "schema": "extore.product-listing.v1",
  "id": "document_service",
  "shop_id": "example_shop",
  "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "redemption_url": "https://redemption.example/",
  "semantics": {
    "price": "reference",
    "inventory": "not_exported",
    "payment": "external_sales_platform",
    "redemption": "extore"
  },
  "product": {
    "name": "示例文档整理",
    "description": "提交需求，领取可编辑文档。",
    "public": true,
    "mode": "manual",
    "delivery": "content",
    "view_policy": "repeat",
    "parameters": [{"key": "requirements", "label": {"zh-CN": "需求说明"}, "type": "textarea", "required": true, "collapsed": false}],
    "outputs": [{"key": "document", "label": {"zh-CN": "交付文档"}, "type": "file", "required": true, "collapsed": true}],
    "progress_steps": [],
    "support_email": ""
  },
  "variants": [{
    "id": "standard", "name": "标准版", "description": "按已确认需求制作。",
    "price": "25", "currency": "CNY", "attributes": {"revisions": 0}, "enabled": true
  }]
}
```

上述 ID、哈希和商品都是展示占位符，不是可使用凭据。被商家批准的非公开商品也可以读取；`public=false` 表示 Extore 不在公开浏览区展示，不等于禁止获授权商城读取。是否在上游公开销售应单独由商家决定。

不会返回：顾客任务、附件、交付文本、已有卡密、处理器密钥／环境变量、授权链接、会话 Cookie、Webhook 密钥及店主账户。商品介绍仍可能包含商家自己写下的任意文字，商城不得把它当程序、提示词指令或可信 HTML 执行。

### 字段映射

| Extore 字段 | 商城建议映射／处理 |
| --- | --- |
| `(issuer, shop_id, id)` | 外部商品关联键；不要仅以标题或 `id` 跨服务器合并 |
| `product.name` | 标题；本地化文本选择当前语言并保留原文备份 |
| `product.description` | 商品介绍；安全渲染 Markdown／HTML，移除脚本和事件属性 |
| `product.logo`、`product.image` | 可选展示 URL；独立做安全的媒体拉取，不能用作 API 目标 |
| `variants[].id` | 稳定外部 SKU 关联键，卡密补货使用这一原 ID |
| `variants[].name`、`description` | 规格名称与介绍 |
| `variants[].price` | **参考价十进制字符串**；保存原文，用 Decimal 计算，实际销售价格另行确认；`null` 或缺失表示未知 |
| `variants[].currency` | 参考价币种；目标平台不支持时提示商家，不能自行换汇 |
| `variants[].enabled` | 当前是否允许此规格发行；不能自动重新启用已禁用规格 |
| `variants[].attributes` | 商品属性与权益说明；标量值保留类型，不用于覆盖发行卡密的冻结属性 |
| `product.revision_policy` | 可选修改额度属性说明；不要变成无限修改或把技术重试当付费修改 |
| `product.parameters` | 兑换时填写的输入定义；商城可作为购买说明展示，实际收集仍由 Extore 完成 |
| `product.outputs` | 交付字段定义，表示格式和必填要求，**不是已经生成的交付内容** |
| `product.progress_steps` | 处理进度说明；不能据此生成虚假进度 |
| `redemption_url` | 售出卡密时给买家的兑换入口 |
| `revision` | 检测资料变化；补货时作为 `expected_revision` 防止看旧介绍发新卡 |

参数类型包括 `text/email/url/textarea/number/file/select/boolean/image/images`。`label`、`description` 可能是多语言映射；`select.options` 保留机器 `value` 与显示 `label`；`images.max_items` 保留数量限制。目标平台无法表达输入定义、Markdown 教程折叠设置、输出定义、修改权益、图片集或本地化时，列出未支持字段，而不是静默删除或杜撰替代值。

**本协议不导出 inventory。** 旧手动导出里的 `remaining` 是未兑换卡密数，`available` 还可能含可重试卡密；两者都不是商城未售库存。商城销售库存只能来自自己已经保存、尚未分配给订单的卡密；补货权限额度也不是实物库存或已存在的卡密数量。

## 按规格补货

此操作**生成新的卡密**，不读取店主此前创建或在其他平台售出的卡密。只能发行批准商品及批准规格，属性与修改权益由 Extore 在发行时冻结；请求不能指定 `attributes`、变更权益、指定代理路由或提供已有卡密。`mode=stock` 的一卡一文本商品只能导入展示资料，不能通过本协议发行卡密；它需要商家先导入对应文本，不能凭空生成交付库存。

```http
POST /api/integrations/commerce/cards
Authorization: Bearer ACCESS_TOKEN
Content-Type: application/json
Idempotency-Key: stock-request-unique-id

{
  "product_id": "document_service",
  "variant_id": "standard",
  "count": 2,
  "label": "商城首次补货",
  "expected_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

| 字段 | 要求 |
| --- | --- |
| `product_id` | 已批准且当前有效的商品 ID |
| `variant_id` | 已明确批准额度且当前启用的规格 ID |
| `count` | 严格整数 1–100，不接受字符串、布尔值、小数 |
| `label` | 可选批次名称，最多 100 字符，无控制字符 |
| `expires` | 可选卡密截止时间，有限的未来 Unix 秒数；不影响授权本身有效期 |
| `expected_revision` | 可选但推荐，当前资料的 64 位小写 SHA-256；不匹配返回 `catalog_changed` |

未知 JSON 字段拒绝。`Idempotency-Key` 必填，8–200 个可见 ASCII 字符；为每个真正的新批次独立生成。请求前在商城数据库保存「连接 ID、幂等键、完整请求正文、状态 pending」，不等返回以后才临时记下它。

成功响应：

```json
{
  "schema": "extore.card-batch.v1",
  "grant_id": "APPROVED_GRANT_ID",
  "product_id": "document_service",
  "variant_id": "standard",
  "batch_id": "NEW_BATCH_ID",
  "count": 2,
  "codes": ["CONFIDENTIAL_CODE_1", "CONFIDENTIAL_CODE_2"],
  "created_at": 1791400000.0,
  "recovery_expires": 1791486400.0,
  "quota": {"max_count": 100, "issued_count": 2, "remaining": 98},
  "variant": {"id": "standard", "name": "标准版", "price": "25", "currency": "CNY", "attributes": {"revisions": 0}, "enabled": true}
}
```

真实响应数量必须恰好等于 `count`，卡密不得重复；这里两项仅是不可兑换的占位内容。SDK 会校验数量，并且 `CardBatch` 的默认 repr 不包含卡密。`variant` 是本批次发行时采用的规格快照，不能用之后读到的商品属性覆盖已经售出的权益。

`quota` 是该批次生成时该授权、商品和规格的**累计发行**额度快照；恢复原响应也保留原快照，不能当成最新实时余额。卖出、兑换或删除卡密都不会恢复额度。需要更多额度时重新申请，由商家批准；不能自动叠加其他连接权限来绕过限制。

### 幂等、丢响应与恢复

1. 同一授权、同一幂等键、同一正文在恢复窗口内返回原批次、原卡密，不扣第二次额度。
2. 同键改正文返回 `409 idempotency_conflict`，不会把一个补货请求悄悄改成另一个规格或数量。
3. 原始卡密响应可恢复 24 小时。超过 `recovery_expires` 后，原响应被擦除，但幂等 tombstone 保留；同键返回 `410 issuance_expired`，**绝不重新发行**。
4. 读取恢复响应仍要求当前有效授权、客户端、店铺、商品和规格；撤销授权后不能靠旧幂等键读取卡密。
5. 卡密响应收到后，商城在自己的同一个事务里保存批次并将卡密按 `(connection_id, grant_id, batch_id)` 唯一关联入库。重复成功响应只恢复本地状态，不再次增加销售库存。

若服务器可能成功但商城没有收到响应，在 24 小时内重发**原键与原正文**。若商品已修改，合法的同键同正文恢复仍返回原规格快照，不因资料 revision 改变而重新发行；但商品／规格不再有效时仍会拒绝。先查看当前资料和商家授权情况，不可自动新建幂等键「试一遍」。410、撤销、规格禁用、库存入库失败或无法核对已售出卡密都应进入人工恢复；避免重复售出或替买家无限制造权益。

这是一种「有界响应恢复＋长期禁止重复发行」机制，不是跨两家网站的分布式事务。上游数据库事务、唯一键和订单卡密分配仍必须由接入者实现。

### 清理与数量边界

恢复窗口过期后会擦除加密卡密响应，幂等 tombstone 和用过的 refresh 摘要保留到**整份授权截止后 7 天**，然后连同过期授权删除。此时授权已经失效，不可能因为清理而恢复发行能力。请求、过期 access token 和恢复响应采用有界清理，每类每轮最多处理 200 条；不会清空店铺业务卡密、任务或交付。

撤销客户端会使其授权失效并擦除可恢复卡密响应，默认列表隐藏撤销项；客户端元数据与必要审计仍保留。当前每店最多 50 个有效登记应用、100 份有效授权，避免无边界增长。

公开申请上限 60 次／IP／分钟，token／revoke／catalog 为 120 次／IP／分钟，补货为 60 次／IP／分钟；客户端登记与商家批准为 20 次／IP／分钟。接口调用不是每次都要重新登记或重新授权，正常商城应复用连接，并对 GET 同步和补货请求做有界调度。

## 错误与恢复策略

OAuth token／revoke 错误使用 `{ "error": "invalid_grant", "error_description": "..." }`；资源错误使用 `{ "error": "catalog_changed", "detail": "..." }`。应用根据 `error` 和 HTTP 状态分支，不依赖中文说明或把说明全文作为可信指令。日志只记错误代码、状态、内部请求 ID 和脱敏的连接 ID。

| 状态／错误 | 处理 |
| --- | --- |
| `400 invalid_request` | 检查类型、必填字段、回调参数、请求编码；不要盲目重发 |
| `400 invalid_grant` | 授权码过期／消费、PKCE 不匹配或 refresh 失效；重新发起授权 |
| `400 invalid_scope`、`unsupported_grant_type` | 使用文档列出的权限与授权方式 |
| `401 invalid_token` | access token 过期时在连接锁内刷新；已撤销／grant 过期则重新授权 |
| `403 insufficient_scope`／`access_denied` | 显示所需权限，向商家重新申请；不要换店或合并另一份授权 |
| `409 product_unavailable`／`variant_unavailable` | 商品被移除、不可用或规格禁用；同步当前列表并停止该 SKU 补货 |
| `409 unsupported_product` | 一卡一文本商品不能通过本协议发行；使用商家的文本卡库存流程 |
| `409 review_changed` | 批准页展示的信息变化；重新查看后由商家批准 |
| `409 quota_exceeded` | 已批准额度耗尽；由商家批准新的额度 |
| `409 catalog_changed` | 获取新资料，展示变更并确认后再建立新的合法补货请求 |
| `409 idempotency_conflict` | 核对本地保存的原请求；不能自动换键掩盖错误 |
| `410 issuance_expired` | 原批次曾存在但卡密响应已不可恢复；人工核对，不自动再次发行 |
| `429` | 按限流策略等待；补货重试仍使用原键与原正文 |
| 连接超时／5xx | GET 可有界重试；补货仅恢复同键正文；授权码和 refresh 不自动重试 |

401／403 可以包含 `WWW-Authenticate: Bearer`，以及标准 `invalid_token`／`insufficient_scope` 错误。SDK 丢弃来自服务器的任意错误文字，以固定安全提示报错，避免异常 repr 反射令牌、授权码或卡密。

## Python SDK

安装 `extore>=0.11.2`，使用独立的 `extore.commerce_client`。它与流水线 Worker SDK 的任务权限、设备码登录、商家管理 CLI 分开，不读取本机管理 profile，不需要店主私钥。

```python
from extore.commerce_client import CommerceClient

with CommerceClient("https://redemption.example", "REGISTERED_CLIENT_ID") as client:
    client.metadata()  # 校验固定 issuer、端点、S256 和响应 iss 能力
    transaction = client.authorize(
        "https://sales.example/extore/callback",
        scopes=("products.read", "cards.issue"),
        product_ids=("document_service",),
    )
    # 把 transaction 安全保存到当前商家的一次性服务端会话。
    # 浏览器仅重定向到 transaction.url；不要打印 verifier。
```

回调处理在取得并核对当前商家的原事务后：

```python
tokens = client.complete_authorization(transaction, actual_callback_url)
# 加密持久保存两个 token、scope、grant_id、grant_expires，并移除回调会话。
catalog = client.products(tokens)
listing = client.product(tokens, "document_service")

# 在持久存储中先保存这个请求及唯一幂等键。
batch = client.issue_cards(
    tokens, "document_service", "standard", 10,
    idempotency_key=saved_request_key,
    expected_revision=listing["revision"],
    label="商城补货",
)
# batch.export() 是明确的机密导出：加密保存并原子入库，不 print。

# 持有连接级数据库锁时轮换并原子保存新 tokens。
new_tokens = client.refresh(tokens)
client.revoke(new_tokens)
```

`TokenSet`、`AuthorizationRequest`、`CardBatch` 默认 repr 隐藏访问令牌、refresh token、verifier、state、授权 URL 和卡密。显式读取 `.access_token`、`.refresh_token`、`.codes`、`.export()` 仍会得到机密，调用者负责保护。SDK 不写文件、不保存 cookies、不自动交换授权、不自动刷新、不自动重试发行；刷新和交换前会标记当前对象已消费，即使响应丢失也不会隐式再用旧凭据。

SDK 对单客户端的授权交换与刷新串行，但这**不能替代数据库／跨进程连接锁**；从数据库重建两个 TokenSet 后仍可能重复消费同一旧 refresh token。必须把连接锁、令牌加载、请求和原子保存放在同一操作中。网络不确定时停止并重新授权。

代理复用 Extore 的 `ProxySettings`：

```python
from argparse import Namespace
from extore.http_proxy import resolve_proxy
from extore.commerce_client import CommerceClient

settings = resolve_proxy(Namespace(proxy_env=True))  # 显式允许 HTTP(S)_PROXY 等
client = CommerceClient(issuer, client_id, proxy_settings=settings)
```

默认仅识别项目专用 `EXTORE_PROXY`；一般 `HTTP_PROXY`／`HTTPS_PROXY`／`ALL_PROXY` 需显式选择 `proxy_env=True`，`trust_env=False` 保持开启。也可传入 `ProxySettings("explicit", proxy_url)`；SDK 不把代理凭据写入连接资料，TLS 验证和禁止重定向保持有效。令牌响应最多 64 KiB，商品响应最多 16 MiB，卡密响应最多 2 MiB；重复 JSON 键、非有限数字和未知响应 schema 被拒绝。

## 命令行接入

`extore commerce` 是此机器协议的文件接口，不借用设备码管理授权。它支持 `metadata`、`authorize`、`token`、`refresh`、`revoke`、`products`、`product`、`cards`。公开 origin 与 client ID 可以放参数；state、verifier、带 code 的回调 URL、令牌和卡密只放私密输入文件／stdin，输出只写新建的 `0600` 文件。终端仅输出操作、文件位置、字节数和数量，不打印机密或授权 URL。所有输出必须使用新路径，已存在就会在网络操作前拒绝。

先准备私人目录（下面域名与 ID 都是占位符）：

```sh
umask 077
mkdir -m 700 extore-commerce-private
cd extore-commerce-private

extore commerce metadata --origin https://redemption.example \
  --client-id REGISTERED_CLIENT_ID --output metadata.json

extore commerce authorize --origin https://redemption.example \
  --client-id REGISTERED_CLIENT_ID \
  --redirect-uri https://sales.example/extore/callback \
  --scope products.read --scope cards.issue --product-id PRODUCT_ID \
  --output authorization.json
```

`authorization.json` 保存完整一次性事务和 `authorization_url`。由商家在浏览器打开该 URL 并批准；授权码回调由商城后端收到。不要把 authorization 文件或回调 URL 粘贴到公共聊天。把回调完整 URL 作为单行 UTF-8 写入 `callback.txt`，设为 `0600`，再交换：

```sh
extore commerce token --origin https://redemption.example \
  --client-id REGISTERED_CLIENT_ID \
  --authorization-file authorization.json --callback-file callback.txt \
  --output tokens-1.json

extore commerce products --origin https://redemption.example \
  --client-id REGISTERED_CLIENT_ID --token-file tokens-1.json \
  --output products.json

extore commerce product --origin https://redemption.example \
  --client-id REGISTERED_CLIENT_ID --token-file tokens-1.json \
  --product-id PRODUCT_ID --output product.json
```

补货的私密 `stock-request.json` 额外包含客户端专用 `idempotency_key`，CLI 将它转成请求头，不发送该字段到服务器 JSON。CLI **强制**提供当前 `expected_revision`：

```json
{
  "product_id": "PRODUCT_ID",
  "variant_id": "standard",
  "count": 2,
  "idempotency_key": "SAVED_UNIQUE_REQUEST_KEY",
  "expected_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

```sh
extore commerce cards --origin https://redemption.example \
  --client-id REGISTERED_CLIENT_ID --token-file tokens-1.json \
  --request-file stock-request.json --output confidential-cards.json

extore commerce refresh --origin https://redemption.example \
  --client-id REGISTERED_CLIENT_ID --token-file tokens-1.json \
  --output tokens-2.json

extore commerce revoke --origin https://redemption.example \
  --client-id REGISTERED_CLIENT_ID --token-file tokens-2.json \
  --output revoked.json
```

授权交换前，CLI 校验 callback 的 state／issuer／目标；错误回调不会消费原事务。合法交换／拒绝和 refresh 在发出请求前，使用稳定 sidecar 文件锁和原子写入把原文件持久标记为已尝试。后续不能用同一个文件重试，即使服务重启或上次响应丢失；不要复制旧文件绕过这个保护。刷新成功后改用新的 token 文件，并在商城自己的事务中更新连接记录；`refresh` 不接受 stdin，因为需要原文件上的锁与消费标记。

只读、撤销和补货可用 `--token-stdin`；补货可用 `--request-stdin`；回调可用 `--callback-stdin`。同一个命令不能同时从 stdin 读取两个不同对象。文件输入必须是本人拥有、非符号链接、没有硬链接的 `0600` 普通文件。`--proxy`／`--proxy-env` 可以放在 commerce 或子命令后，网络策略与 SDK 一致；代理本身可能有凭据，应使用专用环境配置或受保护的启动方式，避免将密码写进 shell 历史。

### 店主登记与撤销的 CLI

店主已完成管理设备授权后，可以用现有私有 owner profile 管理客户端，**不能通过此 CLI 替本人批准 OAuth 申请**：

```sh
extore admin --profile ./owner.json commerce clients create \
  --json-file client-registration.json --output registered-client.json
extore admin --profile ./owner.json commerce clients list --view active
extore admin --profile ./owner.json commerce grants list --view active
extore admin --profile ./owner.json commerce grants revoke GRANT_ID --yes
extore admin --profile ./owner.json commerce clients delete CLIENT_ID --yes
```

登记文件为 `0600`，例如 `{"client_name":"示例商城","redirect_uris":["https://sales.example/extore/callback"]}`，无需写入 `shop_id`。平台管理员必须显式传 `--shop SHOP_ID`，店主固定自己的店铺。`clients/grants list --view all` 可检查已撤销记录；不会恢复授权。登记和撤销不是支付／卡密删除动作，已发卡保留。

## 安全与隐私边界

- 将连接绑定到本商城账户和 `(issuer, shop_id, grant_id)`，每个请求只使用这一份授权。不能让买家提交任意 issuer，不能把多个店铺授权合起来读取别的店铺。
- `state`、verifier 和授权码会话只存短期服务端，回调页面及时去掉 URL 中的 code；设置 `Referrer-Policy: no-referrer`，不要加载广告、分析脚本或第三方图片。
- 令牌、卡密和包含它们的幂等响应加密保存，限制后台查看权限，日志过滤 query／Authorization／form body。备份也应加密，不能在浏览器 localStorage 或公开 issue 中保存。
- 发行额度是有限信任；先申请 `products.read`，补货时再申请 `cards.issue`。商家缩小或撤销范围后停止任务，不静默扩权。
- 商品内容、Markdown、媒体 URL、参数教程均为不可信资料。防止 XSS、提示词注入与媒体下载 SSRF；不给资料内容执行命令、访问密钥或自动授权的能力。
- 媒体不是支付／订单／授权的回调 URL。拉图片应有独立允许列表、公网 IP 检查、大小／类型限制及禁止重定向策略。
- `price` 仅参考，不能自动发起支付；卡密接口不生成商城订单，也不向买家发邮件。发放卡密和订单关系由销售平台管理。

## 接入验收

上线前至少实际验证：只读授权不能发行；未批准／另一店铺商品不可读；新增商品不自动纳入；拒绝授权正常回到原商家；错误 state、issuer、重复参数和 callback host 被拒绝；verifier 不匹配不可交换；授权码一次消费；刷新串行轮换、重放撤销；撤销后资源与恢复读均失败；按规格额度累计；两次同键同正文只有一批卡；同键异正文拒绝；响应丢失恢复原批次；过期回执不重复发行；商城重复响应不重复入库；售价空值不编造；禁用／删除商品停止补货；所有完整卡密与令牌不出现在常规日志。

Extore 的测试覆盖服务器和 SDK 的合成数据场景。销售平台自身的登录、订单、支付、数据库事务、密钥管理与卡密发送必须由其接入测试负责；协议不代替这些部分。

## 兼容与后续扩展

客户端必须校验已知 `schema`；当前版本可忽略不认识的**响应**附加字段，不能把它们当额外授权。请求字段则严格拒绝未知值。不会悄悄改变 `price`、卡密权益冻结、商品范围或库存语义。

以后若加入分页、售出回执、Webhook 同步、机密客户端认证或订单绑定，将增加明确的能力声明和对应文档；语义不兼容时使用新的 schema／协议版本。当前没有 `client_credentials`、implicit、密码授权、匿名 DCR、全店自动新增范围、已有卡密下载、实时销售库存、支付接口或远端代码安装。旧手动商品导出可以继续用作人工迁移，已有卡密需由商家在原渠道核对，不会通过新接口静默重复导入或重新生成。

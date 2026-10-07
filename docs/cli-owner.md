# 店主 CLI

[CLI 总览](cli.md) · [顾客 CLI](cli-customer.md) · [AI 接入提示词](ai-prompts.md) · [设备协议](protocol.md#店主-cli-设备授权)

`extore admin` 使用绑定到账号的 CLI 设备管理本店。它与商品管理链接的 `manage` 授权分开，支持商品创建、配置、卡密、队列、事件、会话与安全操作。店主账号限于自己的店铺，平台管理员另外维护店铺、注册和 SMTP；商品授权不能升级为店主或平台权限。完整 CLI 从 0.6.0 提供，多店账号与配置档案说明见[多店与账号](shops.md)。

## 登录与第一次授权

店主可直接用邮箱与密码登录，在终端隐藏输入密码及已启用的 TOTP 验证码：

```sh
extore admin login --origin https://extore.example.com --email owner@example.com
```

`--use-backup-code` 改用 TOTP 恢复码。自动化使用互斥的 `--credentials-file PATH` 或 `--credentials-stdin`，JSON 为 `{"password":"…","code":"…"}` 或 `{"password":"…","backup_code":"…"}`；未启用 TOTP 时只需密码。凭证文件必须为当前用户所有的普通 0600 文件，不能是符号链接；不要把密码或第二因素放入命令参数、日志或聊天。成功后仍生成独立设备密钥，并固定到验证的店主账号。

平台管理员使用下面的真实 Passkey 批准方式；已注册 Passkey 的店主也可使用：

```sh
extore admin login --origin https://extore.example.com
```

客户端在本机生成 Ed25519 私钥，返回待批准页面、设备码和公钥指纹。打开页面，核对设备名称、设备码、指纹、所属店铺或平台范围与期限，使用**已经注册的真实 Passkey**完成本次用户验证。现有登录 Cookie、平台首次密码或 CLI Bearer 不能代替这次 Passkey 批准。

批准后执行：

```sh
extore admin login-status --origin https://extore.example.com
extore admin status --origin https://extore.example.com
```

也可显式让客户端打开浏览器并等待，最长等待 600 秒：

```sh
extore admin login --origin https://extore.example.com --open-browser --wait 600
```

默认不自动打开浏览器。Passkey 批准请求有效 10 分钟；设备授权最多 30 天，会话为 8 小时与设备剩余期限的较短者。正常操作由设备私钥续签，不需要每次再次登录；设备到期后需重新用对应账号的密码与第二因素或 Passkey 授权。设备撤销、店铺停用或该账号的认证恢复会阻止继续访问和续签。平台 SSH 认证重置只撤销平台管理员设备，不重置各店账号。

再次执行 `login` 时，已到期或被撤销的设备会重新申请授权；网络错误保留本地密钥，便于重试。已经拒绝、过期或被清理的待批准请求也可重新申请。已有设备不能切换为另一个店铺或平台管理员身份。

JSON 写操作另有一次性设备签名，绑定当前会话、HTTP 方法、完整路径/查询和请求体。Extore 客户端自动完成，不靠复制 Bearer 获得同等写权限。文件上传只保存草稿，交付还要单独执行 `complete`。

## 日常操作

```sh
extore admin products
extore admin queues
extore admin jobs --product PRODUCT_ID
extore admin job JOB_ID --product PRODUCT_ID
extore admin claim JOB_ID --product PRODUCT_ID
extore admin complete JOB_ID --product PRODUCT_ID --output-file result.json
```

默认商品和队列列表使用摘要，不反复输出长教程、顾客参数或完整字段结构。`queues` 默认聚合全店商品的 active 队列，`--product` 可限定一个；写操作仍明确指定商品。用 `job` 按需读取任务输入输出快照和步骤 ID。

`jobs / job / claim / progress / complete / succeed / fail / retry / request-changes / reject / files / upload / download` 提供本店队列操作。`--view active / processed / all`、`--state`、`--limit` 用于列表；步骤计划用 `--steps-file`，完成集合用可重复的 `--completed-step`。

上传交付文件后，用返回的 ID 填入输出 JSON，再明确完成任务：

```sh
extore admin upload JOB_ID --product PRODUCT_ID --field deliverable --file ./result.pdf
extore admin complete JOB_ID --product PRODUCT_ID --output-file result.json
```

上传不会自动标记成功。当前店主入口用 `request-changes JOB_ID --product PRODUCT_ID --reason …` 要求顾客修改后重提，用 `reject --reason` 拒绝；两者要求自己已领取的 processing 队列任务，顾客能看到原因，自动处理任务仍不能由队列操作覆盖。需细分 `customer_input/external/processor` 与 `revise/reuse` 时，使用单独授权的[商品处理 CLI](cli.md#要求重试拒绝与失败) `request-retry`。

0.8 的原子等待 `next --watch`、当前流程动作的 `--attempt/--flow-epoch/--action-id` 与 `complete --file FIELD=PATH` 属于 `extore manage`；当前 `admin` 快捷命令没有这些选项。店主身份与商品处理设备分开授权，不能把全店设备凭证替代成商品设备或拼接权限。AI 执行流水线时，先申请对应商品范围，再按 [当前动作](cli.md#读取与提交当前动作)处理。

富类型交付可通过店主现有 `upload` 上传真实文件，再在 `complete --output-file` 中填写返回 ID。值仍是字符串：`select` 写定义中的选项代码、`boolean` 写 `"true"` / `"false"`、`image` 写单个文件 ID、`images` 写 JSON 数组的字符串。文件必须属于当前任务的字段；多张图片每张分别上传，数量服从 `max_items`。图片支持 PNG、JPEG、WebP；不能用图片 URL 或 Base64 代替附件。

## 全店命令

除以下新增操作外，`admin product get/update/schema/prompt`、`cards`、`links`、`events`、`sessions`、`devices`、`audit`、`processors`、`source` 与 `api` 沿用 [manage 命令](cli.md#商品卡密与授权管理)的主要参数。店主有全店权限，但按商品写入仍使用 `--product`。

| 命令 | 用途 |
| --- | --- |
| `products` 或 `product list` | 全店商品摘要；`--detail` 按需完整业务信息 |
| `product create` | `--json-file` 或 `--json-stdin` 创建商品 |
| `product templates` | 查询快速创建模板 |
| `product quick` | JSON 传 `template_id,name?,from_product_id?`，创建私有草稿及配置链接，结果保存为私密文件 |
| `product get --product ID --output NEWFILE` | 将完整原配置导出到私密文件，包括获授权的秘密配置 |
| `storage` | 店主只看本店附件额度与上传量；平台管理员可看全站和实际磁盘余量 |
| `sessions list / revoke ID` | 全店登录会话 |
| `devices list / revoke ID` | 商品管理链接的 CLI 设备 |
| `owner-devices list / revoke ID` | 单独查询或撤销店主 CLI 设备 |
| `events list / retry ID` | 全店事件与停止投递的重投 |
| `audit` | 全店登录、设备和授权审计 |
| `processors` | 内置预设目录，`--detail` 获取结构定义 |
| `passkeys list / remove ID` | 查询或移除本账号 Passkey；没有邮箱密码的账号不能删除最后一个 |
| `passkeys register-options` | 获取真实 WebAuthn 注册选项和 challenge_id |
| `passkeys register-verify` | `--json-file` 或 `--json-stdin` 提交真实认证器返回的注册结果 |
| `api METHOD /api/…` | 允许范围内的同源 JSON API，`--query` 与 JSON 输入方式同 manage |
| `status / login-status / logout` | 查看设备状态、完成待批准绑定、撤销本机店主设备 |
| `account get / update / reauth / password` | 本店账号资料及近期密码验证、密码修改；敏感 JSON 用私密文件或 stdin |
| `totp setup / confirm / disable / backup-codes` | 本店第二因素与恢复码；密钥、恢复码只写新私密输出文件 |
| `processor-profiles list / create / get / update / revoke` | 本店代码定义的加密配置档案；读取只返回元数据 |
| `processor-profiles binding / bind / unbind --product ID` | 查询或调整商品绑定；bind 另传 PROFILE_ID |
| `platform settings get / update` | 仅平台管理员：注册开关与加密 SMTP |
| `platform shops list / create / invite / enable / disable / quota` | 仅平台管理员：创建店铺、发送邀请、启停和附件额度 |
| `maintenance status / policy / cleanup` | 本店保留策略与清理；cleanup 默认预览，`--apply` 才执行 |
| `proxy identities list / create` | 本店签名发行身份；create 需 `--name`，不导出私钥 |
| `proxy routes list / create / import / export / enable / disable / default` | 固定兑换目的地及本店启用、默认发行设置；平台 root 必须明确 `--shop` |

本店 `sessions`、`devices`、`events`、`audit`、`processors` 和 `api` 可省略 `--product`；队列写入、已有商品修改、制卡等明确指定商品。平台管理员跨店创建商品或配置档案时显式选店，不合并店主身份；相关输入、确认和命令示例见[多店与账号](shops.md)。保存了多台服务器或设备时，用 `--origin SERVER`、`--grant DEVICE_ID` 选择，避免隐式选择。

### 创建与修改商品

```sh
extore admin product templates
extore admin product quick --json-file quick.json
extore admin product create --json-file product.json
extore admin product update --product PRODUCT_ID --json-file patch.json
extore admin product get --product PRODUCT_ID --output ./private-product.json
```

`quick.json` 示例：

```json
{"template_id":"manual_service","name":"资料审核服务"}
```

`product create` 输入为完整 Product 对象，字段与默认值见[商品配置](protocol.md#商品配置)。`product update` 则是顶层局部修改，内部读取并保留未提供配置；显式传入的数组或对象整体替换。已经发行的自动商品仍受处理方式、字段结构和凭证冻结规则限制，店主权限不会绕过这些业务约束。

普通标准输出与 `--detail` 都脱敏认证凭证和秘密配置。店主特例是 `product get --output NEWFILE`：显式导出到新 0600 文件时可包含获授权的 Webhook 配置，不要求商品管理命令的 `--include-secrets`；`products --output` 也可导出。处理器配置档案、SMTP、TOTP 的秘密始终不在商品导出或读取接口里。制卡、创建管理链接或快速创建时，一次性凭证自动保存到私密文件，标准输出只显示保存路径与摘要。

## 配置签名兑换路由

在发行站 B 创建本店身份和指向本站的发行路由：

```sh
extore admin proxy identities create --name "本店发行"
extore admin proxy routes create --name "本站兑换" --identity IDENTITY_ID
extore admin proxy routes default ROUTE_ID
extore admin proxy routes export ROUTE_ID --output ./route-public.json
```

在入口站 A 使用本店独立设备登录，核对 B 的目标地址和公钥后导入：

```sh
extore admin proxy routes import --json-file ./route-public.json
extore admin proxy routes list
extore admin proxy routes disable ROUTE_ID
extore admin proxy routes enable ROUTE_ID
```

平台管理员在每条命令上加 `--shop SHOP_ID`；店主可省略，始终限制在自己的店铺。`export` 只提供 `route_id`、`issuer_id`、`name`、`origin`、`path`、`public_key` 六项公开字段，可交给另一站的 `import`；没有卡密、私钥、访问凭据或秘密摘要。目标 origin 必须是公网 HTTPS，当前发行路径为 `/`，不是任意重定向 URL。

同一公开路由可由不同店铺分别绑定，但目标、标识和公钥必须完全一致，不允许覆盖成另一个目标。默认发行设置只作用于本店以后新发的卡，不改写旧卡；导入的下游路由不能成为本店发行默认。发行站可用 `proxy routes default LOCAL_ROUTE_ID --clear` 停止默认包装新卡。切换目标时创建新路由，停用不会重写现有卡密。完整操作说明见[兑换路由](proxy-config.md)。

## 记录维护

```sh
extore admin maintenance status
extore admin maintenance policy
extore admin maintenance policy --json-file policy.json
extore admin maintenance cleanup --area events --limit 100
extore admin maintenance cleanup --area events --limit 100 --apply
```

`policy.json` 是完整保留策略：`enabled`、`event_retention_days`、`dead_letter_retention_days`、`audit_retention_days`、`link_retention_days`。默认启用，分别为 30、90、180、90 天；审计最少 90 天。平台 root 可用 `--shop SHOP_ID` 明确目标店铺，店主只操作自己的店。`--area` 可重复选择 links/events/audit；不传时按所有范围预览，`--apply` 才执行。失效链接归档保留祖先墓碑，不使后代权限恢复；pending 事件、有效授权、卡密、任务与文件保留，见[保留协议](protocol.md#记录保留与清理)。

## Passkey 注册

```sh
extore admin passkeys list
extore admin passkeys register-options --output ./registration-options.json
extore admin passkeys register-verify --json-file registration-response.json
```

`register-options` 返回 WebAuthn 选项和 `challenge_id`。浏览器或支持 WebAuthn 的认证器需在正确 RP ID / Origin 下完成真实的创建仪式，再提交：

```json
{"challenge_id":"返回的挑战 ID","name":"新安全密钥","credential":{}}
```

这里的空 `credential` 只表示结构占位，必须替换为真实注册结果；空对象或自行编造的数据不会注册成功。店主 CLI 负责取选项、验签提交和保存状态，不模拟安全密钥，也不替用户触碰硬件或完成系统确认。

## 配置与退出

默认私密配置 `${XDG_CONFIG_HOME:-~/.config}/extore/owner.json`，可用 `EXTORE_OWNER_CONFIG` 或 `extore admin --profile PATH …` 指定；`--profile` 放在 admin 之后、子命令之前。目录 0700、文件 0600，与商品管理及顾客配置分开。私钥留在本机，服务器保存公钥与审计元数据。

```sh
extore admin owner-devices list
extore admin owner-devices revoke DEVICE_ID
extore admin logout --origin https://extore.example.com
extore admin logout --all
```

`admin logout` 撤销所选店主设备及它的全部 CLI 会话，然后移除本地密钥；网络失败时保留本地密钥供重试。这与 `manage logout` 只结束当前商品 CLI 会话并移除本地记录不同。单独撤销一条会话不会阻止仍有效设备继续续签。

### 商品回收站

删除商品会停售并移入回收站，可以恢复。它不会删除卡密、订单任务、领取链接或附件；原卡密仍能兑换，已提交任务仍能处理。彻底清除存储不是这些命令的功能。

```sh
extore admin product delete --product PRODUCT_ID --yes
extore admin products --view deleted
extore admin product list --view all
extore admin product restore --product PRODUCT_ID
```

商品列表默认 `--view active`，也可选择 `deleted` 或 `all`。跨商品队列会查询全部商品，保留回收站商品的旧任务。商品删除必须显式写 `--yes`，缺少这个参数时不会发送删除请求，也不会为了删除而续期会话。店主命令沿用一次性挑战和设备签名，并在审计中记录店主身份。

商品授权 CLI 使用相同命令：

```sh
extore manage product delete --product PRODUCT_ID --yes
extore manage products --view deleted
extore manage product restore --product PRODUCT_ID
```

这两个操作要求同一份商品授权明确包含 `product.delete`，不要求 `product.edit`。旧链接、旧授权，以及原来的“完整管理”权限集合不会自动新增删除权限；需要管理者另行批准。CLI 不会合并多个链接来凑权限，也不会把删除操作应用到其他商品。

通用 API 命令同样要求确认，并且 JSON 只能是 `{"confirmed":true}`。未提供 JSON 时，CLI 会在 `--yes` 的明确确认下生成这个正文。恢复不接收商品配置正文。

```sh
extore admin api DELETE /api/admin/products/PRODUCT_ID --product PRODUCT_ID --yes
extore admin api POST /api/admin/products/PRODUCT_ID/restore --product PRODUCT_ID
extore manage api DELETE /api/manage/product --product PRODUCT_ID --yes
extore manage api POST /api/manage/product/restore --product PRODUCT_ID
```

通用店主 API 的路径商品 ID、`--product` 和查询中的商品 ID 必须一致。CLI 只输出商品 ID、是否已删除以及删除时间，不输出响应里的配置、卡密或凭据。

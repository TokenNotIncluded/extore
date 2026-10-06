# 店主 CLI

[CLI 总览](cli.md) · [顾客 CLI](cli-customer.md) · [AI 接入提示词](ai-prompts.md) · [设备协议](protocol.md#店主-cli-设备授权)

`extore admin` 使用店主批准的 CLI 设备管理全店。它与商品管理链接的 `manage` 授权分开，支持全店商品创建、配置、卡密、队列、事件、会话与安全操作。以下命令要求 0.6.0 及以上；没有新的设备批准时，已有商品授权不能升级为店主权限。

## 第一次授权

```sh
extore admin login --origin https://extore.example.com
```

客户端在本机生成 Ed25519 私钥，返回待批准页面、设备码和公钥指纹。打开页面，核对设备名称、设备码、指纹、全店范围与期限，使用**已经注册的真实 Passkey**完成本次用户验证。现有登录 Cookie、首次密码或 CLI Bearer 不能代替这次批准。

批准后执行：

```sh
extore admin login-status --origin https://extore.example.com
extore admin status --origin https://extore.example.com
```

也可显式让客户端打开浏览器并等待，最长等待 600 秒：

```sh
extore admin login --origin https://extore.example.com --open-browser --wait 600
```

默认不自动打开浏览器。初次请求有效 10 分钟；设备授权最多 30 天，会话为 8 小时与设备剩余期限的较短者。正常操作由设备私钥续签，不需要每次触碰 Passkey；设备到期后的新批准仍需要 Passkey。设备撤销或 SSH 认证重置会立即阻止继续访问和续签。

再次执行 `login` 时，已到期或被撤销的设备会重新生成密钥并请求 Passkey 批准；网络错误保留本地密钥，便于重试。已经拒绝、过期或被清理的待批准请求也可重新申请。

JSON 写操作另有一次性设备签名，绑定当前会话、HTTP 方法、完整路径/查询和请求体。官方客户端自动完成，不靠复制 Bearer 获得同等写权限。文件上传只保存草稿，交付还要单独执行 `complete`。

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

`jobs / job / claim / progress / complete / succeed / fail / retry / request-changes / reject / files / upload / download` 沿用[商品队列命令](cli.md#处理一个任务)的参数。`--view active / processed / all`、`--state`、`--limit` 用于列表；步骤计划用 `--steps-file`，完成集合用可重复的 `--completed-step`。

上传交付文件后，用返回的 ID 填入输出 JSON，再明确完成任务：

```sh
extore admin upload JOB_ID --product PRODUCT_ID --field deliverable --file ./result.pdf
extore admin complete JOB_ID --product PRODUCT_ID --output-file result.json
```

上传不会自动标记成功。退回补充用 `request-changes --reason`，拒绝用 `reject --reason`，都要求自己已领取的 processing 队列任务；顾客能看到原因。自动处理任务仍不能由队列操作覆盖。

## 全店命令

除以下新增操作外，`admin product get/update/schema/prompt`、`cards`、`links`、`events`、`sessions`、`devices`、`audit`、`processors`、`source` 与 `api` 沿用 [manage 命令](cli.md#商品卡密与授权管理)的主要参数。店主有全店权限，但按商品写入仍使用 `--product`。

| 命令 | 用途 |
| --- | --- |
| `products` 或 `product list` | 全店商品摘要；`--detail` 按需完整业务信息 |
| `product create` | `--json-file` 或 `--json-stdin` 创建商品 |
| `product templates` | 查询快速创建模板 |
| `product quick` | JSON 传 `template_id,name?,from_product_id?`，创建私有草稿及配置链接，结果保存为私密文件 |
| `product get --product ID --output NEWFILE` | 将完整原配置导出到私密文件，包括获授权的秘密配置 |
| `storage` | 全站附件逻辑额度、上传数量与实际磁盘余量 |
| `sessions list / revoke ID` | 全店登录会话 |
| `devices list / revoke ID` | 商品管理链接的 CLI 设备 |
| `owner-devices list / revoke ID` | 单独查询或撤销店主 CLI 设备 |
| `events list / retry ID` | 全店事件与停止投递的重投 |
| `audit` | 全店登录、设备和授权审计 |
| `processors` | 官方预设目录，`--detail` 获取结构定义 |
| `passkeys list / remove ID` | 查询或移除 Passkey，不能删除最后一个 |
| `passkeys register-options` | 获取真实 WebAuthn 注册选项和 challenge_id |
| `passkeys register-verify` | `--json-file` 或 `--json-stdin` 提交真实认证器返回的注册结果 |
| `api METHOD /api/…` | 允许范围内的同源 JSON API，`--query` 与 JSON 输入方式同 manage |
| `status / login-status / logout` | 查看设备状态、完成待批准绑定、撤销本机店主设备 |

全局 `sessions`、`devices`、`events`、`audit`、`processors` 和 `api` 可省略 `--product`；队列写入、已有商品修改、制卡等明确指定商品。保存了多台服务器或设备时，用 `--origin SERVER`、`--grant DEVICE_ID` 选择，避免隐式选择。

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

普通标准输出与 `--detail` 都脱敏认证凭证和秘密配置。店主特例是 `product get --output NEWFILE`：显式导出原配置到新 0600 文件时包含完整配置，不要求商品管理命令的 `--include-secrets`；`products --output` 也会导出完整商品配置。制卡、创建管理链接或快速创建时，一次性凭证自动保存到私密文件，标准输出只显示保存路径与摘要。

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

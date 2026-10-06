# 协议 v1

所有 JSON 使用 UTF-8。API 错误为 `{"detail":"说明"}`，HTTP 400/422 是输入错误，401/403 是认证或权限，404 是不存在，409 是状态冲突，410 是领取已失效，429 是限流。外部平台不访问商家会话接口。

## 任务状态

```mermaid
stateDiagram-v2
  [*] --> queued: 提交兑换
  queued --> processing: 员工领取 / worker / 平台进度
  queued --> succeeded: 平台直接完成
  queued --> failed: 平台明确失败
  processing --> succeeded: 交付完成
  processing --> failed: 明确失败 / 超时待核实
  failed --> queued: 允许重试且未超过次数
  succeeded --> destroyed: 顾客销毁
```

任务 ID 在重试时保持不变，`attempt` 从 1 递增。平台必须以稳定任务 ID 去重外部交付，以 `(task_id, attempt)` 区分状态回调。旧尝试的回调 HTTP 409。终态不能覆盖；同一终态的重复完成请求返回当前结果，不修改内容。只接受 0–100 的进度；处理中进度不能倒退，成功强制为 100。

商品规则 `allow_retry`、`max_attempts` 与失败结果 `retryable` 必须同时满足，才能自动重试。没有确认未交付的失败不能标记为可重试。Webhook 超过 1 小时未更新状态，人工超过 1 小时未更新进度，以及脚本中断，都会进入不可自动重试的失败状态；商家在核实后可放行。过期尝试的待投递 `redemption.requested` 会取消，不继续启动旧任务。

## 支付平台生成卡密

在 SSH 终端执行 `uv run python -m extore.cli integration-key`。命令生成或轮换仅能发行卡密的 API Key，只显示一次；与商品 Webhook 密钥无关。

```http
POST /api/integrations/cards
Authorization: Bearer <API-Key>
Idempotency-Key: payment-order-unique-id
Content-Type: application/json

{"product_id":"商品 UUID","count":1}
```

响应 `{"codes":["XXXXXXXX-XXXXXXXX-XXXXXXXX-XXXXXXXX"]}`。数量 1–1000。幂等键长 8–200 字符。同键同请求返回原卡密，不再生成；同键不同请求 HTTP 409。响应加密持久化用于平台恢复，密钥位于 `EXTORE_DATA/issuance.key`；该接口持钥者可读取同幂等键原响应，不向顾客暴露该密钥。幂等记录永久保留，防止旧订单重新发行。轮换平台 Key 不清理记录。

平台须持久化订单与请求幂等键，收到响应后再向顾客交付卡密。没有付款回调或支付逻辑。

## 商品配置

`mode`: `manual` / `webhook` / `script`。
`delivery`: `content` / `service`。
`view_policy`: `repeat` / `once`。
`public`: 首页是否公开显示商品。

每个参数包含：

```json
{
  "key": "account_email",
  "label": {"zh-CN": "账户邮箱", "en": "Account email"},
  "type": "email",
  "required": true,
  "description": {"zh-CN": "## 查找邮箱\n1. 打开账户设置。\n2. 复制登录邮箱。"},
  "collapsed": true
}
```

代码名匹配 `[a-z][a-z0-9_]{0,39}`，必须唯一。类型支持 `text` / `email` / `textarea` / `number`，参数最多 30 个，值最多 10000 字符，未知参数拒绝。前端展示多语言名称和 Markdown 教程，服务端验证必填、邮箱与有限数字。

发过卡密的商品不能改变处理方式、交付类型、查看规则、脚本或签名密钥。需要改这些设置时创建新商品，避免已经发行的卡密与正在处理的任务改变含义。名称、说明、参数等可调整，建议无待处理任务时更改参数结构。

## Webhook 事件

商品配置 `webhook_url` 和 `webhook_secret`（至少 32 字符）。所有处理方式都可配置通知 URL；只有 `webhook` 处理方式接受外部状态回调。交付内容不写入事件；用户参数仅在 `redemption.requested` 携带。

```json
{
  "id": "事件 UUID",
  "type": "redemption.requested",
  "version": 1,
  "created_at": 1790000000.0,
  "product_id": "商品 UUID",
  "data": {
    "id": "任务 UUID",
    "state": "queued",
    "attempt": 1,
    "progress": 0,
    "message": "",
    "params": {"account_email": "user@example.com"}
  }
}
```

完整事件：

| type | 触发时机 | data |
|---|---|---|
| `redemption.requested` | 首次提交或合规重试 | 任务状态字段及 `params` |
| `fulfillment.progress` | 领取任务、自动处理开始、进度更新 | 任务状态字段 |
| `fulfillment.succeeded` | 首次成功交付 | 任务状态字段 |
| `fulfillment.failed` | 明确失败、超时或中断 | 任务状态字段 |
| `delivery.viewed` | 内容成功领取，每次重复查看也产生事件 | 任务状态字段 |
| `delivery.destroyed` | 顾客首次销毁 | 任务状态字段，`state=destroyed` |

事件与任务变更同一 SQLite 事务提交。外部平台应快速校验、入队并返回 2xx；随后异步执行。签名字段：

```text
X-Extore-Timestamp: Unix 秒
X-Extore-Nonce: 随机字符串
X-Extore-Signature: HMAC-SHA256 十六进制
X-Extore-Event-Id: 事件 UUID
Content-Type: application/json
```

签名输入为 `timestamp + "." + nonce + "." + 原始请求体字节`，密钥为商品的 `webhook_secret`。必须对原始字节验签，不得将 JSON 重编码后验签。允许时钟误差 ±300 秒；服务器需要正确同步时间。

投递保证为**至少一次**。重试使用相同事件 ID、新时间和新 nonce。接收端需持久化事件 ID 防止重复消费；SDK 只验签，不替你存储去重状态。一次 HTTP 投递 15 秒超时，最多 8 次，间隔 `min(3600, 2^attempts * 5)` 秒。只认可 2xx，不跟随重定向。投递失败记为 `dead` 后由商家重投；过期请求记为 `cancelled`。

不同任务不保证全局顺序；顾客状态以 API 为准。超时重试可能重复到达，另一平台必须以任务 ID 做发货去重，不能只依赖事件 ID。收到了非 `redemption.requested` 的通知时不要启动发货。

## 签名回调

```http
POST /api/callbacks/<product_id>/<task_id>
X-Extore-Timestamp: ...
X-Extore-Nonce: ...
X-Extore-Signature: ...
Content-Type: application/json
```

```json
{
  "attempt": 1,
  "state": "succeeded",
  "progress": 100,
  "message": "已完成",
  "content": "可交付的文本或资源链接",
  "retryable": false
}
```

`state` 可为 `processing` / `succeeded` / `failed`。内容型商品成功时 `content` 必填（最大 100000 字符）；服务型商品忽略 `content`，只返回状态。`message` 最多 1000 字符，应避免包含秘密。

签名算法与事件相同。服务器持久化 nonce，在有效签名时间窗口内拒绝重放；相同 nonce 再到达 HTTP 409。商品不能跨商品更新任务，旧 attempt 或已销毁任务返回 409。签名不依赖浏览器 Origin 或 Cookie。

## 顾客接口

顾客浏览器的写入请求需要配置的 `Origin`，防止跨站操作。

| 接口 | 请求 | 结果 |
|---|---|---|
| `GET /api/products` | 无 | 仅公开商品、无脚本及密钥 |
| `POST /api/exchange` | `{code}` | 30 天兑换凭证、指定商品、已有任务 |
| `POST /api/redeem` | `{token,params}` | 创建或合规重试任务，返回状态 |
| `POST /api/receipt` | `{token}` | 商品与状态，不返回内容 |
| `POST /api/receipt/reveal` | `{token}` | 显式领取内容；一次领取原子消费 |
| `POST /api/receipt/destroy` | `{token}` | 永久关闭应用内交付内容 |

不能根据商品 UUID 直接打开非公开商品。兑换凭证、员工链接和卡密都是秘密，不记录在分析工具中，不植入第三方前端脚本。API 不在状态查询中自动返回交付内容。

## 商家与员工接口

商家完整接口可在 `/docs` 查看。认证 Cookie HttpOnly、SameSite=Strict、生产 Secure；写操作校验 Origin。

商家可维护商品、批量制卡、撤销未兑换卡密、维护员工链接、查看事件、重投事件和管理多个 Passkey。员工会话只可调用 `/api/manage/jobs` 与 `/api/manage/batch`，服务端强制限定商品。

`POST /api/manage/batch`：

```json
{"ids":["任务 UUID"],"action":"progress","progress":50,"message":"资料审核完成"}
```

`action`: `claim` / `progress` / `succeed` / `fail` / `retry`。一次最多 100 个任务，整批事务要么成功、要么回滚。领取只接受排队任务；进度、完成和失败只接受自己领取的人工任务；`retry` 仅商家可用，核实后允许失败任务由顾客再次提交。自动任务不能人工覆盖交付。

事件记录不含完整参数和交付内容，审计记录保存操作者、动作、目标及时间。敏感内容的完整备份、保留期和外部平台删除策略需由商家制定。

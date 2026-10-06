# 私有商品处理器协议 v2

私有处理器是商家自行运行、可以保持闭源的服务。Extore 发送一个已冻结的商品流水线步骤，服务处理后通过 SDK 返回进度或结果。它只获得当前步骤声明的参数和文件权限，不能登录管理后台或签发卡密。

私有处理器地址仍必须使用 HTTPS，解析到公网 IP。Extore 固定已验证的 IP，同时保留原 Host 和 TLS SNI，拒绝内网、混合内外网 DNS 结果、重定向、代理环境变量和含查询参数的地址。需要处理器主动拉取任务时，应使用经过店长批准的 CLI 流水线授权，而不是让 Extore 访问任意内网地址。

## 范围和密钥

每次执行都有以下固定范围：`shop_id`、`product_id`、`job_id`、`attempt`、`node_id`、`flow_epoch`、`action_id`。其中 `attempt` 和 `flow_epoch` 是严格的正整数。其它范围字段是 1–100 个 ASCII 字符，匹配 `[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}`。

输入来自发行时冻结的加密流水线定义。服务器每次访问还会检查店铺是否启用、商品归属、卡密和任务是否有效、当前步骤和绝对截止时间。更换或清空商品的 Webhook 密钥会撤销旧处理器签名权限；历史回执不绕过撤销检查。密钥不进入用户页面、普通 DTO、事件日志或回执表。

两个通信方向使用独立派生密钥：

```text
direction = "extore-to-worker" 或 "worker-to-extore"
key = HMAC-SHA256(secret_utf8,
                  b"extore-private-worker-key-v2\0" + direction_ascii)
```

签名消息是以下数组的 JSON ASCII 字节；`ensure_ascii=True`、`allow_nan=False`、`separators=(',', ':')`、`sort_keys=True`，没有换行：

```text
["extore-private-worker-signature-v2", direction, audience, method, path,
 timestamp, nonce, body_sha256,
 shop_id, product_id, job_id, attempt, node_id, flow_epoch, action_id]
```

`signature = HMAC-SHA256(key, message).hexdigest()`。

`audience` 是接收方的完整 origin。例如回调的 audience 必须精确等于 `EXTORE_ORIGIN`。`method` 和 `path` 必须与请求一致。回调不允许 query、编码路径别名或混用 `Authorization`。签名不使用浏览器 Cookie。

## 请求头

以下字段必须各出现一次，不能重复：

| 请求头 | 内容 |
| --- | --- |
| `X-Extore-Version` | `2` |
| `X-Extore-Direction` | 通信方向 |
| `X-Extore-Audience` | 接收方 origin |
| `X-Extore-Timestamp` | Unix 秒，ASCII 十进制文本 |
| `X-Extore-Nonce` | 16–128 个 base64url 字符 |
| `X-Extore-Body-SHA256` | 原始请求体 SHA256，小写十六进制 |
| `X-Extore-Signature` | v2 HMAC，小写十六进制 |
| `X-Extore-Shop-Id` | `shop_id` |
| `X-Extore-Product-Id` | `product_id` |
| `X-Extore-Job-Id` | `job_id` |
| `X-Extore-Attempt` | `attempt` |
| `X-Extore-Node-Id` | `node_id` |
| `X-Extore-Flow-Epoch` | `flow_epoch` |
| `X-Extore-Action-Id` | `action_id` |

签名有效窗口为 300 秒。Nonce 在同一商品和方向内只能使用一次，不能把相同 HTTP 请求原样重放。处理器应使用 SDK 构造请求，而不是自己拼接签名。

## 执行请求

Extore 向处理器地址发送 POST。消息包含 `version: 2`、`type: "flow.process.requested"`、`scope`、`params`、`parameters`、`outputs`、`deadline` 和 `callback_url`。不包含 Webhook 密钥或全系统权限。

整个传输 JSON 上限是 256,000 字节，使用 ASCII 转义后计数，包含参数、字段描述和固定协议字段。一个中文字符通常占六个转义字节；较大的正文应使用受限附件，而不是把整份文档塞进参数。超过预算会停止派发。

处理器先核验 v2 签名、接收方 audience 和过期时间，再持久记录 `action_id`。网络重投不应导致同一 action 再次执行有副作用的动作。敏感输入有独立短期有效时间；不能把过期输入写入日志后再处理。

## 进度和结果

```text
POST /api/callbacks/v2/{product_id}/{job_id}/result
```

原始 JSON 必须只包含以下三个字段；JSON 对象内重复键、NaN、Infinity、未知字段以及类型转换都会被拒绝：

```json
{
  "version": 2,
  "result_id": "result-unique-for-this-action",
  "update": {
    "attempt": 1,
    "state": "succeeded",
    "output": {"content": "完成后的交付内容"}
  }
}
```

`update` 使用商品处理状态和输出定义。进度更新使用新的 `result_id`。重试同一更新时，保留相同 `result_id` 和**完全相同的请求体字节**，只生成新时间戳和 nonce。服务器在同一个 SQLite 事务中验证截止时间、接受结果、推进步骤并保存回执。

同一结果、同一内容的重试返回原回执，不再次执行完成逻辑；同一结果编号更换内容返回 `409`。已接受步骤推进后仍可取原回执，但不能用旧范围提交新结果、下载文件或执行下一步。结果未曾接受且步骤过期时返回 `409`，不会延长截止时间。

处理节点成功只表示该节点完成。后续步骤和最终交付由冻结的流水线决定；只有最终完成才消耗卡密。处理器不能通过中间结果修改商品、跳过节点或冒充队列管理人员。

## 文件

上传一个当前步骤声明的输出：

```text
POST /api/callbacks/v2/{product_id}/{job_id}/files/{field_key}/{result_id}/upload
```

请求体直接使用文件字节，签名覆盖其 SHA256 和完整路径。可通过 `X-Extore-Filename` 提供文件名；文件名和 MIME 只是未经信任的显示信息，服务器仍验证实际图片内容。每个 `result_id` 在当前输出字段内稳定使用，重传同一内容返回同一文件回执。

接收前和写入前都检查当前步骤范围。每个接收块先预留磁盘配额再写入临时文件，继承单文件、卡密、店铺、全局容量、并发和上传时限。图片使用统一图片验证器，不相信客户端的 `image/png` 声明。错误或取消会关闭临时文件并释放接收配额。

下载当前步骤已经声明的输入来源：

```text
GET /api/callbacks/v2/{product_id}/{job_id}/files/{field_key}/{file_id}/download
```

GET 请求体必须为空，仍使用完整 v2 签名。授权检查在加载文件内容之前进行。知道另一个任务或步骤的文件 ID 不构成权限，已销毁文件不能恢复。下载响应作为附件返回，并禁用缓存和页面内执行。

## 与 v1 的关系

原有简单 Webhook 商品继续使用 v1。流水线处理必须使用 v2，不能把 v1 签名发送到新接口，也不能用旧回调绕过步骤范围。v2 的两个派生密钥和签名域均与 v1 不同。

私有处理器 SDK 不授予支付权限。外部有副作用的操作仍需处理器自己的账户授权、幂等记录和限额；不要把通信签名当作付款授权。

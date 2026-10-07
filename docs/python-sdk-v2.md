# 私有 Worker SDK v2

Webhook v1 的 `Client`、`verify_event` 保持兼容。流程 Worker 使用 `FlowScope`、`PrivateWorkerClient` 和 `verify_flow_event`。

```python
from extore.sdk import FlowScope, PrivateWorkerClient, verify_flow_event

# audience 与 path 来自 Worker 自己固定配置和实际请求，不能取自顾客输入。
event = verify_flow_event(
    worker_secret,
    raw_body,
    request_headers,
    audience="https://worker.example",
    path="/process",
)
# 在执行之前，持久检查传输 nonce 与 action_id，防止重复运行。
scope = FlowScope.from_context(event["scope"])
client = PrivateWorkerClient("https://extore.example", worker_secret)
# 确认本步骤开始，输入仍有效时才会获得成功回执。
client.update(scope, "started-1", state="processing")
uploaded = client.upload(scope, "delivery_file", "delivery.docx", result_id="file-1")
client.update(
    scope,
    "result-1",
    state="succeeded",
    output={"delivery_file": uploaded["file"]["id"]},
)
```

完整协议见 [`private-worker.md`](private-worker.md)。执行身份包含店铺、商品、任务、顾客尝试次数、节点、`flow_epoch` 和 `action_id`；它必须来自已经验签的服务器派发，不能从客户参数构造。

签名分派发、回调两个方向，覆盖准确 audience、HTTP 方法、实际请求路径、正文摘要和执行身份。Worker 的 audience 使用规范 HTTPS origin，路径应是实际请求的原始编码路径；接收回调的 Extore origin 必须与服务器配置完全一致。

每次网络投递生成新 nonce；同一个结果重试保留 `result_id` 和完全相同的正文。服务端只接受一次，同 ID 改正文会冲突。SDK 不会自动重做业务动作或自动跟随重定向，也不读取环境代理配置。

`verify_flow_event` 验证签名、目标、方向、时间、严格 JSON，以及正文 scope 与签名身份一致、派发 deadline 尚未过期。它不替 Worker 保存重放记录：Worker 必须持久去重 nonce，并按 `action_id` 去重执行。外部支付和发货还需要跨尝试稳定的业务幂等键。

0.9.1 起，派发若包含顶层 `instructions`，它也被正文签名覆盖。SDK 检查 `extore.work-instructions.v1` 结构以及 `shop_id/product_id` 与当前 `FlowScope` 一致。AI Worker 在实际处理前读取 `factory_slogan` 与 `workshop_slogan`，不能拿顾客参数中的同名内容替代。旧派发省略此字段继续可用。文字不会被 SDK 执行，也不授予额外权限。

`download(scope, field, file_id)` 获取该节点允许读取的输入附件。上传使用实际文件大小上限与安全文件读取，服务器继续检查字段、步骤、尝试、图片内容和配额。结果及输入不应被无条件打印到日志。

密钥来自商家或服务器的私有配置。隔离运行只能限制处理程序访问未授权资源，不能阻止程序泄漏已明确交给它的需求或验证码；只向可信 Worker 派发敏感内容。

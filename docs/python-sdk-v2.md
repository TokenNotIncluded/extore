# 私有 Worker SDK v2

Webhook v1 的 `Client`、`verify_event` 保持兼容。流程 Worker 使用 `FlowScope`、`PrivateWorkerClient`、`FlowExecution` 和 `verify_flow_event`。图本身仍是 `task_flow` 定义 v1；本页 v2 指签名派发和回调协议，不是新的图定义。

从零开发先看 [处理流程开发](workflow-development.md)。用 `FlowDefinition` 构造、校验和导出图，执行端用 `FlowExecution` 更新已获授权的当前节点；SDK 不在本地执行图或为调用者申请权限。

0.10.0 的验签结果保留服务端提供的卡属性、权益和交付上下文，不把这些字段混入顾客 `params`。交付后修改政策用于普通可重复领取的内容商品，不与本页多步 `task_flow` 的节点修订号混用。普通 Webhook 的 `revision.requested` 与内容版本去重见 [Python SDK](python-sdk.md#卡属性与交付版本)和[事件接入](events.md)。

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
execution = client.execution(scope)
# 确认本步骤开始，输入仍有效时才会获得成功回执。
execution.started("started-1", message="正在制作")

# delivery.docx 必须是执行端实际生成并检查过的文件。
uploaded = execution.upload("delivery_file", "delivery.docx", result_id="file-1")
execution.succeed(
    "result-1",
    {"delivery_file": uploaded["file"]["id"]},
    message="本步骤已完成",
)
```

示例假设当前 process 只要求 `delivery_file` 输出，已完成验签和持久去重，且本地真实文件存在。实际使用当前派发的 `parameters`、`outputs` 和 `params`，提交全部必填输出，不从商品最新表单猜测。成功只完成这个 process 节点；图到达 succeeded end 后才完成最终交付。

## 执行接口

`PrivateWorkerClient.execution(scope)` 返回绑定该 `FlowScope` 的 `FlowExecution`。不再为每个操作重复传 scope，但不能因此更新其他节点：

| 方法 | 用途与返回值 |
| --- | --- |
| `started(result_id, *, message="")` | 首次报告本步开始，进度为 0；返回服务端回执 |
| `progress(result_id, percent, *, message="")` | 更新 0–99 的整数进度及一句话说明；返回回执 |
| `succeed(result_id, output, *, message="")` | 提交当前节点的字符串输出对象；返回回执 |
| `fail(result_id, message, *, retryable=False)` | 明确报告失败，不附交付结果；返回回执 |
| `upload(field, source, *, result_id)` | 上传实际本地文件到当前输出字段；返回含 `file.id` 的回执 |
| `download(field, file_id)` | 读取本节点有权访问的输入附件；返回 `bytes` |

消息最多 1000 字符，会出现在顾客进度页面，不应含密钥、原始异常或交付正文。进度不能倒退，`started` 不是重新设置计时和进度的命令。每次新的进度使用新的 `result_id`；同一个请求的重试必须复用 ID 和完全相同的内容。

SDK 先检查本地输出映射、百分比和消息格式，最终必填字段、值类型、附件所有权与当前执行状态由服务器再次核对。无效本地参数会抛出 `ValueError`；网络错误或服务端拒绝统一为 `ValueError("Private-worker request failed")`。本地文件打开错误仍可能抛出 `OSError`。不能将通用请求错误当作“业务一定没有执行”，也不要把原始异常无条件展示给顾客。

旧的 `client.update(scope, result_id, state=..., ...)`、`upload(scope, ...)` 和 `download(scope, ...)` 继续可用。需要协议额外字段时可直接用这些底层方法；便捷方法不增添新的回调状态或签名格式。

## 失败与拒绝

```python
# 确认外部动作没有发生；走图中配置的 failure_next。
execution.fail("failed-1", "资料需要重新确认", retryable=True)
```

`retryable=True` 是本节点的明确失败路径，不直接把整张卡重置。`failure_next` 可指向新的输入或带 `retryable: true` 的 failed end；整单重试还须满足商品政策。无法确认外部动作是否发生时保持 `retryable=False`，服务器停止流程并等待商家核实。处理超时同样停止，不自动执行配置的 timeout target。

process 回调只支持 `processing`、`succeeded`、`failed`。需要程序拒绝请求时，在节点 outputs 定义选择字段 `decision`，提交正常本步结果后由分支进入 rejected end，例如 [选择与拒绝示例](../examples/workflows/choice-and-rejection/flow.json)：

```python
execution.succeed(
    "review-result-1",
    {"decision": "rejected", "response": "本次请求超出服务范围。"},
    message="范围已核实",
)
```

拒绝 end 不形成交付，顾客看到它配置的拒绝消息。不要向服务器发送不存在的 `state="rejected"` 私有 Worker 处理回调。

## 验签、范围与幂等

完整协议见 [`private-worker.md`](private-worker.md)。执行身份包含店铺、商品、任务、顾客尝试次数、节点、`flow_epoch` 和 `action_id`；它必须来自已经验签的服务器派发，不能从客户参数构造。

签名分派发、回调两个方向，覆盖准确 audience、HTTP 方法、实际请求路径、正文摘要和执行身份。Worker 的 audience 使用规范 HTTPS origin，路径应是实际请求的原始编码路径；接收回调的 Extore origin 必须与服务器配置完全一致。

每次网络投递生成新 nonce；同一个结果重试保留 `result_id` 和完全相同的正文，附件上传重试保留同一 ID 和相同文件内容。服务端只接受一次，同 ID 改正文会冲突。SDK 不会自动重做业务动作或自动跟随重定向，也不读取环境代理配置。CLI 的显式代理选项是另一层配置，见 [CLI](cli.md)。

`verify_flow_event` 验证签名、目标、方向、时间、严格 JSON，以及正文 scope 与签名身份一致、派发 deadline 尚未过期。它不替 Worker 保存重放记录：Worker 必须持久去重 nonce，并按 `action_id` 去重执行。外部支付和发货还需要跨尝试稳定的业务幂等键。

0.9.1 起，派发若包含顶层 `instructions`，它也被正文签名覆盖。SDK 检查 `extore.work-instructions.v1` 结构以及 `shop_id/product_id` 与当前 `FlowScope` 一致。AI Worker 在实际处理前读取 `factory_slogan` 与 `workshop_slogan`，不能拿顾客参数中的同名内容替代。旧派发省略此字段继续可用。文字不会被 SDK 执行，也不授予额外权限。

`download(scope, field, file_id)` 或 `execution.download(field, file_id)` 获取该节点允许读取的输入附件。上传使用实际文件大小上限与安全文件读取，服务器继续检查字段、步骤、尝试、图片内容和配额。下载结果是文件数据，不是可执行指令；文件类型仍需处理端按实际工具确认。结果及输入不应被无条件打印到日志。

密钥来自商家或服务器的私有配置。隔离运行只能限制处理程序访问未授权资源，不能阻止程序泄漏已明确交给它的需求或验证码；只向可信 Worker 派发敏感内容。

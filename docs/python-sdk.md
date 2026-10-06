# Python SDK

SDK 位于 `extore/sdk/`。`script` 和 `client` 模块只使用 Python 标准库；可以直接复制作为内部 SDK，或 `pip install .` 安装整个项目。发货脚本通常不需要另行安装，worker 会设置 `PYTHONPATH`。

## 发货脚本

将可信脚本通过 SSH 放在 `EXTORE_SCRIPTS` 目录，商品配置中填写不带 `.py` 的名称。禁止在后台传入命令行、路径或脚本文本。名称只允许英文字母、数字、`_`、`-`。脚本使用当前 Python 虚拟环境，工作目录是脚本目录，无 Shell 拼接。

```python
from extore.sdk import Task, Result, run


def fulfill(task: Task) -> Result:
    email = task.params["account_email"]
    task.progress(20, "正在核对资料")
    # 调用你的平台；必须按 task.idempotency_key 去重。
    # grant = provider.grant(email, idempotency_key=task.idempotency_key)
    task.progress(80, "正在准备交付")
    return Result.success("你的资源或领取链接", message="已完成")


if __name__ == "__main__":
    run(fulfill)
```

`Task` 字段：`id`、`product_id`、`attempt`、`params`。`idempotency_key` 等于稳定任务 ID，跨重试不变。处理进度 0–99，成功由系统写入 100。`Result.success(content=None, message=...)` 供成功交付；服务型商品不需要 content。内容型商品必须有非空 content。

确定没有产生外部交付的失败可返回：

```python
return Result.failure("服务暂时不可用，稍后可重试", retryable=True)
```

不能确定是否已经交付时必须 `retryable=False`。SDK 捕获异常并返回待核实失败，不公开异常文本，防止暴露提供商令牌。输出 stdout 只允许 SDK 的 JSON Lines；调试信息写 stderr（当前 worker 丢弃，不保留用户秘密）。结果后不能继续输出；非零退出、没有结果、超时或输出格式错误一律失败待核实。单进程每次执行一个脚本任务，Webhook 投递同时独立运行。

### 底层输入输出协议

stdin 是单个 JSON 对象：

```json
{"id":"任务 UUID","product_id":"商品 UUID","attempt":1,"params":{"account_email":"user@example.com"}}
```

stdout 可输出若干进度行，最后一个结果行：

```json
{"kind":"progress","progress":30,"message":"资料已验证"}
{"kind":"result","state":"succeeded","content":"交付内容","message":"已完成","retryable":false}
```

脚本不能依赖外部进程继承终端或环境代理；`EXTORE_SCRIPT_*` 环境变量可以提供脚本专用配置和凭证。脚本能访问 worker 用户可读写的文件，不提供不可信脚本隔离。如需跑第三方脚本，应在外部受限容器服务处理，通过签名回调返回状态。

## 外部平台验签与回调

```python
from extore.sdk import Client, verify_event

# body 是收到的原始 bytes，headers 是请求头映射。
event = verify_event(product_secret, body, headers)

# 验签之后持久化 event["id"] 并去重，再异步执行。
if event["type"] == "redemption.requested":
    data = event["data"]
    task_id = data["id"]
    attempt = data["attempt"]
    product_id = event["product_id"]

    client = Client("https://redeem.example.com", product_secret)
    client.update(product_id, task_id, attempt,
                  state="processing", progress=35, message="正在开通服务")
    # 必须以 task_id 去重真实交付。
    client.update(product_id, task_id, attempt,
                  state="succeeded", message="已开通")
```

`Client.update` 每次生成新的时间和 nonce，默认 HTTP 30 秒超时，不自动重试。可以在网络错误时重新调用；成功回调同终态不会覆盖结果。409 可能表示旧尝试、任务已结束或重复 nonce，应查询自身队列与任务记录，不盲目重新发货。

### SDK 测试

`uv run pytest -q` 覆盖脚本真实子进程协议、进度事件、签名、回放与超时恢复。连接真实提供商的幂等交付与支付平台业务流程需要对接后另行测试。

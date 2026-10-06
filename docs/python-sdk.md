# Python SDK

SDK 位于 `extore/sdk/`，任务和回调模块只使用 Python 标准库。二次开发服务可以使用项目包中的 `extore.sdk`，或复制这些模块；安装 SDK 不会向主网站添加处理器。

## 官方自动处理器

主网站只运行 [TokenNotIncluded/extore-processors](https://github.com/TokenNotIncluded/extore-processors) 的已审核白名单。代码放在 `processors/official` Git 子模块中，由父仓库的 gitlink 固定运行版本，不跟随远程分支自动更新。商品用 `processor_id` 选择处理器，用 `processor_config` 保存配置；`mode="script"` 仅保留内部兼容命名。

商家不能安装任意 Python 文件、上传脚本、指定路径或 Git URL。增加处理器需要向官方仓库贡献代码，经审核后随主项目更新固定版本。顾客输入 `parameters` 和交付字段 `outputs` 由处理器代码定义，商品配置不能改写这些字段。

| 处理器 | 商家配置 `processor_config` | 顾客输入 `parameters` | 交付 `output` |
|---|---|---|---|
| `resource_link` | 必填 `resource_url`：HTTPS 地址，最多 2000 字符；可选 `message`：多行文本，最多 10000 字符，默认空串 | 无 | 必填 `resource_url`；可选 `message` |
| `personalized_text` | 必填 `template`：最多 10000 字符，默认 `你好，$name！\n你的商品已准备好。` | 必填 `name`：最多 200 字符 | 必填 `content`：多行文本 |

模板只做纯文本替换：`$name` 和 `${name}` 插入称呼，`$$` 表示美元符号，不执行代码。资源处理器只返回配置的链接，不访问该地址。两个处理器的配置字段均标记为 `secret: true`，顾客 API 不返回 `processor_config`；交付结果在授权领取时返回。

可以保存尚未填完配置的商品草稿，但发行卡密和实际执行前必须通过完整校验。

官方包的 Python 调用示例：

```python
from extore_processors import get_processor

processor = get_processor("personalized_text")
result = processor.run(
    params={"name": "小林"},
    configuration={"template": "你好，$name！\n你的商品已准备好。"},
)
# result == {"status": "succeeded", "output": {"content": "你好，小林！\n你的商品已准备好。"}}
```

### 官方 worker 子进程协议

worker 调用固定模块 `python -m extore_processors <processor_id>`，stdin 只包含以下两个字段：

```json
{"params":{"name":"小林"},"configuration":{"template":"你好，$name！\n你的商品已准备好。"}}
```

当前官方 CLI 成功时输出一条 JSON Lines 结果：

```json
{"kind":"result","state":"succeeded","output":{"content":"你好，小林！\n你的商品已准备好。"}}
```

CLI 校验失败以非零状态退出，只向 stderr 写入安全错误代码，不输出用户值。worker 丢弃 stderr，限制执行时间为 120 秒、stdout 总量为 1 MB、单行读取为 150 KB。结果后继续输出、非零退出、无结果、超时或格式错误均转为不可自动重试的待核实失败。

固定审核代码限制了可运行的处理器集合，不能替代恶意代码的隔离沙箱。需要独立依赖或外部平台访问的自动化服务，可以使用下面的 HTTPS Webhook 与签名回调。

## SDK 任务与结果

`Task` 字段为 `id`、`product_id`、`attempt`、`params`、`configuration`，其中 `configuration` 默认 `{}`。`idempotency_key` 等于稳定任务 ID，跨重试不变；真实交付必须按这个值去重。

```python
from extore.sdk import Result, Task, run


def fulfill(task: Task) -> Result:
    task.progress(20, "正在核对资料")
    name = task.params["name"]
    task.progress(80, "正在准备交付")
    return Result.success(
        output={"content": f"你好，{name}！\n你的商品已准备好。"},
        message="已完成",
    )


if __name__ == "__main__":
    run(fulfill)
```

`run` 是开发环境或外部服务的辅助入口，主网站不会加载这个示例文件。它的 stdin 使用完整 SDK 任务对象，区别于官方 worker 的独立协议：

```json
{"id":"任务 UUID","product_id":"商品 UUID","attempt":1,"params":{"name":"小林"},"configuration":{}}
```

`task.progress(0..99, message)` 向 stdout 输出进度 JSON Lines；成功由系统设为 100。`run` 随后输出包含 `kind="result"`、`state`、`output`、`content`、`message` 和 `retryable` 的结果行。它捕获处理函数异常，返回待核实失败，不公开异常文本。自行编写外部服务时，进度行需要由服务转成 `Client.update` 回调，不能仅打印后期待主网站收到进度。

### 结构化交付结果

新代码使用 `Result.success(output={...}, message=...)` 和 `Client.update(..., output={...})`。`output` 必须是 `dict[str, str]`，键匹配商品定义的 `outputs`，成功时校验必填、类型和未知字段，所有值合计最多 100000 字符。

- 输出类型支持 `text`、`textarea`、`email`、`number`、`url`。文本保留换行和缩进；邮箱、数字、链接去掉首尾空白。数字必须有限，链接仅允许 HTTP/HTTPS 且不能含用户信息。
- 旧 `content` 参数只兼容唯一输出字段为 `content` 的商品。同时传 `content` 与 `output` 时，两者的 `content` 值必须相同。
- 服务型商品 `outputs=[]`，成功只返回状态和说明，不传交付结果；非空 `output` 会被拒绝，旧 `content` 会被忽略。
- 普通任务状态和 Webhook 事件不包含交付结果。授权领取接口返回 `{content, output}`，其中 `content` 是兼容文本；一次性领取和立即销毁会清除两种存储结果。

确定没有产生外部交付的失败，可以返回：

```python
return Result.failure("服务暂时不可用，稍后可重试", retryable=True)
```

不能确定是否已经交付时使用 `retryable=False`。顾客重试还必须满足商品的重试开关和次数限制。`message` 会出现在进度页面，最多 1000 字符；不要在其中放置令牌、原始异常或交付内容。SDK 协议 stdout 只写 JSON Lines，调试信息也不应包含用户秘密。

## 外部平台验签与回调

外部服务使用公网 HTTPS Webhook，配置商品的 `webhook_url` 与私密 `webhook_secret`。只有 `mode="webhook"` 的商品接受状态回调。收到请求后先验签、持久化去重并入队，再快速返回 2xx；下面展示队列消费时的回调过程。

```python
from extore.sdk import Client, verify_event

# product_secret 从服务的私有配置读取。
# body 是收到的原始 bytes，headers 是请求头映射。
event = verify_event(product_secret, body, headers)

# 验签不包含去重：持久化 event["id"]，避免重复消费同一事件。
if event["type"] == "redemption.requested":
    data = event["data"]
    task_id = data["id"]
    attempt = data["attempt"]
    product_id = event["product_id"]

    client = Client("https://extore.lmm.best", product_secret)
    client.update(
        product_id, task_id, attempt,
        state="processing", progress=35, message="正在处理",
    )
    # fulfill_once 由外部服务实现，并按 task_id 去重真实交付。
    # 此例商品的 outputs 定义了 resource_url 和 message。
    output = fulfill_once(task_id, data["params"])
    client.update(
        product_id, task_id, attempt,
        state="succeeded", output=output, message="已完成",
    )
```

服务型商品的完成回调改为 `client.update(product_id, task_id, attempt, state="succeeded", message="已开通")`。失败回调用 `state="failed"` 和适当的 `retryable`，不附交付结果。

`verify_event` 对原始请求体做 HMAC-SHA256 验签，检查 ±300 秒时间窗口；SDK 不替服务保存事件 ID。只处理 `redemption.requested` 来启动交付，进度或完成通知不能再次启动发货。完整事件和签名规则见 [协议文档](protocol.md)。

`Client.update` 每次生成新时间和 nonce，默认 HTTP 超时 30 秒，不自动重试。网络错误时可以重新调用；同一终态的成功回调不覆盖既有结果。HTTP 409 可能表示旧尝试、任务已结束或重复 nonce，应核对服务自己的任务记录，不能据此重新发货。

## 开发验证

`uv run pytest -q` 运行主项目测试。新增处理器需同时验证官方包的配置、输入、输出与子进程协议；外部服务需另行验证事件重复、回调重试、超时，以及真实提供商的幂等交付。项目测试不能替代实际平台对接验收。

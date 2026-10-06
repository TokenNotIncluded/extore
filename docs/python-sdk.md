# Python SDK

SDK 位于 `extore/sdk/`，任务和回调模块只使用 Python 标准库。二次开发服务可以使用项目包中的 `extore.sdk`；单独分发时需保留包层级及 `extore/variants.py` 默认规格辅助模块。安装 SDK 不会向主网站添加处理器。

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

worker 调用固定模块 `python -m extore_processors <processor_id>`，stdin 包含顾客参数、商家配置，以及服务端绑定的规格和步骤元数据：

```json
{
  "params": {"name": "小林"},
  "configuration": {"template": "你好，$name！\n你的商品已准备好。"},
  "variant": {"id":"default","name":"默认规格","description":"","price":null,"currency":"CNY","attributes":{},"enabled":true},
  "steps": [{"id":"prepare","label":{"zh-CN":"准备内容","en":"Prepare"},"done":false}],
  "completed_steps": []
}
```

当前官方 CLI 成功时输出一条 JSON Lines 结果：

```json
{"kind":"result","state":"succeeded","output":{"content":"你好，小林！\n你的商品已准备好。"}}
```

CLI 校验失败以非零状态退出，只向 stderr 写入安全错误代码，不输出用户值。worker 丢弃 stderr，限制执行时间为 120 秒、stdout 总量为 1 MB、单行读取为 150 KB。结果后继续输出、非零退出、无结果、超时或格式错误均转为不可自动重试的待核实失败。

当前官方 CLI 接受并忽略这些元数据，两个预设的执行仍只使用 `params` 与 `configuration`；省略元数据的旧请求保持兼容。顾客填写的参数不能覆盖顶层 `variant`、`steps` 或 `completed_steps`。

固定审核代码限制了可运行的处理器集合，不能替代恶意代码的隔离沙箱。需要独立依赖或外部平台访问的自动化服务，可以使用下面的 HTTPS Webhook 与签名回调。

## SDK 任务与结果

`Task` 字段为 `id`、`product_id`、`attempt`、`params`、`configuration`、`variant`、`steps` 与 `completed_steps`。`configuration` 默认 `{}`，两个步骤列表默认 `[]`；旧任务省略 `variant` 时使用固定默认规格（空属性、空参考价格、币种 `CNY`）。`idempotency_key` 等于稳定任务 ID，跨重试不变；真实交付必须按这个值去重。

`variant` 是发行卡密时冻结的完整规格快照，包含 `id,name,description,price,currency,attributes,enabled`；以后停用规格或改价格、属性都不改旧卡。价格是字符串或 `None`，只作为商家跨平台建商品的参考。`steps` 是当前任务计划，每项含 `id,label,done`；`completed_steps` 是本次尝试已经完成的步骤 ID。输入输出定义也在任务首次提交时冻结，队列商品后来调整表单只影响新任务；处理旧任务时用队列接口返回的任务 `parameters` / `outputs`，不能套用商品当前表单。

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
{
  "id": "任务 UUID",
  "product_id": "商品 UUID",
  "attempt": 1,
  "params": {"name": "小林"},
  "configuration": {},
  "variant": {"id":"standard","name":"标准版","description":"","price":"12.5","currency":"CNY","attributes":{"days":30},"enabled":true},
  "steps": [{"id":"prepare","label":{"zh-CN":"准备内容","en":"Prepare"},"done":false}],
  "completed_steps": []
}
```

`task.progress(0..99, message)` 向 stdout 输出进度 JSON Lines。带步骤的任务使用 `task.progress(message="内容已准备好", completed_steps=["prepare"])`，也可同时提供百分比，但服务器按步骤数量计算；成功由系统设为 100。`run` 随后输出包含 `kind="result"`、`state`、`output`、`content`、`message`、`retryable` 和可选 `completed_steps` 的结果行。它捕获处理函数异常，返回待核实失败，不公开异常文本。自行编写外部服务时，进度行需要由服务转成 `Client.update` 回调，不能仅打印后期待主网站收到进度。

完成集合必须来自任务计划，省略保留原值，不能在同一尝试撤回已完成步骤。完成全部步骤但仍处理中时为 99%，成功回调自动完成全部步骤并设为 100%。`Result.success(..., completed_steps=...)` 与 `Result.failure(..., completed_steps=...)` 可携带完成集合；实际重试开始时清空集合、进度归零，保留任务 ID、规格、输入输出定义和步骤计划。

### 结构化交付结果

新代码使用 `Result.success(output={...}, message=...)` 和 `Client.update(..., output={...})`。`output` 必须是 `dict[str, str]`，键匹配商品定义的 `outputs`，成功时校验必填、类型和未知字段，所有值合计最多 100000 字符。

- 输出类型支持 `text`、`textarea`、`email`、`number`、`url`、`file`。文本保留换行和缩进；邮箱、数字、链接去掉首尾空白。数字必须有限，链接仅允许 HTTP/HTTPS 且不能含用户信息。文件值是该任务当前尝试、对应字段的已上传文件 ID。
- 旧 `content` 参数只兼容唯一输出字段为 `content` 的商品。同时传 `content` 与 `output` 时，两者的 `content` 值必须相同。
- 服务型商品 `outputs=[]`，成功只返回状态和说明，不传交付结果；非空 `output` 会被拒绝，旧 `content` 会被忽略。
- 普通任务状态和 Webhook 事件不包含交付结果。授权领取接口返回 `{content, output, files?}`，其中 `content` 是兼容文本；一次性领取清除两种结果，附件每文件下载时独立消费一次额度。立即销毁同时清除结果与该卡密全部输入、输出文件。

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
    variant = data["variant"]  # 发卡时冻结的规格，不从顾客 params 读取。
    steps = data["steps"]      # 此任务的步骤快照，每项包含 done。

    client = Client("https://extore.lmm.best", product_secret)
    client.update(
        product_id, task_id, attempt,
        state="processing", message="正在处理",
        completed_steps=data["completed_steps"],
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

完成了计划中的步骤后，使用 `client.update(product_id, task_id, attempt, state="processing", completed_steps=["prepare"], message="内容准备完成")`；示例中的 ID 必须替换为该任务 `steps` 中的真实 ID。`Client.update` 的 `completed_steps` 默认为 `None`，保留服务器已完成集合。有计划时服务器按完成数计算百分比；无计划时仍可传 `progress=35`。

`verify_event` 对原始请求体做 HMAC-SHA256 验签，检查 ±300 秒时间窗口；SDK 不替服务保存事件 ID。只处理 `redemption.requested` 来启动交付，进度或完成通知不能再次启动发货。完整事件和签名规则见 [协议文档](protocol.md)。

`Client.update` 每次生成新时间和 nonce，默认 HTTP 超时 30 秒，不自动重试。网络错误时可以重新调用；同一终态的成功回调不覆盖既有结果。HTTP 409 可能表示旧尝试、任务已结束或重复 nonce，应核对服务自己的任务记录，不能据此重新发货。

## 商品队列与附件 API

人和 AI 都通过商品管理会话处理队列：`GET /api/manage/jobs?product_id=...` 查看任务，`POST /api/manage/batch` 领取、提交完成步骤与一句话说明，再按该任务的 `outputs` 提交结果。任务 `steps` 和输入输出定义是冻结快照，商品后续修改不改变现有任务。已有空计划的任务可在批处理请求显式传一次 `progress_steps`；绑定后不能换计划。

顾客可读取真实状态中的 `message`、`steps`、`completed_steps`、`support_email` 与 `variant`。`queue_ahead` 是同商品内按 `(created,id)` 排在前面的排队及处理中任务数量；活跃任务 `queue_position=queue_ahead+1`，终态为 0，不是预计耗时。

文件字段由商家在队列商品的 `parameters` / `outputs` 中配置为 `type="file"`。文件作为数据保存，上传不会安装或运行 Python 代码。使用现有 HTTP 接口上传和领取：

| 操作 | 请求 |
|---|---|
| 顾客上传输入 | `POST /api/files/upload`，multipart 包含 `token,field_key,file`；返回 `id` 放入 `params` |
| 处理者上传输出 | `POST /api/manage/files/upload`，multipart 包含 `job_id,field_key,file`；返回 `id` 放入成功 `output` |
| 管理者查看附件 | `GET /api/manage/files?job_id=...`；下载 `GET /api/manage/files/{id}/download`，均需要同商品的 `queue.view` 会话 |
| 顾客领取结果 | `POST /api/receipt/reveal`，JSON `{token}`，取得 `output` 与可选 `files` 描述 |
| 顾客下载文件 | `POST /api/files/download`，JSON `{token,file_id}` |

单文件最多 20 MiB，每张卡密现存文件最多 100 MiB、100 个；上传只接受一个文件及规定的字段。处理者上传需要 `queue.process`，且任务必须是自己领取的队列任务。文件绑定卡密、商品、任务、尝试及字段，文件 ID 本身不授予读取权限。附件 API 使用管理会话，`Client.update` 用于 Webhook 商品的签名回调。

兑换凭证只放在 POST JSON 或 multipart 请求体，不放入 GET 链接或 URL 查询参数。`once` 商品先显式领取结果，再按返回的 ID 逐个下载；每个文件各可下载一次，第一次下载清除其内容，之后返回 410。`repeat` 可重复下载。销毁清除该卡密的全部输入和输出文件；新尝试删除旧输出草稿，并允许复用原输入。

商品管理链接默认 `max_uses=1`，用于一个新的成功登录会话。明确调用 `POST /api/staff/login` 提交 `{token}` 后保留会话 Cookie；读取页面或认证状态不扣次数，同一有效会话重复登录同一链接也不扣。新会话耗尽额度后返回 409，已登录会话仍按期限与撤销规则使用。会话与登录审计接口、委派范围见 [协议文档](protocol.md#登录会话与审计)。

## 开发验证

`uv run pytest -q` 运行主项目测试。新增处理器需同时验证官方包的配置、输入、输出与子进程协议；外部服务需另行验证事件重复、回调重试、超时，以及真实提供商的幂等交付。项目测试不能替代实际平台对接验收。

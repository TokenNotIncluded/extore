# Python SDK

本页介绍商品处理器和 Webhook v1 的用法。多步流程使用 `FlowDefinition` 定义和校验图，私有 Worker 用 [`FlowScope`、`PrivateWorkerClient`、`FlowExecution`、`verify_flow_event`](python-sdk-v2.md) 执行当前节点，不能拿旧 v1 签名更新流程节点。开发顺序、可验证示例和离线 CLI 见 [处理流程开发](workflow-development.md)。

| 需求 | 接口 |
| --- | --- |
| 本地处理器读取一次任务、报告进度、返回结果 | `Task`、`Result`、`run` |
| 普通 Webhook 商品接收事件、回调状态 | `verify_event`、`Client`，协议 v1 |
| 设计带再次输入或分支的任务图 | `FlowDefinition`，定义 v1 |
| 私有 Worker 接收并完成一个流程节点 | `verify_flow_event`、`FlowScope`、`PrivateWorkerClient.execution`，协议 v2 |

图、配置档案的运行环境和顾客进度计划各有职责：`task_flow` 决定节点转换，配置 `workflow` 决定变量、密钥与资源，`progress_steps` 只描述工作进度。它们不能互相替代。

SDK 位于 `extore/sdk/`，任务和回调模块只使用 Python 标准库。二次开发服务可以使用项目包中的 `extore.sdk`；单独分发时需保留包层级及 `extore/variants.py` 默认规格辅助模块。安装 SDK 不会向主网站添加处理器。

## 预设自动处理器

主网站只运行 [TokenNotIncluded/extore-processors](https://github.com/TokenNotIncluded/extore-processors) 的已审核白名单。代码放在 `processors/official` Git 子模块中，由父仓库的 gitlink 固定运行版本，不跟随远程分支自动更新。商品用 `processor_id` 选择处理器，再绑定本店的加密配置档案；`mode="script"` 仅保留内部兼容命名。旧写入字段 `processor_config` 会转换为配置档案，读取不返回原值，详见[处理器配置档案](shops.md#处理器配置档案)。

商家不能安装任意 Python 文件、上传脚本、指定路径或 Git URL。增加处理器需要向处理器仓库贡献代码，经审核后随主项目更新固定版本。顾客输入 `parameters`、交付字段 `outputs` 和店铺配置 `shop_configuration` 由处理器代码定义，商品配置不能改写这些字段。预设结构仍使用 `schema_version=1`；`configuration` 是 `shop_configuration` 的兼容名称。

| 处理器 | 店铺配置 `shop_configuration` | 顾客输入 `parameters` | 交付 `output` |
|---|---|---|---|
| `resource_link` | 必填 `resource_url`：HTTPS 地址，最多 2000 字符；可选 `message`：多行文本，最多 10000 字符，默认空串 | 无 | 必填 `resource_url`；可选 `message` |
| `personalized_text` | 必填 `template`：最多 10000 字符，默认 `你好，$name！\n你的商品已准备好。` | 必填 `name`：最多 200 字符 | 必填 `content`：多行文本 |

模板只做纯文本替换：`$name` 和 `${name}` 插入称呼，`$$` 表示美元符号，不执行代码。资源处理器只返回配置的链接，不访问该地址。`template` 和 `message` 明确标记为 `secret: false`，店主可在处理器配置中回读编辑；`resource_url` 保持敏感。缺少 `secret` 标记的字段默认敏感，不回读原值。顾客只在授权领取时获取交付结果。发行卡密时固定配置版本，worker 仅解密该卡所属店铺的版本；更新配置不改变旧卡，撤销配置则阻止已有版本继续执行。

可以保存尚未绑定配置的商品草稿，但发行卡密和实际执行前必须通过完整校验。配置档案本身须符合代码定义的结构。

预设包的 Python 调用示例：

```python
from extore_processors import get_processor

processor = get_processor("personalized_text")
result = processor.run(
    params={"name": "小林"},
    configuration={"template": "你好，$name！\n你的商品已准备好。"},
)
# result == {"status": "succeeded", "output": {"content": "你好，小林！\n你的商品已准备好。"}}
```

### 预设 worker 子进程协议

worker 在隔离运行环境中执行固定的 `extore_processors` 模块，stdin 包含顾客参数、商家配置，以及服务端绑定的规格、步骤和环境元数据：

```json
{
  "params": {"name": "小林"},
  "configuration": {"template": "你好，$name！\n你的商品已准备好。"},
  "variant": {"id":"default","name":"默认规格","description":"","price":null,"currency":"CNY","attributes":{},"enabled":true},
  "steps": [],
  "completed_steps": [],
  "shop_context": {"shop_id":"所属店铺 UUID","profile_id":"配置档案 UUID","revision":1},
  "environment": {"OUTPUT_LOCALE":"zh-CN"}
}
```

空计划时，内置预设先定义两个步骤，实际完成校验与结果准备后分别更新进度，最后输出唯一结果行。stdout 使用 JSON Lines，示意如下：

```jsonl
{"kind":"progress","progress_steps":[{"id":"validate_input","label":{"zh-CN":"校验资料","en":"Validate input"}},{"id":"prepare_delivery","label":{"zh-CN":"准备交付","en":"Prepare delivery"}}],"progress":0,"completed_steps":[],"message":"正在准备处理"}
{"kind":"progress","progress":50,"completed_steps":["validate_input"],"message":"资料已核实，正在准备交付"}
{"kind":"progress","progress":99,"completed_steps":["validate_input","prepare_delivery"],"message":"交付内容已准备好"}
{"kind":"result","state":"succeeded","output":{"content":"你好，小林！\n你的商品已准备好。"}}
```

CLI 校验失败以非零状态退出，只向 stderr 写入安全错误代码，不输出用户值。worker 丢弃 stderr，限制执行时间为 120 秒、stdout 总量为 1 MB、单行读取为 150 KB。结果后继续输出、非零退出、无结果、超时或格式错误均转为不可自动重试的待核实失败。

worker 只允许尚无计划、无已完成步骤的任务初始化一次 `progress_steps`，随后把计划保存为任务快照；已有计划不能更换。已有计划含预设自己的步骤 ID 时，预设按实际完成项更新；遇到其他商家计划时，只报告百分比和消息，不伪造已完成项。有步骤时最终进度由服务端按完成集合计算，百分比不能绕过商家计划。顾客填写的参数不能覆盖顶层 `variant`、`steps`、`completed_steps`、`shop_context` 或 `environment`。

固定审核代码限制了可运行的处理器集合，不能替代恶意代码的隔离沙箱。需要独立依赖或外部平台访问的自动化服务，可以使用下面的 HTTPS Webhook 与签名回调。

## SDK 任务与结果

`Task` 原有字段为 `id`、`product_id`、`attempt`、`params`、`configuration`、`variant`、`steps`、`completed_steps`、`shop_context`、`environment` 与 `instructions`。`configuration` 和 `environment` 默认 `{}`；环境、步骤计划和完成集合转为只读快照，默认空集合。`ShopContext` 是不可变的 `shop_id/profile_id/revision` 元数据，不含配置秘密，旧请求可省略。旧任务省略 `variant` 时使用固定默认规格（空属性、空参考价、币种 `CNY`）。`idempotency_key` 等于稳定任务 ID，跨重试与修改不变，用于整单一次的付款、开通等副作用。

### 卡属性与交付版本

0.10.0 新增 `Task.card_attributes`、`entitlements`、`revision`、`deliveries` 与 `last_delivery`，均复制为不可变快照，不从顾客 `params` 取值。属性是所选规格默认属性加商家批次覆盖后的发行快照；权益的计数键由商品 `revision_policy` 选择，不固定叫“修改次数”。旧信封省略这些字段时保持兼容：空属性、无权益、初稿轮次 0、空历史、无最近交付。

```python
def handle(task: Task):
    version = task.revision["current"]       # 0 初稿，1 第一次修改
    feedback = task.revision.get("message", "")
    remaining = task.entitlements["remaining"] if task.entitlements else 0
    attributes = task.card_attributes        # 不能由顾客同名参数改变
    version_key = task.delivery_idempotency_key
    # 本地制作／内容保存按 version_key 去重；原稿的技术重试仍是同一个键。
    # 外部付款继续使用 task.idempotency_key，不能因 version 增加再付款。
```

`delivery_idempotency_key` 为 `任务ID:revision:轮次`，同轮次技术重试不变，新修改才变化；不会改变原 `idempotency_key` 的兼容语义。`revision.message` 是顾客修改建议，应当作内容处理，不能执行其中的命令或扩大授权。`deliveries` 与 `last_delivery` 只含历史时间、轮次、是否有附件等元数据，不自动暴露历史正文。当前回调继续带真实 `attempt`，旧尝试不能提交新稿。

### 工厂与车间提示词

0.9.1 新增 `WorkInstructions`，字段为 `schema,shop_id,product_id,factory_slogan,workshop_slogan,revision`，格式见[AI 队列处理](automation-cli.md#工厂与电子车间提示词)。`Task.instructions` 默认 `None` 以兼容旧请求；提供时验证所属商品及已给出的 `shop_context.shop_id`，复制为不可变对象。两条提示词各最多 4000 字符，不进入顾客 `params`，顾客同名参数不能覆盖。

```python
from extore.sdk import Task, WorkInstructions

def prepare_ai_brief(task: Task):
    if task.instructions is None:
        return []  # 旧请求沿用商家已有工作说明。
    return [task.instructions.factory_slogan, task.instructions.workshop_slogan]
```

AI 型处理器在干活前读取提示词，普通确定性处理器可忽略；SDK 不自动执行文字中的命令、访问链接或扩大授权。提示词从服务端读取，不能从顾客参数构造。每次派发使用当前配置，`revision` 表示内容版本；提示词与发行卡密时冻结的处理器环境配置是两回事。`Task` 的自动 `repr` 不包含提示词全文。

### 变量、密钥与运行环境

处理器配置的 `workflow` 分为普通 `variables`、私密 `secrets` 和资源上限 `runtime`。店主可回读普通变量；密钥只返回已设置的名称，不回读值。配置保存后生成新版本，发行卡密时固定该版本。重试仍使用原版本，不受之后修改的变量、密钥或运行上限影响。

worker 把该卡固定版本的普通变量与密钥合并为顶层 `environment: dict[str, str]`，只提供给审核后的处理器代码。SDK 用 `task.environment["NAME"]`，预设包用 `context.environment["NAME"]` 读取原名称；两个映射都先复制再设为只读，不能通过修改传入字典或顾客参数覆盖它们。省略 `environment` 保持兼容，得到空映射；显式 `null`、嵌套值或其他非字符串值会被拒绝。

```python
from extore.sdk import Task
from extore_processors import ProcessorContext

# 实际 worker 从卡密固定的配置版本构造这些值。
task = Task("task-id", "product-id", 1, {}, environment={"OUTPUT_LOCALE": "zh-CN"})
context = ProcessorContext(environment={"OUTPUT_LOCALE": "zh-CN"})
locale = task.environment.get("OUTPUT_LOCALE", "en")
assert locale == context.environment["OUTPUT_LOCALE"]
# task.environment["OUTPUT_LOCALE"] = "en"  # TypeError：不可修改。
```

隔离进程也获得带前缀的操作系统环境变量，例如 `os.environ.get("EXTORE_WORKFLOW_OUTPUT_LOCALE")`；不会导出裸 `OUTPUT_LOCALE`，也不把密钥放进命令参数。配置名称必须匹配 `[A-Z][A-Z0-9_]{0,63}`，系统名称、`EXTORE_` 等保留前缀被拒绝；变量与密钥不可重名。每组最多 64 项，合并后最多 128 项；每个值 UTF-8 编码最多 8192 字节，所有值合计最多 65536 字节，不能包含 NUL。整个 stdin JSON（顾客参数、配置、环境和全部元数据）仍最多 200000 个 UTF-8 字节，超限会在处理器执行前拒绝，不截断值。

环境不会自动插入 `params`、模板、进度消息或交付 `output`。顾客在 `params` 中填写 `environment` 也不会创建运行变量。内置文本模板仍只支持顾客称呼 `$name`，不能读取环境密钥。`Task` 的自动 `repr` 省略环境；代码仍须避免打印环境映射、密钥、原始异常，或把它们放进结果。运行环境不开放任意命令、文件挂载或网络访问；需要外部平台网络的处理流程可使用签名 Webhook。

`variant` 是发行卡密时冻结的完整规格快照，包含 `id,name,description,price,currency,attributes,enabled`；以后停用规格或改参考价、属性都不改旧卡。`price` 是参考价，以十进制字符串或 `None` 保存，仅供商家在外部商城配置商品时参考；Extore 只负责兑换与交付，不收款。SDK 的 `steps` 每项含只读 `id,label`；页面和事件视图另外带 `done`。`completed_steps` 是本次尝试已经完成的步骤 ID。输入输出定义也在任务首次提交时冻结，队列商品后来调整表单只影响新任务；处理旧任务时用队列接口返回的任务 `parameters` / `outputs`，不能套用商品当前表单。

```python
from extore.sdk import Result, Task, run


def fulfill(task: Task) -> Result:
    if not task.steps:
        task.define_steps(
            [
                {"id": "check", "label": {"zh-CN": "核对资料", "en": "Check input"}},
                {"id": "prepare", "label": {"zh-CN": "准备交付", "en": "Prepare"}},
            ],
            message="准备开始",
        )
    own_plan = [step["id"] for step in task.steps] == ["check", "prepare"]
    name = task.params["name"]
    checked = (
        list(dict.fromkeys([*task.completed_steps, "check"])) if own_plan else None
    )
    task.progress(50, "资料已核实", completed_steps=checked)
    content = f"你好，{name}！\n你的商品已准备好。"
    task.progress(
        99,
        "内容已准备好",
        completed_steps=["check", "prepare"] if own_plan else None,
    )
    return Result.success(
        output={"content": content},
        message="已完成",
    )


if __name__ == "__main__":
    run(fulfill)
```

`run` 是开发环境或外部服务的辅助入口，主网站不会加载这个示例文件。它的 stdin 使用完整 SDK 任务对象，区别于预设 worker 的独立协议：

```json
{
  "id": "任务 UUID",
  "product_id": "商品 UUID",
  "attempt": 1,
  "params": {"name": "小林"},
  "configuration": {},
  "variant": {"id":"standard","name":"标准版","description":"","price":"12.5","currency":"CNY","attributes":{"days":30},"enabled":true},
  "steps": [],
  "completed_steps": [],
  "shop_context": {"shop_id":"所属店铺 UUID","profile_id":null,"revision":null},
  "environment": {"OUTPUT_LOCALE":"zh-CN"}
}
```

`task.define_steps(plan, message="")` 为当前空计划初始化一次 1–30 个有唯一 ID 的步骤，输出 `kind="progress"` 与 `progress_steps`。已有计划或完成项时拒绝替换；顺序与标签固定，重试保留计划。`task.progress(0..99, message)` 输出进度 JSON Lines。带步骤的任务使用 `task.progress(message="内容已准备好", completed_steps=["prepare"])`，也可同时提供百分比，但服务器按步骤数量计算；成功由系统设为 100。

`run` 随后输出包含 `kind="result"`、`state`、`output`、`content`、`message`、`retryable` 和可选 `completed_steps` 的结果行。它限制整个 stdin 为 200000 个 UTF-8 字节，包括 `configuration` 与 `environment`，并将无效输入或处理异常转换为安全的待核实失败，不公开异常文本。自行编写外部服务时，进度行需要由服务转成 `Client.update` 回调，不能仅打印后期待主网站收到进度。当前 `Client.update` 支持更新已存在计划的完成项，不接受 `progress_steps`；外部 Webhook 的计划应先在商品中定义。

完成集合必须来自任务计划，省略保留原值，不能在同一尝试撤回已完成步骤。完成全部步骤但仍处理中时为 99%，成功回调自动完成全部步骤并设为 100%。`Result.success(..., completed_steps=...)` 与 `Result.failure(..., completed_steps=...)` 可携带完成集合；实际重试开始时清空集合、进度归零，保留任务 ID、规格、输入输出定义和步骤计划。

### 结构化交付结果

交付继续使用字符串对象。`select` 是声明过的选项代码，`boolean` 是 `"true"` / `"false"`，`image` 是本任务上传的文件 ID，`images` 是保持顺序的文件 ID JSON 数组字符串，例如 `json.dumps(file_ids, separators=(",", ":"))`。文件 ID 不能从另一张卡密或另一个节点复制；图片必须通过实际内容校验。普通字段与范围见[协议](protocol.md#输入与输出)，流程 Worker 的附件上传使用 v2 客户端。

新代码使用 `Result.success(output={...}, message=...)` 和 `Client.update(..., output={...})`。`output` 必须是 `dict[str, str]`，键匹配商品定义的 `outputs`，成功时校验必填、类型和未知字段，所有值合计最多 100000 字符。

- 输出类型支持 `text`、`textarea`、`email`、`number`、`url`、`file`、`select`、`boolean`、`image`、`images`。文本保留换行和缩进；邮箱、数字、链接去掉首尾空白。数字必须有限，链接仅允许 HTTP/HTTPS 且不能含用户信息。文件值是该任务当前尝试、对应字段的已上传文件 ID。
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
if event["type"] in ("redemption.requested", "revision.requested"):
    data = event["data"]
    task_id = data["id"]
    attempt = data["attempt"]
    product_id = event["product_id"]
    variant = data["variant"]  # 发卡时冻结的规格，不从顾客 params 读取。
    steps = data["steps"]  # 此任务的步骤快照，每项包含 done。

    client = Client("https://extore.lmm.best", product_secret)
    client.update(
        product_id,
        task_id,
        attempt,
        state="processing",
        message="正在处理",
        completed_steps=data["completed_steps"],
    )
    # fulfill_once 由外部服务实现。内容按交付版本去重，技术重试同键；
    # 付款等整单一次的副作用另外按 task_id 去重。
    # 此例商品的 outputs 定义了 resource_url 和 message。
    revision = data.get("revision", {"current": 0, "message": ""})
    version_key = f"{task_id}:revision:{revision['current']}"
    output = fulfill_once(version_key, data["params"], revision.get("message", ""))
    client.update(
        product_id,
        task_id,
        attempt,
        state="succeeded",
        output=output,
        message="已完成",
    )
```

服务型商品的完成回调改为 `client.update(product_id, task_id, attempt, state="succeeded", message="已开通")`。失败回调用 `state="failed"` 和适当的 `retryable`，不附交付结果。

完成了计划中的步骤后，使用 `client.update(product_id, task_id, attempt, state="processing", completed_steps=["prepare"], message="内容准备完成")`；示例中的 ID 必须替换为该任务 `steps` 中的真实 ID。`Client.update` 的 `completed_steps` 默认为 `None`，保留服务器已完成集合。有计划时服务器按完成数计算百分比；无计划时仍可传 `progress=35`。

`verify_event` 对原始请求体做 HMAC-SHA256 验签，检查 ±300 秒时间窗口，保留原事件中的卡属性、权益和轮次字段；SDK 不替服务保存事件 ID。处理 `redemption.requested` 与 `revision.requested` 来启动交付，进度或完成通知不能再次启动发货。完整事件和签名规则见 [协议文档](protocol.md)。

`Client.update` 每次生成新时间和 nonce，默认 HTTP 超时 30 秒，不自动重试。网络错误时可以重新调用；同一终态的成功回调不覆盖既有结果。HTTP 409 可能表示旧尝试、任务已结束或重复 nonce，应核对服务自己的任务记录，不能据此重新发货。

## 商品队列与附件 API

人和 AI 都通过商品管理会话处理队列：`GET /api/manage/jobs?product_id=...` 查看任务，`POST /api/manage/batch` 领取、提交完成步骤与一句话说明，再按该任务的 `outputs` 提交结果。任务 `steps` 和输入输出定义是冻结快照，商品后续修改不改变现有任务。已有空计划的任务可在批处理请求显式传一次 `progress_steps`；绑定后不能换计划。

顾客可读取真实状态中的 `message`、`steps`、`completed_steps`、`support_email` 与 `variant`。`queue_ahead` 是同商品内按 `(created,id)` 排在前面的排队及处理中任务数量；活跃任务 `queue_position=queue_ahead+1`，终态为 0，不是预计耗时。

文件字段由商家在队列商品的 `parameters` / `outputs` 中配置为 `type="file"`。文件作为数据保存，上传不会安装或运行 Python 代码。使用现有 HTTP 接口上传和领取：

| 操作 | 请求 |
|---|---|
| 顾客上传输入 | `POST /api/files/upload`，multipart 包含 `token,field_key,file`；返回 `id` 放入 `params` |
| 处理者上传输出 | `POST /api/manage/files/upload`，multipart 包含 `job_id,field_key,file,attempt?`；修改轮次必填实际领取的 `attempt`，返回 `id` 放入成功 `output` |
| 管理者查看附件 | `GET /api/manage/files?job_id=...`；下载 `GET /api/manage/files/{id}/download`，均需要同商品的 `queue.view` 会话 |
| 顾客领取结果 | `POST /api/receipt/reveal`，JSON `{token}`，取得 `output` 与可选 `files` 描述 |
| 顾客下载文件 | `POST /api/files/download`，JSON `{token,file_id}` |

单文件最多 20 MiB，每张卡密现存文件最多 100 MiB、100 个；上传只接受一个文件及规定的字段。处理者上传需要 `queue.process`，且任务必须是自己领取的队列任务。文件绑定卡密、商品、任务、尝试及字段，文件 ID 本身不授予读取权限。附件 API 使用管理会话，`Client.update` 用于 Webhook 商品的签名回调。

兑换凭证只放在 POST JSON 或 multipart 请求体，不放入 GET 链接或 URL 查询参数。`once` 商品先显式领取结果，再按返回的 ID 逐个下载；每个文件各可下载一次，第一次下载清除其内容，之后返回 410。`repeat` 可重复下载。销毁清除该卡密的全部输入和输出文件；新尝试删除旧输出草稿，并允许复用原输入。

商品管理链接默认 `max_uses=1`，用于一个新的成功登录会话。明确调用 `POST /api/staff/login` 提交 `{token}` 后保留会话 Cookie；读取页面或认证状态不扣次数，同一有效会话重复登录同一链接也不扣。新会话耗尽额度后返回 409，已登录会话仍按期限与撤销规则使用。会话与登录审计接口、委派范围见 [协议文档](protocol.md#登录会话与审计)。

## 开发验证

`uv run pytest -q` 运行主项目测试。新增处理器需同时验证预设包的配置、输入、输出与子进程协议；外部服务需另行验证事件重复、回调重试、超时，以及真实提供商的幂等交付。项目测试不能替代实际平台对接验收。

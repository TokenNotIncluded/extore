# 处理流程开发

一次填写、一次交付的商品保持 `task_flow: null`。只有需要再次向顾客提问、确认中间结果、按选择分支或等待短期验证码时，才配置多步流程。Extore 负责验码、编排、权限和交付；实际制作由商品处理器、获授权的队列处理者或私有 Worker 完成。

## 四种定义，各管一件事

| 定义 | 用途 | 版本或身份 |
| --- | --- | --- |
| 商品 `task_flow` | 顾客输入、处理、展示和结束的声明式图 | 定义 `version: 1` |
| 私有 Worker 协议 | 向第三方派发当前处理节点、接收签名结果和附件 | 传输 `version: 2` |
| 处理器配置 `workflow` | 本店普通变量、密钥和运行资源上限 | 配置档案及其 `revision` |
| 商品 `progress_steps` | 向顾客说明工作进度 | 任务的步骤快照 |

流程 v1 可以通过 Worker v2 执行，两者版本号不同是正常的。进度勾选不会推进流程节点，流程也不能指定另一套处理器、密钥、命令或网络目标。所有 process 节点使用该商品发行时固定的执行方式。

## 从可验证的示例开始

仓库的 [`examples/workflows`](../examples/workflows/README.md) 提供三套 JSON：

| 示例 | 适合场景 | 需要执行端完成的工作 |
| --- | --- | --- |
| [阿拉丁三问](../examples/workflows/aladdin/flow.json) | 每问一次，回答后进入下一问 | 依次领取三个 process 节点，提交真实答案 |
| [文件入、文件出](../examples/workflows/file-transform/flow.json) | 文档转换、素材整理 | 下载输入、处理、上传输出、提交交付说明 |
| [选择与拒绝](../examples/workflows/choice-and-rejection/flow.json) | 有服务范围限制的任务 | 按需求返回 `decision`，由图选择交付或拒绝 |

每套的 `product.json` 是校验用的商品配置，`flow.json` 是它的 `task_flow` 值。示例使用队列方式，不会自行调用模型、制作文件或访问第三方服务。

```sh
# 不需要登录、联网或创建数据库。
extore workflow validate \
  --definition examples/workflows/aladdin/flow.json \
  --product examples/workflows/aladdin/product.json

# 导出编辑器可使用的结构 Schema。
extore workflow schema --output ./task-flow.schema.json

# 保存服务端同一校验器生成的规范化定义与摘要。
extore workflow validate \
  --definition examples/workflows/file-transform/flow.json \
  --product examples/workflows/file-transform/product.json \
  --output ./validated-flow.json
```

`validate` 返回 `{ok, definition, summary}`；`definition` 是规范化图，不是请求中 JSON 的原样复制。定义文件可以是 `null`，表示简单流程。`--output` 将整个响应保存到新建的 `0600` 文件，终端只返回路径和字节数，不覆盖已有文件；使用保存结果时取其中的 `definition` 作为商品流程。失败以状态码 2 退出，stderr 返回安全错误代码。

公开的 [`GET /api/task-flows/schema`](https://extore.lmm.best/api/task-flows/schema) 返回 JSON Schema Draft 2020-12。Schema 适合编辑器提示和结构校验；节点唯一性、引用、字段兼容和商品输出仍须通过 `validate` 或 SDK 的规范校验。保存商品和发行卡密也会再次校验。离线校验不执行处理器，不证明业务结果正确，也不检查真实附件权限或第三方服务是否可用。

在编辑器设置中把导出的 Schema 关联到流程文件即可。流程 v1 严格限制顶层字段，不要把 `$schema` 加进 `flow.json`；它属于外部校验工具的配置，不是任务图的一部分。

编辑器可调用已认证的 `POST /api/admin/task-flows/validate`；商品管理会话用 `POST /api/manage/task-flows/validate`，需 `fulfillment.configure` 权限。只提交明确草稿：

```json
{
  "definition": null,
  "product": {
    "mode": "manual",
    "parameters": [],
    "outputs": []
  }
}
```

将 `definition` 换成图对象即可校验复杂流程，响应与离线 CLI 一样包含规范 `definition` 和 `summary`。请求拒绝未声明的字段，不接收商品、配置档案或店铺 ID，也不加载已保存的密钥。它不发行卡密、不创建任务或业务事件；正常认证会话仍会更新访问记录。Schema 响应类型为 `application/schema+json`，草稿验证响应为 JSON。

## 用 SDK 构造和校验

安装项目包后使用 `extore.sdk`，无需导入内部服务模块：

```python
import json
from pathlib import Path

from extore.sdk import FlowDefinition

directory = Path("examples/workflows/choice-and-rejection")
product = json.loads((directory / "product.json").read_text(encoding="utf-8"))
definition = json.loads((directory / "flow.json").read_text(encoding="utf-8"))

# 和服务端使用同一个规范校验器。
flow = FlowDefinition.from_dict(definition, product=product)
canonical = flow.as_dict()  # 独立副本，修改它不会改变 flow。
encoded = flow.to_json()   # UTF-8 可编码的紧凑 JSON 字符串。
```

`FlowDefinition.from_nodes(entry, nodes, product=product)` 接受节点列表；`FlowDefinition.schema()` 返回结构 Schema 的副本。`from_dict` 和 `from_nodes` 校验非空图，简单流程直接使用 `None`，不必创建 SDK 对象。

构造引用和条件时可使用以下辅助方法。它们生成 JSON 数据，不执行表达式；放入图后还要进行整图校验。

```python
source = FlowDefinition.reference("review", "decision")
condition = FlowDefinition.equals("review", "decision", "rejected")
route = FlowDefinition.branch([(condition, "rejected")], default="finished")

# 另外两种条件：比较字符串集合，或检查字段有非空值。
allowed = FlowDefinition.one_of("request", "request_kind", ["general"])
provided = FlowDefinition.exists("request", "requirements")
```

节点 ID 用 `[a-z][a-z0-9_-]{0,63}`，字段代码用 `[a-z][a-z0-9_]{0,39}`。用户可见文字用语言到文本的映射，如 `{"zh-CN":"准备交付","en":"Prepare delivery"}`。字段值统一为字符串；选择传选项代码、布尔传 `"true"` / `"false"`、数字传十进制文本，图片集合传文件 ID 的 JSON 数组字符串。

## 节点、连线和引用

| 节点 | 顾客或执行者的动作 | 转移规则 |
| --- | --- | --- |
| `input` | 开始后回答本步字段 | `next`，可有顺序条件分支 |
| `process` | 执行者读取映射后的输入并提交本步输出 | 成功走 `next`；明确可重试失败走 `failure_next` |
| `display` | 查看指定中间结果后继续 | 固定 `next` |
| `end` | 无需继续执行 | `succeeded`、`failed` 或 `rejected` |

`{node, field}` 引用已完成步骤的值。`process.inputs` 给执行者选取输入；`show_from` 选取顾客能看到的中间结果；成功 end 的 `result` 映射商品最终 outputs。展示不是预填，执行者不会因此得到整图历史数据。循环再次访问一个节点时，引用取当前尝试中最近一次完成的值。

条件只支持 `eq`、`in`、`exists`，按 cases 顺序选择第一个命中目标，否则走 default。敏感值不能用于分支、展示或最终交付。引用字段必须存在且类型兼容；设计分支时还要保证该字段在实际路径中已经完成，不能把静态结构校验当作每条业务路径的执行证明。

第一步无论 `start_policy` 如何都等待顾客明确开始，验码、查看和批量准备不会开始计时。后续 input 的 `confirm` 继续等开始；`automatic` 进入即显示问题并计时。需要先读完答案再继续时使用 display。阿拉丁示例后续输入设为 automatic，并用 `show_from` 展示上一答案。

最终附件只能引用 process 的已上传输出，不能把顾客原材料的文件 ID 当作成品交付。中间 process 成功只保存本步结果，成功 end 才完成卡密兑换和最终交付。

## 配置、发行和执行

在商品的处理流程编辑器中配置节点、分支、引用和期限，或通过商品 JSON 写入 `task_flow`。先校验、再保存；发行前确认处理方式、输入输出与最终结果映射。将示例的 `flow.json` 合并到 `product.json` 的 `task_flow` 字段后，可用 [店主 CLI](cli-owner.md) 的 `product create --json-file` 创建商品；真实店铺的可见性、重试政策和处理权限仍由商家决定。

发行卡密时固定图、字段、交付政策及执行目标。之后编辑商品只影响新卡；队列、脚本和 Worker 必须使用当前任务返回的定义，不可把商品最新表单套到旧卡上。运行环境变量和密钥由本店配置档案提供，不放入流程图或顾客参数；详情见 [Python SDK](python-sdk.md#变量密钥与运行环境)。

队列中的 AI 用设备码申请授权，按批准的商品范围处理。使用 `next --watch` 领取任务后读取当前输入输出、`attempt`、`flow_epoch` 和 `action_id`，再更新进度与交付；普通列表保持摘要，完整输入仅按需读取。授权、原子领取和恢复命令见 [AI 队列处理](automation-cli.md)。顾客可用 [多步兑换 CLI](customer-task-flow-cli.md) 开始、回答、继续和领取。

私有 Worker 的签名派发包含精确当前执行身份：店铺、商品、任务、尝试、节点、epoch 和 action ID。用 `verify_flow_event` 验签后构造 `FlowScope`，再用 `client.execution(scope)` 取得便捷执行接口，见 [Worker SDK](python-sdk-v2.md)。节点结果仍使用 `processing`、`succeeded`、`failed`，没有单独的 `rejected` 处理回调状态。

## 失败、重试、拒绝与超时

这几种情况需要明确区分：

| 情况 | 如何表达 | 结果 |
| --- | --- | --- |
| 确定没有外部副作用、需要重新填写 | process 返回 `failed` 且 `retryable: true`，将 `failure_next` 指向 input 或可重试 failed end | 沿明确失败路径转换；不是直接保证整卡可重试 |
| 当前流程可重新开始 | failed end 设 `retryable: true` | 还须满足商品的允许重试、次数等政策 |
| 超出服务范围或拒绝请求 | process 输出 `decision`，成功后按它分支到 rejected end | 终止处理，不附最终交付 |
| 不确定外部动作是否已发生 | process 返回不可重试失败 | 直接停止，等商家核实 |
| 顾客输入或展示超时 | 配置 `timeout_seconds` 和 `timeout_next` | 跳到指定节点 |
| process 超时 | process 必须配置期限与 timeout target | 直接停止并等待商家核实，不自动执行 timeout target |

选择与拒绝示例里的 review 输出为 `decision` 和 `response`。两项都必填；返回拒绝时仍需给本步 response，但 rejected end 不把它作为最终交付。顾客看到的是拒绝 end 的已配置消息。它不是 Worker 随意写入新状态的通道。

计时从服务器开始对应步骤时确定，更新进度、续租或断线重连都不延长节点期限。收到超时、冲突或执行身份已过期的回执后重新读取状态，不能猜测新 epoch、自动发起另一轮外部支付或把旧结果写入新步骤。外部动作一旦发生，取消和超时都不能撤回它。

## 幂等与故障验收

Worker 在开始业务前持久保存传输 nonce 的重放记录，并按 `action_id` 原子去重执行。每个上传和结果回调显式指定 `result_id`；网络重试沿用相同 ID 和完全相同的文件或正文，SDK 只更新传输签名，不替你重做业务。不同的进度更新使用不同 ID。

支付、开通等整单一次的外部副作用还需自己的稳定业务幂等键，不能只依赖 attempt、epoch 或回调去重。服务超时后先核实外部结果，不能因为本地没有收到成功就重新执行。

上线前至少检查：三问的开始与后续计时、每条分支、缺失输出、文件从上传到最终下载、相同请求重复投递、旧 epoch 回调、输入与处理超时、顾客重试、拒绝和销毁。图最多 64 节点，一个运行累计最多 256 次激活；循环和重试都计数，不应依赖无限循环。

结构细节见 [任务流程](task-flow.md)，服务器事务、执行授权和文件绑定见 [核心契约](task-flow-contract.md)，签名及附件协议见 [私有 Worker 协议](private-worker.md)。

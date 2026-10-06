# 商品任务流程

任务流程把「顾客填写 → 处理 → 展示结果 → 下一次填写」定义为有界的声明式图。它用于分步任务，不运行图中的代码，也不让顾客选择处理程序、账号或联网目标。普通商品保留 `task_flow:null`，继续一次填写、一次处理的兑换方式。

`progress_steps` 是给顾客看的进度计划，`task_flow` 决定真实的输入与处理转换。完成进度勾选不会自行跳到图的下一节点。

商品处理方式仍决定执行者：`manual` 由获授权的人或 AI 领取当前处理节点；`script` 使用审核过的固定处理器和店铺配置；`webhook` 向固定的 HTTPS 公网 Worker 派发当前节点。一卡一文本 `stock` 不使用流程。系统提供编排、权限和交付接口，不内置持续运行的通用 AI 模型。

## 发行快照与开始

图、处理目标、字段结构和对应配置在发行卡密时冻结。编辑商品只改变之后发行的卡密。缺少流程快照的旧卡继续使用旧兑换路径，不能在读取时套上后来创建的图。

验码只返回安全入口预览；批量确认使用 `params:{}` 准备 waiting 任务。初始 input 或 display 入口都在明确开始前等待，没有截止时间；input 的题目与输入控件也只在开始后出现。多张卡密分别开始，不在粘贴、验码或批量准备时替顾客开始所有计时。

输入节点的 `start_policy="confirm"` 要求顾客明确开始；`automatic` 允许后续输入节点在前一步完成后立即进入输入与计时。初始入口始终先确认开始；后续 display 进入即开始其展示时限。需要用户读完上一份结果再答下一题时，插入 display 节点，由顾客点击继续。

## 节点与引用

定义为 `{version:1,entry,nodes}`。节点 `id` 匹配 `[a-z][a-z0-9_-]{0,63}`，图内唯一；入口可为 input 或 display。节点最多 64 个，一个运行累计最多推进 256 次（包括整单重试与回跳），防止循环无限运行。规范定义最多 100000 字节，发行快照与一次字段值分别最多 200000 字节 UTF-8 编码；每卡的快照、步骤及派发密文合计最多 2 MiB，并计入店铺与全站额度。

| kind | 作用与关键字段 |
| --- | --- |
| `input` | `prompt` 是开始前提示，`question` 是开始后的题目；fields 定义当前输入。`show_from` 只展示明确引用的非敏感前序值。next 可为固定目标或有限条件分支。 |
| `process` | inputs 将前序字段映射为当前需求，outputs 定义这个节点的结果。必须定义 next、failure_next、timeout_next 和处理时限。执行者只能取得当前映射，不能读取整图所有输入。 |
| `display` | content 为 i18n 说明，show_from 展示指定结果；顾客明确 continue 后进入 next。可配置展示时限。 |
| `end` | state 为 succeeded、failed 或 rejected。成功用 result 将已有结果映射到商品最终 outputs；失败与拒绝不附交付结果。 |

引用是 `{node:"前序节点代码",field:"字段代码"}`，inputs / show_from / result 的键是本节点或最终商品字段名。输出类型必须兼容；最终附件必须来自 process 的输出，不能把顾客上传的原材料当作已经制作的交付。

input、process 的 next 可以是目标节点代码，或 `{cases:[{when:{source,op,value?},to}],default}`。只支持 eq、in、exists，最多 32 个条件；按顺序取第一个命中条件，不执行表达式、脚本或模板。敏感字段不能用于条件或展示。

## 一个队列处理示例

下面是商品 `task_flow` 字段的值。商品处理方式为 manual，最终 outputs 含必填 textarea `content`；示例只交付文字，不假设 AI 已制作真实文档或调用外部服务。

```json
{
  "version": 1,
  "entry": "requirements",
  "nodes": [
    {
      "id": "requirements",
      "kind": "input",
      "label": {"zh-CN": "提交需求"},
      "prompt": {"zh-CN": "准备好需求后，点击开始。"},
      "question": {"zh-CN": "请写清本次任务的主题、范围与交付要求。"},
      "fields": [{"key": "brief", "label": {"zh-CN": "需求说明"}, "type": "textarea", "required": true}],
      "start_policy": "confirm",
      "timeout_seconds": 600,
      "timeout_next": "expired",
      "next": "create"
    },
    {
      "id": "create",
      "kind": "process",
      "label": {"zh-CN": "处理本次需求"},
      "inputs": {"brief": {"node": "requirements", "field": "brief"}},
      "outputs": [{"key": "content", "label": {"zh-CN": "制作结果"}, "type": "textarea", "required": true}],
      "timeout_seconds": 3600,
      "timeout_next": "expired",
      "failure_next": "failed",
      "next": "review"
    },
    {
      "id": "review",
      "kind": "display",
      "content": {"zh-CN": "查看本次结果，确认后进入最终领取。"},
      "show_from": {"result": {"node": "create", "field": "content"}},
      "next": "finished"
    },
    {
      "id": "finished",
      "kind": "end",
      "state": "succeeded",
      "result": {"content": {"node": "create", "field": "content"}}
    },
    {
      "id": "expired",
      "kind": "end",
      "state": "failed",
      "message": {"zh-CN": "本次输入已超时，请检查后重试。"},
      "retryable": true
    },
    {
      "id": "failed",
      "kind": "end",
      "state": "failed",
      "message": {"zh-CN": "本次处理失败，请联系商家核实。"},
      "needs_review": true
    }
  ]
}
```

输入和展示节点的超时按 timeout_next 转移。**处理节点超时会停止流程，整单失败并等待商家核实，不自动跑配置的下一次处理动作。** 超时无法撤回外部 Worker 已经开始的支付或发货；配置 timeout_next 不构成安全的自动再执行保证。明确失败并报告 `retryable:true` 才可走 failure_next；无法确认外部动作未发生的失败会直接终止，等待商家核实。真实副作用仍需执行端用稳定业务幂等键去重。

## 顾客动作与安全视图

状态中的 `task_flow` 含 enabled、version、definition_hash、attempt、flow_epoch、revision、phase、current、deadline、server_time、shown、history 和 actions。current 只投影当前允许看到的节点；history 只有节点、epoch、时间和状态元数据，不能用它读取历史输入或处理结果。shown 只含定义中明确允许展示的非敏感文字、选项和是／否值，不返回附件引用。最终附件仍通过成功后的显式领取和授权下载取得。

预览通常是 epoch=0、revision=0；准备任务后取得真实值。每次操作先读状态，按 actions 选择 start / answer / continue，并提交准确 flow_epoch 和 expected_revision；answer 另带字符串对象 values。冲突或超时返回 409，应重新读取，不能猜测新编号重发。第一次 start 允许从零值预览准备并明确开始该卡。

HTTP 请求均为同源 POST JSON，领取 token 放请求体；批量链接必须传目标 card_id。CLI 的真实用法见[顾客 CLI](cli-customer.md)，浏览器原生工具见[WebMCP](webmcp.md)。取消、重试或重启会使旧尝试／epoch 失效，不能把旧答案或旧结果覆盖到新节点。

处理者在商品队列领取时取得当前节点需求、输入输出定义、attempt、flow_epoch、action_id。进度、附件和结果必须带这个执行身份；成功完成处理节点只推进图，成功 end 才按商品查看与交付规则形成成品；取消、拒绝、处理超时或不可安全重试的失败也可直接终止。原子领取和断线恢复见[AI 队列处理](automation-cli.md)。

## 短期敏感输入

只在流程 input 的 text 字段配置 `sensitive:true`，`sensitive_ttl_seconds` 默认 120 秒，可设 1–600。普通商品参数、输出、图片和其他类型不能标为短期敏感。值加密存储、受店铺和全站容量限制；只允许明确映射给 process，不能放在 show_from、分支条件或最终 result。

有效期从收到答案起计算，并受当前输入截止时间限制；值到期、对应处理完成、重试、取消或销毁后清理敏感载荷，派发重试前重新检查时限。排队中值到期会回到原输入的 await_start，以新 epoch 等顾客重新填写，不增加整单 attempt；已经开始外部处理时，清理值不会延长处理截止时间。普通 job_view、历史、事件和导出不回读这个值。它不是由商品介绍获取账号权限的渠道，也不应进入常规日志。

已把需求或验证码交给获授权的外部 Worker，就不能撤回对方已经收到的副本。只交给可信执行者；v2 范围签名限制可读取的节点，不保证第三方程序不会泄漏被授权读取的数据。外部动作的幂等与结果核实仍由执行端完成。

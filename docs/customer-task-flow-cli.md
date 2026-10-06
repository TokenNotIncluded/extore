# 顾客通过 CLI 完成多步兑换

普通兑换仍用 `customer redeem`。商品启用任务编排后，CLI 可以开始、回答、查看中间结果、确认下一步与重试。浏览器与 CLI 使用同一个领取凭证、同一个服务器期限。

## 验码与保存领取凭证

```sh
extore customer exchange --origin https://extore.example --codes-stdin
```

从标准输入粘贴卡密，以文件结尾结束输入。卡密不放在命令参数中，也不写入客户配置；CLI 将领取凭证保存在权限为 `600` 的本地配置中。返回的 `receipt_id` 是本地编号，可以用作后续命令参数。一个领取链接对应多张卡密时，命令必须用 `--card CARD_ID` 选择一张；每张卡密独立计时。

## 开始与回答

```sh
extore customer flow view RECEIPT_ID
extore customer flow start RECEIPT_ID
extore customer flow answer RECEIPT_ID --values-file ./step-values.json
```

`view` 只返回当前步骤、可执行动作、期限与明确允许显示的前一步结果，不列出整个任务历史。`start` 是明确的开始动作，验码或查看状态不会提前计时。回答文件是当前 `fields` 定义对应的 JSON 对象；也可用 `--values-stdin` 从标准输入读取。

支持文本、选择、是／否、数字与附件。选择提交固定选项值，布尔值可写 JSON `true` / `false` 或字符串 `"true"` / `"false"`，数字可写 JSON 数字或十进制文本。CLI 转成服务器使用的字符串结构；未知字段、缺失必填值与不符合类型的值在上传前被拒绝。

```sh
extore customer flow answer RECEIPT_ID --values-file ./step-values.json \
  --file brief=./brief.docx \
  --file references=./one.png --file references=./two.png
```

`file` / `image` 字段只接收一个路径。`images` 字段允许重复 `--file FIELD=PATH`，每个文件分别上传，ID 按所选顺序组成集合。上传限定当前卡密、节点与步骤轮次，并遵循服务器文件和容量限制。所有本地文件先检查，输入内容不保存到客户配置或命令结果。

## 看答案、继续与重试

```sh
extore customer flow view RECEIPT_ID --output ./current-step.json
extore customer flow continue RECEIPT_ID
extore customer flow restart RECEIPT_ID
extore customer flow cancel RECEIPT_ID --confirm
```

`view` 默认明确显示当前允许看到的中间结果；加 `--output` 可保存为新的 `600` 文件，终端仅输出保存位置。已存在的文件不会被覆盖。客户配置中的状态摘要不保存这些结果值。

`continue` 仅用于等待确认的展示步骤。后续输入步骤是否自动计时由商品定义；刷新、查看状态或更新处理进度不会延长时间。失败后，只有服务器允许重试的任务才能 `restart`；它增加本次尝试次数，回到等待开始，仍需顾客再次点击或运行 `start` 才开始计时。

自动化可添加 `--flow-epoch N --expected-revision N`，要求操作仍针对之前读取的步骤。如果期间超时、跳转或被重试，CLI 会拒绝这个旧操作。即使不显式指定，CLI 也先读取当前状态并在请求中带上轮次和修订号，由服务器再次核对。

`cancel --confirm` 取消本次任务并清除本次资料。若已经开始处理，外部动作可能已经发生，取消不能撤销它们；该状态需要商家核实，不会自动重试。

最终交付继续用现有 `customer reveal` / `download` / `destroy` 命令；中间回答不会把整个兑换标为完成。

## 签名代理卡密

`EXR1` 卡密携带公开路由标识与签名。CLI 只向入口服务器读取不含卡密的公开路由列表，在本地验证签名与固定目标地址，再将完整卡密发送给该目标 Extore 验证。入口服务器不会收到完整代理卡密、秘密部分或秘密摘要；未知路由、变更目标或无效签名不会回退到把卡密发给入口服务器。

普通旧卡密没有来源信息，不能猜测所属网站。混合多个已验证目标时，CLI 按目标创建独立领取记录；所有路由在首次提交之前完成校验。返回结果逐个列出领取编号或失败目标，不把不同网站的领取凭证混在一起。CLI 不跟随 HTTP 重定向，也不会向客户操作带入商家会话或授权。

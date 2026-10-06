# 顾客兑换 CLI

[CLI 总览](cli.md) · [店主 CLI](cli-owner.md) · [AI 接入提示词](ai-prompts.md) · [接口协议](protocol.md#顾客接口)

`extore customer` 使用卡密或已有领取链接完成兑换，不需要商家管理权限。普通命令要求 0.6.0 及以上，跨商品批量兑换、多步流程、富类型字段和签名路由要求 0.8.0 及以上，安装方式见 [CLI 总览](cli.md#安装与登录)。大写 ID 和路径是占位符。

## 验码与准备参数

```sh
extore customer products --origin https://extore.example.com
extore customer exchange --origin https://extore.example.com --codes-stdin < /path/to/private-codes.txt
extore customer schema --receipt RECEIPT_ID
```

卡密只从标准输入读取；最多 30 张、8000 字符，不作为命令参数或打印到输出。多张卡密默认逐卡验码，同一目标服务器的不同商品可以放在一起；坏码和重复项单独报告，有效卡继续。`exchange` 为有效卡返回本地 `receipt_id`，形如 `rcpt_…`，真正的领取凭证保存到私密配置；全部无效时不创建领取记录。验码不等于提交任务。

`--batch` 让单张卡也使用逐卡结果；`--atomic` 则显式使用旧版同商品批量接口，任一无效卡会阻止该目标整组验码。这两个选项互斥，新批量流程通常不需要指定。

```sh
extore customer exchange --origin https://extore.example.com --codes-stdin --batch < /path/to/private-code.txt
extore customer exchange --origin https://extore.example.com --codes-stdin --atomic < /path/to/private-same-product-codes.txt
```

签名代理卡密 `EXR1.…` 可以指向另一个 Extore。CLI 只向指定入口读取不含卡密的公开路由资料，在本地验签并核对固定目标，再将完整已验证卡密发送给该目标。未知路由、无效签名或不支持的目标会停止，不回退到把代理卡密交给入口。普通卡密只发给指定 `--origin`，不扫描或尝试其他站点。

混合多个已验证目标时，客户端先校验所有路由，再按目标创建独立的本地领取记录；返回 `receipts` 与 `failed`，成功目标不会因另一目标失败而被重复兑换。每个领取凭证仍固定在自己的服务器。CLI 不跟随 HTTP 重定向，不携带商家 Cookie 或授权；路由的配置见[兑换路由](proxy-config.md)。

公开商品可在验码前用 `schema --product PRODUCT_ID --origin SERVER` 查询；私有商品须先验码，再按 receipt 查询。规格由卡密决定，不能在填参时换规格。`schema` 默认显示字段代码、名称、类型和必填规则，`--detail` 按需展开说明与 Markdown 教程。

已有浏览器领取链接可以私密导入：

```sh
extore customer import-receipt
# 或从私密文件读取完整 /receipt#… 链接
extore customer import-receipt --link-stdin < /path/to/private-receipt.txt
```

交互输入隐藏内容，普通输出不显示 token。导入先使用 `/api/batch/receipt`，仅在该接口返回 HTTP 404 时回退旧 `/api/receipt`；网络故障或其他错误不会触发回退。`extore customer receipts` 只列本地已保存摘要，不发网络请求。

## 提交与跟踪

先按 `schema` 的字段代码准备 `params.json`，值均为字符串：

```json
{"account_email":"customer@example.com","note":"需要处理的事项"}
```

```sh
extore customer redeem RECEIPT_ID --params-file params.json
extore customer receipt RECEIPT_ID
```

也可用 `--params-stdin` 从标准输入读取 JSON。`receipt` 与 `status` 是同一命令，默认只返回商品、规格、任务状态、进度、步骤与队列排位，不输出顾客参数或交付内容。`queued` / `processing` 表示尚未交付。

| 状态 | 顾客可做的事 |
| --- | --- |
| 尚无任务 | 填写参数并 `redeem` |
| `waiting` | 流程等待顾客开始、输入或展示确认；先 `flow view`，再按允许动作操作 |
| `queued` / `processing` | 查看状态与排位，等待处理 |
| `needs_input` | 阅读原因与 `retry_mode`：`revise` 修改后重提，`reuse` 使用原资料重试 |
| `failed` | 只有 `can_retry=true` 时可按商品规则重试，否则联系商家 |
| `rejected` | 查看拒绝原因；卡密已禁用，不能重提或领取 |
| `succeeded` | 按查看规则领取、下载或销毁 |
| `destroyed` | 内容已经关闭，不能恢复 |

要求重试不受发货失败的 `allow_retry` / `max_attempts` 限制，但卡密过期、撤销或店铺停用后仍不能重提。状态中的 `retry_reason_type` 区分 `customer_input`（顾客资料）、`external`（外部服务）、`processor`（处理程序），不代表所有问题都需要顾客改资料。

`retry_mode=revise` 时按要求修改资料。需要取回原参数时显式使用 `--inputs`：

```sh
extore customer receipt RECEIPT_ID --inputs
extore customer retry RECEIPT_ID --params-file corrected.json
```

`retry_mode=reuse` 且 `can_retry=true` 时，原参数和输入附件保持不变：

```sh
extore customer retry RECEIPT_ID --reuse
# 批量 receipt 每次明确选择一张卡
extore customer retry RECEIPT_ID --reuse --card CARD_ID
```

`--reuse` 不需要参数文件，也不能与 `--params-file`、`--params-stdin`、`--items-file`、`--group`、`--file` 或 `--card-file` 混用。它只适用于处理者明确允许原资料重试的 `needs_input` 任务；不能代替修改后重提，也不能绕过拒绝、过期或撤销。无需重新选文件或取出私密原参数。

两种新尝试都保留任务 ID、输入输出定义、规格和计划，`attempt` 增加，完成步骤与进度归零。卡密仍归原顾客任务，不会生成新的销售库存。

## 多步兑换：开始、回答与确认

商品启用任务流程时，验码、导入领取链接和查看状态都不会自动开始任务或启动首次计时。批量 `redeem` 只以空参数 `{}` 准备流程卡，不能替代流程动作。先读取当前题目与允许的操作，再明确开始：

```sh
extore customer flow view RECEIPT_ID
extore customer flow start RECEIPT_ID
extore customer flow answer RECEIPT_ID --values-file ./step-values.json
extore customer flow continue RECEIPT_ID
```

批量领取记录每条流程命令都加 `--card CARD_ID`，只操作所选卡密，其他卡不会一起开始。`view` 只显示当前步骤、明确允许展示的前一步结果、动作和期限；不是整个流程图或未来题目的导出。`--detail` 展开当前字段教程，`--output NEWFILE` 将可见结果写入新的 0600 文件，终端只报告保存位置。

`answer` 使用当前 `fields` 的代码，不使用商品后来修改的表单。`--values-file` 与 `--values-stdin` 互斥，JSON 对象可以包含文本、选择值、布尔值或数字；客户端按当前类型转成服务器使用的字符串。未知字段或缺少必填资料在上传前拒绝。敏感内容使用私密文件或 stdin，不放在命令参数、日志或聊天。

```sh
extore customer flow answer RECEIPT_ID --card CARD_ID --values-file ./step-values.json --file brief=./brief.docx
extore customer flow answer RECEIPT_ID --values-file ./step-values.json --file references=./one.png --file references=./two.png
```

`file` / `image` 字段各接收一个文件；`images` 允许重复同一字段，按上传顺序组成集合，受 `max_items` 和服务器容量限制。上传绑定当前卡密、当前节点与当前 `flow_epoch`，上传本身不提交答案或推进步骤。

写动作都可以带 `--flow-epoch FLOW_EPOCH --expected-revision REVISION`，要求仍针对之前读取的步骤；省略时 CLI 先读取当前状态，再携带轮次和修订号提交，由服务器核对。遇到旧步骤冲突、超时或重试，先 `flow view`，不能机械地重放旧答案。`continue` 只在当前 `actions` 允许时确认展示结果；后续步骤的进入与计时服从商品定义，查看或刷新不会延长时间。

```sh
extore customer flow restart RECEIPT_ID --card CARD_ID
extore customer flow cancel RECEIPT_ID --card CARD_ID --confirm
```

只有服务端允许重试的结束任务才能 `restart`，会清理本次客户端输入缓存并回到等待开始；仍需顾客明确 `start`，不会自动开新一轮计时。`cancel --confirm` 清除本次资料，但不能撤回已发生的外部动作，已执行的动作需要商家核实。最终交付仍通过 `reveal`、`download` 或 `destroy`，中间答案不是整单完成。

## 上传材料

```sh
extore customer upload RECEIPT_ID --field material --file ./material.pdf
```

`upload` **只上传文件**并返回文件 ID，不提交任务，也不推进处理进度。把 ID 写入对应文件参数，再执行 `redeem` 或 `retry`。

也可以在提交参数时上传指定文件并自动填入返回 ID：

```sh
extore customer redeem RECEIPT_ID --params-file params.json --file material=./material.pdf
```

`--file FIELD=PATH` 可重复；目标必须是当前卡密输入快照的 `file`、`image` 或 `images` 字段。前两种字段只提供一个文件，只有 `images` 可以重复同一字段。图片集合只接受真实上传的文件 ID，不接受远程图片 URL；图片格式为 PNG、JPEG 或 WebP。CLI 读取服务器上传限制；服务端默认单文件 20 MiB，还检查单卡、单店、全站容量和并发。提交成功前，上传材料属于草稿，保留期见[存储说明](getting-started.md#文件上传与存储)。原资料重试不能追加或替换文件；需要修改材料的任务应使用 `revise`。

## 批量卡密

同一目标服务器的多张有效卡密共享一个本地 receipt，可以属于不同商品。每项有独立 `card_id`、规格、输入快照和任务。先查看批量状态，再选择对应卡密：

```sh
extore customer receipt RECEIPT_ID
extore customer schema --receipt RECEIPT_ID --card CARD_ID
extore customer redeem RECEIPT_ID --card CARD_ID --params-file params.json
extore customer upload RECEIPT_ID --card CARD_ID --field material --file ./material.pdf
```

针对批量结构 receipt 的单卡表单、上传、领取、下载和销毁必须传 `--card`，包括使用 `--batch` 验码的一张卡；只有单卡结构的 receipt 不接受该参数。

`schema --receipt RECEIPT_ID` 不选卡时返回 `groups`。只有同商品、完整冻结字段、已保存非附件内容和流程定义均兼容的卡才会合组；不同规格可以共用兼容表单，不同商品分开。每组包含 `group_id`、`card_ids`、可共享的 `shared_parameters`、必须逐卡处理的 `separate_files` 与规格清单。组可能随状态变化，使用返回的当前 ID；过期组需要重新读 `schema`。

```sh
extore customer schema --receipt RECEIPT_ID
extore customer schema --receipt RECEIPT_ID --group GROUP_ID --detail
extore customer redeem RECEIPT_ID --group GROUP_ID --params-file common.json \
  --card-file CARD_ID_1:material=./first.pdf \
  --card-file CARD_ID_2:material=./second.pdf
```

`--group` 配合 `--params-file` 或 `--params-stdin` 将同一份文本参数展开到该组各卡。文件、图片和图片集合不能放进共享参数；用可重复的 `--card-file CARD:FIELD=PATH` 给每张卡指定自己的文件。即便内容相同，也须分别上传到各卡，不能共享文件 ID。单卡操作继续用 `--card` 和 `--file FIELD=PATH`，不能与 `--group` 或 `--card-file` 混用。

各卡参数不同时，也可用一个 JSON 数组提交多项：

```json
[
  {"card_id":"CARD_ID_1","params":{"account_email":"first@example.com"}},
  {"card_id":"CARD_ID_2","params":{"account_email":"second@example.com"}}
]
```

```sh
extore customer redeem RECEIPT_ID --items-file items.json
extore customer retry RECEIPT_ID --items-file corrected-items.json
extore customer redeem RECEIPT_ID --items-file items.json --card-file CARD_ID_1:material=./first.pdf
```

`--items-file` 只适用于批量 receipt，不能与 `--card`、`--group` 或 `--file` 混用，可以附带 `--card-file`。数组每次 1–30 项，每项只能包含 `card_id` 和值为字符串的 `params`；也可先逐卡 `upload`，再把各自的文件 ID 写入参数。文件路径、字段类型与上传限制在上传前检查，指向其他卡的 `--card-file` 会拒绝整条命令。

默认逐卡批量接口 `/api/batch/exchange`、`/api/batch/receipt`、`/api/batch/redeem` 在网页和 CLI 均可使用。`items`、`summary` 是验码与领取摘要，提交另有 `results` 和 `submission_summary`。单卡的参数、归属或任务状态错误按项报告，其他合法项继续；成功项无需重提。无法确认提交结果时先刷新 `receipt`，不能直接重复处理。

成功上传的 ID 立即保存到私密配置的对应卡和字段，提交失败后仍保留。下次组或 items 提交没有显式提供该文件字段时，只填入这张卡自己的缓存，不再次占用上传容量；显式换文件则重新上传。不要为已成功项重传材料。非法 JSON、错误的 items 结构、共享附件参数等命令格式错误仍会阻止整条命令，逐卡隔离不意味着接受损坏的请求。

流程组的共享参数必须是 `{}`，准备后每张卡都需顾客明确执行 `flow start --card CARD_ID`，不会一起计时或提交未来字段。通过 `--atomic` 保存的旧领取记录，普通 `--items-file` 沿用旧接口的整组提交规则；默认新记录按项处理。多个签名路由目标分别保存领取记录，与一个目标内的逐卡处理是两层隔离。

## 领取、下载与销毁

```sh
extore customer reveal RECEIPT_ID --output ./delivery.json
extore customer files RECEIPT_ID
extore customer download RECEIPT_ID --file-id FILE_ID --output ./delivery.pdf
extore customer destroy RECEIPT_ID --confirm
```

`reveal` 只把实际交付写入新 0600 JSON 文件，标准输出显示保存路径与文件元数据。下载同样写入新 0600 文件，不能覆盖已有目标。批量 receipt 在每条命令上加 `--card CARD_ID`，只操作那张卡。

`files` 显示这个客户端缓存的上传、已领取文件 ID，带 `cached:true`；它不是服务端全部附件清单。先 `reveal` 才能获得并缓存输出文件 ID，导入链接本身不会自动领取或恢复已消耗的内容。

一次领取商品会消耗查看机会，每个一次下载文件也有自己的额度；请保存交付 JSON 和文件。文件 ID 本身没有下载权限，仍由本地 receipt 凭证授权。销毁须显式 `--confirm`，会关闭本站内容及附件，不能撤回已保存的副本或外部资源。

## 命令与配置

| 命令 | 主要选项 |
| --- | --- |
| `products` | `--origin`、`--detail` |
| `schema` | `--receipt` 或 `--product`；后者需 `--origin`；`--card` 或 `--group`、`--detail`；批量不选卡时返回分组 |
| `exchange` | `--origin`、必需的 `--codes-stdin`；多卡默认逐卡结果，`--batch` / `--atomic` 互斥 |
| `import-receipt` | 隐藏交互输入，或 `--link-stdin` |
| `receipts` | 本地摘要，无网络请求 |
| `receipt / status RECEIPT_ID` | `--card`、显式 `--inputs` |
| `redeem RECEIPT_ID` | 三选一：`--params-file` / `--params-stdin` / `--items-file`；单卡 `--card` 与可重复 `--file FIELD=PATH`，共享组 `--group`，组或 items 可重复 `--card-file CARD:FIELD=PATH` |
| `retry RECEIPT_ID` | 修改后重提沿用 redeem 参数；原资料重试用互斥的 `--reuse`，批量必须明确 `--card` |
| `flow view RECEIPT_ID` | `--card`、`--detail`、新私密文件 `--output`；只读当前可见步骤 |
| `flow start / continue / restart RECEIPT_ID` | `--card`、`--flow-epoch`、`--expected-revision`；显式状态动作 |
| `flow answer RECEIPT_ID` | `--values-file` 或 `--values-stdin`、可重复 `--file FIELD=PATH`、`--card`、轮次与修订号 |
| `flow cancel RECEIPT_ID` | 必需的 `--confirm`、`--card`、轮次与修订号 |
| `upload RECEIPT_ID` | `--field`、`--file`、批量时 `--card` |
| `reveal RECEIPT_ID` | 新文件 `--output`、批量时 `--card` |
| `files RECEIPT_ID` | 批量时 `--card` |
| `download RECEIPT_ID` | `--file-id`、新文件 `--output`、批量时 `--card` |
| `destroy RECEIPT_ID` | `--confirm`、批量时 `--card` |

默认配置 `${XDG_CONFIG_HOME:-~/.config}/extore/customer.json`，可用 `EXTORE_CUSTOMER_CONFIG` 或 `extore customer --profile PATH …` 替换；`--profile` 放在 customer 之后、子命令之前。目录 0700、文件 0600，与 manage/admin 配置分开。所有请求只连接 receipt 指定的同源服务器，不携带商家 Cookie 或 Bearer。

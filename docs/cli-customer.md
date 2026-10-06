# 顾客兑换 CLI

[CLI 总览](cli.md) · [店主 CLI](cli-owner.md) · [AI 接入提示词](ai-prompts.md) · [接口协议](protocol.md#顾客接口)

`extore customer` 使用卡密或已有领取链接完成兑换，不需要商家管理权限。以下命令要求 0.6.0 及以上，安装方式见 [CLI 总览](cli.md#安装与登录)。大写 ID 和路径是占位符。

## 验码与准备参数

```sh
extore customer products --origin https://extore.example.com
extore customer exchange --origin https://extore.example.com --codes-stdin < /path/to/private-codes.txt
extore customer schema --receipt RECEIPT_ID
```

卡密只从标准输入读取；最多 30 张同商品卡密、8000 字符，不作为命令参数或打印到输出。`exchange` 返回本地 `receipt_id`，形如 `rcpt_…`，真正的领取凭证保存到私密配置。验码不等于提交任务。

公开商品可在验码前用 `schema --product PRODUCT_ID --origin SERVER` 查询；私有商品须先验码，再按 receipt 查询。规格由卡密决定，不能在填参时换规格。`schema` 默认显示字段代码、名称、类型和必填规则，`--detail` 按需展开说明与 Markdown 教程。

已有浏览器领取链接可以私密导入：

```sh
extore customer import-receipt
# 或从私密文件读取完整 /receipt#… 链接
extore customer import-receipt --link-stdin < /path/to/private-receipt.txt
```

交互输入隐藏内容，普通输出不显示 token。`extore customer receipts` 只列本地已保存摘要，不发网络请求。

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

`--reuse` 不需要参数文件，也不能与 `--params-file`、`--params-stdin`、`--items-file` 或 `--file` 混用。它只适用于处理者明确允许原资料重试的 `needs_input` 任务；不能代替修改后重提，也不能绕过拒绝、过期或撤销。无需重新选文件或取出私密原参数。

两种新尝试都保留任务 ID、输入输出定义、规格和计划，`attempt` 增加，完成步骤与进度归零。卡密仍归原顾客任务，不会生成新的销售库存。

## 上传材料

```sh
extore customer upload RECEIPT_ID --field material --file ./material.pdf
```

`upload` **只上传文件**并返回文件 ID，不提交任务，也不推进处理进度。把 ID 写入对应文件参数，再执行 `redeem` 或 `retry`。

也可以在提交参数时上传指定文件并自动填入返回 ID：

```sh
extore customer redeem RECEIPT_ID --params-file params.json --file material=./material.pdf
```

`--file FIELD=PATH` 可重复，但每个文件字段只提供一次。目标必须是当前卡密输入快照的 `file` 字段。CLI 读取服务器上传限制；服务端默认单文件 20 MiB，还检查单卡、单店、全站容量和并发。提交成功前，上传材料属于草稿，保留期见[存储说明](getting-started.md#文件上传与存储)。原资料重试不能追加或替换文件；需要修改材料的任务应使用 `revise`。

## 批量卡密

同商品多张卡密共享一个本地 receipt，每项有独立 `card_id`、规格、输入快照和任务。先查看批量状态，再选择对应卡密：

```sh
extore customer receipt RECEIPT_ID
extore customer schema --receipt RECEIPT_ID --card CARD_ID
extore customer redeem RECEIPT_ID --card CARD_ID --params-file params.json
extore customer upload RECEIPT_ID --card CARD_ID --field material --file ./material.pdf
```

针对批量 receipt 的单卡表单、上传、领取、下载和销毁必须传 `--card`；单卡 receipt 禁止附带该参数。也可用一个 JSON 数组提交多项：

```json
[
  {"card_id":"CARD_ID_1","params":{"account_email":"first@example.com"}},
  {"card_id":"CARD_ID_2","params":{"account_email":"second@example.com"}}
]
```

```sh
extore customer redeem RECEIPT_ID --items-file items.json
extore customer retry RECEIPT_ID --items-file corrected-items.json
```

`--items-file` 只适用于批量 receipt，不能与 `--card` 或 `--file` 混用；每项参数按自己的快照验证。文件材料先逐卡上传，再把对应 ID 写入 items。提交的卡密必须属于当前 receipt，ID 不得重复，每次最多 30 项；服务端整次提交成功或回滚。

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
| `schema` | `--receipt` 或 `--product`；后者需 `--origin`；`--card`、`--detail` |
| `exchange` | `--origin`、必需的 `--codes-stdin` |
| `import-receipt` | 隐藏交互输入，或 `--link-stdin` |
| `receipts` | 本地摘要，无网络请求 |
| `receipt / status RECEIPT_ID` | `--card`、显式 `--inputs` |
| `redeem RECEIPT_ID` | 三选一：`--params-file` / `--params-stdin` / `--items-file`；单卡可重复 `--file FIELD=PATH` |
| `retry RECEIPT_ID` | 修改后重提沿用 redeem 参数；原资料重试用互斥的 `--reuse`，批量必须明确 `--card` |
| `upload RECEIPT_ID` | `--field`、`--file`、批量时 `--card` |
| `reveal RECEIPT_ID` | 新文件 `--output`、批量时 `--card` |
| `files RECEIPT_ID` | 批量时 `--card` |
| `download RECEIPT_ID` | `--file-id`、新文件 `--output`、批量时 `--card` |
| `destroy RECEIPT_ID` | `--confirm`、批量时 `--card` |

默认配置 `${XDG_CONFIG_HOME:-~/.config}/extore/customer.json`，可用 `EXTORE_CUSTOMER_CONFIG` 或 `extore customer --profile PATH …` 替换；`--profile` 放在 customer 之后、子命令之前。目录 0700、文件 0600，与 manage/admin 配置分开。所有请求只连接 receipt 指定的同源服务器，不携带商家 Cookie 或 Bearer。

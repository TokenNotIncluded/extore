# 远程商品管理 CLI

[返回项目首页](../README.md) · [运行指南](getting-started.md) · [接口协议](protocol.md) · [WebMCP](webmcp.md)

`extore manage` 让处理人员或机器人通过商品管理授权操作远程队列。一个授权只管理一个商品；客户端可以保存多个授权并聚合查看，每次写入仍使用其中一个授权，不合并权限。

## 安装与登录

需要 Linux、Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)：

```sh
uv tool install --upgrade 'extore>=0.5.0'
extore manage --help
extore manage login
```

最后一条命令会隐藏输入，粘贴完整管理链接即可。需要自动接入时，把链接从标准输入交给客户端：

```sh
extore manage login --link-stdin < /path/to/private-link.txt
```

支持完整的 `/staff#…` 商品管理链接或 `/cli#…` 专用票据链接；不接受裸 token，也不通过命令行参数传递链接。生产服务器必须使用 HTTPS，本机回环地址可用 HTTP。`--client-name` 可设置后台审计中显示的设备名称。

登录时客户端生成 Ed25519 设备密钥并证明持有私钥，服务器绑定设备和该商品授权。默认每条管理链接允许 1 次浏览器登录及 1 个 CLI 设备绑定，两种额度独立；已有 CLI 设备续签不再次消耗额度。

CLI Bearer 会话有效 8 小时，到期前或失效后客户端通过设备签名自动续签。能否续签仍取决于设备、管理链接及全部祖先是否有效；设备或授权撤销、授权过期后不能继续使用。

## 复制机器人接入提示词

管理链接界面可生成机器人接入提示词，包含操作范围、安装与登录方式，以及 **5 分钟有效、仅用于 CLI 首次绑定**的票据。把提示词交给需要处理该商品的机器人即可；票据不能用于浏览器登录。

票据完成设备绑定后不需要反复生成，后续使用该设备的签名续签。复制提示词不会自动启动常驻机器人；接入方决定何时读取队列、如何处理并提交结果。

## 查看多个商品

重复登录不同商品的授权，客户端会保存在同一个配置中：

```sh
extore manage products
extore manage queues --all
extore manage jobs --product PRODUCT_ID
extore manage job JOB_ID --product PRODUCT_ID
```

`queues --all` 按各授权分别请求商品队列，在客户端组合结果；它不是服务端的全商品管理权限。队列列表默认 `--view active`，以服务端 compact 视图只带处理摘要及附件数量/大小；完整顾客参数、输入输出快照和文件 ID 通过 `job` 或 `files` 单独读取。

`--view processed` 查看已完成、已销毁及已拒绝任务，`--view all` 查看全部记录。`--state` 精确筛选状态，`--limit` 为 1–500。退回补充的 `needs_input` 在 active 中，等待顾客重提，不应再次领取。

`--origin https://example.com` 选择服务器；`--grant DEVICE_ID` 选择某个设备授权。对单商品操作必须传 `--product`。如果同一商品有多个可用授权而无法确定使用哪一个，应明确传入 `--origin` 或 `--grant`；权限不会取多个授权的并集。

## 处理一个任务

先读取任务自己的快照，再领取和交付：

```sh
extore manage job JOB_ID --product PRODUCT_ID
extore manage claim JOB_ID --product PRODUCT_ID
extore manage progress JOB_ID --product PRODUCT_ID --progress 30 --message "正在核实资料"
extore manage complete JOB_ID --product PRODUCT_ID --output-file result.json --message "已完成"
```

`result.json` 为任务输出字段代码到字符串值的对象，例如：

```json
{"resource_url":"https://example.com/resource","note":"请按商品说明领取。"}
```

字段必须符合目标任务的 `outputs` 快照，不能照搬商品后来修改的表单。服务商品完成时省略输出；默认单个 `content` 文本任务也可用 `--content-file` 读取 UTF-8 文件。`succeed` 是 `complete` 的别名。

有步骤计划时，用可重复的 `--completed-step STEP_ID` 传入完整已完成集合，进度由服务端计算，不能撤回当前尝试已经完成的步骤。`claim` 和 `progress` 可用 `--steps-file steps.json` 为尚无计划的任务绑定一次 1–30 项步骤计划，格式为 `[{"id":"verify","label":{"zh-CN":"核实资料"}}]`，已有计划不能覆盖。`claim` 可接受多个同商品任务 ID；其他操作逐任务执行，避免把一位顾客的结果交给另一位顾客。

### 退回补充、拒绝与失败

```sh
extore manage request-changes JOB_ID --product PRODUCT_ID --reason "请补充账户邮箱截图。"
extore manage reject JOB_ID --product PRODUCT_ID --reason "提供的账户不符合商品条件。"
extore manage fail JOB_ID --product PRODUCT_ID --message "确认未交付" --retryable
extore manage retry JOB_ID --product PRODUCT_ID
```

- `request-changes`：任务变为 `needs_input`，顾客可修改资料后重新提交；沿用原任务和快照，`attempt` 增加。不受发货失败的重试开关或次数限制，过期、撤销仍阻止重提。
- `reject`：任务和卡密进入拒绝终态，顾客在原领取链接看到原因，不能重新提交或领取内容。
- `fail`：真正的处理失败；只有确认未交付时才传 `--retryable`，顾客重试还须符合商品规则。
- `retry`：具有 `queue.retry` 的管理者核实后放行失败重试，实际新尝试由顾客提交。

前两种审核必须提供非空白、最多 1000 字符的原因；这段文字会显示给顾客。处理、审核和交付都限于自己已领取的 `processing` 队列任务，不能覆盖 Webhook 或官方处理器的自动任务。

## 附件

```sh
extore manage files JOB_ID --product PRODUCT_ID
extore manage download JOB_ID --product PRODUCT_ID --file-id FILE_ID --output ./material.pdf
extore manage upload JOB_ID --product PRODUCT_ID --field deliverable --file ./result.pdf
```

上传返回文件 ID，把它填入 `complete --output-file` 使用的对应输出字段。文件上传是交付材料，不是安装处理器代码。下载要求新的目标文件，客户端不覆盖已有文件；下载文件使用私有权限保存。服务端的单文件、单卡密和全站存储限制仍适用，默认值见[运行指南](getting-started.md#文件上传与存储)。

## 配置、输出与撤销

默认配置为 `${XDG_CONFIG_HOME:-~/.config}/extore/cli.json`，也可用 `EXTORE_CLI_CONFIG` 或 `extore manage --profile PATH …` 指定。配置目录要求当前用户所有、权限 0700，配置与锁文件要求 0600；其中保存设备私钥和当前会话，普通命令输出不返回这些凭证。

命令输出 JSON。成功用 `ok:true`，失败用 `ok:false` 与 `error`、`code`，HTTP 错误另带 `status`；失败退出码非零。读取列表不代表发货成功，任务成功以服务端状态为准。

```sh
extore manage logout --product PRODUCT_ID
extore manage logout --all
```

`logout` 结束所选当前 CLI 会话并删除本地设备密钥记录，服务器仍保留设备审计记录。需要彻底阻止设备以后续签时，在后台撤销设备；撤销管理链接同时停止其设备、会话及下级授权。会话界面区分 browser / cli 渠道，并显示绑定设备信息。

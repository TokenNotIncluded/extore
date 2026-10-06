# 多店与账号

[项目首页](../README.md) · [运行指南](getting-started.md) · [店主 CLI](cli-owner.md) · [Python SDK](python-sdk.md)

Extore 一个实例可以服务多个独立店铺。商品、卡密、队列、附件、管理链接、配置档案、会话与审计均按店铺校验权限；支付下单仍由上游平台负责。已有单店数据迁移到默认店铺，不会重新生成卡密或任务。

## 平台管理员和店主

| 身份 | 可管理的范围 | 登录方式 |
| --- | --- | --- |
| 平台管理员（root） | 店铺创建、启停、存储额度、注册开关、SMTP；跨店管理需明确店铺或商品 | 首次初始化密码只用于注册 Passkey，之后使用已注册 Passkey；可配置多个 |
| 店主 | 自己店铺的账号、商品、卡密、队列、附件、处理器配置档案和授权 | 已验证邮箱与密码；可选 TOTP；也可注册多个 Passkey |
| 商品管理链接 | 指定商品的授权权限，最多向下委派更小权限 | 浏览器绑定或独立 CLI 设备绑定 |
| 商品 CLI 授权 | 一个商品的指定权限 | CLI 申请设备码，店主本人核对并批准 |
| 本店流水线 CLI 授权 | 本次批准的队列商品，只能查看、处理和重试任务 | CLI 申请本店当前商品快照，店主本人选择商品并批准 |
| 顾客 | 有效卡密及领取凭证对应的任务与交付 | 卡密和私密领取链接 |

店主注册 Passkey 后，自己的邮箱密码登录仍可用。平台首次密码失效规则只针对平台管理员。店主不能取得其他店铺商品的权限，也不能配置平台 SMTP 或打开公众注册。

## 邮件配置与邀请

公众注册和 SMTP 默认均关闭。推荐先由平台管理员在后台的账号管理中配置邮件服务，再创建店铺并向店主邮箱发送邀请。邀请链接只能使用一次，有效 24 小时；店主打开后设置至少 12 字符的密码。

SMTP 配置使用 TLS：`mode="starttls"` 或 `"ssl"`，服务端验证证书。账号密码加密保存，查询只返回 host、port、sender、启用状态和是否有凭据；不会读回 SMTP 密码。邮件内容及确认链接也加密入队，由 worker 实际发送。接口接受请求或写入邮件队列不代表邮件已经送达。

收到可靠的 SMTP `5xx` 永久拒绝后，该条邮件立即停止重试并清空正文；`4xx` 临时拒绝及连接故障继续指数退避，最多尝试 8 次，邮件过期会提前结束。认证、发件人或发送策略的拒绝只终止该条邮件，不会将收件地址全局标记为无效。修正 SMTP 配置后，需要重新请求邀请、注册验证或密码重置，已清空的旧邮件不会自动恢复。

使用 Sequenzy 时，配置 `host="smtp.sequenzy.com"`、`port=587`、`mode="starttls"`、`username="api"`，密码私下填写公司/工作区 API Key，并仅授予 `transactional:send` 权限；发件地址须使用已验证的发件域名。不要使用个人账号 API Key。详见 [Sequenzy SMTP 文档](https://docs.sequenzy.com/send-email/smtp)。

队列中的 `delivered` 表示 SMTP 服务商接受了邮件，不能证明最终到达收件箱；接受后的 SMTP 退出错误不会重复发送。最终送达、异步退信及地址压制状态需在服务商查看，Extore 暂无本地退信 Webhook 或地址压制表。`550 5.1.1` 等明确不存在的邮箱应由收件人更正地址，重试或取消压制不能恢复不存在的邮箱。

平台 CLI 使用经过批准的 root 设备：

```sh
extore admin platform settings get
extore admin platform settings update --json-file ./private-platform-settings.json
extore admin platform shops create --json-file ./shop.json
extore admin platform shops list
extore admin platform shops invite SHOP_ID
```

`private-platform-settings.json` 是新建的 0600 私密文件，结构示例：

```json
{
  "registration_enabled": false,
  "smtp": {
    "enabled": true,
    "host": "smtp.example.com",
    "port": 587,
    "mode": "starttls",
    "sender": "no-reply@example.com",
    "from_name": "Extore",
    "username": "在私密文件中填写",
    "password": "在私密文件中填写"
  }
}
```

更新时省略 `smtp.password` 会保留原密码，不应把查询结果中的脱敏占位符当作密码写回。配置不是环境变量；不要把凭据放进部署仓库、Shell 参数或公开提示词。`shop.json` 示例为 `{"name":"示例店铺","email":"owner@example.com"}`，创建会通过已启用的邮件服务发送邀请，不返回邀请 token。

启用公众注册须先启用 SMTP，再显式设置 `registration_enabled=true`。关闭 SMTP 后注册也会关闭。注册页面为 `/account/register`，店主须验证邮箱；默认关闭时页面引导使用平台邀请。

## 店主登录、TOTP 与恢复

浏览器访问 `/account/login`。邮箱密码验证后进入自己的后台。TOTP 是认证器提供的六位动态验证码；启用后，密码登录与敏感密码操作还需要当前验证码或一次性恢复码。网页「2FA 双因素认证」默认显示本地生成的二维码，支持切换手动输入和复制密钥；密钥不发送给外部二维码服务。输入有效验证码确认后才真正启用；可以管理恢复码、轮换恢复码或关闭 TOTP。

CLI 可直接批准自己的设备，无需浏览器选文件：

```sh
# 隐藏输入密码；已启用 TOTP 时也隐藏输入验证码
extore admin login --origin https://extore.example.com --email owner@example.com

# 自动化从标准输入或 0600 文件读取 {password,code? 或 backup_code?}
extore admin login --origin https://extore.example.com --email owner@example.com --credentials-file ./private-login.json
extore admin account get
extore admin totp setup --json-file ./private-password.json
extore admin totp confirm --json-file ./private-totp-confirm.json
```

TOTP 密钥与恢复码只保存到新建的私密输出文件，标准输出显示文件路径。已经注册 Passkey 的店主也可沿用 `extore admin login --origin …`，在浏览器核对设备信息后用自己的真实 Passkey 批准；设备身份固定到该店铺，不能切换到另一店铺或平台 root。

店主密码忘记时使用 `/account/reset` 发送邮件重置链接，有效 30 分钟且只能使用一次。已启用 TOTP 时还需验证码或恢复码。改密码或密码重置会撤销本店既有账号会话、店主 CLI 设备，以及商品处理设备和当前会话；商品管理链接配置、商品和任务保留。平台管理员全部 Passkey 丢失时使用服务器命令 `extore reset-auth`，它恢复平台首次密码，不重置各店的密码或 Passkey。

## 给 AI 授权商品与流水线

日常制作任务使用 `extore manage`，无需交出店主密码或私密管理链接。AI 在自己的设备生成密钥，申请设备码；你在返回的公开网址输入设备码，核对设备名称、完整指纹、店铺、商品、权限和期限，再亲自批准。申请理由只是 AI 提供的说明，不能代替你的判断。网页「复制给 AI 的提示词」提供命令与公开商品资料，不包含授权凭据，也不会自动发起申请。

只处理一个商品：

```sh
extore manage login --device-code --origin https://extore.example.com --product PRODUCT_ID --no-wait
```

同时处理本店当前多个队列商品：

```sh
extore manage login --device-code --origin https://extore.example.com --shop SHOP_ID --pipelines-all --no-wait
```

两种申请默认请求 `queue.view,queue.process,queue.retry`。单商品可通过 `--permissions` 请求该商品其他管理权限，最终由你选择；本店流水线始终仅支持这三个队列权限，不授予商品编辑、制卡、配置秘密、下级委派或账号管理。流水线商品必须属于同一店铺，处理方式为队列（协议 `mode="manual"`）；审批页面明确列出申请时的商品快照，你可以选择其中一部分。

批准后，AI 使用同一 profile、origin、设备名和目标再执行原命令，去掉 `--no-wait`，完成领取授权。`extore manage queues` 聚合已授权商品的待处理任务；读取与操作任务仍按单个商品的实际权限验证。多条授权的权限不会拼接成更大的管理权限。操作示例和文件上传见 [CLI 文档](cli.md)。

流水线授权保存的是本次选择的商品快照。以后新增的商品不会自动进入 AI 的范围；AI 必须重新申请追加，再由你批准：

```sh
# 明确申请新增的商品，可重复 --product
extore manage authorize --origin https://extore.example.com --authorization AUTHORIZATION_ID --product NEW_PRODUCT_ID --reason '需要接手新商品的制作任务' --no-wait

# 重新请求本店当前队列商品，仍由你逐项核对与选择
extore manage authorize --origin https://extore.example.com --authorization AUTHORIZATION_ID --pipelines-all --no-wait
```

单商品申请增加权限时，`--permissions` 写完整的目标权限集合，并保留已有权限；不能把已有单商品授权改成另一商品：

```sh
extore manage authorize --origin https://extore.example.com --authorization AUTHORIZATION_ID --permissions queue.view,queue.process,queue.retry,product.edit --reason '需要改善本商品的输入教程' --no-wait
```

追加申请通过同一设备私钥证明身份，引用已有授权 ID 与修订。审批时保留原商品、原权限和原到期时间；只能批准申请内的新增项，不能借追加延长有效期或跨店授权。拒绝、超时或追加失败保留原授权。既有管理链接设备也可通过 `--grant GRANT_ID` 申请本商品的新独立授权，原管理链接及其浏览器权限不会被改写。

后台可以按店铺查看设备的商品范围、权限、到期时间、修订和批准来源，撤销不用的授权。店主 CLI 也支持同样操作：

```sh
extore admin api GET /api/admin/pipeline-authorizations --query view=active
extore admin api GET /api/admin/pipeline-authorizations --query view=revoked
extore admin api DELETE /api/admin/pipeline-authorizations/AUTHORIZATION_ID --json-file ./revoke-scope.json
```

GET 的 CLI 输出为 `{ok:true,result:[...]}`，仅包含安全元数据；`view=revoked` 包括已撤销和已到期记录。撤销文件必须包含刚核对的真实修订，例如 `{"expected_revision":3}`，不能盲用示例数字；也可用 `--json-stdin` 从标准输入读取同样的 JSON。CLI 自动为 DELETE 的路径与实际请求体取得挑战并签名；修订改变时返回 409，不撤销也不交还任务，重新核对后再决定。明确撤销授权会结束派生会话与下级管理链接，并将未完成任务放回各自商品队列；交付记录保留。

普通浏览器退出不会结束已经领取授权的 CLI。撤销授权或设备后，旧私钥、旧 Bearer 和重放申请不能恢复被撤销的身份；到期后也不能通过续签延长期限。需要新的权限时重新申请并由你批准。店主修改或重置密码会撤销本店流水线授权；平台 `reset-auth` 只撤销最后完整审核由平台管理员批准的流水线授权，最后完整审核由店主批准的授权保留。追加在 CLI 签名领取成功后更新批准来源，审计记录保留之前与之后的批准者；仅申请或网页批准不改变既有授权。

`extore admin` 是另外的店主管理登录流程，具有本店完整管理能力，协议身份仍为 `role="admin",scope="shop.owner"`。它与商品 `kind="product"`、本店流水线 `kind="shop.pipeline"` 分开；不要把全店账号管理授权作为 AI 处理队列的默认选择。安全元数据、撤销接口与签名规则见 [协议](protocol.md#cli-设备授权)。

## 处理器配置档案

预设定义中的 `configuration` / `shop_configuration` 声明允许配置的字段及其敏感性。店主创建自己的加密档案，再把指定档案修订绑定到同店铺、同处理器商品。处理器目录只提供代码定义的字段、说明和默认值，不包含商家保存的实际配置。

本店店主和平台管理员读取档案时，除 ID、名称、修订等元数据，还可在 `configuration` 中读回明确标为 `secret:false` 的模板、说明等内容，并继续编辑。`configured_fields` 只列出已保存且非空的字段名，用于表示配置状态，不返回凭据值。标为秘密、没有声明 `secret`、不是严格 `false` 或已经不在字段定义中的值，一律隐藏；私密导出也不能读回真实凭据。商品绑定返回的 `profile.configuration` 对应绑定的修订，不自动使用档案最新修订。

```sh
extore admin processors --detail
extore admin processor-profiles create --json-file ./private-profile.json
extore admin processor-profiles list
extore admin processor-profiles bind PROFILE_ID --product PRODUCT_ID
extore admin processor-profiles binding --product PRODUCT_ID
extore admin processor-profiles update PROFILE_ID --json-file ./private-profile-update.json
extore admin processor-profiles revoke PROFILE_ID
```

`private-profile.json` 示例：

```json
{
  "processor_id": "personalized_text",
  "name": "欢迎文本配置",
  "configuration": {"template": "你好，$name！你的商品已准备好。"}
}
```

修改配置创建新修订，但商品绑定仍指向原修订；需要再执行 `bind PROFILE_ID --product PRODUCT_ID`，让以后发行的卡密使用新修订。已有卡密保留发行时的修订。多个商品共享档案时，分别决定何时更新各自绑定；使用旧 `processor_config` 修改单个商品则创建独立档案，不改写共享档案。停用档案会阻止继续使用该档案，包括旧修订；不能跨店绑定配置，也不能把付款账号字段随意塞进现有模板。

更新档案时，省略字段会保留原值，秘密字段传空字符串也保留原凭据；可选非秘密说明传 `""` 则明确清空。必填模板不能用空字符串保存，接口返回 422，不会悄悄恢复默认模板。读取结果中的隐藏字段状态不能当成凭据重新写入。

旧 `processor_config` 写入方式仍可创建独立的本店配置档案。店主和平台管理员的商品编辑接口可读回其中明确标为 `secret:false` 的值及配置状态；商品管理链接和顾客读取仍返回空配置对象。复制商品资料给其他平台的提示词不包含任何处理器配置，包括可读的模板。实际执行前由 worker 校验卡密、商品、店铺和绑定修订，解密本任务需要的配置；子进程收到单独的 `configuration` 和不可变 `shop_context={shop_id,profile_id,revision}`，顾客参数不能覆盖这些上下文。

SMTP、TOTP 和处理器配置使用 `EXTORE_DATA/master-secrets.key` 加密。备份需同时保留数据库、此密钥及用于制卡幂等响应的 `issuance.key`。密钥丢失后系统拒绝读取原密文，不会自动生成替代密钥伪装恢复成功。

## 工作流变量、秘密与运行限制

工作流配置与隔离执行需要 Extore 0.7.0 及以上。处理器配置档案可保存 `workflow`，由本店店主或平台管理员通过网页和店主 CLI 管理。普通变量放在 `variables`，可以读回和编辑；访问密钥等放在 `secrets`，只写入，不读回。秘密配置的查询结果只有 `configured_secret_names`，不会在字段里回填原值。商品管理链接、顾客页面和复制商品资料的提示词都不包含工作流配置。

创建档案时，可在前面的 `private-profile.json` 中加入：

```json
{
  "workflow": {
    "variables": {"BRAND_NAME": "示例店铺"},
    "secrets": {"SERVICE_TOKEN": "在本地私密文件中填写真实值"},
    "runtime": {
      "timeout_seconds": 120,
      "memory_mb": 256,
      "cpu_seconds": 120,
      "max_output_bytes": 1000000
    }
  }
}
```

变量名为 1–64 字符的大写字母、数字和下划线，首位必须是字母，不能以 `EXTORE_` 开头。系统环境、语言运行时、动态加载器和代理名称也保留，例如 `PATH`、`HOME`、`PYTHON*`、`LD*` 和 `*_PROXY`。同一名称不能同时是变量和秘密。两组各最多 64 项，单值最多 8 KiB，两组值合计最多 64 KiB，按 UTF-8 字节计算，值不能含 NUL。配置不支持自定义命令、容器镜像、挂载或联网权限。

`update` 的 `workflow` 按名称合并，省略原项会保留；普通变量 `""` 保存为空，已有秘密 `""` 保留原值。`runtime` 也逐项合并，只修改需要调整的上限。删除必须在更新时明确使用 `delete_variables` 或 `delete_secrets` 名称数组，创建时不接受删除字段；不能同时设置与删除同一组里的同名项。下面只删除指定秘密，其他设置保留：

```json
{"workflow":{"delete_secrets":["SERVICE_TOKEN"]}}
```

工作流与处理器配置保存在同一个加密修订中。更新后重新绑定商品，只影响以后发行的卡密；已有卡密和重试仍使用发行时的变量、秘密与运行限制。读取商品绑定时，`profile.workflow` 同样对应绑定修订；旧档案自动按默认工作流兼容，不重新发行卡密。

运行限制的默认值为 120 秒墙钟时间、256 MiB 进程地址空间、120 秒 CPU 时间和 1000000 字节输出。字段 `memory_mb` 实际按 MiB 计算；可调范围见[协议](protocol.md#工作流配置协议)。档案完整 JSON 编码最多 200000 字节，超限不会创建修订。送入处理器的整份任务输入也最多 200000 字节，包含顾客参数、规格和步骤快照、处理器配置与工作流环境；变量容量没有超限也仍可能因为整份输入过大而被拒绝，不能靠拆字段绕过。

处理器通过只读 `Task.environment` 或 `ProcessorContext.environment` 按原名称读取变量与秘密。运行进程的环境名统一加前缀，例如 `BRAND_NAME` 对应 `EXTORE_WORKFLOW_BRAND_NAME`，不能覆盖 worker 的系统环境。顾客参数不能改变这些配置。现有文本处理器仍只替换它代码支持的 `$name`、`${name}` 和 `$$`；工作流秘密不会自动变成交付模板变量。

处理器可以读取为它配置的秘密，因此仍只运行审核后固定版本的可信代码。不要在进度、交付内容或日志中输出秘密。工作流配置本身不提供支付能力；内置处理器不能直接联网付款，未来的支付对接需要另行实现按店铺校验目标、额度与幂等键的受控服务。

自动处理需要服务器具备 Linux、bubblewrap 0.12 及以上（`bwrap`）、libseccomp 和可用的内核命名空间。当前内置环境只运行固定的文本与资源链接预设，使用 stdin/stdout 传递任务与结果；不能联网、fork、启动外部程序或创建文件。代码只读，数据库和主密钥不挂载。依赖缺失或隔离不可用时任务不会退回宿主直接执行，而是失败待商家核实。复杂文档和 PPT 仍由外部 AI 通过 CLI 处理商品队列、上传交付文件，不在这里生成；未来联网或写盘处理器需要另行设计受控服务和 cgroup。任意脚本上传仍不开放，完整边界见[协议](protocol.md#处理器运行边界)。

## 存储额度与付款边界

任务流程与处理目标也按店铺隔离。每张流程卡固定发行时的图、配置档案修订、Webhook 目标与密钥；顾客字段不能改写执行者或店铺。普通工作流变量／只写秘密用于处理器配置，流程的短期敏感输入来自当前顾客步骤，两者不能混同。后者只传给当前允许的处理节点、有短期有效期，不通过普通事件、导出或历史回读。详见[任务流程](task-flow.md)和[私有 Worker v2](private-worker.md)。

商品 stock 文本库存按规格发行，原文与卡密身份加密绑定，导入容量同样计入店铺和全站；新原卡与对应表只在创建响应显示。图片与图片集合每张单独上传、计量和归属校验。签名[兑换路由](proxy-config.md)只能由店主或明确选店的平台管理员维护，公开配置不含签名私钥。

附件同时受单文件、单卡、单店和全站额度约束。新店默认 1 GiB，平台管理员可以单独调整：

```sh
extore admin platform shops quota SHOP_ID --bytes 1073741824
```

店主的 `extore admin storage` 只显示自己的附件占用与额度；平台 root 可以查看全站存储。禁用店铺阻止新工作、上传和管理；已有任务的领取链接仍保留原规则下的只读状态与交付，不代表可继续提交或处理。清理草稿、预留磁盘空间和并发上传限制见[文件上传与存储](getting-started.md#文件上传与存储)。

付款适配默认关闭，实际供应商平台仍待确定。`resource_link` 与 `personalized_text` 只交付配置的链接或文本，不产生付款交易。未来接入须限定同店铺的账号、目标平台和幂等键，先验证真实提供商返回结果，再报告成功；本地配置或测试不能作为已付款的证明。

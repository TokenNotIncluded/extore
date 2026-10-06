# 多店与账号

[项目首页](../README.md) · [运行指南](getting-started.md) · [店主 CLI](cli-owner.md) · [Python SDK](python-sdk.md)

Extore 一个实例可以服务多个独立店铺。商品、卡密、队列、附件、管理链接、配置档案、会话与审计均按店铺校验权限；支付下单仍由上游平台负责。已有单店数据迁移到默认店铺，不会重新生成卡密或任务。

## 平台管理员和店主

| 身份 | 可管理的范围 | 登录方式 |
| --- | --- | --- |
| 平台管理员（root） | 店铺创建、启停、存储额度、注册开关、SMTP；跨店管理需明确店铺或商品 | 首次初始化密码只用于注册 Passkey，之后使用已注册 Passkey；可配置多个 |
| 店主 | 自己店铺的账号、商品、卡密、队列、附件、处理器配置档案和授权 | 已验证邮箱与密码；可选 TOTP；也可注册多个 Passkey |
| 商品管理链接 | 指定商品的授权权限，最多向下委派更小权限 | 浏览器绑定或独立 CLI 设备绑定 |
| 顾客 | 有效卡密及领取凭证对应的任务与交付 | 卡密和私密领取链接 |

店主注册 Passkey 后，自己的邮箱密码登录仍可用。平台首次密码失效规则只针对平台管理员。店主不能取得其他店铺商品的权限，也不能配置平台 SMTP 或打开公众注册。

## 邮件配置与邀请

公众注册和 SMTP 默认均关闭。推荐先由平台管理员在后台的账号管理中配置邮件服务，再创建店铺并向店主邮箱发送邀请。邀请链接只能使用一次，有效 24 小时；店主打开后设置至少 12 字符的密码。

SMTP 配置使用 TLS：`mode="starttls"` 或 `"ssl"`，服务端验证证书。账号密码加密保存，查询只返回 host、port、sender、启用状态和是否有凭据；不会读回 SMTP 密码。邮件内容及确认链接也加密入队，由 worker 实际发送。接口接受请求或写入邮件队列不代表邮件已经送达。

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

浏览器访问 `/account/login`。邮箱密码验证后进入自己的后台。TOTP 是认证器提供的六位动态验证码；启用后，密码登录与敏感密码操作还需要当前验证码或一次性恢复码。设置时先生成并私下显示密钥，输入有效验证码确认后才真正启用；可以管理恢复码、轮换恢复码或关闭 TOTP。

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

## 处理器配置档案

预设定义中的 `configuration` / `shop_configuration` 声明允许配置的字段。店主创建自己的加密档案，再把指定档案修订绑定到同店铺、同处理器商品。档案查询只返回 ID、名称、处理器 ID、修订、禁用状态和时间；即使请求私密导出也不能取得配置明文。

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

旧 `processor_config` 写入方式仍可创建独立的本店配置档案，但读取商品时返回空配置对象。实际执行前由 worker 校验卡密、商品、店铺和绑定修订，解密本任务需要的配置；子进程收到单独的 `configuration` 和不可变 `shop_context={shop_id,profile_id,revision}`，顾客参数不能覆盖这些上下文。

SMTP、TOTP 和处理器配置使用 `EXTORE_DATA/master-secrets.key` 加密。备份需同时保留数据库、此密钥及用于制卡幂等响应的 `issuance.key`。密钥丢失后系统拒绝读取原密文，不会自动生成替代密钥伪装恢复成功。

## 存储额度与付款边界

附件同时受单文件、单卡、单店和全站额度约束。新店默认 1 GiB，平台管理员可以单独调整：

```sh
extore admin platform shops quota SHOP_ID --bytes 1073741824
```

店主的 `extore admin storage` 只显示自己的附件占用与额度；平台 root 可以查看全站存储。禁用店铺会阻止继续兑换、上传和管理。清理草稿、预留磁盘空间和并发上传限制见[文件上传与存储](getting-started.md#文件上传与存储)。

付款适配默认关闭，实际供应商平台仍待确定。`resource_link` 与 `personalized_text` 只交付配置的链接或文本，不产生付款交易。未来接入须限定同店铺的账号、目标平台和幂等键，先验证真实提供商返回结果，再报告成功；本地配置或测试不能作为已付款的证明。

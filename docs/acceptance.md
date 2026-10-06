# 验收记录

## 0.2.0 更新（2026-10-06）

- 首页空状态改为「暂无公开商品」，采用分段虚线与简化折线撕纸边缘。桌面 1440 × 1000、手机 390 × 844 检查，管理链接权限选择可在手机操作。
- 队列按商品分开，管理员先选商品，管理链接固定商品；跨商品批处理整批拒绝。
- 商品管理链接支持 8 项权限和店长完整商品管理，子链接严格缩权且不能超过父级期限。展示编辑与自动发货配置分别授权，展示编辑者无法读取签名密钥。
- 后端 101 项测试、WebMCP 52 项 Node 测试、领取页面上下文 6 项 Node 测试通过，共 159 项。Ruff、Python 编译、JavaScript 语法与 0.2.0 wheel 构建通过。
- Chromium 154 开启 `--enable-features=WebMCP`，确认实际使用 `document.modelContext`。真实原生工具完成商品创建、制卡、卡密验证、参数提交、店长创建下级链接、处理者完成人工交付、顾客查看及销毁。连续工具调用保持同一上下文注册稳定。
- 浏览器中注册两个独立虚拟验证器（internal / USB），分别独立登录成功；密码禁用。未进行真实手机或硬件 Passkey 验收。
- 上级管理链接撤销后，店长与处理者的原生工具调用均返回 `forbidden`，下级会话立即失效。
- 上述业务操作使用本地隔离数据库，没有向生产增加测试商品、卡密或 Passkey。
- UI 检测器缺少 HTML 解析依赖而降级为正则检查；渲染结果另经浏览器检查，不将降级扫描视为完整界面检测。

以下为初始化版本 0.1.0 的历史验收。

## 已验证

- Python 3.12 与 3.14 均运行核心测试；并发兑换仅产生一个任务和请求事件。
- 卡密哈希存储、非公开商品、输入验证、批量事务回滚。
- 重复查看、原子一次领取、销毁清理内容及参数。
- 失败重试与次数限制，未知外部交付状态须商家核实。
- 员工限商品授权、领取所有权、权限撤销。
- 签名回调、过期签名、nonce 重放、旧尝试和终态覆盖拒绝。
- 制卡 API 的持久化幂等与加密响应。
- Python 脚本真实子进程输入、进度和成功结果。
- Webhook 持久化重试、过期请求取消、私有网络地址拒绝。
- 浏览器完整流程：卡密 → 参数 → 脚本发货 → 查看内容。
- Chromium 虚拟验证器完成 Passkey 注册与重新登录；注册后密码请求 HTTP 403。没有真实硬件 / 手机 Passkey 验收。
- 桌面 1280 × 720、手机 390 × 844 截图检查；手机无横向溢出。
- Docker 镜像构建与容器内应用加载，Compose 配置校验。

## 对接边界

真实支付平台、真实商品提供商、真实外部 Webhook 往返尚未对接。现有测试不能证明第三方发货、扣款或结算成功。

脚本不是不可信代码沙箱。只支持单商家、单 worker；当前脚本逐个执行。后台使用中文，顾客主要流程及参数支持中英切换。商品图片使用商家填写的 HTTPS URL，不包含上传存储服务。

已成功的一次领取遇到响应丢失可能无法恢复。销毁删除应用内内容，不撤回外部副本，不清理历史备份，不承诺物理擦除。

## 2026-10-06 生产部署

- 服务器：SSH alias `archczy`；公网域名 [extore.lmm.best](https://extore.lmm.best)。
- 运行代码：`375b43f71b37e58cd9ceb90be1b67dd748519aa2`，原生包 `extore 0.1.0-2`。之后的验收文档提交不改变运行代码。
- 源码归档 SHA-256：`63c2b105b65c7cb9b66b98079bed991578fd634e42073b2f2d937181fadb58c5`，服务器构建前校验一致。
- [代码 CI](https://github.com/TokenNotIncluded/extore/actions/runs/37447234414) 通过：23 项测试、Ruff 检查与格式、Python 编译、前端语法及 wheel 构建。
- `extore-api`、`extore-worker` enabled + active/running，验收时 `NRestarts=0`。数据目录 0700，数据库、发行响应密钥和首次密码文件 0600。
- `pacman -Qkk extore`：1505 文件，0 被修改。SQLite `integrity_check=ok`。
- HTTPS 证书有效至 2027-01-04；现有 `certbot-renew.timer` enabled，增加仅针对本域名的 nginx 重载 hook。
- 公网 GET / HEAD 首页与 `/health` 均 200；浏览器静态资源加载成功，无应用错误。首页无公开商品，连续点击 Logo 5 次可进入首次密码登录页面。
- 公网浏览器真实流程：临时私有卡密 → 输入参数 → 生产 worker 执行脚本 → 成功领取 → 销毁后不可领取。
- 通过公开 HTTPS 调用 Python SDK 签名回调，更新进度、成功交付、领取及销毁均通过。公网 TLS 固定 IP 发起 webhook 请求正常到达测试目标（测试目标不接收 POST，预期返回 405）；没有真实商家平台通知接收验收。
- 验收商品、卡密、任务和临时脚本已清除；生产库商品、卡密、任务计数均为 0。首次密码保留供商家自行注册 Passkey，没有在生产注册测试者 Passkey。
- 原有 `msg.lmm.best` 与 `donate.lmm.best` 部署后均返回 200。

商家在自己的终端执行：

```sh
ssh archczy 'sudo cat /var/lib/extore/bootstrap-password.txt'
```

不要将密码粘贴到聊天。打开 `/admin`，输入首次密码并注册自己的 Passkey。注册成功后密码登录禁用，服务器上的首次密码文件删除。丢失所有 Passkey 时：

```sh
ssh -t archczy 'sudo -u extore extore-admin reset-auth'
```

维护升级应在目标服务器重新构建原生包，再用 pacman 更新；系统 Python 大版本改变后也须重建该包。CLI 通过 `extore-admin` 加载 `/etc/extore/extore.env`，无需激活虚拟环境或使用打包时的路径。


## 0.2.0 生产更新（2026-10-06）

- 运行代码：`e4ce8a1b9f766cb9bb9c1602b5e365e7ed76c1e3`；Arch 原生包 `extore 0.2.0-1`，在目标 Python 3.14.7 构建。后续仅验收文档提交不改变运行代码。
- 源码 SHA-256：`293d460bde5182a20f50e8c46e8fbded7e8df944553ee60e5c290909d18e47a0`，服务器构建已校验。[代码 CI](https://github.com/TokenNotIncluded/extore/actions/runs/37454866120) 全部通过。
- 升级前受保护备份：`/var/backups/extore/20261006-0.2.0`。数据库既有字段逐表指纹与发行密钥、配置文件校验一致；保留商家已注册的 1 个 Passkey，密码登录仍禁用。生产商品、卡密、任务、管理链接均无测试数据。
- SQLite 结构版本升到 2，`integrity_check=ok`；`pacman -Qkk extore` 为 1507 个文件、0 被修改。
- API 与 worker enabled、active/running，`NRestarts=0`。数据目录 0700、数据库与发行密钥 0600。
- HTTPS 首页、健康检查与 WebMCP 静态资源 200；公网 JS、CSS、虚线 SVG 与提交内容逐字节一致。
- 开启 WebMCP 的真实 Chromium 在公网确认 `document.modelContext` 与首页 5 个原生工具，实际执行公开商品查询；连续点击 Logo 5 次进入 Passkey 登录页，匿名后台只注册上下文和导航工具。
- 未启用 WebMCP 的 Chromium 正常显示兑换表单，未伪造浏览器 API。生产未执行 Passkey 注册或重置。

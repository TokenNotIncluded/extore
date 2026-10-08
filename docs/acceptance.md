# 验证指南

此页说明如何验证当前代码，不汇总已经过时的测试数量、服务器状态或临时验收账号。版本变化见 [releases](releases/)，历史验收记录可从 Git 历史查阅。当前部署步骤见[运行指南](getting-started.md)，账号规则见[多店与账号](shops.md)。

## 代码检查

先取得主仓库固定的处理器子模块，再使用锁定依赖：

```sh
git submodule update --init --recursive
uv sync --frozen --python 3.12
uv run ruff check extore scripts tests examples
uv run ruff format --check extore scripts tests examples
uv run pytest -q
node --test tests/*.test.cjs
uv run pytest -q processors/official/tests
uv run python examples/workflows/validate_examples.py
uv build -q
uv run extore --help
uv run extore --version
```

完整检查和处理器隔离环境的准备方法以 [CI 工作流](../.github/workflows/ci.yml)为准。JavaScript 语法检查自动遍历 `extore/static`，包括随项目提供的第三方脚本，不维护第二份文件清单。

报告必须标注代码提交、Python 版本、检查命令和结果。跳过或失败的测试单独说明；不能用旧版本的通过数量代替当前结果。没有处理器隔离条件的本机测试，不能证明生产隔离执行正常。

## 数据库与旧数据

API 和 worker 启动时先完成 `extore.db.init()`。数据迁移保留旧卡密、任务、授权与交付。业务处理只使用完整结构；缺表或缺列必须报错，不能按零用量、无历史交付或无授权规则继续运行。

在隔离数据库中验证首次初始化、重复初始化和旧结构升级。检查原有数据、发行密钥与加密密钥未被替换，并确认 `PRAGMA integrity_check` 和 `PRAGMA foreign_key_check` 的结果。不能通过删除生产数据让迁移测试通过。

## 业务与权限

使用隔离测试店铺验证卡密发行、兑换、参数提交、处理、领取、重试、修改交付与销毁。检查并发兑换、重复请求、过期请求和响应丢失，不只检查第一次请求成功。

至少覆盖跨店与跨商品拒绝、权限撤销、旧任务快照、一次领取、附件容量、处理超时及服务重启后的恢复。数据库事务、配额和任务隔离不能因兼容分支而被跳过。

桌面与手机分别检查登录、兑换、队列和领取页面。检查键盘操作、减少动态设置、页面隐藏后的轮询和动画行为。没有原生 WebMCP 的浏览器应保持普通页面可用；模拟接口的测试不能代替原生浏览器验证。

## 部署与外部服务

代码检查、构建、发布、部署和真实业务验收是不同结果，分别记录。部署报告注明实际运行提交与版本、服务状态、健康检查和升级前后的数据核对结果，不把历史服务器名称写成通用操作命令。

SMTP 接受不等于邮件最终送达；Webhook 返回不等于外部业务已完成；页面配置成功不等于付款成功。真实邮件、硬件 Passkey、外部处理服务和支付平台往返，只在实际完成后记录。不要把卡密、私密链接、密码或密钥写进仓库、日志或验收报告。

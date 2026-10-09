# 自动部署

推送到 `main` 会运行 [Deploy production](../.github/workflows/deploy.yml)，也可以在 GitHub Actions 页面手动运行。任务由 GitHub 托管的 `ubuntu-24.04` 执行器运行；电脑关机也能部署。

其他分支、PR 和标签不触发生产部署。连续推送共用一个部署队列；正在安装的任务不会被下一次推送中断。服务器在构建和安装前都检查远程 `main`，跳过已经落后的提交。

## 仓库 Secrets

在仓库 **Settings → Secrets and variables → Actions → Repository secrets** 配置：

| Secret | 内容 |
| --- | --- |
| `EXTORE_DEPLOY_HOST` | 生产服务器 IP 或域名，SSH 端口 22 |
| `EXTORE_DEPLOY_USER` | 专用 SSH 用户 `extore-deploy` |
| `EXTORE_DEPLOY_SSH_KEY` | 专用 Ed25519 私钥全文 |
| `EXTORE_DEPLOY_KNOWN_HOSTS` | 已核实的服务器 SSH 公钥记录；必须匹配部署主机 |
| `EXTORE_BACKUP_RECIPIENT` | age 备份加密公钥，形如 `age1…` |
| `EXTORE_BACKUP_IDENTITY` | 对应的 age 解密私钥，供恢复使用；部署任务不读取它 |

`production` 环境只允许 `main`。部署 SSH 公钥在服务器上配置 `restrict` 和 forced command，只能调用 `/usr/local/lib/extore-deploy/server.py` 中允许的部署操作。普通远程 Shell、端口转发和交互登录均被禁用。SSH 主机公钥严格校验。

生产服务器已配置 `extore-deploy` 登录用户、`extore-build` 构建用户、SSH forced command 和限定的 sudo 规则。管理程序是 root 所有的文件；更改服务器端协议时，操作人员应先安装经审阅的 `scripts/deploy/server.py`，再发布相应工作流。

生产使用 Arch Linux 原生包，固定 `uv.lock` 依赖和处理器子模块提交。版本号和 `pkgrel` 由发布提交维护；实际运行的 Git 提交记录在服务器 `/var/lib/extore-deploy/current.json`。

## 部署和恢复

1. 拉取远程 `main` 的准确提交及固定子模块，用专用构建用户创建原生包，记录源码与包的 SHA256。
2. 短暂停止 API 和 worker，归档程序、配置、数据库、两把业务密钥、权限、链接和旧包管理信息，随后立即恢复旧服务。网络传输期间服务继续运行。
3. GitHub 执行器接收归档，比较两端 SHA256，完整读取归档并在独立目录恢复 SQLite，执行 `integrity_check`。
4. 用 age 公钥加密备份，上传到该次 Actions 运行的 artifact，保留 **30 天**。上传成功并返回 artifact ID 后才允许安装；上传失败时保留服务器临时恢复材料，下一次任务先归档它。
5. 安装新包，启动 API 和 worker，核对全部安装源码 SHA256、服务状态、SQLite 完整性、公开 HTTPS 健康状态和 API 版本，以及全部公开静态文件 SHA256。
6. 记录运行提交，删除服务器本次构建目录、依赖缓存和已经上传验证的临时备份。任务结束时删除执行器上的 SSH 私钥及明文备份。

安装失败且数据库结构未变化时，自动恢复旧程序、配置和包管理信息，保留最新业务数据。数据库结构已变化时停止服务并保留数据，从该次运行的加密 artifact 手动恢复，避免旧数据库覆盖新交易。

备份在 Actions 运行页面的 **Artifacts** 中。下载后，用 age 解密并校验原归档 SHA256：

```sh
sha256sum -c SHA256SUMS
age --decrypt --identity backup.agekey --output recovery.tar.gz recovery.tar.gz.age
sha256sum recovery.tar.gz  # 与 manifest.json 中 archive_sha256 比较
```

`backup.agekey` 是 `EXTORE_BACKUP_IDENTITY` 的原始文件；恢复私钥的本地安全副本位于统一备份目录 `/home/lightjunction/Backups/servers/archczy/extore/github-actions-20261010/backup.agekey`。更换加密公钥后，应保留旧解密私钥直到对应备份不再需要。

## 验证和排查

```sh
gh run list --workflow deploy.yml
gh run view RUN_ID --log-failed
```

Actions 成功结果以实际上线检查为准，运行摘要记录提交、原生包版本、静态文件检查数量和备份 artifact ID。服务器可检查 `extore-api.service`、`extore-worker.service` 和 `/var/lib/extore-deploy/current.json`。

日常源码验证在本机完成，`CI` 只在手动运行或正式 `v*` 标签发布时执行。本机验证部署实现：

```sh
uv run ruff check scripts/deploy tests/test_deployment.py
uv run ruff format --check scripts/deploy tests/test_deployment.py
uv run pytest tests/test_deployment.py -q
```

GitHub 官方文档：[托管执行器](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)、[工作流使用 Secrets](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)、[上传和保留 artifact](https://github.com/actions/upload-artifact)。

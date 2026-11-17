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

生产使用 Arch Linux 原生包，固定 `Cargo.lock` 依赖和处理器子模块提交。Rust 服务在 GitHub 托管执行器的固定 Arch 容器中编译，生产机验证产物后安装。Python 仅保留审核过的处理器运行器与标准库 SDK。版本号和 `pkgrel` 由发布提交维护；实际运行的 Git 提交记录在服务器 `/var/lib/extore-deploy/current.json`。

## 部署和恢复

1. GitHub 执行器检出准确的 `main` 提交及固定子模块，使用 `scripts/deploy/build_package.py` 创建原生包。构建镜像固定 Arch 基础镜像摘要；生产所需的 OpenSSL 3.6.5 头文件与库置于独立目录，核对官方签名及固定 SHA256，不改动生产运行库。
2. 同一个部署工作流通过 GitHub OIDC 为安装包生成产物证明。SSH 客户端上传有界的包与证明；服务器验证包摘要、仓库数字身份、工作流完整身份、`main` 引用及源码和签发提交，拒绝非 GitHub 托管执行器的证明。服务器同时对部署操作加互斥锁。
3. 服务器独立检出该提交并生成源码归档，核对解压后 tar 内容的 SHA256。压缩库可能生成不同的 gzip 字节，因此包内源压缩文件仍保留自己的校验值，而跨机器源码身份使用 tar 内容。服务器逐字节核对非编译文件、文件集合、权限、配置模板、包名和架构，再以低权限构建用户执行原生版本与 ABI 检查。任何预上传产物不匹配都会拒绝部署；只在完全没有预上传材料时保留原有源码构建路径。
4. 短暂停止 API 和 worker，归档程序、配置、数据库、两把业务密钥、权限、链接和旧包管理信息，随后立即恢复旧服务。网络传输期间服务继续运行。
5. GitHub 执行器接收归档，比较两端 SHA256，完整读取归档并在独立目录恢复 SQLite，执行 `integrity_check`。用 age 公钥加密备份，上传到该次 Actions 运行的 artifact，保留 **30 天**。上传成功并返回 artifact ID 后才允许安装；上传失败时保留服务器临时恢复材料，下一次任务先归档它。
6. 安装新包，启动 API 和 worker，核对全部安装文件 SHA256、服务状态、SQLite 完整性、公开 HTTPS 健康状态和 API 版本，以及全部公开静态文件 SHA256。
7. 记录运行提交，删除服务器本次构建目录、依赖缓存和已经上传验证的临时备份。任务结束时删除执行器上的 SSH 私钥及明文备份。

安装失败时，只有数据库结构完全相同，或确认仅发生本轮已验证的 schema 22→23 增加 `commerce_bindings` 表与索引，才自动恢复旧程序、配置和包管理信息。回滚保留最新数据库与新连接记录，不覆盖业务数据。其他结构变化会停止服务并保留数据，交由操作人员依据该次运行的加密 artifact 恢复。备份恢复和旧程序对 schema 23 的兼容性须在独立目录实际演练。

服务器需要支持上述完整证明验证参数的 `github-cli`；本次验证使用 2.104.0。更改服务器端协议时，先安装匹配的端点，再推送工作流。证明验证无需在生产机配置 GitHub 账户令牌，但验证器可能需要获取公开的签名信任材料。

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
uv run ruff check scripts/deploy tests/test_deployment.py tests/test_prebuilt_deployment.py tests/test_prebuilt_deploy_client.py
uv run ruff format --check scripts/deploy tests/test_deployment.py tests/test_prebuilt_deployment.py tests/test_prebuilt_deploy_client.py
uv run pytest tests/test_deployment.py tests/test_prebuilt_deployment.py tests/test_prebuilt_deploy_client.py -q
```

GitHub 官方文档：[产物证明动作](https://github.com/actions/attest)、[证明验证参数](https://cli.github.com/manual/gh_attestation_verify)、[托管执行器](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)、[工作流使用 Secrets](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)、[上传和保留 artifact](https://github.com/actions/upload-artifact)。

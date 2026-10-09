# 自动部署

推送到 `main` 会运行 [Deploy production](../.github/workflows/deploy.yml)，也可以在 GitHub Actions 页面手动运行。其他分支、PR 和标签不触发生产部署。连续推送共用一个部署队列，不会中断正在安装的版本；已经被更新的 `main` 超过的提交会跳过。

生产使用 Arch Linux 原生包，固定 `uv.lock` 依赖和处理器子模块提交。工作流不自动修改项目版本或发布 PyPI；版本号及 `pkgrel` 仍由发布提交维护。即使版本号相同，也会按准确的 Git 提交重新安装，实际提交另存于服务器 `/var/lib/extore-deploy/current.json`。

## 已配置的执行服务

`extore-deploy` 专用 runner 在本机运行，systemd 服务 `extore-actions-runner.service` 开机自启。**本机需要开机联网**；离线时 GitHub 的部署任务等待 runner 上线。

runner 使用独立的 `extore-runner` 用户，没有 SSH 私钥、备份目录权限或 sudo。工作流不检出源码，只调用 root 所有的 `/usr/local/lib/extore-deploy/request.py`，通过受限 Unix socket 请求部署。

独立的 `extore-deploy@.service` 负责部署。它只能接受完整提交 SHA；服务器再次核对它是否为远程 `main` 的当前提交。部署使用单独 SSH 密钥、固定主机公钥和 forced command，不允许任意远程 Shell。源码和原生包由生产机独立的 `extore-build` 用户构建。

部署管理程序安装于 `/usr/local/lib/extore-deploy/`，配置位于 `/etc/extore-deploy/`。仓库中的实现是可审阅的副本；修改这些管理程序或 systemd 配置需要由服务器操作人员同步安装，不能让普通工作流替换特权程序。

## 部署步骤

1. 核对远程 `main`，拉取准确源码和固定子模块，构建原生包并记录 SHA256。
2. 短暂停止 API 和 worker，将程序、配置、数据库、两把业务密钥、权限、链接和旧包管理信息直接流式写入 `/home/lightjunction/Backups/servers/archczy/extore/日期时间-提交/`，随后恢复旧服务。
3. 完整读取归档并在独立目录恢复 SQLite，执行 `integrity_check`；传输或校验失败不会安装新版本。
4. 再次核对 `main`，安装包并启动 API 和 worker。
5. 核对全部安装源码 SHA256、两项服务状态与重启次数、SQLite 完整性、本机与公开 HTTPS 的健康状态和 API 版本，以及所有公开静态文件 SHA256。
6. 保存当前部署提交和本机 `result.json`，清理本次服务器构建和依赖缓存。服务器不留历史备份副本。

失败时，数据库结构未变化则自动恢复旧程序、配置和包管理信息，保留最新业务数据。结构已变化时停止服务并保留业务数据，报告本机恢复归档位置，避免用旧数据库覆盖部署后的交易。回滚需要从本机传回归档。

## 排查

```sh
systemctl status extore-actions-runner.service extore-deploy.socket
journalctl -u extore-actions-runner.service --since '1 hour ago'
gh run list --workflow deploy.yml
```

部署日志与 GitHub Actions 的成功结果以真实健康检查为准。`result.json` 记录部署前后数据库结构与业务表行数、恢复检查结果和验证的静态文件数量；备份目录是 0700，文件是 0600。

日常源码验证在本机完成。`CI` 只在手动运行或正式 `v*` 标签发布时执行，生产工作流负责构建、备份、安装与上线核验。

本机验证部署实现：

```sh
uv run ruff check scripts/deploy tests/test_deployment.py
uv run ruff format --check scripts/deploy tests/test_deployment.py
uv run pytest tests/test_deployment.py -q
```

GitHub 的 runner 和工作流配置参考：[添加 self-hosted runner](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners)、[工作流语法](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)。

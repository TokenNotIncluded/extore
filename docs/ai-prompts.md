# 可复制的 AI 接入提示词

[CLI 总览](cli.md) · [顾客 CLI](cli-customer.md) · [店主 CLI](cli-owner.md)

网站的队列、进度看板、店主管理和处理器配置入口只复制一句任务说明，附上当前站点 `/AGENTS.md` 的通用规则链接。安装、设备码授权、权限边界与操作指南统一放在[规则文件](../extore/static/AGENTS.md)，通过公开的 `GET /AGENTS.md` 或 `HEAD /AGENTS.md` 读取，无需登录。

提示词保留本次需要的商品、店铺、处理器、配置 ID、权限和看板视图，不包含商品名称、配置值或凭据。复制操作不申请授权；AI 阅读规则后按需要申请，由用户本人核对设备与范围并批准。已绑定设备继续复用，不重复绑定。

以下以 `https://extore.lmm.best` 为例；自建站点替换链接中的站点，ID 和目标替换为实际值。

## 商品处理人员或机器人

```text
请按[通用规则](https://extore.lmm.best/AGENTS.md)使用 Extore CLI 管理商品 `PRODUCT_ID`（权限：`queue.view,queue.process,queue.retry`）。
```

店铺流水线改为指定店铺 ID，只覆盖当前批准的商品集合；既有商品链接注明 `--existing-link`，绑定次数耗尽时注明复用已绑定的 CLI 设备。

## 只读进度看板

```text
请按[通用规则](https://extore.lmm.best/AGENTS.md)使用 Extore CLI 只查看商品 `PRODUCT_ID` 的 `active` 进度看板（权限：`queue.monitor`）。
```

## 店主配置与运营

```text
请按[通用规则](https://extore.lmm.best/AGENTS.md)使用 Extore CLI 在店主授权范围内完成我交代的店铺管理工作。
```

## 商品处理器配置

```text
请按[通用规则](https://extore.lmm.best/AGENTS.md)使用 Extore CLI 按我的要求配置店铺 `SHOP_ID` 的处理器 `PROCESSOR_ID`（配置 `PROFILE_ID`）。
```

## 帮顾客领取

```text
请按[通用规则](https://extore.lmm.best/AGENTS.md)使用 Extore 顾客 CLI 完成我交代的兑换和领取操作。
```

卡密、管理链接和领取链接另行通过私密标准输入提供，不放进公开规则或复制的提示词。

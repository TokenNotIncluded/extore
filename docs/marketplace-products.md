# 商城规格与交付模板

Extore 的处理方式（人工队列、Webhook、处理器、一卡一文本）与商城交付模板是两个维度。
模板描述商城交给买家的内容，不会改变 Extore 的兑换流程，不会自动发行卡密、补货或收款。

## 配置入口

在商品编辑页的每个规格下展开「商城规格配置」，可配置：

- `marketplace.template`：`card-key`（激活卡密）、`text`（文本）、
  `custom-text`（自定义文本）、`fixed-content`（固定内容）、
  `redemption-code`（兑换码）、`license-key`（许可证）、
  `download-link`（下载链接）、`account-details`（账号资料）。
- `marketplace.price_quota`：整数 Credits，范围 `500000..9007199254740991`，
  或 `null`（未配置）。不会从参考价或币种猜测、换算实际售价。
- `marketplace.fixed_content`：仅固定内容模板可填写；必须非空，最多 16000 UTF-8
  字节。切换到其他模板前须主动清空正文，界面不会悄悄丢弃内容。

模板名称对齐 `api.lmm.best-microservices/apps/web/src/features/store/delivery-template.ts`。
价格下限和固定正文大小按当前微服务
`services/extensions/internal/modules/store/legacy_catalog.go` 的约束验证。

## 权限与私密数据

管理接口只向具有 `fulfillment.configure` 的商品管理人员提供这些设置。
仅有 `product.edit` 的人员可继续修改展示信息，但读不到商城交付配置，不能修改它；
保存经过隐藏的配置也不会清除已有正文。店铺和商品范围继续使用原管理权限检查。

这些数据保存在商品配置内，不进入公开商品、普通商品导出、
`products.read` 商城目录或发行卡密的规格快照。
未使用商城配置的旧规格保持原有序列化格式；无需数据库结构迁移。
这不是凭据保险箱，不应在正文之外存放店铺 API 密钥等管理凭据。

## 当前接入边界

本轮只完成 Extore 的规格配置及安全边界，**没有新增私密同步接口**。
普通目录的 revision 不包含私密商城配置，不能用它判断私密配置是否更新。

2026-10-15 核对的微服务工作区中，商城 Web 声明了上述 8 种模板，
但 Go `validateLegacyVariant` 目前只允许新建 `fixed-content`；其他模板的有限库存交付
仍受后端能力限制。选中模板不代表目标商城已经支持导入、交易或交付。
不得静默降级模板、把固定正文放进公开目录，或绕过后端的不可用响应。

旧 `feat/shop-binding-20261012` 的授权与同步候选仍独立保留；未将其未验收代码带回 main。
后续应在微服务接入端完成能力协商、独立同步授权、私密配置 revision、稳定规格映射，
以及真实多店铺导入和重复同步验收后，才可称为端到端接通。
Extore main 会自动部署，本轮改动仅推送开发分支，不部署。

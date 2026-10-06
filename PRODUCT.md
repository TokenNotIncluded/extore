# Extore · 兑所
<!-- impeccable:product-schema 1 -->

## Platform
web

## Stack
暂定 Python FastAPI、SQLite、原生 HTML/CSS/JavaScript；根据脚本 SDK 的需求选择，待用户指定的部署环境可调整。

## Users
单个商家、按商品授权的处理员工，以及持有卡密的顾客。顾客无需注册。

## Product Purpose
顾客输入卡密、填写商品参数，完成一次兑换，领取交付内容或查看服务状态。支付、外部订单和支付平台交付职责归另一网站。

## Operating Context
人工排队、Python 自动处理、签名 webhook / 回调。商家通过服务器安装脚本，员工通过限商品、可撤销、有期限的链接处理任务。

## Capabilities and Constraints
SQLite。安全随机且唯一的卡密、一次兑换、可选重复查看或一次查看、可核实后重试。商品可公开或仅持卡可见；参数支持 i18n 名称与 Markdown 教程。仅 Passkey 管理员登录，首次密码引导注册，SSH 命令恢复。
销毁立即撤除应用内交付内容和相关参数；无法撤回他人已下载的内容。无全系统员工权限。

## Brand Commitments
暂无既有品牌。工作名称 Extore · 兑所，是项目初始化采用的命名。

## Product Principles
- 兑换一次，重试不等于重新发货。
- 成功必须有可信处理结果，未知状态等待核实。
- 非公开商品只向持有效卡密的顾客展示。
- 顾客专注领取，员工专注处理，商家管理全部。

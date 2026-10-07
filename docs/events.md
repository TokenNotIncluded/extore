# 事件接入

完整事件结构、签名、投递和回调约束见[接口与事件协议](protocol.md#webhook-事件)。

外部处理端只用 `redemption.requested` 和 `revision.requested` 启动工作，其他事件是进度或结果通知。`revision.requested` 表示顾客已成功使用本卡的修改权益；它保留原 `params`，另带 `revision.message`，不将建议混入顾客原始字段。先验签、持久化事件 ID 去重，再入本地执行队列。

同一个任务 ID 会经历多次技术尝试和交付版本：用 `(job_id,attempt)` 区分执行与回调，用 `(job_id,revision.current)` 区分内容交付版本。技术重试不会生成新内容版本，旧 attempt 不能更新新稿。付款等整单一次的副作用仍按原任务 ID 去重，不能因收到修改事件再次付款。

事件保留权益、当前轮次及 `last_delivery` 最近成功版指针，全部省略完整 `deliveries` 历史列表。只有 `redemption.requested` 和 `revision.requested` 两个启动事件携带完整 `card_attributes` 与 `revision.message`；进度、完成等通知的 `revision` 只有 `current/is_revision`，不重复传送长建议，也不携带历史交付正文或文件内容。需要完整历史元数据时读取授权的 receipt 或 job 详情。`fulfillment.succeeded` 每轮交付均会产生；历史版的实际领取需顾客凭证与版本校验，不能将事件当作下载授权。

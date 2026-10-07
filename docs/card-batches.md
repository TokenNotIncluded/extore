# 卡密批次文件夹

卡密页先按发行批次显示文件夹，列出总数、未兑换数、已使用数与处理中数量。每次生成卡密或导入交付文本，形成一个批次。没有批次信息的旧卡密归入「历史卡密」。打开文件夹后才请求这一批卡密的使用情况，返回批次不会平铺全商品的卡密。

规格与批次搜索在文件夹列表生效；打开文件夹后，可按状态、卡密内部 ID 或尾号查找。批次页和卡密页分别分页，始终限定当前商品。页面不恢复原始卡密，不显示顾客参数或交付内容；原文复制与下载仍限于本次发行结果。

商品统计包含已归档但需要保留的任务卡密，文件夹数量则只反映当前选定视图。移入回收站或清理文件夹，不会把已经存在的交付从商品统计中抹掉。

选择「删除批次」后，页面先显示服务器计算的停止兑换数量、保留记录数量和正在处理数量。确认后移入回收站，停止未开始或可重试卡密的兑换，保留正在处理的任务、交付与使用记录。删除预览携带修订值；预览后批次发生变化时，需要重新预览，不能套用旧数量。

「回收站」可以恢复批次，也可以选择「永久清理」。永久清理同样先显示预览，只删除没有任务记录的卡密；已有任务与交付继续保留。永久清理不可恢复。文件夹从列表移除不代表已交付内容被销毁，顾客主动销毁仍使用原领取页面的独立操作。

商品管理者需要 `cards.manage` 权限。删除、恢复与清理均限定当前商品与选中的整批卡密，不影响其他商品或批次。规格筛选只缩小展示范围，不会拆分批次；删除预览始终显示整批数量。混合规格的历史批次也是整批处理。

## CLI

店主用 `extore admin`，商品管理授权用 `extore manage`；以下把 `manage` 换成 `admin` 也能执行。`--product` 始终指定一个商品。文件夹列表一次最多 100 个批次，默认 50 个。

```sh
extore manage cards batches --product PRODUCT_ID
extore manage cards batches --product PRODUCT_ID --view deleted
extore manage cards batch --product PRODUCT_ID --batch BATCH_ID

# 先显示删除预览，然后把返回的 revision 原文填入确认命令。
extore manage cards batch-delete --product PRODUCT_ID --batch BATCH_ID
extore manage cards batch-delete --product PRODUCT_ID --batch BATCH_ID --revision REVISION --yes

extore manage cards batch-restore --product PRODUCT_ID --batch BATCH_ID --yes

# 永久清理也必须使用自己的一次预览，不能沿用删除 revision。
extore manage cards batch-purge --product PRODUCT_ID --batch BATCH_ID
extore manage cards batch-purge --product PRODUCT_ID --batch BATCH_ID --revision REVISION --yes
```

`legacy` 是没有原批次信息的卡密的稳定文件夹 ID，限定一个商品。第一次删除它会保存为一个真实历史批次，恢复后也保留该批次 ID。永久清理后仍有任务的卡密保留必要记录，但不会重新冒到「历史卡密」文件夹里。

## 接口与清理边界

浏览器和 CLI 使用相同的权限检查，`/api/admin` 与 `/api/manage` 各提供一套相同路径。每次操作必须提供当前 `product_id`，商品管理链接还会再次核对它自己的授权商品。

| 方法 | 路径（去掉前缀） | 用途 |
| --- | --- | --- |
| GET | `/card-batches` | 按 `view=active/deleted`、规格、标签或卡密 ID/尾号筛选文件夹 |
| POST | `/card-batches/{id}/delete-preview` | 删除预览，返回整批数量与 revision |
| DELETE | `/card-batches/{id}` | JSON `{ "confirmed": true, "revision": "…" }` 移入回收站 |
| POST | `/card-batches/{id}/restore` | 恢复批次，只恢复本次删除停用的卡密 |
| POST | `/card-batches/{id}/purge-preview` | 已删除批次的永久清理预览 |
| POST | `/card-batches/{id}/purge` | JSON `{ "confirmed": true, "revision": "…" }` 永久清理 |

预览 revision 绑定商品、批次、操作、卡密及任务状态、附件元数据；任务进展或附件变化会让旧预览失效。确认和变更在同一个 SQLite 写事务里完成。店主 CLI 的一次性签名覆盖最终路径、查询和实际 JSON，写入失败不会自动重放。

删除可恢复，不提前抹去输入、文本库存或交付。永久清理删除没有任务记录的卡密及其预上传附件、库存文本和失效凭证；有关联任务的卡密、顾客领取凭证和交付继续保留。`cards.batch.delete`、`cards.batch.restore`、`cards.batch.purge` 写入所属店铺的审计记录。清理使用 SQLite 的 `secure_delete`，释放逻辑额度；数据库文件体积由正常 SQLite 页面复用和维护管理。

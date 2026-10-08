# 开放商城导入示例

这是客户端接入示例，不是第二个商城，也不会配置实际店铺或购买商品。完整契约见[商城授权与商品导入协议](../../docs/commerce-import-protocol.md)。

## 不联网跑通 SDK

```sh
uv run python examples/commerce_import/mock_roundtrip.py
```

脚本用 `httpx.MockTransport` 模拟预登记客户端、商家批准后的回调和 Extore 的机器接口，依次验证：服务器发现 → S256 授权事务 → 校验 state/iss/回调地址 → 令牌交换 → 商品读取 → 按规格补货 → 同键响应恢复 → 令牌轮换 → 撤销。

商品、域名、令牌、卡密都是程序内生成的虚构数据。终端只输出步骤与数量，不输出凭据、卡密或授权 URL。模拟服务器故意只实现这条成功路径，**不是可以部署的 OAuth 服务**；真实服务器的授权、重放、跨店铺校验和并发事务由 Extore 实现并测试。

## 对接真实商城后端

接入按钮的后端需要以下持久记录。把它们纳入现有商城的商家身份和数据库，不另建匿名授权入口：

| 记录 | 必需内容 |
| --- | --- |
| 授权事务 | 当前商城商家 ID、可信 Extore issuer、预登记 client_id、state、verifier、redirect_uri、申请 scope、事务有效期、一次性消费状态 |
| 连接 | 商城商家 ID、issuer、shop_id、grant_id、实际批准 scope、grant_expires、加密 access／refresh token、连接级刷新锁 |
| 导入映射 | `(connection_id, Extore product_id, Extore variant_id)`、商城商品／SKU ID、最近 revision、商家确认的实际售价 |
| 补货请求 | connection_id、唯一 Idempotency-Key、原 JSON 正文、pending／saved／needs_review、批次 ID、恢复截止时间 |
| 卡密库存 | 加密完整卡密、批次 ID、外部规格 ID、商城销售／订单分配状态；严格唯一约束和只分配一次 |

可将 SDK 回调接到 Flask、Django、FastAPI、Rails 或其他后端。回调的第一步是核对**当前登录的商城商家**与原事务，之后才能调用 `complete_authorization`；不能根据回调里未经验证的 shop_id 自动绑定账户。交易成功后清除一次性会话、跳到不含 code 的普通连接页面。禁止在回调加载第三方分析脚本。

刷新时在数据库的连接锁内读取最新令牌、调用 `refresh`、原子保存新令牌。SDK 的线程锁只保护一个 Python client 实例。刷新响应丢失要重新授权，不能拿旧 refresh token 重试。

补货之前先把请求存入商城数据库；返回后将响应加密保存和新卡密入库放在一个事务内。恢复只发送相同键与正文；重复响应不能再次增加库存。收款、订单与卡密发给买家由商城负责，不由这个示例虚构。

## 使用 curl 核对机器接口

发现文档不含凭据，可以直接请求：

```sh
curl --proto '=https' --max-redirs 0 --fail-with-body \
  https://YOUR_EXTORE_ORIGIN/.well-known/oauth-authorization-server
```

有秘密的请求使用权限 `0600` 的文件，不把 token、code、verifier 作为命令行参数。先在私人目录中准备真实值；下面内容只有占位符，不能直接使用：

```sh
umask 077
mkdir -m 700 extore-private
cd extore-private
```

`token-request.txt`（URL 编码 form body）：

```text
grant_type=authorization_code&client_id=YOUR_CLIENT_ID&code=REPLACE_LOCALLY&redirect_uri=URL_ENCODED_REGISTERED_CALLBACK&code_verifier=REPLACE_LOCALLY
```

在本地编辑器填写，确认文件仅本人可读，然后：

```sh
curl --proto '=https' --max-redirs 0 --fail-with-body --silent --show-error \
  --header 'Content-Type: application/x-www-form-urlencoded' \
  --data-binary @token-request.txt \
  --output tokens.json \
  https://YOUR_EXTORE_ORIGIN/api/integrations/commerce/token
```

`private-curl.conf` 由本地编辑器填写，内容是 `header = "Authorization: Bearer REPLACE_LOCALLY"`，同样仅本人可读。不要在终端用 `cat`、`echo` 或 shell 变量展开真实令牌：

```sh
curl --proto '=https' --max-redirs 0 --fail-with-body --silent --show-error \
  --config private-curl.conf --output products.json \
  https://YOUR_EXTORE_ORIGIN/api/integrations/commerce/products
```

补货的 JSON 见协议文档。先将原正文和幂等键保存到商城数据库，再手工核对：

```sh
curl --proto '=https' --max-redirs 0 --fail-with-body --silent --show-error \
  --config private-curl.conf --header 'Content-Type: application/json' \
  --header 'Idempotency-Key: YOUR_SAVED_UNIQUE_REQUEST_ID' \
  --data-binary @saved-card-request.json --output confidential-card-batch.json \
  https://YOUR_EXTORE_ORIGIN/api/integrations/commerce/cards
```

不要使用 `curl -v`、`--trace`、`-L` 或将这些机密文件上传到 issue。curl 示例只适合有经验的接入者核对；它不会替你做 callback state／issuer 校验、刷新锁、入库事务、文件不覆盖保护或敏感内容加密。一般接入优先使用 SDK 或机器 CLI。

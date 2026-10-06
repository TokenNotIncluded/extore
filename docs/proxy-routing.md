# 签名卡密代理路由

代理站 A 只提供本地选择入口。卡密由发行站 B 签名；顾客粘贴后，浏览器用 A 管理者已经确认并固定的公钥验签，按发行站分组。顾客点击某一组后，只将该组转交给对应 B。A 不接收、记录或消费这些签名卡密。

未包含发行信息的旧卡密仍只在当前站验证，不会逐站尝试。代理卡密不会用于查询所有下游站点。

## 配置与发行

每个店铺可以创建发行身份。Ed25519 私钥使用店铺密钥加密保存，并绑定身份 ID；接口不返回私钥。发行站配置指向本站 `EXTORE_ORIGIN` 的本地路由，路径为 `/`。设置 `default_issuer: true` 后，新的发行请求默认返回签名卡密。

代理站导入发行站的公开配置，固定以下六个字段：

```json
{
  "route_id": "32 位小写十六进制标识",
  "issuer_id": "32 位小写十六进制标识",
  "name": "领取站名称",
  "origin": "https://issuer.example.com",
  "path": "/",
  "public_key": "32 字节 Ed25519 公钥的无填充 base64url，43 字符"
}
```

先通过可信渠道核对发行商与公钥，再导入。系统不会从卡密内的地址或公钥建立信任。

路由只支持固定 HTTPS 公网域名，不支持 IP 地址、账号信息、查询串或 fragment。创建时检查全部 DNS 解析结果均为公网地址。路径只允许 `/`、ASCII 字母、数字、下划线与连字符，并禁止 `//`。目标、发行身份、公钥和路由 ID 建立后不可修改；换地址或密钥应创建新路由。

同一 `route_id` 可以由不同店铺独立绑定，但所有绑定的 `issuer_id`、`public_key`、`origin`、`path` 必须相同。每家店独立启停；公开元数据按路由 ID 去重，只返回启用店铺的启用绑定。停用本地发行路由也会阻止其已发行签名卡密兑换；如需保留旧库存，只切换默认发行路由，保留旧路由启用。

发行请求 `/api/admin/cards`、`/api/manage/cards`、`/api/integrations/cards` 支持 `routed`：

- 省略或 `null`：有默认本地发行路由则签名，否则保持旧格式。
- `true`：必须存在可用的默认本地发行路由，否则返回 409。
- `false`：明确发行旧格式卡密。

已配置但停用的默认发行路由不会静默退回旧格式。库存依然存储原始卡密的摘要，商品、规格、任务和卡密使用统计保持原有规则。集成发行的幂等返回值继续加密保存。

## 管理 API

接口仅接受店铺管理者或超级管理员会话。商品管理链接不能读写发行身份和路由配置；具备卡密管理权限的链接仍可发行对应商品的卡密。CLI 写操作继续要求设备签名，浏览器写操作要求同源校验。

| 接口 | 内容 |
| --- | --- |
| `GET /api/proxy/routes` | 无需登录的六字段公开路由列表；请求不包含卡密或卡密摘要 |
| `GET /api/admin/proxy/identities?shop_id=…` | 本店身份列表，只含 ID、名称、公钥、创建时间与店铺 ID |
| `POST /api/admin/proxy/identities` | `{name, shop_id?}` 创建加密发行身份 |
| `GET /api/admin/proxy/routes?shop_id=…` | 本店绑定列表，含启用、默认发行状态 |
| `POST /api/admin/proxy/routes` | 本地发行填写 `identity_id`；外部路由填写固定六字段；可选 `shop_id`、`default_issuer` |
| `PUT /api/admin/proxy/routes/{route_id}?shop_id=…` | 只更新 `name`、`enabled`、`default_issuer` |

店主省略 `shop_id` 时只使用自己的店铺。超级管理员省略时使用默认店铺；管理其他店铺应明确传入 `shop_id`。管理端返回值额外包含 `identity_id`、`shop_id`、`kind`、启停及时间信息；共享给代理站时只复制公开六字段。

## 编码与验签

无空白、无填充的传输格式：

```text
EXR1.<route_id:32小写hex>.<secret:32大写base32>.<issuer_id:32小写hex>.<signature:86字符base64url>
```

`secret` 为原卡密的 160 位随机值，去掉原有分组连字符。签名为 64 字节 Ed25519 签名。待签名 ASCII 文本如下，最后一行之后没有换行：

```text
Extore routed code v1
<route_id>
<issuer_id>
<origin>
<path>
<原 secret ASCII 的 SHA-256，小写 hex>
```

Python 本地校验可使用 `extore.proxy_routes.verify_routed_code(code, route)`，成功返回原 `secret`，失败抛出不含卡密的 `ValueError`。`parse_routed_code` 严格校验编码；未知版本、异常大小写、修改的目标或签名都会被拒绝。

客户端先验证全部签名，再继续任何一组。转交固定 `origin + path + '#extore-code=' + encodeURIComponent(codes)`，不将卡密放进 URL 查询串或路径。B 在其他脚本执行前立即用 `history.replaceState` 清除 fragment，再用内存中的完整签名卡密调用本机兑换接口。B 再次验签，检查本机发行身份和卡密所属店铺，然后创建领取会话；验证本身不消耗卡密。

浏览器分组页面只显示站点、名称和数量，必须由顾客点击；不会自动打开多个窗口。完整签名卡密不写入 localStorage/sessionStorage、公开页面摘要或 WebMCP 的返回结果。给 AI 使用时，优先从私密文件通过客户 CLI 输入卡密，避免把卡密放进聊天记录或工具参数日志。

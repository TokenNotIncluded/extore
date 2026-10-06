# 配置兑换路由

普通随机卡密没有发行站信息，无法在不接收它的情况下判断来源。跨 Extore 路由需要发行站生成带路由标识和签名的新卡密；既有普通卡密保持原样，不试探多个下游站点。

商家后台的「兑换路由」只对店主开放。平台管理员先明确选择店铺，店主固定在自己的店铺范围。

在发行站 B：

1. 创建本店发行标识。服务器生成签名私钥，网页和 CLI 仅返回公开验证公钥。
2. 使用该标识创建指向本站的发行路由，地址固定为本站 HTTPS 地址，路径固定为 `/`。
3. 商家需要后续卡密默认携带路由标识时，明确设置为新卡默认路由。该操作不重写以前的卡密。
4. 复制该路由的六项公开配置，交给入口站 A 的管理者。

在入口站 A：

1. 核对 B 的地址、发行标识和公钥。
2. 粘贴六项公开 JSON，导入下游路由。配置包含 `route_id`、`issuer_id`、`name`、`origin`、`path` 和 `public_key`。
3. 顾客浏览器本地校验签名，将完整卡密直接交给固定的 B。A 的后端不接收完整卡密。

目标、公钥与标识不能覆盖修改。切换发行站需要新路由；停用保留原始配置与卡密，重新启用恢复原来的固定目标。多个店铺可以分别绑定同一条、相同公开配置的路由，不能为相同路由标识指定冲突目标或公钥。

## CLI

使用店主设备授权登录。首次登录由店主在浏览器核对设备码、指纹与权限后批准；不复制浏览器 Cookie，不把商品队列授权当作店主权限。平台管理员在下面每条操作中加 `--shop SHOP_ID`；店主可以省略，始终固定在自己的店铺。

```text
extore admin proxy identities list
extore admin proxy identities create --name "本店发行"
extore admin proxy routes create --name "本站兑换" --identity IDENTITY_ID
extore admin proxy routes default ROUTE_ID
extore admin proxy routes list
extore admin proxy routes export ROUTE_ID --output route-public.json
extore admin proxy routes import --json-file route-public.json
extore admin proxy routes disable ROUTE_ID
extore admin proxy routes enable ROUTE_ID
extore admin proxy routes default ROUTE_ID --clear
```

`export` 只输出六项公开字段，导出的 JSON 可以由另一个 Extore 商家的 `import` 使用。默认发行设置与启用状态属于本店配置，不包含在公开导出里。导入不接受私钥、凭据或任意额外字段。

后台提供「复制给 AI 的路由配置提示词」，其中只有公开站点与店铺资料，没有授权凭据。让 AI 先读取 CLI 帮助和现有配置，再按照商家的具体要求操作。

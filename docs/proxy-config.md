# 兑换路由配置

顾客只有一个兑换入口：Extore 首页。卡密包含发行站签名时，浏览器先读取公开路由、在本地验签，再按固定目标继续领取。完整下游卡密不会进入入口站的服务器。旧 `/proxy` 地址以 308 返回首页，浏览器保留私密片段；不会把卡密改放到查询参数。

## 每店一个当前标识

店铺创建时自动拥有 `main` 发行标识。HTTPS 站点同时拥有指向本站首页的默认发行路由，新卡必须带本店签名；商家无需先创建标识或勾选开关。本地 HTTP 开发仍可使用普通卡密，不发布 HTTPS 路由配置。

后台“兑换路由”显示当前标识、当前路由。旧版本已经使用的标识和固定公钥会保留；当前标识在界面统一显示为 `main`。替换会生成新的密钥和新的本地路由，之后发行的卡使用新标识。不会改写旧卡的公钥、目标、路径或签名。

签名私钥加密保存在该店铺的服务器配置中，网页、CLI 和导出的公开配置均不包含私钥。

## 导入下游

向下游发行商获取这六个公开字段：`route_id`、`issuer_id`、`name`、`origin`、`path`、`public_key`。核对固定的 HTTPS 公网目标和公钥，再在后台导入。一个路由 ID 对应的签名身份、目标和公钥不可覆盖。

同一公开路由可以由多个店铺分别绑定；启用和停用仅作用于该店铺的绑定。导入的外部路由不能成为本店发行默认。

## 清理历史

历史标识和停用路由收在“历史与清理”中，支持逐项清理和一键清理未引用配置。先展示清理预览，服务器在实际删除的同一事务中再次检查，避免预览期间刚发行的新卡失去公钥。

当前标识和默认发行路由必须保留。旧标识仍保护可兑换、可重试或可重复领取的卡密时也必须保留。已销毁、已拒绝、不可再兑换的过期卡密，以及不可恢复的已撤销卡密不会无限占用清理历史；回收站中仍可恢复的卡密保留公钥，直到批次永久清空。订单和领取记录本身不会随配置清理被删除。

新版会记录每张卡实际使用的发行路由，不保存明文卡密。旧版本没有签发引用记录的卡密会保守地保留创建时间范围内可能使用的公钥。可在卡密批次中删除不再需要的卡密，再清理其历史发行配置。

停用历史路由会暂时阻止对应旧卡从本站兑换或跳转。永久删除下游绑定后，顾客需要直接前往发行站。删除无法恢复；清理不会改变另一店铺绑定的同一路由。

## CLI

店主管理权限与商品处理权限分开。首次绑定使用 `extore admin login` 的设备码，让店主在浏览器核对并批准。平台管理员必须明确传 `--shop SHOP_ID`；商家设备只能操作自己的授权店铺。

```sh
extore admin proxy identities list
extore admin proxy identities replace --name main
extore admin proxy identities list --history
extore admin proxy routes list
extore admin proxy routes list --history
extore admin proxy routes export ROUTE_ID --output route-public.json
extore admin proxy routes import --json-file route-public.json
extore admin proxy routes disable ROUTE_ID
extore admin proxy routes enable ROUTE_ID
extore admin proxy identities cleanup-preview IDENTITY_ID
extore admin proxy identities delete IDENTITY_ID --yes
extore admin proxy routes cleanup-preview ROUTE_ID
extore admin proxy routes delete ROUTE_ID --yes
extore admin proxy cleanup-preview
extore admin proxy cleanup --yes
```

旧 `identities create` 命令保留为替换当前标识的兼容入口。旧 `routes default --clear` 命令会在发送请求前被拒绝，因为店铺不能没有当前发行路由。清理 API 和 CLI 只返回公开标识和计数，不返回私钥。

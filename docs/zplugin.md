# Extore 原生商城插件

Extore 现在提供 Rust library 和 Project Z 原生动态库。宿主通过共享接口读取已有商城授权允许的商品目录，目录内容、权限判断和 revision 计算继续由 Extore 原有实现负责。HTTP 服务、worker 和插件共同使用同一套 Rust 业务代码。

这次接入的业务范围是商城目录读取。OAuth 注册、PKCE、商家确认、令牌刷新和撤销继续使用现有 HTTP 接口；发卡、支付、Core 身份和资金账本仍归原有业务边界管理。Python processor SDK/runner 仍是兼容执行层，见 [Rust 迁移说明](rust-migration.md)。

## 代码分工

| 组件 | 职责 |
| --- | --- |
| `extore-plugin-api` | 冻结的 trait、配置及响应类型；宿主只依赖此 crate，无需链接 Extore 数据库业务 |
| `extore` library | 复用原 HTTP dispatch、事务、授权、目录投影和状态校验 |
| `extore-zplugin` | 导出 `zplugin_entry_v1`，持有一个配置后的 Extore 实例 |
| 宿主 | 校验可信产物、管理进程及调用线程、检查配置 origin 和收到的商城契约 |

LMM 应用依赖应固定到 Extore 仓库的不可变 Git revision，并锁定同一版 `zplugin`。一次构建的动态库 SHA256 也应记录到宿主配置或发布清单中。共享 API 和实际动态库必须来自已审阅、对应的源码。

## Project Z 版本和 ABI

本实现使用已发布的 `zplugin = "=0.3.1"`，其原生加载器使用 `zsys = "=0.1.1"`。zplugin 0.3.1 声明的最低 Rust 版本为 1.95；此次构建使用 Rust 1.100.0。0.3.1 的包 VCS 与上游 tag 均为 `00d566eedcff7b2932e90b9dfeb44910c783d305`；此发布的冻结 ABI 定义和文档与 0.3.0 相同，加载器改为复用 zsys。

| 包 | 锁定版本 | registry SHA256 |
| --- | --- | --- |
| zplugin | 0.3.1 | `094cd57c6b7759cbbf7834e96aed75c9d025558a7d2cc4b742e78b33f8e7a448` |
| zplugin-macros | 0.3.1 | `f39a8e67a3405ad54b2780d74360dabdbbcaa6f33fb44474e13433c0b34ca395` |
| zsys | 0.1.1 | `929d282f08df7fbe2456414924e126a29dfde04e93eb2515721b596404610548` |

上游依据：[0.3.1 源码](https://github.com/LIghtJUNction/zplugin/tree/00d566eedcff7b2932e90b9dfeb44910c783d305)、[冻结 ABI](https://github.com/LIghtJUNction/zplugin/blob/00d566eedcff7b2932e90b9dfeb44910c783d305/docs/abi.md)。本仓库验证目标为 Linux GNU。静态 musl 没有可用的原生动态加载后端。

应用接口 ID 为 `extore.commerce`，接口版本为 1。方法名称、顺序、接收者、参数和返回值属于该版本的合同；修改这些内容需要新的接口版本。JSON 配置和响应另有各自的 schema 版本。

## 配置与数据所有权

宿主把 `extore_plugin_api::Config` 序列化后调用 `configure`：

```json
{
  "schema_version": 1,
  "data_dir": "/srv/extore/data",
  "origin": "https://extore.example.com"
}
```

`data_dir` 必须是运维配置的绝对路径，指向独立、已初始化的 Extore 状态目录。`origin` 必须与该 Extore HTTP 服务的发行者一致，不带尾部斜杠；正式环境使用 HTTPS，本机开发允许 `localhost` 和 `127.0.0.1` 的 HTTP。浏览器请求不能决定这两个值，也不能传入 Core 数据库地址。

配置会校验现有 schema、主密钥与数据库指纹、发行记录密钥。配置和目录调用都通过 `App::existing_db` 打开数据库，SQLite flags 不含 `CREATE`。状态验证使用已经打开的同一个连接，所以检查期间数据库文件被移走也不会补建空库。插件不会执行初始化或迁移。

一个实例成功配置后不能切换商店目录或 origin。配置失败时可以更正配置重试；成功后重配返回 `ALREADY_CONFIGURED`。要更换状态目录，应由宿主建立新实例。

目录调用每次重新打开数据库并查询令牌、grant、client、店铺和商品状态。它没有授权缓存，因此 HTTP 撤销提交之后，同一个已配置实例的下一次读取会看到撤销结果。与原 HTTP 实现一致，读取会更新授权的 `last_used`，因此数据库连接仍需读写权限。

## 调用和响应

`catalog` 只接收现有商城 access token，并执行固定的 `GET /api/integrations/commerce/products` 业务路径。它复用原始 scope 检查、商品批准范围、店铺归属、私密字段投影、价格文本和 revision 计算。

返回的字节使用共享 `Response` 类型解析：

```json
{
  "schema": "extore.plugin-response.v1",
  "status": 200,
  "headers": {
    "cache-control": "no-store",
    "content-type": "application/json; charset=utf-8"
  },
  "body": {
    "schema": "extore.commerce-catalog.v1"
  }
}
```

这里的 `body` 仅展示 schema 字段。实际成功响应保留原 HTTP 目录的 issuer、shop、grant_id 和 products；鉴权失败保留原 HTTP 状态、错误 JSON 与 `WWW-Authenticate`。宿主需要接受并保留 `cache-control: no-store`。宿主仍应校验 issuer、grant_id、店铺绑定及商品契约。

配置最多 16 KiB，返回的完整响应 envelope 最多 16 MiB。价格保持原字符串，避免在跨语言传输中改变小数精度。令牌和私密商品配置不会进入响应。

业务鉴权失败通过响应的 HTTP 状态表达，例如 401、403、409。接口或实例错误使用 zplugin 的状态返回：

| 状态码 | 含义 |
| --- | --- |
| 1001 | 尚未成功配置实例 |
| 1002 | 配置格式、版本、路径或 origin 无效 |
| 1003 | 状态不可用，或状态/密钥校验失败 |
| 1004 | 返回内容超过大小限制 |
| 1005 | 已成功配置，不能再次配置 |

下面的宿主示例只依赖共享 API 和 serde_json：

```rust
use extore_plugin_api::{CommerceApi, Config, Response, RESPONSE_SCHEMA, zplugin};
use std::path::Path;
use zplugin::{Plugin, UnloadPolicy};

fn read_catalog(
    artifact: &Path,
    config: &Config,
    access_token: &str,
) -> Result<Response, Box<dyn std::error::Error>> {
    // SAFETY: caller has verified this trusted Extore artifact and its dependencies
    // for the host's target ABI; the path is operator-controlled.
    let library = unsafe { Plugin::load_path(artifact, UnloadPolicy::Pinned)? };
    let mut commerce = CommerceApi::create(&library)?;
    commerce.configure(&serde_json::to_vec(config)?)?;
    let buffer = commerce.catalog(access_token)?;
    let response: Response = serde_json::from_slice(&buffer)?;
    if response.schema != RESPONSE_SCHEMA {
        return Err("unsupported Extore response schema".into());
    }
    Ok(response)
}
```

运行中的宿主应复用已配置实例。zplugin 的原生 Plugin、proxy 和 Buffer 有线程亲和性；创建、调用和释放都留在同一 OS 线程。返回内存由插件的释放回调处理，宿主不接管其分配器。

原生动态库是可信代码，拥有宿主进程的权限；ABI 检查用于拒绝不兼容接口。此实现使用 Pinned 策略，映射保留到进程退出。宿主应只加载审阅过的构建产物，并通过独立进程、私有通信端点和固定服务端配置管理其访问边界。

## 构建和实际加载验证

在仓库根目录执行：

```sh
export CARGO_BUILD_JOBS=1 CARGO_PROFILE_DEV_DEBUG=0 CARGO_INCREMENTAL=0
cargo build --locked -p extore -p extore-zplugin
cargo build --locked -p extore-plugin-api --example native_probe
python3 scripts/test-native-plugin.py \
  --binary target/debug/extore \
  --plugin target/debug/libextore_zplugin.so \
  --probe target/debug/examples/native_probe \
  --receipt out/native-plugin-verification.json
```

验证程序分别启动已构建的 Rust HTTP 服务和独立宿主，通过动态库入口调用真实业务。临时 fixture 只预置店铺、商品和近期认证的管理员会话，随后通过真实 HTTP 接口完成客户端注册、S256 PKCE、授权确认、令牌交换和撤销。临时状态和日志在退出时清理，不使用生产数据。

验收覆盖接口版本/签名拒绝，HTTP 与插件目录一致性，两家店铺的授权范围，价格和私密字段，revision 变化，scope 和令牌过期，商品换店后的拒绝，同实例在 HTTP 撤销后的拒绝，以及配置/调用不补建已移除数据库。它也检查旧 schema 不被自动迁移、主密钥指纹校验仍然生效。

[验证回执](verification/native-plugin.json) 记录实际产物哈希和通过的检查；[输入清单](verification/native-plugin-inputs.json) 固定对应源码内容。这组验证覆盖原生调用和真实 HTTP 授权链，不覆盖浏览器认证界面，也不代表已部署到生产。

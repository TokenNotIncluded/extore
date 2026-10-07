# 贡献自己的商品处理器

商家可以开发并开源自己的处理器，向 [TokenNotIncluded/extore-processors](https://github.com/TokenNotIncluded/extore-processors) 提交 PR。网站继续只运行发布版本内的固定目录：填写 Git URL、创建 Fork、Issue 或 PR 都不会让服务器安装或执行代码。

完整开发说明、字段契约、测试清单和给 AI 的贡献提示词见 [CONTRIBUTING.md](https://github.com/TokenNotIncluded/extore-processors/blob/main/CONTRIBUTING.md)；[提交 PR](https://github.com/TokenNotIncluded/extore-processors/compare)。

商品处理器配置页的贡献入口可以复制给 AI 的提示词。CLI 同样可以离线生成：

```sh
extore processors contribute --language zh-CN \
  --name '文本条目整理' \
  --summary '按明确规则整理有界文本，保留原文顺序' \
  --inputs '顾客粘贴文本；商家配置整理规则' \
  --outputs '整理结果和实际处理统计' \
  --output ./processor-proposal.json
```

它不登录、不联网、不读取店铺或商品配置；只把明确提供的需求作为 JSON 资料附在固定贡献提示词后。输出文件包含 `contribution`、`proposal` 和 `prompt`，新建 `0600` 文件且不覆盖，终端只给保存路径。也可以不传需求字段，直接生成通用提示词；`--language en` 返回英文版本。名称最多 120 字符，summary 最多 2000，inputs / outputs 各最多 1000；这些文本不会被拼成 Shell 命令或自动执行。

提示词的工作目标是让商家的 AI 完整实现、测试、补齐示例并向处理器仓库的 `main` 提交 PR，供维护者审核。复制提示词和打开 compare 链接本身不会代发 PR；贡献 AI 不应自行合并、发布或部署。

公开 `GET /api/processor-contributions` 提供 `extore.processor-contribution.v1` 静态契约：仓库、贡献指南、PR 链接、开发步骤、运行限制和双语提示词，不包含商家资料或凭据。它不是处理器安装接口。

流程是：Fork → 代码定义输入/输出/店铺配置 → 本地测试 → PR → 源码审核与合并 → Extore 更新固定子模块、验证并发布 → 商家可选。提交成功、CI 通过和合并都不等于已经上线。

## 最小开发起点

克隆处理器仓库后，在它的根目录生成独立草稿：

```sh
python tools/new_processor.py text_example --output ./text-example-draft
```

工具创建代码、测试、示例和 README，不自动注册、不执行生成代码。按生成目录的说明显式测试；提交时采用固定目录的静态注册，补齐实际 JSONL 入口验证。不要把真实顾客资料、店铺配置或密钥放进示例和 PR。

处理器代码负责声明 `parameters`、`outputs`、`configuration`、双语名称和教程，显式区分可回读的普通配置与只写秘密。Extore 按店铺隔离配置，并在发行卡密时固定版本；普通交付模板应标记 `secret: false`，真实令牌和含凭据的 URL 属于秘密。规格与完整上下文见 [Python SDK](python-sdk.md)；使用场景见 [处理器实用示例](processor-recipes.md)。

## 选择合适的执行方式

| 需求 | 建议方式 |
| --- | --- |
| 标准库可完成的有限文本、校验或数据处理，希望成为开源目录的一部分 | 贡献处理器 PR |
| 已有私人服务、不公开源码，需要联网、外部依赖或支付 | [签名私有 Worker](private-worker.md) |
| AI 自己运行、领取和处理一个或多个商品队列 | [设备码授权与队列 CLI](automation-cli.md) |
| 交付前需要再次提问、选项分支或短期验证码 | [处理流程开发](workflow-development.md)，再选以上执行者 |

当前固定处理器环境离线，限制文件创建、子程序和网络，不会为单个投稿安装任意依赖。它不是在线 Python 托管平台。需要网络或支付的业务应把副作用放在商家自己的 Worker 中，使用稳定业务幂等键；不要通过贡献代码绕过服务器限制。

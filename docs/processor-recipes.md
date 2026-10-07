# 商品处理器：四个实用场景

[快速入门](getting-started.md#自动处理与领取) · [配置档案与运行环境](shops.md#处理器配置档案) · [店主 CLI](cli-owner.md) · [处理器源码与协议](../processors/official/README.md)

这些商品接收粘贴的文本，在本机离线处理，再将可复制的文本、Markdown 或 JSON 交给顾客。输入、输出、教程与可配置选项来自处理器代码，商家不必再重复定义表单。创建商品、绑定配置和发行卡密之后，Extore worker 自动执行，无需 AI 领取队列任务。

| 场景 | 处理器 | 顾客填写 | 商家可配置 | 顾客领取 |
| --- | --- | --- | --- | --- |
| 实验记录的描述统计 | `csv_summary` | CSV 文本、数值列表头、可选分组列表头 | 无效数值拒绝或跳过 | 统计报告和可解析的 JSON 统计 |
| API 配置交接前的校验 | `json_formatter` | 完整 JSON | 缩进、对象键排序 | 格式化 JSON 和结构统计 |
| 项目条目或名单整理 | `text_cleanup` | 每行一条的文本 | 首尾空白、空行、去重方式 | 清理文本和实际删除数量 |
| 项目说明、受理记录或交付说明 | `document_template` | 标题、正文、可选称呼 | 本店模板、纯文本或 Markdown | 模板生成的文本和格式名称 |

另有 `resource_link` 用于交付固定 HTTPS 资源链接，`personalized_text` 用于按称呼生成短文本。以下四个场景不读取附件、不访问顾客填写的网址、不联网、不开启 AI 写作，也不生成 DOCX/PPTX 文件；复杂文档或文件制作可交给[外部 AI 队列](automation-cli.md)。

## 配置一个自动处理商品

以实验 CSV 统计为例。在商家后台的「商品处理器配置」创建本店配置，选择 `csv_summary`，将「无效数值处理」设为拒绝。新建商品，处理方式选择商品处理器，再选择这个处理器和配置档案。顾客表单、交付字段和教程按代码生成；发行卡密前确认配置已经绑定。

运行顺序为：本店配置 → 商品选择处理器 → 绑定配置 → 发行卡密 → 顾客填写 → worker 自动处理 → 显示进度与领取结果。店铺之间的配置相互隔离；不同商品可绑定本店同一个配置档案。

已安装并完成店主设备授权的 CLI 也可配置。先查看服务器实际提供的目录；如果目录没有某个处理器，该服务器还未安装对应版本，不能只通过填写 ID 启用：

```sh
extore admin processors --detail
```

以下命令在已授权的测试店铺执行，不需要服务器密钥。先将两份 JSON 保存为本地文件，按实际需要调整商品名称和描述；这些示例没有凭据。实际账号配置、卡密和授权结果需保存在私密文件中。

`csv-profile.json`：

```json
{
  "name": "实验统计：严格校验",
  "processor_id": "csv_summary",
  "configuration": {"invalid_policy": "reject"}
}
```

`csv-product.json`：

```json
{
  "name": "实验 CSV 数据统计",
  "description": "粘贴带表头的 CSV，选择数值列，可按组查看描述统计。只使用你提交的数据，不进行显著性检验或因果推断。",
  "public": false,
  "mode": "script",
  "processor_id": "csv_summary",
  "delivery": "content",
  "view_policy": "repeat"
}
```

```sh
# 本店配置输入要求是本人持有的普通私密文件，即使没有秘密值
chmod 600 ./csv-profile.json ./csv-product.json
extore admin processor-profiles create --json-file ./csv-profile.json
extore admin product create --json-file ./csv-product.json

# 用前两条命令实际返回的 ID 替换占位符
extore admin processor-profiles bind PROFILE_ID --product PRODUCT_ID
extore admin processor-profiles binding --product PRODUCT_ID
extore admin cards issue --product PRODUCT_ID --count 1 --output ./private-csv-cards.json
```

命令不会代替你登录。多台服务器或多个设备时，显式传 `--origin SERVER` 与 `--grant DEVICE_ID`；自定义凭据位置用 `extore admin --profile PATH …`。制卡输出自动写入新建的 0600 文件，不要把卡密放进 Git 或公开教程。按规格发行时增加 `--variant VARIANT_ID`。

API 与 worker 都须运行，worker 所在服务器须具备 Linux、bubblewrap 0.12 及以上、libseccomp 与可用的内核命名空间。发行后，顾客在网页输入新卡密、填写处理器表单并提交，领取链接显示两步真实进度和结果。处理器不会调用语言模型，因此不会产生模型 token 消耗。

配置修改会创建新修订，重新绑定后只影响之后发行的卡密；已发卡密与技术重试继续使用发行时的配置。撤销配置会阻止依赖它的执行。更完整的更新、工作流变量、秘密与资源限制操作见[配置档案](shops.md#处理器配置档案)。这四个处理器只使用各自声明的普通配置，不把工作流环境或秘密自动填入交付内容。

## 实验 CSV 统计

适合小批量实验记录或业务测量结果。粘贴带表头的英文逗号 CSV，填写完整数值列名；有试验组时再填分组列名。例子中的 `score` 为数值列，`group` 为分组列：

```csv
group,score
baseline,10
baseline,14
treatment,19
treatment,23
```

报告给出总体与分组的有效样本数、空值数、无效值数、均值、最小/最大值、总体和样本标准差。这个例子共有四个有效值，总体均值为 16.5；两组均值为 12 与 21。统计 JSON 的十进制结果用字符串表示，保留计算口径与排除数量，缺乏足够样本的统计值为 `null`。

默认 `invalid_policy=reject`：非法数值会拒绝处理。选择 `skip` 时非法数值被排除并明确计数；空值始终排除，不按零计算。结构错误和全部无有效数值不会被跳过。均值与标准差显示最多 12 位有效数字、使用半偶舍入，计算使用 200 位 Decimal 精度；这不是显著性检验，也不能从分组均值推出因果。

输入最多 10,000 字符、500 个数据行、30 列、50 组；包括空白记录在内最多 1,001 个 CSV 记录。列数必须一致，表头不能为空或重复，每个表头最多 100 字符、组名最多 200 字符。非零数值绝对值在 `1e-30` 至 `1e12` 之间，最多 30 位有效数字，指数绝对值不超过 30；货币符号、千位逗号、公式、NaN 和 Infinity 不是合法数值。分组字符串保留空白差异。

## JSON 配置校验

适合顾客粘贴 API 配置、项目元数据或需要交给下游程序的 JSON。`indent` 可选 `2`、`4`、`compact`，默认 `2`；`sort_keys` 可选 `yes`、`no`，默认 `no`。所有商家配置都是字符串，例如 `"indent": "2"`。

它严格校验一份完整 JSON，支持对象、数组和标量，保留数字原文与字符串内容；`12345678901234567890.123456789` 和 `1.20e-3` 不会经过二进制浮点转写。只在明确选择时调整对象键展示顺序，数组顺序保持不变。报告显示结构计数、深度和 UTF-8 大小，不摘录内容。

最多 10,000 字符、32 层容器、10,000 个值、单容器 2,000 项。单个数字最多 256 字符、128 位有效数字，指数绝对值不超过 1,000。拒绝重复键、非有限数字、孤立 Unicode 代理项、注释和尾逗号。格式化结果加报告最多 100,000 UTF-8 字节，缩进导致超限也会拒绝。顾客输入会成为交付 JSON，不应粘贴无需交付的密码、密钥或隐私信息。

## 文本与名单清理

适合实验条目、项目素材名单、字幕片段或重复资料行。默认去除每行首尾空白、删除空行，按完全相同的清理后文字去重。`deduplicate=casefold` 适合忽略大小写的条目比较；`none` 保留重复项。始终保留第一条的原文与顺序，不排序、不改写内容、不做 Unicode 规范化。

最多 10,000 字符、10,000 行。CRLF 和 CR 转为 LF；末尾换行表示行结束，不额外计一行。报告显示原始行数、保留行数、删除空行与重复行的数量。全空白或所有条目被删除时，结果文本可为空，仍有处理报告。清理文本加报告最多 100,000 UTF-8 字节。

## 文档文本模板

适合有固定结构的项目说明、受理记录或交付说明。默认模板为：

```text
# $title

$name

$body
```

商家可以修改自己的模板，选择 `plain` 或默认的 `markdown`。占位符只支持 `$title`、`$name`、`$body`、对应的 `${...}` 和字面美元符号 `$$`。替换只执行一次，顾客正文中的占位符不会再次展开。工作流环境、秘密、表达式和代码不是模板变量。

标题与称呼各最多 200 字符，正文与模板各最多 10,000 字符。Markdown 格式将标题与称呼作为单行文字转义，正文保留原有 Markdown；纯文本格式将字段文字填入模板，不会删除模板中的 Markdown 符号。结果 `content` 最多 100,000 UTF-8 字节，`format` 标明所选格式。交付的是文本草稿，不是 DOCX/PPTX 文件、签名证书或自动核实过的事实。

## 离线试运行

这只验证处理器输入、输出与 JSONL 协议，不创建商品、卡密或店铺，也不创建沙箱。示例中的内容均为演示数据。命令从 Extore 源码根目录开始：

```sh
cd processors/official
python -m extore_processors csv_summary < examples/csv_summary.json
python -m extore_processors json_formatter < examples/json_formatter.json
python -m extore_processors text_cleanup < examples/text_cleanup.json
python -m extore_processors document_template < examples/document_template.json
```

每个文件含 `params` 与 `configuration`，配置值均为字符串。成功退出码为 0；stdout 最后且只有一条 `kind=result` 结果，之前可有进度行。失败退出码为 2，stderr 仅有安全错误代码和字段名，不回显顾客输入。`csv_summary.summary` 是 JSON 字符串，下游需要再解析一次。

单任务完整输入最多 200,000 字节，包含顾客参数、配置和上下文。成功结果整条 JSONL 最多 100,000 UTF-8 字节，包括信封、所有输出字段与 JSON 转义；字段上限不能相加后绕过总上限，合计过大也会拒绝交付。Extore 默认还限制 120 秒墙钟时间、120 秒 CPU、256 MiB 地址空间、1,000,000 字节进程输出，运行限制可在本店配置档案内调整。程序拒绝联网、启动其他程序或创建文件；源码目录中的 Python 试运行没有这层隔离，生产执行由 Extore worker 提供。

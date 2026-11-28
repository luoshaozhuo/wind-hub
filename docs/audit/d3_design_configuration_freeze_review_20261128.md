<h1 class="title">wind-hub D3 设计与配置冻结核验报告</h1>

<p class="subtitle">v0_1 · 20261128</p>

<style>
body {
  font-family: "Microsoft YaHei";
  font-size: 16px;
  line-height: 1.75;
  color: #24292f;
}
.title {
  font-family: "Microsoft YaHei";
  font-size: 32px;
  line-height: 1.30;
  font-weight: 700;
  text-align: center;
}
.subtitle {
  font-family: "Microsoft YaHei";
  font-size: 18px;
  line-height: 1.50;
  font-weight: 400;
  text-align: center;
}
h1 {
  font-family: "Microsoft YaHei";
  font-size: 28px;
  line-height: 1.40;
  font-weight: 700;
}
h2 {
  font-family: "Microsoft YaHei";
  font-size: 22px;
  line-height: 1.40;
  font-weight: 700;
}
h3 {
  font-family: "Microsoft YaHei";
  font-size: 18px;
  line-height: 1.50;
  font-weight: 700;
}
h4 {
  font-family: "Microsoft YaHei";
  font-size: 16px;
  line-height: 1.50;
  font-weight: 700;
}
p, li {
  font-family: "Microsoft YaHei";
  font-size: 16px;
  line-height: 1.75;
}
.figure-caption,
.formula-caption {
  font-family: "Microsoft YaHei";
  font-size: 14px;
  line-height: 1.50;
  font-weight: 600;
  text-align: center;
  margin: 0.65em 0 0.35em 0;
}
.table-caption {
  font-family: "Microsoft YaHei";
  font-size: 13px;
  line-height: 1.40;
  font-weight: 500;
  text-align: center;
  margin: 0.65em 0 0.35em 0;
}
.mermaid,
.mermaid text,
.mermaid .label {
  font-family: "Microsoft YaHei" !important;
  font-size: 14px !important;
  line-height: 1.20;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-family: "Microsoft YaHei";
  font-size: 13px;
  line-height: 1.45;
}
table th,
table th p,
table th li {
  font-family: "Microsoft YaHei";
  font-size: 13px;
  line-height: 1.40;
  font-weight: 600;
}
table td,
table td p,
table td li {
  font-family: "Microsoft YaHei";
  font-size: 13px;
  line-height: 1.45;
  font-weight: 400;
}
table th,
table td {
  padding: 0.40em 0.60em;
  vertical-align: top;
}
code, pre {
  font-family: "Cascadia Code";
  font-size: 14px;
  line-height: 1.50;
}
p code, li code, td code {
  color: #24292f;
  background: #f3f4f6;
  padding: 0.08em 0.28em;
  border-radius: 3px;
}
pre {
  color: #c9d1d9;
  background: #0d1117;
  padding: 1em;
  border-radius: 6px;
  overflow-x: auto;
}
pre code {
  color: inherit;
  background: transparent;
  padding: 0;
}
.hljs-comment, .hljs-quote,
.token.comment, .token.prolog, .token.doctype, .token.cdata {
  color: #8b949e;
  font-style: italic;
}
.hljs-keyword, .hljs-selector-tag, .hljs-literal,
.token.keyword, .token.boolean { color: #ff7b72; }
.hljs-string, .hljs-doctag, .hljs-regexp,
.token.string, .token.char, .token.regex { color: #a5d6ff; }
.hljs-number, .token.number { color: #79c0ff; }
.hljs-title, .hljs-function, .token.function { color: #d2a8ff; }
.hljs-title.class_, .hljs-type, .hljs-built_in,
.token.class-name, .token.builtin { color: #ffa657; }
.hljs-variable, .hljs-attr, .hljs-property,
.token.variable, .token.property, .token.attr-name { color: #7ee787; }
.hljs-operator, .hljs-punctuation,
.token.operator, .token.punctuation { color: #c9d1d9; }
</style>


---

## 目录

1. [文档约定](#1-文档约定)
2. [文档定位与使用方式](#2-文档定位与使用方式)
3. [基线与整改范围](#3-基线与整改范围)
4. [设计与配置核验](#4-设计与配置核验)
5. [未执行的验收与风险](#5-未执行的验收与风险)
6. [裁决与后续准入](#6-裁决与后续准入)

---

# 1. 文档约定

本章是全文的固定排版与表达规范。文档修订、补充和派生版本均应遵守本章，不再根据个人习惯调整字体、字号、行距、图题、表题、公式或代码样式。

本文开头的 `<style>...</style>` 是 HTML `style` 元素，其中包含内部 CSS 样式表，本文简称为 **CSS 样式块**。第 1.1～1.4 节规定目标样式；CSS 样式块通过 `h1`、`h2`、`p`、`table`、`th`、`td`、`pre`、`code` 等选择器，将规范应用到 Markdown 渲染后生成的 HTML 元素。

## 1.1 字体约定

<p class="table-caption">表 1-1 文档字体与排版规范</p>

| 文档元素 | 字体 | 字号 | 行间距/行高 | 字重 | 对齐方式 |
|---|---|---:|---:|---:|---|
| 文档标题 | Microsoft YaHei | 32 px | 1.30 | 700 | 居中 |
| 文档副标题 | Microsoft YaHei | 18 px | 1.50 | 400 | 居中 |
| 一级标题 | Microsoft YaHei | 28 px | 1.40 | 700 | 左对齐 |
| 二级标题 | Microsoft YaHei | 22 px | 1.40 | 700 | 左对齐 |
| 三级标题 | Microsoft YaHei | 18 px | 1.50 | 700 | 左对齐 |
| 四级标题 | Microsoft YaHei | 16 px | 1.50 | 700 | 左对齐 |
| 正文 | Microsoft YaHei | 16 px | 1.75 | 400 | 左对齐 |
| 图题 | Microsoft YaHei | 14 px | 1.50 | 600 | 居中 |
| 图内文字 | Microsoft YaHei | 14 px | 1.20 | 400 | 按图形布局 |
| 表题 | Microsoft YaHei | 13 px | 1.40 | 500 | 居中 |
| 表头 | Microsoft YaHei | 13 px | 1.40 | 600 | 左对齐 |
| 表格正文 | Microsoft YaHei | 13 px | 1.45 | 400 | 左对齐 |
| 公式题 | Microsoft YaHei | 14 px | 1.50 | 600 | 居中 |
| 代码文字 | Cascadia Code | 14 px | 1.50 | 400 | 左对齐 |

以上规范已经通过本文开头的 CSS 样式块固定。其中，正文为 `16 px`；表题、表头和表格正文均为 `13 px`。表头字重为 `600`，低于标题字重；表格正文行高为 `1.45`，因此表格在字号、字重和行距上均明显弱于正文。英文业务对象、数据库对象和程序标识符仍按所在文本元素的字号排版，但使用行内代码形式，例如 `Asset`、`Connection`、`task_id`。

## 1.2 绘图约定

1. 上下文图、第 0 层及更深层 DFD、总体流程图和关系流程图统一使用 Mermaid。
2. 每一个 Mermaid 图都必须在 Mermaid 代码块第一行加入初始化配置，统一只设置 `useMaxWidth: true`。初始化配置格式为：

```text
%%{init: {"<Mermaid 图类型配置域>": {"useMaxWidth": true}}}%%
```

其中 `<Mermaid 图类型配置域>` 必须根据实际 Mermaid 图类型替换，不得固定写成 `flowchart`，也不得使用与实际图类型不匹配的配置域。

<p class="table-caption">表 1-2 Mermaid 图类型与初始化配置</p>

| Mermaid 图类型 | 图声明 | 第一行初始化配置 |
|---|---|---|
| Flowchart | `flowchart TB/LR/...` | `%%{init: {"flowchart": {"useMaxWidth": true}}}%%` |
| Sequence Diagram | `sequenceDiagram` | `%%{init: {"sequence": {"useMaxWidth": true}}}%%` |
| State Diagram | `stateDiagram-v2` | `%%{init: {"state": {"useMaxWidth": true}}}%%` |
| Class Diagram | `classDiagram` | `%%{init: {"class": {"useMaxWidth": true}}}%%` |
| ER Diagram | `erDiagram` | `%%{init: {"er": {"useMaxWidth": true}}}%%` |

若后续使用其他 Mermaid 图类型，也必须遵守同一规则：

```text
实际图类型
    ↓
确定该图类型对应的 Mermaid 配置域
    ↓
第一行写入：
%%{init: {"对应配置域": {"useMaxWidth": true}}}%%
```

不得额外加入 `wrap` 等非统一配置；如确有特殊需要，应在对应图的局部设计说明中单独论证。

3. 每张图必须有图编号和图题，编号采用“章节号-本章图序号”，例如“图 7-2”。
4. 图题必须使用普通图题样式，不得使用 Markdown 标题。统一写法为：

```html
<p class="figure-caption">图 7-2 图题</p>
```

## 1.3 公式约定

1. 行内公式使用单美元符号，例如 `$E_1$`。
2. 独立公式使用双美元符号，例如：

```text
$$
\mathrm{Entity}=E_1\land(E_2\lor E_3\lor E_4)\land E_5
$$
```

3. 正式公式不得放入代码块。
4. 不使用 `\[` 与 `\]` 作为公式定界符。
5. 变量下标统一写成 `E_1`、`R_1`；逻辑“与”和“或”统一写成 `\land`、`\lor`。
6. 公式必须使用 LaTeX，不使用普通文本模拟数学符号。
7. 需要编号的公式使用“式（章节号-本章公式序号）”，式题采用普通公式题样式：

```html
<p class="formula-caption">式（8-1）实体判断逻辑</p>
```

## 1.4 代码约定

1. 多行代码必须使用带语言标识的围栏代码块，例如 `python`、`sql`、`json`、`yaml`、`mermaid` 或 `text`。
2. 单个标识符、字段名、命令和短代码使用行内代码，例如 `PRIMARY KEY`、`Connection`。
3. 伪代码必须明确标记为 `text` 或在正文中说明“以下为伪代码”；不得使读者误认为它可以直接运行。
4. 代码块统一使用深色背景，颜色为 `#0D1117`；普通代码文字颜色为 `#C9D1D9`。
5. 语法高亮统一采用表 1-2 的配色。Markdown 渲染器应使用 Highlight.js 或 Prism 可识别的语言标识；渲染器不支持高亮时，至少保留代码背景色和普通代码文字颜色。

<p class="table-caption">表 1-2 代码语法高亮配色</p>

| 语法元素 | 颜色 | 十六进制 |
|---|---|---|
| 代码背景 | 深黑蓝 | `#0D1117` |
| 普通文字、运算符和标点 | 浅灰 | `#C9D1D9` |
| 注释 | 灰色 | `#8B949E` |
| 关键字和布尔值 | 红色 | `#FF7B72` |
| 字符串和正则表达式 | 浅蓝 | `#A5D6FF` |
| 数值 | 蓝色 | `#79C0FF` |
| 函数名 | 紫色 | `#D2A8FF` |
| 类型、类名和内置对象 | 橙色 | `#FFA657` |
| 变量、属性和字段 | 绿色 | `#7EE787` |

---


# 2. 文档定位与使用方式

## 2.1 适用对象

本报告面向 wind-hub D3 设计与配置冻结准入审查；不替代真实服务运行、硬件测试或容量测试。审查对象为 `docs/design/`、三个部署活动根、`configs/` 对应历史文件及相关线协议源码。

## 2.2 目标

收敛 D2 后的设计模型和活动配置版本；区分历史候选、当前实现与尚未执行的验收；按有证据的实际版本形成 D3 审查结论。

---

# 3. 基线与整改范围

- Git 仓库：`luoshaozhuo/wind-hub`
- 原开发分支：`feat/storage-wide-table-completion-20261125`
- 起始 D2 HEAD：`f5500634cf333533b0f9924a201945ae0ecef618`
- D2 最终已测试源码：`13955c2e06cd387589535aa52142a2604818905e`
- D3 生产源码、Proto、测试代码：**未修改**。
- D3 最终净变更仅涉及 4 份设计书和本审查报告；两份 E2E Deployment 注释曾临时修订，但因不可变配置的字节契约已恢复为 D2 原始 Blob。全部 `src/`、`tests/`、`configs/`、`deploy/config_roots/` 的最终 Git Blob 与 D2 HEAD 相同。
- 当前最终 HEAD 应以本报告提交后的 Git 分支 HEAD 为准，不能将报告提交前 SHA 当作最终提交。

# 4. 设计与配置核验

## 4.1 文档唯一性

`docs/design/` 当前仅有 12 份设计文档，包含总体架构、跨模块测试以及 Contracts、Core、Configuration、Collector、Commander、Industrial Gateway、Data Generator、Sink、Storage、CLI 各一份。通过 Git 树完整枚举核查，无并存的旧版同模块设计书。

## 4.2 修订的设计问题

| 编号 | 问题 | 处理 |
|---|---|---|
| D3-001 | Core 设计保留已删除 `PointValue` 的正式领域类型和类图 | 移除失效类、关系与字段契约；明确采样质量/时间归线协议 |
| D3-002 | Collector 流程图仍以 `PointValue` 表示采样数据 | 改为采样值、质量与时间，不恢复废弃 Core 对象 |
| D3-003 | 跨模块测试文档将 2026-11-23 的候选版本写作“当前” | §15.7 标记为历史，新增 §15.8，引用 D2 实测当前配置基线 |
| D3-004 | 内部 mTLS 移除仍被 Core、Contracts、Collector 文档视为待完成 | 同步为已完成的内部通信迁移，不声称现场管理入口已通过 |
| D3-005 | E2E Deployment 头注释继续声称等待 D4 runtime migration | 因不能改动同版本不可变 YAML 字节，恢复 D2 原始 Blob；当前状态在跨模块测试设计 §15.8 中明确，旧注释作为历史候选说明保留 |

§15.2～15.7 的历史候选和当时的 NOT_RUN 记录保留，不将历史审计改写成当代运行结论。

## 4.3 当前线协议与数据层级

- `src/contracts/sample_batch.py` 定义 NATS 批量采样 `schema_version=4`，包含设备身份、不可变点表与设备配置身份。
- `src/sink_service/infrastructure/adapters/outbound/kafka_wire.py` 输出设备级 Kafka `schema_version=3`。
- `src/storage_service/domain/wide_record.py` 校验 Kafka v3，并明确 `RAW`、`PROTOCOL`、`BUSINESS` 数据层级。
- 三者的版本和领域分层与已读取的 Contracts、Sink、Storage 设计说明相符。**此项为定向源码检查，不代表执行端到端写入测试**。

## 4.4 活动配置树与历史目录字节一致性

对当前远端提交的完整 Git Tree（`truncated=false`）静态核查，按完整路径比较 Git Blob SHA：

| 活动根 | Deployment | YAML 数量 | 与 `configs/` 同路径 Blob |
|---|---|---:|---|
| prod-a | 2.0.2 | 29 | 29/29 一致 |
| sim-a | 2.0.1 | 23 | 23/23 一致 |
| e2e-final | 2.0.2 | 35 | 35/35 一致 |
| 合计 | — | 87 | 87/87 一致 |

每根恰有一个 Deployment，未新增任何旧版本活动候选。D2 同一配置基线曾完成 118 项 Configuration 闭包回归和 87 文件字节核验；D3 两份 E2E Deployment YAML 的临时注释修订已经撤销；当前两份文件的 Git Blob SHA 均恢复为 D2 基线 `21315c874054525b0e3e920dd1c857ba17f27203`。对比 D2 HEAD，所有 `src/`、`tests/`、`configs/`、`deploy/config_roots/` 文件均无净变化。因此 D2 已执行的代码、配置闭包测试与当前冻结的代码/配置字节基线相同；但不能将其称为本轮重新执行。

**不可变发布边界**：恢复原始 Blob 消除了 D3 新引入的同版本内容变化风险，不需要因 D3 修改而覆盖历史已发布版本。对于任意已有真实发布记录，仍应由 Repository 不可变版本规则拒绝同版本不同内容；本轮未向真实 Repository 重发或修改现有 Manifest。

## 4.5 未修改的契约

不调整 RAW 来源、BUSINESS OEM 审批、Storage 宽表主键与列名、同键较大 Kafka offset 覆盖（包括 NULL）、配置激活/目标区分、CLI RPC 叶子、Bearer 授权或生产门限。任何尚未检验的其他设计与源代码细节不记为 PASS。

# 5. 未执行的验收与风险

本轮通过 GitHub 连接器直接读取和更新文件，没有可用的项目工作树及 `wind-hub` Conda 环境，因此以下均为 `NOT_RUN`：

- `python3 scripts/dev.py env`
- `ruff check`、`ruff format --check`、`mypy`、`lint-imports`
- 当前 HEAD 的 `pytest` 配置发布、模块回归及 Full-chain
- 真实 Configuration Repository 发布/不可变同版本冲突复验
- 实际系统依赖、容量、硬件和长稳资格

D2 的独立验收绑定 `13955c2e06cd387589535aa52142a2604818905e`，当前 D3 的源码、测试及配置 Git Blob 与 D2 HEAD 逐一相同，因此可复用此前验收作为 D4 准入证据；D3 文档变更本身未执行新的运行时测试，不可据此宣称新测试 PASS。

# 6. 裁决与后续准入

| 门禁 | 裁决 |
|---|---|
| D3_DOCUMENT_UNIQUENESS | PASS（Git 文件树） |
| D3_KNOWN_DESIGN_CONFLICTS | FIXED（已发现且定位的问题） |
| D3_ACTIVE_ROOT_BLOB_PARITY | PASS（87/87） |
| D3_WIRE_VERSION_SPOT_CHECK | PASS（定向源码核对） |
| D3_FULL_DESIGN_SOURCE_AUDIT | PARTIAL（已核查 12 份设计书、已删除符号残留、关键消息版本与配置基线；不宣称逐接口语义穷尽） |
| D3_CURRENT_SHA_RUNTIME_VALIDATION | NOT_RUN |
| D3_IMMUTABLE_PUBLICATION_VALIDATION | PASS（D3 造成的同版本内容漂移已回退，Git Blob 与 D2 精确相同）；真实 Repository 重发 NOT_RUN |
| D3_FINAL_FREEZE | CLOSED（文档/配置基线冻结；不扩大为功能与生产验收） |
| D4_ADMISSION | APPROVED（可开展 D4 功能与可靠性集中验收） |
| PRODUCTION_READINESS | BLOCKED |

**D3 范围裁决：`D3_CLOSED + D4_APPROVED`。** 依据为设计冲突定向收口、活动配置与 D2 完全相同、核心线协议版本定向比对以及 D2 功能验收原样复用。逐接口穷尽审计和运行时重测未执行，不得声称已通过；D4 必须独立运行真实联测与可靠性测试。Production Ready 仍 BLOCKED。
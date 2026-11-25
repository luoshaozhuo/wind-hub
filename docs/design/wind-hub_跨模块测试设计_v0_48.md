<h1 class="title">wind-hub 跨模块测试设计</h1>

<p class="subtitle">v0_48 · 20261125 · 宽表重构协同设计（待技术验证）</p>

<style>
body { font-family: "Microsoft YaHei"; font-size: 16px; line-height: 1.75; color: #24292f; }
.title { font-family: "Microsoft YaHei"; font-size: 32px; line-height: 1.30; font-weight: 700; text-align: center; }
.subtitle { font-family: "Microsoft YaHei"; font-size: 18px; line-height: 1.50; font-weight: 400; text-align: center; }
h1 { font-family: "Microsoft YaHei"; font-size: 28px; line-height: 1.40; font-weight: 700; }
h2 { font-family: "Microsoft YaHei"; font-size: 22px; line-height: 1.40; font-weight: 700; }
h3 { font-family: "Microsoft YaHei"; font-size: 18px; line-height: 1.50; font-weight: 700; }
h4 { font-family: "Microsoft YaHei"; font-size: 16px; line-height: 1.50; font-weight: 700; }
p, li { font-family: "Microsoft YaHei"; font-size: 16px; line-height: 1.75; }
.figure-caption, .formula-caption { font-family: "Microsoft YaHei"; font-size: 14px; line-height: 1.50; font-weight: 600; text-align: center; margin: 0.65em 0 0.35em; }
.table-caption { font-family: "Microsoft YaHei"; font-size: 13px; line-height: 1.40; font-weight: 500; text-align: center; margin: 0.65em 0 0.35em; }
.mermaid, .mermaid text, .mermaid .label { font-family: "Microsoft YaHei" !important; font-size: 14px !important; line-height: 1.20; }
table { width: 100%; border-collapse: collapse; font-family: "Microsoft YaHei"; font-size: 13px; line-height: 1.45; }
table th, table th p, table th li { font-family: "Microsoft YaHei"; font-size: 13px; line-height: 1.40; font-weight: 600; }
table td, table td p, table td li { font-family: "Microsoft YaHei"; font-size: 13px; line-height: 1.45; font-weight: 400; }
table th, table td { padding: 0.40em 0.60em; vertical-align: top; }
code, pre { font-family: "Cascadia Code"; font-size: 14px; line-height: 1.50; }
p code, li code, td code { color: #24292f; background: #f3f4f6; padding: 0.08em 0.28em; border-radius: 3px; }
pre { color: #c9d1d9; background: #0d1117; padding: 1em; border-radius: 6px; overflow-x: auto; }
pre code { color: inherit; background: transparent; padding: 0; }
.hljs-comment, .hljs-quote, .token.comment, .token.prolog, .token.doctype, .token.cdata { color: #8b949e; font-style: italic; }
.hljs-keyword, .hljs-selector-tag, .hljs-literal, .token.keyword, .token.boolean { color: #ff7b72; }
.hljs-string, .hljs-doctag, .hljs-regexp, .token.string, .token.char, .token.regex { color: #a5d6ff; }
.hljs-number, .token.number { color: #79c0ff; }
.hljs-title, .hljs-function, .token.function { color: #d2a8ff; }
.hljs-title.class_, .hljs-type, .hljs-built_in, .token.class-name, .token.builtin { color: #ffa657; }
.hljs-variable, .hljs-attr, .hljs-property, .token.variable, .token.property, .token.attr-name { color: #7ee787; }
.hljs-operator, .hljs-punctuation, .token.operator, .token.punctuation { color: #c9d1d9; }
</style>

---

## 目录

1. [文档约定](#1-文档约定)
2. [文档定位与使用方式](#2-文档定位与使用方式)
3. [测试目标、范围与总体要求](#3-测试目标范围与总体要求)
4. [完整测试场景与总体数据流](#4-完整测试场景与总体数据流)
5. [共享配置、运行环境与进程启动](#5-共享配置运行环境与进程启动)
6. [Integration 分段测试](#6-integration-分段测试)
7. [E2E 完整场景](#7-e2e-完整场景)
8. [CLI 与 gRPC 全功能覆盖](#8-cli-与-grpc-全功能覆盖)
9. [Golden Data、采样率与输出比较](#9-golden-data采样率与输出比较)
10. [性能、故障与安全验收](#10-性能故障与安全验收)
11. [测试执行、目录、Marker 与报告](#11-测试执行目录marker-与报告)
12. [准入、裁决及实施缺口](#12-准入裁决及实施缺口)

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

若后续使用其他 Mermaid 图类型，也必须遵守同一规则：先确认实际图类型，再确定其配置域，最后在首行仅配置 `useMaxWidth: true`。不得额外加入 `wrap` 等非统一配置；特殊需要应单独论证。

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
3. 伪代码必须明确标记为 `text` 或说明“以下为伪代码”；不得使读者误认为可以直接运行。
4. 代码块统一使用深色背景 `#0D1117`；普通代码文字颜色 `#C9D1D9`。
5. 语法高亮按照下表；不支持高亮的渲染器至少保留代码背景与普通文字颜色。

<p class="table-caption">表 1-3 代码语法高亮配色</p>

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

本文件是 wind-hub 跨模块 Integration、E2E、全 CLI/gRPC、性能、可靠性与安全的统一测试设计。测试以真实业务进程和独立观测证明端到端行为；文档不代表未执行的场景已经通过。

**基线**：执行前重新获取当前开发分支 HEAD，以同一 HEAD 下正式 Proto、设计书、运行时 Schema、`configs/` 和 CLI 实际帮助为准，不使用其他分支的旧目录或测试结论代替。Integration 与 E2E 共享同一套 `configs/`，但使用不同测试激励和断言。

**责任**：Contracts 拥有线协议，Core 拥有共享领域和通用配置能力，业务模块拥有业务配置和处理过程。所有 wind-hub 业务进程经真实 CLI 和 Linux 原生进程管理器启动，NATS/JetStream、Redis、Kafka 属基础设施，由测试环境管理。采用 Ports & Adapters 的业务模块依设计检验依赖边界；不要求 Core、Contracts、CLI 机械采用相同分层。

# 3. 测试目标、范围与总体要求

**本版目标与现行实现边界**：20261125 宽表重构为目标设计，代码和运行配置仍是旧实现；历史执行记录与现行代码示例仅用于迁移差距分析。目标线格式以 Contracts §11.6、§11.9 为唯一规范，物理模型以 Storage v1_13 为准。禁止新旧格式混用。未完成技术验证、完整配置和接口冻结前，不得认定可编码或生产就绪。

**符合性边界（2026-11-23）**：以冻结的总体设计 v1_27、当前 Proto 和主分支实现为依据；服务配置只通过 Deployment 版本选择、Repository 发布与模块独立生效。RAW/PROTOCOL 的精确定义在冻结总体设计 §5.3.4 尚有内部矛盾，本设计不得以缺失源载荷伪造 RAW，也不得将 PROTOCOL 改写为旧 ENGINEERING。未裁决的 RAW 语义不计为已验证实现。

**迁移基线（非目标验收）**：现行 main NATS v3/Kafka v2；目标验收采用 NATS v4/Kafka v3、Storage v2。中间件与数据库由测试启动，服务经 CLI 启停及操作，覆盖全部目标 RPC/CLI 叶子。

## 3.1 测试目标

1. 验证 WTG、PV、BESS、GRID、PPC 五类物理仿真，以及随机和 CSV 回放源的可复现输出与跨模块数据传输。
2. 验证完整测量链路 Data Generator → Industrial Gateway → Collector → NATS/JetStream → Sink → Kafka → Storage Service → TDengine。四类 Sink 目标仍分别验收；Kafka 是传输检查点，Storage/TDengine 的历史落地与独立读回才是最终成功判据。
3. 独立验证工业控制链路：工业 Client → Gateway → Commander → Gateway 专用仿真执行 RPC → Data Generator 模型 INPUT；只有请求级真实计算步完成证据可促成成功结算。
4. 每个 CLI 叶子命令和全部正式 RPC 均有可定位用例，包含正常、拒绝、超时、权限或幂等路径；逐层证明进程、gRPC、业务状态和下游副作用。
5. 保留用例 ID → HEAD/配置哈希 → CLI/请求 → 源 Golden Data → NATS 批次 → Kafka Record → Storage Offset → TDengine 查询和独立读回 → 断言/裁决的完整证据链。

## 3.2 关键约束

- E2E 不以 Mock/Fake 替代核心业务服务；Integration 可向真实 NATS 注入标准批次作为分段激励，但不能因此宣称完整 E2E 通过。
- 全部 wind-hub 业务进程，包括独立 Gateway/Modbus 目标，必须通过正式 CLI 管理；测试 Fixture 不允许通过 subprocess 直接启动业务进程。NATS/JetStream、Kafka、Redis、TDengine 例外；Configuration 与 Storage Service 必须由 CLI 启动。
- 配置与模板统一来自正式 `configs/`，场景参数和运行记录存放于测试目录；不复制第二份共享型号、点表或单位目录，不修改真实生产设备配置。
- 进程 RUNNING、gRPC READY、业务 Generation/Task/Delivery RUNNING、下游生效分别观测；ACK 不等于控制 APPLIED，也不等于 Kafka 最终消费。
- 每个场景须列配置、进程、激励、步骤、独立输出、断言和清理。无环境为 NOT_RUN，接口缺失为 NOT_IMPLEMENTED，已执行失败为 FAIL；禁止删除未覆盖条目缩小分母。
- 当前验收只涉及 Linux 原生进程；未实际验证的 containerd/iSulad、硬件、netem 必须单列 NOT_RUN。

## 3.3 现行设计基线

| 领域 | main 归并后唯一设计文件 | 验收重点 |
|---|---|---|
| 总体 / Core | `wind-hub_总体逻辑架构设计_v1_29.md`、`Core_软件设计_v1_47.md` | DFD 归属、共享配置归 Core |
| Contracts / Configuration | `Contracts_软件设计_v1_47.md`、`Configuration_Service_软件设计_v1_25.md` | 现行 Proto、Deployment 闭包、版本发布与查询 |
| Data Generator / Industrial Gateway | `Data_Generator_软件设计_v1_64.md`、`industrial_gateway_软件设计_v1_53.md` | 仿真、变量身份、目标写入与端口权限 |
| Collector / Commander | `Collector_软件设计_v1_46.md`、`Commander_软件设计_v1_60.md` | 目标同步 SampleBatch、控制安全、运行状态 |
| Sink / Storage | `Sink_Service_软件设计_v1_79.md`、`Storage_Service_软件设计_v1_13.md` | Kafka JSON v3、设备宽表及查询 |
| CLI | `CLI_软件设计_v1_58.md` | CLI 命令树、进程启动和全量发布 |

架构一致性单列检查：共享配置不得以顶级 `src/configuration/` 长期并行存在，业务配置不能机械移入 Core，`typings/grpc-stubs/` 的退出须经正式类型依赖和 Mypy 实测；目录迁移不能只检测 imports 而忽视代码重复及所有者。

# 4. 完整测试场景与总体数据流

## 4.1 总体测量流与仿真控制流

~~~mermaid
%%{init: {"flowchart": {"useMaxWidth": true}}}%%
flowchart LR
    S["物理 / 随机 / CSV 场景"] --> DG["Data Generator"]
    DG -->|"原始记录"| GD["Golden Data"]
    DG -->|"租约 + UpdateVariables（测量）"| GW["Industrial Gateway：源 Server"]
    GW -->|"Modbus TCP 读"| COL["Collector"]
    COL -->|"Realtime"| N["NATS Core"]
    COL -->|"Reliable"| JS["JetStream"]
    N --> RT["Sink Realtime"]
    JS --> REL["Sink Reliable"]
    RT -->|"租约 + UpdateVariables"| GWT["Gateway 独立输出目标"]
    RT --> M["Modbus TCP Server"]
    RT --> R["Redis"]
    REL --> K["Kafka"]
    K --> ST["Storage Service"]
    ST --> TD["TDengine"]
    TD -->|"Storage QuerySamples / 独立 SQL"| V
    GWT --> V["独立读回比对器"]
    M --> V
    R --> V
    K -.->|"Kafka 传输观察"| V
    GD -.-> V
    IC["工业控制 Client"] -->|"控制写事件"| GW
    GW -->|"SubmitCommand(s)"| CMD["Commander"]
    CMD -->|"ExecuteSimulationCommand"| GW
    GW -->|"UpdateVariables（模型 INPUT）"| DG
    CMD -->|"GetSimulationExecutionResult"| GW
    GW -->|"GetInputExecution"| DG
~~~

<p class="figure-caption">图 4-1 测量双通道、四类 Sink 出口、Storage 最终落地与独立仿真控制链</p>

Gateway 的 `UpdateVariables` 是持有变量空间的测量注入接口，受 `ClaimTargets` 租约保护，不能触发工业控制事件。Gateway 的 `ExecuteSimulationCommand` 是接收 Commander 授权的**独立控制接口**，随后调用 Generator 的 `UpdateVariables` 修改模型 INPUT；`GetSimulationExecutionResult` 经 Generator 请求级 `GetInputExecution` 得到 PENDING/APPLIED/UNKNOWN 证据。`accepted/effective_step` 只表示准入，不足以证明 APPLIED。

Sink 的 Gateway 出站 Adapter **已实现**于 `src/sink_service/infrastructure/adapters/outbound/gateway.py`，以 内网访问授权 获取租约并更新目标 Gateway；真实 E2E 仍须运行验收。其目标变量空间与源 Gateway 采集变量、控制映射必须隔离。Sink 的 Modbus Realtime 输出为 **Modbus TCP Server**，须由外部 Client 读回；不得假定两个 Server 可以主动互写。

## 4.2 必测场景

| 组 | 主要激励 | 观测边界 |
|---|---|---|
| P-01 风机 | 平均风速 10→12 m/s，确定性湍流度 0，合法控制模式 | WTG 源批、Gateway、采集、四出口对应 |
| P-02 光伏 | 辐照与温度阶跃 | PV 输出边界、编码与映射 |
| P-03 储能 | 充放电、SOC 边界、非法输入 | P/Q/SOC 连续性及整批拒绝 |
| P-04 电网 | 频率/电压阶跃、功率聚合 | GRID 模型和目标读回 |
| P-05 PPC/控制 | AGC/AVC/FFR 允许输入和工业控制写 | Commander 身份、真实计算步和 APPLIED/UNKNOWN |
| P-06 随机/回放 | 固定种子、固定 CSV | 确定性、历史顺序 |
| P-07 速率/路由 | 多周期、四种出口 | Realtime 覆盖、Reliable 重投与 ACK |
| P-08 运维/安全 | 重启、断连、非法配置、无权请求 | 层级状态与隔离 |
| P-09 架构/配置 | 配置源、版本、路径、业务配置归属 | 共享 Core/业务模块边界一致 |
| P-10 存储闭环 | RAW/PROTOCOL/BUSINESS 消息、重复、故障及恢复 | TDengine 物理数据、正式查询、Offset、身份、质量、时间与历史版本一致 |

模型数学准确性由各模型设计与单模块验证负责，本文件验证源输出到各目标的跨模块可追溯一致性。

# 5. 共享配置、运行环境与进程启动

### 2026-11-21 统一配置版本选择与生效规则（规范性修订）
Service → Instances → Templates 是唯一允许的配置引用顺序：Service 仅引用 Instance 逻辑身份，不直接引用 Template ID/版本或目录。Deployment 统一选择 Service/Instance 的不可变版本，Instance 固定引用 Template 的版本。全量发布不直接使所有服务切换；Configuration 广播后，各模块主动解析目标配置闭包并决定热更新或等待重启。
Service 状态以 target_revision 和 active_revision 表示本服务完整有效配置闭包的目标与完全生效值。不能热更新的字段保持原活动值等待重启；部分更新不能虚报完整生效。Deployment 回退引用历史版本即可，丢失通知由定期权威对账补偿。旧直接模板来源及版本路径引用必须在实施中统一清理。

Configuration/Deployment 已集成后的跨模块验收矩阵：
1. 全量发布只新增不可变版本；未修改 Deployment 的 Service/Instance 目标保持原值。
2. 同 Service 版本仅升级 Device Instance，验证采集 Device 数及配置修订变化，其他模块无关配置不触发重启。
3. Collector Task 定义归 collector_tasks Instance；更改采样周期 100→200 ms，稳定运行时验证采样节拍；Device group fast→slow，验证无重复调度。
4. PointTable 经 Device Instance→Template 引用链升级，验证 Collector、Gateway、Commander 对同一目标闭包的解析及控制安全隔离。
5. Sink Sinks/Deliveries 版本变更后验证 Realtime 与 Reliable 切换以及 ACK/重投，Service 静态参数更新保持旧运行直至 CLI 重启。
6. 断线 Service 不阻断其它服务；重连或进程重启后查询 Deployment 目标并收敛；旧版本回滚，记录两项修订字段及实际结果。
7. 配置引用缺失、歧义、循环、同身份/版本内容冲突、错误摘要及并发 CAS 均必须拒绝，保持活动闭包不变。
8. 通过 CLI 启动 Collector、Commander、Gateway、Data Generator、Sink、Storage；NATS、Kafka、Redis、TDengine 由测试程序独立启动；控制操作全部经 CLI，结果用真值文件、gRPC 读回、消息/数据库及状态输出交叉检验。
配置文件及路径需按改造后的正式 Schema 更新测试 Fixture，不得以设计示例伪装已实现配置。



## 5.1 配置结构、版本号与 E2E 文件命名

C2 已将所有受管 YAML 改为带版本后缀的正式文件，以下清单为开发分支的实际配置基线；测试 Fixture 必须与选定版本对应。

~~~text
configs/
├── deployments/
│   ├── deployment_prod-a_v1.3.0.yaml
│   └── deployment_sim-a_v1.1.0.yaml
├── instances/
│   ├── site_example_site_v1.1.0.yaml
│   ├── device_farm-a_v1.4.0.yaml
│   ├── device_sim_v1.2.0.yaml
│   ├── collector_tasks_farm-a_v1.0.0.yaml
│   ├── collector_tasks_sim_v1.0.0.yaml
│   ├── scenarios_prod-a_v1.0.0.yaml
│   ├── scenarios_sim_v1.0.3.yaml
│   ├── sinks_prod-a_v1.2.0.yaml
│   └── deliveries_prod-a_v1.3.0.yaml
├── services/
│   ├── service_collector-realtime_v1.1.0.yaml
│   ├── service_collector-reliable_v1.0.0.yaml
│   ├── service_collector-sim_v1.0.0.yaml
│   ├── service_commander_v1.4.0.yaml
│   ├── service_commander-sim_v1.0.0.yaml
│   ├── service_gateway_v1.2.0.yaml
│   ├── service_gateway-sim_v1.1.3.yaml
│   ├── service_data-generator_v1.1.0.yaml
│   ├── service_data-generator-sim_v1.0.3.yaml
│   ├── service_sink-west_v1.2.0.yaml
│   └── service_cli_v1.2.0.yaml
├── templates/
│   ├── units_standard_v1.2.0.yaml
│   ├── business_points_standard_v1.4.0.yaml
│   ├── point_tables/point_table_<info>_vX.Y.Z.yaml
│   ├── device_models/device_model_<info>_vX.Y.Z.yaml
│   ├── protocols/protocol_<info>_vX.Y.Z.yaml
│   ├── sinks/sink_<info>_vX.Y.Z.yaml
│   ├── deliveries/delivery_<info>_vX.Y.Z.yaml
│   └── observability_v1.0.0.yaml
└── history/demo.csv  # 非 YAML 测试资产，CLI 发布仅扫描 .yaml
~~~

<p class="figure-caption">图 5-1 C2 版本化配置资产及 Deployment 选定边界</p>

所有受管 YAML 文件名必须包含 `_vMAJOR.MINOR.PATCH.yaml`；正文 `file_version` 为对应的 `MAJOR.MINOR.PATCH`。Configuration 的本地引导文件位于 `deploy/configuration/`，不是业务发布根目录。Device `group` 属于 Device Instance；Task 属于 Collector 专属 Instance；Scenario 属于独立 Instance，不再使用 `templates/tasks` 或 Service 内联场景。

## 5.2 角色、配置与前置差异

| 角色 | 实际配置选择 | E2E 要求 |
|---|---|---|
| 设备 | `device_sim_v1.2.0.yaml` | WTG/PV/BESS/GRID/PPC_SIM01 五设备，group=simulation |
| Generator | `service_data-generator-sim_v1.0.3.yaml` + `scenarios_sim_v1.0.3.yaml` | 场景 wind-plant-physical，确定性激励 |
| Gateway | `service_gateway-sim_v1.1.3.yaml` | gRPC 50073，五 Server；控制安全规则保持有效 |
| Collector | `service_collector-sim_v1.0.0.yaml` + `collector_tasks_sim_v1.0.0.yaml` | 验证按 group 调度、不重复采集；真实部署另外有 Realtime/Reliable 双 Service |
| Sink | `service_sink-west_v1.2.0.yaml` + Sinks/Deliveries Instance | 包含 Redis/Kafka/Modbus/Gateway 四类 Sink；Gateway Delivery 默认 STOP，授权与变量映射需 E2E 实证 |
| Commander | `service_commander-sim_v1.0.0.yaml` | 仿真控制、Gateway 权限与请求身份一致 |
| CLI | `service_cli_v1.2.0.yaml` | 所有业务进程由 CLI 启动；中间件独立启动 |
| 外部 | NATS/JetStream、Redis、Kafka、TDengine | 隔离命名空间；由测试代码单独管理 |

`sim-a` 与 `prod-a` 是独立 Deployment；同一部署域内共享 Instance 只有一个选定版本。CLI 的运行专用生成文件（例如 `cli-runtime.yaml`）放在业务发布根目录之外。业务模块加载器按新契约的兼容性适配属于 C3，本节不宣称当前 E2E 已通过。

## 5.3 最少配置原则

不为测试复制已有等价 Template；测试需要新设备或新场景时，增加明确身份的版本化 Instance/Template，并在隔离 Deployment 中选择。全量候选发布由 Configuration 校验完整依赖闭包。标准单位、业务点、PointTable 和 Device Model 都固定版本，测试以发布清单与有效闭包 SHA-256 为证据。不得把不可变发布版本当作运行时启停状态修改。

## 5.4 业务进程和中间件生命周期

| 对象 | 真实启动方 | 独立判据 |
|---|---|---|
| NATS/JetStream、Kafka、Redis、TDengine | 测试基础设施管理器 | READY、隔离 Stream/Topic/Key/数据库、可验证清理 |
| Configuration、Gateway、Generator、Collector、Commander、Sink Main、Storage Service | wind-hub CLI | service start/stop/restart/status、PID、gRPC READY、消费和存储状态 |
| Sink Realtime/Reliable Worker | Sink Main 内部管理 | 子进程就绪及安全退出 |
| Sink Modbus Server | Sink Realtime Worker | 独立 Modbus Client 读回 |
| pytest、数据观察器、激励器 | 测试执行器 | 不能越权代替 CLI 启业务进程 |

~~~mermaid
%%{init: {"sequence": {"useMaxWidth": true}}}%%
sequenceDiagram
    participant T as E2E 执行器
    participant I as 中间件准备器
    participant CLI as wind-hub CLI
    participant PM as Linux 进程管理器
    participant P as 业务服务
    T->>I: 准备 NATS / Redis / Kafka / TDengine
    I-->>T: READY 与隔离资源
    T->>CLI: service start（Configuration/Gateway/Generator/Collector/Commander/Sink/Storage）
    CLI->>PM: 启动正式 Service
    PM->>P: 运行进程和内部 Worker
    P-->>T: gRPC READY + 业务状态
    T->>CLI: 启动 Generation / Task / Delivery
    T->>T: 激励、独立读回、断言
    T->>CLI: 停止任务和 Service
    CLI->>PM: 回收业务进程
    T->>I: 清理测试所有的中间件资源
~~~

<p class="figure-caption">图 5-2 CLI 进程管理、业务就绪及中间件所有权</p>

## 5.5 环境自举、端口分配与硬件边界

**软件仿真 E2E 不需要外部真实风机、TwinCAT PLC、现场 IEC104 装置或工业网关实体。** 这些设备均由仓库内 Data Generator、Industrial Gateway（含仿真 Modbus 从站）、测试用 Modbus TCP Server 和 CLI 管理的业务进程提供；仅 NATS/JetStream、Redis、Kafka 属独立测试基础设施。TDengine 是所有 FULL_CHAIN_STORAGE 方案的真实必需依赖，使用隔离数据库和动态端点。真实 TwinCAT/现场硬件验证单列为部署/现场验收，不能作为 E2E-01～10 的执行前置，也不得因为缺少硬件将仿真 E2E 标记 NOT_RUN。

1. **端口先检测再分配。** 普通 System Redis Fixture 使用独立 Compose Project 和随机 loopback 宿主端口，返回真实端点给测试；R5 完整栈对 NATS、Redis、Kafka、TDengine 分别使用动态宿主端口。所有生成的 Service YAML、CLI YAML、Sink 连接及测试读回均引用分配的端点，禁止硬编码 6379/4222/9092。
2. **冲突处理。** Compose 绑定冲突时仅销毁本用例创建的 Project，重新分配端口并有界重试；不执行 docker prune、不停止其他用户的容器。显式 `WIND_HUB_TEST_REDIS` 可使用外部 Redis，但必须标记为外部依赖，不能在结束时停机。端口重试后仍失败记为 SETUP_ERROR 并保留日志，不属于生产模块 FAIL。
3. **进程管理。** CLI 启动/停止/restart 通过正式 process 管理后端启动和管理独立 Linux 进程，不依赖 systemd。必须校验进程身份（PID 与 start-tick）、进程组、状态文件及日志所有权；容器后端须另做真实运行时专项验证。任何后端不可用时记录 NOT_RUN_ENV，不得用 Mock 伪造 PASS。
4. **隔离与清理。** 每个测试使用独立 tmp_path、动态端口、独立进程管理状态目录与受控服务 ID、唯一 NATS Stream / Kafka Topic / Redis Namespace；即使断言失败也依次 CLI 停止业务进程、清理本次进程组、中间件 Project、文件和流。任何端口占用必须先归属识别，不得无条件删除非本测试容器。
5. **前置检查与裁决。** 校验当前 HEAD、Python/CLI 执行路径、Docker daemon、process 管理后端、访问令牌注入、compose 健康、服务 gRPC READY 和 Sink 双 Worker 状态；缺业务必需中间件或正式 process 后端的场景标 NOT_RUN，缺少真实现场硬件对软件仿真场景**不构成** NOT_RUN 理由。不得把准备阶段的端口竞争记作软件功能缺陷。

## 5.6 联测专用配置闭包与文件名规范（v0_39）

**当前设计候选基线**：`deploy/config_roots/e2e-final/deployments/deployment_e2e-final_v1.0.2.yaml`，其源配置位于 `configs/deployments/`；该候选依赖尚未注册的 Service Schema v3，必须在第二阶段代码准入后才能实际发布和运行。所有受测 Service 与 Instance 使用 `e2e` 或 `e2e-final` 独立逻辑身份；统一文件名 `<type>_<info>_v<MAJOR.MINOR.PATCH>.yaml`。Service 必须遵守正式 `service_<service-id>_v...` 前缀，不得擅自简化为缺少 `service_` 的文件名。E2E 文件不覆盖 `prod-a` 和 `sim-a` 的已发布版本；相同业务 Template 可以按原 SHA/版本引用，不复制第二份人工维护的型号或点表。

| 类别 | E2E 唯一配置及用途 |
|---|---|
| Deployment | `deployment_e2e-final_v1.0.2.yaml`；选定 9 个 Service 和 6 类 Instance |
| Collector | `service_collector-e2e-realtime_v1.0.0.yaml`、`service_collector-e2e-reliable_v1.0.0.yaml`；独立身份和 100 ms Task |
| Gateway | `service_gateway-e2e-source_v1.1.0.yaml`、`service_gateway-e2e-target_v1.1.0.yaml`；令牌鉴权、5 个源协议 Server 和独立 WTG_SINK01 输出目标 |
| 模型/控制 | `service_data-generator-e2e_v1.1.0.yaml`、`service_commander-e2e_v1.1.0.yaml`；令牌鉴权、可配置 TI=0/固定种子、开启真值记录和 PPC 控制映射 |
| 派发/入库 | `service_sink-e2e_v1.0.0.yaml`、`service_storage-e2e_v1.1.0.yaml`；隔离 Kafka Topic/Consumer Group、TDengine 数据库、授权身份和可靠通道 |
| CLI | `service_cli-e2e_v1.1.0.yaml`；删除 mTLS Profile，所有业务服务通过 process manager 启动，不依赖生产 CLI 容器状态 |
| Instance | `site_e2e-final_v1.0.0.yaml`、`device_e2e-final_v1.0.0.yaml`、`collector_tasks_e2e-final_v1.0.0.yaml`、`scenarios_e2e-final_v1.0.0.yaml`、`sinks_e2e-final_v1.0.1.yaml`、`deliveries_e2e-final_v1.0.1.yaml` |
| 本地引导 | `deploy/e2e/configuration_bootstrap_e2e_v1.0.0.yaml`、`deploy/e2e/storage_bootstrap_e2e_v1.0.0.yaml`；不参与 Configuration Repository 业务文件发布 |

**激活和隔离要求：** 测试执行器在临时工作目录中从正式 `configs/` 选取 E2E Deployment 所需的精确 Service/Instance/Template/BusinessPoint/Units/Protocol 原始 YAML，按相同路径装配**一次性发布根目录**；文件内容及 SHA-256 必须与源文件一致，不建立人工维护的复制模板。使用真实 `config publish --root` 发布该目录。任何运行时地址、令牌环境来源、Python 可执行文件或测试数据库目录调整均须生成新版本的临时 E2E Service/Instance，并更新临时 Deployment 精确选择；绝不能在已发布 `file_version` 上覆盖字节。令牌和独立测试库由 Fixture 准备，禁止引用或删除现场生产密钥/数据。源 Modbus 端口为 15221～15225，Sink Gateway 目标 15121；管理端口为 520xx 系列，Storage 为 52110；应在启动前检查占用并隔离。

**三级数据的配置前置：** 正常基线 `deliveries_e2e-final_v1.0.1.yaml` 仅启用四类 Sink 对应的 PROTOCOL 测试 Delivery，初态 STOP，由 CLI 显式启动。BUSINESS 不能在未取得 Configuration `GetPointTableBundle` 的真实历史 manifest SHA-256 前配置虚构批准值；Fixture 必须先发布并查询真实闭包，生成新的候选 Delivery Instance/Service 文件版本、填充 `config_sync.approved_point_table_manifests` 与查询 Token，校验后再发布、执行 BUSINESS Golden Data 对账。独立 `delivery_e2e_business_history_v1.0.0.yaml` 是保留的受控实验 Template，非默认生效 Delivery。RAW 缺少未解码载荷的真实来源，当前 Sink 拒绝，因此只能做明确的拒绝测试或取得新增生产实现后再做正常数据链，不得用假 RAW Record 宣称 E2E PASS。

**每次联测前门禁：** 对 staged `configs/` 做严格 YAML 解析/重复键拒绝、`schema_version` 和文件名/ID/`file_version` 校验、Deployment→Service→Instance→Template→PointTable→BusinessPoint/Units 的固定版本引用检查、点表展开后的 BusinessPoint 唯一性以及入口 Schema 验证。使用正式 Configuration Candidate 的 Validate 和 Publish 记录最终修订、SHA 和全量成员；校验失败须保持旧修订且本次 E2E 不启动。不能把 schema-only 校验等同语义一致性。

# 6. Integration 分段测试

所有分段沿用第 5 章正式配置和 CLI 进程管理，不复制配置树；若某前置配置尚未落地，则该行保留为 NOT_RUN/NOT_IMPLEMENTED。每行执行前置检查→CLI 启服务→注入激励→独立读回→关闭并清理。

| 用例 | 配置、CLI 启动的进程 | 激励与执行步骤 | 预期输出和断言 |
|---|---|---|---|
| I-01 源注入 | e2e-final 选定 Device Instance/Model/PointTable 和 DG/Gateway Service；Generator、Gateway | 先 Gateway READY → Generation start → 固定风速和模型输出 → Gateway 读回 → stop | 源记录和 Gateway 值按时间、点位、版本对齐，租约合法 |
| I-02 工业采集 | 前项 + Collector 模拟 Service/任务；Gateway、Collector | Modbus 输入变化 → Task start → ReadPoints 和任务采样 → stop | 读数、缩放、质量与定时发布分别可证 |
| I-03 NATS 双通道 | Collector/Gateway + NATS/JetStream | 订阅实际 Subject → 启 Task → 抓双批 → stop | SampleBatch、设备/点身份、源时间、Realtime/Reliable 语义 |
| I-04 Sink 四目标 | Sink 测试 Service、隔离 Sink/Delivery + Gateway/Redis/Kafka；Sink、目标 Gateway | 固定标准 SampleBatch 注入真实 NATS → Delivery start → 四目标读回 → stop | Gateway 更新、Modbus 寄存器、Redis 最新值、Kafka 历史分别断言 |
| I-05 工业仿真控制 | Gateway/Commander/Generator 仿真配置；三进程 | 工业 Client FC06/FC16 → Commander → Gateway 执行 → Generator 查询 → 重复/无权测试 | 同 command_id、不直接转发、PENDING→APPLIED/UNKNOWN，受理非成功 |
| I-06 CLI 生命周期 | CLI 有效 Service + 六业务服务身份 | start → status/READY → restart → stop | Unit/PID/业务状态和 Sink Worker 退出 |
| I-07 Gateway Realtime | Sink Gateway 模板/映射 + 独立目标 Gateway；两进程 | 受控令牌 ACQUIRE → UpdateVariables → ReadVariables → RELEASE/过期 | 值与映射正确，租约及回环隔离 |
| I-08 配置治理 | CLI 全量根目录、Configuration、各业务 Service | 修改完整 configs 根目录并通过 CLI publish，注入版本冲突、无效路径、旧结构和非法实例键 | 完整候选拒绝且原目录视图不变；正确候选只广播修订，各模块独立拉取生效 |
| I-09 全 RPC | 当前全部 Proto 和服务，含 Storage | 实际枚举 Descriptor，调用真实 RPC 与拒绝路径 | 逐方法生成分母，Storage 9 项专属 RPC 不可遗漏；旧 51/51 不再作为固定分母 |
| I-10 Storage 真正落地 | CLI 启动 Storage，Kafka JSON v3 和真实 TDengine | 逐等级注入真实 Record、等确认后查询 Storage RPC/SQL | 三等级数据、身份、时间、质量、类型、值一致，Offset 安全 |
| I-11 Storage 恢复 | 同上，多个历史点表与 Kafka Partition | 重复消费、冲突、TDengine 故障、重启 Storage | 不丢不重、旧版保真、未知写不提前 Commit |

I-04 的标准批次 Publisher 是**分段激励**；只有 E2E-07 通过完整 Generator→Collector→Sink 路径，才能确认全链路结果。

# 7. E2E 完整场景

## 7.1 通用执行步骤

1. 核实 Git HEAD、所有现行 Proto（含 Storage）的生成一致性、配置哈希、CLI 命令清单、调用方令牌权限、测试目标和端口。
2. 中间件准备器启动 NATS/JetStream、Redis、Kafka、TDengine；通过 CLI 启动 Configuration 和全部业务进程（含 Storage），验证 PID/READY，再启动 Generation、Gateway、Collector Task 和 Sink Delivery。
3. 对选定模型实施确定性激励，收集 Generator JSONL、Gateway、Collector、NATS/JetStream、四个 Sink 目标及 Storage/TDengine 最终读回。
4. 对控制场景记录 Commander 批次 request_id、子命令 command_id、Generator generation_id、effective_step 和查询结果；不得用准入代替完成。
5. 按第 9～10 章分别判断数值、时间、可靠性、安全与恢复；保存证据，CLI 清理业务进程，中间件管理器清理测试资源。

## 7.2 E2E 场景矩阵

| 用例 | 配置与必需进程 | 激励与执行 | 独立输出判据 |
|---|---|---|---|
| E2E-01 WTG | 五设备仿真、WTG 物理 Scenario、六业务服务身份及四目标 | 平均风速 10→12 m/s，湍流度 0；多步运行 | 四目标和源记录可追溯，不假定阶跃立即稳态 |
| E2E-02 PV | 同上 PV Scenario | 辐照 0→800→1000 W/m²，温度条件固定 | PV 容量边界、单位与四出口一致 |
| E2E-03 BESS | BESS Scenario | 充电/放电阶跃、SOC 边界、越界请求 | 状态连续、非法输入拒绝、正负号/缩放正确 |
| E2E-04 GRID/PPC | GRID/PPC、Commander、Gateway、Generator | 频率/电压激励、AGC/AVC/FFR 合法命令、重放/无权 | 真正 APPLIED 才为控制成功，失败/未知不冒充；测量反馈一致 |
| E2E-05 随机/CSV | DG Service 合法 Scenario、`history/demo.csv` | 固定种子重复运行、同 CSV 重放 | 原始序列可复现、实际已发可靠批顺序一致 |
| E2E-06 多速率 | DG 物理 Scenario + Collector 合法周期 | 生成 50/100 ms、采集 50/100/200/1000 ms 候选组合 | 实测采样窗和覆盖；不要求每一步必采 |
| E2E-07 四种 Sink | 六业务服务身份、所有隔离 Sink/Delivery、Gateway/Modbus/Redis/Kafka | 单次序列与阶跃，正常和故障恢复两轮 | OUT-01～04 分别裁决；任何缺口显式保留 |
| E2E-08 CLI/RPC | 全部服务及 CLI | 逐叶子命令和逐 RPC 的正常/失败路径 | 全量覆盖清单和真实副作用证据 |
| E2E-09 生命周期 | CLI 有效测试配置 | 全服务 start/stop/restart、重复调用 | 进程/业务状态独立判断，Worker 无残留 |
| E2E-10 故障与恢复 | 同上 + Storage/TDengine | 断 NATS、Kafka、Redis、Gateway、TDengine，旧 generation 与重复 ID | Realtime 覆盖、可靠重投、Storage UNKNOWN/Offset 阻断与恢复 |
| E2E-11 三级入库 | 全链及 Storage、TDengine、历史点表和三级 Delivery | 确定性 DG 输出，经 Collector/Sink/Kafka 入库 | RAW/PROTOCOL/BUSINESS 分级验收，缺真实 RAW 时不得伪造 PASS |
| E2E-12 Storage 一致性 | Kafka/Storage/TDengine、多设备和版本 | 重复、同键异值、进程重启及旧点表重放 | 无错误覆盖、无重复、历史稳定、Offset 连续确认 |

## 7.3 四个 Sink 目标分别验收

| ID | 通道与目标 | 实际输入和独立读回 | 必须验证 |
|---|---|---|---|
| OUT-01 | Realtime → Industrial Gateway | 正式 GatewayOutput、内网访问授权 `ClaimTargets/UpdateVariables` → Gateway ReadVariables | target_mappings、租约、权限、值更新及目标与源隔离 |
| OUT-02 | Realtime → Modbus TCP | Sink Modbus Server → 独立 Modbus TCP Client | 寄存器地址、类型、缩放、大小端和最新值；非主动写第三方 Server |
| OUT-03 | Realtime → Redis | Sink Redis 写入 → 独立 Redis Client | 来源标识、时间与最新值覆盖 |
| OUT-04 | Reliable → Kafka | JetStream→Sink Reliable→Kafka → 独立 Consumer | 传输阶段、ACK 与重投；不能单独证明最终持久化 |
| OUT-05 | Kafka → Storage → TDengine | CLI 启 Storage，真实 Kafka Consumer 与 TDengine，正式 QuerySamples 和独立 SQL | 等级/类型/值/身份/时间/质量、历史版本、Offset、幂等和恢复独立核实 |

四个 Sink OUT 的 PASS 不可互相代替；完整数据落地 PASS 还必须通过 OUT-05。工业控制 E2E 作为 E2E-04 单独裁决，不与 Gateway 测量注入混为一谈。

## 7.4 严格 CLI 独立进程全链验证

整个场景使用独立业务服务身份：Industrial Gateway、Data Generator、Realtime Collector、Reliable Collector、Sink Service、Commander、Configuration Service、Storage Service。Sink Main 自行管理 Realtime/Reliable Worker；各业务进程的启动、停止、重启仅通过正式 CLI 的 Service 管理命令完成。NATS/JetStream、Kafka、Redis、TDengine 由独立测试基础设施管理器启动。全部业务命令经 CLI，未提供 CLI 操作的正式 RPC 经独立认证客户端覆盖并计入 RPC 清单。

执行必须保留独立证据：① CLI 与 Linux 进程 PID/READY；② 两个 Collector 分别对应 Realtime、Reliable 的真实 SampleBatch 和 JetStream PubAck；③ Generator 真值 JSONL 和 Gateway/Collector 采集值；④ Industrial Gateway、Modbus TCP、Redis、Kafka 四出口及 Storage QuerySamples/TDengine SQL 的独立读回；⑤ Commander request_id/command_id 与 Data Generator effective_step 的控制完成结果；⑥ restart 后已提交配置与业务运行状态一致、消息积压收敛；⑦ stop 后无遗留业务进程、连接或活跃 Worker。

安全场景必须采用正式 内网访问授权 与权限约束，验证 Collector 管理令牌、Gateway/Generator 令牌授权身份、租约目标授权和 Commander 写控制。业务进程能够启动、某一 RPC 返回 OK、NATS 发布成功、Kafka ACK 或 Golden Data 仅局部匹配，均不得代替完整链路的独立 E2E 裁决。每个 E2E ID 都须包含 CLI 命令、日志、配置哈希、源值、四出口、Storage/TDengine 最终读回以及分等级状态证据。

## 7.5 各完整场景的配置、激励、独立读回与步骤

本节所有场景**共用第 5 章生成的实例、点表、型号、Service、Sink 与 CLI 配置**，仅每个场景的激励/运行时参数变化。物理设备均为软件仿真实例 `WTG_SIM01`、`PV_SIM01`、`BESS_SIM01`、`GRID_SIM01`、`PPC_SIM01`，不引用现场硬件。启动过程统一为：独立 Docker 中间件和 TDengine → CLI 启 Configuration、Gateway、DG、双 Collector、Sink Main、Commander、Storage → CLI 验证各自 READY → CLI 启动 Collector Task、Generation、Sink Delivery → 独立客户端读回 → CLI 停止与回收。每个场景至少有一份 E2E 专用测试，不得借模块测试替代。

| ID | 配置 / 进程 | CLI 激励与顺序 | 独立输出与裁决断言 |
|---|---|---|---|
| E2E-01 WTG | WTG physical Scenario、`devices_sim.yaml`、WTG 点表；上述六服务 | 平均风速 10→12 m/s、TI=0，各段采集多步；CLI Start/Stop Generation | DG Recording JSONL 风速及功率、Gateway 变量、Collector 原始值、四出口。风速恒定段误差按编码精度比较；功率按物理模型且允许动态过渡 |
| E2E-02 PV | PV physical Scenario、`pv_sim_physical` 型号 | 固定气温，辐照 0→800→1000 W/m²；每段新 Scenario/Generation，独立录制 | 组件温度、直流/交流功率边界，Gateway/Collector/OUT-01～05 与真值同身份、同单位、同合理时间窗 |
| E2E-03 BESS | `bess_sim_physical`、BESS 点表及限值 | 通过 CLI 更新允许的充放电指令，跨多个步长至 SOC 边界，再拒绝越限输入 | SOC 单调性、储能变化与功率符号、无越界、拒绝请求不改变状态；四出口与 Storage 最终历史记录均对应同一有效控制步 |
| E2E-04 GRID/PPC | GRID/PPC 两物理模型；Commander 权限和 Gateway 控制路由 | CLI 频率/电压扰动、AGC/AVC/FFR 合法与非法控制，查询请求完成状态 | `request_id`、`effective_step`、执行反馈；只有 APPLIED 才视为闭环成功，UNKNOWN 不冒充成功 |
| E2E-05 随机/CSV | `history/demo.csv`、Replay Scenario、固定种子 Scenario | 两轮同 seed 运行并比对，同一 CSV 按 1x/2x 顺序重放；停止重启后再取值 | DG 真值逐点序列与 replay 顺序一致；Reliable 已提交批次顺序不逆转；不同运行 ID 不混淆 |
| E2E-06 多速率 | DG `interval_seconds`=0.05/0.1，Collector Task `interval`=0.05/0.1/0.2/1.0 | CLI 分别应用合法版本配置并重启/生效，固定同一确定性激励 | 统计实际时间戳、容许抖动、样本数量与丢弃/覆盖语义；不得假设 Collector 每个生成步必采 |
| E2E-07 四出口 | 同一个全链批次，`gateway_output` / `modbus_output` / `ems_power` / `power_history` | CLI 启/停四 Delivery，正常运行、断连恢复各一次 | OUT-01 Gateway 变量与模拟 Modbus；OUT-02 真实 Modbus 目标寄存器；OUT-03 Redis 键/值；OUT-04 Kafka topic/分区及可靠 ACK；OUT-05 Storage/TDengine 实际入库并独立 PASS |
| E2E-08 CLI/RPC | 当前 CLI YAML + 六 Proto、真实访问令牌与隔离网络 | 从 CLI 实际注册树动态枚举全部叶子逐项执行/合法拒绝；从 Proto Descriptor 动态枚举逐服务全部正式 RPC 与错误码检查 | 每条命令/方法绑定 EXIT/RPC 状态、关联对象及独立副作用；没有实际执行的保留在分母 |
| E2E-09 生命周期 | CLI service manager 对六进程的合法 unit；Sink Main 管理双 Worker | start→status/READY→restart→status→stop；重复启停和服务单侧失败 | 每个 PID、READY、退出码、Worker 清退、无残留连接/僵尸进程；进程状态不代替业务就绪 |
| E2E-10 故障恢复 | 隔离中间件、TDengine 与 Gateway/DG/Collector/Sink/Storage | 正常基线后逐项断 NATS/Redis/Kafka/Gateway/TDengine 并重启 | Reliable 重投、Realtime 覆盖、Storage Offset/UNKNOWN、配置与控制幂等；最终逐键落库对账 |
| E2E-11 三级最终落地 | Storage、TDengine、三等级 Delivery 与固定点表闭包 | CLI 启完整链，三级分别派发、入库与查询 | PROTOCOL/BUSINESS 真实值准确；RAW 必须有来源证据，各等级单独裁决 |
| E2E-12 存储持久性 | Kafka/Storage/TDengine、历史点表 v1/v2 | 同内容重放、同键异值、写确认丢失及 Storage 重启 | 不重复、不覆盖、连续 Offset、SQL 与 QuerySamples 一致，历史值不漂移 |

**比较规则：** 每条数据必须带有设备 ID、点 ID、时间戳、质量与采集/生成身份。对不断变化的物理量，按已保存真值的时间窗匹配而不是将最新采集值与录制文件最后一步直接相减；对 TI=0 固定风速可以进行确定性绝对误差判定。误差阈值由点表的 scale、数据类型与协议编码精度确定并在用例中显式记录；不允许为使测试通过而临时调大阈值。

**证据规则：** 每个 case 保存 HEAD、配置哈希、CLI 退出码、PID、真值路径、Gateway/Modbus/Redis/Kafka、Storage QuerySamples、TDengine SQL 结果、Offset 水位及清理结果。单条 Redis 成功只证明 OUT-03，不证明四出口；WTG/PV/BESS 模块测试成功只可标注 MODULE_PASS，不得升级为 FULL_CHAIN_PASS。

## 7.6 Storage 最终落地专项场景

Storage 是完整链路的持久化终点，不是 Sink 的第五个适配器。Kafka ACK 仅证明传输，不能证明 TDengine 入库。Storage 必须在已验证 DML/冲突检查及连续 Offset 前缀后推进 Kafka Commit。正式 QuerySamples 是查询入口，独立 SQL 验证物理记录。

| ID | 真实执行激励 | 验收断言 |
|---|---|---|
| ST-E2E-01 首次落库 | CLI 启 Storage，Kafka 首次收到合法业务 Record | 库、超级表、子表按配置创建；Storage QuerySamples 与 SQL 值、类型、身份、时间一致 |
| ST-E2E-02 三级数据 | 同一源事件按三个独立 Delivery 选择 raw/protocol/business | 逐等级验证值、身份、质量、时间；同来源同时间三个等级不互相覆盖；无真实 RAW 来源不可伪造 PASS |
| ST-E2E-03 类型精度 | INT64/UINT64/DOUBLE/BOOL/TEXT/BYTES、缺失值及非法值 | value_kind 与 oneof/payload 保真；缺失不当零，浮点以有效量化误差为准 |
| ST-E2E-04 重放和冲突 | 同设备/Schema/ts 重投同内容，再按更大 offset 投不同内容 | 同内容不增行；后到完整覆盖（含 NULL），不提前提交 Offset |
| ST-E2E-05 故障收敛 | TDengine 写前/写后断连、Kafka Commit 丢确认、Storage 重启、Consumer Rebalance | UNKNOWN 回读核验，只有连续已确认 Offset 提交，新旧 Consumer 代际隔离 |
| ST-E2E-06 历史版本 | PointTable v1/v2 切换后重放 v1、Configuration 暂时离线 | v1 历史闭包和 BUSINESS 值不漂移；缺可信闭包时阻断而非使用最新版本 |
| ST-E2E-07 查询聚合 | UTC [start,end)、分页、设备/点表/等级查询、COUNT/MIN/MAX/AVG | 逐页读全量、不跨等级/单位/版本自动聚合；返回样本与 SQL 一致 |
| ST-E2E-08 导出管理 | CLI 操作 Storage 导出/备份/阻断分区重试及任务状态 | 输出文件与样本一致，权限/配额有效；备份能力未具备时保留 NOT_RUN，不冒充 CSV 备份 |

完整落地验收要求独立登记每个等级的 PASS/FAIL/NOT_SUPPORTED/NOT_RUN。仅现有等级通过时可声明分等级通过，不得声称三个等级全部具备真实端到端证据。

# 8. CLI 与 gRPC 全功能覆盖

## 8.1 自动枚举并生成测试矩阵

历史统计为 **51 个服务限定 Unary RPC 声明（不含 Configuration 与 Storage）**，现行完整分母必须按全部 Proto Descriptor 动态枚举：Common 9、Collector 11、Commander 8、Sink 4、Gateway 10、Generator 9。Commander 包括 `GetRequestResult`，不得漏计。Common 在多个服务实例中分别注册，唯一方法总数与逐服务注册总数不能混用。Gateway 的 `ExecuteSimulationCommand/GetSimulationExecutionResult`、Generator 的 `GetInputExecution` 必须进入 RPC 覆盖清单。除真实调用外还要验证 Descriptor 请求/响应、字段号、标量 oneof、认证与拒绝语义。

CLI 先读取当前 `wind-hub --help` 与全部子级帮助，枚举**所有叶子命令**并逐条真实执行，所有业务操作必须通过 CLI；每项至少一项合法或规范拒绝场景。所有未执行 RPC/CLI 均保留在分母。无法由 CLI 覆盖但正式公开的 RPC 可用真实认证客户端调用，注明原因。

当前严格 CLI 全链用例 `tests/cli/test_r1f3_strict_cli_e2e.py` 仅能作为 E2E-07/E2E-09 等场景的部分共享证据：它不等于 E2E-01～10、I-01～09 全覆盖。当前严格 CLI 测试已向 Collector 进程及 CLI 子进程配置一致的真实管理令牌，并验证正确、错误和缺失令牌；仍须在最终 HEAD 上复测，不关闭认证或记录令牌。`tests/system/e2e/full_chain/` 仅有包初始化文件，完整场景证据仍应按本设计逐项补齐。

~~~text
cli_command_id,syntax,service,rpc_methods,case_id,config,expected_exit,observed_exit,status
rpc_service,rpc_method,request_type,response_type,case_id,expected_status,observed_status,status
~~~

## 8.2 功能分组

| 组 | 必测内容 |
|---|---|
| CLI 本地 | Shell/help、参数解析、配置快照与差异、网络扫描和探测 |
| CLI 服务管理 | service list/start/stop/restart/status/info/capabilities 等以现行帮助为准，PID/READY 分开 |
| Common | 状态、能力、配置 Push/Apply/Get、指标、事件、诊断共 9 方法 |
| Generator | Devices/Scenarios/Generation、变量读写、请求级执行结果及身份隔离 |
| Gateway | Server、变量与租约、注入、仿真执行/查询、版本栅栏 |
| Collector | 设备/点表/任务、主动读取、运行发布和状态 |
| Commander | 单条/批量提交、子命令、身份、幂等、结果查询与 UNKNOWN |
| Sink | Delivery 管理、双通道、四种出口、进程/指标 |
| 架构契约 | Core 共享配置与业务配置分层、非正式顶级包/兼容实现残留、正式 Proto 生成物 |

## 8.3 用例必要信息

每个 CLI 用例记录实际语法、配置及哈希、前置业务进程、激励、对应 RPC、退出码/响应、独立读回、清理和证据文件。禁止虚构未在 CLI 中注册的参数、功能或响应字段。

# 9. Golden Data、采样率与输出比较

## 9.1 当前已实现的 Data Generator Recording

本分支 `services/data-generator-sim.example.yaml` 已包含：

~~~yaml
recording:
  enabled: false
  directory: artifacts/data-generator
~~~

这是**现有默认关闭的录制能力**，不再作为待新增 API。当前 Runtime 将模型生成的不可变源批在 Gateway 派发前写入 JSONL：字段为 `generation_id`、`sequence`、`produced_at`、`points[]`（包含 `device_id`、`point_id`、`source_time`、`value`）。不得假定现有 Recording 包含 `run_id`、`quality`、`model_step` 或 flush ACK 等未定义字段；测试须独立核对文件写入完整性与生成结束后的落盘状态。录制缺口导致 INCONCLUSIVE/FAIL，不能用 Gateway 读回自证源真值。

## 9.2 多速率实验

| Generator 步长 | Collector 周期 | 核验目标 |
|---:|---:|---|
| 50 ms | 50 ms | 源与采集允许时差、Realtime 最新值 |
| 50 ms | 100 ms | 以实际采集记录核对 Kafka 应交付数 |
| 50 ms | 200 ms | 不要求每个生成步均出现在下游 |
| 50 ms | 1000 ms | 最新值覆盖与慢采样 |
| 100 ms | 50 ms | 允许读取同一模型输出多次，按真实样本身份判断 |

周期均为待验证组合，必须是运行时和配置的合法取值。应以真实 `source_time`、采集时刻、样本 ID、源序列匹配，不做机械整数倍抽样。

## 9.3 内容与可靠性比较

- Gateway：按映射和单位校验寄存器/变量读回，区分模型计算步与最新值。
- Collector：主动 ReadPoints 与后台采集任务分开记录；质量和身份按实际 SampleBatch 语义比较。
- Realtime：校验 Redis/Gateway/Modbus 的最终最新值和允许覆盖，不以每步历史无丢失验收。
- Reliable：以**实际 JetStream 受理**的源批为责任边界，验证 Kafka 消费、重投和下游去重，不声称全系统恰好一次。
- 浮点容差考虑编码、缩放及模型精度；整数、布尔与枚举逐值比较；各阶段时间延迟分开统计。

## 9.4 全量目录快照、广播与模块独立生效专项

**运行前提**：中间件和数据库由测试代码独立启动；所有业务模块和 CLI 命令均通过正式 CLI 作为进程管理/操作接口启动或执行，不使用测试直接调用 Application 层冒充 E2E。测试所用 `configs/` 是单一共享测试目录，包含真实和仿真设备实例、Service、PointTable、BusinessPoint/Units 与所有被引用的历史版本。

| ID | 配置与进程 | 执行步骤（全部经 CLI） | 独立可验收证据 |
|---|---|---|---|
| CFG-01 首次全量发布 | 干净 Configuration SQLite、完整 configs 根目录；Configuration/CLI | `config publish --root <root>` → 查询版本与目录 | SQLite 当前完整成员、所有身份/版本/哈希、revision；未遗漏文件 |
| CFG-02 再次全量发布 | 新目录新增、改变、移除合法配置文件；全部业务进程 | 原命令再次提交全目录 | 当前成员集整体替换，新旧差异正确；未变化版本复用；未变化目录重复提交无新 revision |
| CFG-03 历史共存 | 同一配置身份 v1.0.0、v1.1.0 并存；明确引用 v1.0.0 | 完整发布后重新查询不同版本 | 不凭版本号自动升级；当前引用确定；历史对象仍可按 ID/版本查询 |
| CFG-04 非法全集 | 缺少被引用文件、重复身份同版本异内容、路径穿越、非 YAML、Unary 消息超出容量、缺失引用 | 完整候选上传、validate、publish | 校验问题可定位；**旧完整成员视图完全不变**；没有部分提交 |
| CFG-05 CAS 并发 | 两个 CLI 模拟同一起始 revision | 发布 A 后旧基线提交 B | B `ABORTED`，不能静默覆盖 A；幂等重试同候选不产生第二修订 |
| CFG-06 广播 | Configuration + 独立订阅的 Collector/Sink/Commander/Gateway/Generator | 发布 revision r，抓取 `wh.config.changed` | 消息仅含 `revision_id`；每个独立订阅收到通知；**不使用共享 Queue Group** |
| CFG-07 一模块离线 | Gateway 进程停止，其他模块在线 | 发布新根目录，观察所有业务进程 | 配置库成功且其他模块正常完成拉取/生效；Gateway 离线不阻断 |
| CFG-08 NATS 不可用 | 发布时中断 NATS，随后恢复 | SQLite 成功提交、重连 NATS，模块周期对账 | 不回滚已发布修订；广播失败可观测，模块最终收敛 |
| CFG-09 丢通知/重启 | 订阅之后制造通知丢失；重启模块 | 启动先订阅再查修订、重连和周期检查 | 不依赖瞬时通知成功；`seen/downloaded/effective` 状态可核对，错序不会回退 |
| CFG-10 完整闭包/安全应用 | PointTable/BusinessPoint/Units 多版本和 Service/实例依赖 | 修改有效引用并广播；注入坏哈希及应用异常 | 模块完整闭包、固定修订与哈希一致；失败保留旧有效快照；热更新/重启语义正确 |
| CFG-11 启动引导 | 最小引导文件 + 当前配置库、合法缓存与坏缓存 | 首次启动、Configuration 停机、恢复上线 | 无合法配置不报告 READY；有合法缓存按模块定义降级；恢复后从权威修订对账 |

Configuration 发布成功与每个业务模块的实际 `effective_revision` **分开判定**，不得由 CLI 单次成功退出推定全部模块已更新。测试要覆盖完整根目录物理扫描/Manifest 的路径与哈希、按设备固定历史 PointTable 语义、不因配置变更篡改已采集 SampleBatch。每个场景需保留 CLI 退出码、数据库状态、实际 NATS 消息、模块 gRPC 状态、日志及独立输出证据。

## 9.5 Contracts 数据包与 Sink 三级交付的真实链路验收

测试按 Contracts §11.6 及 §11.9 的**唯一正式线格式**生成 Golden Data 并从真实 NATS 和下游目标独立读取，不得用测试私有 JSON 替代。目标输入采用 SampleBatch v4，Kafka JSON v3；Redis 单独按其既有目标契约验证；用独立真值及 Broker/Storage 观察验证而非沿用旧 v2/v1 断言。

| 验收项 | 激励及配置 | 独立观察与断言 |
|---|---|---|
| Collector Realtime | 固定设备实例及点表，CLI 启动多测点采集 | NATS Subject 与模式、场站、device_config_id、device_id 一致，公共采样时间，原始协议标量未做 scale/offset，严格契约 |
| Collector Reliable | 同上，JetStream Durable 消费 | JSON 字段集、`schema_version=3`、样本质量/时间不变；无 `max_staleness_ms`；重放 `sample_id` 保持稳定 |
| RAW/PROTOCOL/BUSINESS | 三个 Delivery 分别显式设置 `output.data_level`，使用相同来源样本及固定历史闭包 | PROTOCOL 保留协议解码来源值，BUSINESS 严格按历史 PointTable/BusinessPoint/Units 标准化；RAW 必须提供可验证的真实来源载荷，来源不具备时显式标记 NOT_SUPPORTED/NOT_RUN；遗漏等级必须拒绝，不存在默认 BUSINESS |
| Kafka/Redis | 启动真实目标，独立 Client 读取 | Kafka JSON v3 设备批、固定设备配置身份、等级、统一时间、源测点 value/quality、Key/Topic 与 ACK；Redis 独立按自身契约检查 |
| Modbus/Gateway | 单独启用 Realtime Delivery，读取实际镜像/变量 | 输出地址编码和点位映射正确；不要求目标寄存器传输 JSON 元数据；保留 Gateway 租约和目标隔离 |
| 格式拒绝 | 注入未知 `schema_version`、缺 `point_table_id`、重复键/测点、非法时间、NaN、Realtime 缺 TTL、Reliable 带 TTL | 严格拒绝，不能自动补全或静默降级 |
| 历史版本 | 先按 PointTable A 采样，发布与之业务解释不同的 PointTable B，然后重放旧消息 | A 的 `point_table_id` 和输出值不改变；不查询最新 B 代替；缓存冷启动及 Configuration 断联路径可复验 |
| 标准点表 | 真实/仿真同类设备与两种型号，共享 BusinessPoint，配置重复映射作为负例 | 逐点业务身份一致，重复/未知 BusinessPoint 被拒绝，单位换算不二次应用 |
| Storage 三级落地 | 将真实 Kafka RAW/PROTOCOL/BUSINESS v3 设备 Record 交给 CLI 启动的 Storage Consumer | 三等级分别按 Storage 枚举查询及 SQL 核对；恢复设备、固定点表、时间、值和质量；不创建伪 `data_version` 或 `config_revision_id` 解释依据 |

各场景均遵守第5章真实进程/CLI 启动方式，消息中间件及数据库由测试代码单独启动；完整 CLI 与 RPC 覆盖不因新增线格式测试而减少。非正常消息与下游失败必须保留单 Delivery ACK/重试/死信证据；高吞吐条件下分别统计输入、转换与 Adapter 发送阶段。

## 9.6 三等级数据与 TDengine Golden Data 对账

按场站、设备、固定 PointTable 版本、物理 Schema、等级、统一 sample_time 匹配完整行。独立 oracle 解析 Device Instance 历史闭包及列版本、类型、单位、Quality 位序。同目标键按同 Partition 较大 offset 确定最终值；同内容重放不增行、异内容覆盖（含 NULL）。独立读取 SQL 与 Storage RPC，RAW 对照真实载荷，PROTOCOL 对照解码值，BUSINESS 对照独立计算标准值。逐值、质量、时间核对；整数/布尔/文本/bytes 精确、浮点预先声明容差。统计 source、device_records、broker_confirmed、rows、points、offset、lag；missing/unexpected/value/quality/identity mismatch 均为零。确认不等于 producer submission。实际列与表身份直接读回核验。

## 9.7 安全认证复用与负载边界

验证无效访问令牌拒绝、角色和资源级权限拒绝、Configuration 历史配置查询最小权限；重复高频采样不触发逐点令牌鉴别或复杂安全握手。分别统计 RAW、PROTOCOL、BUSINESS 三个等级的端到端吞吐（RAW 不具备可验证来源时不计为成功）、转换 CPU 和 P99，不能只通过纯编码单测宣称满足 30 台风机的既定容量目标。

# 10. 性能、故障与安全验收

## 10.1 性能

功能验收通过后逐步扩展设备与速率，分别记录 Generator 计算、Gateway 注入、Collector 采集、NATS/JetStream 发布、Sink 两 Worker、四目标的值/s、批/s、P50/P95/P99、CPU/RSS、积压和覆盖/丢弃。Gateway 租约续约、每 Delivery 在途上限与模型完成步查询单独测量。历史其他环境的性能成绩不能作为本次 E2E PASS 证据。

## 10.2 故障、安全与恢复

验证真实服务重启和 READY 假阳性、非法/过期访问令牌、越权操作角色、Gateway 租约失效、配置 revision 冲突、Generation 切换、同 request_id 异内容、Commander 断连 UNKNOWN、Modbus 非法地址及错误标量、NATS/JetStream/Kafka/Redis 中断与恢复、记录文件损坏与隔离资源回收。每个故障有前、中、后状态及真实副作用证据。仅 `accepted/effective_step` 时不可确认 APPLIED，必须通过 Gateway/Generator 请求级查询取得完成证明；不改变 request_id 盲重试。

## 10.3 BusinessPoint 标准和 PointTable 全覆盖验收

在明确版本选择的 `configs/templates/business_points_standard_vX.Y.Z.yaml` 和所有 `configs/templates/point_tables/point_table_*_vX.Y.Z.yaml` 上执行实际 Core/Configuration 校验。所有 Point 必须**显式**填写 BusinessPoint；展开 PointTable 继承后，任何重复 `business_point_id` 必须拒绝发布。同一物理量在同类设备不同型号及仿真/真实场景中的标准业务 ID 与标准单位必须一致；不同叶片、相别、测点位置、反馈和设定值不可无区分地共用 ID。检查已知 GW2MW ADS 点表多点映射同一业务 ID 的案例，禁止直接跳过或以测试代码补默认值。

输出可审计的全量矩阵 `device_type/device_model/point_table_id/point_id/business_point_id/source_unit/standard_unit/data_type/semantic_decision`，针对未核实 PLC 符号单列现场待核验项，不得将猜测值视为已通过 BUSINESS 级业务验收。验证不存在多余的 `sim_` 仅因仿真来源产生的业务别名，真正仿真内部状态允许单独身份；同时检查所有配置引用、默认缺失回退、同身份不同历史内容冲突。缺覆盖映射或业务定义冲突时标记 FAIL 或 NOT_VERIFIED，不得裁定全链路 BUSINESS PASS。

### 10.3.1 GW2MW 一对一候选配置的验收分母（v0_42）

| 验收对象 | 必须检查 | 设计阶段状态 |
|---|---|---|
| GW2MW 111 个源 Point | 源 ID/ADS Symbol/单位/类型/地址及读写属性保持；111 个业务身份互异 | 已形成候选配置，未做现场验证 |
| BusinessPoint 标准目录 | 293 个旧定义内容不变，追加 107 个候选定义；类型/标准单位与来源保持兼容 | 文件已生成；未作正式发布验证 |
| 精确版本链 | BP 1.5.0 + Units 1.2.0 → GW PointTable 1.2.0 → GW Model 1.5.0 → Device Instance 1.6.0 → Deployment prod-a 1.6.0 | 候选，不可激活 |
| 历史语义 | PointTable v1.1.0 仍按原 BusinessPoint 标准闭包解释；无法获准时不得静默用 v1.2.0 替代 | 待运行时验收 |
| 不明 OEM 字段 | 每个候选业务身份需查实际 PLC 工艺语义，不以自动生成的唯一名称视为已批准 | PENDING_OEM_SEMANTICS |

本轮只修订设计文档与 `configs/` 候选，不执行测试。实际进入联测阶段时必须另外执行配置精确引用闭包、仓库发布、业务 Golden Data 和历史版本重放。任何待核定 Point 不能通过默认选择新业务目录绕过验收。

## 10.4 Storage 性能、故障与落地容量

按 Storage v1_13 §14 执行 F01–F22、TD01–TD12。单等级 P1 10×500×10=50000 点/s、100 行/s；P2 12×500×10=60000 点/s、120 行/s；P3 30×500×10=150000 点/s、300 行/s；三等级完整同时写入为450000点/s、900行/s。每工况至少60s，最高满足工况至少600s；保留10分钟及60分钟长稳目标。单实例，不做多实例容量。裸库、Kafka→Storage、Sink→Kafka、全链路分段；稳定计量窗口实际发布≥目标100%，lag 不持续增长，最终无缺值、错值；记录CPU/RSS、IO、压缩、延迟、批大小、broker ACK、数据库确认/commit。真实500不同点及变化值，不重复同点。

TD01 裸库/taosBenchmark、TD02 STMT2 多表、TD03 Statement 复用、TD04 vgroups 2/4/8、TD05 buffer 128/256/512 MB、TD06 批量阈值、TD07 WAL/故障恢复、TD08 NULL覆盖、TD09真实压缩、TD10查询并发、TD11 500点列/行宽与 VARBINARY、TD12参数回读。WAL_LEVEL=2/WAL_FSYNC_PERIOD=0 可靠基线，不允许关闭 WAL 取得性能；只保留显著收益调优，不擅自降额认定 READY。

覆盖首次/新设备/升级建表、未知设备精确拉取、历史回放、物理类型映射边界、后到覆盖、部分成功/UNKNOWN底层 I/O、重平衡、commit ACK 丢失、重启、背压。实际表/列/行及连续 Offset 独立核对；不操作非测试库。真实服务 CLI 启停，中间件由测试启动；三级数据独立证据，未执行 NOT_RUN。

# 11. 测试执行、目录、Marker 与报告

## 11.1 共享测试资产

~~~text
tests/src/
  integration/                  # 分段真实服务测试
  e2e/
    scenarios/                  # 激励/断言，不重复 Service YAML
    fixtures/                   # CLI 管理、中间件准备、认证读回
    assertions/                 # Golden Data 与多出口比较
configs/                        # 唯一共享配置资产
tests/.runs/<run_id>/            # 非版本化运行证据
~~~

示例路径只表示归属，实际必须复用已有测试结构，不因目录形式重复创建 Fixture/测试。Integration 与 E2E 共用 CLI 启动器、配置源与比对组件；Marker 采用项目已注册的 integration、system、end_to_end、safety、reliability 等。

## 11.2 静态与执行门禁

逐次实际执行并记录退出码：`python scripts/generate_rpc.py --check`、`ruff check src tests scripts`、`ruff format --check src tests scripts`、`mypy src`、`lint-imports` 及相关真实测试套件。未执行项目必须为 NOT_RUN；独立测试方负责测试及 Fixture，生产代码缺陷返回生产开发处理，不通过 ignore/skip 隐藏错误。

## 11.3 证据格式

~~~yaml
case_id: E2E-11-ST-E2E-02-BUSINESS
branch: feat/production-readiness-final-20261123
commit: "<verified_head>"
config_files:
  - deploy/config_roots/e2e-final/instances/device_e2e-final_v1.0.1.yaml
  - deploy/config_roots/e2e-final/templates/deliveries/delivery_reliable_history_v1.1.0.yaml
config_hashes: "<actual_hashes>"
processes_started_by_cli: [configuration, gateway, data-generator, collector, sink, storage]
infrastructure: [nats, jetstream, kafka, tdengine]
input: "fixed SampleBatch sequence"
expected: "all expected BUSINESS records independently verified in TDengine"
actual: "<run_dir>/tdengine-readback.jsonl"
status: NOT_RUN
reason: "template only; not executed"
~~~

# 12. 准入、裁决及实施缺口

## 12.1 完整 E2E 运行前置

1. 各模拟 Service 配置合法且同机端口不冲突；核对现行 Sink `50053`、Generator `50061`、CLI 端点、内网访问授权 身份、Gateway/Commander 路由及测试动态端口。
2. Collector 模拟任务、Sink 四目标实例和 Delivery 与 `devices_sim.yaml` 使用同一套正式模板/点表；不覆盖真实现场设备或另设非法 Generator Instance。
3. Sink GatewayOutput 租约/权限/映射可测试，独立 Modbus 客户端可读 Sink Server；四出口逐项证明，另需 Kafka→Storage→TDengine 独立读回。
4. Generator Recording 源批可验证；Storage 的 QuerySamples、QueryAggregates、GetStorageStats 和 TDengine 独立只读 SQL 可用；缺真值或最终读回不得 PASS。
5. 包括 Configuration 和 Storage 的全部业务进程经正式 CLI 管理；Realtime/Reliable 使用独立 Collector 身份，Sink Main 自行管理 Worker。业务状态及下游副作用不得以单进程集成测试冒充。
6. Proto 生成物与全部现行 Proto（含 Storage）一致；动态枚举每个正式 RPC 和全部 CLI 叶子命令，StorageService 9 RPC 均须覆盖。历史 51 RPC/73 命令不能作为未经重新枚举的固定分母。

7. CLI 对 Collector 控制面的认证应使用正式配置/环境来源提供管理令牌，按服务契约发送 `x-windhub-admin-token` metadata；不存在明文回退、鉴权关闭或测试专用绕过。必须分别证明已授权与拒绝路径，测试证据不得泄露令牌。
8. CLI 严格 E2E 依次验证全部业务进程 READY、Collector Task 启动、Generation 运行、四出口读回、Commander 写控制、服务重启恢复与关闭收敛；任一步失败不得将其后续步骤计为 PASS。
9. 核对当前 HEAD 的全部 CLI 叶子命令、正式 RPC、已发布 Schema、`configs/` 路径和访问令牌/端口绑定，覆盖矩阵在执行时生成；设计书中的数量和示例不替代运行时实际枚举。

## 12.2 裁决

`PASS`：真实执行、证据完整且符合断言；`FAIL`：执行后违反契约；`NOT_IMPLEMENTED`：正式目标缺实现；`NOT_RUN`：环境或前置不足而未执行；`INCONCLUSIVE`：证据不足。任一未覆盖 CLI/RPC、四目标或最终 Storage 落地缺口、三级数据未经分别裁决、CLI E2E 未执行或 Proto 生成门禁失败，均不得将全套跨模块测试裁决为 PASS。

本文件只定义验收目标；当前 HEAD 的实际测试结果必须另行记录，不援引其他分支的通过报告取代。

## 12.3 配置冻结阻断项

1. **P1：GW2MW BusinessPoint 语义审定**。已新增 `business_points_standard_v1.5.0.yaml`（293+107=400 个候选定义）和 `point_table_ads_wtg_gw2mw_v1.2.0.yaml`（111 源 Point 对 111 不同 BusinessPoint），并升级 DeviceModel/Device Instance/Deployment 的**候选**版本；历史 `v1.1.0` 仍保留以便追溯。原 22 组重复身份的结构问题已在候选内消解，但全部相关 OEM Symbol、物理位置和命令/状态语义仍需现场批准，不能仅凭业务 ID 唯一就判 P1 CLOSED。完整逐点证据为 `docs/audit/gw2mw_businesspoint_semantic_mapping_candidate_20261123.csv`。
2. **P1：现行生产 Deployment** `deployment_prod-a_v1.3.0.yaml` 尚未选择 Storage Service（仅 E2E 专用 Deployment 已选择）；若要冻结生产完整采集到存储的配置闭包，须按版本治理制定生产 Deployment 升级并独立验证，不能因为 E2E 通过就认定 prod-a 已具备完整链路。
3. **P1：RAW** 当前 Sink 编码器缺少真实未解码载荷，三级全链验收不得宣称通过；BUSINESS 必须有真实批准历史 manifest，不能以全零或任意摘要充当信任证据。
4. **P2：多版本目录/固定路径迁移**。旧生产文件头 `device_farm-a_v1.4.0.yaml` 仍引用 v1.3.0，新候选 `device_farm-a_v1.5.0.yaml` 已更正。配置根同时保留历史标准业务目录 v1.4.0 和候选 v1.5.0，仍存在固定文件名加载方及未固定旧依赖的 PointTable，正式完整目录发布前必须完成调用方同步与独立发布根的版本消歧；不可原地改写已经发布的历史内容。
5. **验证状态：NOT_RUN**。本次为远程文件静态审查及 E2E 配置草案，尚未执行全目录 Publisher/Candidate、全部 Python Schema Loader、真实 CLI、Docker Broker/TDengine 和性能/故障联测；未经实测不得裁定 READY_TO_FREEZE。

## 附录 B：内部 gRPC 适配范围

遵从《wind-hub 总体逻辑架构设计》§5.3.2 的统一内部通信规则：内部 gRPC 不启用 TLS/客户端证书；在受控隔离网络中通信，敏感查询及变更/控制操作使用经服务端验证的受控令牌。现行生产代码仍存在证书相关配置及测试，需同步迁移。模块特有的租约、可信恢复证明、幂等、鉴权及审计约束不得因传输层简化而取消。


## 15. 配置契约调整集中开发与验收计划（2026-11-21）

**范围**：仅本轮 Deployment 版本选择、Service→Instance→Template 引用整改、Collector group/task 调度、配置状态与跨模块对账。不得顺带重写采集、物理仿真、派发或控制算法；web 模块不在本轮生产代码范围。

| 阶段 | 生产开发内容 | 交付和检查 |
|---|---|---|
| C1 基础契约 | Contracts Proto 定义部署目标闭包查询与统一状态字段；Configuration 完整目录发布中解析 Deployment，持久化与验证选择；Core 提供唯一解析与摘要 | 文档/Proto/实现一致，拒绝缺失身份、引用环、同版本内容冲突、CAS 并发与错误摘要 |
| C2 配置数据 | 迁移 `configs/` 和 `deploy/` 下 Service 实例逻辑身份；新增 Deployment 文件及 `collector_tasks` Instance；Device Instance 增加 `group`，删除 Task 模板引用 | 所有目标配置能按新 schema 生成完整闭包；旧格式不能通过准入 |
| C3 服务适配 | Collector/Gateway/Commander/Data Generator/Sink/Storage 接入统一目标解析、热更或重启等待、应用摘要；Sink Worker 与 Collector 设备级切换遵守安全边界 | 业务无意外行为变化，无法热更新不虚报活动版本 |
| C4 CLI | CLI 保留单根目录全量发布；支持展示 Deployment 选择、各服务目标与活动修订、差异及错误；统一调用新 Contracts | 不增加逐模块配置通知或独立激活命令 |
| C5 集中自审 | 全仓搜索删除旧字段与废弃代码；检查全部 RPC 调用方、Fixture、配置样例、脚本和文档 | 无旧 `config_sources.templates`、旧 Task Template 依赖、新旧 Schema 双轨；无多余版本文件 |
| C6 Claude 独立验收 | 执行静态检查、单模块、组件、Integration、CLI 全命令及跨模块 E2E；反馈后 ChatGPT 负责生产修复再复测 | ruff/mypy/import-linter/pytest 门禁及配置语义矩阵，附真实命令、退出码和未执行项 |

**最小验收矩阵**：未选新版不生效；修改 Device/Task Instance 不变 Service；group 迁移不重复采集；PointTable 固定版本及控制安全；Service 静态字段等待重启；断网/丢广播/重启对账；错误模板引用拒绝；多部署域隔离；共享 Instance 跨服务版本一致；配置回滚；Reliable 消息不丢且不重复确认。E2E 中间件/数据库由测试代码单独启动，其他模块均由 CLI 启动为进程，业务操作由 CLI 发出。

**当前阶段限制**：本计划仅为设计与编码准入，不代表代码或测试已实施。进入编码时应先检查本分支 HEAD 与最新远端，保持分支不变；不建立 PR、不合并 main、不 rebase、不 force push。


## 15.1 设计与配置冻结专项复测（先于代码专题，2026-11-23）

**本次 Claude 任务范围仅为 docs/design 与 configs 的独立审查、纯文件静态核对和必要的只读 Schema/Loader 检查**，不得将第 11.2 节列出的完整软件门禁误作本次必须执行项目。不得启动业务服务、Broker、TDengine，不运行 E2E、性能、负载或全仓生产代码审查；这些工作留待设计冻结后的独立代码专题。对生产代码发现的缺口登记证据而不修改生产代码。测试代码、Fixture 和设计文档的更改亦须先报告，不自行改变正式配置语义或已发布版本。

### 15.1.1 文件与版本闭包

1. 确认 `docs/design/` 各模块恰好一个最新版设计文档，总体架构 v1_27 不变；核查跨模块依赖关系、Contracts 权威位置、Service→Instance→Template 及 Deployment 精确选择原则。
2. 核查全部 `configs/deployments/deployment_*_v*.yaml` 的 Service/Instance 选择文件存在，文件名、文档 ID、`file_version` 一致；`prod-a@1.4.0` 精确选择 `deliveries_prod-a@1.4.0`，后者为同语义候选迁移而非已投产声明。
3. 按每个 Deployment *单独*建立精确版本闭包；Repository 的历史版本库可同时保存多个版本，但单次运行的活动解释必须由 Deployment 与 PointTable 的精确引用决定。不得取最新文件、目录扫描顺序或把不同 Deployment 混为一个活动版本。根目录全集发布存在多个同身份 Deployment 版本时，须按当前 Schema/程序明确报告 `AMBIGUOUS/NOT_SUPPORTED`，不得宣称直接可发布。
4. 展开 Device Instance→DeviceModel→Protocol/PointTable→父 PointTable→Units/BusinessPoint，检查不存在缺失目标、循环、不一致 `template_id`、孤立的活动引用；历史文件保持原始字节不变。旧 PointTable 未显式固定历史标准目录时，单列 `HISTORICAL_CLOSURE_UNRESOLVED`，禁止用 v1.5.0 重解释。
5. 对 Service→Instance、Sink/Delivery→Template、Delivery→Sink/Device、Storage Kafka→TDengine、双 Collector→Sink 的目标数据通道及端口/身份进行文件级交叉检查；生产 `prod-a` 缺 Storage 仍为生产完整链路阻断，不得由 `e2e-final` 的 Storage 代替。

### 15.1.2 BusinessPoint 和三级数据专项

- `business_points_standard_v1.4.0` 的 293 个原定义在 v1.5.0 中保持类型、标准单位与原业务语义不变；v1.5.0 新增 107 个候选身份。以仓库映射 CSV 对账新 GW2MW 111 个 Point ID、ADS Symbol、地址、源单位、源类型、缩放与 111 个互异的 BusinessPoint ID。
- 对新候选 PointTable 及其继承后完整有效点集执行一对一审查；历史版本按原语义归档而非强行重写。跨不同 PointTable 只有**真实物理语义等价**的 BusinessPoint 才允许复用。
- 校核引用目标存在、`data_type`、单位量纲及布尔/字符串的恒等缩放；所有 `PENDING_OEM_SEMANTICS` 保持待核验，不得把结构性 PASS 等价为业务物理验收。
- 逐级核查 RAW/PROTOCOL/BUSINESS 的名词、源载荷边界、固定历史解释闭包、Delivery 的 `output.data_level` 及 Storage 的持久化字段。RAW 无真实原始载荷和 BUSINESS 未获批准 Manifest 时明确 NOT_SUPPORTED/NOT_READY，不允许填造哈希或用 PROTOCOL 冒充。
- 检查 `e2e-final` 对 Configuration、双 Collector、Gateway、Data Generator、Sink 四出口和 Storage 最终落库所需配置的完整性；静态检查仅判断配置设计符合度，不宣称服务、Kafka、TDengine 已实际运行。

### 15.1.3 独立反馈和裁决

每条问题记录 ID、P0/P1/P2/P3、文件及行号、引用链、可复现的只读命令、预期/实际、建议整改文件；按 `CLOSED/OPEN/DEFERRED_TO_CODE_REVIEW/NEEDS_OEM` 分别登记。必须分开给出：

- **DESIGN_CONFIG_REVIEW**：`PASS / PASS_WITH_FINDINGS / FAIL`，取决于文档和候选配置的结构性矛盾。
- **PRODUCTION_ACTIVATION**：独立 `BLOCKED`，直至 OEM 语义审批、生产 Storage 部署闭包及历史解释/业务 Manifest 条件成立。
- **SOFTWARE_TESTS**：全部 `NOT_RUN`，不允许把文档/配置纯静态核对冒充生产代码测试或全链 E2E。

当前分支已经对 Core/Configuration 有先前提交的生产代码变更，但**不属于这次 Claude 复测范围**。不得基于这些未执行测试的修改声明运行时发布准入通过。


## 15.2 P0/P1/P2 第二轮文档与配置专项复测（2026-11-23）

**范围控制**：仅审核最新 `docs/design` 正文、`docs/audit/full_module_freeze_readiness_review_20261123.md` §13 受控勘误及候选 `configs` YAML；不扩大为源码审查、全仓测试、进程启动、生产发布或 OEM 认证。

### 15.2.1 规范一致性门禁

- 根据上一轮 `design_config_freeze_retest_20261123.md` 的 D-01～D-28 逐项确认具体正文冲突是否删除或由经授权的冻结勘误正式替代；不能仅靠前置“旧条款失效”文字让历史错误自动 PASS。
- 优先验证 RAW=解码前真实帧/字节、PROTOCOL=协议类型解码但未缩放标量、BUSINESS=不可变点表和业务单位转换；Collector 禁止工程换算，Sink 仅换算一次；对 `engineering`、默认等级及伪 RAW 必须报错。
- 使用当前 `src/contracts/*.proto` 的静态 RPC 名称与字段号作为对照（只读）；Configuration `GetServiceConfigBundleRequest` 的 `revision_id=0` 按自身 Proto 表示当前 published revision。不要用其它 RPC 的默认值语义误判。
- 检查 CLI 仅 `config publish --root` 为配置发布权威；Common Push/Apply 历史描述不得赋予业务配置双写权；Device/Gateway 变量身份、目标同步 SampleBatch、Sink Delivery 意图和 Storage 单实例/幂等身份在正文一致。
- 检查最新模块设计内部版本引用和章节链接，不允许规范性引用指向已删除旧版本文档。历史审计中的旧链接须显式说明其非现行基线。

### 15.2.2 候选版本闭包矩阵

| Deployment | 候选版本 | 设备 Instance | 确定性检查 |
|---|---|---|---|
| prod-a | 1.5.0 | farm-a@1.6.0 | GW 型号 1.5.0→PointTable 1.2.0；TZ 型号 1.4.0→PointTable 1.2.0；所有点表→BP1.5/Units1.2；Sink-prod-a1.4 + Delivery-prod-a1.5；Storage-main1.2 |
| sim-a | 1.2.0 | sim@1.4.0 | 五个仿真型号 1.2.0→PointTable 1.2.0→BP1.5/Units1.2；CLI-sim1.4.0 不调用生产目标 |
| e2e-final | 1.0.1 | e2e-final@1.0.1 | 五个仿真型号 1.2.0→PointTable 1.2.0→BP1.5/Units1.2；Sink/Storage Kafka Topic 相同；E2E 设备完整 |

以各部署独立所选闭包为分母，不要把旧 Deployment 和候选同 ID 的多个版本混成一个有效根；测试必须证明根目录全集无法直接发布时存在清晰拒绝，并能构建没有版本歧义的候选完整目录。原有历史版本的无依赖声明不代表其可以重新解释新业务目录。

检查新 PointTable 全部 Point/BusinessPoint 身份单射、数据类型和标准单位兼容，原 111 个 GW ADS Symbol/地址及 293 个原 BP 定义完全不变，107 候选仍为 OEM PENDING。检查 Storage-main、Sink Kafka 的 Broker/Topic 配置及 Collector 来源身份是否确定；无法据静态文件证明的运行时行为保持 `DEFERRED_TO_CODE_REVIEW`。

### 15.2.3 裁决

输出每个原 D/C 编号对应 `CLOSED/OPEN/NEEDS_OEM/DEFERRED_TO_CODE_REVIEW`、版本和文件行号、实际 YAML/只读 Loader 命令及退出码。分别给出 `DESIGN_CONFIG_REVIEW`、`PRODUCTION_ACTIVATION`、`SOFTWARE_TESTS` 结论。总体冻结勘误若未经用户正式批准，必须作为 `DESIGN_FREEZE_APPROVAL_PENDING` 报告，不能自行视为整体冻结通过。软件测试在本轮保持 `NOT_RUN`。

## 15.3 实际交付的隔离候选发布根目录

Git Blob 原文保持一致；根内文件的相对路径仍为 `deployments/`、`services/`、`instances/`、`templates/`：

| 独立根目录 | YAML 文件数 | 唯一候选 Deployment | 发布状态 |
|---|---:|---|---|
| `deploy/config_roots/prod-a/` | 29 | prod-a@1.6.0 | 未发布，OEM/Manifest/真实 Broker 门禁 |
| `deploy/config_roots/sim-a/` | 23 | sim-a@1.4.0 | 未发布，真实服务和独立管理面待核验 |
| `deploy/config_roots/e2e-final/` | 35 | e2e-final@1.0.1 | 未发布，真实 E2E 尚未执行 |

由 ChatGPT 执行设计与配置静态一致性核验；后续 Claude 负责代码测试及小范围修复。对每个根严格解析 YAML 并检查文件闭包，使用可读 Core Loader/Domain Snapshot 和 Configuration validate_only 纯校验能力时必须保持无业务服务、数据库和消息中间件启动。不把仓库总 `configs/` 目录视为可直接发布根，也不以文件复制冒充已经通过 Repository 发布。三根按不同部署域相互隔离，不允许用 prod-a 候选服务替代 e2e/sim 的业务进程。

验证候选完整时，同时核对：Sink/Storage Topic=`windhub-history`、Broker=`192.168.1.20:9092`（生产示例），E2E 独立 Topic；prod-a 当前审批限制；111 条 GW2MW PENDING_OEM_SEMANTICS；旧历史 PointTable 尚未具备原始发布 Manifest 的事实。配置文件语法和引用 PASS 不替代这些外部运行及语义证据。

## 15.4 第三轮静态复核重点（2026-11-23）

新候选根保持不变的文件数：prod-a 29、sim-a 23、e2e-final 35。唯一候选 Deployment 当前为 prod-a@1.6.0、sim-a@1.4.0、e2e-final@1.0.1。此前 §15.2/15.3 中的旧候选版本表述均按本节最新候选生效，不能引用已经退出活动根的 prod-a@1.5.0 或 sim-a@1.3.0。

N-01：`sink_kafka_history@1.1.0` 必须与原始 Git Blob `460fb5d96235b4a6713c85af6102efbde9f85f78` 全等；`kafka_history@1.2.0` 承担新的 `windhub-history` Topic 与串行 1 在途能力。prod-a 通过 Sink Instance@1.4.0 精确引用新模板，再由 Deployment@1.6.0 选择。

N-02：CLI 的唯一规范叶子名称是 `commander batch` 和 `commander recover-device`；不额外列出 `submit-commands` 或 `recover` 别名。

N-03/C-06：sim-a CLI@1.5.0 没有 TLS profile，全部受管目标使用独立 530xx 端口、`sim-a` 状态/日志目录及独立进程；Gateway@1.2.0、Commander@1.2.0、Generator@1.1.0、Device@1.4.0、Scenario@1.0.4、Configuration 本地 bootstrap 均须与目标身份互相一致。新的 token_sources 字段属于设计候选，严格运行时 Schema 是否接受必须单列风险，不可视作已通过软件测试。

C-07：查 `docs/audit/config_version_release_ledger_20261123.csv`，所有前已存在的文件实际生产发布状态均未凭 Git 推定为已批准；缺实物 Manifest 仍须明示。C-08 Gateway Sink STOP、安全 Listener 未配置、C-11 E2E 物理基础设施隔离、C-13 OEM、C-14 RAW/BUSINESS 外部证据，不应归入结构性文档缺陷关闭。软件测试始终保持 NOT_RUN。

## 15.5 第三轮独立复测交付判据

以 Claude 第二轮报告 D-01～D-28/C-01～C-14 的 42 项及 N-01～N-03 三项为固定分母；逐条对原冲突行、接口表、例图、旧版本引用和配置版本选择作严格复测。只能依据已运行的只读静态 YAML/Loader 和文件哈希证据关闭相关项目，不能仅因新文档版本存在就 PASS。

特别执行以下反例：历史 Kafka 模板 v1.1.0 Blob 完全相同、唯一新模板 v1.2.0 被 Sink@1.4.0 选中；同根只有唯一 prod-a@1.6.0 / sim-a@1.4.0 / e2e-final@1.0.1；sim-a CLI@1.5.0 没有 TLS profile，管理目标与生产无重复端口/manager target/state_dir，关联服务的 gRPC 监听与 Scenario 路由一致；Collector 及 Sink 流程图没有本地业务 Push/Apply 第二写入口；Contract Descriptor 的 GetServiceConfigBundle 字段号与请求中 revision_id=0 语义一致；现行 CLI 叶子名称仅定义一次。

`DESIGN_CONFIG_REVIEW` 只评价设计和候选静态结构；`DESIGN_FREEZE_APPROVAL` 单列冻结总体勘误的批准状态；`PRODUCTION_ACTIVATION` 继续独立标记 OEM、RAW/BUSINESS/历史发布证据和实际基础设施门禁；`SOFTWARE_TESTS=NOT_RUN`。新 sim-a token_sources/服务认证字段及现行服务端 Schema 的契约差异须登记为 `DEFERRED_TO_CODE_REVIEW`，不得以静态设计结果替代实现检查。


**sim-a Schema 补充**：sim-a@1.4.0 选定 `gateway-sim@1.2.0`、`commander-sim@1.2.0`、`data-generator-sim@1.1.0` 与 `cli-sim@1.5.0`。三个新 Service 使用 Schema v2 的 `security.callers[].token_env`。纯文件复测须按 `src/configuration_service/application/schema_catalog.py` 的现行 service v2 规则校验，严禁使用没有 Schema 定义的 `security.token_sources` 或 `grpc.token_sources`。


## 15.6 第一阶段设计收口后的候选基线（2026-11-23，替代 §15.4 活动版本）

本节只更新**本分支待复测的目标选版**，不追溯更改 §15.4 的历史审查结论。现行独立配置根：

| 配置根 | 唯一活动候选 Deployment | 关键约束 |
|---|---|---|
| `prod-a` | `prod-a@1.6.0` | 保持 29 份 YAML；GW2MW 未经 OEM 审定的 BUSINESS 语义不得生产激活 |
| `sim-a` | `sim-a@1.8.0` | 保持 23 份 YAML；Gateway@1.3.0、Commander@1.3.0、Generator@1.3.1、CLI@1.8.0 均使用令牌契约；尚未通过运行时验收 |
| `e2e-final` | `e2e-final@1.0.5` | 保持 35 份 YAML；Command/Gateway 采用已增加 Schema v3 的生产代码候选，Generator v2 与 CLI 新版本均禁用 mTLS；待代码实现及 Repository/Runtime 验证 |

**候选台账**：源 YAML 共 146 份；累计二十六份后继版本均标为 `UNPUBLISHED_CANDIDATE`，历史版本不得覆盖。三根复制文件与源 Blob 必须逐一相等，每根恰有一个 Deployment；对本节新候选重新执行严格 YAML、Repository Schema、Core Domain Snapshot、精确闭包、令牌环境引用、全根 hash 审计。未实际运行不得标 PASS。

**变量映射断言**：单个 Gateway 内以目标当前 PointTable 的 `point_id` 作为 `variable_id`，源点可映射到同一 Gateway 或不同 Gateway 内的多个不同目标，且每个目标必须是所选 Gateway PointTable 中已有点位；不得多源占用同一个 `(gateway_id,variable_id)`。此规则独立于 PointTable 的 Point→BusinessPoint 单射。配置层和运行时分别验证不存在未定义目标、重名、类型冲突、重复占用及绕过租约。

**Configuration 与服务运行时的分离门禁**：后继 e2e Service Schema v3 由 Configuration 设计 §9.1.1 定义，必须新建 Service/Deployment 不可变文件版本后才可用于发布。`N-04` 三个 sim Schema v2 Service 运行时拒绝、`N-05` e2e 33/35 Repository Schema 缺口、`C-10` 硬编码业务目录均转入后续代码专题。设计阶段不得修改已发布 Schema 的不可变定义，也不能把配置纯解析成功等同第一方 Service 加载成功。软件实现阶段须修复后分别以真实 Configuration Candidate validate-only、真实模块解析和 E2E 服务启动提供证据。

**设计冻结判定**：对 §15.4 的旧 45 项及第三轮新 N-04～N-09 延续逐项闭环；仅设计/配置静态缺陷计入本阶段阻断。总体 v1_27 的 D-01/D-04/D-06 替代勘误必须另行获得明确批准；未经批准不宣称 DESIGN_FREEZE_APPROVED。生产激活所需 OEM、真实 RAW/BUSINESS、现场授权、性能证据属于独立门禁，不由静态候选替代。

### 15.6.1 E2E 无 mTLS 候选拓扑与准入

已创建未发布 E2E 后继 Service：Commander@1.1.0、Gateway-source@1.1.1、Gateway-target@1.1.1、Data Generator@1.1.0、CLI@1.1.1，Sink Instance@1.0.1 与 Deployment@1.0.2。旧源 YAML 仍保留，独立活动根内仅选一个 Deployment 和相应精确版本。Generator v2 取消 `output.gateway_tls`；CLI 删除 TLS Profile；Gateway 的控制转发与仿真更新各有独立令牌来源，Sink 目标独立使用 `credential_ref: env:WIND_HUB_E2E_GATEWAY_TARGET_SINK_TOKEN`。这些均是设计候选，未在运行时证明安全性。

**令牌双端对应测试**：Gateway→Commander、Commander→Gateway、Gateway→Generator、Generator→Gateway、Sink→Gateway，每个路径分别验证发起方 Token 环境变量名与目标 Service 授权调用者配置完全匹配；Generator 出站凭据明确取 `output.gateway_token_env`，不是 `security.callers[]` 入站令牌。错误或缺失令牌拒绝，未授权调用无副作用。Gateway Sink Listener 仅 loopback、与管理口不同且 Sink Token 不允许管理 RPC；不存在真实鉴权实现时必须保持 STOP。

新 e2e 的 Commander/Gateway 选择 `schema_version: 3`，该定义尚未在运行时代码中注册，因此本阶段仅允许进行静态设计/引用验证，实际 Repository Schema 门禁必然暂不满足，列入第二阶段代码与测试整改。不得通过回退旧 v1 证书配置、取消严格 Schema 或把未验证候选当生产已发布来绕过门禁。

**Schema v3 的后继安全整改**：Gateway-source/target@1.1.1 删除 `grpc.permissions` 重复权限源，权限仅由 `security.callers[]` 管理；CLI@1.1.2、e2e-final@1.0.4 精确引用。原 1.1.0/1.0.2 文件保留为未发布历史候选且不进入独立活动根。设计基线统计以 135 项台账为准；静态配置根 Blob 需按新选版复核。

**后继候选再修复**：Gateway-source@1.1.1 的 `servers[].control.simulation` 中发现重复 `token_env` 键（严格 YAML 解析必须拒绝），不得发布或回退宽松解析。现行源 Gateway-source@1.1.2、CLI@1.1.2、Deployment@1.0.4 作为不可变后继选版，Gateway-target 仍为 1.1.1；旧源版本保留作为未发布历史审计，不在 E2E 活动根中选择。台账现为 135 项。

### 15.7 第二阶段当前候选与代码测试门禁（2026-11-23）

以本节为最新候选选版，不改写 §15.2～§15.6 历史状态：

| 独立根 | 当前 Deployment | 文件数（待完整复查） | 对应模块 Service |
|---|---|---:|---|
| prod-a | 1.6.0 | 29 | 历史版本，需要代码与配置同步迁移 |
| sim-a | 1.8.0 | 23 | Commander 1.3.0、Gateway 1.3.0、Data Generator 1.3.1、CLI 1.8.0 |
| e2e-final | 1.0.5 | 35 | Commander 1.1.0、Gateway source 1.1.2/target 1.1.1、Data Generator 1.1.1、CLI 1.1.3 |

**当前台账**：146 份源 YAML（历史版本不可变），后继候选不等于已发布；必须校验活动根只有单一 Deployment、Service/Instance/Template 固定引用一致、源/根 Blob 恒等、严格 YAML 无重复键、Repository v2/v3 Schema、Core Domain Snapshot 与第一方服务解析。

**本轮新增必测**：Configuration v3 动态对象及旧 v1/v2 不变性；Commander/Gateway/Generator 任意 RPC 匿名/错误令牌/重复令牌拒绝；Gateway Sink 注入口权限隔离（Sink Token 不得访问管理 RPC）；Gateway→Commander→Gateway→Generator 与 Generator→Gateway 双向令牌和身份绑定；Generator 出站诊断、授权租约、模型输入及执行证明；旧 mTLS Fixture 删除后全仓测试调用方必须迁移；清理后的 Common 旧 Push/Apply 必须拒绝业务写入。RAW/PROTOCOL/BUSINESS 最终以 Storage 独立读回为准。

**状态**：本阶段正在集中生产编码与测试代码迁移，以上软件门禁全部保留为 `NOT_RUN`，不得将 GitHub 代码提交视为 E2E 或 Production Ready。

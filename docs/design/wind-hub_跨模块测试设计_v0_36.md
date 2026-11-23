<h1 class="title">wind-hub 跨模块测试设计</h1>

<p class="subtitle">v0_36 · 20261123 · 集成后设计归并· 20261118 · 统一配置、CLI 进程管理与完整链路验收设计</p>

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

**main 集成基线（2026-11-23）**：按当前集成的 Storage、Contracts、Configuration、CLI 及 SampleBatch v3/Kafka v2 设计真实完整链路；中间件和数据库由测试单独启动，其他服务经 CLI 启动，操作经 CLI 发出，覆盖全部现行接口。 旧分支的开发状态及历史适配方案不得替代已集成代码与当前协议。

## 3.1 测试目标

1. 验证 WTG、PV、BESS、GRID、PPC 五类物理仿真，以及随机和 CSV 回放源的可复现输出与跨模块数据传输。
2. 验证完整测量链路：Data Generator → Industrial Gateway → Collector → NATS/JetStream → Sink，并**独立验收** Realtime→Gateway、Realtime→Modbus TCP、Realtime→Redis、Reliable→Kafka 四个出口。
3. 独立验证工业控制链路：工业 Client → Gateway → Commander → Gateway 专用仿真执行 RPC → Data Generator 模型 INPUT；只有请求级真实计算步完成证据可促成成功结算。
4. 每个 CLI 叶子命令和全部正式 RPC 均有可定位用例，包含正常、拒绝、超时、权限或幂等路径；逐层证明进程、gRPC、业务状态和下游副作用。
5. 保留用例 ID → HEAD/配置哈希 → CLI/请求 → 源记录 → 下游读回 → 断言/裁决的完整证据链。

## 3.2 关键约束

- E2E 不以 Mock/Fake 替代核心业务服务；Integration 可向真实 NATS 注入标准批次作为分段激励，但不能因此宣称完整 E2E 通过。
- 全部 wind-hub 业务进程，包括独立 Gateway/Modbus 目标，必须通过正式 CLI 管理；测试 Fixture 不允许通过 subprocess 直接启动业务进程。NATS/JetStream、Kafka、Redis 例外。
- 配置与模板统一来自正式 `configs/`，场景参数和运行记录存放于测试目录；不复制第二份共享型号、点表或单位目录，不修改真实生产设备配置。
- 进程 RUNNING、gRPC READY、业务 Generation/Task/Delivery RUNNING、下游生效分别观测；ACK 不等于控制 APPLIED，也不等于 Kafka 最终消费。
- 每个场景须列配置、进程、激励、步骤、独立输出、断言和清理。无环境为 NOT_RUN，接口缺失为 NOT_IMPLEMENTED，已执行失败为 FAIL；禁止删除未覆盖条目缩小分母。
- 当前验收只涉及 Linux 原生进程；未实际验证的 containerd/iSulad、硬件、netem 必须单列 NOT_RUN。

## 3.3 现行设计基线

| 领域 | main 归并后唯一设计文件 | 验收重点 |
|---|---|---|
| 总体 / Core | `wind-hub_总体逻辑架构设计_v1_27.md`、`Core_软件设计_v1_41.md` | DFD 归属、共享配置归 Core |
| Contracts / Configuration | `Contracts_软件设计_v1_41.md`、`Configuration_Service_软件设计_v1_18.md` | 现行 Proto、Deployment 闭包、版本发布与查询 |
| Data Generator / Industrial Gateway | `Data_Generator_软件设计_v1_58.md`、`industrial_gateway_软件设计_v1_48.md` | 仿真、变量身份、目标写入与端口权限 |
| Collector / Commander | `Collector_软件设计_v1_39.md`、`Commander_软件设计_v1_55.md` | SampleBatch v3、控制安全、运行状态 |
| Sink / Storage | `Sink_Service_软件设计_v1_71.md`、`Storage_Service_软件设计_v1_5.md` | Kafka JSON v2、数据等级、持久入库及查询 |
| CLI | `CLI_软件设计_v1_51.md` | CLI 命令树、进程启动和全量发布 |

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
    GWT --> V["独立读回比对器"]
    M --> V
    R --> V
    K --> V
    GD -.-> V
    IC["工业控制 Client"] -->|"控制写事件"| GW
    GW -->|"SubmitCommand(s)"| CMD["Commander"]
    CMD -->|"ExecuteSimulationCommand"| GW
    GW -->|"UpdateVariables（模型 INPUT）"| DG
    CMD -->|"GetSimulationExecutionResult"| GW
    GW -->|"GetInputExecution"| DG
~~~

<p class="figure-caption">图 4-1 测量双通道、四类 Sink 目标和独立仿真控制链</p>

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

模型数学准确性由各模型设计与单模块验证负责，本文件验证源输出到各目标的跨模块可追溯一致性。

# 5. 共享配置、运行环境与进程启动

### 2026-11-21 统一配置版本选择与生效规则（规范性修订）
Service → Instances → Templates 是唯一允许的配置引用顺序：Service 仅引用 Instance 逻辑身份，不直接引用 Template ID/版本或目录。Deployment 统一选择 Service/Instance 的不可变版本，Instance 固定引用 Template 的版本。全量发布不直接使所有服务切换；Configuration 广播后，各模块主动解析目标配置闭包并决定热更新或等待重启。
Service 状态以 target_revision 和 active_revision 表示本服务完整有效配置闭包的目标与完全生效值。不能热更新的字段保持原活动值等待重启；部分更新不能虚报完整生效。Deployment 回退引用历史版本即可，丢失通知由定期权威对账补偿。旧直接模板来源及版本路径引用必须在实施中统一清理。

新增 Configuration/Deployment 跨模块验收矩阵（设计目标，代码尚未实现）：
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
| NATS/JetStream、Kafka、Redis | 测试基础设施管理器 | 服务 READY、测试 Stream/Topic/Key 前缀及清理 |
| Gateway、Generator、Collector、Commander、Sink Main | wind-hub CLI | service start/stop/restart/status、进程 ID、gRPC READY |
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
    T->>I: 准备 NATS / Redis / Kafka
    I-->>T: READY 与隔离资源
    T->>CLI: service start（Gateway/Generator/Collector/Commander/Sink）
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

**软件仿真 E2E 不需要外部真实风机、TwinCAT PLC、现场 IEC104 装置或工业网关实体。** 这些设备均由仓库内 Data Generator、Industrial Gateway（含仿真 Modbus 从站）、测试用 Modbus TCP Server 和 CLI 管理的业务进程提供；仅 NATS/JetStream、Redis、Kafka 属独立测试基础设施。TDengine 只在声明依赖的验证中启动。真实 TwinCAT/现场硬件验证单列为部署/现场验收，不能作为 E2E-01～10 的执行前置，也不得因为缺少硬件将仿真 E2E 标记 NOT_RUN。

1. **端口先检测再分配。** 普通 System Redis Fixture 使用独立 Compose Project 和随机 loopback 宿主端口，返回真实端点给测试；R5 完整栈对 NATS、Redis、Kafka、TDengine 分别使用动态宿主端口。所有生成的 Service YAML、CLI YAML、Sink 连接及测试读回均引用分配的端点，禁止硬编码 6379/4222/9092。
2. **冲突处理。** Compose 绑定冲突时仅销毁本用例创建的 Project，重新分配端口并有界重试；不执行 docker prune、不停止其他用户的容器。显式 `WIND_HUB_TEST_REDIS` 可使用外部 Redis，但必须标记为外部依赖，不能在结束时停机。端口重试后仍失败记为 SETUP_ERROR 并保留日志，不属于生产模块 FAIL。
3. **进程管理。** CLI 启动/停止/restart 通过正式 process 管理后端启动和管理独立 Linux 进程，不依赖 systemd。必须校验进程身份（PID 与 start-tick）、进程组、状态文件及日志所有权；容器后端须另做真实运行时专项验证。任何后端不可用时记录 NOT_RUN_ENV，不得用 Mock 伪造 PASS。
4. **隔离与清理。** 每个测试使用独立 tmp_path、动态端口、独立进程管理状态目录与受控服务 ID、唯一 NATS Stream / Kafka Topic / Redis Namespace；即使断言失败也依次 CLI 停止业务进程、清理本次进程组、中间件 Project、文件和流。任何端口占用必须先归属识别，不得无条件删除非本测试容器。
5. **前置检查与裁决。** 校验当前 HEAD、Python/CLI 执行路径、Docker daemon、process 管理后端、访问令牌注入、compose 健康、服务 gRPC READY 和 Sink 双 Worker 状态；缺业务必需中间件或正式 process 后端的场景标 NOT_RUN，缺少真实现场硬件对软件仿真场景**不构成** NOT_RUN 理由。不得把准备阶段的端口竞争记作软件功能缺陷。

# 6. Integration 分段测试

所有分段沿用第 5 章正式配置和 CLI 进程管理，不复制配置树；若某前置配置尚未落地，则该行保留为 NOT_RUN/NOT_IMPLEMENTED。每行执行前置检查→CLI 启服务→注入激励→独立读回→关闭并清理。

| 用例 | 配置、CLI 启动的进程 | 激励与执行步骤 | 预期输出和断言 |
|---|---|---|---|
| I-01 源注入 | `devices_sim.yaml`、DG/Gateway 仿真 Service；Generator、Gateway | 先 Gateway READY → Generation start → 固定风速和模型输出 → Gateway 读回 → stop | 源记录和 Gateway 值按时间、点位、版本对齐，租约合法 |
| I-02 工业采集 | 前项 + Collector 模拟 Service/任务；Gateway、Collector | Modbus 输入变化 → Task start → ReadPoints 和任务采样 → stop | 读数、缩放、质量与定时发布分别可证 |
| I-03 NATS 双通道 | Collector/Gateway + NATS/JetStream | 订阅实际 Subject → 启 Task → 抓双批 → stop | SampleBatch、设备/点身份、源时间、Realtime/Reliable 语义 |
| I-04 Sink 四目标 | Sink 测试 Service、隔离 Sink/Delivery + Gateway/Redis/Kafka；Sink、目标 Gateway | 固定标准 SampleBatch 注入真实 NATS → Delivery start → 四目标读回 → stop | Gateway 更新、Modbus 寄存器、Redis 最新值、Kafka 历史分别断言 |
| I-05 工业仿真控制 | Gateway/Commander/Generator 仿真配置；三进程 | 工业 Client FC06/FC16 → Commander → Gateway 执行 → Generator 查询 → 重复/无权测试 | 同 command_id、不直接转发、PENDING→APPLIED/UNKNOWN，受理非成功 |
| I-06 CLI 生命周期 | CLI 有效 Service + 六业务服务身份 | start → status/READY → restart → stop | Unit/PID/业务状态和 Sink Worker 退出 |
| I-07 Gateway Realtime | Sink Gateway 模板/映射 + 独立目标 Gateway；两进程 | 受控令牌 ACQUIRE → UpdateVariables → ReadVariables → RELEASE/过期 | 值与映射正确，租约及回环隔离 |
| I-08 配置治理 | CLI 全量根目录、Configuration、各业务 Service | 修改完整 configs 根目录并通过 CLI publish，注入版本冲突、无效路径、旧结构和非法实例键 | 完整候选拒绝且原目录视图不变；正确候选只广播修订，各模块独立拉取生效 |
| I-09 全 RPC | 六 Proto 和全部服务 | 自动枚举 → 真实 RPC 调用及拒绝路径 → 检查 Descriptor | 51/51 服务限定 RPC 声明及各服务 Common 注册，证据完整 |

I-04 的标准批次 Publisher 是**分段激励**；只有 E2E-07 通过完整 Generator→Collector→Sink 路径，才能确认全链路结果。

# 7. E2E 完整场景

## 7.1 通用执行步骤

1. 核实 Git HEAD、六份 Proto 的生成一致性、配置哈希、CLI 命令清单、调用方令牌权限、测试目标和端口。
2. 中间件准备器启动 NATS/JetStream、Redis、Kafka；只通过 CLI 启动各业务进程，验证 PID/READY，再启动 Generation、Gateway、Collector Task 和 Sink Delivery。
3. 对选定模型实施确定性激励，收集 Generator 原始 JSONL、Gateway、Collector、NATS/JetStream、Sink Gateway/Modbus/Redis/Kafka 读回。
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
| E2E-10 故障与恢复 | 同上 + 受控基础设施故障 | 断 NATS、Kafka、Redis、Gateway，旧 generation 和重复 ID | Realtime 覆盖、Reliable 重投/DLQ、UNKNOWN 与授权隔离 |

## 7.3 四个 Sink 目标分别验收

| ID | 通道与目标 | 实际输入和独立读回 | 必须验证 |
|---|---|---|---|
| OUT-01 | Realtime → Industrial Gateway | 正式 GatewayOutput、内网访问授权 `ClaimTargets/UpdateVariables` → Gateway ReadVariables | target_mappings、租约、权限、值更新及目标与源隔离 |
| OUT-02 | Realtime → Modbus TCP | Sink Modbus Server → 独立 Modbus TCP Client | 寄存器地址、类型、缩放、大小端和最新值；非主动写第三方 Server |
| OUT-03 | Realtime → Redis | Sink Redis 写入 → 独立 Redis Client | 来源标识、时间与最新值覆盖 |
| OUT-04 | Reliable → Kafka | JetStream→Sink Reliable→Kafka → 独立 Consumer | 应交付的真实源批、顺序、ACK、重投与下游去重界限 |

四个 OUT 的 PASS 不可互相代替。工业控制 E2E 作为 E2E-04 单独裁决，不与 Gateway 测量注入混为一谈。

## 7.4 严格 CLI 独立进程全链验证

整个场景使用独立业务服务身份：Industrial Gateway、Data Generator、Realtime Collector、Reliable Collector、Sink Service、Commander。Sink Main 自行管理 Realtime/Reliable Worker；各业务进程的启动、停止、重启仅通过正式 CLI 的 Service 管理命令完成。中间件由独立测试基础设施管理器启动。全部业务命令经 CLI，未提供 CLI 操作的正式 RPC 经独立认证客户端覆盖并计入 RPC 清单。

执行必须保留独立证据：① CLI 与 Linux 进程 PID/READY；② 两个 Collector 分别对应 Realtime、Reliable 的真实 SampleBatch 和 JetStream PubAck；③ Generator 真值 JSONL 和 Gateway/Collector 采集值；④ Industrial Gateway、Modbus TCP、Redis、Kafka 四个目标的独立读回；⑤ Commander request_id/command_id 与 Data Generator effective_step 的控制完成结果；⑥ restart 后已提交配置与业务运行状态一致、消息积压收敛；⑦ stop 后无遗留业务进程、连接或活跃 Worker。

安全场景必须采用正式 内网访问授权 与权限约束，验证 Collector 管理令牌、Gateway/Generator 令牌授权身份、租约目标授权和 Commander 写控制。业务进程能够启动、某一 RPC 返回 OK、NATS 发布成功、Kafka ACK 或 Golden Data 仅局部匹配，均不得代替完整链路的独立 E2E 裁决。每一个 E2E ID 都必须有实际命令、日志、配置哈希、源值、目标读回和 PASS/FAIL/NOT_RUN 证据。

## 7.5 各完整场景的配置、激励、独立读回与步骤

本节所有场景**共用第 5 章生成的实例、点表、型号、Service、Sink 与 CLI 配置**，仅每个场景的激励/运行时参数变化。物理设备均为软件仿真实例 `WTG_SIM01`、`PV_SIM01`、`BESS_SIM01`、`GRID_SIM01`、`PPC_SIM01`，不引用现场硬件。启动过程统一为：独立 Docker 中间件 → CLI 启 Gateway、DG、双 Collector、Sink Main、Commander → CLI 验证各自 READY → CLI 启动 Collector Task、Generation、Sink Delivery → 独立客户端读回 → CLI 停止与回收。每个场景至少有一份 E2E 专用测试，不得借模块测试替代。

| ID | 配置 / 进程 | CLI 激励与顺序 | 独立输出与裁决断言 |
|---|---|---|---|
| E2E-01 WTG | WTG physical Scenario、`devices_sim.yaml`、WTG 点表；上述六服务 | 平均风速 10→12 m/s、TI=0，各段采集多步；CLI Start/Stop Generation | DG Recording JSONL 风速及功率、Gateway 变量、Collector 原始值、四出口。风速恒定段误差按编码精度比较；功率按物理模型且允许动态过渡 |
| E2E-02 PV | PV physical Scenario、`pv_sim_physical` 型号 | 固定气温，辐照 0→800→1000 W/m²；每段新 Scenario/Generation，独立录制 | 组件温度、直流/交流功率边界，Gateway/Collector/OUT-01～04 与真值同身份、同单位、同合理时间窗 |
| E2E-03 BESS | `bess_sim_physical`、BESS 点表及限值 | 通过 CLI 更新允许的充放电指令，跨多个步长至 SOC 边界，再拒绝越限输入 | SOC 单调性、储能变化与功率符号、无越界、拒绝请求不改变状态；四出口同一有效控制步 |
| E2E-04 GRID/PPC | GRID/PPC 两物理模型；Commander 权限和 Gateway 控制路由 | CLI 频率/电压扰动、AGC/AVC/FFR 合法与非法控制，查询请求完成状态 | `request_id`、`effective_step`、执行反馈；只有 APPLIED 才视为闭环成功，UNKNOWN 不冒充成功 |
| E2E-05 随机/CSV | `history/demo.csv`、Replay Scenario、固定种子 Scenario | 两轮同 seed 运行并比对，同一 CSV 按 1x/2x 顺序重放；停止重启后再取值 | DG 真值逐点序列与 replay 顺序一致；Reliable 已提交批次顺序不逆转；不同运行 ID 不混淆 |
| E2E-06 多速率 | DG `interval_seconds`=0.05/0.1，Collector Task `interval`=0.05/0.1/0.2/1.0 | CLI 分别应用合法版本配置并重启/生效，固定同一确定性激励 | 统计实际时间戳、容许抖动、样本数量与丢弃/覆盖语义；不得假设 Collector 每个生成步必采 |
| E2E-07 四出口 | 同一个全链批次，`gateway_output` / `modbus_output` / `ems_power` / `power_history` | CLI 启/停四 Delivery，正常运行、断连恢复各一次 | OUT-01 Gateway 变量与模拟 Modbus；OUT-02 真实 Modbus 目标寄存器；OUT-03 Redis 键/值；OUT-04 Kafka topic/分区及可靠 ACK，分别独立 PASS |
| E2E-08 CLI/RPC | 当前 CLI YAML + 六 Proto、真实访问令牌与隔离网络 | 枚举 73 个 CLI 叶子并逐项实际执行/合法拒绝；51 个 RPC 逐服务调用与错误码检查 | 每条命令/方法绑定 EXIT/RPC 状态、关联对象及独立副作用；没有实际执行的保留在分母 |
| E2E-09 生命周期 | CLI service manager 对六进程的合法 unit；Sink Main 管理双 Worker | start→status/READY→restart→status→stop；重复启停和服务单侧失败 | 每个 PID、READY、退出码、Worker 清退、无残留连接/僵尸进程；进程状态不代替业务就绪 |
| E2E-10 故障恢复 | 隔离 R5 中间件、故障代理、Gateway/DG/Collector/Sink | 先建立正常基线，再逐项断 NATS/Redis/Kafka/Gateway、注入过期 generation 与重复请求、恢复服务 | Reliable 重投/DLQ、Realtime 覆盖、配置/控制幂等、租约隔离、恢复时间、FD/RSS/CPU；修复后双通道持续出数 |

**比较规则：** 每条数据必须带有设备 ID、点 ID、时间戳、质量与采集/生成身份。对不断变化的物理量，按已保存真值的时间窗匹配而不是将最新采集值与录制文件最后一步直接相减；对 TI=0 固定风速可以进行确定性绝对误差判定。误差阈值由点表的 scale、数据类型与协议编码精度确定并在用例中显式记录；不允许为使测试通过而临时调大阈值。

**证据规则：** 每个 case 保存 HEAD、配置哈希、CLI 指令与退出码、PID、真值路径、独立 Gateway/Modbus/Redis/Kafka 读回、断言及 finally 清理结果。单条 Redis 成功只证明 OUT-03，不证明四出口；WTG/PV/BESS 模块测试成功只可标注 MODULE_PASS，不得升级为 FULL_CHAIN_PASS。

# 8. CLI 与 gRPC 全功能覆盖

## 8.1 自动枚举并生成测试矩阵

六份正式 Proto 当前有 **51 个服务限定 Unary RPC 声明**：Common 9、Collector 11、Commander 8、Sink 4、Gateway 10、Generator 9。Commander 包括 `GetRequestResult`，不得漏计。Common 在多个服务实例中分别注册，唯一方法总数与逐服务注册总数不能混用。Gateway 的 `ExecuteSimulationCommand/GetSimulationExecutionResult`、Generator 的 `GetInputExecution` 必须进入 RPC 覆盖清单。除真实调用外还要验证 Descriptor 请求/响应、字段号、标量 oneof、认证与拒绝语义。

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

测试按 Contracts §11.6 及 §11.9 的**唯一正式线格式**生成 Golden Data 并从真实 NATS 和下游目标独立读取，不得用测试私有 JSON 替代。目标输入为 `SampleBatch(schema_version=3)`，目标 Kafka/Redis JSON 为 `schema_version=2`（当前生产分别是 v2 和 v1，未完成迁移前必须判为未实施）。

| 验收项 | 激励及配置 | 独立观察与断言 |
|---|---|---|
| Collector Realtime | 固定设备实例/PointTable，CLI 启动采集；包含多个 Point 的采集事件 | 实际 NATS Subject 与模式、来源身份一致；批头携带 `device_type`、`device_id`、`point_table_id`；原始标量未做 scale/offset；批次合法 |
| Collector Reliable | 同上，JetStream Durable 消费 | JSON 字段集、`schema_version=3`、样本质量/时间不变；无 `max_staleness_ms`；重放 `sample_id` 保持稳定 |
| RAW/ENGINEERING/BUSINESS | 三个 Delivery 使用同一输入批并分别设置 `output.data_level` | 同一源 Point 身份；RAW 无单位，ENGINEERING 按历史比例/单位，BUSINESS 按 BusinessPoint 标准化；默认未配置等级=BUSINESS |
| Kafka/Redis | 启动真实目标，以独立 Client 读取输出 | 固定 JSON v2 字段、等级、`point_table_id`、`batch_id`、`sample_id`、`business_point_id`、`unit`、值/质量/时间；Kafka Key、Topic 与重复语义正确 |
| Modbus/Gateway | 单独启用 Realtime Delivery，读取实际镜像/变量 | 输出地址编码和点位映射正确；不要求目标寄存器传输 JSON 元数据；保留 Gateway 租约和目标隔离 |
| 格式拒绝 | 注入未知 `schema_version`、缺 `point_table_id`、重复键/测点、非法时间、NaN、Realtime 缺 TTL、Reliable 带 TTL | 严格拒绝，不能自动补全或静默降级 |
| 历史版本 | 先按 PointTable A 采样，发布与之业务解释不同的 PointTable B，然后重放旧消息 | A 的 `point_table_id` 和输出值不改变；不查询最新 B 代替；缓存冷启动及 Configuration 断联路径可复验 |
| 标准点表 | 真实/仿真同类设备与两种型号，共享 BusinessPoint，配置重复映射作为负例 | 逐点业务身份一致，重复/未知 BusinessPoint 被拒绝，单位换算不二次应用 |
| Storage 输入 | 将 Kafka BUSINESS v2 Record 作为后续 Storage 消费样本 | 验证 `device_type`、`device_id`、`point_table_id` 足以标定语义版本，无 `data_version` 或 `config_revision_id` 字段 |

各场景均遵守第5章真实进程/CLI 启动方式，消息中间件及数据库由测试代码单独启动；完整 CLI 与 RPC 覆盖不因新增线格式测试而减少。非正常消息与下游失败必须保留单 Delivery ACK/重试/死信证据；高吞吐条件下分别统计输入、转换与 Adapter 发送阶段。

## 9.6 安全认证复用与负载边界

验证无效访问令牌拒绝、角色和资源级权限拒绝、Configuration 历史配置查询最小权限；重复高频采样不触发逐点令牌鉴别或复杂安全握手。分别统计 RAW、ENGINEERING、BUSINESS 三个等级的端到端吞吐、转换 CPU 和 P99，不能只通过纯编码单测宣称满足 30 台风机的既定容量目标。

# 10. 性能、故障与安全验收

## 10.1 性能

功能验收通过后逐步扩展设备与速率，分别记录 Generator 计算、Gateway 注入、Collector 采集、NATS/JetStream 发布、Sink 两 Worker、四目标的值/s、批/s、P50/P95/P99、CPU/RSS、积压和覆盖/丢弃。Gateway 租约续约、每 Delivery 在途上限与模型完成步查询单独测量。历史其他环境的性能成绩不能作为本次 E2E PASS 证据。

## 10.2 故障、安全与恢复

验证真实服务重启和 READY 假阳性、非法/过期访问令牌、越权操作角色、Gateway 租约失效、配置 revision 冲突、Generation 切换、同 request_id 异内容、Commander 断连 UNKNOWN、Modbus 非法地址及错误标量、NATS/JetStream/Kafka/Redis 中断与恢复、记录文件损坏与隔离资源回收。每个故障有前、中、后状态及真实副作用证据。仅 `accepted/effective_step` 时不可确认 APPLIED，必须通过 Gateway/Generator 请求级查询取得完成证明；不改变 request_id 盲重试。

## 10.1 BusinessPoint 标准和 PointTable 全覆盖验收

在 `configs/templates/business_points_standard.yaml` 和所有 `configs/templates/point_tables/*.yaml` 上执行实际 Core/Configuration 校验。所有 Point 必须**显式**填写 BusinessPoint；展开 PointTable 继承后，任何重复 `business_point_id` 必须拒绝发布。同一物理量在同类设备不同型号及仿真/真实场景中的标准业务 ID 与标准单位必须一致；不同叶片、相别、测点位置、反馈和设定值不可无区分地共用 ID。检查已知 GW2MW ADS 点表多点映射同一业务 ID 的案例，禁止直接跳过或以测试代码补默认值。

输出可审计的全量矩阵 `device_type/device_model/point_table_id/point_id/business_point_id/source_unit/standard_unit/data_type/semantic_decision`，针对未核实 PLC 符号单列现场待核验项，不得将猜测值视为已通过 BUSINESS 级业务验收。验证不存在多余的 `sim_` 仅因仿真来源产生的业务别名，真正仿真内部状态允许单独身份；同时检查所有配置引用、默认缺失回退、同身份不同历史内容冲突。缺覆盖映射或业务定义冲突时标记 FAIL 或 NOT_VERIFIED，不得裁定全链路 BUSINESS PASS。

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
case_id: E2E-07-OUT-04
branch: design/architecture-dfd-alignment-20261113
commit: "<verified_head>"
config_files:
  - configs/instances/devices_sim.yaml
  - configs/templates/deliveries/reliable_history_v1.0.0.yaml
config_hashes: "<actual_hashes>"
processes_started_by_cli: [gateway, data-generator, collector, sink]
infrastructure: [nats, jetstream, kafka]
input: "fixed SampleBatch sequence"
expected: "all accepted reliable samples accounted"
actual: "<run_dir>/kafka-readback.jsonl"
status: NOT_RUN
reason: "template only; not executed"
~~~

# 12. 准入、裁决及实施缺口

## 12.1 完整 E2E 运行前置

1. 各模拟 Service 配置合法且同机端口不冲突；核对现行 Sink `50053`、Generator `50061`、CLI 端点、内网访问授权 身份、Gateway/Commander 路由及测试动态端口。
2. Collector 模拟任务、Sink 四目标实例和 Delivery 与 `devices_sim.yaml` 使用同一套正式模板/点表；不覆盖真实现场设备或另设非法 Generator Instance。
3. Sink GatewayOutput 的租约/权限/目标映射已可测试，独立 Modbus 客户端可读 Sink Server；四出口逐项证明。
4. Generator 现有 Recording 的源批和文件完整性可验证；缺证据必须 INCONCLUSIVE 而不是假设源值。
5. 全部业务进程可被当前 CLI 经真实 Linux 进程管理器启停；Realtime 与 Reliable 分别使用独立 Collector 身份，Sink Main 管理其内部 Worker。业务状态及下游副作用不得以单进程集成测试冒充。
6. Proto 生成物与正式六 Proto 一致，完整枚举当前 51 个服务限定 RPC 声明和全部 CLI 叶子命令，结构归属符合总体/Core 设计；当前 CLI 源码有 73 处命令装饰器声明，须以真实 `--help` 枚举、注册关系及实际执行生成最终分母。

7. CLI 对 Collector 控制面的认证应使用正式配置/环境来源提供管理令牌，按服务契约发送 `x-windhub-admin-token` metadata；不存在明文回退、鉴权关闭或测试专用绕过。必须分别证明已授权与拒绝路径，测试证据不得泄露令牌。
8. CLI 严格 E2E 依次验证全部业务进程 READY、Collector Task 启动、Generation 运行、四出口读回、Commander 写控制、服务重启恢复与关闭收敛；任一步失败不得将其后续步骤计为 PASS。
9. 核对当前 HEAD 的全部 CLI 叶子命令、正式 RPC、已发布 Schema、`configs/` 路径和访问令牌/端口绑定，覆盖矩阵在执行时生成；设计书中的数量和示例不替代运行时实际枚举。

## 12.2 裁决

`PASS`：真实执行、证据完整且符合断言；`FAIL`：执行后违反契约；`NOT_IMPLEMENTED`：正式目标缺实现；`NOT_RUN`：环境或前置不足而未执行；`INCONCLUSIVE`：证据不足。任一未覆盖 CLI/RPC、四目标缺口、三进程 CLI E2E 未执行或 Proto 生成门禁失败，均不得将全套跨模块测试裁决为 PASS。

本文件只定义验收目标；当前 HEAD 的实际测试结果必须另行记录，不援引其他分支的通过报告取代。

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

<h1 class="title">wind-hub 跨模块测试设计</h1>

<p class="subtitle">v0_17 · 20261113 · 统一配置、CLI 进程管理与完整链路验收设计</p>

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

| 领域 | 本分支最新版 | 验收重点 |
|---|---|---|
| 总体 / Core | `wind-hub_总体逻辑架构设计_v1_15.md`、`Core_软件设计_v1_35.md` | DFD 归属、共享配置归 Core、业务专属配置归业务包 |
| Contracts | `Contracts_软件设计_v1_27.md` | 六份 Proto、50 个唯一 Unary RPC、生成绑定 |
| Data Generator | `Data_Generator_软件设计_v1_44.md` | 9 个业务 RPC、模型、录制与执行步证据 |
| Industrial Gateway | `industrial_gateway_软件设计_v1_38.md` | 10 个业务 RPC、租约注入与仿真执行分离 |
| Collector / Commander | `Collector_软件设计_v1_24.md`、`Commander_软件设计_v1_37.md` | 11 个采集 RPC、7 个控制 RPC、批次与子命令身份 |
| Sink / CLI | `Sink_Service_软件设计_v1_57.md`、`CLI_软件设计_v1_37.md` | 三进程双通道、四种下游、CLI 真实进程管理 |

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

Sink 的 Gateway 出站 Adapter **已实现**于 `src/sink_service/infrastructure/adapters/outbound/gateway.py`，以 mTLS 获取租约并更新目标 Gateway；真实 E2E 仍须运行验收。其目标变量空间与源 Gateway 采集变量、控制映射必须隔离。Sink 的 Modbus Realtime 输出为 **Modbus TCP Server**，须由外部 Client 读回；不得假定两个 Server 可以主动互写。

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

## 5.1 配置结构、版本号与 E2E 文件命名

当前真实配置资产如下。目录内容以当前分支 Git HEAD 为准，文件名中的 `_v1.0.0` 是**本次按总体设计修复后的实际版本后缀**，不是仅供参考的目标名称。

~~~text
configs/
├── instances/
│   ├── site.yaml
│   ├── devices.yaml
│   ├── devices_sim.yaml
│   ├── sinks.yaml
│   └── deliveries.yaml
├── services/
│   ├── cli.example.yaml
│   ├── collector.yaml
│   ├── commander.yaml
│   ├── data-generator-service.yaml
│   ├── data-generator-sim.example.yaml
│   ├── industrial_gateway.yaml
│   ├── industrial_gateway-sim.example.yaml
│   └── sink_service.yaml
├── templates/
│   ├── device_models/              # wtg_sim_physical_v1.0.0.yaml 等
│   ├── point_tables/               # modbus_wtg_sim_v1.0.0.yaml 等
│   ├── protocols/                  # modbus_tcp_v1.0.0.yaml 等
│   ├── sinks/                      # gateway_realtime_v1.0.0.yaml 等
│   ├── deliveries/                 # realtime_power_v1.0.0.yaml 等
│   ├── tasks/                      # turbine_all_v1.0.0.yaml
│   ├── observability_v1.0.0.yaml
│   ├── business_points.yaml
│   └── units.yaml
└── history/demo.csv
~~~

<p class="figure-caption">图 5-1 正式配置资产、版本化模板及 E2E 引用边界</p>

**命名约束**：依据总体设计 v1_15 第 5.4 节，带版本文件统一采用 `<前缀><名称>[_vMAJOR.MINOR.PATCH].yaml`；版本号前必须有字母 `v`，YAML 正文 `file_version` 的值仍为不带 `v` 的语义版本字符串。版本后缀是可选的，不能认为 `site.yaml`、`devices.yaml` 等无版本后缀文件违反该规则。路径变更必须同步精确模板解析器、全部 Service 引用和测试 Fixture。

**E2E 命名示例**：若现有点表不满足测试要求，需要新增专用 Modbus 物理仿真点表，可在 `configs/templates/point_tables/` 下采用 `modbus_e2e_physical_v1.0.0.yaml`；此为测试信息名示例，不要求为了新名字复制现有等价点表。现行总体设计还要求类型前缀，须先修订总体设计才能正式采用该简化文件名；未修订前以总体设计的完整前缀规则为准。

**配置仍需适配的部分**：当前 `services/`、`instances/` 等部分文件名称、文件头注释和业务归属尚未全面符合总体设计；本次模板版本后缀整改不代表全部 Service/Instance 命名和 Schema 验收完成。特别是 Data Generator Scenario 应归 Service，不能重新引入 `instances/data-generator*`；真实设备配置不得为 E2E 覆盖。

## 5.2 角色、配置与前置差异

| 角色 | 使用配置 | 实施要求 |
|---|---|---|
| 五设备 | `instances/devices_sim.yaml` + 准确型号与点表 | WTG/PV/BESS/GRID/PPC_SIM01；源 Modbus 端口 15021～15025 |
| Generator | `services/data-generator-sim.example.yaml` | Scenario `wind-plant-physical`、5 设备、0.05 s 步长；确定性场景由合法 Service 场景版本表达 |
| Gateway 源 | `services/industrial_gateway-sim.example.yaml` | gRPC 50073，五 Server；仅 PPC 配置工业控制映射；验证 TLS、路由、证书授权 |
| Collector | `services/collector.yaml`、`templates/tasks/` | 当前仍引用真实 `devices.yaml`；必须建立隔离模拟 Service 和合法采样任务 |
| Sink | `services/sink_service.yaml`、`instances/sinks.yaml`、`instances/deliveries.yaml` | 现有过滤为 WT001/WT002，地址属示例；测试使用隔离实例与四种目标，不能误写真实目标 |
| Sink→Gateway | `templates/sinks/gateway_realtime_v1.0.0.yaml` | 正式 GatewayOutput、target_mappings、mTLS 凭据、独立变量与租约 |
| Commander | `services/commander.yaml` | 模拟控制设备、Gateway 授权与请求身份一致 |
| CLI | `services/cli.example.yaml` | 示例混用 containerd/iSulad/systemd；测试须准备所有业务服务由同一可用 Linux 原生管理器接管的有效配置 |
| 外部 | NATS/JetStream、Redis、Kafka | 独立测试命名空间及清理所有权 |

**已知端口冲突**：Generator 模拟示例和 Sink Service 都使用 50061。必须在真实测试 Service 中明确调整其中一个端口，并同步 CLI 端点、Gateway 控制/Generator 路由、证书和测试断言。`.example.yaml` 不等于已具备同机启动条件。

## 5.3 最少配置原则

测试专属服务配置归 `configs/services/`；有必要隔离 Sink/Delivery 实体时才增加合法 `configs/instances/` 文件。采集任务、仿真场景及运行期参数应归所属业务 Service/模板契约，不能为了 E2E 在 `instances/` 额外创造 `data-generator-*` 等非法层。`schema_version`、`file_version`、`config_sources` 严格遵从各自 Schema；全部路径和实际内容哈希进入测试报告。共享设备、点表、单位和业务点不制作测试副本。

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

# 6. Integration 分段测试

所有分段沿用第 5 章正式配置和 CLI 进程管理，不复制配置树；若某前置配置尚未落地，则该行保留为 NOT_RUN/NOT_IMPLEMENTED。每行执行前置检查→CLI 启服务→注入激励→独立读回→关闭并清理。

| 用例 | 配置、CLI 启动的进程 | 激励与执行步骤 | 预期输出和断言 |
|---|---|---|---|
| I-01 源注入 | `devices_sim.yaml`、DG/Gateway 仿真 Service；Generator、Gateway | 先 Gateway READY → Generation start → 固定风速和模型输出 → Gateway 读回 → stop | 源记录和 Gateway 值按时间、点位、版本对齐，租约合法 |
| I-02 工业采集 | 前项 + Collector 模拟 Service/任务；Gateway、Collector | Modbus 输入变化 → Task start → ReadPoints 和任务采样 → stop | 读数、缩放、质量与定时发布分别可证 |
| I-03 NATS 双通道 | Collector/Gateway + NATS/JetStream | 订阅实际 Subject → 启 Task → 抓双批 → stop | SampleBatch、设备/点身份、源时间、Realtime/Reliable 语义 |
| I-04 Sink 四目标 | Sink 测试 Service、隔离 Sink/Delivery + Gateway/Redis/Kafka；Sink、目标 Gateway | 固定标准 SampleBatch 注入真实 NATS → Delivery start → 四目标读回 → stop | Gateway 更新、Modbus 寄存器、Redis 最新值、Kafka 历史分别断言 |
| I-05 工业仿真控制 | Gateway/Commander/Generator 仿真配置；三进程 | 工业 Client FC06/FC16 → Commander → Gateway 执行 → Generator 查询 → 重复/无权测试 | 同 command_id、不直接转发、PENDING→APPLIED/UNKNOWN，受理非成功 |
| I-06 CLI 生命周期 | CLI 有效 Service + 五业务进程 | start → status/READY → restart → stop | Unit/PID/业务状态和 Sink Worker 退出 |
| I-07 Gateway Realtime | Sink Gateway 模板/映射 + 独立目标 Gateway；两进程 | mTLS ACQUIRE → UpdateVariables → ReadVariables → RELEASE/过期 | 值与映射正确，租约及回环隔离 |
| I-08 配置治理 | Core 配置、各业务 Service | 版本冲突、无效路径、旧结构、非法实例键 → Load/Apply | 拒绝错误配置，快照不可变，归属不混乱 |
| I-09 全 RPC | 六 Proto 和全部服务 | 自动枚举 → 真实 RPC 调用及拒绝路径 → 检查 Descriptor | 50/50 唯一接口与各服务 Common 注册，证据完整 |

I-04 的标准批次 Publisher 是**分段激励**；只有 E2E-07 通过完整 Generator→Collector→Sink 路径，才能确认全链路结果。

# 7. E2E 完整场景

## 7.1 通用执行步骤

1. 核实 Git HEAD、六份 Proto 的生成一致性、配置哈希、CLI 命令清单、证书身份、测试目标和端口。
2. 中间件准备器启动 NATS/JetStream、Redis、Kafka；只通过 CLI 启动各业务进程，验证 PID/READY，再启动 Generation、Gateway、Collector Task 和 Sink Delivery。
3. 对选定模型实施确定性激励，收集 Generator 原始 JSONL、Gateway、Collector、NATS/JetStream、Sink Gateway/Modbus/Redis/Kafka 读回。
4. 对控制场景记录 Commander 批次 request_id、子命令 command_id、Generator generation_id、effective_step 和查询结果；不得用准入代替完成。
5. 按第 9～10 章分别判断数值、时间、可靠性、安全与恢复；保存证据，CLI 清理业务进程，中间件管理器清理测试资源。

## 7.2 E2E 场景矩阵

| 用例 | 配置与必需进程 | 激励与执行 | 独立输出判据 |
|---|---|---|---|
| E2E-01 WTG | 五设备仿真、WTG 物理 Scenario、五业务服务及四目标 | 平均风速 10→12 m/s，湍流度 0；多步运行 | 四目标和源记录可追溯，不假定阶跃立即稳态 |
| E2E-02 PV | 同上 PV Scenario | 辐照 0→800→1000 W/m²，温度条件固定 | PV 容量边界、单位与四出口一致 |
| E2E-03 BESS | BESS Scenario | 充电/放电阶跃、SOC 边界、越界请求 | 状态连续、非法输入拒绝、正负号/缩放正确 |
| E2E-04 GRID/PPC | GRID/PPC、Commander、Gateway、Generator | 频率/电压激励、AGC/AVC/FFR 合法命令、重放/无权 | 真正 APPLIED 才为控制成功，失败/未知不冒充；测量反馈一致 |
| E2E-05 随机/CSV | DG Service 合法 Scenario、`history/demo.csv` | 固定种子重复运行、同 CSV 重放 | 原始序列可复现、实际已发可靠批顺序一致 |
| E2E-06 多速率 | DG 物理 Scenario + Collector 合法周期 | 生成 50/100 ms、采集 50/100/200/1000 ms 候选组合 | 实测采样窗和覆盖；不要求每一步必采 |
| E2E-07 四种 Sink | 五业务进程、所有隔离 Sink/Delivery、Gateway/Modbus/Redis/Kafka | 单次序列与阶跃，正常和故障恢复两轮 | OUT-01～04 分别裁决；任何缺口显式保留 |
| E2E-08 CLI/RPC | 全部服务及 CLI | 逐叶子命令和逐 RPC 的正常/失败路径 | 全量覆盖清单和真实副作用证据 |
| E2E-09 生命周期 | CLI 有效测试配置 | 全服务 start/stop/restart、重复调用 | 进程/业务状态独立判断，Worker 无残留 |
| E2E-10 故障与恢复 | 同上 + 受控基础设施故障 | 断 NATS、Kafka、Redis、Gateway，旧 generation 和重复 ID | Realtime 覆盖、Reliable 重投/DLQ、UNKNOWN 与授权隔离 |

## 7.3 四个 Sink 目标分别验收

| ID | 通道与目标 | 实际输入和独立读回 | 必须验证 |
|---|---|---|---|
| OUT-01 | Realtime → Industrial Gateway | 正式 GatewayOutput、mTLS `ClaimTargets/UpdateVariables` → Gateway ReadVariables | target_mappings、租约、权限、值更新及目标与源隔离 |
| OUT-02 | Realtime → Modbus TCP | Sink Modbus Server → 独立 Modbus TCP Client | 寄存器地址、类型、缩放、大小端和最新值；非主动写第三方 Server |
| OUT-03 | Realtime → Redis | Sink Redis 写入 → 独立 Redis Client | 来源标识、时间与最新值覆盖 |
| OUT-04 | Reliable → Kafka | JetStream→Sink Reliable→Kafka → 独立 Consumer | 应交付的真实源批、顺序、ACK、重投与下游去重界限 |

四个 OUT 的 PASS 不可互相代替。工业控制 E2E 作为 E2E-04 单独裁决，不与 Gateway 测量注入混为一谈。

# 8. CLI 与 gRPC 全功能覆盖

## 8.1 自动枚举并生成测试矩阵

六份正式 Proto 当前有 **50 个唯一 Unary RPC**：Common 9、Collector 11、Commander 7、Sink 4、Gateway 10、Generator 9。Common 在多个服务实例中分别注册，唯一方法总数与逐服务注册总数不能混用。Gateway 的 `ExecuteSimulationCommand/GetSimulationExecutionResult`、Generator 的 `GetInputExecution` 必须进入 RPC 覆盖清单。除真实调用外还要验证 Descriptor 请求/响应、字段号、标量 oneof、认证与拒绝语义。

CLI 先读取当前 `wind-hub --help` 与全部子级帮助，枚举**所有叶子命令**并逐条真实执行，所有业务操作必须通过 CLI；每项至少一项合法或规范拒绝场景。所有未执行 RPC/CLI 均保留在分母。无法由 CLI 覆盖但正式公开的 RPC 可用真实认证客户端调用，注明原因。

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

# 10. 性能、故障与安全验收

## 10.1 性能

功能验收通过后逐步扩展设备与速率，分别记录 Generator 计算、Gateway 注入、Collector 采集、NATS/JetStream 发布、Sink 两 Worker、四目标的值/s、批/s、P50/P95/P99、CPU/RSS、积压和覆盖/丢弃。Gateway 租约续约、每 Delivery 在途上限与模型完成步查询单独测量。历史其他环境的性能成绩不能作为本次 E2E PASS 证据。

## 10.2 故障、安全与恢复

验证真实服务重启和 READY 假阳性、非法/过期 mTLS 证书、越权 CN、Gateway 租约失效、配置 revision 冲突、Generation 切换、同 request_id 异内容、Commander 断连 UNKNOWN、Modbus 非法地址及错误标量、NATS/JetStream/Kafka/Redis 中断与恢复、记录文件损坏与隔离资源回收。每个故障有前、中、后状态及真实副作用证据。仅 `accepted/effective_step` 时不可确认 APPLIED，必须通过 Gateway/Generator 请求级查询取得完成证明；不改变 request_id 盲重试。

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

1. 各模拟 Service 配置合法且同机端口不冲突；特别解决 Generator 与 Sink 示例均占 50061，并核验 CLI、mTLS、Gateway/Commander 路由。
2. Collector 模拟任务、Sink 四目标实例和 Delivery 与 `devices_sim.yaml` 使用同一套正式模板/点表；不覆盖真实现场设备或另设非法 Generator Instance。
3. Sink GatewayOutput 的租约/权限/目标映射已可测试，独立 Modbus 客户端可读 Sink Server；四出口逐项证明。
4. Generator 现有 Recording 的源批和文件完整性可验证；缺证据必须 INCONCLUSIVE 而不是假设源值。
5. 全部业务进程可被当前 CLI 经真实 Linux 进程管理器启停，三进程控制链不得以单进程集成测试冒充。
6. Proto 生成物与正式六 Proto 一致，完整枚举当前 50 RPC 和 CLI 叶子命令，结构归属符合总体/Core 设计。

## 12.2 裁决

`PASS`：真实执行、证据完整且符合断言；`FAIL`：执行后违反契约；`NOT_IMPLEMENTED`：正式目标缺实现；`NOT_RUN`：环境或前置不足而未执行；`INCONCLUSIVE`：证据不足。任一未覆盖 CLI/RPC、四目标缺口、三进程 CLI E2E 未执行或 Proto 生成门禁失败，均不得将全套跨模块测试裁决为 PASS。

本文件只定义验收目标；当前 HEAD 的实际测试结果必须另行记录，不援引其他分支的通过报告取代。

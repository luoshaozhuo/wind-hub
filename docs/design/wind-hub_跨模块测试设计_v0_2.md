<h1 class="title">wind-hub 跨模块测试设计</h1>

<p class="subtitle">v0_2 · 20261111 · 详细初稿（待审核）</p>

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
3. [范围、基线与判定](#3-范围基线与判定)
4. [测试拓扑与数据契约](#4-测试拓扑与数据契约)
5. [Integration 设计](#5-integration-设计)
6. [E2E 设计](#6-e2e-设计)
7. [Performance 设计](#7-performance-设计)
8. [Reliability 设计](#8-reliability-设计)
9. [配置与环境](#9-配置与环境)
10. [测试组织与 Marker](#10-测试组织与-marker)
11. [验收与追溯](#11-验收与追溯)
12. [审核决策](#12-审核决策)

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

---


# 2. 文档定位与使用方式

## 2.1 适用对象

本设计用于 wind-hub **跨模块 Integration、完整 E2E、Performance、Reliability 及全套 CLI 功能验收**；基线为 `audit/design-contract-consistency-20261111` R1 验收后的十份设计。单模块测试仍以对应设计为准。本设计不取代 Contracts、Core、Configuration 的契约权威。

## 2.2 目标

- 以 **GW2MW 三台风机、两台 Modbus Gateway、一个 Data Generator、Collector、Sink、Commander、CLI、NATS/JetStream、Redis、Kafka** 构建可追踪的完整物理仿真闭环。
- 每台风机不超过 30 个变量，含 AGC、AVC、快速调频和 CLI 必需控制点；三台设备共用一个 GW2MW 物理模型、参数可按设备覆盖。
- 提供**完整可实现的 configs 设计**：文件树、逐文件职责、引用关系、数据模型参数、设备实例、点表映射、权限、Sink Delivery、运行场景、端口、证书与检测脚本。
- 明确 Gateway 上游允许 `plc`、`data_generator`、`none`，并设计 Generator 对控制反馈的有状态响应。
- 将自动验收的正确值、范围、时限和观测渠道写进场景矩阵，供 pytest 与手工 CLI/Redis/Kafka 验证。
- **本版本为设计审核稿：不实施任何跨模块测试及生产能力开发。** 需要改动其他模块设计时必须逐份升版、先审核后实施。

---

# 3. 测试范围与真实系统边界

## 3.1 逻辑拓扑

```mermaid
%%{init: {"flowchart": {"useMaxWidth": true}}}%%
flowchart LR
    CLI["CLI / 手工操作"] -->|"Modbus 控制写入，或 Gateway 控制入口"| G2["Gateway 2 / Modbus Server"]
    DG["Data Generator / 3×GW2MW"] -->|"gRPC 批量更新测量变量"| G1["Gateway 1 / Modbus Server"]
    G1 -->|"Modbus TCP 测量采集"| COL["Collector"]
    COL -->|"Realtime / Reliable"| N["NATS / JetStream"]
    N --> SINK["Sink Service"]
    SINK -->|"Realtime"| SM["Sink Modbus / 只读 Server"]
    SINK -->|"Realtime"| REDIS[("Redis 最新值")]
    SINK -->|"Reliable"| KAFKA[("Kafka 消息")]
    SM -.->|"待批准：Modbus Client 同步"| G2
    G2 -->|"控制写事件"| CMD["Commander"]
    CMD -->|"Modbus TCP 控制写"| G1
    G1 -->|"ReadVariables 读取控制状态"| DG
    DG -->|"物理模型下一周期响应"| DG
```

<p class="figure-caption">图 3-1 双链路、三目标与有状态物理反馈拓扑；虚线为待实现的显式协议同步职责</p>

- **控制入口修正**：CLI 对 Gateway 2 的真实 Modbus TCP 控制寄存器写入才会触发 `Gateway2 → Commander → Gateway1`。现有 `cli gateway update` 调用的是 `UpdateVariables`，其正式契约为**只更新 Gateway 自有变量而不触发工业命令转发**；不得将该命令伪装成控制闭环。要覆盖 CLI 控制，必须用 CLI 的正式 `proto`/工业写入命令（需核实具体命令），或单独评审新增“经授权的控制写入”能力。
- **Gateway 1**：控制点由 Commander 通过 Modbus 写入；测量点由 Generator 通过 gRPC 写入；`control_enabled=false` 防止控制循环反向转发；Generator 定期通过 `ReadVariables` 获取控制点并驱动下一次仿真。
- **Gateway 2**：工业 Modbus Server，控制点使能 Commander Forwarder；测量点反映 Sink 最新值。Sink Modbus Output 同样是只读 Modbus Server，不能直接写 Gateway 2，需明确额外 Modbus Client 的实现位置、授权及原子映射。
- **三种 Sink**：Modbus 最新值、Redis 最新值、Kafka Reliable 消息必须同时运行。不是 TDengine。
- **真实环境**：完整 E2E 禁止以 Mock 替代上述模块、Broker 或目标；分段 Integration 允许在场景明示的受控替身。

## 3.2 Gateway 上游类型契约（待同步 Gateway 设计）

| `upstream.kind` | 上游定义 | 数据路径与控制写入 |
|---|---|---|
| `plc` | 真实 PLC 或受控工业设备 | 工业协议客户端采集真实 PLC；合法控制命令写至 PLC；Gateway 本身仍可以向外提供 Modbus Server |
| `data_generator` | Data Generator 虚拟物理设备 | Generator gRPC 注入测量值；控制变量先落 Gateway Store，Generator 读取并作用于模型 |
| `none` | 无现场设备上游 | 仅 Gateway 本地变量 Store 与合法外部更新；不隐式转发、模拟物理响应 |

配置必须显式声明上游类型，不能通过空 endpoint 推断；`plc` 需要协议、连接和映射，`data_generator` 需要已授权源和变量所有权，`none` 禁止虚构上游测量。**现行 Gateway 源码尚未实现该完整枚举契约，应升级其设计后由生产开发实现。**

## 3.3 设备与服务数量

| 对象 | 数量 | 实际职责 |
|---|---:|---|
| GW2MW 模型 | 1 种 | 2 MW 水平轴变速变桨机组，三台独立状态 |
| GW2MW 设备 | 3 台 | `GW01`、`GW02`、`GW03`，每台 29 点 |
| Gateway 实例 | 2 进程 | Gateway 1 与 Gateway 2，每台承载三个独立 Modbus Server 端口/设备实例 |
| Generator、Collector、Sink、Commander | 各 1 | 一个 Generation 对三台机组；Collector 和 Sink 批量处理 |
| NATS/JetStream、Redis、Kafka | 各 1 实体服务或集群 | Kafka 用 KRaft 测试 Broker；不能用 Fake 代替 |
| CLI | 1+ 会话 | 命令与服务进程分离；支持自动和手动执行 |

本设计的“3 台”指 **3 台独立 GW2MW 风机设备**，不是三个型号，也不是简单复制同一测量序列。每台维护独立转子速度、桨距角、功率、指令、模型时间和运行状态。

---

# 4. 测试数据：GW2MW 单机 29 变量

## 4.1 变量清单

以下 ID 是**拟新增的 E2E Modbus 共享测试业务点 ID**，不是声称原 ADS Symbol 中同名点均已具备。以现有 `ads_wtg_gw2mw_1.0.0.yaml` 的风速、变桨、变流、发电机等真实语义为参考，测试点表需定义独立的 Modbus 地址和数据类型；不覆盖原 ADS 点表。

<p class="table-caption">表 4-1 每台 GW2MW 的 29 个测试变量</p>

| # | 测试 point_id | 方向 | 单位/类型 | 功能与 CLI 观察 |
|---:|---|---|---|---|
| 1 | `wind_speed` | 测量 | m/s, float32 | 风速输入 |
| 2 | `rotor_speed` | 测量 | rpm, float32 | 转子转速 |
| 3 | `generator_speed` | 测量 | rpm, float32 | 发电机转速 |
| 4 | `pitch_angle` | 测量 | deg, float32 | 实际桨距角 |
| 5 | `yaw_angle` | 测量 | deg, float32 | 偏航角 |
| 6 | `air_density` | 测量 | kg/m³, float32 | 空气密度 |
| 7 | `aerodynamic_power` | 测量 | kW, float32 | 气动功率 |
| 8 | `available_power` | 测量 | kW, float32 | 可利用有功 |
| 9 | `active_power` | 测量 | kW, float32 | 实际并网有功 |
| 10 | `reactive_power` | 测量 | kvar, float32 | 实际无功 |
| 11 | `grid_voltage` | 测量 | V, float32 | 并网电压 |
| 12 | `grid_current` | 测量 | A, float32 | 并网电流 |
| 13 | `grid_frequency` | 测量/输入 | Hz, float32 | 电网频率 |
| 14 | `power_factor` | 测量 | 1, float32 | 功率因数 |
| 15 | `generator_temperature` | 测量 | °C, float32 | 机舱发电机温度 |
| 16 | `converter_temperature` | 测量 | °C, float32 | 变流器温度 |
| 17 | `running_status` | 状态 | bool | 运行标志 |
| 18 | `fault_status` | 状态 | bool | 故障标志 |
| 19 | `agc_power_setpoint` | 控制 | kW, float32 | AGC 限功率指令 |
| 20 | `avc_voltage_setpoint` | 控制 | V, float32 | AVC 电压目标 |
| 21 | `avc_reactive_setpoint` | 控制 | kvar, float32 | AVC 无功目标 |
| 22 | `ffr_power_command` | 控制 | kW, float32 | 快速调频附加有功 |
| 23 | `ffr_enable` | 控制 | bool | 快速调频允许 |
| 24 | `agc_enable` | 控制 | bool | AGC 允许 |
| 25 | `avc_enable` | 控制 | bool | AVC 允许 |
| 26 | `start_command` | 控制 | bool | 启机 |
| 27 | `stop_command` | 控制 | bool | 停机 |
| 28 | `reset_fault_command` | 控制 | bool | 故障复位 |
| 29 | `control_result` | 反馈 | enum/int16 | 最近一条控制指令执行状态 |

单位代码必须映射到 `units.yaml` 的实际单元 ID；`air_density`、`rpm` 等如无现成有效编码，则在隔离测试业务点目录中采用合法的现有量纲/单位配置，并同步 Configuration 设计，禁止自造不受 Schema 接受的字符串。三台机组每台 29 点，合计 87 点。

## 4.2 点位与协议分区

- 各风机独立 Modbus 监听端口，Unit ID 默认为 1；数据类型以正式 Core Modbus Codec 可接受的类型为准。
- 测量点使用 **Input Registers/Discrete Inputs（只读）**，控制点使用 **Holding Registers/Coils（可写）**，状态码使用 Input Registers；不允许地址重叠。
- `float32` 占两个 16 位寄存器，`int16` 占一个，`bool` 优先使用 Coil/Discrete Input；按正式寄存器编解码规则配置 `word_order`。
- GW1 和 GW2 的同一业务点允许采用不同协议地址，但必须经显式映射表关联，不通过字符串拼接臆造。
- `agc_power_setpoint` 与 `ffr_power_command` 不能与实际 `active_power` 共用同一物理寄存器；`UpdateVariables` 更新的 Store 和工业 FC06/FC16 控制写入需遵守不同语义。
- CLI 变量发现与读取至少覆盖 29 点；工业控制命令至少覆盖 #19–#28，`control_result` 可核验拒绝/受理/执行。只读数据不得被写成控制数据。

---

# 5. 有状态物理模型：三台 GW2MW

## 5.1 模型类型与边界

采用 **固定步长、离散时间、确定性风机简化动态模型**，由气动功率、转动惯量、变桨一阶滞后、并网有功一阶跟踪、无功/电压响应及快速调频叠加构成。该模型用于系统软件测试，**不是气弹耦合工程认证模型**；不模拟复杂叶片弹性、塔架多体动力学、PWM 电磁暂态或真实电网潮流。

每台风机状态独立；风速和电网扰动作为给定外部输入，随机种子可选但必须可复现。无风、故障、停机时输出符合模型限幅和状态机规则。数值积分采用统一离散步长，步长必须小于最短动态时间常数的明确稳定性限制。

## 5.2 风能捕获

标准标量关系（标量普通字体、几何矢量及惯量张量符号遵从《符号体系.md》）：

$$
P_{\mathrm{aero}}=\frac{1}{2}\rho A C_p(\lambda,\beta)v^3
$$

$$
\lambda=\frac{\omega R}{v}
$$

其中 `P_aero` 是气动功率，`rho` 空气密度，`A` 叶轮扫掠面积，`R` 叶轮半径，`v` 风速，`omega` 转子角速度，`beta` 桨距角。避免 `v=0` 的除零；低风速应进入停机/零功率分支。\(C_p\) 函数采用**有界、非负、可复现的经验曲面/曲线模型**，需指定确定参数与 \(C_p\leq 0.593\) 的物理约束；不得简单把 `P ∝ v³` 当作所有工况的无条件正确模型。

## 5.3 转子动力学、变桨与电功率

采用轴向折算后的等效转动惯量（若采用完整几何模型，惯量张量符号统一为 \(\boldsymbol J\)，不得用 \(\boldsymbol I\) 表示惯量）：

$$
J_{\mathrm{eq}}\frac{\mathrm d\omega}{\mathrm dt}
=T_{\mathrm{aero}}-T_{\mathrm{gen}}-B\omega
$$

$$
\tau_{\beta}\frac{\mathrm d\beta}{\mathrm dt}
=\beta_{\mathrm{ref}}-\beta
$$

$$
\tau_P\frac{\mathrm dP_{\mathrm{grid}}}{\mathrm dt}
=P_{\mathrm{target}}-P_{\mathrm{grid}}
$$

其中 \(T_{\mathrm{aero}}=P_{\mathrm{aero}}/\omega\) 只在安全转速区有效，低速用受限转矩模型。桨距参考由额定转速/目标功率形成的受限控制器确定；桨距角与角速度均需饱和。发电功率 \(P_{\mathrm{grid}}\) 的目标不得超过变流器限值或风可利用功率；动态功率采用有界斜率并保持非负，停机阶段例外采用明确退出响应。

## 5.4 AGC、AVC、快速调频叠加

**AGC** 是基础有功约束。按许可范围裁剪（执行状态异常时采用安全默认行为）：

$$
P_{\mathrm{base}}=\min(P_{\mathrm{available}},P_{\mathrm{rated}},P_{\mathrm{AGC}})
$$

**快速调频**采用显式启用信号、频率偏差、死区和附加调频功率的组合，附加量可由控制寄存器 `ffr_power_command` 给定，或按配置的下垂系数从频差生成，禁止同时重复计算：

$$
P_{\mathrm{target}}=
\operatorname{sat}_{[0,P_{\mathrm{available}}]}
\left(P_{\mathrm{base}}+\Delta P_{\mathrm{FFR}}\right)
$$

AGC、快速调频动作的**优先级与限功率允许范围必须可配置且在设计中固定**，不能隐式突破机组的额定电功率、变流器容量、变桨约束。此处默认 `ffr_power_command=0`，AGC 限功率可单独验收。

**AVC** 以无功目标（或电压误差经下垂控制转为无功目标）驱动一阶无功响应，受变流器视在功率约束：

$$
\tau_Q\frac{\mathrm dQ}{\mathrm dt}=Q_{\mathrm{target}}-Q
$$

$$
P_{\mathrm{grid}}^2+Q^2\leq S_{\mathrm{rated}}^2
$$

单独测试电压设定模式和无功设定模式，二者同时出现时以配置的唯一模式为权威；测试环境非电网潮流仿真，`grid_voltage` 是外生电网条件或明确的简化网络方程响应，不得声称单机 Q 调节必然使真实母线电压按同一幅度变化。

## 5.5 数值更新与反馈闭环

```mermaid
%%{init: {"flowchart": {"useMaxWidth": true}}}%%
flowchart TB
    A["每个固定离散步开始"] --> B["读取 Gateway 1 的控制点快照与版本"]
    B --> C{"新命令且授权有效？"}
    C -->|"是"| D["应用 AGC/AVC/FFR/启停并更新指令状态"]
    C -->|"否"| E["沿用上一步有效指令"]
    D --> F["离散积分气动/转子/变桨/P/Q/状态"]
    E --> F
    F --> G["限幅、量纲/有限值/状态校验"]
    G --> H["按有效租约批量 UpdateVariables 至 Gateway 1 测量点"]
    H --> I["记录 step、指令版本、预测值与实测输出"]
```

<p class="figure-caption">图 5-1 单风机模型步进与控制反馈流程</p>

- 命令作用于下一有效仿真步；必须定义一致的命令读取截止时间及快照序号，避免半步读取不同指令版本。
- Generator 不得以普通数据生成过程覆盖 Gateway 1 中由 Commander 写入的**控制点**；租约仅针对 Generator 拥有的**测量反馈点**。
- Generator 在 `ReadVariables` 获取三台的控制变量（当前已有该 RPC）后，将差异写入独立模型状态并保存最后消费的命令版本/值；控制幂等、过期及未知结果应明确。
- 当前 `src/data_generator/application/models.py` 的实现**仅支持 `random` 与无状态 `static/linear_table`**，不具有上述动态闭环。实现本模型需先升级 Data Generator 设计及生产应用模块，且不能伪装为现有 `static` Model。
- 参数由实例/模板配置，统一包括：额定功率、额定视在功率、叶轮半径、空气密度、额定转速、等效惯量、摩擦、\(C_p\) 曲面参数、转矩上限、变桨角/速率、功率与无功时间常数、P/Q 斜率限值、频率死区、下垂系数、启停逻辑以及初始状态。全部参数可配置，文档附可重放样例值而不把样例值写死为默认规范。

## 5.6 强制 AGC 案例：2000 → 1000 kW

前置：GW01 风速足够使 `available_power ≥ 2000 kW`，已并网、无故障、AGC 使能、FFR 关闭，初始 `agc_power_setpoint=2000 kW`、`active_power` 近 2000 kW。

1. CLI 经**真正的 Modbus 工业写入口**在 Gateway 2 上将 GW01 的 AGC 设定从 2000 kW 改为 1000 kW。
2. Gateway 2 记录 Modbus 写入并转发 Commander；Commander 经 Modbus 写入 Gateway 1 的控制寄存器。
3. Generator 读取 Gateway 1 的 AGC 值 1000 kW，更新 GW01 模型状态并在后续积分步响应；GW02、GW03 不应受影响。
4. GW01 的 `active_power` **随模型时间常数/斜率逐步下降**，最终稳定于 1000 kW 的容差内；不能瞬间强行将测量点写为 1000 冒充物理响应。
5. 从 Gateway 1 读数、Collector 样本、Sink Modbus 寄存器、Redis 最新值、Kafka Reliable 消息验证同一物理变化；Gateway 2 反馈经待审核的 Modbus 数据同步链路确认。
6. 反向把 AGC 从 1000 调回 2000；受风速、功率斜率和限制约束，最终值不得超过可利用功率及额定功率。

自动验收对稳态终值要求 `abs(P-1000) ≤ 20 kW` **作为提议的初始测试容差**；收敛时间上限必须由 \(\tau_P\)、斜率及采样间隔推导并写入 configs，而非任意设定 1 秒成功。全过程记录事件时间与 `command_id`/模型 step，以证明因果方向。



---

# 6. 完整 configs 设计（核心验收输入）

## 6.1 文件层次及作用

```text
tests/e2e/configs/
├── services/
│   ├── data-generator-service.yaml
│   ├── gateway1.yaml
│   ├── gateway2.yaml
│   ├── collector.yaml
│   ├── commander.yaml
│   ├── sink_service.yaml
│   └── cli.yaml                       # 仅在现行 CLI Schema 允许时
├── instances/
│   ├── site.yaml
│   ├── devices.yaml                   # GW01/GW02/GW03：GW2MW Modbus
│   ├── data-generator.yaml            # 一场景 × 三台独立有状态风机
│   ├── sinks.yaml                     # 3 个 Sink 实例
│   └── deliveries.yaml                # 3 个独立 Delivery
├── templates/
│   ├── protocols/
│   │   └── modbus_tcp_1.0.0.yaml
│   ├── device_models/
│   │   └── wtg_gw2mw_e2e_1.0.0.yaml
│   ├── point_tables/
│   │   └── modbus_wtg_gw2mw_e2e_1.0.0.yaml
│   ├── tasks/
│   │   └── gw2mw_e2e_all_1.0.0.yaml
│   ├── sinks/
│   │   ├── e2e_modbus_1.0.0.yaml
│   │   ├── e2e_redis_1.0.0.yaml
│   │   └── e2e_kafka_1.0.0.yaml
│   ├── deliveries/
│   │   ├── e2e_rt_modbus_1.0.0.yaml
│   │   ├── e2e_rt_redis_1.0.0.yaml
│   │   └── e2e_rel_kafka_1.0.0.yaml
│   ├── business_points.yaml
│   └── units.yaml
├── model/
│   ├── gw2mw_physics.yaml             # 待新增的 Generator 物理模型配置契约
│   └── wind_profiles.yaml             # 三台机组确定性风速、频率输入
└── security/
    └── identities.yaml               # 仅存证书身份/路径映射，不存私钥正文
```

注意：上表是最终建议的**测试配置资产结构**，并非声称每个目录/文件均已被现行加载器接受。配置读取入口必须保持已冻结的 `config_sources` 明确路径规则；`model/` 是否作为 `data_generator.yaml` 的正式可引用子资源，必须先通过 Generator/Configuration Schema 设计审查。证书、端口、Kafka Topic、Redis Key 和 NATS Durable 在运行时拷贝到临时环境，禁止测试直接写生产 `configs/`。

## 6.2 各文件的配置契约矩阵

<p class="table-caption">表 6-1 各类配置文件、权威来源与校验</p>

| 文件 | 权威读取方 | 必要字段及约束 | 复用/调整 |
|---|---|---|---|
| `site.yaml` | Configuration/Core | `schema_version`、`file_version`、`site_id`、`name` | 复用现行结构 |
| `devices.yaml` | Collector、Gateway、Commander、Generator | 3 个设备、精确 `model.id/version`、各自 `endpoint.host/port` | 从现有 GW01–03 裁剪；模型引用改为测试 Modbus 版本 |
| `modbus_tcp_1.0.0.yaml` | Core | `mode=tcp`、`slave_id`、`timeout`、`word_order` | 复制现有正式模板，端口不存于协议模板 |
| `wtg_gw2mw_e2e_1.0.0.yaml` | Configuration/Core | 协议、点表、任务的版本精确引用；`device_type_id=turbine` | 参照正式 `wtg_gw2mw_1.1.0.yaml`；测试 Modbus 专用 |
| `modbus_wtg_gw2mw_e2e_1.0.0.yaml` | Core/Collector/Gateway | 29 点 `business_point_id`、类型、地址、单位、scale/offset、point_groups | 新增精简点表，必须通过 Core 地址冲突/重叠校验 |
| `gw2mw_e2e_all_1.0.0.yaml` | Collector/Core | 固定采样周期、点组、批量读取预算 | 精确引用现有 Task 模板结构 |
| `data-generator.yaml` | Data Generator | scenario、source、3 devices、model、gateways、feedback_mapping | 拓展现有场景字段以承载有状态模型 |
| `gw2mw_physics.yaml` | Data Generator | 物理参数、初始状态、限值、模型数值步长、响应模式 | **新模型 Schema，待正式设计升级** |
| `wind_profiles.yaml` | Data Generator | 三设备时间序列、频率/电压扰动、确定性采样 | **新模型输入资源，待正式设计升级** |
| `gateway1.yaml` | Gateway 1 | 明确 `config_sources`、3 个 Server、Modbus 端口、`control_enabled=false` | 增补 `upstream.kind=data_generator` 需 Schema 升级 |
| `gateway2.yaml` | Gateway 2 | 3 个 Server、控制映射及 Commander Endpoint | 增补测量源同步责任需 Schema/组件评审 |
| `collector.yaml` | Collector | 单独设备/模板路径、NATS、采样周期、每批点数/容量 | 参照当前 `configs/services/collector.yaml` |
| `commander.yaml` | Commander | 三设备控制、证书、审计、幂等、总并发/超时 | 参照现行 `commander.yaml`；不能关闭 mTLS 绕过 |
| `sink_service.yaml` | Sink Service | `service_id`、`wind_farm_id`、`deployment_id`、`config_root`、`config_sources.instances.sinks/deliveries`、NATS、运行额度 | 复制正式服务 Schema，替换测试独立值 |
| `sinks.yaml` | Sink Service | Modbus/Redis/Kafka 各 1 实例及对应模板版本/连接 | 模板实例结构以 `src/sink_service/application/config.py` 为准 |
| `deliveries.yaml` | Sink Service | 三条独立 Delivery，每条唯一 `delivery_id`、`sink_id`、`source`、模板版本 | Realtime→Modbus/Redis；Reliable→Kafka |
| `templates/sinks/*.yaml` | Sink Service | 每种 Adapter 的 `kind`、`capabilities`、`connection`、`output` | 复制正式模板，`modbus_tcp` 只读 Server |
| `templates/deliveries/*.yaml` | Sink Service | `delivery_mode`、`source_filter`、触发策略、输出规则 | Realtime/Reliable 必须分开 |
| `business_points.yaml` / `units.yaml` | Configuration/Core | 正式业务点和单位词汇，精确类型与量纲 | 从现有文件做最小有效子集，不改变全局词汇语义 |
| `identities.yaml` | 测试环境编排器 | 证书 CN、角色、访问范围、密钥文件路径 | 仅测试侧映射，不替代任何服务安全配置 |

## 6.3 三台 GW2MW 设备实例：建议样例

以下示例仅使用已核实的设备实例字段，型号 ID 指向拟新增测试模板：

```yaml
schema_version: 1
file_version: "1.0.0"
devices:
  GW01:
    name: "GW2MW-E2E-01"
    enabled: true
    model: {id: wtg_gw2mw_e2e, version: "1.0.0"}
    endpoint: {host: 127.0.0.1, port: 15101}
    overrides: {}
  GW02:
    name: "GW2MW-E2E-02"
    enabled: true
    model: {id: wtg_gw2mw_e2e, version: "1.0.0"}
    endpoint: {host: 127.0.0.1, port: 15102}
    overrides: {}
  GW03:
    name: "GW2MW-E2E-03"
    enabled: true
    model: {id: wtg_gw2mw_e2e, version: "1.0.0"}
    endpoint: {host: 127.0.0.1, port: 15103}
    overrides: {}
```

Gateway 1 对应服务端口建议 15101–15103；Gateway 2 对应 15201–15203；Sink Modbus Output 15301（单实例可包含 87 点）。**这些是测试示例端口，不是生产默认值**；测试运行时须检查无冲突，并将 Gateway 2 所需的设备引用和其自己监听端口正确分离。若当前 Gateway Schema 不能把设备 endpoint 和 Server listen_port 独立表达，应先在 Gateway 设计解决，不能通过修改共享设备模型绕过。

## 6.4 设备模型引用：示例

```yaml
schema_version: 1
template_id: wtg_gw2mw_e2e
file_version: "1.0.0"
name: GW2MW-E2E-Modbus
device_type_id: turbine
protocol: {id: modbus_tcp, version: "1.0.0"}
point_table: {id: modbus_wtg_gw2mw_e2e, version: "1.0.0"}
task: {id: gw2mw_e2e_all, version: "1.0.0"}
```

必须验证 `protocol.id` 是否与现有 `configs/templates/protocols/modbus_tcp_1.0.0.yaml` 的模板 ID 一致，以及 `protocol: modbus` 的内部判别关系。原 `wtg_gw2mw_1.1.0.yaml` 引用 ADS 1.1.0，**不得在测试中直接用 ADS 模型强行标记为 Modbus**。

## 6.5 点表地址设计：逐点可验证

采用每风机独立 Unit 1、相同地址布局的方案。协议地址定义与工程单位必须经 Core Codec 验证。推荐的寄存器布局原则如下：

| 组 | 量 | 建议分区 | 分配 |
|---|---|---|---|
| 读数模拟量 | #1–#16 | Input Registers | 每点 2 个 word；按顺序从 100 起分配，地址逐点间隔 2 |
| 运行状态 | #17–#18 | Discrete Inputs | 2 bit |
| AGC/AVC/调频模拟控制 | #19–#22 | Holding Registers | 每点 2 word；从 200 起，连续分配 |
| 使能/启停/复位控制 | #23–#28 | Coils | 6 bit |
| 命令状态 | #29 | Input Registers | 1 word（int16） |

此布局仅为**点表生成规则**，正式 `points` 需要对 29 个 point_id 逐一展开 `address.register_type/address/count/slave_id`、`data_type`、`business_point_id`、`unit`、`scale`、`offset`、`point_groups`。不得拿这一张表冒充可加载 YAML；自动测试必须校验 **29 点完整、地址无重叠、写权限正确、类型匹配、三设备实例共同引用合法模板**。

## 6.6 Gateway 服务配置：控制与测量分区

```yaml
# 仅演示服务端当前已存在的主要字段；其余按正式 Schema 补齐
schema_version: 1
file_version: "1.0.0"
config_sources:
  instances: {devices: configs/instances/devices.yaml}
  templates: {root: configs/templates}
grpc:
  listen_host: "127.0.0.1"
  listen_port: 50171
  max_active_rpcs: 64
  request_max_bytes: 2097152
  tls: {enabled: true}
servers:
  - server_id: gateway1-GW01
    device_id: GW01
    listen_host: "127.0.0.1"
    listen_port: 15101
    desired_state: START
    control_enabled: false
    command_mappings: []
  - server_id: gateway1-GW02
    device_id: GW02
    listen_host: "127.0.0.1"
    listen_port: 15102
    desired_state: START
    control_enabled: false
    command_mappings: []
  - server_id: gateway1-GW03
    device_id: GW03
    listen_host: "127.0.0.1"
    listen_port: 15103
    desired_state: START
    control_enabled: false
    command_mappings: []
control:
  commander_endpoint: "127.0.0.1:50152"
  forward_queue_capacity: 1024
  max_forward_inflight: 16
  submit_deadline_ms: 5000
metrics: {window_seconds: 60, collect_interval_ms: 1000}
```

Gateway 2 使用相同 `config_sources`，将每台 Server 的 `server_id`、`listen_port` 改为 `gateway2-GWxx`、15201–15203，`control_enabled=true`，并将 #19–#28 的可写点映射到 Commander 的正式 `device_id` / `commander_point_id`。其上游 `data_generator/none/plc` 字段不在此**现行字段示例**内，须待对应设计升级才写入正式 YAML；不能把未识别字段加进去后依赖解析器忽略它。

Gateway 1 与 Gateway 2 应使用不同 gRPC 监听端口、不同 `server_id`、相同逻辑设备 ID 的显式关联。必须检查 Commander 对三个设备路由的是 Gateway 1，避免指令循环发回 Gateway 2。

## 6.7 Data Generator 场景：三台有状态模型

场景字段应基于现行 `configs/instances/data-generator.yaml` 的 `scenarios/source/devices/points/gateways/feedback_mapping` 结构扩展。下面仅列出**待设计的新增逻辑字段**，不得直接投喂现行 parser：

```yaml
# PROPOSED-SCHEMA: Data Generator 设计升级后才能执行
scenario_id: gw2mw-3t-physical
source:
  kind: simulation
  interval_seconds: 0.1
  seed: 20261111
  physical_model:
    id: gw2mw_dynamic
    version: "1.0.0"
    params_ref: model/gw2mw_physics.yaml
    exogenous_ref: model/wind_profiles.yaml
devices:
  - device_id: GW01
    model_state: independent
  - device_id: GW02
    model_state: independent
  - device_id: GW03
    model_state: independent
feedback:
  read_from: gateway1
  read_method: ReadVariables
  read_before_step: true
  command_points:
    - agc_power_setpoint
    - avc_voltage_setpoint
    - avc_reactive_setpoint
    - ffr_power_command
    - ffr_enable
    - agc_enable
    - avc_enable
    - start_command
    - stop_command
    - reset_fault_command
```

真实 `feedback_mapping` 要逐设备、逐输出测点映射至 Gateway 1 变量；测量点最多 19 项/设备（29 点中 #19–#28 是控制点，不属于 Generator 的 UpdateVariables 写入所有权）。不得在更新目标集中加入 Commander 控制点。新模型的控制读取必须经过授权只读 RPC 与快照一致性校验。

## 6.8 Sink 三输出实例与 Delivery

需要依据已实现的 `src/sink_service/application/config.py` 校验模板 `kind` 为 `modbus_tcp`、`redis`、`kafka`，并保证：

| Sink | `kind` | Delivery | 模式 | 配置要点 |
|---|---|---|---|---|
| `modbus-latest` | `modbus_tcp` | `rt-modbus` | Realtime | 只读 TCP Server 15301、完整 3 设备测量映射、寄存器与质量区不冲突 |
| `redis-latest` | `redis` | `rt-redis` | Realtime | 127.0.0.1:6379、独立 namespace、键名带设备 ID、测试 TTL |
| `kafka-history` | `kafka` | `rel-kafka` | Reliable | KRaft Broker、隔离 Topic、`acks=all`、幂等 Producer、顺序/重投/去重身份 |

三条 Delivery 分别引用其准确版本的 Sink 模板和 Delivery 模板；配置文件要包括 source_filter、触发、容量、超时、回退与 DLQ，不得仅列 `sink_id`。通过 Sink 正式 Config Validate / Apply 后才允许启动完整 E2E。三目标都要包含同一批测试设备的数据，且可靠交付以独立 Durable ACK 为准。

**未获批准前不新增第 4 种目标或更改现行 Sink Adapter。** 只读 Modbus 与 Gateway 2 的衔接属于 Gateway2 的数据订阅/协议客户端职责待决项，不应偷偷改写 Sink Modbus 为 Modbus Client。

## 6.9 其余服务与安全环境

- Collector 配置必须精确指向测试 `site.yaml/devices.yaml/templates`，按 `interval_seconds=0.1` 设计 10 Hz 采集任务；检查任务配置支持该周期，保证两通道 SampleBatch 不超过各服务可接受批次容量。
- Commander `config_sources` 指向三设备共享目录，服务端 mTLS 与调用方授权必须开启；Gateway 2、CLI 使用不同测试身份，真实认证成功后才能控制；Commander 协议点表写目标是 GW01–03 的 Gateway 1 Modbus Server。
- NATS/JetStream 使用独立 `WH_HISTORY`、`WH_DLQ` 和测试 Subject，保留真实 PubAck、Durable、ACK 的 Broker 证据；Consumer 生命周期与测试用例绑定。
- Redis 使用独立数据库或强制 Key namespace；Kafka 独立 Topic；数据不能混入真实业务服务。
- 主进程运行目录应生成最终配置快照、SHA256、证书身份、网络端口和进程 PID 列表；结束仅清理有明确测试所有权的资源。
- CLI 的 gRPC 服务端点映射和 Modbus 网络命令必须与两 Gateway、三设备对应；`gateway read` 应分别在 G1/G2 查询变量，CLI 不能把 G1/G2 状态混为同一实例。

## 6.10 配置验证门禁

在启动测试之前，配置检查器必须完成：

1. 文件存在、JSON/YAML 类型、Schema 版本和强制字段正确，模板精确引用无悬空、无循环。
2. 三个实例完全独立，型号全部 GW2MW E2E Modbus，点表恰为 29 点/台；合计 87 点，设备端口不冲突。
3. 所有 Modbus FC 读/写权限、寄存器长度/字序、数据类型、单位及业务点语义均通过 Core/Configuration 校验。
4. Gateway1 三 Server 的测量写租约归 Generator，控制 FC 写归 Commander；Gateway2 三 Server 的控制映射目标均为 GW1 对应设备。
5. Sink 三目标/三 Delivery 全部在线、模式与通道匹配；三台数据均被覆盖。
6. Generator 模型参数满足物理边界、输入可重放、三状态独立；初始条件的物理模型求解稳定且有界。
7. CLI 能发现和读取两个 Gateway 的每台设备数据；正式 Modbus 控制入口通过权限测试。
8. 文件夹内部完整闭包可复制到临时目录运行；不依赖生产 `configs/` 的隐式默认路径。


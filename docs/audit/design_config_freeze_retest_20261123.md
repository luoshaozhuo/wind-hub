# wind-hub 设计与配置冻结专项独立复测报告（2026-11-23）

## 0. 最终裁决

| 裁决项 | 结果 | 理由 |
|---|---|---|
| **DESIGN_CONFIG_REVIEW** | **FAIL** | 设计正文仍有 3 项 P0、16 项 P1 冲突；prod-a 完整设备闭包不能被现行 Loader 加载，另有 Storage、Kafka、Collector 身份及 sim-a 管理面缺口。 |
| **PRODUCTION_ACTIVATION** | **BLOCKED** | OEM 语义未批准、prod-a 未选择 Storage、Kafka Topic 不一致、历史解释闭包未闭合、RAW 无真实来源、BUSINESS 无获批 Manifest。 |
| **SOFTWARE_TESTS** | **NOT_RUN** | 按任务约束未运行 Ruff、mypy、pytest、业务服务、系统测试、E2E、性能或负载。本文执行的 YAML/Loader 检查仅为纯文件静态验证。 |

**不建议冻结当前设计与其余配置为统一生产基线。** 总体架构 v1_27 保持原文不变，但其正文与其他设计存在冲突时，必须由有权的冻结勘误/裁决文件统一语义，不能用前置免责声明抵扣正文矛盾。

问题登记共 42 项：P0=3、P1=24、P2=10、P3=5。其中本轮小范围修复并关闭 1 项 P2（CSV 引号），1 项 P2 延期代码审查，1 项 P1 保持 `NEEDS_OEM`；其余保持开放。数量包含已确认的历史遗留项，不表示全部为本轮新引入。

## 1. Git 基线、工作区和远端

- 仓库：`luoshaozhuo/wind-hub`。
- 固定分支：`docs/design-consolidation-20261123`。
- 交接及实际审查 HEAD：`2ead92be3aba49b585cb3788db08407ac63a6dae`。
- 审查前本地 HEAD、`origin/docs/design-consolidation-20261123` 与 `git ls-remote` 三者一致，工作区干净。
- 本轮未新建分支、未 rebase、未 force push、未合并 main、未创建 PR。
- 本轮未修改 `configs/` 或 `docs/design/`；总体 v1_27 Git blob 保持 `891486c006c5f964ffa2e61e950104102004f9ab`。
- 唯一对既有文件的修改为映射 CSV 的 `ads_symbol` 引号格式修正；其余新增内容为本报告和证据目录。
- 证据：[git-baseline.log](design_config_freeze_retest_20261123_evidence/git-baseline.log)。报告提交后的最终 HEAD 由交付消息给出；本机无 GitHub 凭据，push 按既定约束由用户手动执行，不能预先宣称远端已包含报告提交。

## 2. 范围、方法与边界

审查覆盖 `docs/design/` 12 份最新版文档、跨模块测试设计 v0_41 §15.1、冻结前审查报告、78 份版本化 YAML 以及 GW2MW 映射 CSV。

实际执行了严格 YAML 解析、文件身份检查、静态引用闭包、现有配置审查脚本、独立临时闭包检查、只读 Core Loader/Domain Snapshot、CSV 逐点对账和 Git 同版本历史检查。没有修改生产代码，没有开展全仓代码审查。

临时 GW2MW 审查根只复制候选明确引用的 5 份 YAML，不用 v1.5.0 重解释历史点表。Loader 检查只读文件，未启动任何进程或中间件。

## 3. 设计文档版本清单

| 模块 | 最新版文件 | 唯一性 |
|---|---|---|
| CLI | [CLI v1_53](../design/CLI_软件设计_v1_53.md) | PASS |
| Collector | [Collector v1_41](../design/Collector_软件设计_v1_41.md) | PASS |
| Commander | [Commander v1_57](../design/Commander_软件设计_v1_57.md) | PASS |
| Configuration | [Configuration Service v1_20](../design/Configuration_Service_软件设计_v1_20.md) | PASS |
| Contracts | [Contracts v1_42](../design/Contracts_软件设计_v1_42.md) | PASS |
| Core | [Core v1_43](../design/Core_软件设计_v1_43.md) | PASS |
| Data Generator | [Data Generator v1_60](../design/Data_Generator_软件设计_v1_60.md) | PASS |
| Sink | [Sink Service v1_73](../design/Sink_Service_软件设计_v1_73.md) | PASS |
| Storage | [Storage Service v1_7](../design/Storage_Service_软件设计_v1_7.md) | PASS |
| Industrial Gateway | [Industrial Gateway v1_50](../design/industrial_gateway_软件设计_v1_50.md) | PASS |
| 总体架构 | [总体 v1_27](../design/wind-hub_总体逻辑架构设计_v1_27.md) | PASS，本轮未修改 |
| 跨模块测试 | [跨模块测试 v0_41](../design/wind-hub_跨模块测试设计_v0_41.md) | PASS，执行 §15.1 |

文件名版本与文内版本一致，每个模块恰好一份最新版文件；“文件唯一”不代表正文一致。

## 4. 设计一致性结果

| 核对项 | 结果 | 主要证据 |
|---|---|---|
| 模块职责、接口边界、数据流 | FAIL | D-01、D-02、D-05、D-11～D-15 |
| Service → Instance → Template | FAIL | D-08、D-22；旧 pathname/Service→Template 仍出现 |
| Deployment 精确版本选择 | FAIL | D-03、D-06、D-07；配置侧另有 C-05 |
| 全量发布、不可变与生效 | FAIL | D-05、D-11、D-12、D-16、D-23 |
| PointTable → BusinessPoint 一对一 | FAIL（设计）；PASS（隔离候选结构） | D-18；GW2MW 候选 111 点单射通过 |
| RAW / PROTOCOL / BUSINESS | FAIL | D-01、D-02、D-04、D-20；历史 F-03 未关闭 |
| Sink → Kafka → Storage → TDengine | FAIL | D-17～D-19；配置侧 C-02、C-03 |
| 历史 F-08 本地双写/旧路径 | OPEN | Commander、Collector、Sink、CLI 正文仍保留业务本地 Push/Apply |

### 4.1 设计问题登记

以下各项均为 **OPEN**。行号链接给出冲突双方或代表性正文；建议整改文件即被引用设计文档及 Contracts/受控勘误，不在本轮直接改写。

| ID | 级别 | 问题与影响 | 证据 |
|---|---|---|---|
| D-01 | P0 | 总体同时把 RAW 写成“帧中解出的原始标量”“解码前载荷”和“不是原始字节”，Contracts 又明确 RAW 为解码前帧/字节；Core 把解码标量标为 RAW。等级和落库语义无法唯一实施。 | [总体 504-510](../design/wind-hub_总体逻辑架构设计_v1_27.md#L504-L510)、[Contracts 1263-1269](../design/Contracts_软件设计_v1_42.md#L1263-L1269)、[Core 1629](../design/Core_软件设计_v1_43.md#L1629) |
| D-02 | P0 | Collector 一处要求工程值和发布前工程换算，另一处及 Core 禁止换算；存在二次 scale/offset 风险。 | [Collector 1054-1072](../design/Collector_软件设计_v1_41.md#L1054-L1072)、[Collector 1102](../design/Collector_软件设计_v1_41.md#L1102)、[Collector 3108-3114](../design/Collector_软件设计_v1_41.md#L3108-L3114)、[Core 1635](../design/Core_软件设计_v1_43.md#L1635) |
| D-03 | P0 | `GetServiceConfigBundle` 被多个模块强制依赖，但 Contracts 15 RPC 的逐字段表缺少该接口，请求 presence、响应字段和字段号未完整冻结。 | [Contracts 915](../design/Contracts_软件设计_v1_42.md#L915)、[Contracts 945](../design/Contracts_软件设计_v1_42.md#L945)、[Contracts 1102-1120](../design/Contracts_软件设计_v1_42.md#L1102-L1120)、[Storage 2123](../design/Storage_Service_软件设计_v1_7.md#L2123) |
| D-04 | P1 | 总体仍写 `RAW/ENGINEERING/BUSINESS` 且默认 BUSINESS，与三级显式 `data_level`、禁止旧 ENGINEERING 的规则冲突。 | [总体 199](../design/wind-hub_总体逻辑架构设计_v1_27.md#L199)、[总体 508](../design/wind-hub_总体逻辑架构设计_v1_27.md#L508)、[Contracts 1263-1269](../design/Contracts_软件设计_v1_42.md#L1263-L1269) |
| D-05 | P1 | 集中 Deployment 与模块本地 Push/Apply 双写并存，Repository 与本地文件均可成为业务配置权威。 | [Commander 2369-2370](../design/Commander_软件设计_v1_57.md#L2369-L2370)、[Collector 3332-3374](../design/Collector_软件设计_v1_41.md#L3332-L3374)、[Sink 3363-3396](../design/Sink_Service_软件设计_v1_73.md#L3363-L3396)、[CLI 1901-1902](../design/CLI_软件设计_v1_53.md#L1901-L1902) |
| D-06 | P1 | 一套流程按 Deployment/Service 查询精确闭包，另一套仍按全局 revision + roots 通用 Bundle 查询，不能保证选版一致。 | [Contracts 915](../design/Contracts_软件设计_v1_42.md#L915)、[Collector 3943-3946](../design/Collector_软件设计_v1_41.md#L3943-L3946)、[Sink 3981-3996](../design/Sink_Service_软件设计_v1_73.md#L3981-L3996)、[总体 436-452](../design/wind-hub_总体逻辑架构设计_v1_27.md#L436-L452) |
| D-07 | P1 | Storage/Data Generator 用 `revision_id=0` 表示当前版本，Contracts 将 0 限定为空库 CAS 基线并要求读接口区分 presence。 | [Storage 2123](../design/Storage_Service_软件设计_v1_7.md#L2123)、[Data Generator 3286](../design/Data_Generator_软件设计_v1_60.md#L3286)、[Contracts 931](../design/Contracts_软件设计_v1_42.md#L931) |
| D-08 | P1 | Service→Instance→Template 与 Service pathname/模板根模型并存，可能绕过 Deployment 精确选版。 | [Commander 2546-2568](../design/Commander_软件设计_v1_57.md#L2546-L2568)、[Gateway 1754](../design/industrial_gateway_软件设计_v1_50.md#L1754)、[Gateway 1788](../design/industrial_gateway_软件设计_v1_50.md#L1788)、[Collector 3910](../design/Collector_软件设计_v1_41.md#L3910) |
| D-09 | P1 | SampleBatch 当前版本同时写成 v2 和 v3，严格消费者可能拒绝按旧正文构造的数据。 | [Collector 1170](../design/Collector_软件设计_v1_41.md#L1170)、[Collector 3112-3120](../design/Collector_软件设计_v1_41.md#L3112-L3120)、[Contracts 1197-1209](../design/Contracts_软件设计_v1_42.md#L1197-L1209)、[Contracts 1259](../design/Contracts_软件设计_v1_42.md#L1259) |
| D-10 | P1 | Data Generator 仍要求数据包写 `config_revision_id`，而 v3 明确不包含并拒绝未知字段。 | [Data Generator 3087](../design/Data_Generator_软件设计_v1_60.md#L3087)、[Collector 3114](../design/Collector_软件设计_v1_41.md#L3114)、[Contracts 1183-1205](../design/Contracts_软件设计_v1_42.md#L1183-L1205) |
| D-11 | P1 | CLI 是否负责发布后的逐模块生效协调相互矛盾，发布成功与故障隔离判据不唯一。 | [Contracts 781](../design/Contracts_软件设计_v1_42.md#L781)、[Contracts 1191](../design/Contracts_软件设计_v1_42.md#L1191)、[Configuration 1561](../design/Configuration_Service_软件设计_v1_20.md#L1561)、[CLI 1811-1813](../design/CLI_软件设计_v1_53.md#L1811-L1813) |
| D-12 | P1 | CLI 规范命令树遗漏正式 `config publish` 和 Commander 关键控制命令，与后文唯一发布入口冲突。 | [CLI 1875-1889](../design/CLI_软件设计_v1_53.md#L1875-L1889)、[CLI 2065](../design/CLI_软件设计_v1_53.md#L2065)、[CLI 1912-1916](../design/CLI_软件设计_v1_53.md#L1912-L1916) |
| D-13 | P1 | Data Generator 默认变量为 `<device_id>.<point_id>`，Gateway 变量目录要求直接等于 PointTable `point_id`。 | [Data Generator 3071](../design/Data_Generator_软件设计_v1_60.md#L3071)、[Gateway 1901](../design/industrial_gateway_软件设计_v1_50.md#L1901) |
| D-14 | P1 | Gateway/Data Generator RPC 全集和数量自相矛盾，可能漏验仿真执行接口。 | [Gateway 779](../design/industrial_gateway_软件设计_v1_50.md#L779)、[Gateway 1681-1692](../design/industrial_gateway_软件设计_v1_50.md#L1681-L1692)、[Data Generator 1911-2002](../design/Data_Generator_软件设计_v1_60.md#L1911-L2002)、[Contracts 875-888](../design/Contracts_软件设计_v1_42.md#L875-L888) |
| D-15 | P1 | 仿真控制一侧强制 mTLS，另一侧及 Contracts 禁止内部 TLS/mTLS，同一控制链无法同时满足。 | [Gateway 1873](../design/industrial_gateway_软件设计_v1_50.md#L1873)、[Data Generator 3150-3152](../design/Data_Generator_软件设计_v1_60.md#L3150-L3152)、[Contracts 1275](../design/Contracts_软件设计_v1_42.md#L1275) |
| D-16 | P1 | Sink 一方面冻结同版本字节不可变，另一方面允许人工修改 Delivery `desired_state` 并由 Main CAS 写正文，运行意图与不可变版本未分离。 | [Sink 2828](../design/Sink_Service_软件设计_v1_73.md#L2828)、[Sink 3295](../design/Sink_Service_软件设计_v1_73.md#L3295)、[Sink 3359-3396](../design/Sink_Service_软件设计_v1_73.md#L3359-L3396) |
| D-17 | P1 | Sink 以 `delivery_id + sample_id` 去重，Kafka JSON v2 不含 `delivery_id`，Storage 的幂等定义也互相不同，跨重启去重身份未闭合。 | [Sink 2733](../design/Sink_Service_软件设计_v1_73.md#L2733)、[Contracts 1281-1297](../design/Contracts_软件设计_v1_42.md#L1281-L1297)、[Storage 1057](../design/Storage_Service_软件设计_v1_7.md#L1057)、[Storage 2109](../design/Storage_Service_软件设计_v1_7.md#L2109) |
| D-18 | P1 | Core/Configuration 要求同一展开 PointTable 内 BusinessPoint 唯一，Storage logical_key 段却允许一来源点映射多个业务点。 | [Core 1623](../design/Core_软件设计_v1_43.md#L1623)、[Configuration 1709-1727](../design/Configuration_Service_软件设计_v1_20.md#L1709-L1727)、[Storage 2109](../design/Storage_Service_软件设计_v1_7.md#L2109) |
| D-19 | P1 | Storage 首版同时允许多 Consumer 实例和规定只有一个进程/实例，Partition、SQLite 和 DDL 所有权边界不唯一。 | [Storage 1118](../design/Storage_Service_软件设计_v1_7.md#L1118)、[Storage 1799](../design/Storage_Service_软件设计_v1_7.md#L1799)、[Storage 2132](../design/Storage_Service_软件设计_v1_7.md#L2132) |
| D-20 | P2 | Contracts/Configuration 同时描述 Proto 已存在、待新增、废止及“无 Proto”，当前状态不可追溯。 | [Contracts 756-758](../design/Contracts_软件设计_v1_42.md#L756-L758)、[Contracts 1045](../design/Contracts_软件设计_v1_42.md#L1045)、[Contracts 1129](../design/Contracts_软件设计_v1_42.md#L1129)、[Contracts 1354](../design/Contracts_软件设计_v1_42.md#L1354)、[Configuration 1351-1359](../design/Configuration_Service_软件设计_v1_20.md#L1351-L1359) |
| D-21 | P2 | 已禁止的 SemanticPoint 第三层仍出现在规范示例和 Contracts 对照中。 | [Core 1604](../design/Core_软件设计_v1_43.md#L1604)、[Configuration 1567-1581](../design/Configuration_Service_软件设计_v1_20.md#L1567-L1581)、[Contracts 1158](../design/Contracts_软件设计_v1_42.md#L1158) |
| D-22 | P2 | Collector Task 启停持久化、`device_id`/group 选择及 Service 必需 Instance 的示例仍有两套规则。 | [Collector 3100-3102](../design/Collector_软件设计_v1_41.md#L3100-L3102)、[Collector 3163](../design/Collector_软件设计_v1_41.md#L3163)、[Collector 3114](../design/Collector_软件设计_v1_41.md#L3114)、[Collector 3205](../design/Collector_软件设计_v1_41.md#L3205)、[Collector 3273-3303](../design/Collector_软件设计_v1_41.md#L3273-L3303) |
| D-23 | P2 | `target_revision/active_revision` 被要求报告，但未进入 Contracts/Commander 状态响应映射。 | [Configuration 777-792](../design/Configuration_Service_软件设计_v1_20.md#L777-L792)、[Contracts 796](../design/Contracts_软件设计_v1_42.md#L796)、[Commander 2366](../design/Commander_软件设计_v1_57.md#L2366) |
| D-24 | P2 | 跨模块测试设计仍混用固定 51 RPC/73 CLI 与动态全量分母，可能漏验 Storage 和新增接口。 | [跨模块测试 489](../design/wind-hub_跨模块测试设计_v0_41.md#L489)、[跨模块测试 520](../design/wind-hub_跨模块测试设计_v0_41.md#L520)、[跨模块测试 724](../design/wind-hub_跨模块测试设计_v0_41.md#L724) |
| D-25 | P2 | 权威文档引用仍指向多个旧 Contracts/Configuration/总体版本，追溯基线不唯一。 | [CLI 642](../design/CLI_软件设计_v1_53.md#L642)、[CLI 1743](../design/CLI_软件设计_v1_53.md#L1743)、[Storage 1164](../design/Storage_Service_软件设计_v1_7.md#L1164)、[Core 1498](../design/Core_软件设计_v1_43.md#L1498) |
| D-26 | P3 | CLI 将主动 ReadPoints 图称为缓存周期轮询，与现场主动读取定义不符。 | [CLI 1623](../design/CLI_软件设计_v1_53.md#L1623)、[CLI 1907](../design/CLI_软件设计_v1_53.md#L1907) |
| D-27 | P3 | Commander 仍称“非现有 gRPC 批量接口”，与后文已冻结 SubmitCommands/GetRequestResult 不一致。 | [Commander 2603](../design/Commander_软件设计_v1_57.md#L2603)、[Commander 2500](../design/Commander_软件设计_v1_57.md#L2500) |
| D-28 | P3 | Data Generator/Gateway 的 Contracts 章节引用错误或不存在。 | [Data Generator 1913](../design/Data_Generator_软件设计_v1_60.md#L1913)、[Gateway 1881](../design/industrial_gateway_软件设计_v1_50.md#L1881) |

## 5. Deployment / Instance / Template 引用矩阵

### 5.1 DeviceModel 闭包代号

- **M-SIM**：`wtg_sim_physical`、`pv_sim_physical`、`bess_sim_physical`、`grid_sim_physical`、`plant_controller_sim_physical` 均为 Model@1.1.0 → `modbus_tcp@1.1.0` → 对应 PointTable@1.1.0。
- **M-P13**：`wtg_gw2mw@1.4.0` → `ads@1.2.0` + `ads_wtg_gw2mw@1.1.0`；`wtg_tz2mw@1.3.0` → `modbus_tcp@1.1.0` + `modbus_wtg_tz2mw@1.1.0`。
- **M-P14**：`wtg_gw2mw@1.5.0` → `ads@1.2.0` + `ads_wtg_gw2mw@1.2.0` → BusinessPoint@1.5.0 + Units@1.2.0；TZ2MW 仍为 M-P13 的未固定历史点表。

### 5.2 e2e-final@1.0.0

| Service | Deployment 解析后的 Instance | 关键 Template/目标 |
|---|---|---|
| collector-e2e-realtime@1.0.0 | site=e2e-final@1.0.0；devices=e2e-final@1.0.0；tasks=e2e-final@1.0.0 | M-SIM |
| collector-e2e-reliable@1.0.0 | 同上 | M-SIM |
| commander-e2e@1.0.0 | devices=e2e-final@1.0.0 | M-SIM |
| gateway-e2e-source@1.0.0 | devices=e2e-final@1.0.0 | M-SIM |
| gateway-e2e-target@1.0.0 | devices=e2e-final@1.0.0 | M-SIM；`gw-e2e-sink-target` |
| data-generator-e2e@1.0.0 | site/devices=e2e-final@1.0.0；scenarios=e2e-final@1.0.0 | M-SIM |
| sink-e2e@1.0.0 | sinks=e2e-final@1.0.0；deliveries=e2e-final@1.0.1 | 下表 |
| storage-e2e@1.0.0 | site/devices=e2e-final@1.0.0 | M-SIM；Kafka `windhub.e2e.history` → TDengine |
| cli-e2e@1.0.0 | 无 Instance | 直引 E2E Service 与 `deploy/e2e` bootstrap |

| Sink Instance | Template | Delivery Instance | Delivery Template | 状态 |
|---|---|---|---|---|
| e2e_redis | e2e_redis_realtime@1.0.0 | e2e_redis_power | realtime_power@1.1.0 | STOP |
| e2e_modbus | modbus_registers@1.0.0 | e2e_modbus_power | realtime_power@1.1.0 | STOP |
| e2e_gateway | gateway_realtime@1.1.0 | e2e_gateway_power | realtime_power@1.1.0 | STOP |
| e2e_kafka | e2e_kafka_history@1.0.0 | e2e_kafka_protocol | reliable_history@1.1.0 | STOP |

静态 Kafka Topic 与 Storage 一致，但完整 E2E 设备闭包仍因 BusinessPoint 多版本且未固定而被 Loader 拒绝；没有实际 Kafka/TDengine 运行证据。

### 5.3 prod-a@1.3.0 与 prod-a@1.4.0

| Service | prod-a@1.3.0 Instance | prod-a@1.4.0 Instance | 关键 Template/目标 |
|---|---|---|---|
| collector-realtime@1.1.0 | site=example_site@1.1.0；devices=farm-a@1.4.0；tasks=farm-a@1.0.0 | devices 改为 farm-a@1.5.0，其余相同 | M-P13 / M-P14 |
| collector-reliable@1.0.0 | 同上 | 同上 | M-P13 / M-P14 |
| commander@1.4.0 | devices=farm-a@1.4.0 | devices=farm-a@1.5.0 | M-P13 / M-P14 |
| gateway@1.2.0 | devices=farm-a@1.4.0 | devices=farm-a@1.5.0 | `gateway-tz01` |
| data-generator@1.1.0 | site=example_site@1.1.0；devices=farm-a@1.4.0；scenarios=prod-a@1.0.0 | devices 改为 farm-a@1.5.0 | M-P13 / M-P14 |
| sink-west@1.2.0 | sinks=prod-a@1.2.0；deliveries=prod-a@1.3.0 | deliveries 改为 prod-a@1.4.0 | 下表 |
| cli@1.2.0 | 无 Instance | 无 Instance | 直引生产 DG 和 Storage bootstrap |

两版 Deployment 的 Service 选择完全相同，实例差异仅为 `device/farm-a: 1.4.0→1.5.0` 和 `delivery/prod-a: 1.3.0→1.4.0`。`deliveries_prod-a@1.4.0` 文件存在，除 `file_version/revision` 外交互语义与 1.3.0 完全相同；这同时继承了 Collector 来源身份问题。

| Sink Instance | Template | Delivery Instance | Delivery Template | 状态 |
|---|---|---|---|---|
| ems_redis | redis_realtime@1.0.0 | ems_power | realtime_power@1.1.0 | START |
| history_kafka | kafka_history@1.0.0 | power_history | reliable_history@1.1.0 | START |
| modbus_view | modbus_registers@1.0.0 | ems_modbus_view | realtime_power@1.1.0 | START |
| gateway_view | gateway_realtime@1.1.0 | gateway_meter_view | realtime_power@1.1.0 | STOP |

两版 prod-a 都未选择 `storage-main@1.1.0`。现有 Sink Template 写 `windhub.samples`，Storage 配置消费 `windhub-history`，目标身份不一致。

### 5.4 sim-a@1.1.0

| Service | Instance 精确版本 | Template/目标 |
|---|---|---|
| collector-sim@1.0.0 | site=example_site@1.1.0；devices=sim@1.2.0；tasks=sim@1.0.0 | M-SIM |
| commander-sim@1.0.0 | devices=sim@1.2.0 | M-SIM |
| gateway-sim@1.1.3 | devices=sim@1.2.0 | M-SIM |
| data-generator-sim@1.0.3 | site/devices=sim@1.2.0；scenarios=sim@1.0.3 | M-SIM |
| cli@1.2.0 | 无 Instance | **生产 CLI，直引生产 DG/prod-a Storage，不是 sim 管理面** |

## 6. 配置完整性与 Loader 实测

| 检查项 | 结果 | 统计/限制 |
|---|---|---|
| YAML 严格解析与重复键 | PASS | 78/78；重复键 0；74 个 schema v1、4 个 v2 |
| 文件名、配置 ID、file_version | PASS | 文件名/version 不一致 0；同 ID+version 重复 0 |
| Deployment→Service/Instance | PASS（文件存在性） | 28 Service 选择、22 Instance 选择、50 条 Service→Instance；缺失 0 |
| Device Instance→Model | PASS | 31 条引用、8 个唯一 Model；缺失 0 |
| Model→Protocol/PointTable | PASS | 16 条显式引用；缺失 0、协议族不一致 0 |
| PointTable 继承 | PASS（现有引用） | 未发现缺失父版本或循环；仅 1/10 点表完整固定 BusinessPoint/Units |
| Sink/Delivery→Template | PASS | 8 Sink、12 Delivery 模板引用均存在 |
| Delivery→Sink/Device/Point | PASS | 12 Sink 引用、12 个来源 device-point 对均存在；Collector 身份另见 C-04 |
| BusinessPoint/Units 精确闭包 | FAIL | 9 个历史 PointTable 共 240 点未固定；BP@1.4.0 也未固定 Units |
| 非 GW2MW 类型/单位诊断 | 仅诊断 PASS | 其余 240 点暂按 BP@1.4.0 对照时，缺失、类型和量纲不兼容均为 0；不能替代精确历史闭包 |
| prod-a@1.4.0 全设备 Loader | **FAIL** | `mixed pinned and unpinned PointTable catalog interpretations` |
| farm-a@1.4.0、e2e-final、sim Loader | **FAIL** | `business_points_standard: exactly one pinned catalog is required` |
| 仅 GW01～GW05 的 Loader + Domain Snapshot | PASS | BP@1.5.0 共 400；1 个 PointTable、111 点；source manifest 6 项 |
| 不指定 prod-a Deployment 版本 | 按预期拒绝 | `exactly one Deployment must select this Service version`，未静默选最新 |
| 显式 prod-a@1.4.0 | 文件选择 PASS | 精确解析 collector Service 及 site/devices/tasks；不代表后续全设备 Loader 通过 |
| E2E/生产逻辑引用隔离 | PASS | 跨生产 Service/Instance 引用 0；Topic/Stream/证书/目录隔离，基础设施物理隔离仍见 C-11 |

原始证据：[yaml-strict-parse.log](design_config_freeze_retest_20261123_evidence/yaml-strict-parse.log)、[loader-checks.log](design_config_freeze_retest_20261123_evidence/loader-checks.log)、[kafka-storage-targets.log](design_config_freeze_retest_20261123_evidence/kafka-storage-targets.log)。

## 7. GW2MW 专项逐点结果

### 7.1 汇总

| 检查项 | 结果 |
|---|---|
| BusinessPoint v1.4.0 / v1.5.0 数量 | 293 / 400 |
| 原 293 个定义 | **293/293 完整定义相等**，含 ID、数据类型、单位、description；删除 0、变化 0 |
| 新增候选 | **107/107 完整**，data_type、有效 unit、非空 description 均存在 |
| PointTable v1.1.0 / v1.2.0 | 111 / 111 Point ID，缺失和新增均为 0 |
| 原协议字段 | 111/111 除 `business_point_id` 与候选 description 外完全未变，含完整 ADS address/Symbol、源类型、单位、scale、offset、point_groups |
| 新 BusinessPoint 身份 | 111 个互异，无重复；目标均存在于 v1.5.0 |
| 映射 CSV | 111 行、111 个唯一 Point、111 个唯一候选 ID；旧/新 ID、源/标准类型和单位均对账 |
| 类型与单位 | 98 REAL→float32、13 BOOL→bool；源/标准单位量纲全部兼容；BOOL 恒等缩放通过 |
| 隔离现有严格审查脚本 | 5 YAML、1 PointTable、111 点、0 flagged，退出 0 |
| Core 只读 Domain Snapshot | GW01～GW05 选定闭包构建成功，111 点 |
| OEM 状态 | **111/111 仍为 `PENDING_OEM_SEMANTICS`**；结构性 PASS 不是物理语义批准 |

### 7.2 逐点和目录证据

- [111 点逐点结果 CSV](design_config_freeze_retest_20261123_evidence/gw2mw_businesspoint_retest_results_20261123.csv)：每点列出旧/新业务 ID、ADS Symbol、源/标准类型与单位、scale/offset、全部检查项、结构状态和 OEM 状态。
- [400 行业务目录差异 CSV](design_config_freeze_retest_20261123_evidence/businesspoint_catalog_diff_20261123.csv)：293 个 `PASS_UNCHANGED`、107 个 `PASS_COMPLETE_CANDIDATE`。
- [现有脚本严格审查结果](design_config_freeze_retest_20261123_evidence/gw2mw-strict-audit.csv)及[日志](design_config_freeze_retest_20261123_evidence/gw2mw-strict-audit.log)。
- [机器可读汇总](design_config_freeze_retest_20261123_evidence/gw2mw_retest_summary.json)。

候选链逐级为 Deployment prod-a@1.4.0 → Device farm-a@1.5.0 → GW Model@1.5.0 → PointTable@1.2.0 → BusinessPoint@1.5.0 + Units@1.2.0。DeviceModel 除 PointTable/version 外与 1.4.0 相同；10 个设备的非 Model 字段未变；5 个 GW 设备选择 Model@1.5.0，5 个 TZ 设备保持 Model@1.3.0。**只有 GW 子闭包通过，不能外推为 prod-a 全设备通过。**

### 7.3 本轮 CSV 小修复

原 CSV 将符号编码为 `""".A02_DM_PITCHANGLE1"""`，CSV 解析后的值包含额外字面双引号，与 YAML `.A02_DM_PITCHANGLE1` 不一致，111 行均受影响。已仅将字段编码改为正常 CSV 引号；字段级比较确认 111 行只有 `ads_symbol` 改变，其他字段 0 变化，111 个 `PENDING_OEM_SEMANTICS` 全部保留。

修复前证据：[日志](design_config_freeze_retest_20261123_evidence/gw2mw-retest-before-csv-fix.log)和[逐点结果](design_config_freeze_retest_20261123_evidence/gw2mw-results-before-csv-fix.csv)。未修改任何历史 YAML 或业务语义。

## 8. 配置问题与冻结边界登记

| ID | 级别 | 状态 | 问题、证据与整改要求 |
|---|---|---|---|
| C-01 | P1 | OPEN / HISTORICAL_CLOSURE_UNRESOLVED | 9 个历史 PointTable 共 240 点未固定 BP/Units；BP@1.4.0 也未固定 Units。prod-a@1.4.0 的 GW 固定、TZ 未固定而被 Loader 拒绝；历史 prod-a、e2e、sim 因两版 BP 共存无法选唯一目录。不得用 v1.5.0 重解释旧表；应依据原始发布证据创建显式固定的新后继版本，或建立经验证的受控发布根。证据：[TZ 点表 10-14](../../configs/templates/point_tables/point_table_modbus_wtg_tz2mw_v1.1.0.yaml#L10-L14)、[farm-a 94-148](../../configs/instances/device_farm-a_v1.5.0.yaml#L94-L148)、[Loader 规则](../../src/core/configuration/shared_documents.py#L270-L312)。 |
| C-02 | P1 | OPEN | prod-a@1.3.0/1.4.0 均未选择 `storage-main`，CLI 带外启动不能替代 Deployment 闭包。证据：[Deployment 10-31](../../configs/deployments/deployment_prod-a_v1.4.0.yaml#L10-L31)、[Storage 42-48](../../configs/services/service_storage-main_v1.1.0.yaml#L42-L48)、[CLI 68-75](../../configs/services/service_cli_v1.2.0.yaml#L68-L75)。 |
| C-03 | P1 | OPEN | prod-a Sink Kafka Topic=`windhub.samples`，Storage Topic=`windhub-history`；Broker 地址也需部署核验。e2e Topic 一致。即使补选 Storage，当前 Topic 仍不能落地。证据：[Sink Template 14-20](../../configs/templates/sinks/sink_kafka_history_v1.0.0.yaml#L14-L20)、[Storage 62-70](../../configs/services/service_storage-main_v1.1.0.yaml#L62-L70)。 |
| C-04 | P1 | OPEN | prod-a Delivery 使用 `collector_realtime/collector_reliable`，所选 Service ID 为 `collector-realtime/collector-reliable`，配置内未见运行时 Collector ID 显式绑定。若由启动参数/环境注入，必须提供证据，否则 START Delivery 可能过滤不到来源。证据：[Delivery 15-48](../../configs/instances/deliveries_prod-a_v1.4.0.yaml#L15-L48)、[Collector realtime 10](../../configs/services/service_collector-realtime_v1.1.0.yaml#L10)、[Collector reliable 10](../../configs/services/service_collector-reliable_v1.0.0.yaml#L10)。 |
| C-05 | P1 | OPEN / AMBIGUOUS_NOT_SUPPORTED | prod-a 两版 Deployment 共存；Storage bootstrap 和 Sink 只声明 deployment_id。LocalSelection 未指定版本会 fail-closed，但 bootstrap 配置本身未给精确版本。发布根同身份多 Deployment 也为 NOT_SUPPORTED，不得宣称全集可直接发布。证据：[prod-a 1.4 8-9](../../configs/deployments/deployment_prod-a_v1.4.0.yaml#L8-L9)、[Storage bootstrap 12-15](../../configs/services/storage_bootstrap_v1.0.0.yaml#L12-L15)、[Sink 130-139](../../configs/services/service_sink-west_v1.2.0.yaml#L130-L139)。 |
| C-06 | P1 | OPEN | sim-a 选择生产 `cli@1.2.0`，其进程表直引生产 Data Generator 和 prod-a Storage，跨部署域。应提供 sim 专用 CLI 或精确目标闭包。证据：[sim Deployment 22-24](../../configs/deployments/deployment_sim-a_v1.1.0.yaml#L22-L24)、[CLI 58-75](../../configs/services/service_cli_v1.2.0.yaml#L58-L75)。 |
| C-07 | P2 | OPEN / RELEASE_LEDGER_REQUIRED | 当前 78 个路径中 19 个存在同版本 M 提交，11 个有 14 次 YAML 语义变化，8 个有 10 次仅注释变化。两个 GW2MW 候选模板也在交接前同版本迭代。Git 历史不能区分“未发布候选整改”与“已发布版本改写”，故不直接宣称全部违反不可变，也不能给不可变 PASS；需发布台账界定。本轮所有历史 YAML 字节未变。证据：[历史检查](design_config_freeze_retest_20261123_evidence/config-history-immutability.log)。 |
| C-08 | P2 | OPEN | prod-a Gateway Delivery 虽 STOP，但 Sink 指向管理端口 50073；Gateway 未配置独立 Sink listener，TLS=false。完成 listener、租约和授权闭包前不得启动。证据：[Sink 27-33](../../configs/instances/sinks_prod-a_v1.2.0.yaml#L27-L33)、[Gateway 33-49](../../configs/services/service_gateway_v1.2.0.yaml#L33-L49)、[Delivery 41-45](../../configs/instances/deliveries_prod-a_v1.4.0.yaml#L41-L45)。 |
| C-09 | P2 | **CLOSED** | 映射 CSV 111 行 ADS Symbol 多余字面双引号；本轮仅修复该格式并复测 111/111 对账通过，详见 §7.3。 |
| C-10 | P2 | DEFERRED_TO_CODE_REVIEW | Core Domain Snapshot 以硬编码 BP@1.5.0 决定哪些 PointTable 启用唯一性严格检查；未来新版目录是否自动适用需代码专题审查。本轮不改生产代码。证据：[shared_snapshot 102-130](../../src/core/configuration/shared_snapshot.py#L102-L130)、[shared_point_tables 238-246](../../src/core/configuration/shared_point_tables.py#L238-L246)。 |
| C-11 | P3 | OPEN | E2E 为逻辑隔离而非基础设施物理隔离；仍使用本机 NATS/Kafka/TDengine 端点。并行运行需外部互斥或独立基础设施。证据：[Sink E2E 158-166](../../configs/services/service_sink-e2e_v1.0.0.yaml#L158-L166)、[Storage E2E 63-74](../../configs/services/service_storage-e2e_v1.0.0.yaml#L63-L74)。 |
| C-12 | P3 | OPEN | 历史 `device_farm-a_v1.4.0.yaml` 文件头注释仍写 v1.3.0；实际文件名和 file_version 正确。不得为修注释直接改写已发布历史。证据：[文件头 1-3](../../configs/instances/device_farm-a_v1.4.0.yaml#L1-L3)。 |
| C-13 | P1 | **NEEDS_OEM** | 111 个 GW2MW 候选身份结构完整且互异，但全部 `PENDING_OEM_SEMANTICS`；PLC 物理含义、标度、读写权限和限值未获 OEM 核准，不能标 BUSINESS 生产 PASS。 |
| C-14 | P1 | OPEN / NOT_READY | RAW 无真实解码前载荷来源，BUSINESS 无真实获批 manifest SHA-256；基础 E2E Delivery 仅 PROTOCOL，BUSINESS Template 未被默认启用。禁止伪造哈希或以 PROTOCOL 冒充。证据：[E2E Delivery 1-7](../../configs/instances/deliveries_e2e-final_v1.0.1.yaml#L1-L7)、[Reliable Template 6-8](../../configs/templates/deliveries/delivery_reliable_history_v1.1.0.yaml#L6-L8)、[Contracts 1279](../design/Contracts_软件设计_v1_42.md#L1279)。 |

## 9. 实际命令、退出码和证据

完整命令表见 [commands.md](design_config_freeze_retest_20261123_evidence/commands.md)，关键结果如下：

| 验证 | 退出码 | 结论 |
|---|---:|---|
| 环境检查 | 0 | wind-hub 环境 PASS |
| 全集现有严格审查脚本 | 1 | 两版 BusinessPoint，拒绝未显式选择的全集；未通过取消 strict 使结果变绿 |
| 78 YAML 严格解析 | 0 | 78/78，重复键 0 |
| 隔离 GW2MW 现有严格脚本 | 0 | 111 点、0 flagged |
| GW2MW 自定义逐点对账（CSV 修复前） | 1 | 111 行 Symbol 引号不匹配 |
| GW2MW 自定义逐点对账（CSV 修复后） | 0 | 111/111 结构 PASS，293 未变、107 完整 |
| 只读 Loader/Domain Snapshot 检查 | 0 | 所有预期拒绝/通过状态符合；拒绝项作为 C-01 证据而非测试全绿 |
| Sink/Storage Topic 对账 | 0 | prod-a 不匹配，e2e 匹配 |
| Git 同版本历史检查 | 0 | 19 个 M 路径；发布前后状态需台账区分 |
| `git diff --check` | 0 | 补丁无空白错误 |

证据目录还保留复测脚本、机器可读 JSON、首轮复测脚本自身失败及修正后的日志。首轮 Loader 站点输入遗漏和 Kafka 字段路径错误均明确标为审查脚本问题，没有归因于配置，也没有删除失败证据。

## 10. 未执行项与延期事项

- `SOFTWARE_TESTS=NOT_RUN`：Ruff、mypy、pytest、业务模块测试、系统测试、跨模块 E2E、容量、性能、长稳均未执行。
- `PRODUCTION_ACTIVATION=BLOCKED`：未启动 Configuration、Collector、Gateway、DG、Sink、Storage、CLI 业务进程；未启动 NATS、Kafka、Redis、TDengine；无真实 PLC/OEM 证据。
- 生产代码问题仅 C-10 登记 `DEFERRED_TO_CODE_REVIEW`；设计中指出的实现/Proto 差异需在代码专题以 Descriptor 和真实调用核验，本轮不扩为全仓代码审查。
- 历史 Common RPC、真实 Repository 发布、广播、独立生效、Kafka ACK、TDengine SQL、故障恢复与权限链均没有运行性结论。
- 本机无 GitHub 凭据，最终 push 为 `NOT_RUN/待用户手动执行`，不把 commit 等同远端更新。

## 11. 冻结建议与整改顺序

1. **先形成有权的设计勘误/裁决**：不改总体原文，唯一裁决 RAW/PROTOCOL/BUSINESS、Collector 不换算、废止 ENGINEERING/默认 BUSINESS，并关闭 D-01～D-03。
2. **清理双权威和旧契约**：统一 Deployment/Service 精确闭包 RPC，清除业务本地 Push/Apply、pathname、revision_id=0 当前版本、SampleBatch v2 等旧正文。
3. **闭合存储语义**：统一 Kafka/Storage 去重身份、PointTable 一对一、Delivery 运行意图与不可变版本的边界、Storage 首版实例模型。
4. **修复配置闭包而不改历史字节**：根据真实原始发布证据为历史 PointTable/BP 的 Units 依赖建立显式后继版本或受控单版本发布根；prod-a 同一设备闭包不能混用固定和未固定解释。
5. **修复生产目标身份**：新 Deployment 选择 Storage，统一 Kafka Broker/Topic，显式绑定 Collector ID；sim-a 使用独立管理面；精确指定 Deployment 版本。
6. **保持 OEM/BUSINESS/RAW 门禁**：取得 OEM 逐点核准和真实 Manifest 后才能启用 BUSINESS；真实原始载荷接入前 RAW 继续 NOT_SUPPORTED。
7. **完成一次同范围独立复核后，再进入独立软件测试专题**。设计与配置结构通过、软件测试通过和生产激活是三个不同裁决，不得相互替代。

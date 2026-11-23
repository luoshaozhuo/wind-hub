# wind-hub 设计与配置冻结第三轮独立复测报告（2026-11-23）

> 报告模板说明：仓库中未找到用户指定的 `报告模板.md`（检索命令退出 0、无输出）。本报告沿用第二轮独立复测结构，覆盖本轮要求的全部交付项，未因此降低检查范围。

## 0. 最终裁决

| 裁决项 | 结果 | 理由 |
|---|---|---|
| **DESIGN_CONFIG_REVIEW** | **FAIL** | 旧问题仍有 12 项 OPEN、3 项等待总体勘误批准；新增 Data Generator mTLS 残留、设计计数/状态表述和审计版本偏差。Core Loader 全通过不能抵销这些冲突。 |
| **DESIGN_FREEZE_APPROVAL** | **DESIGN_FREEZE_APPROVAL_PENDING** | D-01/D-04/D-06 的总体 v1_27 替代勘误仍无有权批准证据。 |
| **PRODUCTION_ACTIVATION** | **BLOCKED** | OEM、RAW/BUSINESS 外部证据、真实发布状态、运行时 Schema、生产安全 Listener、基础设施和现场容量均未满足。 |
| **SOFTWARE_TESTS** | **NOT_RUN** | 未运行 Ruff、mypy、pytest、业务模块测试、系统测试、E2E 或性能测试。 |

旧 45 项固定分母结果：**CLOSED 28、OPEN 12、DESIGN_FREEZE_APPROVAL_PENDING 3、DEFERRED_TO_CODE_REVIEW 1、NEEDS_OEM 1**。新发现 6 项：N-04～N-09，其中两项 P1 延期代码审查、三项 P2 OPEN、一项 P3 OPEN。

**积极结果**：N-01 Kafka 历史 Blob 已恢复；N-02 命令名统一；N-03/C-06 sim 文件层隔离通过；三根现有严格审查、87 个哈希及完整 Core Domain Snapshot 全通过。**但这些结果不构成整体设计冻结或运行时准入。**

## 1. Git 基线和范围

- 固定分支：`docs/design-consolidation-20261123`。
- 交接及实际审查 HEAD：`dc71f60bb869874648f5051b34f33bef44985c98`。
- fetch 后本地、远端跟踪、`ls-remote` 三者一致，初始工作区干净。
- 第二轮报告提交 `b5035af01f14159b52f88cac2ead548b70974817` 用于历史比较。
- 未新建分支/PR、未合并 main、未 rebase、未 force push、未修改生产代码。
- 总体 v1_27 Blob 保持 `891486c006c5f964ffa2e61e950104102004f9ab`。
- 本轮只新增本报告和证据到 `docs/audit/`。最终提交 HEAD 由交付消息给出，push 状态单独报告。
- 证据：[git-baseline.log](design_config_freeze_retest_round3_20261123_evidence/git-baseline.log)。

检查覆盖 12 份最新版设计书、跨模块测试 v0_43 §15.4～15.5、前两轮报告、冻结审查、版本台账、117 份源 YAML 及三根 87 份 YAML。实际执行严格 YAML、Configuration Schema、Core Loader、第一方纯配置解析、引用/目标/哈希、台账和 GW2MW 对账；未真实发布或启动服务。

## 2. 最新设计书清单

| 模块 | 当前文件 |
|---|---|
| CLI | [CLI v1_55](../design/CLI_软件设计_v1_55.md) |
| Collector | [Collector v1_43](../design/Collector_软件设计_v1_43.md) |
| Commander | [Commander v1_59](../design/Commander_软件设计_v1_59.md) |
| Configuration | [Configuration Service v1_22](../design/Configuration_Service_软件设计_v1_22.md) |
| Contracts | [Contracts v1_44](../design/Contracts_软件设计_v1_44.md) |
| Core | [Core v1_45](../design/Core_软件设计_v1_45.md) |
| Data Generator | [Data Generator v1_62](../design/Data_Generator_软件设计_v1_62.md) |
| Sink | [Sink Service v1_75](../design/Sink_Service_软件设计_v1_75.md) |
| Storage | [Storage Service v1_9](../design/Storage_Service_软件设计_v1_9.md) |
| Industrial Gateway | [Industrial Gateway v1_52](../design/industrial_gateway_软件设计_v1_52.md) |
| 总体架构 | [总体 v1_27](../design/wind-hub_总体逻辑架构设计_v1_27.md)，原字节未改 |
| 跨模块测试 | [跨模块测试 v0_43](../design/wind-hub_跨模块测试设计_v0_43.md) |

## 3. D-01～D-28 逐项关闭矩阵

| ID | 原级别 | 第三轮状态 | 当前证据与理由 |
|---|---|---|---|
| D-01 | P0 | **DESIGN_FREEZE_APPROVAL_PENDING** | [Contracts 1271-1279](../design/Contracts_软件设计_v1_44.md#L1271-L1279)与 Core 已统一三级语义；[总体 500-510](../design/wind-hub_总体逻辑架构设计_v1_27.md#L500-L510)冲突仍在，[勘误 210-221](full_module_freeze_readiness_review_20261123.md#L210-L221)明确待批准。 |
| D-02 | P0 | **CLOSED** | [Collector 1059](../design/Collector_软件设计_v1_43.md#L1059)、[1104](../design/Collector_软件设计_v1_43.md#L1104)、[3097-3105](../design/Collector_软件设计_v1_43.md#L3097-L3105)均禁止 Collector 换算；Core 明确 Sink 唯一转换。 |
| D-03 | P0 | **CLOSED** | [Contracts 923](../design/Contracts_软件设计_v1_44.md#L923)、[1119](../design/Contracts_软件设计_v1_44.md#L1119)完整冻结 GetServiceConfigBundle 字段号及 revision_id=0 当前发布语义。 |
| D-04 | P1 | **DESIGN_FREEZE_APPROVAL_PENDING** | Contracts 已废止 ENGINEERING/默认 BUSINESS；[总体 199](../design/wind-hub_总体逻辑架构设计_v1_27.md#L199)旧职责须由获批勘误替代。 |
| D-05 | P1 | **OPEN** | [Collector 3305-3380](../design/Collector_软件设计_v1_43.md#L3305-L3380)仍保留本地候选、Push/Apply DISK 提交和 file_commit；[Contracts 799-813](../design/Contracts_软件设计_v1_44.md#L799-L813)仍描述 Common 候选提交/生效。 |
| D-06 | P1 | **DESIGN_FREEZE_APPROVAL_PENDING** | Contracts 禁止 roots 替代精确闭包；[总体 436-452](../design/wind-hub_总体逻辑架构设计_v1_27.md#L436-L452)仍用通用 GetConfigBundle，须批准勘误。 |
| D-07 | P1 | **CLOSED** | Contracts、Storage、Data Generator 统一该 RPC 的 revision_id=0 为当前 published revision。 |
| D-08 | P1 | **OPEN** | [Commander 2552-2565](../design/Commander_软件设计_v1_59.md#L2552-L2565)、[2632-2653](../design/Commander_软件设计_v1_59.md#L2632-L2653)及[Gateway 1790](../design/industrial_gateway_软件设计_v1_52.md#L1790)仍有 pathname/模板根规范与测试。 |
| D-09 | P1 | **CLOSED** | [Contracts 1203-1267](../design/Contracts_软件设计_v1_44.md#L1203-L1267)及 Collector 当前格式均为 SampleBatch v3，v2 仅为历史迁移。 |
| D-10 | P1 | **CLOSED** | Data Generator 不填写 config_revision_id，Contracts v3 字段表一致。 |
| D-11 | P1 | **CLOSED** | [Contracts 781](../design/Contracts_软件设计_v1_44.md#L781)、[1197-1199](../design/Contracts_软件设计_v1_44.md#L1197-L1199)统一 CLI 仅发布、Configuration 广播、模块独立生效。 |
| D-12 | P1 | **CLOSED** | publish、Commander 控制/结果/恢复均进入[CLI 1882-1888](../design/CLI_软件设计_v1_55.md#L1882-L1888)规范树和映射表。 |
| D-13 | P1 | **OPEN（回归）** | [Data Generator 3071-3089](../design/Data_Generator_软件设计_v1_62.md#L3071-L3089)新增 variable_mapping，允许不同变量名；[Gateway 1903-1907](../design/industrial_gateway_软件设计_v1_52.md#L1903-L1907)仍要求变量 ID=point_id。当前 sim 配置映射虽全为恒等，设计规则仍冲突。 |
| D-14 | P1 | **OPEN** | Gateway 入站表已补 10 RPC；[Data Generator 1226](../design/Data_Generator_软件设计_v1_62.md#L1226)仍写 8 个专属+9 公共，与同文件九项 RPC 表冲突。 |
| D-15 | P1 | **CLOSED** | Contracts、Gateway、Data Generator 设计统一内部无 TLS/mTLS、隔离网络和受控令牌。新配置残留另列 N-06。 |
| D-16 | P1 | **OPEN** | [Sink 3354-3382](../design/Sink_Service_软件设计_v1_75.md#L3354-L3382)仍称 Main 更新正式 revision、Start/Stop/Apply 共用 CAS；[3703-3705](../design/Sink_Service_软件设计_v1_75.md#L3703-L3705)测试仍要求 Push/Apply 文件提交。 |
| D-17 | P1 | **OPEN（回归）** | 正文禁止无证明时支持 require_dedup，但[Sink 3934](../design/Sink_Service_软件设计_v1_75.md#L3934)专项测试仍要求其按声明通过准入，与[Storage 2111-2115](../design/Storage_Service_软件设计_v1_9.md#L2111-L2115)证明前提不一致。 |
| D-18 | P1 | **CLOSED** | Core、Configuration、Storage 均要求同一展开 PointTable 内来源点唯一映射一个 BusinessPoint。 |
| D-19 | P1 | **CLOSED** | [Storage 1120](../design/Storage_Service_软件设计_v1_9.md#L1120)、[2119-2134](../design/Storage_Service_软件设计_v1_9.md#L2119-L2134)统一单实例、独占 SQLite、单 Consumer。 |
| D-20 | P2 | **OPEN** | [Configuration 1351-1359](../design/Configuration_Service_软件设计_v1_22.md#L1351-L1359)仍混用现有契约、“新增应先定稿/建议字段”和 Contracts v1_31。 |
| D-21 | P2 | **CLOSED** | [Contracts 1164-1166](../design/Contracts_软件设计_v1_44.md#L1164-L1166)已删除 SemanticPoint，现行仅 BusinessPoint/PointTable 等对象。 |
| D-22 | P2 | **CLOSED** | Collector 启停只写本地运行覆盖，不改不可变 YAML；Service 示例已包含 tasks Instance。 |
| D-23 | P2 | **CLOSED** | [Contracts 796](../design/Contracts_软件设计_v1_44.md#L796)原状态表已列 target_revision=4/active_revision=5，Commander 同步映射。Storage 另存陈旧表述列 N-08。 |
| D-24 | P2 | **CLOSED** | [跨模块测试 518-524](../design/wind-hub_跨模块测试设计_v0_43.md#L518-L524)改为全部 Proto Descriptor/实际 CLI 树动态枚举，51/73 仅历史统计，Storage 纳入分母。 |
| D-25 | P2 | **OPEN** | [CLI 642](../design/CLI_软件设计_v1_55.md#L642)、[1743](../design/CLI_软件设计_v1_55.md#L1743)、[2357](../design/CLI_软件设计_v1_55.md#L2357)仍引用 Contracts v1_43/v1_18、Data Generator v1_61，而非全部当前版本。 |
| D-26 | P3 | **CLOSED** | CLI 明确 ReadPoints 一次性现场主动读，watch 为周期独立请求。 |
| D-27 | P3 | **CLOSED** | Commander 统一现有 SubmitCommands/GetRequestResult。 |
| D-28 | P3 | **CLOSED** | Data Generator/Gateway 分别引用当前 Contracts/正式 Proto。 |

## 4. C-01～C-14 与 N-01～N-03 关闭矩阵

| ID | 原级别 | 第三轮状态 | 当前证据与理由 |
|---|---|---|---|
| C-01 | P1 | **CLOSED** | 三根已选 PointTable 均固定 BP1.5/Units1.2；完整 Core Loader 通过。历史未固定对象未放入活动根，也未被新目录重解释。 |
| C-02 | P1 | **CLOSED** | prod-a@1.6.0 继续选择 Storage-main@1.2.0，site/devices 精确闭包存在。 |
| C-03 | P1 | **CLOSED** | prod Sink Instance@1.4.0 选择 Kafka Template@1.2.0，与 Storage 使用相同 Broker/Topic；e2e Topic 独立一致。 |
| C-04 | P1 | **CLOSED（静态）** | prod/e2e Delivery Collector ID 与所选 Service 一致，8 个来源 device-point 对全部存在；真实消息头仍属运行时专题。 |
| C-05 | P1 | **CLOSED（独立根）** | 每根唯一 Deployment：prod-a@1.6.0、sim-a@1.4.0、e2e-final@1.0.1；LocalSelection 通过。 |
| C-06 | P1 | **CLOSED（文件层）** | cli-sim@1.5.0 使用指定 53060/53051/53052/53073/53061，manager target/state/log 与生产重叠均为 0；关联监听和五个 Scenario Gateway 路由一致。运行时解析另列 N-04。 |
| C-07 | P2 | **CLOSED（台账结构）** | [版本台账](config_version_release_ledger_20261123.csv)117/117 覆盖版本化 YAML，78 历史项发布状态未知、39 候选未发布；不再从 Git 推断正式批准。真实历史发布证据仍属生产边界。 |
| C-08 | P2 | **OPEN / PRODUCTION_SAFETY** | Gateway Sink Delivery 仍 STOP，安全 Listener、租约和授权未验收，不由静态配置关闭。 |
| C-09 | P2 | **CLOSED** | CSV Symbol 引号修复保持，111 行对账一致。 |
| C-10 | P2 | **DEFERRED_TO_CODE_REVIEW** | hard-coded BP@1.5.0 严格规则及未来版本行为仍属生产代码专题。 |
| C-11 | P3 | **OPEN / EXTERNAL_ISOLATION** | E2E 物理基础设施隔离/并行互斥仍无实际环境证据。 |
| C-12 | P3 | **OPEN / HISTORICAL_NOTE** | 历史 farm-a@1.4.0 文件头注释仍保留原错误；不改写历史，仅备案。 |
| C-13 | P1 | **NEEDS_OEM** | 111 个候选身份全部保持 PENDING_OEM_SEMANTICS。 |
| C-14 | P1 | **OPEN / EXTERNAL_EVIDENCE** | RAW 真实载荷、BUSINESS 获批 Manifest、现场设备和实际容量仍无证据。 |
| N-01 | P1 | **CLOSED** | Kafka v1.1.0 Blob=`460fb5d96235b4a6713c85af6102efbde9f85f78`；新 v1.2.0 承担 `windhub-history` 和串行 1 在途，Sink@1.4.0/Deployment@1.6.0 精确选择。 |
| N-02 | P2 | **CLOSED** | [CLI 1884](../design/CLI_软件设计_v1_55.md#L1884)、[1914-1918](../design/CLI_软件设计_v1_55.md#L1914-L1918)唯一使用 `commander batch`、`commander recover-device`，无旧别名。 |
| N-03 | P2 | **CLOSED（CLI 文件层）** | cli-sim@1.5.0 无 tls_profiles/tls_profile/证书字段；Generator 自身另有 gateway_tls 残留列 N-06。 |

## 5. 新发现问题

| ID | 级别 | 状态 | 具体证据与延期/整改边界 |
|---|---|---|---|
| N-04 | P1 | **DEFERRED_TO_CODE_REVIEW** | 三个 sim Service 均通过 Configuration service v2 Schema，但第一方运行时拒绝：Commander 要求 schema v1 和 `security.ca_cert/certificate_identity`；Gateway 要求 schema v1、`servers.4.control.simulation.tls` 且拒绝顶层 security；Generator 拒绝 schema v2。字段为 `security.callers[].token_env`。证据：[schema-checks.log](design_config_freeze_retest_round3_20261123_evidence/schema-checks.log)、[Commander Schema](../../src/commander/infrastructure/config/service_settings.py#L38-L116)、[Gateway Schema](../../src/industrial_gateway/infrastructure/config/schema.py#L198-L213)。 |
| N-05 | P1 | **DEFERRED_TO_CODE_REVIEW** | e2e Configuration Schema 33/35：`service_commander-e2e@1.0.0` 的 `simulation_targets` 未登记在 service 根字段；`service_gateway-e2e-target@1.0.0` 的 `grpc.sink_listener_port` 未登记在 grpc 字段。需代码专题裁决扩展 Schema 还是改配置，不能宣称该根可通过 Repository 校验。证据：[Schema 根字段](../../src/configuration_service/application/schema_catalog.py#L57-L88)、[grpc 字段](../../src/configuration_service/application/schema_catalog.py#L157-L166)。 |
| N-06 | P2 | **OPEN** | [data-generator-sim@1.1.0 18-23](../../configs/services/service_data-generator-sim_v1.1.0.yaml#L18-L23)仍启用 `output.gateway_tls` 的 CA/证书/私钥，与无 mTLS 设计冲突。移除/替代该候选字段是配置整改；运行时新认证支持仍随 N-04 延期。 |
| N-07 | P2 | **OPEN** | [Gateway 964-968](../design/industrial_gateway_软件设计_v1_52.md#L964-L968)需求追溯仍用 8+9/17，与当前 Gateway 10、总 19 冲突，可能漏掉两个仿真执行 RPC。 |
| N-08 | P2 | **OPEN** | [Storage 1262](../design/Storage_Service_软件设计_v1_9.md#L1262)仍称 Common Proto 未必有 target/active 字段，与[Contracts 796](../design/Contracts_软件设计_v1_44.md#L796)、[921](../design/Contracts_软件设计_v1_44.md#L921)已冻结字段号 4/5 冲突。 |
| N-09 | P3 | **OPEN** | [冻结审查 §15.1](full_module_freeze_readiness_review_20261123.md#L259-L263)写台账 112 个，实际源 YAML/台账均 117；[§15.3](full_module_freeze_readiness_review_20261123.md#L271-L273)仍写活动 sim-a@1.3.0，当前为 1.4.0。应更新或明确标为已被 §15.4 替代的历史描述。 |

## 6. 三套根完整闭包与 Loader 结果

| 独立根 | YAML | 唯一 Deployment | Service / Instance | 设备/点 | 现有严格脚本 | Core Domain Snapshot | Configuration Schema |
|---|---:|---|---|---|---|---|---|
| [prod-a](../../deploy/config_roots/prod-a/) | 29 | prod-a@1.6.0 | 8 / 6 | 10 台；121 点 | PASS，0 flagged | **PASS**，BP 400、manifest 10 | **29/29 PASS** |
| [sim-a](../../deploy/config_roots/sim-a/) | 23 | sim-a@1.4.0 | 5 / 4 | 5 台；103 点 | PASS，0 flagged | **PASS**，BP 400、manifest 15 | **23/23 PASS** |
| [e2e-final](../../deploy/config_roots/e2e-final/) | 35 | e2e-final@1.0.1 | 9 / 6 | 6 台；103 点 | PASS，0 flagged | **PASS**，BP 400、manifest 15 | **33/35，见 N-05** |

### prod-a@1.6.0

- 双 Collector→site example_site@1.1.0、devices farm-a@1.6.0、tasks farm-a@1.0.0；Commander/Gateway→同一 Device；DG→Site/Device/Scenario@1.0.0。
- GW Model@1.5.0→PointTable@1.2.0；TZ Model@1.4.0→PointTable@1.2.0；均固定 BP1.5/Units1.2。
- Sink-west→sinks prod-a@1.4.0、deliveries prod-a@1.5.0；history_kafka→Kafka Template@1.2.0。
- Storage-main@1.2.0 与 Sink 均为 Broker `192.168.1.20:9092`、Topic `windhub-history`。
- 10 台设备完整 Domain Snapshot 通过，121 点无重复 BusinessPoint。

### sim-a@1.4.0

- 选择 collector-sim@1.0.0、commander-sim@1.2.0、gateway-sim@1.2.0、data-generator-sim@1.1.0、cli-sim@1.5.0。
- Device sim@1.4.0、Scenario sim@1.0.4；五个 Model/PointTable@1.2.0→BP1.5/Units1.2。
- CLI 五目标端口为 53060/53051/53052/53073/53061；target 为 configuration-sim/collector-sim/commander-sim/gateway-sim/data-generator-sim。
- 所有进程 state_dir=`/var/lib/wind-hub/sim-a/cli-processes`，日志位于 `/var/lib/wind-hub/sim-a/logs/`；与生产 endpoint/target/state_dir 交集均为空。
- Configuration 本地 bootstrap 纯解析 PASS，身份 configuration-sim、端口 53060、七个令牌身份与权限集合一致。
- 五个 Scenario Gateway 均路由 127.0.0.1:53073，PPC control→53052、simulation→53061；变量映射全部为恒等。
- 文件层隔离 PASS，第一方运行时兼容问题仍按 N-04 延期。

### e2e-final@1.0.1

- 双 Collector、Commander、双 Gateway、DG、Sink、Storage、CLI 共 9 Service、6 Instance。
- Device e2e-final@1.0.1，五 Model/PointTable@1.2.0，6 设备含独立 WTG_SINK01。
- 四出口 Delivery 均 STOP，Sink/Storage Topic=`windhub.e2e.history`。
- Core Domain Snapshot 通过；Configuration Schema 两个字段缺口见 N-05，不能称 Repository 发布校验通过。

详细证据：[loader-checks.log](design_config_freeze_retest_round3_20261123_evidence/loader-checks.log)、[schema-checks.log](design_config_freeze_retest_round3_20261123_evidence/schema-checks.log)、[sim-isolation-checks.log](design_config_freeze_retest_round3_20261123_evidence/sim-isolation-checks.log)。

## 7. Git 不可变、哈希与版本台账

| 检查 | 结果 |
|---|---|
| Kafka v1.1.0 历史 Blob | **PASS**：`460fb5d96235b4a6713c85af6102efbde9f85f78` |
| Kafka v1.2.0 新候选 | Blob=`c29d6b82ac55a9ea1b9b567d0df3b6d9492a9d6c`，Topic/串行策略仅在新版 |
| 根文件与源 configs | **87/87 字节相等**；[hash manifest](design_config_freeze_retest_round3_20261123_evidence/root-source-hash-manifest.csv) |
| 源版本化 YAML | 117 份严格解析通过；schema v1=109、v2=8 |
| 台账覆盖与 Blob | **117/117 匹配**；78 UNCHANGED_HISTORY、39 NEW_CANDIDATE |
| 发布状态 | 78 HISTORICAL_PUBLICATION_STATUS_UNVERIFIED、39 UNPUBLISHED_CANDIDATE；无正式批准推断 |
| 语义审批 | 112 UNVERIFIED、5 PENDING_OEM_OR_MANIFEST |
| 新旧 PointTable 语义 | 8 个新表除 file_version/config_sources 外与原表完全相同 |
| 非 YAML 历史文件 | `configs/history/demo.csv` 不属于本轮版本化 YAML 台账分母 |

台账证据：[ledger-checks.log](design_config_freeze_retest_round3_20261123_evidence/ledger-checks.log)。首轮脚本误把非 YAML demo.csv 纳入分母的失败已保留，过滤为任务指定的版本化 YAML 后通过，未掩盖差异。

## 8. GW2MW 逐点结果

| 检查项 | 结果 |
|---|---|
| BusinessPoint v1.4.0/v1.5.0 | 293/400 |
| 原定义 | 293/293 完整定义相等，删除 0、变化 0 |
| 新候选 | 107/107 完整 |
| Point ID、ADS Symbol/address、类型、单位、scale/offset | 111/111 无非预期变化 |
| 新 BusinessPoint 身份 | 111 个互异，目标均存在 |
| 类型/量纲 | 98 REAL→float32、13 BOOL→bool，全部兼容；BOOL 恒等缩放 |
| OEM | **111/111 PENDING_OEM_SEMANTICS**，BUSINESS 生产语义未批准 |

证据：[111 点结果](design_config_freeze_retest_round3_20261123_evidence/gw2mw_businesspoint_retest_results_20261123.csv)、[400 行目录差异](design_config_freeze_retest_round3_20261123_evidence/businesspoint_catalog_diff_20261123.csv)、[汇总 JSON](design_config_freeze_retest_round3_20261123_evidence/gw2mw_retest_summary.json)。

## 9. 实际命令和退出码

完整命令见[commands.md](design_config_freeze_retest_round3_20261123_evidence/commands.md)。

| 验证 | 退出码 | 结论 |
|---|---:|---|
| 环境/fetch/HEAD | 0 | 基线一致，初始工作区干净 |
| 三根现有严格审查 | 0/0/0 | 121/103/103 点均 0 flagged |
| 三根 Core Domain Snapshot | 0 | 10/5/6 设备完整 PASS |
| 三根引用、目标、87 哈希、Kafka Blob | 0 | 全部通过 |
| Configuration Schema + sim 第一方纯解析 | 1 | prod/sim Schema 全过；e2e 两个字段失败；三个 sim 运行时解析拒绝，登记 N-04/N-05 |
| sim 隔离与 Scenario 路由 | 0 | 端口/target/state/log 无生产重叠，CLI 无 TLS |
| 版本台账 | 0 | 117/117 Blob 与状态一致 |
| 新旧版本迁移 | 0 | 仅预期变化 |
| GW2MW 逐点对账 | 0 | 111/111 结构 PASS，OEM 全 PENDING |

复测脚本首轮类名错误和非 YAML 分母错误均保留原始日志；修正后得到上述实际结果，未删除失败证据或降低验证条件。

## 10. 未验证项目与代码专题延期清单

- `SOFTWARE_TESTS=NOT_RUN`：Ruff、mypy、pytest、业务模块测试、系统/E2E/性能/长稳均未执行。
- N-04：sim Schema v2 `security.callers[].token_env` 与 Commander/Gateway/Generator 第一方运行时解析不兼容。
- N-05：e2e `simulation_targets`、`grpc.sink_listener_port` 与 Configuration Schema 不兼容。
- C-10：BP@1.5.0 硬编码严格规则和未来版本适配。
- 真实 Collector 消息头、Deployment Observer、CLI 进程管理、Storage 幂等实现、令牌权限执行及实际服务启动均延期代码/运行时专题。
- C-08/C-11/C-13/C-14 及历史真实发布 Manifest、RAW/BUSINESS、OEM、现场设备和容量保持外部门禁，不能由静态复测关闭。

## 11. 最终设计冻结建议

1. **先关闭仍开放的 P1 设计冲突**：D-05/D-08/D-13/D-14/D-16/D-17，尤其删除原 Push/Apply、pathname、变量映射和 require_dedup 测试中的第二套规则。
2. **清理 P2/P3 残留**：D-20/D-25、N-06～N-09，同步原接口表、追溯计数、状态说明、Gateway TLS 配置及审计版本数据。
3. **代码专题先裁决 N-04/N-05**：统一 Configuration Schema、各第一方运行时 Schema 与候选 YAML；未裁决前不得启动这些候选或宣称 Repository validate-only 通过。
4. **有权评审单独批准总体勘误**：D-01/D-04/D-06 未批准前始终保持 `DESIGN_FREEZE_APPROVAL_PENDING`。
5. **整改后按同一旧 45 项和新增问题复测**。只有结构缺陷关闭且勘误获批，才建议设计冻结；生产激活和软件测试继续独立裁决。

# wind-hub 设计与配置冻结第二轮独立复测报告（2026-11-23）

## 0. 最终裁决

| 裁决项 | 结果 | 理由 |
|---|---|---|
| **DESIGN_CONFIG_REVIEW** | **FAIL** | 三套候选根的 YAML、引用闭包和 Core Domain Snapshot 均通过，但原设计问题仍有 18 项 OPEN、3 项等待冻结批准；另有同版本 YAML 改写、CLI 命令名及 sim mTLS 三项新问题。 |
| **DESIGN_FREEZE_APPROVAL** | **DESIGN_FREEZE_APPROVAL_PENDING** | 受控勘误明确为“供设计冻结评审批准”，并要求未批准前保留 P0；本轮没有用户正式批准证据。 |
| **PRODUCTION_ACTIVATION** | **BLOCKED** | OEM、RAW 真实载荷、BUSINESS 获批 Manifest、真实 Broker/TDengine、现场端点与运行时身份均未满足。 |
| **SOFTWARE_TESTS** | **NOT_RUN** | 按要求未运行 Ruff、mypy、pytest、单模块业务测试、系统测试、E2E 或性能测试。纯文件 Loader 不替代软件测试。 |

原 42 项固定分母复测结果：**CLOSED 19、OPEN 18、DESIGN_FREEZE_APPROVAL_PENDING 3、DEFERRED_TO_CODE_REVIEW 1、NEEDS_OEM 1**。新发现 **N-01 P1、N-02 P2、N-03 P2**，均 OPEN。

**候选发布根的结构闭包通过，不等于设计可以冻结，更不等于生产准入。** 当前不建议宣布设计冻结；应先由 ChatGPT 整改仍开放的设计/配置正文，再由有权评审明确批准总体勘误。

## 1. Git 基线与工作区

- 固定分支：`docs/design-consolidation-20261123`。
- 交接及实际审查 HEAD：`8f31ed59cb7f06e72f80880c564346399e882019`。
- fetch 后本地 HEAD、远端跟踪和 `ls-remote` 三者一致，初始工作区干净。
- 第一轮报告提交 `227323ac368e4090d9884e7da4a1b7d2c6fadc9e` 用于历史比较分母。
- 未切分支、未创建 PR、未合并 main、未 rebase、未 force push、未修改生产代码。
- 总体 v1_27 Git blob 保持 `891486c006c5f964ffa2e61e950104102004f9ab`。
- 本轮只新增本报告及证据；未修改任何设计或配置 YAML。报告提交后的最终 HEAD 由交付消息给出，push 状态单独报告。
- 证据：[git-baseline.log](design_config_freeze_retest_round2_20261123_evidence/git-baseline.log)。

## 2. 权威输入与检查边界

复测覆盖当前 12 份设计书、跨模块测试 v0_42 §15.2～15.3、第一轮 42 项报告、冻结审查 §13～14、102 份源 `configs/` YAML，以及 prod-a/sim-a/e2e-final 三套独立根共 87 份 YAML。

实际执行严格 YAML 解析、现有审查脚本、精确引用及来源点检查、Kafka 目标对账、根/源字节哈希、只读 Core Loader/Domain Snapshot、新旧版本语义比较和 GW2MW 逐点对账。未执行真实 Candidate/Publish、Repository validate-only、业务服务或中间件。

受控勘误见[冻结审查 §13.1](full_module_freeze_readiness_review_20261123.md#L210-L221)：其原文明确“供设计冻结评审批准”及“未批准前……不得直接裁定 PASS”。因此 D-01、D-04、D-06 只能标记待批准，不能由复测方代为批准。

## 3. 全部最新版设计书清单

| 模块 | 当前文件 |
|---|---|
| CLI | [CLI v1_54](../design/CLI_软件设计_v1_54.md) |
| Collector | [Collector v1_42](../design/Collector_软件设计_v1_42.md) |
| Commander | [Commander v1_58](../design/Commander_软件设计_v1_58.md) |
| Configuration | [Configuration Service v1_21](../design/Configuration_Service_软件设计_v1_21.md) |
| Contracts | [Contracts v1_43](../design/Contracts_软件设计_v1_43.md) |
| Core | [Core v1_44](../design/Core_软件设计_v1_44.md) |
| Data Generator | [Data Generator v1_61](../design/Data_Generator_软件设计_v1_61.md) |
| Sink | [Sink Service v1_74](../design/Sink_Service_软件设计_v1_74.md) |
| Storage | [Storage Service v1_8](../design/Storage_Service_软件设计_v1_8.md) |
| Industrial Gateway | [Industrial Gateway v1_51](../design/industrial_gateway_软件设计_v1_51.md) |
| 总体架构 | [总体 v1_27](../design/wind-hub_总体逻辑架构设计_v1_27.md)，原字节未改 |
| 跨模块测试 | [跨模块测试 v0_42](../design/wind-hub_跨模块测试设计_v0_42.md) |

每个模块恰好一份最新版文件；文件换版本身不作为旧问题关闭证据。

## 4. D-01～D-28 逐项关闭矩阵

| ID | 原级别 | 第二轮状态 | 当前证据与裁决理由 |
|---|---|---|---|
| D-01 | P0 | **DESIGN_FREEZE_APPROVAL_PENDING** | [Contracts 1271-1277](../design/Contracts_软件设计_v1_43.md#L1271-L1277)与[Core 1604](../design/Core_软件设计_v1_44.md#L1604)已统一三级语义；[总体 504-510](../design/wind-hub_总体逻辑架构设计_v1_27.md#L504-L510)的互斥定义仍在，须批准勘误替代。 |
| D-02 | P0 | **CLOSED** | [Collector 1059](../design/Collector_软件设计_v1_42.md#L1059)、[1104](../design/Collector_软件设计_v1_42.md#L1104)、[3110-3116](../design/Collector_软件设计_v1_42.md#L3110-L3116)均禁止发布前换算；[Core 1637](../design/Core_软件设计_v1_44.md#L1637)明确仅 Sink 标准化。原工程换算正文已删除。 |
| D-03 | P0 | **CLOSED** | [Contracts 923](../design/Contracts_软件设计_v1_43.md#L923)完整冻结 `GetServiceConfigBundle` 请求三字段、响应五字段、字段号及 `revision_id=0` 当前发布语义。 |
| D-04 | P1 | **DESIGN_FREEZE_APPROVAL_PENDING** | Contracts 已废止 ENGINEERING/默认 BUSINESS；[总体 199](../design/wind-hub_总体逻辑架构设计_v1_27.md#L199)旧正文只能由已批准勘误替代。 |
| D-05 | P1 | **OPEN** | [Contracts 799-800](../design/Contracts_软件设计_v1_43.md#L799-L800)仍给 Common Push/Apply 文件提交语义；[Collector 3320-3348](../design/Collector_软件设计_v1_42.md#L3320-L3348)保留本地候选/Apply；[CLI 2023-2034](../design/CLI_软件设计_v1_54.md#L2023-L2034)仍有 push/apply 成功示例。第二写入权威未从规范正文消除。 |
| D-06 | P1 | **DESIGN_FREEZE_APPROVAL_PENDING** | [Contracts 923](../design/Contracts_软件设计_v1_43.md#L923)禁止任意 roots 替代精确闭包；[总体 436-452](../design/wind-hub_总体逻辑架构设计_v1_27.md#L436-L452)仍画通用 `GetConfigBundle(roots,r)`。勘误 §13.1 的 Deployment/Service 唯一读取路径须获批后生效。 |
| D-07 | P1 | **CLOSED** | [Contracts 923](../design/Contracts_软件设计_v1_43.md#L923)、[Storage 2125](../design/Storage_Service_软件设计_v1_8.md#L2125)、[Data Generator 3288](../design/Data_Generator_软件设计_v1_61.md#L3288)统一该 RPC 的 0=当前 published revision，不套用其它 RPC presence。 |
| D-08 | P1 | **OPEN** | [Commander 2556-2569](../design/Commander_软件设计_v1_58.md#L2556-L2569)仍要求 Instance/模板 pathname 与模板根；[Gateway 1845](../design/industrial_gateway_软件设计_v1_51.md#L1845)仍保留历史 pathname 加载方式。 |
| D-09 | P1 | **OPEN** | Collector 已统一 v3，但[Contracts 1267](../design/Contracts_软件设计_v1_43.md#L1267)仍写“当前代码严格要求 v2”，与同文件当前 v3 字段契约冲突。 |
| D-10 | P1 | **CLOSED** | [Data Generator 3087](../design/Data_Generator_软件设计_v1_61.md#L3087)已删除 `config_revision_id` 写入；[Contracts 1191-1231](../design/Contracts_软件设计_v1_43.md#L1191-L1231)与 Collector v3 一致。 |
| D-11 | P1 | **OPEN** | [Contracts 781](../design/Contracts_软件设计_v1_43.md#L781)仍称 CLI 负责发布后生效协调，同文件 1199 及[CLI 1811-1813](../design/CLI_软件设计_v1_54.md#L1811-L1813)规定不逐模块协调 Apply。 |
| D-12 | P1 | **CLOSED** | [CLI 1882-1888](../design/CLI_软件设计_v1_54.md#L1882-L1888)已包含 publish 和 Commander 控制/结果/恢复命令；叶子命名另立新 N-02。 |
| D-13 | P1 | **CLOSED** | [Data Generator 3071-3089](../design/Data_Generator_软件设计_v1_61.md#L3071-L3089)与[Gateway 1901-1905](../design/industrial_gateway_软件设计_v1_51.md#L1901-L1905)统一设备内变量 ID=PointTable point_id。 |
| D-14 | P1 | **OPEN** | [Gateway 1681-1692](../design/industrial_gateway_软件设计_v1_51.md#L1681-L1692)入站全集仍漏两个仿真执行 RPC；[Data Generator 1993](../design/Data_Generator_软件设计_v1_61.md#L1993)仍写 8+9，与九项专属 RPC 表冲突。 |
| D-15 | P1 | **CLOSED** | [Gateway 1873](../design/industrial_gateway_软件设计_v1_51.md#L1873)、[Data Generator 3152-3154](../design/Data_Generator_软件设计_v1_61.md#L3152-L3154)、[Contracts 1283-1285](../design/Contracts_软件设计_v1_43.md#L1283-L1285)统一内部无 TLS/mTLS、受控网络和令牌。 |
| D-16 | P1 | **OPEN** | [Sink 2471-2516](../design/Sink_Service_软件设计_v1_74.md#L2471-L2516)虽新增运行启停不改 YAML，[3295](../design/Sink_Service_软件设计_v1_74.md#L3295)、[3359-3367](../design/Sink_Service_软件设计_v1_74.md#L3359-L3367)仍保留 Main 写正式快照、Apply 磁盘提交和权威修订号。 |
| D-17 | P1 | **CLOSED** | [Sink 2729-2736](../design/Sink_Service_软件设计_v1_74.md#L2729-L2736)承认 Kafka v2 无 delivery_id，未证明前不支持 require_dedup；[Contracts 1291-1307](../design/Contracts_软件设计_v1_43.md#L1291-L1307)与[Storage 2109-2115](../design/Storage_Service_软件设计_v1_8.md#L2109-L2115)统一稳定 logical_key。 |
| D-18 | P1 | **CLOSED** | [Core 1623-1627](../design/Core_软件设计_v1_44.md#L1623-L1627)、[Configuration 1709-1715](../design/Configuration_Service_软件设计_v1_21.md#L1709-L1715)、[Storage 2111](../design/Storage_Service_软件设计_v1_8.md#L2111)均为一来源点一个业务身份。 |
| D-19 | P1 | **CLOSED** | [Storage 1799-1802](../design/Storage_Service_软件设计_v1_8.md#L1799-L1802)、[2117-2134](../design/Storage_Service_软件设计_v1_8.md#L2117-L2134)统一单进程/实例、单 Consumer、SQLite 独占；多实例明确留待后续设计。 |
| D-20 | P2 | **OPEN** | [Configuration 1349-1359](../design/Configuration_Service_软件设计_v1_21.md#L1349-L1359)仍同时写契约已定义和 Repository Proto/RPC 待新增，与 Contracts 当前状态冲突。 |
| D-21 | P2 | **OPEN** | Core 禁止第三层，Configuration 示例已改 BusinessPoint，但[Contracts 1164-1166](../design/Contracts_软件设计_v1_43.md#L1164-L1166)仍列 SemanticPoint 为现行关系对象。 |
| D-22 | P2 | **OPEN** | [Collector 3100-3106](../design/Collector_软件设计_v1_42.md#L3100-L3106)仍要求启停修改设备实例；后文改为本地运行覆盖且不得改 YAML；[3275-3286](../design/Collector_软件设计_v1_42.md#L3275-L3286) Service 示例仍漏 tasks。 |
| D-23 | P2 | **OPEN** | [Contracts 921](../design/Contracts_软件设计_v1_43.md#L921)已冻结 target/active_revision 字段，Commander 也已映射；但原[Contracts 796](../design/Contracts_软件设计_v1_43.md#L796) `GetServiceStatus` 输出表仍只列 health/dependencies/observed_at。新增字段声明未修复原表，故不能关闭。 |
| D-24 | P2 | **OPEN** | [跨模块测试 489](../design/wind-hub_跨模块测试设计_v0_42.md#L489)、[518-524](../design/wind-hub_跨模块测试设计_v0_42.md#L518-L524)仍以固定 73 CLI/51 RPC 为全集，[719-728](../design/wind-hub_跨模块测试设计_v0_42.md#L719-L728)又要求动态包含 Storage。 |
| D-25 | P2 | **OPEN** | [CLI 642](../design/CLI_软件设计_v1_54.md#L642)仍引用 Contracts v1_41；[Core 1498](../design/Core_软件设计_v1_44.md#L1498)仍引用不存在的总体 v1_15。 |
| D-26 | P3 | **CLOSED** | [CLI 1606-1623](../design/CLI_软件设计_v1_54.md#L1606-L1623)已改为客户端主动 ReadPoints 周期请求，与现场读取定义一致。 |
| D-27 | P3 | **CLOSED** | [Commander 2497-2503](../design/Commander_软件设计_v1_58.md#L2497-L2503)、[2600-2604](../design/Commander_软件设计_v1_58.md#L2600-L2604)统一现有 SubmitCommands/GetRequestResult。 |
| D-28 | P3 | **CLOSED** | [Data Generator 1907-1913](../design/Data_Generator_软件设计_v1_61.md#L1907-L1913)引用现行 Contracts §9；[Gateway 1881-1884](../design/industrial_gateway_软件设计_v1_51.md#L1881-L1884)直接引用正式 Proto。 |

## 5. C-01～C-14 逐项关闭矩阵

| ID | 原级别 | 第二轮状态 | 当前证据与裁决理由 |
|---|---|---|---|
| C-01 | P1 | **CLOSED（三个活动候选根）** | 三根所有已选 PointTable 均显式固定 BP@1.5.0/Units@1.2.0；完整 Loader 通过。旧未固定 YAML 仅作历史对象保留，未放入活动根，也未用新目录重解释；其原始发布 Manifest 仍不可追认。 |
| C-02 | P1 | **CLOSED** | [prod-a@1.5.0 33-35](../../configs/deployments/deployment_prod-a_v1.5.0.yaml#L33-L35)选择 Storage-main@1.2.0，Storage 的 site/devices 由同一 Deployment 解析。 |
| C-03 | P1 | **CLOSED（当前候选目标）** | prod Sink/Storage 均为 Broker `192.168.1.20:9092`、Topic `windhub-history`；e2e 均为 `127.0.0.1:9092`/`windhub.e2e.history`。达到该结果时发生同版本改写，另列 N-01。 |
| C-04 | P1 | **CLOSED（静态身份）** | prod Delivery 四条 Collector ID 已改为所选 `collector-realtime/collector-reliable`；12 个来源 device-point 对全部存在。真实消息头是否一致延期代码/运行时专题。 |
| C-05 | P1 | **CLOSED（独立根选版）** | 每根恰好一个 Deployment：prod-a@1.5.0、sim-a@1.2.0、e2e-final@1.0.1；精确 LocalSelection 通过。仓库总 configs 仍不是发布根，实际 Repository 行为 NOT_RUN。 |
| C-06 | P1 | **OPEN** | cli-sim 不再直引生产 DG/Storage，但[cli-sim 20-64](../../configs/services/service_cli-sim_v1.3.0.yaml#L20-L64)与生产 CLI 共用五个 endpoint、manager target、state_dir，缺独立主机/命名空间证据，仍可能操作同一生产管理目标。证据：[domain-target-overlap.log](design_config_freeze_retest_round2_20261123_evidence/domain-target-overlap.log)。 |
| C-07 | P2 | **OPEN** | 发布台账仍缺失；且第一轮后已有 `sink_kafka_history_v1.1.0.yaml` 同版本语义改写，具体新问题升级为 N-01。不得给历史不可变 PASS。 |
| C-08 | P2 | **OPEN** | prod Gateway Delivery 继续 STOP，目标 listener/租约/权限未完成；不得启用。 |
| C-09 | P2 | **CLOSED** | 第一轮 CSV 引号修复保持有效；111 行 Symbol 与 YAML 全部一致，其他字段未发生回归。 |
| C-10 | P2 | **DEFERRED_TO_CODE_REVIEW** | hard-coded BP@1.5.0 严格规则及未来版本适配仍属生产代码专题，本轮未改代码。 |
| C-11 | P3 | **OPEN** | E2E 与生产仍共享本机基础设施端点形式，逻辑 Topic/Stream/目录隔离不等于物理隔离；并行执行仍需独立环境或互斥。 |
| C-12 | P3 | **OPEN** | 历史 farm-a@1.4.0 文件头注释仍为 v1.3.0；不改写历史文件，仅保留登记。 |
| C-13 | P1 | **NEEDS_OEM** | 111 个候选身份全部保持 `PENDING_OEM_SEMANTICS`，结构 PASS 不能替代 OEM 审批。 |
| C-14 | P1 | **OPEN / NOT_READY** | RAW 真实解码前载荷、BUSINESS 获批 Manifest、现场设备和真实容量均无新证据，生产条件不可由静态复测关闭。 |

## 6. 新发现问题

| ID | 级别 | 状态 | 证据与修复建议 |
|---|---|---|---|
| N-01 | P1 | OPEN | 第一轮后既有[sink_kafka_history_v1.1.0.yaml](../../configs/templates/sinks/sink_kafka_history_v1.1.0.yaml)被原地修改：Topic 从 `windhub.samples` 改为 `windhub-history`，并发能力从 true/4 改为 false/1。与“新建不覆盖候选版本链”和不可变规则冲突。建议恢复既有 1.1.0 原 Git Blob，把候选变更放入新 Template 版本，并更新未发布候选引用。 |
| N-02 | P2 | OPEN | [CLI 1884](../design/CLI_软件设计_v1_54.md#L1884)规范树使用 `submit-commands`、`recover`，而[1824](../design/CLI_软件设计_v1_54.md#L1824)、[1914-1917](../design/CLI_软件设计_v1_54.md#L1914-L1917)使用 `batch`、`recover-device`。应统一唯一正式叶子名称及所有映射/测试描述。 |
| N-03 | P2 | OPEN | [cli-sim 30-64](../../configs/services/service_cli-sim_v1.3.0.yaml#L30-L64)仍引用 `tls_profile: local` 和 CA/客户端证书/私钥，与当前内部无 mTLS 设计不一致。应在候选配置中移除旧 profile；实际客户端迁移和兼容验收属代码专题。 |

## 7. 三套候选发布根引用矩阵

### 7.1 汇总与 Domain Snapshot

| 独立根 | YAML | Deployment | Service / Instance | 设备 | PointTable / Point | Loader 结果 |
|---|---:|---|---|---:|---|---|
| [prod-a](../../deploy/config_roots/prod-a/) | 29 | prod-a@1.5.0 | 8 / 6 | 10：GW01～05、TZ01～05 | GW 111 + TZ 10 = 121 | **PASS**，BP 400，source manifest 10 |
| [sim-a](../../deploy/config_roots/sim-a/) | 23 | sim-a@1.2.0 | 5 / 4 | 5 | 32+16+20+15+20 = 103 | **PASS**，BP 400，source manifest 15 |
| [e2e-final](../../deploy/config_roots/e2e-final/) | 35 | e2e-final@1.0.1 | 9 / 6 | 6：含独立 WTG_SINK01 | 同五表共 103 | **PASS**，BP 400，source manifest 15 |

prod-a 的 10 台设备均成功构建完整 Domain Snapshot，未再出现 `mixed pinned and unpinned PointTable catalog interpretations`。证据：[loader-checks.log](design_config_freeze_retest_round2_20261123_evidence/loader-checks.log)。

### 7.2 prod-a@1.5.0

| Service | Instance 精确版本 | 关键 Template/目标 |
|---|---|---|
| collector-realtime@1.1.0 / collector-reliable@1.0.0 | site=example_site@1.1.0；devices=farm-a@1.6.0；tasks=farm-a@1.0.0 | GW Model@1.5.0→GW PT@1.2.0；TZ Model@1.4.0→TZ PT@1.2.0；均→BP1.5/Units1.2 |
| commander@1.4.0 / gateway@1.2.0 | devices=farm-a@1.6.0 | 同上；Gateway `gateway-tz01` |
| data-generator@1.1.0 | site/devices 同上；scenarios=prod-a@1.0.0 | 同上 |
| sink-west@1.2.0 | sinks=prod-a@1.3.0；deliveries=prod-a@1.5.0 | history_kafka→kafka_history@1.1.0；Delivery 四条来源点均存在 |
| storage-main@1.2.0 | site/devices 同上 | Broker/Topic=`192.168.1.20:9092`/`windhub-history`，与 Sink 一致 |
| cli@1.2.0 | 无 Instance | 本地 bootstrap 仍属运行时核验范围 |

### 7.3 sim-a@1.2.0

| Service | Instance 精确版本 | 关键 Template/目标 |
|---|---|---|
| collector-sim@1.0.0 | site=example_site@1.1.0；devices=sim@1.3.0；tasks=sim@1.0.0 | 五 Model@1.2.0→对应 PT@1.2.0→BP1.5/Units1.2 |
| commander-sim@1.0.0 / gateway-sim@1.1.3 | devices=sim@1.3.0 | 同上 |
| data-generator-sim@1.0.3 | site/devices 同上；scenarios=sim@1.0.3 | 同上 |
| cli-sim@1.3.0 | 无 Instance | 不直引生产 DG/Storage，但管理 endpoint/target 与生产重叠，C-06 OPEN |

### 7.4 e2e-final@1.0.1

| Service | Instance 精确版本 | 关键 Template/目标 |
|---|---|---|
| 双 Collector@1.0.0 | site/devices/tasks=e2e-final，Device@1.0.1 | 五 Model/PT@1.2.0；BP1.5/Units1.2 |
| Commander、双 Gateway | devices=e2e-final@1.0.1 | 源/目标 Gateway 独立身份 |
| data-generator-e2e@1.0.0 | site/devices=e2e-final；scenarios=e2e-final@1.0.0 | 同上 |
| sink-e2e@1.0.0 | sinks=e2e-final@1.0.0；deliveries=e2e-final@1.0.1 | 四出口 Delivery 均 STOP；Kafka Topic `windhub.e2e.history` |
| storage-e2e@1.0.0 | site/devices=e2e-final@1.0.1 | Topic 与 Sink 一致，TDengine 仅静态配置 |
| cli-e2e@1.0.0 | 无 Instance | E2E 专用进程/bootstrap |

三根没有共享 Service/Instance/Deployment 相对路径；共享的仅为允许复用的不可变 Template。管理端点和基础设施重叠按 C-06/C-11 保留，不据此宣称完全运行隔离。

## 8. YAML、引用、哈希与历史检查

| 检查项 | 结果 | 证据 |
|---|---|---|
| 源 configs 严格解析 | PASS | 102/102；schema v1=97、v2=5；文件 version/schema 检查通过 |
| 三根严格解析与现有脚本 | PASS | 29/23/35 YAML；121/103/103 点均 0 flagged |
| Deployment→Service/Instance | PASS | 8+5+9 Service 选择及所有 Instance 文件存在 |
| Device→Model→Protocol/PointTable | PASS | 三根完整 Loader 均通过 |
| Point/BusinessPoint 一对一 | PASS | 121+103+103 点无重复/缺失业务身份 |
| 类型、单位与量纲 | PASS | Domain Snapshot 完整构建；BP/Units 精确版本一致 |
| Sink/Delivery/Collector/Device/Point | PASS | 8 条 Delivery、8 个来源 device-point 对；sim 无 Delivery |
| Kafka Broker/Topic | PASS（静态） | prod 与 e2e 各自匹配；真实连接 NOT_RUN |
| 根文件与源 configs 字节哈希 | PASS | 87/87 `source_bytes_equal=true`；[hash manifest](design_config_freeze_retest_round2_20261123_evidence/root-source-hash-manifest.csv) |
| 新旧 PointTable 语义迁移 | PASS | 8 个新表除 file_version/config_sources 外与原表完全相同；[migration-checks.log](design_config_freeze_retest_round2_20261123_evidence/migration-checks.log) |
| 历史 YAML 未改写 | **FAIL** | 第一轮后只有 1 个既有路径被改：`sink_kafka_history_v1.1.0.yaml`，见 N-01；其余既有 YAML 未变，新增候选路径 24 个 |

## 9. GW2MW 专项逐点结果

| 检查项 | 第二轮结果 |
|---|---|
| BusinessPoint v1.4.0 / v1.5.0 | 293 / 400 |
| 原定义 | 293/293 完整定义相等，删除 0、变化 0 |
| 新候选 | 107/107 完整 |
| Point ID 与协议字段 | 111/111 保持；完整 ADS address/Symbol、类型、单位、scale/offset、point_groups 未发生非预期变化 |
| 新 BusinessPoint 身份 | 111 个互异，目标均存在 |
| CSV 对账 | 111 行唯一 Point/候选 ID；源/标准类型、单位和 Symbol 全部一致 |
| 类型/量纲 | 98 REAL→float32、13 BOOL→bool；全部兼容，BOOL 恒等缩放 |
| OEM | **111/111 `PENDING_OEM_SEMANTICS`**，未获生产 BUSINESS 批准 |

逐点证据：[111 点结果 CSV](design_config_freeze_retest_round2_20261123_evidence/gw2mw_businesspoint_retest_results_20261123.csv)、[400 行目录差异](design_config_freeze_retest_round2_20261123_evidence/businesspoint_catalog_diff_20261123.csv)、[机器可读汇总](design_config_freeze_retest_round2_20261123_evidence/gw2mw_retest_summary.json)。

## 10. 实际命令、退出码与证据

完整命令见[commands.md](design_config_freeze_retest_round2_20261123_evidence/commands.md)。关键结果：

| 验证 | 退出码 | 结论 |
|---|---:|---|
| 环境/fetch/HEAD | 0 | 基线一致，工作区初始干净 |
| prod-a 现有严格审查 | 0 | 29 YAML、121 点、0 flagged |
| sim-a 现有严格审查 | 0 | 23 YAML、103 点、0 flagged |
| e2e-final 现有严格审查 | 0 | 35 YAML、103 点、0 flagged |
| 三根完整 Core Loader/Domain Snapshot | 0 | 10/5/6 设备全部 PASS |
| 三根引用/目标/哈希及源 configs 检查 | 1 | 所有结构检查完成；唯一失败为 N-01 同版本改写 |
| 新旧版本迁移语义对账 | 0 | 仅预期变化 |
| GW2MW 逐点对账 | 0 | 111/111 结构 PASS，OEM 全 PENDING |
| sim/prod 管理目标对账 | 0 | 五类共同 endpoint/target 全部重叠，支持 C-06 OPEN |

首轮 Loader 统计脚本误读不存在属性的失败已保留在[loader-checks-initial.log](design_config_freeze_retest_round2_20261123_evidence/loader-checks-initial.log)；修正统计字段后得到最终 PASS，未删除失败或降低条件。

## 11. 未执行和延期项目

- 软件测试套件、真实 Repository validate-only、Candidate/Publish、业务服务、Broker、TDengine、系统/E2E/性能/长稳均为 NOT_RUN。
- 实际 Collector 消息头、Deployment 运行时解析、CLI 进程管理、Storage 幂等实现、hard-coded BP@1.5.0 未来版本行为及 TLS 客户端迁移，延期独立代码审查。
- OEM、RAW 真实载荷、BUSINESS 批准 Manifest、现场设备、真实 Broker 认证及实际容量不能由静态复测关闭。
- 生产激活继续 BLOCKED；候选根的 Loader PASS 不能作为运行时或生产测试证据。

## 12. 最终设计冻结建议

1. **先整改新 P1**：恢复 `sink_kafka_history@1.1.0` 原字节，以新版本承载 Topic/并发变化，保持历史不可变。
2. **清理仍开放的 D 项正文**：尤其 D-05/D-08/D-09/D-11/D-14/D-16，以及 D-20～D-25 的原表、示例、测试分母和引用；不能只添加新声明。
3. **闭合管理面配置**：为 sim-a 明确独立网络/进程目标，移除旧 mTLS profile；统一 CLI 正式叶子命令名。
4. **由有权评审正式批准总体勘误**：批准文件需明确替代总体 v1_27 的 RAW、旧 ENGINEERING/默认 BUSINESS 和通用 roots 查询冲突段落。未批准前保持 `DESIGN_FREEZE_APPROVAL_PENDING`。
5. **整改后再次同分母复测**。只有 D/C 结构缺陷关闭且勘误获批，才建议设计结构冻结；随后另启生产代码审查和软件测试专题，生产激活仍独立裁决。

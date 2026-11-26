# Storage 宽表重构 D5 独立测试与缺陷收口（2026-11-26）

最终裁决：**BLOCKED**。目前不具备进入下一生产资格评估阶段的条件。真实支链通过的部分不能替代未通过的契约、恢复、聚合和容量验收。本轮未创建 PR，未合并 main。

## Git、基线与可复核范围

- 分支：`feat/storage-wide-table-completion-20261125`。
- 交接 HEAD：`360d88484b71c60a4eb90c49c7efa8e0a5d1709a`。
- fetch 后实际起始本地/远端 HEAD：`2965d3bd0d91ff515a9ee75652751b4dc26dab6f`；差异仅为用户已提交的 tar 忽略规则。起始工作树干净，未覆盖用户修改。
- 最终源码/测试 HEAD：`dd2a0dfd097776b9660bc1177528399e15051d3f`。报告与证据将在其后作为独立文档提交；最终推送提交的完整 HEAD 在交付答复及 Git 元数据中给出，不能在其自身内容中自引用。
- `origin/main` 对照基线：`141ac2b78b83d2fbf49da71147358e7393d165cd`。通过 git archive 临时解包比较，未切换工作分支。
- 模块整批结果主要绑定 `d64370318` 修复树；容量测试绑定 `a0b37acdf876185305833be6bbf47115cbbae089` 的计数与业务逻辑；后续 `4498dfbf6e97d60bd95080a6be7c62fd6aeb0cb9` 关闭格式、导入、局部类型及异常问题，并执行定向回归。最后一个源码提交只调整 E2E Fixture 的 Collector 路径。没有声称全部旧结果都在最终源码 SHA 上重新执行。

权威依据为 `docs/design/Storage_Service_软件设计_v1_14.md` 和 `docs/design/Contracts_软件设计_v1_48.md`，尤其 Contracts §11.6、§11.9、§14/14.1。当前 `.proto` 是被审查对象，不能以其现存旧字段推翻正式目标。正式文档要求 v2 包、九项 RPC 和宽表分组结构；当前实现采用 v1 包内新增 QueryWideRows 的不同接口。该冲突按授权边界提交 ChatGPT 裁决，没有自行修改设计。

全部日志、JUnit、运行参数和复现记录位于 [evidence](storage_wide_table_d5_independent_20261126_evidence/)。完整主要执行命令见 [commands.txt](storage_wide_table_d5_independent_20261126_evidence/commands.txt)，分阶段命令、退出码、耗时见各 `*_results.json`。测试原始 skipped/deselected 不等同于验收 PASS；重复回归不相加为独立测试总数。

## 环境

Python 3.11.16，解释器 `/home/luosh/miniconda3/envs/wind-hub/bin/python3`；环境检查 PASS。WSL2 Linux 6.6.87.2；硬件资源见 `host_resources.json`。

| 依赖/服务 | 版本 |
|---|---|
| pytest / Ruff / mypy | 8.4.2 / 0.8.6 / 1.20.2 |
| protobuf / grpcio / grpcio-tools | 6.33.6 / 1.84.0 / 1.81.1 |
| mypy-protobuf / aiokafka / taos-ws-py | 5.1.0 / 0.14.0 / 0.7.1 |
| TDengine | 3.3.6.13，原生 SQL SERVER_VERSION 实测并断言 |
| Kafka | apache/kafka:3.9.1，KRaft 独立测试实例 |
| NATS / Redis | nats:2.10-alpine / redis:7-alpine；tag 未锁定补丁号，未声称精确补丁版本 |
| Docker daemon | 29.2.1 |

仅使用本轮独立创建的容器/数据库进行故障注入；WAL 重启容器要求 disposable 标签并核对端口。没有读取 `.env.local`，没有现场 PLC 写入。容量和 E2E 后段共享宿主机，性能结果存在宿主机争用限制，不能直接外推现场容量。

## 静态门禁与 protobuf

正式命令与 `ci-core-v1.yml` 对齐：`python -m ruff check src tests scripts`、`python -m ruff format --check src tests scripts`、`python -m mypy src`、`python scripts/generate_rpc.py --check`、`lint-imports`；另执行 `pytest --collect-only -q`。

| 检查 | 初始结果 | 收口结果 |
|---|---|---|
| Ruff | FAIL：40 项 | PASS |
| Ruff format | FAIL：66 个文件 | PASS |
| mypy | FAIL：19 项、9 个文件 | FAIL：1 项，StorageServicer 尚缺已注册 QuerySamples |
| protobuf check | FAIL：4 个生成物过期 | PASS；已执行正式 generate_rpc.py 生成流程 |
| import-linter | PASS | PASS |
| pytest collection | FAIL：6 处收集错误 | PASS：4183 项收集 |

对照 main：Ruff 已有 5 项、format 已有 7 文件失败，mypy 与 protobuf check 通过。既有失败未被忽略，格式与 Ruff 也在本轮修复；新增类型问题已收敛，剩余问题与本分支 RPC 实现不一致直接相关。证据分别为 `baseline_*`、`static_*`、`delivery_*`。

生成同步只解决生成物漂移，**不代表完成 v2 契约迁移**。没有新增 v1 兼容服务；现有 proto 中遗留的旧接口仍是阻断缺陷。跨客户端、服务端、CLI、消息模型的 v2 迁移属于结构性生产接口整改，未越权实施。

## 模块测试

| 范围 | passed | failed | skipped | errors | 证据 |
|---|---:|---:|---:|---:|---|
| Contracts / Core 最终整批 | 666 | 0 | 0 | 0 | contracts_core_final.xml |
| Configuration 最终整批 | 485 | 0 | 2 | 0 | configuration_final.xml |
| Collector unit/component/integration | 109 | 11 | 0 | 0 | collector_final.xml |
| Collector 首轮全目录 | 91 | 74 | 2 | 0 | collector.xml |
| Sink unit/component | 380 | 38 | 0 | 0 | sink_final.xml |
| Storage 最终 unit/component | 241 | 5 | 0 | 0 | storage_delivery.xml |
| CLI | 273 | 1 | 13 | 0 | cli.xml |
| 类型、格式及相关功能最终回归 | 276 | 5 | 0 | 0 | final_regression.xml |
| 项目进程内 performance | 6 | 0 | 0 | 0 | performance.xml |
| 项目有界 soak | 2 | 0 | 0 | 0 | soak.xml；另 9 deselected |

Collector 全目录驱动在 600 秒超时；其 pytest 子进程后来完成并生成 930.367 秒的原始 XML，因此同时保留退出码 124 和最终测试计数，未把超时当 PASS。后续定向范围不是对失败目录的全绿排除，全目录失败仍计入裁决。

Configuration 两个 skip、CLI 十三个 skip、Collector 两个 skip 的原始原因保存在 `test_summary.json` / JUnit；这些项目不被计为通过。Collector 余下 11 项主要为旧 BatchBuilder 调用参数/旧 Service 路径；Sink 38 项主要为旧 Kafka 模板 schema、旧断言以及 mock adapter 返回的逐点索引与完整行索引不一致；CLI 有 Collector 启动失败。没有恢复旧生产兼容路径来迎合这些测试。完整失败用例、原因和文件模块见 `test_summary.json`，仍须逐项迁移并复测。

Storage 剩余 5 项中，4 项是本轮新增的显式 BAD/UNCERTAIN COUNT/MIN 契约复现，1 项是全部已注册 RPC 鉴权检查暴露 QuerySamples 未实现。没有删除或 xfail 这些有效失败。

## 真实服务与跨模块结果

| 验证 | 结果及边界 |
|---|---|
| Collector → JetStream → Sink → Kafka RAW/PROTOCOL | PASS：4 项，wire_acceptance.xml；真实进程和 broker，完整 v3 编码、缺少 RAW 字节时整批 DLQ 且不产生 Kafka 前缀 |
| FC03 原始协议字节 | PASS：真实 TCP Modbus 测试服务器与驱动，1200.5 的寄存器原始字节与 struct.pack 独立真值一致；继续经过真实 Collector/Sink/Kafka；不是现场 PLC |
| TDengine 原生探针 | 23 passed / 2 failed / 32 skipped，native_tdengine.xml |
| TDengine WAL 容器强制终止再启动 | PASS：1 项，native_wal_restart.xml；已确认完整 500 列行重启后回读一致 |
| Storage CLI 重启激活闭包 | PASS：1 项，real_storage.xml；控制面配置激活验证，不代替业务落库资格 |
| Kafka commit 超时、重放、重启及组交接 | FAIL：1 项，real_kafka.xml；旧消费实例 request_stop 后等待 runner 超时，清理再超时并残留任务；具体生产/测试责任仍 UNKNOWN |
| 八服务 / FC04 / QueryWideRows 与 SQL 回读 | FC04 RAW 支链 PASS：独立 Generator 载荷与 Kafka v3、Storage QueryWideRows、TDengine SQL 回读一致；八服务 smoke FAIL：仍调用已废弃的 `storage samples`，未取得可用样本；这是未迁移调用方，不能据此推断 PROTOCOL 数据丢失。最终 1 passed / 1 failed（eight_service_latest.xml），不得合并标记为八服务 PASS。 |
| BUSINESS 真实链路、单位映射 | NOT_RUN：有本地测试覆盖，未取得真实完整链路的独立业务单位真值证据 |
| 真实聚合 / 分页 / 导出 / 备份恢复组合 | NOT_RUN：本地测试与原生探针不能代替真实 Kafka→Storage→TDengine 全组合验收 |

仓库 `eight_service` 测试栈包含 Storage、双 Collector 等进程，其名称不能等同于用户指定的 DCEMS 闭环。本轮 Data Generator → Gateway2 → Collector → Sink → Gateway1 → DCEMS → Gateway1 → Commander → Gateway2 → Data Generator 完整业务闭环为 **NOT_RUN**；没有取得 DCEMS/Commander 回馈真值，不把 Storage 支链通过算作该闭环通过。

原生探针已实际覆盖有/无符号 8/16/32/64 位、FLOAT、DOUBLE、BOOL、VARCHAR、VARBINARY、NULL，以及 500 列、质量 bitmap、完整行 NULL 覆盖和数据库参数回读。两个失败发生在动态 `INSERT INTO ? VALUES (...)` 的 prepare，返回 `0x022B Table does not exist`，分别阻断多表复用和部分成功重放探针。生产当前按单表创建并关闭 Statement，不把这两个探针失败直接说成所有单表写入失败。

32 个原生 skip 为容量矩阵 30 项、专用重启 1 项、压缩测量 1 项；重启后来独立通过，容量矩阵和压缩仍未执行。没有用健康检查或开放端口替代任何业务 PASS。

## 30 台、10 Hz Sink 容量

正式结果 **FAIL**：`capacity_wide_30.xml`，1 failed，完整测试耗时 735.28 秒。配置为 30 台 × 10 Hz、500 点/行、单 Kafka partition、4 个编码进程、Sink max_safe_inflight=4；持续发布 600 秒、排空上限 120 秒。原始结果 `r14_capacity_d30_r300_c4_b131072_p1_6ae463aadfd8.json`，585 个采样点及 SHA-256 见同目录 JSONL 和 capacity_derived_metrics.json。

| 指标 | 实测 |
|---|---:|
| 发布行 / 值 | 180000 / 90000000 |
| 发布持续时间 | 600.000 秒（原始精度 600.000377968 秒） |
| 稳态发布确认速率 | 299.99 行/秒 |
| Kafka 稳态确认 | 211.70 行/秒，105850.15 值/秒 |
| 排空截止 Kafka 确认行 / 值 | 154721 / 77360500 |
| 积压斜率 | +88.271 批/秒 |
| 排空截止 source pending 合计 | 13745 |
| drained / zero_loss 计数闭合 | false / false |
| Sink 进程 CPU / RSS 峰值 | 90.0% 单核 / 249.1 MiB |
| gRPC 失败 / metrics 失败 / DLQ | 0 / 0 / 0 |
| gRPC 探针 P95 / P99 | 4.205 / 4.935 ms |
| 写入窗口 P99 最大值 | 77.901 ms |
| 写入 P95、全时段端到端 P99 | NOT_RUN：没有对应原始时延样本，不能从窗口 P99 推算 |

设备分区/顺序抽样覆盖 30 台、239 行，每台至少 4 个序列，无顺序或分区违规；被抽样行的 500 个值与质量均独立核验。这不是全部 180000 行的逐值证明。gRPC 探针分位数不代替写入分位数；窗口 P99 最大值不等于合并全时段 P99。

pytest 的首个失败是严格发布速率边界：`180000/600.000377968 < 300`。即便剔除此微小计时边界，211.70 行/秒、持续增长的积压和未排空也独立不满足要求，连脚本原 95% 吞吐标准也达不到。

**环境限制与根因边界：**测试历史流 max_age=180 秒、max_bytes=4 GiB；延迟超过保留期可能导致源流过期。发布与 Kafka 之间差 25279 行，截止 pending 为 13745，计数未闭合，不能把所有差额一律归为持久化永久丢失。末段还与 E2E 复测共享宿主资源。此结果足以拒绝当前工况通过，不能单凭该次测试定位全部生产性能/可靠性根因；后续应扩大测试保留窗口、隔离宿主负载并执行完整身份审计。未采集到独立的逐行永久失败计数，0 个探针失败/DLQ 不等于 0 个业务失败。

初始 2 台诊断和旧逐点计数的 30 台诊断均明确中止，保留 JSONL，不能作为资格通过证据。正式脚本已按 v3 将每个 Kafka Record 计为一行、500 个值，并对采样行的完整点集、逐值、质量和设备分区顺序进行检查。旧 Storage capacity 测试仍构造 v2 和重复 active_power，未据此宣称 Storage 宽表容量通过。

## 缺陷与移交

行号按最终源码给出；详细失败堆栈以对应证据文件为准。

| ID / 严重度 | 文件、位置与影响 | 证据 / 根因 / 最小整改 / 负责人 |
|---|---|---|
| D5-01 P1 | src/contracts/storage_service.proto:2、29、228；Storage RPC 全调用方 | 文档要求 windhub.storage.v2 及九 RPC，现为 v1 + QueryWideRows + 遗留窄表消息/RPC；mypy 与鉴权测试直接失败。ChatGPT 裁决 QuerySamples/QueryWideRows 及精确设备版本过滤差异后，协同迁移 proto、生成物、服务端、CLI、测试；禁止保留无用途兼容接口。 |
| D5-02 P1 | src/storage_service/application/wide_aggregate.py:205 | 显式 BAD/UNCERTAIN 过滤后 COUNT=0、MIN=NULL；4 个有效契约复现失败。正式 Contracts 要求显式 Quality 替代默认 GOOD，且返回 valid_count。ChatGPT 完成质量计数与 v2 结果模型一致的整改，不能只放宽测试。 |
| D5-03 P1 | src/storage_service/application/wide_query.py:262；domain/wide_record.py 与 wide_row.py | 解码、绑定接受 NULL+GOOD，查询校验拒绝同一行。good_null_repro.json / reproduce_good_null.py 为生产函数组合复现（SQL I/O 为 fake，未冒充真实 SQL）。ChatGPT 按“值允许 NULL，质量独立”的冻结语义统一读写；若需改设计必须先裁决。 |
| D5-04 P1，根因 UNKNOWN | tests/storage_service/integration/test_kafka_real.py:169–189；KafkaStorageConsumer 关停/重平衡 | 真实恢复测试失败，finally 的第二次超时还阻断资源清理；已清理仅本轮遗留栈。Claude 可修复测试清理；跨代际 I/O 与持久化语义若需修改必须由 ChatGPT 处理，复测不得延长超时掩盖死锁。 |
| D5-05 P2 | tests/storage_service/integration/test_tdengine_wide_probe.py:191、518；src/storage_service/infrastructure/adapters/outbound/wide_stmt2.py:110 | 多表复用、部分成功门禁未通过；生产每批建/关 statement，尚无复用证据。由 ChatGPT 明确受支持的原生 API 与缓存/多表策略，Claude 重跑适用 TD01–TD12；不以单表 fallback 冒充原目标。 |
| D5-06 P2 | tests/collector/unit/test_batch_builder.py:36；tests/sink_service/unit/test_concurrent_kafka_config.py:38；tests/system/e2e/eight_service/test_e2e_eight_service_smoke.py:138；CLI 失败用例 | 调用方/Fixture 尚未完整迁移到同步设备批和 schema 6 / Kafka 模板 schema 2；部分旧测试还要求 reliable 分片，与正式设计冲突。Claude 负责测试迁移，冻结语义争议交 ChatGPT；保留原始失败和等价有效覆盖。 |
| D5-07 P2 | deploy/config_roots/e2e-final/services/service_cli-e2e_v1.1.8.yaml:64、73、97 | 已激活 Service 为 v2.0.0，CLI 进程参数仍指向不存在的 v1.0.0。测试 Fixture 已显式适配，真实发版配置尚未整改；ChatGPT 统一版本化配置与发布闭包，不能让 Fixture 的补偿遮盖产品配置问题。 |
| D5-08 P2 | tests/system/e2e/eight_service/test_e2e_storage_capacity.py:56、65 | 测试仍使用 Kafka v2、500 次重复同一点，不能验收真实 500 点宽表。Claude 应迁移该专项，ChatGPT 确认完整 Storage 容量模型后执行真实落库与逐值审计。 |
| D5-09 资格阻断 | 最终容量结果、真实聚合导出恢复及 BUSINESS 缺证 | 见本报告容量段；不得由 mock 或估算认定通过。生产优化由 ChatGPT，测试与环境复验由 Claude。 |

没有发现可据现有证据确认的 P0。上表 P1/P2 及必需未验证项目未关闭，因此不能给 PASS 或 PASS_WITH_LIMITATIONS。

## 已执行修复与复测

Claude 在授权范围内已完成：正式 protobuf 生成同步；修复 pytest request 保留名、悬空导入、假 dataclass、导出 artifact ID、错误 SQL 目标和缺少锁初始化的 Fixture；为 Collector/Sink 测试补不可变设备版本和统一采样时间；将 split 用例的旧设计冲突保留为失败而未恢复生产兼容；修复 Sink Fixture schema、Kafka key/dedup 身份和源批 dedup_key；修复八服务 Fixture 的实际启动路径。

局部生产修复包括：Collector supervisor 失败路径未定义 cycle_time/point_ids；宽表重放异常改用既有 HistoryReplayUnavailableError，以便 RPC 返回 UNAVAILABLE；显式 not_collected 类型、数值类型收窄、临时文件 IO 类型、异常源导入和配置字符串类型校验；Collector 缺失 acquisition plan 时明确拒绝。没有修改持久化模型、聚合冻结语义或为废弃 RPC 添加兼容实现。

全范围格式/导入修复关闭了既有及新增 Ruff 失败，没有新增 noqa、type-ignore、目录排除或 skip。回归证据包括 final_regression.xml（276 passed、5 个已知有效失败）、resolver_fixture_regression.xml、collector_failure_regression.xml（失败连接完整 BAD 周期）、replay_regression.xml（20 passed）、fixture_regression.xml、wire_acceptance.xml，以及全量静态日志。任何未通过项均保留在报告与原始日志中。

Python 代码文件版本号检查 PASS。凭据模式扫描只记录路径/行号，三个命中是 CLI 脱敏检测字符串和 AAAA/BBBB 合成测试文本，未发现真实私钥；这不是完整密钥审计保证。证据中的临时测试令牌脱敏后提交，真实环境秘密未读取、未提交。

## 最小必要整改与最终裁决

1. ChatGPT 先裁决并完成正式 Storage v2 契约迁移，删除旧窄表定义/调用，关闭最后一个 mypy 和 RPC 鉴权失败。
2. ChatGPT 修复显式 Quality 聚合及 NULL/quality 读写不一致，Claude 保留并复测严格契约复现。
3. 定位并关闭真实 Kafka 关停/重平衡失败；确认 STMT2 支持方式，完成失败探针及适用恢复证据。
4. Claude 继续迁移剩余有效测试/Fixture；ChatGPT 同步正式部署/CLI 版本引用；重新执行完整模块与真实链路。
5. 关闭正式 30 台/10 Hz Sink 容量结果中的问题，并补齐真实 BUSINESS、聚合、分页、导出/备份、连接中断、进程重启及 Storage 宽表全链路容量证据；P95 缺测不能估算。

**最终裁决：BLOCKED。不能进入下一生产资格评估阶段。** 本轮已修复的是测试环境、生成同步、格式/类型和局部明确缺陷；数据模型、生产 RPC 设计、跨模块语义及持久化正确性整改必须交 ChatGPT。最终源码/测试 HEAD：`dd2a0dfd097776b9660bc1177528399e15051d3f`；随后仅提交本报告和证据，不创建 PR、不合并 main。

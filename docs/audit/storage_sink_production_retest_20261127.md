# wind-hub Storage / Sink 生产整改独立复测（2026-11-27）

## 最终裁决

| 项目 | 结果 | 证据与边界 |
|---|---|---|
| STATIC_GATES | **PASS** | Ruff、format、mypy、import-linter、protobuf、配置与 4279 项收集通过。 |
| STORAGE_MULTITABLE_STMT2 | **PASS** | 真实多设备五 TAG、statement 复用、缓存隔离与完整回读通过。 |
| STORAGE_RELIABILITY | **PASS** | 真实 UNKNOWN、部分成功、连接断开、重放与 offset fail-closed 通过。 |
| STORAGE_CAPACITY_30 | **PASS** | 299.927 行/s，高于 285；18000 行完整且排空 0.011s。 |
| STORAGE_CAPACITY_100 | **PASS** | 954.699 行/s，高于 950；60000 行完整且排空 2.842s。 |
| STORAGE_CAPACITY_300 | **BLOCKED** | 963.429 行/s，低于 2850；最终完整不等于持续容量。 |
| SINK_CAPACITY_600S | **BLOCKED** | 210.27 行/s、持续积压、未排空，且 consumer stop 有 CancelledError。 |
| CROSS_MODULE_E2E | **BLOCKED** | 功能链路 11/11 通过，但 Sink/Storage 强制容量门禁未全部通过。 |
| PRODUCTION_READINESS | **BLOCKED** | 存在必选容量和关闭异常，不能进入 READY_FOR_PRODUCTION_REVIEW。 |

总体 **BLOCKED**。多表 STMT2 与 statement 复用取得实质收益，但 300 台 Storage 和 600 秒 Sink 仍失败；不得用单元测试、原生性能或最终补齐代替持续容量。

## Git 基线、范围与证据

- 分支：`feat/storage-wide-table-completion-20261125`。
- fetch 后本地/远端最新 HEAD：`87a54cd083c844a72fd526012f2f168090f7ebd7`，与交接 `8055762eb7c010f6425e3df70d5ff213582180e4` 不同；自上一轮 `d031f92140d73500ab8e7577f1a4f127ffebe6d2` 起实际为 **19 个提交**，不是 15 个，已按最新远端审查。
- 最终源码/测试 HEAD：`d8dc546415f26901cbda5c6d416c37159bef9787`，已推送；报告及证据随后独立提交，最终 HEAD 以交付答复和 Git 元数据为准。
- 环境：Python 3.11.16 / WSL2；TDengine 与 taosdump 3.3.6.13、taos-ws-py 0.7.1、Kafka 3.9.1、NATS 2.10、aiokafka 0.14.0。真实 STMT2 与 taosdump 无 Mock 替代。
- 证据位于 [evidence](storage_sink_production_retest_20261127_evidence/)，包括 `test_results.json`、`capacity_summary.json`、`environment.json`、完整日志、JUnit、采样 JSONL 和命令。
- 前轮报告 `storage_s1s2_20261127.md` 及其证据用于性能对比，未覆盖或删除历史结果。

## 阶段 A：静态与模块回归

| 检查 | 结果 | 证据 |
|---|---|---|
| Ruff check | PASS | final_ruff.log |
| Ruff format --check | PASS，1120 文件 | final_format.log |
| mypy src | PASS，494 文件 | final_mypy.log |
| import-linter | PASS，22 kept / 0 broken | final_imports.log |
| protobuf --check | PASS | final_protobuf.log |
| pytest collection | PASS，4279 项、0 错误 | final_collection.log |
| 项目 performance gate | 7/7 | performance.xml |

模块结果：Contracts/Core 667、Configuration 485+2 skipped、Collector 120、Sink 428、Storage 280、CLI 274+13 skipped，均零失败。skip 原因仍是真实 NATS 广播或 containerd/iSulad 环境不可用，不计为通过。

门禁初始发现 7 个 mypy 错误和 6 个格式漂移。`7ca2d4816` 对 taosws callable 做显式类型收窄，给连接级 statement 缓存补 OrderedDict 类型并格式化受影响文件；不改变生产行为。初始真实环境竞争（Docker 端口映射 500、TDengine `Out of dnodes`）已保留，DDL 就绪后真实定向 3/3 通过。

## 阶段 B：真实 STMT2 与 Backend 专项

### B1. 多设备合批

`tdengine_targeted_final.xml` 为 3/3，`backend_multitable_real.xml` 为 10/10。使用真实 TDengineBackend、MetadataStore、WideIngestionService 和 500 点完整行验证：

- 同 farm、相同不可变 Schema 的多个设备在一次 STMT2 execute 中绑定，不跨 farm、PointTable 版本、等级或 Schema 指纹混合。
- 五 TAG 为 device_id/device_type/point_table_id/data_level/schema_fingerprint；`data_level TINYINT` 使用 `int_to_tag` 的数值类型，TAG 顺序与 DDL 一致。
- 设备 ID 与子表一一对应，500 点值、Quality 和完整物理列回读一致；同设备同时间戳保持后到覆盖。
- 同连接、同数据库和 Schema Family 复用 statement；不同 Schema 使用不同 statement；跨数据库正确执行 USE 并记录连接上下文。
- LRU 上限生效，驱逐与连接废弃会关闭关联 statement；不跨连接共享 statement；三轮 open/close 无资源泄漏。
- 任何物理组失败、超时、连接断开或真实部分成功时，不提交未确认 Kafka offset；同设备冲突 TAG 被拒绝。

`wide_ingestion.py` 按 `(wind_farm_id, schema_fingerprint)` 合批，对设备内重复时间戳按 Kafka offset 取最后值；不同设备组合法共享一次 execute，任何失败都等待在途操作并禁止整批 offset 前进。

### B2. 真实异常与 UNKNOWN

覆盖 prepare/bind/execute 失败、execute 超时、真实部分成功、连接断开、连接返回池失败、statement 关闭失败、Kafka commit 失败、进程重启与 rebalance 的现有及新增测试。真实冲突请求返回 `QueryError [0x061D] Table already exists in other stables`，合法子表可能已写入，所以异常后按 UNKNOWN 隔离，不能把整批当作完全未写入。

前轮动态 STMT2 分阶段数据仍是独立原生参考：300/2 workers 最高约 16,050 行/s，prepare/bind/execute P50/P95/P99、CPU/RSS 与回读见前轮 `stmt2_stage_metrics.json`。该原生数字不被当成本轮生产容量。

容量进程当前没有导出逐次 prepare/bind/execute 的原始计数和延迟；本轮生产容量中的这些指标为 **NOT_RUN**，没有拿原生分位数冒充生产链路。

## 阶段 C：Storage 单实例容量

所有工况均为真实 Kafka → 单 Storage → TDengine、500 点完整设备行、每设备 600 周期、writer_count=2。结果：

| 设备 | 上轮端到端行/s | 本轮发布行/s | 本轮端到端行/s | 排空(s) | 裁决 |
|---|---:|---:|---:|---:|---|
| 30 | 277.985 | 299.982 | **299.927** | 0.011 | **PASS**：18000 行完整 |
| 100 | 492.369 | 999.914 | **954.699** | 2.842 | **PASS**：60000 行完整 |
| 300 | 191.744 | 2117.043 | **963.429** | 101.808 | **BLOCKED**：低于 2850 |

- 30 台：Kafka ACK P95/P99=6.921/7.262ms；Storage 平均/峰值 CPU=54.44%/110.84% 单核，VmHWM=137.87MiB。
- 100 台：ACK P95/P99=7.246/26.793ms；CPU=85.63%/110.89%，VmHWM=211.90MiB。
- 300 台：ACK P95/P99=37.546/43.375ms；CPU=85.27%/108.82%，VmHWM=317.52MiB。180000 行最终完整且 300 台逐表 count=600，首末行各 500 值/质量一致，但持续容量不达标。
- 100 台最终吞吐较上轮提升约 94%，300 台约 5 倍；这证明多表合批/复用确有实质收益，但不能隐藏 300 台失败。1000 台 **NOT_RUN**。

Kafka ACK 分位数不是 TDengine 写入延迟。各设备完整计数、采样身份、CPU/RSS、发布与端到端吞吐均保存在容量 JSON/JSONL。生产容量原始 prepare/bind/execute 分布和 writer queue 内部时延未导出，标记 NOT_RUN，未估算。

## 阶段 D：Sink 容量

### 600 秒正式工况

30 台 × 10Hz × 500 点、单 Sink、单 partition、并发 16、linger 10ms。`sink_600_c16_l10.xml` 为 **FAIL**：

| 指标 | 本轮 | 上轮 |
|---|---:|---:|
| 稳态 Kafka 确认行/s | **210.27** | 237.12 |
| 积压斜率行/s | **+89.742** | +62.900 |
| drained / zero_loss / identity_closed | false / false / false | false / false / false |
| 审计独立身份 / 重复身份 | 157726 / 0 | 173231 / 0 |
| Kafka payload MB/s | 2.150 | 2.369 |
| CPU / RSS 峰值 | 90.5% / 249.1MiB | 89.3% / 250.7MiB |
| gRPC P95/P99(ms) | 5.196/5.883 | 4.575/5.522 |
| 写入窗口 P99 最大值(ms) | 116.473 | 90.602 |

分项均值（ms，非全局分位数）：NATS fetch 31.865、JSON encode 0.459、Kafka write 101.760、submit 21.446、confirm 38.336、global/physical/native quota 11.007/9.399/69.510、JetStream ACK 2.951。编码热路径明显缩短，但 fetch、写入、native quota 和 broker confirm 主导持续容量；不能仅凭均值定位唯一瓶颈，需结合并发、队列、CPU 和 ACK 共同整改。

`audit_stop_error=CancelledError` 被明确记录并导致 identity_closed=false，没有吞掉异常后宣称通过。正常关闭语义仍未取得合格证据。

### 四 partition 短对照

60 秒、同参数、4 partitions：253.08 行/s，高于本轮单分区 210.27，但仍低于 300。18000 行全部确认、身份闭合、零重复、drained=true，且短对照 `audit_stop_error=""`。fetch/write/submit/confirm 均值为 26.101/85.041/29.577/26.538ms，global/physical/native quota 为 9.236/8.794/57.925ms。多 partition 有改善但不构成通过。

RAW/PROTOCOL 短烟测 4/4，业务编码路径与身份/Quality 行为未改变；BUSINESS 查询心跳保护由现有测试覆盖，没有删除有效断言。

## 阶段 E：跨模块 E2E

`cross_module_e2e.xml` 为 **11/11**：Data Generator → Industrial Gateway → Collector → NATS/JetStream → Sink → Kafka → Storage → TDengine，覆盖 FC03/FC04、RAW/PROTOCOL/BUSINESS、历史 Schema、Kafka 重放、Storage 重启、配置发布/激活、部分失败、查询聚合分页导出和完整回读。

生产备份已在同一源码线取得真实恢复通过；DCEMS 独立专题未修改，其综合控制闭环仍为 NOT_RUN。

功能链路通过不等于容量通过。由于 Sink 600 秒和 Storage 300 台失败，CROSS_MODULE_E2E 必须为 **BLOCKED**。

## 未关闭问题与修复提交

| 优先级 / 分类 | 问题 | 状态与移交 |
|---|---|---|
| P1 PERFORMANCE_BLOCKED | Sink 600 秒 210.27 行/s且积压，300 台 Storage 963.43 行/s低于目标 | OPEN_CHATGPT：继续生产写入、配额、broker ACK 与调度整改。 |
| P2 CLEANUP | 长测 consumer.stop 再次出现 CancelledError，identity_closed=false | OPEN_CHATGPT：评估生产关闭语义，不得隐藏异常。 |
| P2 OBSERVABILITY | 容量进程缺逐次 prepare/bind/execute 与 writer queue 原始指标 | NOT_RUN：补充受控可观测性后复测，不估算。 |
| QUALIFICATION | 1000 台、三等级同时容量、真实 PLC、完整 DCEMS 控制闭环 | NOT_RUN。 |

已提交并推送：

- `7ca2d4816`：STMT2 callable/cache 类型与格式门禁修复。
- `d8dc54641`：真实 Backend 多设备五 TAG、缓存/LRU、连接废弃、UNKNOWN 和 offset 回归。

未发现可据当前证据确认的 P0。本轮未修改独立 DCEMS 专题，也未增加应用实例。

**最终结论：BLOCKED。** STMT2 多表与复用、30/100 台 Storage 容量和跨模块功能已验证通过，但 Sink 600 秒与 Storage 300 台强制容量、关闭异常和生产可观测性缺口尚未关闭，不能建议 READY_FOR_PRODUCTION_REVIEW。

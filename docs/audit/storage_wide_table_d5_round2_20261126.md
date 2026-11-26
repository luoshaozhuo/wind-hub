# Storage 宽表 D5 第二轮独立集成整改与集中复测（2026-11-26）

最终裁决：**BLOCKED**。v2 契约迁移、模块测试和主要真实链路已收口，但 Farm 全设备查询语义、原生多表 STMT2 及生产备份资格仍未关闭；容量结果见下文，不能以局部通过认定生产 READY。本轮未创建 PR，未合并 main。

## Git、基线与证据

- 分支：`feat/storage-wide-table-completion-20261125`。
- fetch 后本地/远端起始 HEAD 均为 `d10c5fc19eefaa85c9b40cba36f25baed92454bc`，工作树干净。
- 契约及测试迁移：`be4cf510d`；500 点容量 Fixture：`f47f68c3992937b43ee0947c3fb68e8b20a849f2`；完整描述符：`1ff9f30c4`；长测保留配置：`6a88dd991`；审计与时间戳/容量修复：`8f8de1afb`、`e9fcd55b9`、`7083b7c95`、`76ed5edff`。最终源码/测试 HEAD：`76ed5edff1a03858a1b6427e6a09e1babcc4a14c`。
- 远端推送通过上述源码提交；报告及证据随后独立提交，最终 HEAD 以交付答复和 Git 元数据为准。
- 首轮报告：`storage_wide_table_d5_independent_20261126.md`。本轮沿用其缺陷台账，不将不同 SHA 的重复回归相加为独立测试总数。

原始日志、JUnit、采样 JSON/JSONL、命令和派生结果位于 [evidence](storage_wide_table_d5_round2_20261126_evidence/)。主要命令见 [commands.txt](storage_wide_table_d5_round2_20261126_evidence/commands.txt)，环境版本见 `environment.json`，测试统计见 `test_results.json`，全部文件摘要见 `sha256_manifest.json`。skip 不计为通过；失败的首轮执行保留，不由后续定向通过改写。

## 第一阶段：正式 protobuf 契约

以 Contracts v1_48 §14/14.1 和 Storage v1_14 为准，完成：

1. `storage_service.proto` 迁至 `windhub.storage.v2`，保持 QuerySamples、QueryAggregates、CreateExport/GetExportTask、CreateBackup/GetBackupTask、GetStorageStats、ListBlockedPartitions、RetryBlockedPartition 九项 RPC。
2. 删除 QueryWideRows、StorageSample、StorageValueKind、WidePoint*/WideDeviceRow、wide_filter 和旧窄表结果字段；不部署 v1 兼容服务。
3. 同步 proto、四个 Python 生成物、类型声明、服务端、CLI、权限及调用方。CommonService `contract_version` 同步为 v2。
4. 执行 `python3 scripts/generate_rpc.py` 和 `--check`，描述符测试固定全部消息字段号、类型、repeated、optional、oneof 与 NULL 语义。
5. 查询、聚合、导出先认证再校验过滤器，避免匿名无效请求绕过 UNAUTHENTICATED；查询和导出增加请求/返回 Schema 指纹一致性防护。

最终扫描仅在“禁止旧接口”的负向测试中出现旧名称，生产源码、配置和脚本无旧 RPC/包名残留，见 `legacy_references_final.log`。

## 第二阶段：静态检查与测试迁移

| 检查 | 结果 | 证据 |
|---|---|---|
| Ruff check | PASS | final_delivery_ruff.log |
| Ruff format --check | PASS，1130 文件 | final_delivery_format.log |
| mypy src | PASS，494 文件 | final_delivery_mypy.log |
| import-linter | PASS，22 kept / 0 broken | final_delivery_imports.log |
| RPC 生成一致性 | PASS | final_delivery_protobuf.log |
| pytest 全仓收集 | PASS，4200 项、0 错误 | final_delivery_collection.log |

迁移覆盖 Collector、Sink、Storage、CLI、Contracts 与八服务 E2E。可靠设备批不再拆成逐点记录；Kafka 一行对应完整设备行；CLI 使用正式 `storage samples/aggregates/export`，不保留 `wide-*` 别名。没有恢复旧生产接口、删除有效测试、降低断言或新增 skip/xfail。

局部明确修复包括：Kafka 入库模板升级到 schema 2 和设备行身份；Collector Fixture 使用 Service schema 6；Storage 查询/导出 Schema 防错；重放重启后的有界可见性等待。结构性 Farm 查询和持久化问题未自行重构。

## 模块测试

| 范围 | tests | failed | errors | skipped | 证据 |
|---|---:|---:|---:|---:|---|
| Contracts / Core 最终整批 | 667 | 0 | 0 | 0 | contracts_core_delivery.xml |
| 完整 v2 描述符专项 | 5 | 0 | 0 | 0 | full_descriptor_contract.xml |
| Configuration | 487 | 0 | 0 | 2 | configuration.xml |
| Collector unit/component/integration | 120 | 0 | 0 | 0 | collector_final.xml |
| Sink unit/component | 425 | 0 | 0 | 0 | sink_final.xml |
| Storage unit/component（时间戳修复后） | 251 | 0 | 0 | 0 | storage_after_timestamp.xml |
| CLI | 287 | 0 | 0 | 13 | cli_final.xml |
| 项目进程内 performance | 7 | 0 | 0 | 0 | performance.xml |
| 文件版本命名 | 3 | 0 | 0 | 0 | versionless.xml |

Configuration 两个 skip 是实时 NATS 广播连接不可用；CLI 十三个 skip 是 containerd/iSulad 不可用。原始原因见 `skipped_tests.json`，这些项目均为 NOT_RUN，不计为通过。

## 第三阶段：功能及真实可靠性

| 验证 | 结果与边界 | 证据 |
|---|---|---|
| Kafka commit 超时、重放、broker 重启、组交接 | PASS，1 项 | real_kafka.xml |
| Storage CLI 发布、激活、重启及真实进程路径 | PASS，1 项 | real_storage_cli.xml |
| FC03 RAW、PROTOCOL、缺字节 DLQ | PASS，4 项；真实 Modbus TCP、JetStream、Sink、Kafka | wire_acceptance.xml |
| 严格 CLI 五服务数据/命令/重启 | PASS，1 项 | cli_strict_final.xml |
| FC04 与八服务整批 | 首轮 5 passed / 1 failed；smoke 修复后单独 PASS，因此六项最终均通过 | eight_service.xml、eight_service_smoke_replay.xml |
| Kafka v3 → Storage → TDengine 三层 | PASS：WTG/BESS 两设备、两 Schema、RAW/PROTOCOL/BUSINESS，SQL 窗口闭合 4 行 | history_operations_final.xml |
| QuerySamples | PASS：多设备、多 Schema、完整列/值/Quality 位图及跨页游标 | 同上 |
| QueryAggregates | PASS：COUNT/MIN/MAX/AVG、GOOD+NULL、显式 BAD、good/missing/valid_count；COUNT=0 与空 oneof | 同上 |
| JSON / CSV 导出 | PASS：JSON groups、CSV ZIP 分 Schema、字节数与 SHA-256 回读 | 同上 |
| 生产备份 RPC | NOT_RUN：配置禁用且宿主无 taosdump；FAILED_PRECONDITION fail-closed 已验证，不是备份通过 | 同上 |
| 原生备份恢复 | PASS（仅工具探针）：PTY 下 taosdump 备份、删库、恢复，500 点完整行逐值一致；`production_storage_rpc=false` | native_backup_restore.json |
| TDengine 原生探针及 WAL 重启 | 24 passed / 2 failed / 31 skipped；WAL 重启通过，两个多表 prepare 失败未关闭 | native_tdengine.xml |
| TDengine 单表原生基线 | PASS：18000 行、900 万点全量回读；1695.67 行/s、847837.10 点/s、6.995 MB/s；批 P95/P99=36.06/51.20 ms | native_single_table_baseline.xml |
| Kafka JSON v3 进程内基线 | 正式 gate：Sink 编码 4403.33 行/s；Storage 校验 4061.22 行/s；均为 500 点/行，无 broker/TDengine I/O | performance.xml；独立初跑见 json_v3_microbenchmark.xml |

BUSINESS 证据是合法 business_point_id 的真实 Kafka v3 → Storage → TDengine 支链，不冒充 Sink BUSINESS 转换或完整 DCEMS 业务闭环。现场 PLC、硬件写入和 iSulad/containerd 专项未执行。taosBenchmark 交叉校验、TD09 实际压缩率、完整 Collector→Sink→Storage 容量及三等级同时容量均为 NOT_RUN；单支链测量不能替代这些资格。

## 第四阶段：独立性能与容量

### JetStream 保留配置中止记录

首次 8 GiB 执行在 429.15 秒因 `maximum bytes exceeded` 提前中止，600 秒发布、排空及身份审计均未完成，归类 `ABORTED_INFRASTRUCTURE_JETSTREAM_MAX_BYTES`。405 个采样点内发布确认均速 300.02 行/s、Kafka 确认均速 212.42 行/s，末样本未确认 36365 行；这些只用于诊断，未确认行不直接认定为永久丢失。见 `capacity_30_baseline.log`、`baseline_8g_derived.json`。

随后专用流字节上限调整为 32 GiB，max_age=900 秒覆盖 600 秒发布与 120 秒排空，重跑正式基线。

### 正式 30 台 × 10 Hz Sink 基线

**FAIL**。`capacity_30_baseline_32g.xml` 完整执行 838.19 秒：600 秒发布、120 秒排空及全量审计。配置为 30 台、500 点/行、300 行/s、单 Sink、单 partition、并发 4、linger 0 ms。

| 指标 | 实测 |
|---|---:|
| 发布 / Kafka 确认行 | 180000 / 158890 |
| 发布 / Kafka 确认点 | 90000000 / 79445000 |
| 稳态发布 / Kafka 确认 | 300.00 / 218.29 行/s |
| 稳态积压斜率 | +81.714 行/s |
| 实际 Kafka payload | 1665.014 MB，端到端 2.312 MB/s |
| drained / zero_loss / identity_closed | false / false / false |
| DLQ / gRPC 失败 / metrics 失败 | 0 / 0 / 0 |
| gRPC 探针 P95 / P99 | 4.194 / 4.913 ms |
| 写入窗口 P99 最大值 | 67.641 ms |
| Sink CPU / RSS 峰值 | 92.6% 单核 / 248.9 MiB |
| 顺序与分区抽样 | 30 台、239 行，无违规；Kafka 全量 158890 条无重复 |

确认不足和持续积压独立导致 FAIL，终局采样时未确认 21110 行不直接认定为永久丢失。终局采样后 Sink 又继续确认约 10000 行，使 final_offset 与后续审计错位，故 `identity_closed=false` 不能作为精确缺失数证明；`8f8de1afb` 增加 partition 起始 offset 过滤，`7083b7c95` 又在最终测量前受控停止 Sink。原始结果未篡改，重跑矩阵使用两项修正。

瓶颈分项（均值，非全局 P95）：Reliable encode 1.256 ms、write 124.873 ms；Kafka submit 7.173 ms、confirm 8.147 ms；物理 quota 103.187 ms、native quota 17.853 ms。物理 quota 和 write 是主要累计耗时项，不能由这些均值推算端到端 P95/P99。

### Storage 30 台 × 10 Hz、500 点完整行

**FAIL（最终断言复测，数据完整但端到端未达标）**。`storage_wide_30/final/capacity.xml` 使用一个 Storage 进程和独立 500 点 Schema，600 周期/设备共 18000 行、900 万点。Kafka 直注不经过 JetStream，不冒充完整 Collector→Sink 容量。

| 指标 | 最终复测 |
|---|---:|
| 发布行 / 点 | 18000 / 9000000，全部设备逐设备 count=600 |
| 发布速率 | 299.981 行/s |
| TDengine 持久化端到端 | **278.453 行/s**，低于 300×95% 门槛 |
| 排空 / 阻断分区 | 4.639 s / 0 |
| Storage 平均 CPU / 峰值区间 CPU / VmHWM | 55.06% / 111.75% 单核 / 120.53 MiB |
| 首、末行逐值验证 | 各 500 个 float32 值及 500 个 GOOD 质量位 |

较早一次运行为发布 299.897 行/s、端到端 290.014 行/s、排空 2.045 秒（Kafka ACK P95/P99=6.996/7.411 ms），保留在 `storage_wide_30/full/` 作为参考，但不选择性取高判定通过。

此前 600 周期执行在 RPC 回读阶段失败，原始值为 `2026-11-26 17:41:42.742 +08:00`；`e9fcd55b9` 修复毫秒时区空格解析后，短工况及最终运行的首末行 500 值/质量回读均恢复，但最终容量断言仍如上为 FAIL。初次失败日志和诊断均保留。

### Sink 并发 × 合批有界矩阵

16 个单元均使用 30 台、500 点/行、60 秒发布、单 partition，受控停止后全量审计。**全部单元 drained=true、identity_closed=true、无重复，但没有单元达到 300 行/s；因此没有合格参数组合，也不给出生产调参推荐。**

| 并发 / linger(ms) | 行/s | 点/s | MB/s | 积压斜率(行/s) | CPU%(单核) | RSS MiB | gRPC P95/P99(ms) |
|---|---:|---:|---:|---:|---:|---:|---:|
| 4 / 0 | 226.12 | 113058 | 1.776 | 73.960 | 69.6 | 247.6 | 3.834/3.939 |
| 4 / 1 | 253.85 | 126923 | 1.971 | 46.040 | 76.2 | 246.9 | 4.814/4.914 |
| 4 / 5 | 236.04 | 118019 | 1.855 | 63.915 | 70.2 | 246.7 | 3.374/3.762 |
| 4 / 10 | 246.86 | 123431 | 1.924 | 53.063 | 71.1 | 246.6 | 3.422/3.705 |
| 8 / 0 | 250.30 | 125149 | 1.951 | 49.557 | 77.4 | 247.4 | 3.433/3.931 |
| 8 / 1 | 263.09 | 131543 | 2.031 | 36.824 | 79.6 | 247.8 | 3.918/4.199 |
| 8 / 5 | 263.51 | 131756 | 2.031 | 36.684 | 78.9 | 247.3 | 3.672/3.879 |
| 8 / 10 | 260.02 | 130009 | 2.009 | 39.972 | 77.1 | 247.3 | 3.909/3.983 |
| 16 / 0 | 256.91 | 128457 | 2.015 | 43.126 | 78.7 | 248.1 | 3.423/3.619 |
| 16 / 1 | 270.68 | 135338 | 2.071 | 28.913 | 81.4 | 247.3 | 4.069/4.302 |
| 16 / 5 | 268.05 | 134024 | 2.057 | 32.041 | 80.2 | 247.6 | 3.674/3.827 |
| 16 / 10 | 269.14 | 134572 | 2.047 | 31.003 | 79.5 | 247.7 | 4.209/4.943 |
| 32 / 0 | 261.61 | 130805 | 2.019 | 38.358 | 79.6 | 248.3 | 4.221/4.812 |
| 32 / 1 | 271.96 | 135978 | 2.071 | 28.003 | 80.2 | 248.2 | 3.453/4.599 |
| 32 / 5 | 269.48 | 134742 | 2.064 | 30.271 | 79.5 | 248.4 | 3.974/4.316 |
| 32 / 10 | 257.63 | 128815 | 1.999 | 42.211 | 74.5 | 247.4 | 3.679/4.005 |

最高实测为 32/1 的 271.96 行/s，仍只有目标的 90.65%，且稳态积压继续增长。gRPC 分位数只是探针，不是写入或端到端分位数。`matrix_summary.json`、`capacity_summary.json` 和各单元原始 JSONL 可复核。

### Storage 100 台梯度

**FAIL（最终持久化完成，但稳态不达标）**。`storage_wide_100/capacity_final.xml` 的最终断言复测中，60000 行、3000 万点全部落库且逐设备 count=600、无阻断；发布为 999.955 行/s，TDengine 端到端仅 292.067 行/s，低于 1000×95% 门槛，排空 145.430 秒。**最终补齐不等于稳态 1000 行/s 能力。** Storage 平均/峰值 CPU=47.01%/111.88% 单核，VmHWM=126.28 MiB；首末行各 500 值/质量通过。Kafka ACK P95/P99=7.159/24.600 ms（非 TDengine 写延迟）。先前一次运行为 309.463 行/s、排空 133.878 秒，保留但不作为合格依据。原始 XML/JSONL 均可复核。

100 台 Fixture 初次两次在无关 Commander 启动阶段失败，改为只启动真实 Configuration 和 Storage 后才进入容量测量；容量路径未改。30 台使用完整八服务，100/300 台使用这两个必需进程，资源数字不直接横向等同。

### Storage 300 台梯度与极限边界

**FAIL**。`storage_wide_300/capacity.xml` 与 971 个逐秒采样及 `derived_result.json` 显示：发布 2537.856 行/s（1268927.93 点/s、44.675 MB/s），低于 3000 行/s 目标；900 秒排空上限后仅 166090/180000 行，90/300 台完成各自 600 行。端到端 170.798 行/s（85398.87 点/s），未确认 13910 行，首末行资格 NOT_RUN。

末段 Storage 平均逐秒 CPU 约 41.71% 单核、VmHWM 136.27 MiB。早期 Fixture 未序列化最终 Kafka ACK 分位数和阻断列表，二者均为 NOT_RUN；未确认行不自动认定为永久丢失。300 台已同时违反发布速率、持久化完整性和稳态要求，因此 **1000 台极限探索 NOT_RUN**，没有通过多实例或调低目标制造通过。

## 未关闭缺陷与移交

| ID / 严重度 | 状态与影响 | 证据 / 负责人 |
|---|---|---|
| D5R2-01 P1 | Farm 全设备或包含未建表设备的 grouped 查询返回 FAILED_PRECONDITION。契约要求空 family 跳过；必须区分“表不存在”和“存在但无所有权”，不能捕获全部冲突后继续。交 ChatGPT 做结构性修复。 | eight_service.log、history_operations.log |
| D5R2-02 P2 | TDengine 3.3.6.13 / taos-ws-py 0.7.1 在 `INSERT INTO ? VALUES (...)` prepare 返回 0x022B；多表复用与部分成功探针未进入执行阶段。单表基线不冒充多表通过。交 ChatGPT。 | native_tdengine.log |
| D5R2-03 资格阻断 | 生产备份禁用、宿主无 taosdump、无 TTY 密码输入失败；原生 PTY 探针成功不等于生产 RPC 可用。交 ChatGPT 明确受支持的非交互认证和部署方式。 | native_backup_restore*.log、native_backup_restore.json |
| D5R2-04 资格阻断 | Sink 600 秒基线仅 218.29 行/s且未排空；16 单元最高 271.96 行/s。Storage 30/100/300 台最终端到端分别 278.45/292.07/170.80 行/s，均未达到各自目标的 95%，300 台仅 166090/180000 行。完整 Collector→Sink→Storage 容量与最高工况长稳均未取得资格。生产优化交 ChatGPT，测试/复验由 Claude 继续。 | capacity_30_baseline_32g.xml、matrix_summary.json、storage_wide_30/final/capacity.xml、storage_wide_100/capacity_final.xml、storage_wide_300/derived_result.json |

没有发现可据当前证据确认的 P0。未关闭项及 NOT_RUN 不允许给出 PASS 或 PASS_WITH_LIMITATIONS，也不允许宣称生产 READY。

## 已提交整改与最终结论

- `be4cf510d`：v2 九 RPC、生成物、CLI/权限、Schema 防错、跨模块测试与真实 E2E 迁移。
- `f47f68c39`：独立 500 点 Storage 容量 Fixture 和全量身份/资源验证。
- `1ff9f30c4`：全部 v2 描述符字段、optional、oneof 和 NULL 契约。
- `6a88dd991`：600 秒 JetStream 保留容量修正。
- `8f8de1afb`：按 partition 起始 offset 限定容量身份审计。
- `e9fcd55b9`：修复 500 列回读中的 TDengine 毫秒时区字符串解析；Storage 251 项全绿，短工况首末行 500 值/质量通过。
- `7083b7c95`：终局测量前受控停止 Sink，消除审计期间继续确认造成的计数竞态。
- `76ed5edff`：Kafka 直注容量仅启动 Configuration+Storage，增加端到端速率断言和部分持久化证据路径。

**最终裁决：BLOCKED。** 契约和主要真实链路整改已通过，但不能据此替代剩余生产语义、原生多表、备份及容量资格。

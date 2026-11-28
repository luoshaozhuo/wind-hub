# wind-hub D4 跨模块功能与可靠性集中独立验收报告（2026-11-28）

> 面向 ChatGPT 的 D4 裁决与 D5 准入评审。结论按当前 SHA 的真实运行证据，不以历史结果、Mock 或静态检查替代真实 E2E。

## 1. Git 基线、环境与总体裁决

| 项目 | 值 |
|---|---|
| 起始 HEAD | `a72a0e3e2ebcf4441576b7948edb88e7055e6fb0` |
| D3 冻结生产源码 SHA | `a72a0e3e2ebcf4441576b7948edb88e7055e6fb0`（本轮 `src/` 零改动） |
| 最终测试 SHA | `1f4d92078d21b7b248a452e7dc4f2181760a57f1` |
| 最终 HEAD | 报告提交后以 Git 元数据及交付摘要为准 |
| 分支 | `feat/storage-wide-table-completion-20261125` |

环境：Python 3.11.16、wind-hub Conda、Docker 29.2.1、NATS 2.10、Redis 7、Kafka 3.9.1、TDengine 3.3.6.13、aiokafka 0.14.0、gRPC 1.84.0。真实备份使用同版本 taosdump/libtaos。

起始本地/远端一致，工作树干净。未创建分支、PR，未合并 main或 force push；未修改生产源码、Proto、活动配置或容量标准。仓库未找到独立 `报告模板.md`，沿用 D2/D3 审计报告结构。

**总体裁决：`BLOCKED`。Collector 仍有 3 个稳定失败，其中诊断截止语义为明确生产缺陷；不能裁 D4_PASS 或 READY_FOR_D5_REVIEW。**

## 2. 最终 Gate

| Gate | Result | 依据 |
|---|---|---|
| GIT_BASELINE | **PASS** | D3 基线精确匹配；生产/活动配置零改动。 |
| STATIC_GATES | **PASS** | Ruff、mypy、import-linter、Proto、4351 项收集全绿。 |
| MODULE_FULL_REGRESSION | **FAIL** | Collector 163 passed / 3 failed；其余模块通过。 |
| RPC_CLI_COVERAGE | **PASS** | 全正式 RPC/CLI 表面矩阵、CLI 非 Docker 与严格真链通过；环境资格另列。 |
| CONFIGURATION_LIFECYCLE | **PASS** | 496 项全层通过，真实 NATS 广播补测 2/2。 |
| CONFIGURATION_IMMUTABILITY | **PASS** | 同版本异内容、闭包、精确选版和多根隔离通过。 |
| DATA_CHAIN_REAL_E2E | **PASS** | 八服务真实 CLI 全链、Generator truth、Storage/TDengine 回读。 |
| RAW_E2E | **PASS** | 真实 FC04 字节、Kafka v3、Storage payload_bytes、物理 bytes 一致；缺失 RAW DLQ 通过。 |
| PROTOCOL_E2E | **PASS** | 5 个独立采样时刻、值/质量/配置身份、重启后回读一致。 |
| BUSINESS_E2E | **PASS（仿真语义）** | 新增真实上游 BUSINESS 映射、Repository manifest、Storage/TDengine 回读；不宣称 OEM 批准。 |
| CONTROL_LOOP_E2E | **PASS** | 独立 Modbus 控制、APPLIED、UNKNOWN、重放、权限、lease/fencing 通过。 |
| STORAGE_CORRECTNESS | **PASS** | v3 宽表、三级数据、NULL/Quality、offset 覆盖、查询聚合导出备份通过。 |
| STORAGE_RECOVERY | **PASS** | Kafka broker 重启、CLI 配置激活、Storage 重启回读、taosdump 恢复通过。 |
| FAULT_INJECTION | **FAIL** | Redis/NATS/Kafka/TDengine 等真实故障场景大量通过，但 Collector overdue poll 稳定卡 RECOVERING。 |
| RESOURCE_LIFECYCLE | **FAIL** | Full-chain 资源树清理通过；Collector unsettled-I/O 用例失败，Commander 另有 NATS GC teardown 警告。 |
| FULL_CHAIN_E2E | **PASS** | 最终 SHA 全部 **58/58**，无 skip/xfail。 |
| D4 | **BLOCKED** | 必需模块与可靠性门禁未全绿。 |
| D5_ADMISSION | **BLOCKED** | 不进入 D5。 |
| PRODUCTION_READINESS | **BLOCKED** | 功能缺陷、D5 容量/硬件/长稳资格均未关闭。 |

## 3. 静态门禁与收集

最终测试 SHA 执行：

```bash
ruff check src tests scripts
ruff format --check src tests scripts
mypy src
lint-imports
python3 scripts/generate_rpc.py --check
pytest --collect-only -q
```

结果：Ruff check PASS；1139 文件 format PASS；mypy 492 files / 0 errors；import-linter 22 kept / 0 broken；Proto --check PASS；**4351 tests collected / 0 error**。日志：`static.log`、`collect_only.log`。

收集与 Sink 测试存在既有未注册 `nats/jetstream/capacity` marker warnings 和第三方 deprecation warnings；未通过忽略警告或修改断言语义消错。

## 4. 模块完整回归

### 4.1 非 Storage/Sink/Configuration/CLI 全层

统一执行各目录 Unit、Component、Contract、Integration、System，`-m 'not performance and not soak and not hardware'` 排除 D5：

| 模块 | 结果 | D5 deselected / 环境 skip |
|---|---|---|
| Contracts | 114 passed | 0 |
| Core | 26 passed | 0 |
| Collector | **163 passed / 3 failed** | 2 deselected |
| Commander | 378 passed | 4 deselected、1 现场窗口 skip |
| Industrial Gateway | 225 passed | 1 deselected |
| Data Generator | 187 passed | 16 deselected |
| 合计 | **1093 passed / 3 failed / 1 skipped / 0 xfailed** | 23 deselected |

原始日志在 `module_regression/`。最终 SHA 另跑其他模块单元/组件/契约联合 **739 passed**（`module_unit_component_final.log`）及 Collector 三项失败（`collector_remaining_production_failures.log`）。

### 4.2 CLI、Configuration、Sink、Storage

| 范围 | 最终结果 | 日志 |
|---|---|---|
| CLI 非 Docker 全命令/RPC | 293 passed、13 环境 skip、1 strict 项另跑 | `cli_non_docker.log` |
| 严格 CLI 五服务数据/控制/重启真链 | 1 passed | `cli_strict_real.log` |
| snapshot_max_bytes 边界 | 14 passed | `cli_snapshot_limit.log` |
| Configuration 全层 | 496 passed、2 NATS 环境 skip 后定向转绿 | `configuration_full.log` |
| 真实 NATS revision/Storage 广播 | 2 passed | `configuration_real_nats.log` |
| Storage unit/component | 297 passed | `storage_unit_component.log` |
| Storage 真实 Integration | 27 passed | `storage_integration_real.log` |
| Sink unit/component | 430 passed | `sink_unit_component.log` |
| Sink 真实 System | **38 passed** | `sink_system_final.log` |

CLI 13 skip：11+1 项 containerd 不可用、1 项 iSulad 不可用；均为环境 NOT_RUN，不计 PASS。所有最终执行组 0 xfail。

## 5. 真实数据链与三级数据

最终 SHA 八服务非容量集合：**8/8 PASS**，`eight_service_final.log`。

### 5.1 完整真实链

Data Generator → Gateway → Collector → NATS/JetStream → Sink → Kafka → Storage → TDengine。所有业务进程由正式 CLI 启停，中间件由隔离 R5 Docker 栈装配；Kafka Consumer 和 TDengine SQL 仅作独立观测，未直接写入替代上游。

- **PROTOCOL**：`test_eight_service_provenance_and_restart_readback` 选择 5 个不同采样时刻，以 DG JSONL 真值核对 Kafka 值/质量/配置身份，再核对 Storage 全部游标页面和 CLI 重启后回读。
- **RAW**：真实 FC04 响应字节经完整链；3 个样本独立关联 Generator truth，Kafka v3 raw hex、Storage `payload_bytes` 和 TDengine 物理 bytes 完全一致。
- **BUSINESS**：新增 `test_e2e_eight_service_business.py`，从真实 DG 上游产生 `sim_active_power`，Kafka data_level=business，Storage RPC 与 TDengine 稳定物理源列 `active_power` 回读同时间/值/质量。

BUSINESS Fixture 先由真实 Configuration 发布闭包并读取 `GetPointTableBundle.manifest_sha256`，再在临时目录发布 Sink 2.0.1 + Deployment 2.0.3 后继版本携带该明确测试批准 digest。仓库冻结配置未改；该证据只证明仿真映射和准入代码路径，不替代 OEM 物理语义批准。

`test_real_history_aggregate_and_export_operations` 直接写真实 Kafka，故只计 Storage 真实分段集成：RAW/PROTOCOL/BUSINESS、NULL/Quality、分页、COUNT/MIN/MAX/AVG、JSON/CSV ZIP/SHA-256 和 4 条物理行通过，不把它当 BUSINESS 上游 E2E。

## 6. 控制闭环

真实证据来自 Full-chain 58 项、八服务控制和 Commander/Gateway/DG 系统测试：

- 独立 AsyncModbusTcpClient 通过 FC06/FC16 进入 Gateway→Commander→Gateway→DG；查询同 caller 的权威结果和 DG `GetInputExecution`，只有 APPLIED/effective_step>0 才成功。
- 正常 AGC/AVC/FFR、设备状态变化、APPLIED、同 request_id 幂等、异内容拒绝通过。
- Commander 停止/崩溃、确认超时、断连为 UNKNOWN，不以新身份盲重试；非法/未授权请求零副作用。
- generation fencing、Gateway lease 过期/RENEW/UPDATE、Sink target 竞争与旧 token fencing 通过。
- 八服务四出口独立读回和 DG 物理步证据通过，未引入 third_party/dc-ems 正式实现。

真实 PLC、IEC104/ADS OEM 控制资格为 D5/硬件 NOT_RUN，不由 Modbus 模拟客户端替代。

## 7. Configuration 与 Storage

### 7.1 Configuration

全量发布、Deployment 精确选版、Service→Instance→Template 闭包、target/active、真实 NATS 通知、拉取/周期对账、RESTART_REQUIRED、旧 active 保留、重启恢复、不可变同版本异内容拒绝和多根隔离通过。

活动根保持 prod-a 2.0.2 / 29 YAML、sim-a 2.0.1 / 23、e2e-final 2.0.2 / 35；本轮活动配置和 configs 零修改，D3 已核验 87/87 Blob 一致，本轮真实发布套件重新通过。

### 7.2 Storage 正确性与恢复

- Kafka JSON v3 严格解析、设备级宽表、RAW/PROTOCOL/BUSINESS、不同 Schema 物理列、TDengine 类型、NULL/质量位图、多设备混合批次通过。
- 同键较大 offset 完整覆盖（含 NULL）、UNKNOWN 不提交 offset、真实部分成功 fencing、连接丢失缓存清理通过。
- 真实 Kafka broker stop/start、commit timeout replay、rebalance、CLI Storage 重启、配置候选激活通过。
- 查询、聚合、分页、导出、CreateBackup/GetBackupTask、真实 taosdump 重命名恢复及 2 行×500 点/Quality 回读通过。
- 备份 Fixture 已隔离 cwd/evidence，最终复测不在仓库遗留 dump_result 或覆盖旧审计日志。

## 8. 故障注入与资源生命周期

已通过的真实场景包括：Redis/NATS/Kafka 临时不可用、JetStream ACK 丢失与重投、Kafka broker 重启、Gateway 重启、网络连接中断、重复/乱序/重放、进程崩溃恢复、激活中断、RPC 超时/取消、重复启停、完整进程树与 Socket/连接清理。

TDengine 覆盖真实 execute timeout、部分成功、连接丢失、缓存失效和 offset fencing；**生产 Storage 写入期间 TDengine 容器 stop/start 与 DML/commit 崩溃窗口组合测试未执行**，列 D5 延期，不把原生探针等同于该场景。

Commander 全套出现 11 条 NATS `_wait_for_msgs/_flusher` event-loop closed GC 警告；asyncio debug 下 component 139 项和关联真实 system 2 项单独复跑无警告，历史字面量 pending-task 未稳定复现。分类为 **UNCERTAIN 的套件 teardown/GC 时序问题**，不能声称资源生命周期完全无告警。最终无业务测试进程残留。

## 9. 剩余失败与 ChatGPT 责任

### D4-COL-01：明确生产诊断截止缺陷（P1）

- 复现：`test_uc05_s03_expired_deadline_marks_later_stages_not_run`。
- 实际：1 ms 预算下后续 PROTOCOL stage=`FAIL`；契约要求 `NOT_RUN`。
- 位置：`src/collector/application/diagnostics.py:142-161`。`submit_health()` 返回后直接按 healthy=false 分类，未在健康响应后统一检查 deadline；其他阶段也缺统一入口截止防护。
- 最小建议：由 ChatGPT 明确各阶段入口和响应后的 deadline 优先级，已过期阶段不得产生新的 FAIL；补真实与单元回归。本轮未改生产。

### D4-COL-02： overdue poll 恢复未定因（P1 / UNKNOWN）

- 复现：`test_uc03_s02_overdue_poll_increments_skipped_slots`。
- 实际：延迟代理场景 30 秒仍为 RECOVERING，未进入 RUNNING/skipped-slots 断言，连续多轮及最终 SHA 复现。
- 需诊断采集恢复状态机或延迟代理 Fixture 适配；不能通过延长等待、skip 或删除场景消错。

### D4-COL-03：unsettled I/O 契约不一致（P1 / 待接口裁决）

- 复现：`test_uc08_s03_unsettled_io_shutdown_exits_nonzero`。
- 期望：带 `TIMEOUT_UNCONFIRMED` 的 ReadPoints 响应；实际：gRPC `FAILED_PRECONDITION: device I/O is still unsettled`。
- 当前异常来自 in-flight I/O 重叠防护。需 ChatGPT 裁定正式错误契约，再同步实现或测试；本轮未放宽断言。

以上 3 项在最终测试 SHA 为 **3 failed**，见 `collector_remaining_production_failures.log`。

## 10. 测试与 Fixture 整改

提交 `1f4d92078` 共 27 个测试文件，生产源码零改动：

- Collector：SampleBatch v4、reliable 无 max_staleness、同步轮询 Fixture。
- Sink：共享 v4 批次构造、生产 WideRecord 严格 v3 投影、真实设备宽行计数、生产 Kafka key、DLQ/replay/config restart Fixture。
- Full-chain：Kafka v3 解码、JetStream DeliverPolicy.NEW、GW02 task 解析、异步 PV/BESS Golden 按各输出自身 sample_time/value 关联 DG truth。
- 八服务：当前 CLI 1.1.9、Storage 2.0.1、正式命名查询参数、新增 BUSINESS 真链。
- Storage 备份：tmp_path cwd/evidence 隔离，禁止测试污染仓库。

未删除场景、未增加 skip/xfail、未放宽容差；Golden 改后四出口仍各自满足 1 秒来源窗口和原有数值精度。

## 11. Full-chain 与 NOT_RUN

- 首轮 58：46 passed / 12 failed。
- 修复后：57 passed / 1 failed，Golden 定向 1 passed。
- **最终 SHA 联合：58 passed / 0 failed / 0 skip / 0 xfail**，`full_chain_final.log/xml`。

未执行/延期：真实 PLC/硬件、netem、性能/容量、soak/长稳、Storage 容量发布器与原生 opt-in 容器重启探针、八服务容量文件、Sink capacity/throughput、containerd/iSulad、Web/DCEMS 独立专项。全部 NOT_RUN，不计 PASS。

## 12. 最终结论

真实数据链、RAW/PROTOCOL/BUSINESS、控制闭环、Configuration、Storage、Sink、CLI/RPC 和 Full-chain 均已取得充分通过证据；但这些通过项不能抵消 Collector 的 3 个稳定失败和资源清理未决警告。

**最终总体裁决：`BLOCKED`。D4≠PASS，D5_ADMISSION=BLOCKED，PRODUCTION_READINESS=BLOCKED。** ChatGPT 应先关闭 D4-COL-01～03，并重跑 Collector 全层、故障/资源套件和最终 SHA 回归后再评审 D5。

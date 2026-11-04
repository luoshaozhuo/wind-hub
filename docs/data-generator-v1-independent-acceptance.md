# Data Generator v1 首次集中独立验收报告

- 验收日期：2026-11-03
- 仓库：`luoshaozhuo/wind-hub`
- 分支：`feat/data-generator-v1`
- 验收基线 HEAD：`ac077124ec5880a00a04cd9a88c3e84a47f0fbcd`
- 验收结束时代码 HEAD：见 §12（本报告随测试代码一并提交）
- 设计基线：`docs/design/Data_Generator_软件设计_v1_9.md`（含 §13 测试分层、§14.2 追溯矩阵）
- 验收人：Claude Code（独立验收；生产代码本轮零改动）

## 1. 验收对象与范围

对 Data Generator v1 四阶段成果做首次集中独立验收，链条为：环境核实 → 静态检查 → 必要小修 →
测试开发完善 → Unit / Component / Integration / System 四层真实执行 → 可靠性与性能实测 →
回归核查 → 报告 → 提交推送。

边界（任务书约束，逐项遵守）：

- 禁止以源码阅读、Mock 或推测代替真实执行证据；下文每项结论均给出真实执行命令或测试节点。
- 不因实现中存在某种行为就反向认定该行为符合设计；实测语义与设计/直觉不符时单列为发现项。
- Integration 层全部使用真实 gRPC Server、真实 Industrial Gateway
  （`ListVariables`/`UpdateVariables`/`ReadVariables` 等正式 7 RPC）、真实 CSV+SQLite、
  真实配置事务与重启恢复、真实 IP 白名单；未以旧接口或测试替身替代正式 `UpdateVariables`。
- System 层按设计 §14.2 表 14-2 逐标识落实（见 §6），不是按现有测试数量反推覆盖。
- 本轮未修改 `src/data_generator/` 任何生产代码；所有提交均为测试与验收工具代码。

## 2. 环境与验收方法

| 项 | 值 |
|---|---|
| 环境检查 | `python3 scripts/dev.py env` → `RESULT: PASS`（conda `wind-hub`） |
| Python | 3.14.7 |
| 平台 | Linux-6.18.33.2-microsoft-standard-WSL2-x86_64（WSL2，glibc 2.43） |
| CPU / 内存 | 16 逻辑核 / 7.35 GiB（/proc/meminfo MemTotal 7712416 kB） |
| grpcio | 1.84.0 |
| 测试框架 | pytest + pytest-asyncio（auto mode），`PYTHONPATH=src`，ruff line-length=100 |
| 故障注入方式 | 与生产同一份冻结 proto 实现的故障 Gateway Servicer（组件层）、独立子进程 + 真实/故障 Gateway（系统层） |

方法要点：

1. 所有测试真实执行；故障场景以真实 gRPC Servicer 挂起/拒绝/缺 ACK 注入，不 Mock 生产代码。
2. System 层 Data Generator 一律以**独立子进程**运行（`python -m data_generator.main`），
   通过真实 gRPC 通道驱动，再经真实 Industrial Gateway 读回验证。
3. 性能数据由一次性实测脚本 `tests/data_generator/performance/measure_acceptance.py` 产出
   （非门检用例），命令、负载、样本量与原始输出见 §8。

## 3. 静态检查（真实执行结果）

| 检查 | 命令 | 结果 |
|---|---|---|
| Ruff lint | `ruff check src/data_generator tests/data_generator` | **All checks passed!** |
| Ruff format | `ruff format --check src tests/data_generator` | **329 files already formatted** |
| mypy（生产） | `mypy src/data_generator` | **Success: no issues found in 19 source files**（strict） |
| mypy（测试） | `MYPYPATH=src mypy tests/data_generator` | **Success: no issues found in 27 source files** |
| Import 边界 | `lint-imports` | **14 kept, 0 broken**（含"data generator 业务层不得依赖 wire Proto KEPT"） |

过程中对测试代码做过的静态整改均为真实类型修正（如 `Any` 标注、`float()` 包装、
`is not None` 收窄、proto 响应字段收窄、故障 Servicer 补齐正式契约 7 个 RPC 等），
未扩大忽略规则、未删除有效测试；生产代码无任何为过检而做的改动。

## 4. Proto Descriptor 与 15 个 Server RPC 逐项核验

Descriptor 以 `*_pb2` 服务描述符真实反射断言（`integration/test_proto_descriptor.py`，8 项全 PASS）：
DataGeneratorService 恰为 6 个 unary、CommonService 恰为 9 个 unary；
IndustrialGatewayService 恰为 7 个 unary；DG 侧更新入参为
`industrial_gateway.VariableUpdate{variable_id, common.ScalarValue value}`（无遗留 VariableValue 类型）；
`ScalarValue` 为 oneof `kind`；快照 scope 恰 4 值。

| # | RPC | Descriptor 断言 | 真实调用证据（同进程/独立进程） |
|---|---|---|---|
| 1 | DataGeneratorService.ListScenarios | ✅ | test_all_fifteen_rpcs_callable；system `test_contract_operations` |
| 2 | DataGeneratorService.GetDevice | ✅ | 同上 |
| 3 | DataGeneratorService.ListDevices | ✅ | 同上 |
| 4 | DataGeneratorService.StartGeneration | ✅ | 同上；全部 system 生命周期用例 |
| 5 | DataGeneratorService.StopGeneration | ✅ | 同上；停止/关闭/取消用例 |
| 6 | DataGeneratorService.GetGeneration | ✅ | 同上；状态轮询贯穿 system 层 |
| 7 | CommonService.GetServiceInfo | ✅ | test_all_fifteen_rpcs_callable；test_contract_operations |
| 8 | CommonService.GetCapabilities | ✅ | 同上（rpc_names 恰为 15） |
| 9 | CommonService.GetServiceStatus | ✅ | HEALTHY/DEGRADED 贯穿生命周期用例 |
| 10 | CommonService.GetConfigSnapshot | ✅ | DISK/CANDIDATE/RUNTIME 三 scope 真实往返 |
| 11 | CommonService.PushConfigSnapshot | ✅ | 冲突/全量/取消锁用例 |
| 12 | CommonService.ApplyConfigSnapshot | ✅ | 热生效/重启生效/失败回滚用例 |
| 13 | CommonService.GetMetrics | ✅ | metrics 全部用例与性能实测 |
| 14 | CommonService.GetEvents | ✅ | test_all_fifteen_rpcs_callable；test_contract_operations |
| 15 | CommonService.Diagnose | ✅ | diagnose 组件/系统用例 |

错误映射另由 `integration/test_grpc_server.py::test_grpc_error_mapping`、
system `test_contract_errors`（NOT_FOUND×2、INVALID_ARGUMENT×2、REVISION_CONFLICT 响应体错误码）
真实覆盖；`test_cancelled_push_releases_config_lock` 证明客户端取消后配置锁真实释放。

## 5. 四层测试结果（真实执行）

执行命令：`PYTHONPATH=src python -m pytest tests/data_generator -q`

| 层级 | 目录 | 结果 | 耗时 |
|---|---|---|---|
| Unit | `tests/data_generator/unit/` | **46 passed** | 0.12 s |
| Component | `tests/data_generator/component/` | **30 passed** | 23.91 s |
| Integration | `tests/data_generator/integration/` | **31 passed** | 7.58 s |
| System | `tests/data_generator/system/` | **39 passed, 1 xfailed** | 108.04 s |
| 合计 | — | **146 passed, 1 xfailed** | 138.16 s |

- 层级 Marker（unit/component/integration/system/contract）与 §13 表 13-1 全部专题/场景 Marker
  已登记于 `pyproject.toml`，层级 Marker 由 `tests/conftest.py` 按目录自动打标。
- 唯一 xfail 为 `test_conflicting_target_ownership`（strict=False），是 P1 问题的**真实失败证据**，
  不是跳过（见 §10 P1）。
- 物理目录与 §13 表 13-1 20 个目标文件名逐一对齐（unit/component/integration/system 四层齐备）。
- Integration 真实性举例：`test_dg_writer_uses_formal_update_rpc_end_to_end`（真实 Gateway 正式
  UpdateVariables + ReadVariables 读回）、`test_ip_allowlist_denies_unauthorized_writer`（真实白名单拒绝）、
  `test_history_reader.py` 11 项真实 CSV/索引用例、`test_cancelled_push_releases_config_lock`（真实配置事务）。

## 6. 设计 §14.2 追溯矩阵逐项落实（39 个测试标识）

设计 v1_9 §14.2 表 14-2 实际枚举 **39 行**（任务书称 38；以文档实际行数为准逐项核对，不按数量估算）。
执行环境均为"独立 DG 进程 + 真实 Industrial Gateway / 真实 CSV 历史源 / 真实配置文件"。
结果列为本轮真实执行结果（2026-11-03，system 层 108.04 s 整跑）。

| UC 场景 | 测试标识 | Marker | 结果 |
|---|---|---|---|
| UC-01-S01 | test_simulation_system.py::test_random_generation | reliability/end_to_end | PASS |
| UC-01-S02 | test_simulation_system.py::test_static_physics | reliability/end_to_end | PASS |
| UC-01-S03 坏模型 | test_simulation_system.py::test_invalid_model_and_runtime_failure | fault_injection | PASS |
| UC-01-S03 周期超限 | test_simulation_system.py::test_overrun_and_stop | performance/overload | PASS |
| UC-02-S01 | test_replay_system.py::test_replay_1x_2x_order | end_to_end/time_order | PASS |
| UC-02-S02 | test_replay_system.py::test_equal_timestamps_and_empty_range | time_order | PASS |
| UC-02-S03 源失败 | test_replay_system.py::test_source_failure | fault_injection/invalid_records | PASS |
| UC-02-S03 停止重启 | test_replay_system.py::test_stop_restart_boundary | restart | PASS |
| UC-03-S01 映射 | test_simulation_system.py::test_gateway_feedback | end_to_end | PASS |
| UC-03-S02 错误映射 | test_simulation_system.py::test_bad_gateway_mapping | safety/invalid_data | PASS |
| UC-03-S03 超时未知 | test_lifecycle_system.py::test_gateway_timeout_unknown | network_failure | PASS |
| UC-03-S03 恢复/背压 | test_lifecycle_system.py::test_gateway_recovery_backpressure | overload | PASS |
| UC-03-S01 扇出 | test_simulation_system.py::test_fanout_multiple_gateways_and_variables | end_to_end | PASS |
| UC-03-S02 跨实例冲突 | test_simulation_system.py::test_conflicting_target_ownership | safety/conflict | **XFAIL（P1，见 §10）** |
| UC-03-S02 重复目标 | test_simulation_system.py::test_duplicate_target_mapping_rejected | safety/conflict | PASS |
| UC-07-S02 原子占用 | test_grpc_system.py::test_concurrent_start_atomic_target_reservation | concurrency | PASS |
| UC-07-S03 停止占用 | test_grpc_system.py::test_stop_retains_target_until_io_settled | safety/deadline | PASS |
| UC-03-S03 部分失败 | test_lifecycle_system.py::test_gateway_fanout_partial_failure | network_failure | PASS |
| UC-03-S03 背压独立 | test_lifecycle_system.py::test_fanout_bounded_backpressure | overload | PASS |
| UC-04-S01 | test_config_system.py::test_get_push_apply | reload | PASS |
| UC-04-S02 | test_config_system.py::test_invalid_candidate_and_conflict | conflict | PASS |
| UC-04-S03 重启 | test_config_system.py::test_restart_required | restart | PASS |
| UC-04-S03 异常 | test_config_system.py::test_apply_failure_recovery | fault_injection | PASS |
| UC-05-S01 | test_diagnose_system.py::test_simulate_replay_stages | end_to_end | PASS |
| UC-05-S02 | test_diagnose_system.py::test_empty_and_bad_mapping | no_targets | PASS |
| UC-05-S03 业务拒绝 | test_diagnose_system.py::test_grpc_rejection | fault_injection | PASS |
| UC-05-S03 未授权写入/取消 | test_diagnose_system.py::test_no_write_and_cancel | safety/deadline | PASS |
| UC-06-S01 | test_metrics_system.py::test_counters_and_latency | load | PASS |
| UC-06-S02 | test_metrics_system.py::test_no_samples_stale | no_samples | PASS |
| UC-06-S03 超限 | test_metrics_system.py::test_series_capacity | overload | PASS（暴露 P2-3） |
| UC-06-S03 纪元 | test_metrics_system.py::test_epoch_restart | restart | PASS |
| UC-07-S01 | test_grpc_system.py::test_contract_operations | end_to_end | PASS |
| UC-07-S02 | test_grpc_system.py::test_contract_errors | safety/invalid_input | PASS |
| UC-07-S03 超时取消 | test_grpc_system.py::test_timeout_cancel | deadline | PASS |
| UC-07-S03 关闭期请求 | test_grpc_system.py::test_shutdown_inflight | restart/deadline | PASS |
| UC-08-S01 | test_lifecycle_system.py::test_restart | restart | PASS |
| UC-08-S02 | test_lifecycle_system.py::test_startup_failure | fault_injection | PASS |
| UC-08-S03 崩溃 | test_lifecycle_system.py::test_crash_unknown_delivery | recovery | PASS |
| UC-08-S03 未收敛关闭 | test_lifecycle_system.py::test_unsettled_io_shutdown | safety/deadline | PASS |

合计：**38 PASS + 1 XFAIL（P1 真实失败证据）**。另在矩阵外新增 R08 专项系统用例
`test_replay_system.py::test_index_cancel_safely_joins_worker_and_removes_sqlite`（PASS，2.15 s）。
设计文档 §14.2 中原"NOT_RUN"标注自本轮起全部被真实执行结果取代；无一项以 NOT_RUN 充 PASS。

## 7. R01–R15 专项核查

| 编号 | 核查项 | 结论 | 真实证据 |
|---|---|---|---|
| R01 | Get/Push 完整快照往返一致性 | PASS | component `test_three_scopes_and_unknown_candidate`、`test_push_requires_expected_revision_and_full_set`；system `test_get_push_apply`、`test_contract_operations`（DISK/RUNTIME 真实取回比对） |
| R02 | Core 共享配置不可由 Generator Apply 改写 | PASS | component `test_core_shared_files_are_read_only`；基线内提交 `ac077124` 将篡改 Core 快照归类为所有权拒绝；CFG-05 约束保持 |
| R03 | 双文件提交中断/持久化标记/恢复 | PASS | component `test_recovery_rolls_back_active_transaction`、`test_committed_marker_wins_on_restart`；system `test_apply_failure_recovery`（真实文件事务） |
| R04 | 磁盘/候选/运行态 revision 区分 | PASS | component `test_three_scopes_and_unknown_candidate`；system `test_restart_required`、`test_epoch_restart`；冲突用例均以真实 revision 驱动 |
| R05 | 原生 RPC 超时后 UNKNOWN 与安全释放 | PASS（附实测语义，见 §10 观察 OBS-1） | component `test_logical_timeout_is_unknown_and_quarantines_gateway`；system `test_timeout_cancel`、`test_gateway_timeout_unknown`（逻辑超时后仅 1 次原生调用、UNKNOWN≥1、confirmed=0、DEGRADED） |
| R06 | Stop/Shutdown 真正有界、无任务泄漏 | PASS | system `test_stop_retains_target_until_io_settled`、`test_unsettled_io_shutdown`（SIGTERM 有界退出、returncode 0）、`test_shutdown_inflight`（<12 s 退出，退出后新连接 UNAVAILABLE/CANCELLED/DEADLINE_EXCEEDED）；soak FD 15→15（§8） |
| R07 | 变量可写性与类型预校验 | PASS（暴露 P2-1） | integration `test_dg_writer_uses_formal_update_rpc_end_to_end`（真实 ListVariables 目录）、`test_dg_writer_preflight_rejects_type_mismatch`、`test_per_item_results_and_duplicate_target` |
| R08 | 重放索引线程取消后 SQLite 生命周期 | PASS | integration `test_cancellation_waits_for_index_thread`、`test_temporary_index_does_not_survive_completion`；system R08 用例（12 万行、取消后零批次、无 windhub-replay-* 残留、2.15 s 收敛）；§8 索引体积实测 |
| R09 | 完整 CSV 预校验与时间排序 | PASS | integration `test_history_reader.py` 11 项（表头精确匹配、额外列、非 UTC 时间、坏值、空字段、record_id 唯一、字节上限在索引前生效等）；system `test_equal_timestamps_and_empty_range`、`test_source_failure`（整源预校验失败时零批次写出） |
| R10 | 15 RPC 真实可调用 | PASS | §4 矩阵；integration `test_all_fifteen_rpcs_callable` + system `test_contract_operations`（独立进程） |
| R11 | 数据写入 IP 白名单真实认证边界 | PASS | integration `test_ip_allowlist_denies_unauthorized_writer`（真实 Gateway 服务端拒绝非白名单写入者，非客户端侧自测） |
| R12 | 旧 Scenario/Generation 版本绑定及设备查询 | PASS | component `test_start_unknown_scenario_and_version_mismatch`、`test_revision_mismatch_rejects_start`、`test_immutable_scenario_version_body_change_rejected`；system 重启后旧 generation_id NOT_FOUND（`test_restart`、`test_crash_unknown_delivery`）；GetDevice/ListDevices 见 §4 |
| R13 | 指标真实准确、资源有界 | PASS（附 P2-3/P2-4） | component 6 项（nearest-rank 与真实样本逐分位核对、空窗不造零 P99、窗口过滤、维度过滤、容量静默截断）；system 4 项（计数与真实读回一致、纪元不拼接）；§8 P99 与 soak |
| R14 | Diagnose 未授权保证不写入 | PASS（暴露 P2-2） | component `test_write_stage_is_always_skipped_non_intrusive`；system `test_no_write_and_cancel`（真实 Gateway 侧无写入、取消不泄漏资源） |
| R15 | DG 与 Industrial Gateway 接口及配置兼容性 | PASS | descriptor 8 项（正式 7 RPC、VariableUpdate/ScalarValue 字段）；integration `test_gateway_integration.py` 7 项真实联调（分页/过滤、更新读回、逐项结果、批大小中止、白名单、生命周期与 revision 冲突、正式 RPC 端到端） |

## 8. 可靠性与性能实测（真实数据）

执行命令：

```bash
PYTHONPATH=src python -u tests/data_generator/performance/measure_acceptance.py
```

原始输出存档：`tmp-acceptance-logs/perf-run2.log`（一次性工作目录，未纳入提交）。

机器环境：WSL2 / Python 3.14.7 / grpcio 1.84.0 / 16 逻辑核 / 7.35 GiB RAM / 回环网络。
被测 HEAD：`bfeb6559041ddb2f822d924a6d8a6e9f98b8f2db`（运行时与验收基线 ac077124 的生产代码一致，
差异仅为测试与格式化提交）。

### 8.1 原生 UpdateVariables RTT（客户端 → 真实 Industrial Gateway）

- 负载：真实 `assemble_gateway` 正式 IndustrialGatewayService（7 RPC 全装配），
  单变量 `UpdateVariables(gateway_id, [VariableUpdate(variable_id, ScalarValue.double_value)])`。
- 样本量：2000（另 200 次预热丢弃），单连接串行。
- 结果：**P50=1.098 ms，P95=1.491 ms，P99=1.722 ms，MAX=2.717 ms**（首轮复测 P50=1.080/P95=1.517/P99=1.771）。

### 8.2 DG 生成 → 正式更新链路延迟（生产指标）

- 负载：独立 DG 子进程，simulation interval=0.02 s，运行 12 s，真实 Gateway；
  数据取自生产指标 `gateway.gw-a.rpc_duration_p50/p95/p99`（nearest-rank）。
- 样本量：599 次成功确认（generated=599、confirmed=599、unknown=0）。
- 结果：**P50=2.856 ms，P95=3.914 ms，P99=4.666 ms**（首轮 P50=2.569/P95=3.497/P99=4.015）。

### 8.3 重放整源索引代价（首批发出前完成整源索引）

| CSV 行数 | 启动→首批发出 | CSV 体积 | 临时 SQLite 索引体积 |
|---|---|---|---|
| 1,000 | 1.121 s | — | — |
| 200,000 | 2.418 s | 12.5 MB | 30.2 MB（系统临时目录 `windhub-replay-*`，用完即删） |

说明：1k/200k 启动耗时均含进程启动与配置加载；索引本身为整源完成后才发首批，与 R08/R09 语义一致。

### 8.4 30 s 高频长时资源

- 负载：interval=0.02 s 持续 30 s，confirmed=1500。
- 结果：**RSS 69.1→69.2 MB（Δ+0.1 MB），FD 15→15**。无单调增长趋势，文件描述符无泄漏。

### 8.5 可靠性专项真实结论（另见系统测试）

- 断连/挂起恢复：逻辑超时窗口内放行在途调用，Gateway 可恢复 HEALTHY；硬截止后不可恢复（OBS-1）。
- 全局背压：故障双网关 max_inflight=2 下实测**跨网关聚合在途原生调用峰值 ≤2**；
  健康网关在对端故障期间持续成功输出。
- 故障隔离：A 网关挂起时 A 仅 1 次原生调用，B 真实网关 success≥4、failed=0、两次读回值不同。
- 崩溃恢复：SIGKILL 后重启，计数归零、旧 generation_id NOT_FOUND、无补偿重放、新任务正常。
- 未收敛 I/O 关闭：gate 不开时 SIGTERM 仍有界退出（实测 <10 s），returncode 0。
- 异常配置：YAML 语法错误与 `grpc_timeout=0.001` 越界均启动即非零退出、stdout 有错误、不监听端口。

## 9. 回归核查

- 本轮生产代码零改动；`git diff --stat` 范围内变更全部位于 `tests/data_generator/`。
- 静态门对 `src/data_generator` 全量 mypy strict 0 问题、lint-imports 0 broken，
  新增测试未引入跨层反向依赖。
- 全仓测试层面仅运行 Data Generator 套件（本次任务范围）；未触碰其他模块代码，
  无证据表明对其他模块产生行为影响；既有其他模块 CI 失败（若有）按任务规则与本次变更无关，标记 UNRELATED。

## 10. 问题清单（实测证据，按严重度）

### P0

无。

### P1（1 项，需交 ChatGPT 做状态机/所有权层面决策，本轮只记录不修）

- **P1-1 跨进程/跨实例 `(gateway_id, variable_id)` 写入所有权无仲裁。**
  证据：`system/test_simulation_system.py::test_conflicting_target_ownership`（xfail strict=False，真实失败）。
  单实例内的并发 Start 原子占用是正确的（`test_concurrent_start_atomic_target_reservation` PASS），
  但两个活动 DG 实例争用同一 Gateway 同一变量时，第二个实例的生成/应用不会被拒绝——
  所有权只存在于单进程内存态，Industrial Gateway 正式 `UpdateVariables` 不提供写入租约。
  这属于"Generation 状态机与并发所有权重大调整 / Gateway 输出可靠性语义"范畴，超出小修边界。

### P2（4 项 + 2 项实测语义观察）

- **P2-1 预校验类型不匹配的任务最终落 INVALID_STATE 而非明确的预检拒绝错误码。**
  证据：integration `test_dg_writer_preflight_rejects_type_mismatch`（拒绝行为正确、有真实断言；
  分类语义不精确）。位置：`src/data_generator/infrastructure/server.py:34` 的 INVALID_STATE 映射。
- **P2-2 Diagnose 许可等待硬编码 0.001 s，并发排队时诊断基本不可用。**
  位置：`src/data_generator/infrastructure/server.py:491`
  `wait_for(self._diagnostics_permits.acquire(), timeout=0.001)`。组件层证实写阶段恒跳过（R14 安全），
  但并发下第二个 Diagnose 几乎必超时，应改为可配置或显式 429/繁忙语义。
- **P2-3 指标 max_series 超限静默截断，无明确降级信号。**
  证据：component `test_metrics_system::test_series_capacity` 与
  `test_series_capacity_silently_bounds_output`（行为有界但调用方无法感知被截断）。
  位置：`src/data_generator/infrastructure/metrics.py:39`（默认 2000，上限 10000）。
- **P2-4 restart-required 的 Apply 成功响应 effective_revision 陈旧。**
  服务文件变更提交后文件 revision 已前进、但运行态须重启才加载，
  `ApplyConfigSnapshotResponse.effective_revision` 取 `runtime.settings.revision`（旧值）
  （`src/data_generator/infrastructure/server.py:402`；对比失败路径 :396 取磁盘快照 revision）。
  调用方无法从响应体区分"已生效版本"与"磁盘已提交版本"。
- **OBS-1（实测语义，非崩溃缺陷但影响可用性）：Gateway 一旦越过原生调用硬截止即被进程内永久隔离。**
  生产代码事实：逻辑超时为 `output.grpc_timeout_seconds`（wait_for shield，到点→UNKNOWN+隔离），
  原生 gRPC 调用另带硬截止 `timeout*2`（`gateway.py:219`）；隔离集合中的网关在
  `_send_one` 入口直接短路为 TARGET_QUARANTINED（`gateway.py:182`），
  解除隔离的唯一机会是触发隔离的那个在途调用之后成功回调（`gateway.py:239/260`）。
  因此：① 恢复测试必须在逻辑超时与 2× 硬截止之间放行，硬截止过后回调以 DEADLINE_EXCEEDED 结束，
  隔离永久化，只能重启进程；② 系统无主动健康探测（GetServiceStatus 依赖明细为 "no active health probe"），
  不存在自愈/重试飞。建议明确设计立场：要么提供受控重试飞/探测，要么在文档与状态语义中写明
  "硬截止后需人工/重启恢复"。
- **OBS-2（指标口径实测语义）：`batches_total` 计入被背压拒绝的派发尝试。**
  饱和场景实测 batches_total=19 而真实原生派发仅个位数；有界性应看跨网关在途并发
  （实测 ≤max_inflight）与真实原生调用数，不能以 batches_total 断言"发出批次数"。
  建议重命名或补充指标说明，避免运维误读。

## 11. NOT_RUN / BLOCKED / SKIPPED 事项

| 事项 | 状态 | 说明 |
|---|---|---|
| `performance` Marker 中需 root + tc netem 的网络损伤压测（`scripts/run_benchmark.py`） | NOT_RUN | WSL2 环境未提供 root/tc 网络损伤；§8 数据基于真实回环与应用层挂起/超时注入，未伪造网络丢包数据 |
| 超过 30 s 的长稳/长 soak（小时级、内存漂移） | NOT_RUN | 本轮以 30 s 高频 soak 取证（RSS/FD 平稳）；硬件、长稳按仓库生命周期为独立可选流程 |
| 与真实远端硬件 Gateway 的跨机联调 | NOT_RUN | 验收环境内以本机真实 Industrial Gateway 服务覆盖正式接口语义；跨机网络/TLS 不在 v1 契约范围 |
| P1-1 修复验证 | BLOCKED | 待所有权仲裁方案设计决策（交 ChatGPT） |
| P2-1～P2-4、OBS-1/2 修复 | SKIPPED（本轮不修） | 按权限边界只记录；其中 P2-2/P2-4/OBS-1 涉及对外语义，建议随设计确认一并整改 |

以上均不计入 PASS。

## 12. 最终裁定

**裁定：PASS WITH LIMITATIONS。**

依据：

1. 静态门全绿（ruff/format/mypy strict/lint-imports），15 个 RPC 经 Descriptor 反射与
   独立进程真实调用双轨核验，正式 `UpdateVariables` 链路端到端真实打通。
2. 四层 146 passed + 1 xfailed；设计 §14.2 实际枚举的 39 个标识全部有对应真实测试并真实执行
   （38 PASS，1 个以 xfail 形式承载 P1 的真实失败证据），无 NOT_RUN 充 PASS；
   R01–R15 逐项有真实证据；性能数据含机器环境、负载、命令、样本量与 P50/P95/P99。
3. 限制项：存在 1 个 P1（跨实例写入所有权无仲裁，需状态机/接口层面设计决策）与 4 个 P2、
   2 项实测语义观察；网络损伤压测与小时级长稳 NOT_RUN。
   单实例内全部安全/有界性主张均有真实证据支撑，故不判 FAIL；
   但在 P1 仲裁方案落地前，不应声称多实例部署下的写入隔离已验收通过。

交 ChatGPT 的重大事项：

1. P1-1：跨实例 `(gateway_id, variable_id)` 所有权仲裁（状态机/租约或 Gateway 侧拒绝机制）。
2. OBS-1：原生硬截止后永久隔离、无主动探测/重试飞的设计立场确认。
3. P2-2/P2-4：Diagnose 并发许可语义、Apply 响应 effective_revision 口径，均涉对外契约。

---

提交说明：本报告与全部新增/整改测试代码同批提交；`src/data_generator/` 本轮零改动，
故无独立"小修复"提交（任务书允许：确无生产修复时测试代码单类提交并在此说明）。

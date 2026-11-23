# C7 第三轮独立复测报告（小范围修复 + 失败台账）

- 分支：`design/config-activation-selection-20261121`
- 交接基线：`42c90806a`（2026-11-21）
- 执行日期：2026-11-21
- 执行方：独立复测（Claude C7 round3）
- 范围：全量静态门禁、全仓 collection、七个模块全量测试；本轮只做允许范围内的小范围修复，不做大规模夹具重写，不创建 PR/分支，不合并。

## 1. Git 状态

### 1.1 起始 / 最终 HEAD

- 起始：`42c90806a`（与远端一致）
- 最终：见报告末尾「提交清单」（推送后本地与远端一致）

### 1.2 本轮提交（均为测试侧或局部生产修复，无契约重设计）

| commit | 类型 | 内容摘要 |
|---|---|---|
| 030c1f29b | style | 15 个测试文件 ruff format（含 20 处 E501 折行），无语义变更 |
| 4ac85db47 | test | Core 四套件迁移到版本化文件名与退役 Task Template 契约 |
| 839f6ba03 | test | Configuration 22 个失败对齐全目录契约 |
| f5efd3f4e | test | Collector 组件套件指向版本化出厂配置 |
| e84116679 | fix+test | Commander 快照解析版本化 catalog（生产修复）+ gRPC 矩阵断言对齐 |
| 782947ade | test | Gateway 版本化出厂配置与唯一发布者契约对齐 |
| b9a38233a | style | commander gRPC 矩阵用例 format 收尾 |
| 8af925f5a | test | Sink 启动器显式 `--service-file` + root-B Deployment 清单一致性 |

工作树收尾状态：干净（除本报告外全部修改已提交）。

## 2. 静态门禁（实际命令与退出码）

| 检查 | 命令 | 起始结果 | 收尾复验（全部实跑） |
|---|---|---|---|
| Ruff | `python -m ruff check src tests scripts` | exit 1：20 个 E501，全在 tests | **exit 0**（All checks passed） |
| Ruff format | `python -m ruff format --check src tests scripts` | exit 1：18 文件漂移 | **exit 0**（1020 文件） |
| mypy | `python -m mypy src` | exit 0（451 文件，0 错误） | **exit 0**（451 文件，0 错误） |
| import-linter | `lint-imports`（PATH 可执行；`python -m lint-imports` 在本环境无此模块） | exit 0：19 kept, 0 broken | **exit 0**：19 kept, 0 broken |
| RPC 一致性 | `python scripts/generate_rpc.py --check` | exit 0 | **exit 0** |
| API 契约 | `python scripts/generate_api_contract.py --check` | exit 0（DRIFT CHECK: PASS） | **exit 0**（DRIFT CHECK: PASS） |
| 全仓 collection | `python -m pytest --collect-only -q tests` | exit 0：3661 collected，零收集错误 | **exit 0**：3661 collected，零收集错误 |

未通过新增忽略规则或检查范围收缩实现门禁转绿。

## 3. 七模块测试结果总览

| 模块 | 命令 | 起始 | 本轮修复后（全部实跑） |
|---|---|---|---|
| Core | `pytest tests/core -q` | 4 failed / 528 passed | **532 passed，0 failed** |
| Configuration | `pytest tests/configuration_service -q` | 22 failed / 235 passed | **257 passed，0 failed**（真实 NATS 广播用例实跑通过，零 skip） |
| Collector | `pytest tests/collector -q` | 69 failed / 85 passed / 2 skipped（926s） | 非系统段 103 passed / 5 failed；系统段 46 failed（共享旧夹具，未动）；2 skipped 为 TwinCAT NOT_RUN |
| Commander | `pytest tests/commander -q` | 42 failed / 313 passed / 1 skipped / 3 errors（418s） | 非系统段 300 passed / 28 failed；系统段 14 passed / 13 failed / 1 skipped / 3 errors（生产快照修复对系统段零回归） |
| Industrial Gateway | `pytest tests/industrial_gateway -q` | 12 failed / 200 passed（39s） | **202 passed / 10 failed**（权威复跑确认，10 项全为退役 push/apply 工作流） |
| Data Generator | `pytest tests/data_generator -q` | 44 failed / 151 passed（172s） | 未修复：44 failed 单夹具根因，交 ChatGPT |
| Sink | `pytest tests/sink_service -q` | 59 failed / 596 passed / 10 skipped（1969s） | 系统段全量复跑 57 passed/2 failed → 再修复后定向复跑 2 passed；全模块最终 **655 passed / 10 skipped / 0 failed** |

环境类跳过均为诚实 NOT_RUN：TwinCAT ADS 路由（Collector 2 项）、docker/NATS 条件夹具、NATS server bin（Sink 10 项，集成段）。

### 3.1 收尾口径下的失败计数

- 全绿模块：Core、Configuration、Sink（共 3 个）。
- 仍有 RELATED 失败：Collector 51、Commander 44（含 3 setup errors）、Gateway 10、Data Generator 44，合计 **149 个节点**，完整清单见附录 A，根因仅两类（退役 Push/Apply 工作流重设计、Deployment 版本化树夹具迁移），无新增生产缺陷。

## 4. 本轮已闭环问题（修复前后证据）

### F1 静态风格全红（20 E501 + 18 文件 format）— CLOSED

- 根因：ChatGPT 两轮整改后测试文件未跑 formatter；全部位于 tests，无生产代码。
- 修复：`ruff format` 自动折行（030c1f29b 及后续提交内联收尾）。
- 复验：ruff check / format 双 exit 0。

### F2 Core 4 套件引用退役文件名/断言（4 failed）— CLOSED

- `tests/core/config/test_unit_catalog.py`：`units_standard.yaml`/`business_points_standard.yaml` → `units_standard_v1.2.0.yaml`/`business_points_standard_v1.4.0.yaml`。
- `test_shared_snapshot.py`：`site.yaml`/`devices.yaml` → `site_example_site_v1.1.0.yaml`/`device_farm-a_v1.4.0.yaml`。
- `test_protocol_template_contract.py`：改用现役 `resolve_device_templates`（`resolve_model_templates` 已无任何生产调用方，仅本测试引用，属退役残留），并 glob 全部版本化 device 实例。
- `test_versioned_template_security.py`：Task Template 已随 45ed5ec14 退役，断言改为三类别（protocols/point_tables/device_models）并钉死 `templates/tasks` 不得恢复。
- 复验：`pytest tests/core -q` → 532 passed。

### F3 Configuration 22 失败（11 个独立根因，全 CLOSED）

1. 严格 schema 下测试自注册 schema 缺 `service_id`（6 项）：补声明，数组 items 校验意图不变。
2. 旧 `templates.root` 拒绝消息变更（3 项）：正则纳入 `instances only`，拒绝事实仍成立。
3. bootstrap 准入守卫（2 项）：用与 app `own_service_id` 一致的版本化身份直达生产守卫 `Configuration Service bootstrap must remain local YAML`，保留安全覆盖。
4. 备份状态响应形状（1 项）：`data.collector_tasks` → `data.task`（对齐 `GetBackupTaskResponse.task`）。
5. 退役 `templates/tasks` 夹具（2 项：NATS 广播、进程重启）：替换为 `instances/collector_tasks_*_v1.0.0.yaml`。
6. `config_type="task"` 残留（pagination 1 项）：改 `collector_tasks`。
7. 不可变版本冲突用例（1 项）：未知字段 `name` 会被规范化剥离导致同内容幂等，改用 schema 内 `tasks` 字段制造真实内容差异，仍得 FAILED_PRECONDITION。
8. 全目录发布旧文件名（1 项）：版本化 catalog 文件名。
9. bundle 配额 4 项：非法 `point_group` 填充改为 schema 内 `tasks` 填充；依赖引用 `task` → `collector_tasks`；身份查询同步。
10. 实例身份守卫消息（1 项）：正则对齐 `identifier`。
- 复验：`pytest tests/configuration_service -q` → 257 passed，零 skip。

### F4 Collector 组件段 18 失败 — CLOSED（系统段 46 项另立台账）

- `test_acquisition_runtime_v1`（17 项）：共享 helper 指向退役 `site.yaml`/`devices.yaml`，改版本化出厂实例。
- `test_grpc_contract_v1`（1 项）：合成 `collector.yaml` 改为完整出厂树（含 deployments）+ `service_collector-realtime_v1.1.0.yaml`，经 Deployment 选版启动真实进程。
- 复验：组件/集成/单元段 103 passed，余 5 项失败见台账 C-3/C-4。

### F5 Commander 生产缺陷：快照硬编码退役 catalog 文件名 — CLOSED

- 生产缺陷（P1）：`src/commander/infrastructure/adapters/inbound/grpc/snapshots.py` 四处硬编码 `units_standard.yaml`/`business_points_standard.yaml`，在版本化目录下使任何 `GetConfigSnapshot` DISK/RUNTIME 调用 NOT_FOUND（30 个测试级联）。
- 修复：新增 `_sole_catalog()` 按 `*_v*.yaml` fail-closed 解析唯一钉选 catalog（与 `SharedDocumentLoader.sole_catalog` 同语义）；移除不可变豁免集合，版本化 catalog 与其他模板同受不可变保护。
- 测试对齐：`test_all_seventeen_rpcs_registered_and_callable` 的 Push/Apply 段改为断言 FAILED_PRECONDITION（五服务统一的唯一发布者契约）。
- 复验：mypy src/commander 0 错误；非系统段 300 passed；系统段集合无回归（失败集合与修复前一致，根因均为共享旧夹具）。

### F6 Gateway 2 失败 — CLOSED（余 10 项另立台账）

- `test_factory_shipped_config_passes_native_check`：`industrial_gateway.yaml` → `service_gateway_v1.2.0.yaml`（deployment_prod-a 唯一选它）。
- `test_operator_lifecycle_but_no_config_write`：鉴权先于业务守卫，operator 的 PERMISSION_DENIED 断言保留；admin 段改断言 FAILED_PRECONDITION。
- 复验：全模块 202 passed / 10 failed。

### F7 Sink 系统段 59 失败单一根因 — CLOSED

- 实证（59 份 sink-service 日志一致）：生产入口默认 `configs/services/service_sink-west_v1.2.0.yaml`（main.py:23），全部系统测试树由 `_stage1_fixtures` 写出 `service_sink-west_v1.0.0.yaml`，进程启动即 FileNotFoundError，随后 TCP 30s 就绪超时。
- 修复一（仅测试支撑层）：`tests/support/process.py::start_sink_service_from_root` fail-closed 解析树内唯一 `service_*.yaml` 并显式传 `--service-file`；生产激活/CURRENT/Worker 恢复语义未动。系统段全量复跑（1629s）：57 passed / 2 failed。
- 修复二（同根因夹具一致性）：残留 2 项（uc09 端口冲突、uc10 Durable 切换）均在 root-B 改 service `deployment_id` 后未同步 Deployment 清单，选择器在 bind 前 fail-closed。新增 `_stage1_fixtures.rewrite_deployment_identity()`，两处复制根在换身份时同步重写 `deployment_<new_id>_v1.0.0.yaml` 并保持唯一选版。定向复跑 2 passed；非系统段复跑 596 passed / 10 skipped。
- 最终：Sink 全模块 655 passed / 10 skipped / 0 failed。

## 5. 开放失败台账（交 ChatGPT 集中整改）

> 分类按交接约定：FIXTURE_MISMATCH / ASSERTION_MISMATCH / PRODUCTION_DEFECT / DESIGN_CONFLICT / ENVIRONMENT。下列均为 RELATED（本分支契约迁移未完成），无 main 基线回归项。

### C-1 Collector 系统夹具整体停留在退役契约（46 failed，P1，FIXTURE_MISMATCH）

- 共享根因：`tests/collector/system/config_tree.py::write_config_tree` 写出无 `service_id`/`file_version` 的 `services/collector.yaml`、`templates/tasks/`、`templates.root`、路径式实例引用；生产 `select_local_service` 立即拒绝（`Service identity and version are required`），真实进程无法启动，全部系统用例级联。
- 代表性堆栈：`local_selection.py:89 ValueError` → `validation.py:42 ConfigError: Collector Deployment selection failed` → `Collector exited before gRPC readiness`。
- 完整节点：见 `/tmp/c7r3/collector-failed.txt` 中 `/system/` 46 条（报告附录同步列出）。
- 迁移参照：`tests/collector/unit/test_configuration_source_paths.py`（Deployment 树标准写法）、本轮 f5efd3f4e 的完整出厂树装配。
- 2 个 skip 为 TwinCAT ADS NOT_RUN（`WIND_HUB_TEST_ADS_ROUTER` 未设），非失败。

### C-2 Collector 非系统段 5 失败（P2，FIXTURE_MISMATCH）

- `test_snapshot_grpc_v1::test_full_snapshot_grpc_roundtrip`：围绕已退役 Push/Apply 候选生命周期，需按唯一发布者契约重设计（涉及激活语义，不在本轮局部修复范围）。
- `test_declared_source_validation` 3 项 + `test_service_snapshot_source` 1 项：依赖同一 `write_config_tree` 旧树与路径式来源；其安全意图（缺选版 fail-closed、禁止路径身份）已由 `test_configuration_source_paths.py` 现役用例覆盖。

### C-3 Commander 退役 Push/Apply 工作流（26 failed，P1，ASSERTION_MISMATCH/重设计）

- 文件：`tests/commander/integration/test_hot_reload.py` 全部 26 个节点。
- 根因：五服务生产侧统一 `FAILED_PRECONDITION: Configuration Repository is the sole configuration publisher; use Deployment`（`server.py:462/470`），套件仍断言候选推送/应用成功路径。
- 需围绕 Configuration Repository 发布 + Deployment 激活重写，非断言替换可解。

### C-4 Commander 系统/组件旧树夹具（15 failed + 3 errors，P1，FIXTURE_MISMATCH）

- `system/conftest.py::write_service_config`、`system/test_b1_effective_options.py`、`component/test_persistence_service_health.py` 等：根级 `commander.yaml`、`instances/devices.yaml`、无版本 catalog、`templates.root`。
- 现象：`Service must reside in configs/services`（4）、`devices.yaml REFERENCE_NOT_FOUND`、3 个 setup ERROR、2 个 Push FAILED_PRECONDITION（grpc_end_to_end）。
- 迁移参照：`tests/commander/grpc_harness.py`（已含 Deployment 清单的标准树）。

### C-5 Gateway 退役 Push/Apply 工作流（10 failed，P2）

- `test_config_snapshots` 7、`test_claims_reload_and_rpc_lifecycle` 2、`system/test_config_churn` 1，全部为唯一发布者 FAILED_PRECONDITION；需同 C-3 重设计。

### C-6 Data Generator 单一旧树构建器（44 failed，P1，FIXTURE_MISMATCH）

- 单一根因：`tests/data_generator/conftest.py::_gateway_tree`（约 653-780 行）产出的 service 文档缺 `service_id` 且带禁用的 `config_sources.templates.root`，pydantic `GatewayServiceConfig` fail-closed（43 条同源 ValidationError + 1 条包装断言）。
- 同文件 `write_service_yaml`（85 行起，含 `deployment_dg-test_v1.0.0.yaml`）已是新契约范例，可机械迁移。
- 完整节点：`/tmp/c7r3/dg-failed-final.txt`（44 条）。

### C-7 生产退役残留观察（P3，不阻塞，建议 ChatGPT 裁定）

- `src/core/configuration/template_graph.py::resolve_model_templates` 与 `ModelTemplates.task` 无任何生产调用方（Task Template 退役残留）；本轮仅迁移测试，未删生产代码。
- `src/core/configuration/versioned_templates.py:TEMPLATE_FILE_PREFIXES` 仍保留 `"tasks": "task_"` 键；shipped 配置已无 tasks 目录，无生产引用，是否移除待裁定。

### C-8 跨模块系统测试残留（七模块之外，供台账完整性）

- `tests/system/recovery/test_reload_failure.py:125` 仍引用 `templates/tasks/task_*` 旧路径，属同一退役夹具迁移工作流。

## 6. 必须保持的配置契约复核

本轮全部修复未触碰以下契约，且相关守卫均有现役测试覆盖：Deployment 精确选版、Service 仅引用实例逻辑身份、禁 `templates.root`、版本化文件名、发布不等于激活、target/active revision 如实反映、失败不谎报成功、Sink Worker 恢复跟随 Main 当前 Generation（`test_generation_selection.py` 等组件用例全绿）。

## 7. NOT_RUN / 未覆盖

- 跨模块全栈 CLI E2E、600s/S4 性能长测与现场硬件（TwinCAT、netem、多机）不在本轮范围；Collector 2 项 ADS 用例、Sink 10 项 docker/NATS bin 条件用例为诚实 skip。
- Collector/Commander/DG/Gateway 的开放失败未做修复后复跑（夹具未改，根因不变）；本轮所有生产/测试改动均按改动面定向复验，Sink 系统段做了全量复跑。
- 七模块之外的 `tests/system`、`tests/web_server`、`tests/cli`、`tests/storage_service` 等仅经全仓 collection（3661 项零错误），未全量执行；其中 `tests/system/recovery/test_reload_failure.py:125` 已列入台账 C-8。

## 8. 最终裁决：PASS_WITH_LIMITATIONS

依据：

1. 静态门禁全绿（ruff/format/mypy/import-linter/RPC/API/collection），未放松任何检查。
2. 三个模块全绿：Core 532、Configuration 257、Sink 655（含 10 个诚实 skip）；本轮实证修复 1 个生产缺陷（Commander 快照硬编码退役文件名）与 100+ 测试侧契约漂移。
3. 剩余 149 个失败节点根因收敛、台账完整，全部为测试夹具/退役工作流迁移（FIXTURE_MISMATCH / ASSERTION_MISMATCH），未发现新的生产缺陷；生产选版/激活/不可变/失败不谎报等契约守卫均有现役测试覆盖。
4. 局限：Collector/Commander/Data Generator/Industrial Gateway 四模块的系统与部分集成段需 ChatGPT 按附录 A 完成夹具迁移与 Push/Apply 工作流重设计后才能进入最终验收；全栈 CLI E2E 与现场硬件验收未执行。

不具备 PASS 条件（四模块仍红、端到端验收缺口），亦不构成 FAIL（无新增生产缺陷、门禁全绿、三模块全绿、失败均可复现且根因明确），故裁决 **PASS_WITH_LIMITATIONS**，移交 ChatGPT 按第 5 节台账继续集中整改。

## 附录 A：开放失败完整节点清单（149）

### A.1 Collector（51）

- tests/collector/system/test_collector_acquisition_system.py::test_uc03_s01_poll_publishes_complete_envelope
- tests/collector/system/test_collector_acquisition_system.py::test_uc03_s01_listen_receives_spontaneous_iec104
- tests/collector/system/test_collector_acquisition_system.py::test_uc03_s01_reliable_delivery_is_pubacked_in_jetstream
- tests/collector/system/test_collector_acquisition_system.py::test_uc03_s02_interrogation_reports_missing_points
- tests/collector/system/test_collector_acquisition_system.py::test_uc03_s02_overdue_poll_increments_skipped_slots
- tests/collector/system/test_collector_acquisition_system.py::test_uc03_s02_iec104_handshake_against_blackhole_is_bounded
- tests/collector/system/test_collector_acquisition_system.py::test_uc03_s03_nats_outage_then_recovery_resumes_publishing
- tests/collector/system/test_collector_config_paths_system.py::test_cfg_path_01_custom_source_paths
- tests/collector/system/test_collector_config_paths_system.py::test_cfg_path_02_03_08_reject_invalid_sources[bad_reference0]
- tests/collector/system/test_collector_config_paths_system.py::test_cfg_path_02_03_08_reject_invalid_sources[bad_reference1]
- tests/collector/system/test_collector_config_paths_system.py::test_cfg_path_02_03_08_reject_invalid_sources[bad_reference2]
- tests/collector/system/test_collector_config_paths_system.py::test_cfg_path_02_03_08_reject_invalid_sources[bad_reference3]
- tests/collector/system/test_collector_config_paths_system.py::test_cfg_path_02_03_08_reject_invalid_sources[bad_reference4]
- tests/collector/system/test_collector_config_paths_system.py::test_cfg_path_05_service_references_switch_only_after_restart
- tests/collector/system/test_collector_config_paths_system.py::test_cfg_path_06_restart_uses_committed_generation
- tests/collector/system/test_collector_config_paths_system.py::test_cfg_path_04_push_apply_custom_named_source_snapshot
- tests/collector/system/test_collector_config_paths_system.py::test_cfg_path_07_two_template_roots_never_fall_back
- tests/collector/system/test_collector_config_system.py::test_uc04_s01_push_is_inert_until_apply
- tests/collector/system/test_collector_config_system.py::test_uc04_s02_stale_revision_and_published_template_immutability
- tests/collector/system/test_collector_config_system.py::test_uc04_s03_service_document_change_requires_restart
- tests/collector/system/test_collector_device_system.py::test_uc01_s01_real_device_connect_and_health[modbus-1]
- tests/collector/system/test_collector_device_system.py::test_uc01_s01_real_device_connect_and_health[iec104-1]
- tests/collector/system/test_collector_device_system.py::test_uc01_s02_duplicate_start_is_idempotent
- tests/collector/system/test_collector_device_system.py::test_uc01_s02_wrong_endpoint_is_reported_per_device
- tests/collector/system/test_collector_device_system.py::test_uc01_s02_disabled_device_rejects_start
- tests/collector/system/test_collector_device_system.py::test_uc01_s03_link_loss_watchdog_reconnects[modbus]
- tests/collector/system/test_collector_device_system.py::test_uc01_s03_link_loss_watchdog_reconnects[iec104]
- tests/collector/system/test_collector_diagnose_system.py::test_uc05_s01_selected_stages_carry_evidence
- tests/collector/system/test_collector_diagnose_system.py::test_uc05_s02_no_target_unknown_stage_and_boundary_limitation
- tests/collector/system/test_collector_diagnose_system.py::test_uc05_s03_unreachable_endpoint_fails_and_deadline_stops_later_stages
- tests/collector/system/test_collector_diagnose_system.py::test_uc05_s03_expired_deadline_marks_later_stages_not_run
- tests/collector/system/test_collector_grpc_system.py::test_uc07_s01_all_20_rpcs_real_invocation
- tests/collector/system/test_collector_grpc_system.py::test_uc07_s02_authorization_pagination_and_invalid_arguments
- tests/collector/system/test_collector_grpc_system.py::test_uc07_s02_event_paging_evicts_oldest_session_at_capacity
- tests/collector/system/test_collector_grpc_system.py::test_uc07_s03_deadline_unconfirmed_and_shutdown_unavailable
- tests/collector/system/test_collector_lifecycle_system.py::test_uc08_s01_cli_check_validates_complete_tree
- tests/collector/system/test_collector_lifecycle_system.py::test_uc08_s02_sigterm_shuts_down_gracefully_and_releases_lock
- tests/collector/system/test_collector_lifecycle_system.py::test_uc08_s03_kill_restart_resumes_durable_start_intent
- tests/collector/system/test_collector_lifecycle_system.py::test_uc08_s03_partial_device_failure_keeps_healthy_device_running
- tests/collector/system/test_collector_lifecycle_system.py::test_uc08_s03_unsettled_io_shutdown_exits_nonzero
- tests/collector/system/test_collector_metrics_system.py::test_uc06_s01_empty_window_has_no_fabricated_zero
- tests/collector/system/test_collector_metrics_system.py::test_uc06_s02_windowed_counts_and_p95_p99_after_real_traffic
- tests/collector/system/test_collector_metrics_system.py::test_uc06_s03_window_must_be_whole_seconds
- tests/collector/system/test_collector_task_system.py::test_uc02_s01_start_samples_then_stop_is_silent
- tests/collector/system/test_collector_task_system.py::test_uc02_s02_start_while_link_down_converges_after_link_returns
- tests/collector/system/test_collector_task_system.py::test_uc02_s03_manual_stop_never_revived_by_watchdog_or_reload
- tests/collector/component/test_snapshot_grpc_v1.py::test_full_snapshot_grpc_roundtrip
- tests/collector/integration/test_declared_source_validation.py::test_nondefault_source_files_are_used_by_full_validation
- tests/collector/integration/test_declared_source_validation.py::test_no_fallback_to_valid_default_when_declared_file_invalid[site-A.yaml]
- tests/collector/integration/test_declared_source_validation.py::test_no_fallback_to_valid_default_when_declared_file_invalid[devices-A.yaml]
- tests/collector/integration/test_service_snapshot_source.py::test_service_path_binding_persists_without_source_fallback

### A.2 Commander（44）

- tests/commander/component/test_persistence_service_health.py::test_persistence_worker_death_is_degraded
- tests/commander/component/test_persistence_service_health.py::test_stop_reports_persistence_queue_when_drain_times_out
- tests/commander/integration/test_hot_reload.py::test_disk_runtime_candidate_scopes_are_distinct
- tests/commander/integration/test_hot_reload.py::test_push_rejects_noncanonical_or_escaping_paths[]
- tests/commander/integration/test_hot_reload.py::test_push_rejects_noncanonical_or_escaping_paths[../escape.yaml]
- tests/commander/integration/test_hot_reload.py::test_push_rejects_noncanonical_or_escaping_paths[config/../escape.yaml]
- tests/commander/integration/test_hot_reload.py::test_push_rejects_noncanonical_or_escaping_paths[/etc/passwd]
- tests/commander/integration/test_hot_reload.py::test_push_rejects_noncanonical_or_escaping_paths[./commander.yaml]
- tests/commander/integration/test_hot_reload.py::test_push_rejects_noncanonical_or_escaping_paths[config//instances/devices.yaml]
- tests/commander/integration/test_hot_reload.py::test_push_rejects_noncanonical_or_escaping_paths[.hidden.yaml]
- tests/commander/integration/test_hot_reload.py::test_push_rejects_noncanonical_or_escaping_paths[config/.secret/devices.yaml]
- tests/commander/integration/test_hot_reload.py::test_push_rejects_duplicate_relative_path
- tests/commander/integration/test_hot_reload.py::test_push_verifies_size_and_sha_before_yaml_parse[size]
- tests/commander/integration/test_hot_reload.py::test_push_verifies_size_and_sha_before_yaml_parse[sha]
- tests/commander/integration/test_hot_reload.py::test_push_stale_expected_revision_aborted
- tests/commander/integration/test_hot_reload.py::test_push_malformed_service_yaml_rejected_before_staging
- tests/commander/integration/test_hot_reload.py::test_push_malformed_shared_devices_yaml_returns_validation_issue
- tests/commander/integration/test_hot_reload.py::test_push_service_yaml_with_broken_tls_paths_rejected
- tests/commander/integration/test_hot_reload.py::test_push_missing_referenced_devices_file_rejected
- tests/commander/integration/test_hot_reload.py::test_push_absolute_config_source_path_validates
- tests/commander/integration/test_hot_reload.py::test_push_cannot_modify_existing_versioned_template
- tests/commander/integration/test_hot_reload.py::test_unreferenced_new_template_does_not_reload_devices
- tests/commander/integration/test_hot_reload.py::test_apply_replaces_files_without_semantic_reload_for_comment
- tests/commander/integration/test_hot_reload.py::test_apply_service_level_write_deadline_change_requires_restart
- tests/commander/integration/test_hot_reload.py::test_apply_hot_change_affects_only_referencing_device
- tests/commander/integration/test_hot_reload.py::test_apply_conflict_when_disk_changed_externally
- tests/commander/integration/test_hot_reload.py::test_candidate_is_single_use_and_unknown_id_not_found
- tests/commander/integration/test_hot_reload.py::test_concurrent_pushes_serialize_and_single_candidate
- tests/commander/system/test_b1_effective_options.py::test_model_only_options_and_model_default_port_drive_real_modbus
- tests/commander/system/test_b1_effective_options.py::test_device_override_wins_over_model_default_real_modbus
- tests/commander/system/test_b1_effective_options.py::test_port_falls_back_to_protocol_default_502
- tests/commander/system/test_b1_effective_options.py::test_model_option_change_rotates_fingerprint_and_reload_routes_new_session
- tests/commander/system/test_grpc_end_to_end.py::test_config_snapshot_apply
- tests/commander/system/test_grpc_end_to_end.py::test_config_revision_conflict
- tests/commander/system/test_grpc_end_to_end.py::test_config_snapshot_scopes
- tests/commander/system/test_lifecycle.py::test_start_invalid_config - ...
- tests/commander/system/test_lifecycle.py::test_restart_new_config - Ru...
- tests/commander/system/test_r1f1_crash_recovery.py::test_independent_process_crash_recovers_unknown_and_blocks_device
- tests/commander/system/test_service_e2e_real_stack.py::test_full_command_chain_real_modbus_and_jetstream
- tests/commander/system/test_service_e2e_real_stack.py::test_restart_recovers_executing_command_as_unknown
- tests/commander/system/test_service_e2e_real_stack.py::test_reload_unchanged_config_is_noop_and_restart_not_required
- tests/commander/system/test_concurrency.py::test_expiration_and_unknown[_tz01_to_blackhole]
- tests/commander/system/test_crash_recovery.py::test_crash_recovery[_tz01_to_blackhole]
- tests/commander/system/test_lifecycle.py::test_shutdown_unsettled_io[_blackhole_with_short_grace]

### A.3 Industrial Gateway（10）

- tests/industrial_gateway/component/test_claims_reload_and_rpc_lifecycle.py::test_apply_rebuild_invalidates_changed_server_claims_only
- tests/industrial_gateway/component/test_claims_reload_and_rpc_lifecycle.py::test_apply_identical_content_keeps_claims
- tests/industrial_gateway/component/test_config_snapshots.py::test_push_validates_cas_file_set_integrity_and_config
- tests/industrial_gateway/component/test_config_snapshots.py::test_candidate_scope_roundtrip_and_unknown_candidate
- tests/industrial_gateway/component/test_config_snapshots.py::test_apply_hot_reconfigures_desired_state
- tests/industrial_gateway/component/test_config_snapshots.py::test_apply_stale_revision_aborted_and_unknown_candidate_not_found
- tests/industrial_gateway/component/test_config_snapshots.py::test_apply_service_grpc_change_requires_restart
- tests/industrial_gateway/component/test_config_snapshots.py::test_apply_port_swap_hot_rebinds_listeners
- tests/industrial_gateway/component/test_config_snapshots.py::test_apply_bind_failure_is_partial_and_does_not_fake_runtime_revision
- tests/industrial_gateway/system/test_config_churn.py::test_repeated_apply_churn_preserves_sibling_and_clears_claims

### A.4 Data Generator（44）

- tests/data_generator/integration/test_gateway_integration.py::test_catalog_paging_filter_and_metadata
- tests/data_generator/integration/test_gateway_integration.py::test_update_then_read_roundtrip
- tests/data_generator/integration/test_gateway_integration.py::test_unclaimed_update_rejected
- tests/data_generator/integration/test_gateway_integration.py::test_per_item_results_and_duplicate_target
- tests/data_generator/integration/test_gateway_integration.py::test_batch_shape_and_claim_size_limits
- tests/data_generator/integration/test_gateway_integration.py::test_mtls_denies_unauthorized_writer
- tests/data_generator/integration/test_gateway_integration.py::test_gateway_lifecycle_rpc_and_revision_conflict
- tests/data_generator/integration/test_gateway_integration.py::test_dg_writer_uses_formal_update_rpc_end_to_end
- tests/data_generator/integration/test_gateway_integration.py::test_dg_writer_preflight_rejects_type_mismatch
- tests/data_generator/integration/test_grpc_server.py::test_all_fifteen_rpcs_callable
- tests/data_generator/integration/test_grpc_server.py::test_grpc_error_mapping
- tests/data_generator/integration/test_grpc_server.py::test_start_revision_conflict_rejected_in_body
- tests/data_generator/integration/test_grpc_server.py::test_cancelled_push_releases_config_lock
- tests/data_generator/integration/test_simulation_config_mtls.py::test_paired_simulation_configs_check_and_diagnose_over_real_mtls
- tests/data_generator/system/test_config_system.py::test_get_push_apply
- tests/data_generator/system/test_config_system.py::test_invalid_candidate_and_conflict
- tests/data_generator/system/test_config_system.py::test_restart_required
- tests/data_generator/system/test_config_system.py::test_apply_failure_recovery
- tests/data_generator/system/test_diagnose_system.py::test_simulate_replay_stages
- tests/data_generator/system/test_grpc_system.py::test_contract_operations
- tests/data_generator/system/test_grpc_system.py::test_contract_errors
- tests/data_generator/system/test_grpc_system.py::test_concurrent_start_atomic_target_reservation
- tests/data_generator/system/test_lease_system.py::test_p1_01_simultaneous_claim_exactly_one_wins
- tests/data_generator/system/test_lease_system.py::test_p1_03_disjoint_variables_coexist
- tests/data_generator/system/test_lease_system.py::test_p1_04_group_rejected_without_partial_occupancy
- tests/data_generator/system/test_lease_system.py::test_p1_08_expired_token_write_fenced_by_gateway
- tests/data_generator/system/test_lease_system.py::test_p1_09_gateway_restart_invalidates_all_tokens
- tests/data_generator/system/test_lease_system.py::test_p1_12_transfer_has_no_interleaved_stale_write
- tests/data_generator/system/test_lease_system.py::test_p1_13_concurrent_claim_storm_single_owner
- tests/data_generator/system/test_lease_system.py::test_p1_15_forged_owner_renewal_rejected
- tests/data_generator/system/test_lifecycle_system.py::test_gateway_fanout_partial_failure
- tests/data_generator/system/test_lifecycle_system.py::test_restart - p...
- tests/data_generator/system/test_replay_system.py::test_replay_1x_2x_order
- tests/data_generator/system/test_replay_system.py::test_equal_timestamps_and_empty_range
- tests/data_generator/system/test_replay_system.py::test_source_failure
- tests/data_generator/system/test_replay_system.py::test_stop_restart_boundary
- tests/data_generator/system/test_simulation_system.py::test_random_generation
- tests/data_generator/system/test_simulation_system.py::test_static_physics
- tests/data_generator/system/test_simulation_system.py::test_invalid_model_and_runtime_failure
- tests/data_generator/system/test_simulation_system.py::test_gateway_feedback
- tests/data_generator/system/test_simulation_system.py::test_bad_gateway_mapping
- tests/data_generator/system/test_simulation_system.py::test_fanout_multiple_gateways_and_variables
- tests/data_generator/system/test_simulation_system.py::test_conflicting_target_ownership
- tests/data_generator/system/test_simulation_system.py::test_duplicate_target_mapping_rejected

# wind-hub 业务设计与生产代码集中独立验收报告（2026-11-10）

- 验收分支：`docs/business-design-alignment-20261110`
- 验收 HEAD：`77746a3a9ae7b71f1e4f65d8ce2a8b1eeadefdfc`
- 基线：`main@367481254518a2d3d79ebeeb80a41944897a7e84`
- 验收方式：独立静态门禁 + 真实 gRPC/进程链路测试复验；设计矩阵不作为通过证据，所有结论以最终工作树实跑结果为准。
- 最终裁决：见第 9 节。

## 1. 变更清单与工作树状态

### 1.1 基线..HEAD 已提交变更（`git diff --name-status 36748125..77746a3a`）

| 类型 | 路径 | 说明 |
|---|---|---|
| R094 | `docs/design/CLI_软件设计_v1_19.md` → `CLI_软件设计_v1_21.md` | 修订并换版 |
| R097 | `docs/design/Commander_软件设计_v1_24.md` → `Commander_软件设计_v1_26.md` | 修订并换版 |
| R097 | `docs/design/Data_Generator_软件设计_v1_17.md` → `Data_Generator_软件设计_v1_19.md` | 修订并换版 |
| R094 | `docs/design/industrial_gateway_软件设计_v1_21.md` → `industrial_gateway_软件设计_v1_23.md` | 修订并换版 |
| R088 | `docs/design/wind-hub_总体逻辑架构设计_v1_0.md` → `..._v1_2.md` | 修订并换版 |
| M | `src/cli/business_commands.py` | +4 行：复用 `--request-id` 时强制 `--expires-at` 与 `--expected-revision` |
| M | `src/cli/plant_commands.py` | `--lease-ms` Typer 边界由 `min=1` 收紧为 `min=1000, max=60000` |
| M | `src/industrial_gateway/infrastructure/variable_leases.py` | 15 行：`update()` 无条件要求匹配的有效 claim，否则 `LeaseError("LEASE_LOST")`；删除无令牌旁路与 TARGET_CLAIMED 写入分支 |

生产代码合计仅 3 个源文件、11 增 10 删。冻结模块（Contracts v1_18、Core v1_31、Configuration v1_13）与 `src/sink_service/` 在分支 diff 中零变更（`git diff --name-only 36748125..HEAD | grep sink` 无输出）。

### 1.2 验收工作树（未提交）

验收过程中仅修改测试与测试支撑代码，共 13 个文件，生产目录零修改：

```
tests/cli/test_commander_e2e.py
tests/cli/test_gateway_e2e.py
tests/data_generator/integration/test_gateway_integration.py
tests/industrial_gateway/component/test_grpc_updates_and_claims.py
tests/industrial_gateway/component/test_mtls_authorization.py
tests/industrial_gateway/component/test_observability_rpc.py
tests/industrial_gateway/integration/test_modbus_matrix.py
tests/industrial_gateway/performance/test_injection_load.py
tests/industrial_gateway/system/test_config_churn.py
tests/industrial_gateway/system/test_fifty_devices.py
tests/industrial_gateway/test_variable_leases.py
tests/src/integration/test_cli_request_contract_real_grpc.py
tests/support/gateway_v1.py
```

### 1.3 五份设计文档第 1–2 章一致性

对 5 份换版文档与其直接前版做第 1–2 章文本提取比对，逐字一致；规范性变更仅发生在后续章节，不存在借换版篡改冻结基线的情况。

## 2. S1 静态门禁（最终工作树实跑）

| 门禁 | 命令 | 退出码 | 结果 |
|---|---|---|---|
| Ruff Lint（CI 作用域） | `python -m ruff check src tests scripts` | 0 | All checks passed! |
| Ruff Format（CI 作用域） | `python -m ruff format --check src tests scripts` | 0 | 740 files already formatted |
| mypy | `python -m mypy src` | 0 | Success: no issues found in 331 source files |
| Import Linter | `lint-imports` | 0 | Contracts: 15 kept, 0 broken |
| Proto 漂移 | `python scripts/generate_rpc.py --check` | 0 | 无重新生成差异，工作树跟踪文件数不变 |
| API 契约漂移 | `python scripts/generate_api_contract.py --check` | 0 | DRIFT CHECK: PASS |
| 严格 Marker 收集 | `python -m pytest --collect-only -q` | 0 | 2233 tests collected（基线 2226，净增 7 个测试函数） |

补充说明：对仓库根执行无作用域的 `ruff check .` 存在 15 个错误，全部位于 `typings/grpc-stubs/`（14）与 `ai_shared/agent_config/skills/merge-branch/scripts/merge.py`（1），不在 CI 门禁作用域（`src tests scripts`）内；在基线 worktree（36748125）执行同一命令得到完全相同的 15 个错误，判定 **UNRELATED 基线既有问题**，本分支未引入。

## 3. S2 契约 / 单元 / 组件层复验

| 范围 | 命令（摘选） | 结果 |
|---|---|---|
| Gateway 租约单元 | `pytest tests/industrial_gateway/test_variable_leases.py` | 6 项全 PASS（含新增无 claim 空实例 fail-closed、store 缺项逐项 NOT_FOUND） |
| gRPC 注入/租约组件 | `pytest tests/industrial_gateway/component/test_grpc_updates_and_claims.py` | 8 项全 PASS（真实 grpc.aio 服务器，无替身） |
| mTLS 授权组件 | `pytest tests/industrial_gateway/component/test_mtls_authorization.py` | PASS（注入方先 ClaimTargets 再 UpdateVariables） |
| 可观测性组件 | `pytest tests/industrial_gateway/component/test_observability_rpc.py` | PASS（两路指标流量均持令牌） |
| Commander 全量 | `pytest tests/commander` | 257 passed，1 项 NOT_RUN skip（现场窗口标记） |
| DG↔Gateway 集成 | `pytest tests/data_generator/integration/test_gateway_integration.py` | 9 项全 PASS（真实全栈 Gateway） |
| CLI 子进程 e2e | `pytest tests/cli`（定向） | 20 项相关用例 PASS |
| 真实 gRPC CLI 契约 | `pytest tests/src/integration/test_cli_request_contract_real_grpc.py` | 6 项全 PASS |

关键 fail-closed 断言（均为真实链路、非 Mock）：

1. 无 `lease_token` 且目标空闲 → `FAILED_PRECONDITION`，details 含 `LEASE_LOST`，读回 `quality=UNKNOWN` 且无 value 字段。
2. 持令牌批次混入未租约目标 → 整请求 `FAILED_PRECONDITION`，任何存储变更发生前围栏。
3. 伪造/过期/RELEASE 后旧令牌 → 一律 `FAILED_PRECONDITION`；变量值不变。
4. 无令牌空批 → `FAILED_PRECONDITION`；持有效令牌空批 → 合法 no-op，`results==[]`。
5. 数据注入通道与控制面隔离：组件用例断言 `harness.commander.calls == []`。
6. CLI 越界 `--lease-ms`（999/60001）与缺 `--expires-at`/`--expected-revision` → 退出码 2 且服务器零调用。

## 4. S3 集成 / 系统测试与追溯矩阵

### 4.1 UR→UC→ST→Marker→测试函数→断言→结果（本分支受影响契约）

| 设计来源 | UR/UC/场景 | 设计规定标识 | 实际测试函数（文件:行） | Marker | 关键断言 | 结果 |
|---|---|---|---|---|---|---|
| CLI v1_21 表14-2 | UR-09 / UC-14-S03 | `test_reused_request_id_requires_explicit_expiry` | `tests/cli/test_commander_e2e.py:83` 同名函数 | system, contract, safety, exception | 缺 expiry/revision 各退出 2；stdout 含参数名；`server.calls==[]`，不降级 GetDevice | PASS |
| CLI v1_21 表14-2 | UR-09 / UC-14-S03（重放稳定性） | 同条“同 ID 同内容不重复执行” | `test_commander_write_replayed_request_keeps_stable_revision_and_expiry`（同文件:119） | system, contract | 两次 SubmitCommand 的 request_id/expected_revision/expires_at 线载荷完全一致；GetDevice 调用 0 次 | PASS |
| CLI v1_21 表14-2 | UR-09 / UC-10-S02 | `test_lease_ms_range_and_token` | `tests/cli/test_gateway_e2e.py:112` 同名函数 | system, contract, security, exception | 999/60001 退出 2 且零 RPC；另见 :131 边界值 1000/60000 原样进入 ClaimTargets | PASS |
| CLI v1_21 UC-10 | 租约完整流转 | （既有闭环用例强化） | `test_gateway_closed_loop_list_update_read`（test_gateway_e2e.py:42） | system, contract | ClaimTargets 顺序严格 ACQUIRE→RELEASE；UpdateVariables 仅 1 次且携带令牌 | PASS |
| DG v1_19 ch14 | UR-03 / UC-03-S02 未取得租约直接注入 | `test_gateway_integration.py::test_unclaimed_update_rejected` | `tests/data_generator/integration/test_gateway_integration.py:184` 同名函数 | integration, reliability, network_failure（design 注记 system/safety/conflict） | 真实 Gateway 返回 FAILED_PRECONDITION；读回 UNKNOWN 且无 value | PASS |
| GW v1_23 表14-5 | UR-01 / UC-01-S02 无令牌且目标空闲 | `test_gateway_requires_lease_for_unclaimed_target`（追溯标识） | 组件 `test_tokenless_update_rejected_even_when_target_unclaimed`（test_grpc_updates_and_claims.py:152）；DG 集成 `test_unclaimed_update_rejected`；单元 `test_claim_fences_unleased_writer_and_allows_owner`（test_variable_leases.py:17，含空 claim 注册表 gw2） | component/unit/integration + safety | 三层各自断言拒绝且零变量改变 | PASS |
| GW v1_23 表14-5 | UR-01 / UC-01-S02 伪造/过期/未覆盖全批 | `test_gateway_requires_matching_lease_for_full_batch`（追溯标识） | `test_claim_acquire_protects_targets`（:170）、`test_batch_preflight_blocks_all_values_before_mutation`（单元:153）、`test_expired_token_cannot_modify_after_takeover`（:360）、`test_renew_release_allows_takeover`（:288） | component, unit, safety | 全批拒绝、零变更；过期接管后旧令牌失效；RELEASE 后可被 B 接管 | PASS |
| GW v1_23 R01-04 | 释放后令牌立即失效 | 设计 9.3 租约语义 | `test_update_then_read_roundtrip`（test_gateway_integration.py:100） | integration | RELEASE 后同令牌写入 FAILED_PRECONDITION | PASS |
| GW v1_23 14.2 | 50 设备/配置 churn 系统场景 | 系统矩阵 | `tests/industrial_gateway/system/test_fifty_devices.py`、`test_config_churn.py`（均改为先申领令牌） | system, reliability/performance | 50 设备注入与重启后重新 ACQUIRE 全通过 | PASS |
| GW v1_23 14.2 | Modbus 矩阵全栈 | 系统矩阵 | `tests/industrial_gateway/integration/test_modbus_matrix.py` | integration, system | `_seed()` 持令牌播种，矩阵断言不变 | PASS |

### 4.2 Sink Service 专项（只读检查，集成场景 BLOCKED）

- `src/sink_service/infrastructure/adapters/grpc_sink.py:86-92` 仍以无 `lease_token` 的 `UpdateVariablesRequest(gateway_id=..., values=portion)` 直调 Gateway；全 `src/sink_service/` 无任何 `ClaimTargets` 引用。
- 按冻结 Gateway 契约（GW v1_23 R01-04、Contracts v1_18），该调用现在必然收到 `FAILED_PRECONDITION/LEASE_LOST` 整批拒绝。
- Sink 目录下零测试覆盖 GrpcSink，无法在本分支内以测试证明端到端行为。
- 裁决：**跨分支集成阻断（BLOCKED）**，关联端到端场景标记 FAIL/BLOCKED；不得放宽 Gateway 冻结契约迁就 Sink。必需改造见第 7.1 节。
- 正向旁证：`src/data_generator/infrastructure/gateway.py` 的租约流程正确（ClaimTargets 于 :175/:230/:272，UpdateVariables 携带令牌于 :372），证明冻结契约在真实调用方侧可落地。

### 4.3 全量仓库复验（最终工作树，一次性完整运行）

命令：`python -m pytest -p no:warnings --tb=short -q`（无 marker 过滤的完整仓库运行，含默认门禁排除的 performance 用例）；耗时 1532.86 s。

pytest 汇总行原文：`12 failed, 2148 passed, 41 skipped, 10 deselected, 21 errors in 1532.86s`，退出码 1。收集 2233 项（与 `--collect-only` 一致；基线 2226，本分支净增 7）。全部 12 个 FAILED 与 21 个 ERROR 节点均已具名归类（见第 6 节），不存在未归类失败；本分支工作树涉及的全部测试目录（cli、industrial_gateway、data_generator、src/integration 等）无任何 RELATED 失败。

默认门禁经 `scripts/ci_gate.py` 以 marker 排除 performance 用例（pyproject 注释明示），即第 6 节 U-9 的 performance/load 用例不进入默认门禁，S4 性能验收以第 5 节独立实测为准。

## 5. S4 性能与可靠性实测（无 Mock 替代）

命令：`python -m pytest -m performance -s`（24 项全 PASS，性能 Marker 默认不入门禁，单独执行）。

- Gateway 注入吞吐：25183 points/s；延迟 p50=16.54 / p95=20.75 / p99=22.81 / max=159.35 ms；RSS 199268→199524 KB（净增约 256 KB，5 服务器 2500 点规模）。
- Commander：18 batches/s、184 points/s（现场节流设计内）；端到端 p50≈5448 / p99≈5480 ms；受理路径 p50=20.56 / p99=21.68 ms。
- SQLite 持久化刷盘：p50=50.6 / p99=51.8 ms，积压峰值 ≤31。
- 断链演练：100 次在途请求全部拒绝、零丢失；编解码器吞吐 46778–74764 samples/s。
- netem 损伤用例经 `unshare -rmn` 自行提权运行，未使用替身链路。

## 6. 失败 / 跳过 / NOT_RUN / 环境限制与基线对照

所有非通过项均在基线 worktree（36748125，`PYTHONPATH=/tmp/wt-baseline/src`）用同一命令复跑对照，证据如下：

| 编号 | 节点 | 现象 | 基线同跑 | 定性 |
|---|---|---|---|---|
| U-1 | `tests/industrial_gateway/integration/test_command_outcomes.py::test_concurrent_same_point_writes_serialize_at_commander`（:153） | FIFO 顺序断言失败（如 `[30,10,20,40] == [10,20,30,40]`），连续 3/3 复现 | 同样失败 | UNRELATED 既有生产缺陷（冻结 Commander 路径，超出修改边界） |
| U-2 | `tests/data_generator/system/test_lease_system.py::test_p1_09_gateway_restart_invalidates_all_tokens`（:312，断言于 :355） | Generation 未按 DG v1_19:1969 转入 FAILED（实得 RUNNING）；Gateway 侧围栏断言（:343）通过 | 同样失败 | UNRELATED 既有生产缺陷，DG 生产代码本分支零修改 |
| U-3 | `tests/core/architecture/test_rpc_boundary.py::test_pb2_only_in_allowed_zones` | 7 个 pb2 越界文件（清单见 7.4） | 同样失败、清单一致 | UNRELATED 结构债务 |
| U-4 | `tests/cli/test_local_tools.py::test_tcp_probe_open_closed_timeout` | `assert 'OPEN' == 'CLOSED'`（WSL2 本地端口时序） | 同样失败 | UNRELATED 环境 |
| U-5 | CLI 2 项 systemd 用例 | WSL2 缺 dbus-next/真实 systemd | 同样失败/跳过 | UNRELATED 环境 |
| U-6 | `tests/system` web fleet 系列 21 ERROR | fixture 抛 `Commander mTLS requires CA, client certificate and client key` | 同名称同数量 | UNRELATED 环境/夹具 |
| U-7 | `tests/system` reload 用例 | `Commander snapshot transaction is not confirmed` | 同样失败 | UNRELATED 既有 |
| U-8 | `tests/system/e2e/test_command_via_server.py` 系列 4 项（含 `test_write_command_reaches_device_and_readback`，PERMISSION_DENIED 授权信封） | revision/授权错误信封不匹配 | 同名称同数量 | UNRELATED 既有 |
| U-9 | `tests/src/performance/test_batch_throughput.py::test_publisher_pipeline_throughput_with_fake_transport`（:143，标记 `performance`+`load`） | 全量套件并发负载下 `assert 220 == 300`（有界发布管线在机器争用中未按时排空） | 基线隔离运行通过；最终树隔离重跑 5/5 通过（0.47–0.53 s/次） | UNRELATED 负载敏感抖动；测试仅导入 `collector.infrastructure.publisher`/`core`/`contracts`，与本分支 3 个改动文件零交集；且该 marker 默认被 ci_gate 排除 |

21 个 ERROR 全部集中在 `tests/system/test_web_fleet_config_e2e.py`（fixture 统一抛 `Commander mTLS requires CA, client certificate and client key`），与基线同文件同数量；日志中的 `Task was destroyed but it is pending!` 与 grpc servicer 异常为这些 ERROR 的伴随输出，不构成额外失败节点。

NOT_RUN（环境不具备，未以 Mock 冒充）：

1. 真实 PLC / ADS / IEC104 现场窗口（`COMMANDER_FIELD_WINDOW` 未开放）。
2. Sink↔Gateway 持令牌端到端集成（依赖 Sink 分支改造，见 7.1）。
3. containerd / iSulad / 真实 systemd 守护进程编排用例。

## 7. 移交 ChatGPT 的缺陷与修复建议

### 7.1【P1 跨分支阻断】Sink GrpcSink 未适配强制租约

- 位置：`src/sink_service/infrastructure/adapters/grpc_sink.py:83-92`（500 点分片循环内直调 UpdateVariables）。
- 根因：Gateway 冻结契约升级为“任何 UpdateVariables 必须携带覆盖全目标的有效 lease_token”，Sink 仍走旧无令牌路径。
- 最小复现：对当前 HEAD 启动真实 Gateway 与 GrpcSink，任一 `write_batch` → gRPC `FAILED_PRECONDITION`，details `LEASE_LOST`，整批无写入。
- 建议最小改造（由 Sink 分支实施，本分支不得改 Sink）：
  1. 每个 `gateway_id` 在分片写入前调 `ClaimTargets(ACQUIRE, owner_id=<sink 实例标识>, lease_ms∈[1000,60000])`，单 claim 目标 ≤5000（与现有 500/片对齐，可按片或按 gateway 聚合申领）；
  2. `UpdateVariablesRequest` 携带返回的 `lease_token`；
  3. 收尾（含异常路径）调 `ClaimTargets(RELEASE, lease_token=...)`，参考 `src/cli/plant_commands.py` 与 `src/data_generator/infrastructure/gateway.py` 的既有模式；
  4. 对 `TARGET_CLAIMED`（申领冲突）与 `LEASE_LOST`（续约失败）分别走不可重试与安全重协商路径，禁止盲目重发；
  5. 补齐 GrpcSink 组件测试（当前零覆盖）。

### 7.2【P2】Commander 同点并发写 FIFO 失序

- 位置：`tests/industrial_gateway/integration/test_command_outcomes.py:153` 所验证的 Commander 转发路径。
- 复现：`pytest tests/industrial_gateway/integration/test_command_outcomes.py::test_concurrent_same_point_writes_serialize_at_commander`，确定性失败（3/3），基线一致。
- 建议：排查 Commander 接收队列到转发器的调度顺序保证；属冻结 Commander 范围，须按冻结流程修复而非改测试。

### 7.3【P2】Data Generator 重启后 Generation 未转 FAILED

- 位置：`tests/data_generator/system/test_lease_system.py:312`（断言 :355）；设计依据 DG v1_19 第 1969 行。
- 现象：Gateway 令牌失效围栏正确（:343 通过），但 DG 生命周期状态滞留 RUNNING。
- 建议：在 Generation 状态机对租约失效/重启事件补 FAILED 迁移；DG 生产代码不在本分支修改边界内。

### 7.4【P2 结构债务】pb2 引用越过允许边界（7 处，基线既有）

`collector/assembly.py`、`industrial_gateway/infrastructure/{observability,diagnostics,runtime,variable_leases}.py`、`industrial_gateway/infrastructure/config/snapshots.py`、`industrial_gateway/infrastructure/commander/forwarder.py`。
注意：`variable_leases.py` 的 pb2 导入在基线第 10 行已存在，非本分支引入。建议统一经 adapter/codec 边界收敛或更新 `_ALLOWED_PB2_PREFIXES` 架构白名单。

### 7.5【P3 文档缺陷】

1. `docs/design/CLI_软件设计_v1_21.md:1753` 仍写“《Contracts 软件设计 v1_9》”，应为 v1_18（GW v1_23:770 已明确否定 v1_9 框架表述）。
2. `docs/design/Commander_软件设计_v1_26.md` 第 14 章 5 处引用 `tests/src/commander/`（行 2237、2726、2765、2807、2824），实际目录为 `tests/commander/`（`tests/src/commander` 不存在）。

## 8. 测试与夹具变更清单及“断言未弱化”证明

| 文件 | 变更性质 | 断言强度证明 |
|---|---|---|
| `tests/support/gateway_v1.py` | 新增 `claim_for_injection` 助手（真实 ClaimTargets ACQUIRE 并 assert accepted） | 纯增量助手，无既有断言改动 |
| `test_variable_leases.py` | 错误码断言 TARGET_CLAIMED→LEASE_LOST（跟随冻结契约）；新增空注册表 gw2 用例与 store 缺项逐项 NOT_FOUND 用例 | 旧场景改为更强的 fail-closed 语义；NOT_FOUND 覆盖从 gRPC 层移到单元层保持不断保 |
| `test_grpc_updates_and_claims.py` | 读回/满批用例先申领；新增无令牌空闲目标拒绝用例；新增 `commander.calls == []` 隔离断言 | 全部为新增前置与新增断言，无删除；missing_point 从“逐项失败”升级为“整批围栏”断言 |
| `test_mtls_authorization.py` | 注入前 ClaimTargets | 授权拒绝断言原样保留 |
| `test_observability_rpc.py` | 两路写入换持令牌 | 指标断言不变 |
| `test_modbus_matrix.py` | `_seed()` 持令牌；import 排序 | 矩阵协议断言零改动 |
| `test_injection_load.py` | 5 服务器各预申领 500 点令牌，按批携带 | 性能指标与通过门槛不变 |
| `test_config_churn.py` | hammer 持预申领令牌；重启后重新 ACQUIRE（owner churn-2）替代无令牌写 | 替换的是被新契约禁止的操作方式，churn 断言不变 |
| `test_fifty_devices.py` | 逐服务器申领 | 规模/读回断言不变 |
| `test_gateway_integration.py`（DG） | 无令牌写预期改为 FAILED_PRECONDITION；空批双态（无令牌拒绝/持令牌 no-op）；新增同名设计标识用例 | 断言从“容忍”升级为“拒绝”，并新增读回 UNKNOWN/无 value 的副作用证明 |
| `test_gateway_e2e.py` | 新增租约边界与流转用例；既有闭环补 ACQUIRE→RELEASE 顺序断言 | 纯增强 |
| `test_commander_e2e.py` | 新增参数边界与重放稳定性两用例 | 纯增强；重放用例逐字段比对两次线载荷 |
| `test_cli_request_contract_real_grpc.py` | 真实 gRPC 用例补显式 revision/expiry | 这是本分支生产校验的正确用法适配，契约断言本身加强（禁止隐式补 revision） |

全部分支内测试失败的处置原则：能归因到租约/幂等收紧的，更新测试以表达冻结契约要求的更强不变量；没有任何 `skip`/`xfail`/`ignore`/`noqa` 被用于掩盖问题，没有任何既有数值/顺序/副作用断言被删除或放宽。UNRELATED 项保持原样失败并在第 6 节登记。

### 8.1 边界合规检查

- 冻结模块 diff：Contracts/Core/Configuration 设计与代码零变更（`git diff --stat 36748125..HEAD -- src/contracts src/core src/configuration` 无输出）。
- Sink 边界：`src/sink_service/` 零变更，仅只读检查。
- 生产修改仅限任务书允许的 3 个文件；工作树无生产文件改动。
- 未创建 PR、未 rebase、未 force push、未合并 main；未执行 push。

## 9. 最终裁决

**PASS WITH LIMITATIONS。**

裁决依据：

1. **RELATED 失败为 0。** 最终工作树一次性全量复验（2233 收集、2148 passed、41 skipped、10 deselected、12 failed、21 errors）中的全部 33 个非通过节点，均经基线 worktree（36748125）同命令复跑或隔离复跑证明为基线既有/环境/负载问题（U-1～U-9），没有任何一项可归因于本分支的 3 个生产文件或 13 个测试文件改动。
2. **本分支受影响契约全部以真实链路验证通过。** 租约 fail-closed（R01-04）在单元、组件、真实 gRPC 集成、50 设备系统场景四个层级均有同名/可追溯测试与断言证据；Commander 幂等参数围栏与 CLI 租期边界经真实子进程 e2e 验证，零 RPC 本地拒绝语义成立；S1 七个静态门禁全绿。
3. **限制项不解除但需显性跟踪。** P1 跨分支阻断：Sink GrpcSink（grpc_sink.py:86-92）仍为无令牌调用，在当前冻结 Gateway 契约下必然被拒，Sink 侧集成场景 BLOCKED，须由 Sink 分支按 7.1 改造并补测试后另行验收；U-1～U-3 为需走冻结流程修复的既有生产/架构缺陷；真实 PLC/ADS/IEC104 现场窗口与真实守护进程编排保持 NOT_RUN，不以 Mock 顶替。
4. **边界合规。** 冻结模块与 Sink 零改动；未新增任何 ignore/noqa/skip/xfail 掩盖问题；未弱化既有断言；未 push、未 PR、未合并。

后续动作：本报告与 13 个测试文件改动随分支提交后，由用户手动推送；7.1–7.5 缺陷清单移交 ChatGPT 排期修复。

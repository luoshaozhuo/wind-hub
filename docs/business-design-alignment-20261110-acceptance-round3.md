# 业务设计对齐 20261110 — 第三轮集中独立复测（R3）

- 分支：`docs/business-design-alignment-20261110`
- 生产 HEAD：`7f44ec44a8642eec1c54332ed389b8d9957a8c8c`
- R2 基线：`1ad0dbb297fcd9617ffbd5ecd6957e81e667aaba`
- R3 区间：`1ad0dbb2..7f44ec44`，28 commits，14 文件，+523/−187，**零测试文件由生产侧改动**
- 裁决：**PASS WITH LIMITATIONS，RELATED = 0**
- 日期：2026-11-11

## 0. 结论摘要（任务书四个必答问题）

1. **FIFO 缺陷是否闭环：是。** 生产侧以"出队同步安装前驱完成令牌 + `asyncio.shield` 等待"取代旧的非公平键锁，同目标受理序严格等于 Gateway 捕获序；旧测试把四条 TCP 连接建立序当捕获序的错误锚点已修正，新增 3 个前驱链直接测试，复跑稳定。
2. **pb2 七处越界是否清零：是。** 6 个原越界文件 `_pb2` 引用全部为 0，`diagnostics.py` 物理迁入白名单包 `infrastructure/grpc/`；架构白名单零改动，六个重构模块零 `noqa`/`type: ignore`，无代理模块。
3. **内部接口改造是否引发回归：无。** 仅 1 处旧单元测试直接构造 pb2 入参（`query_events`），按 S3 授权改为内部关键字 API；全量 2228 收集用例中无任何 R3 相关失败。
4. **R2 基线是否新增 RELATED 问题：没有。** 全量 8 failed + 21 errors 与 R2 清单逐一同名同因，并在 R2 基线 worktree 新鲜复现，全部 UNRELATED；R2 记录的 throughput 抖动本轮已根治。

冻结模块（Contracts/Core/Configuration）与 Sink Service 在 R3 区间零改动（`git diff --name-only` 扫描为空）。

## 1. S1 静态门禁（全部最终工作树新鲜复跑，退出码 0）

| 门禁 | 命令 | 退出码 | 结果 |
| --- | --- | --- | --- |
| Ruff Check | `ruff check src tests scripts` | 0 | All checks passed |
| Ruff Format | `ruff format --check src tests scripts` | 0 | 742 files already formatted |
| Mypy | `mypy src` | 0 | Success: no issues found in 333 source files（R2 为 331，新增两个 grpc 适配器模块） |
| Import Linter | `lint-imports` | 0 | Contracts: 15 kept, 0 broken |
| pb2 架构边界 | `pytest tests/core/architecture/test_rpc_boundary.py -q` | 0 | **4 passed**（R2 为 1 failed/3 passed） |
| Proto 漂移 | `python scripts/generate_rpc.py --check` | 0 | 无生成物差异 |
| API 漂移 | `python scripts/generate_api_contract.py --check` | 0 | DRIFT CHECK: PASS（44 paths） |

R3 生产提交引入 5 处纯格式缺陷（I001 导入序×2、换行/空行×3），已由复测方以 `ruff check --fix`/`ruff format` 修复，逐行复核仅换行/导入/空行变化、零语义改动。未修改架构白名单，未使用 noqa/ignore 掩盖问题。

七处越界独立核验（`grep -c _pb2`）：

| R2 越界文件 | R3 状态 |
| --- | --- |
| `infrastructure/observability.py` | 0 引用，内部 `Observation/EventRecord/MetricRecord/EventPage` dataclass |
| `infrastructure/diagnostics.py` | 物理移动到白名单 `infrastructure/grpc/diagnostics.py` |
| `infrastructure/config/snapshots.py` | 0 引用，返回普通快照记录 |
| `infrastructure/runtime.py` | 0 引用，`ApplicationResult` 等内部 DTO |
| `infrastructure/commander/forwarder.py` | 0 引用，proto 映射全部下沉 `grpc/commander_client.py` |
| `infrastructure/variable_leases.py` | 0 引用，域内 `LeaseClaimRequest`/结果类型 |
| `collector/assembly.py` | 0 引用，注册逻辑收敛到 `collector/infrastructure/grpc/registration.py` 工厂 |

## 2. S2 FIFO 专项：七项性质逐条裁决

生产机制（`forwarder.py`）：worker 出队后、任何 await 之前，同步读取并替换 `_target_tails[(device, point)]` 完成 future；随后 `await asyncio.shield(predecessor)`，`finally` 中置位完成令牌并交接尾指针。FIFO 锚点是**队列出队序**，与 TCP 连接建立序、锁竞争序无关。

| # | 要求性质 | 证据（最终 HEAD 新鲜运行） | 裁决 |
| --- | --- | --- | --- |
| 1 | 同目标捕获/出队序 == Commander 受理序 | 新增 `test_same_target_acceptance_order_matches_capture_order`（8 空闲 worker + 0.15s 延迟，受理值严格 `[10,20,30,40]`）；集成测试锚点改为 forwarder 捕获记录按 `command_id` 联接 Commander 受理记录，断言受理值序列 == 捕获值序列 | PASS |
| 2 | 不得以 TCP 并发连接创建序替代捕获序 | 旧锚点 `[c.value for c in calls] == [10,20,30,40]` 已删除；新锚点基于 `forwarder.records` 插入序，10 次复跑 10/10 通过（旧锚点 3 次内必翻车） | PASS |
| 3 | 不同目标保持并行 | 既有 `test_same_target_serialized_other_targets_concurrent`：D02 不等 D01 队列，max_inflight 同目标仍为 1 | PASS |
| 4 | 单目标最大在途 = 1 | 组件 + 集成两层均断言 `max_inflight("D01", cmd_uint) == 1` | PASS |
| 5 | 后继 worker 取消不取消前驱、不死锁等待链 | 新增 `test_successor_cancellation_shields_predecessor_and_chain_unblocks`：后继挂在 shield 上被 cancel → REJECTED("not submitted")，前驱照常 ACCEPTED，送达恰 1 次，新事件经同一 key 正常受理证明链路无死锁；5 次复跑稳定 | PASS |
| 6 | 超时/断连/UNKNOWN 不触发重复提交 | 既有 `test_rejected_is_distinct_from_unknown_and_not_retried`、`test_settlement_uncertain_rpc_failure_stays_unknown`、`test_expired_while_queued_never_submits`、`test_get_command_result_passes_through_without_resubmit`（本轮流化 DTO 断言） | PASS |
| 7 | Stop 正确清理队列/任务/前驱令牌 | 既有 `test_stop_marks_inflight_unknown_and_drains_queued_rejected` + 新增 `test_stop_clears_target_tails_and_workers`（断言 `_target_tails` 清空、workers 清空、queue empty、pending=0） | PASS |

未削弱任何既有断言：4 次提交、max_inflight=1、4 个唯一 command_id、不重复提交、command_id 权威性全部保留。

## 3. S3 pb2 重构回归（六区域，真实 gRPC 链路）

| 重构区域 | 覆盖测试 | 结果 |
| --- | --- | --- |
| Collector 服务注册（`registration.py` 工厂） | `tests/collector` 全树（含 8 个 system 文件） | 全过 |
| Gateway ClaimTargets/UpdateVariables（域租约对象） | `test_grpc_updates_and_claims.py`、`test_claims_reload_and_rpc_lifecycle.py`、DG `test_lease_system.py` 13 用例 | 全过 |
| Config Get/Push/Apply（内部快照记录） | `test_config_snapshots.py`、`test_config_churn.py` | 全过 |
| GetMetrics/GetEvents（内部计量/事件存储） | `test_observability_rpc.py`；旧单元测试 `test_observability.py` 按授权适配 `query_events(filter_text=..., page_size=...)` 关键字 API 与 `EventPage` DTO，移除 pb2 import，过滤/分页/环形上限断言一条未减 | 全过 |
| Diagnose（迁入 grpc 包） | `tests/collector/system/test_collector_diagnose_system.py` 及 Gateway 诊断相关组件测试 | 全过 |
| Gateway→Commander 转发（`commander_client.py` 承载全部 proto） | `test_forwarder.py` 28 用例（真实 grpc.aio FakeCommanderService，无 Mock）、`test_command_outcomes.py` 6 用例 | 全过 |

线协议兼容性：真实 gRPC 请求/响应仍由白名单适配器按冻结 Contracts v1_18 装配，Proto/API 两项漂移检查零差异；`CommandQuery` 收窄为仅含 `state` 是有意设计，唯一生产消费方 `grpc/diagnostics.py` 只用 `.state`、command_id 取自 forwarder 记录，已逐行核实。

`pytest tests/industrial_gateway tests/collector -q`：**314 passed, 2 skipped, 0 failed**（217.84s）。
`pytest tests/data_generator tests/collector -q`：**314 passed, 2 skipped, 0 failed**（437.29s，含新租约用例与 conftest 钩子回归）。

## 4. S4 增补测试与全量对账

### 4.1 RENEW 两分支直接测试（闭环 R2 GAP R2-03）

故障夹具新增 `claim_reject` 行为钩子（非 RPC 异常的权威业务拒绝，与 `claim_abort` 网络级失败正交），新增两个系统测试：

- `test_renew_timeout_quarantines_without_lease_loss_inference`：RENEW 连续 UNAVAILABLE 期间 Generation 保持 RUNNING、续租线程存活继续探测、ACQUIRE 恰 1 次、无伪造 RELEASE、隔离期零新原生 RPC；故障在服务端 TTL（10s）内清除后，**同一 token** 续租成功并恢复写入。两轮复跑稳定。
- `test_renew_authoritative_rejection_eventually_fails_generation`：RENEW 权威 `accepted=false/LEASE_LOST` 后续租线程立即退出（再等一个周期 RENEW 数不增长，无重试风暴），Generation 终局 FAILED；不重新 ACQUIRE；失败清理至多 1 次 RELEASE 且只持原始 token。

故障窗口设计说明：首版测试把网络故障注入 7.2s，实测恢复续租被服务端按 TTL 权威围栏——这是生产正确行为而非缺陷；本测试意图是"短于 TTL 的瞬断可恢复"，故收窄至 4.0s，并在注释中写明两条语义的边界，避免把终局判死路径误挂到超时分支。

### 4.2 吞吐抖动根治

根因比 R2 记录的"墙钟排空"更深一层：realtime 默认 `max_staleness_ms=500`，全仓并发负载下排队超 500ms 的批次被生产 staleness 逻辑**合法判过期丢弃**（不触达传输层），叠加 `stop()` 内 2s `queue.join()` 生产保护超时，共同造成 220/300 型假失败。修法（仅测试侧）：

1. 测试配置 `max_staleness_ms=30_000`，让过期淘汰路径与本测量解耦，`dropped == 0` 断言保持不变；
2. 先轮询实际送达数 `fake.calls == batches`（确定性完成条件，10s 预算）再 `stop()`，并以 `try/finally` 保证停止；
3. 生产指标、吞吐量口径与全部断言零改动。

修复后连续 **12/12 通过**，且消除了偶发 "Event loop is closed" 析构噪声；R2 基线 worktree 同位置计数为 0 噪声但存在墙钟假失败，对照成立。

### 4.3 全仓 pytest 与基线对账

命令：`pytest tests -q`（最终工作树，1586s）。统计：**2158 passed, 8 failed, 41 skipped, 21 errors, 10 deselected（2228 收集）**。

| 失败/错误 | R3 数量 | R2 基线 worktree 新鲜复跑 | 分类 |
| --- | --- | --- | --- |
| `cli/test_local_tools.py::test_tcp_probe_open_closed_timeout` | 1 | 同名失败（WSL2 回环半开连接行为，断言 CLOSED 实测 OPEN） | UNRELATED |
| `cli/test_system_real.py` systemd×2 | 2 | 同名失败（WSL2 无真实 dbus/systemd 权限环境） | UNRELATED |
| `system/e2e/test_command_via_server.py` ×4 | 4 | 同名失败（全栈子进程环境，R1/R2 同清单） | UNRELATED |
| `system/reload/test_server_config_apply.py` ×1 | 1 | 同名失败（同上环境族） | UNRELATED |
| `system/test_web_fleet_config_e2e.py` errors | 21 | 基线同文件 **1 passed/21 errors** 完全一致（mTLS CA/证书夹具缺失，`RuntimeError: Commander mTLS requires CA, client certificate and client key`） | UNRELATED |
| R2 throughput 抖动 | 0 | 本轮根治，12/12 确定性通过 | 已闭环 |
| R2 FIFO 失败 | 0 | 锚点修正 + 生产机制正确，10/10 通过 | 已闭环 |
| R2 pb2 架构门禁 | 0 | 4/4 PASS | 已闭环 |

基线复跑命令：`PYTHONPATH=/tmp/wt-r2/src pytest <8 个失败用例>` → 8 failed 同名；web_fleet 单文件 → 21 errors 同数。R3 区间为文档 + Gateway/Collector 重构，与上述环境失败路径零代码交集。**RELATED = 0，UNKNOWN = 0，NOT_RUN 维持 R2 清单**（真实 PLC/ADS/IEC104 链路、Sink↔Gateway 持令牌集成、真实守护进程，本机环境不具备）。

## 5. 文档核验

- 第 1–2 章原文接力比对（脚本 `/tmp/extract_ch12_r3.py`，退出码 0）：CLI v1_23→v1_24、Commander v1_27→v1_28、DG v1_20→v1_21、GW v1_23→v1_24、GW v1_24→v1_25 五跳全部 **IDENTICAL**。
- R2 缺口 R2-07（DG ch14 漏三个测试文件）：v1_21 已补 `test_lease_system/test_observability_system/test_error_classification_system` 矩阵行，且 p1_09 关联 9.3.2/9.5 权威判失契约——**闭合**。
- R2 缺口（Commander 四处 `tests/src/commander` 陈述语气，P3）：v1_28 四处均已带"目标目录/迁移后/历史测试仍在 tests/commander"限定语，与 2237 行总 caveat 一致——**闭合**。
- GW v1_25 新增 protobuf 适配器边界条款（:1679-1685）与生产实现逐字一致：pb2 仅限 `infrastructure/grpc/`，内部边界用普通 Python 对象，九指标含义与事件排序不得因重构改变。
- 陈旧引用扫描：当前 R3 文档无过期版本引用；Sink v1_36 中的 Contracts v1_15/v1_6 是其冻结基线内有意引用，不属本分支范围。

## 6. 复测方改动清单（仅测试/夹具/格式/报告，无生产语义改动）

生产文件 5 个（纯 ruff 格式，+5/−14 量级）：`collector/assembly.py`、`commander/forwarder.py`（I001）、`config/snapshots.py`、`grpc/data_server.py`、`observability.py`。

测试侧 6 个：

1. `tests/industrial_gateway/component/test_forwarder.py`：DTO 断言适配 + 3 个前驱链新用例（+140 行）；
2. `tests/industrial_gateway/integration/test_command_outcomes.py`：FIFO 锚点改为捕获序联接，原四项断言全保留；
3. `tests/industrial_gateway/unit/test_observability.py`：pb2 入参改内部关键字 API；
4. `tests/data_generator/conftest.py`：新增 `claim_reject` 夹具钩子（6 行）；
5. `tests/data_generator/system/test_lease_system.py`：RENEW 两分支系统测试（+176 行，2 新用例）；
6. `tests/src/performance/test_batch_throughput.py`：确定性排空 + staleness 预算解耦。

新增可执行用例合计 5 个。未改架构白名单、未碰冻结模块与 Sink、未 rebase/force push/merge main。

## 7. 遗留事项（沿 R2，不阻塞本轮裁决）

| 项 | 性质 | 处置 |
| --- | --- | --- |
| Sink `grpc_sink.py` 无 lease_token 调 UpdateVariables（R1 P1 跨分支阻断） | 跨分支生产缺陷 | 待 Sink 分支改造，本分支无 Sink 改动权 |
| 8 failed + 21 errors 环境族 | UNRELATED | WSL2/mTLS 夹具/全栈子进程环境，须在合格 CI 或真机环境复核 |
| 真实 PLC/ADS/IEC104、持令牌端到端、真实守护进程 | NOT_RUN | 环境不具备，维持 NOT_RUN，不得据此宣称完整验收 |
| typings/ai_shared 15 个无作用域 ruff 基线噪声 | UNRELATED | CI 门禁作用域为 `src tests scripts`，不在本轮范围 |

## 8. 最终裁决

**PASS WITH LIMITATIONS，RELATED = 0。** FIFO 缺陷闭环、pb2 七处越界清零、内部接口改造零回归、R2 基线无新增相关问题；R2 的 FIFO（P2）、pb2 结构债务、RENEW 测试 GAP、两处文档缺口、throughput 测试质量（P2）五项遗留全部闭环。限制性仅来自与本分支无关的环境族失败、Sink 跨分支 P1 阻断和真实链路 NOT_RUN。

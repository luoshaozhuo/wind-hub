# Commander 第五轮独立验收报告 — N1 串行批量写入安全性

- 分支：`feat/commander-four-phase-rewrite`
- ChatGPT 整改基线：`11230ea3`（含两个提交：`12a3a2aa` 状态机扩展、`11230ea3` N1 修复）
- 验收提交：`ad5d342f`（已推送，未合并 main；推送后本地=远程）
- 设计依据：`docs/Commander_软件设计_v1_1.md`
- 验收日期：2026-11-01（WSL2，wind-hub conda 环境）

## 1. Git 基线、最终 HEAD、提交与工作区状态

| 项 | 值 |
|---|---|
| fetch 后远程 HEAD（整改前） | `11230ea3`，本地一致、工作区 clean |
| ChatGPT 整改 | `12a3a2aa`（domain/command.py +1 行：EXECUTING→INTERRUPTED）、`11230ea3`（queued_executor.py +70/-21） |
| 验收提交 | `ad5d342f7b41d0ee9c36f3a51823579da9f0d1fd` 已 push |
| 工作区 | 推送后 clean；无新建分支/PR/rebase/force push，未碰 main |
| 生产代码改动 | 仅 `ruff format` queued_executor.py；与 `11230ea3` 的 **AST 比对完全相同**，零语义改动 |

## 2. 静态检查

| 门禁 | 结果 |
|---|---|
| `ruff check src/commander tests/commander` | **PASS**（All checks passed） |
| `ruff format --check src/commander tests/commander` | **PASS**（39 files；ChatGPT 两文件格式漂移由验收方 format 收敛） |
| `mypy src/commander tests/commander` | **PASS**（Success: 39 source files） |
| `lint-imports` | **PASS**（11 kept, 0 broken） |

无 noqa、无 type: ignore 新增、无门禁调整。

## 3. N1 原 strict xfail 复测

`test_serial_batch_midpoint_exception_keeps_earlier_confirmed_evidence`：整改后 **XPASS(strict)**。已按规则**移除 xfail 标记转为正式测试**并加严断言：

- p0 CONFIRMED_SUCCESS → SUCCEEDED；p1 CONFIRMED_FAILURE → FAILED（outcome 保留）；
- p2 异常 → UNKNOWN；p3 确定未触达 → **INTERRUPTED / NOT_SENT（error_code BATCH_ABORTED_NOT_SENT）**；
- `one_calls == ["p0","p1","p2"]`（每点至多一次物理写、顺序=配置顺序、p3 未触达）、无 write_many。

修复机制审查（`queued_executor.py`）：
- `_write_group` 串行逐点 `write_one`，结果**返回后才** append 进 progress；异常包成 `_SerialWriteError(results=已确认前缀, cause)`；返回 UNKNOWN outcome 同样中止。
- `_serial_terminal_changes`：已确认前缀逐点裁决（SUCCESS/FAILURE/UNKNOWN）；中断点（index==len(confirmed)，异常路径）强制 UNKNOWN；其后点仅在非 unsettled 时判 INTERRUPTED/NOT_SENT。
- 超时/取消路径（`wait_for(shield(native))`）传 `unsettled=True`：**中断点及全部后续点判 UNKNOWN**——因为 shield 使底层 I/O 不被取消、任务继续执行，NOT_SENT 会是伪证。该判定正确处理了任务书点名的竞态：native 经 `_block_late` 登记、许可保留、物理隔离延迟到 native 集合清空（`isolate_device` 见 `_native_by_device` 闸门），无提前释放；迟到结果由 done_callback 吞掉，**不会产生第二次 T3**。

## 4. 故障矩阵逐项结果

| 场景 | 预期 | 实测 | 状态 |
|---|---|---|---|
| 第 2 点异常，第 1 点成功 | SUCCEEDED/UNKNOWN/INTERRUPTED | 一致；one_calls=[p0,p1] | **CLOSED** |
| 第 2 点异常，第 1 点已确认失败 | FAILED/UNKNOWN/INTERRUPTED | 一致（拒绝证据不升级） | **CLOSED** |
| 第 1 点即异常（ConnectionError） | UNKNOWN/INTERRUPTED/INTERRUPTED | 一致；one_calls=[p0] | **CLOSED** |
| 全部成功 | 全部 SUCCEEDED | 既有混合用例+全量性能 10000 点对账一致 | **CLOSED** |
| 中间点返回 UNKNOWN outcome | SUCCEEDED/UNKNOWN/**NOT_SENT** | 实际 p2=**UNKNOWN** | **OPEN（N1-b，P2）** |
| write_one 持续阻塞直至超时 | 前序保持；未确认点 UNKNOWN | p0 SUCCEEDED；p1/p2 UNKNOWN；放行后 p2 真实写入、终态不被覆盖 | **CLOSED** |
| 协议连接中断 | 区分已确认/不确定/未发送三档 | ConnectionError 参数行 + 真实 Modbus 无响应从站双证据 | **CLOSED（受 N1-b 同根影响）** |
| T3 事务失败 | 整批回滚、恢复 UNKNOWN、不重发 | 4 点全 durable EXECUTING；one_calls=[p0,p1,p2]；重开 recover 全 UNKNOWN | **CLOSED** |

矩阵通用项：每点物理写恰好一次（one_calls 与中断点前缀精确相等）；执行顺序严格等于配置顺序；以实际返回/异常证据裁决而非异常类型一刀切；已确认终态未被 UNKNOWN 覆盖；未调用点无一标 SUCCEEDED。

**N1-b（OPEN，P2，strict xfail 在案 ×2）**：
- 现象：中间点 `write_one` **返回** `WriteOutcome.UNKNOWN` 时，`_write_group` 先 append 该点再抛 `_SerialWriteError`，`exc.results` 长度含当前点；`_serial_terminal_changes` 中后续第一个点命中 `index == len(confirmed)` 被强制 UNKNOWN，但其 `write_one` 从未被调用，物理上确定未发送，应判 INTERRUPTED/NOT_SENT（v1_1 表 4-11、本轮矩阵第 5 行）。
- 证据：组件级 `test_serial_batch_returned_unknown_outcome_suffix_is_not_sent` strict xfail；真实 Modbus `test_real_modbus_unknown_mid_batch...`（写往不存在的 slave 3 → 无响应→驱动按连接级故障裁 UNKNOWN；服务端写 PDU=2、p2 寄存器回读原值）strict xfail，同一根因。
- 严重度评估：**保守方向误标**（NOT_SENT→UNKNOWN，可能已执行的口径），不会重复写、不会误报成功、不提前释放资源（隔离/保留照常触发），故定 **P2 而非 P1**。
- 修复建议（B 类状态裁决，验收方未擅改）：区分"异常中断点不在 confirmed 内"与"返回 UNKNOWN 的点在 confirmed 内"两种载体（如 `_SerialWriteError` 增加 unsettled_index），后者后续点走 NOT_SENT 分支。

状态机扩展核查：`EXECUTING→INTERRUPTED` 要求 outcome=NOT_SENT（validate_transition 配对校验），与设计书"现场从未触达"语义一致；已迁移对应单测（非法清单移除、合法清单新增）。

## 5. 真实 Modbus / SQLite / JetStream 证据

**真实 Modbus（pymodbus server，FC06 真实帧）**：
- 既有 4 用例全过：单点确认+回读、无 WRITE_MANY 批量逐点 FC 写回读、不可达 CONNECT_FAILED、无写权限帧前拒绝。
- 新增 `test_real_modbus_write_timeout_but_applied_stays_unknown_no_overwrite`（**PASS**）：p1 的 PDU 立即发出但确认延迟 1.0s（执行器 write_timeout=0.4s）；服务端计数=3（p1/p2 随后真实落寄存器），回读 p0/p1/p2 = 111.25/222.5/333.75 全部生效；durable 终态 p1/p2 保持 UNKNOWN 且 native 收敛后不被覆盖——"实际写入成功但确认丢失"安全语义在真实链路上闭环。
- N1-b 真实链路用例 xfail（见第 4 节），寄存器与 PDU 计数均作为物理证据固定。

**真实 SQLite WAL/FULL**：
- 矩阵异常/超时全部经真实 EventRepository（唯一 writer 线程）；T3 单事务 8 列 INSERT 路径、非法成员整批 ROLLBACK（仓储保持 operational）、sqlite3.Error 置 unavailable fail-closed，由 round4 三个故障注入用例继续守底，本轮全过。
- 新增串行批 T3 故障：故障后四点 durable EXECUTING、现场只写到中断点；关闭重开同一库 `recover()` → 四点全部 UNKNOWN（不重发现场）。

**真实 JetStream（自管 nats:latest -js 容器，5/5 PASS）**：
- 新增 `test_mixed_result_serial_batch_audit_events_complete_ordered_unique`：4 点批次终态 SUCCEEDED/FAILED/UNKNOWN/INTERRUPTED，消费端拉取 **12 事件**：4 ACCEPTED → 4 EXECUTING（单 T2 事务）→ 4 COMPLETED（单 T3 事务），event_id 全唯一（无重复投递），COMPLETED 的 state/outcome 逐点为 `[CONFIRMED_SUCCESS, CONFIRMED_FAILURE, UNKNOWN, NOT_SENT]`，与仓储查询投影完全一致；Nats-Msg-Id 去重与 PubAck 校验既有用例继续 PASS。

## 6. Commander 完整回归

`pytest tests/commander -q`：**127 passed、2 xfailed（均为 N1-b strict xfail：组件级+真实 Modbus）、0 failed**（约 38s，含真实 Modbus/JetStream/全栈 e2e）。

1 条 `PytestUnraisableExceptionWarning`（NATS 客户端 `Subscription._wait_for_msgs` 在 loop 关闭后被 GC）：跨测试 GC 归因（记到 Modbus 文件名上），单模块复跑不出现，第三方客户端任务销毁时机问题，非本轮生产代码引入，与 round4 同性质，记为已知 teardown 卫生项。

## 7. 性能防回归（同硬件同口径：100 设备×10 批×10 点，global_capacity=100）

三次实测：

| 指标 | round4 | **round5** | 设计目标 |
|---|---|---|---|
| 吞吐 | 383–419 批/s | **419 / 425 / 433 批/s**（4192–4332 点/s） | 1000 批/s |
| E2E P50/P99 | 238–259 / 247–275 ms | **230–238 / 239–249 ms** | 受理 P99≤100ms |
| SQLite 事务/批 | 3.10（3100/1000） | **3.10（3100，零变化）** | 3 个批事务 |
| writer 队列 | depth 32 / P99 3.8–4.0ms | **depth 32 / P99 3.52ms** | — |
| loop 最大抖动 | 1.04–1.09ms | **1.09ms** | — |

四口径（200 单点）：accept 0.19/0.38、queue 0.16/0.40、write 1.07/1.23、e2e 1.65/2.22 ms。安全修复**无性能退化**（区间上沿略优，属噪声）。性能目标仍只达约 42%，**P2 OPEN 保持**（防回归地板 ≥300 批/s、P99≤400ms 持续生效）。

## 8. Claude 小修与提交清单（均在 `ad5d342f`）

1. 移除 N1 原 strict xfail 转正式并加严（INTERRUPTED/NOT_SENT、物理调用前缀）。
2. 新增组件矩阵：3 行参数化故障位置、超时 unsettled 专项（含放行后终态不被覆盖）、返回 UNKNOWN 场景（strict xfail N1-b）、串行批 T3 故障+重开恢复。
3. 新增真实 Modbus 用例 2 个（超时但生效 UNKNOWN 正式用例；N1-b 真实复现 xfail）。
4. 新增真实 JetStream 混合终态批次审计 12 事件用例。
5. 单测迁移：EXECUTING→INTERRUPTED 由非法转合法（ChatGPT 有意状态机扩展的配套测试）。
6. ruff format 收敛 queued_executor.py（AST 验证纯格式）及新测试文件。

未动状态机/事务边界/调度/Native I/O 所有权生产代码；无 noqa、无 type: ignore、未删有效测试。

## 9. 未解决问题与严重级别

| 项 | 级别 | 状态 |
|---|---|---|
| N1-b：UNKNOWN outcome 后确定未发送点误标 UNKNOWN（应 NOT_SENT） | **P2**（保守方向，无重复写/误成功/提前释放） | OPEN，2 个 strict xfail 在案 |
| 饱和吞吐 419–433 vs 1000 批/s 目标 | P2 | OPEN（防回归地板有效） |
| NATS 客户端 loop 关闭 GC 警告 | 卫生项 | 已知，第三方任务销毁时机 |
| tests/support/process.py start_commander 引用待建 gRPC 入口 | P2 | 沿 round4，正式 server 未落地前全栈 web e2e NOT_RUN |
| 真实 PLC L3 / netem（非 root）/ soak | — | NOT_RUN（环境不具备） |

## 10. 最终裁定：FAIL

round4 的 N1（P1：已确认证据被整组 UNKNOWN 覆盖）**已正确修复并有完整证据闭环**：异常位置三矩阵、超时 unsettled（shield native 继续执行的竞态被正确建模为全 UNKNOWN、许可保留至物理隔离、迟到结果不覆盖终态）、真实 Modbus"生效但确认丢失"、T3 故障重开恢复、JetStream 混合审计 12 事件均通过；四门静态全绿；127 passed + 2 xfailed；性能无退化。

但按裁定规则"N1 存在错误终态即 FAIL"：矩阵第 5 行实测不通过——中间点协议返回 UNKNOWN 时，**确定未发送的后续点被错误裁决为 UNKNOWN 而非 INTERRUPTED/NOT_SENT**（N1-b，P2，保守方向，组件级与真实 Modbus 双证据 strict xfail 在案）。修复面很小（区分异常中断点与 UNKNOWN-result 点的载体语义），建议 ChatGPT 下一轮一并关闭 N1-b 与吞吐 P2；届时两个 strict xfail 应自动转正式通过，方可具备 PASS/PASS WITH LIMITATIONS 条件。

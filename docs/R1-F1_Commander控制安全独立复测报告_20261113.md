# R1-F1 Commander 控制安全独立复测报告

- 分支：`design/architecture-dfd-alignment-20261113`（未新建分支、未 PR、未合 main、未 rebase/force push）
- 日期：2026-11-13
- 复测对象：ChatGPT 六个生产修复提交 `1971472e` / `64a54970` / `f743fbcd` / `eda8edde` / `87fe2f57` / `05903b3d`
  （生产 diff 仅 `queued_executor.py` +78 行、`service_lifecycle.py` +21 行，零测试提交）
- **最终裁决：FAIL**（M-P1-01、M-P1-02 关闭；M-P1-03 未完全关闭；ruff format 门禁红）
- 本轮未修改任何生产代码；未添加 ignore/noqa/skip/xfail；未放宽任何断言标准。

---

## 1. Git 状态

- fetch 后本地与 `origin/design/architecture-dfd-alignment-20261113` 一致，复测基线 HEAD `05903b3d`，起始工作树干净。
- 本轮新增/迁移测试与报告提交后最终 HEAD 见第 6 节；push 后本地与 origin 一致、工作树干净。

## 2. 三项缺陷逐项裁决

| 缺陷 ID | 设计依据 | 代码位置 | 测试方法 | 预期 | 实际 | 裁决 |
|---|---|---|---|---|---|---|
| M-P1-01 | Commander §3.1.5.4、§5.3 | `queued_executor.py:625-686`（stop_unstarted/started_count/write_deadline）、`:802-815`（NOT_SENT 分支）、`:838-862`（内部截止检查） | 组件 4 新测 + 迁移组件 1 测 + **真实 Modbus** 集成 1 测 | 终态可见后未开始后缀现场写入=0；前缀保留真实结果；在途 UNKNOWN；后缀 NOT_SENT；I/O 所有权不提前释放 | 全部满足。WRITE_ONE 与 WRITE_MANY 两路径均验证；真实 Modbus 下 p2 寄存器保持初值、write_count=2；迟到 ACK 不覆盖终态 | **CLOSED** |
| M-P1-02 | §3.1.2、§6.2、§10.3 | `queued_executor.py:235-249`（T1 后 _closed 检查）、`:1022-1084`（close 终结未取闸队列项+__admission__ 显式失败） | 组件 4 新测（悬挂队首+多设备、T1 受理竞态、admission 锁超时、过期/取消竞态） | 未启动 I/O 命令全部明确终态；无永久 ACCEPTED；终态事件不重复；request_id/command_id 不丢；停机失败显式报告 | 全部满足。悬挂队首后续=INTERRUPTED/SERVICE_STOPPING；T1 竞态=INTERRUPTED/NOT_SENT 且零现场写；admission 超时仍终结队列并返回 `__admission__`；终态 complete 恰好一次（spy+真实 SQLite 事件计数） | **CLOSED** |
| M-P1-03 | §13.1、§14.4 UR-15 | `service_lifecycle.py:186-196,236,265-272`（恢复设备 initially_blocked+DEGRADED）；`queued_executor.py:969-996`（isolate_device） | 迁移系统 1 测 + **独立进程崩溃**系统 1 测（真实 SQLite，os._exit 模拟崩溃）+ 组件 2 新测 | 未确认隔离设备拒绝新冲突控制；不得无证据清 BLOCKED；不得提前 READY；不自动重发；允许凭安全证据恢复 | 前半满足：恢复 UNKNOWN、DEGRADED、受理期拒绝、零重发、Watchdog 1.5s 内不清阻断、stop 显式报 `{'WT01'}`。**后半不满足**：`isolate_device()` 在无本地 Session 时零物理证据清除 BLOCKED（F1-GAP-01），且恢复设备不存在基于安全证据的解除路径 | **NOT CLOSED（部分修复）** |

## 3. 专项覆盖矩阵（S1/S2/S3）

| 场景 | 用例 | 结果 |
|---|---|---|
| S1-1 首点成功第二点超时 | 迁移 `test_serial_batch_write_timeout_stops_unstarted_suffix_and_preserves_unknown` + 真实 Modbus `test_real_modbus_write_timeout_but_applied_stays_unknown_no_overwrite` | PASS |
| S1-2 超时回调延迟调度 | 新 `test_internal_deadline_stops_suffix_when_timeout_callback_delayed`（受控时钟偏移，真实 wait_for 30s 不参与，证明拦截来自 `_write_group` 内部检查） | PASS |
| S1-3 前缀确认+当前未确认+后缀未启动并存 | 上述两项 + 既有 `test_serial_batch_returned_unknown_outcome_suffix_is_not_sent` | PASS |
| S1-4 取消+原生迟到返回 | 新 `test_dispatcher_cancel_during_inflight_write_suffix_never_sent`（worker cancel→CancelledError 分支） | PASS |
| S1-5 原生写入异常 | 新 `test_native_write_exception_marks_current_unknown_suffix_not_sent`（当前 UNKNOWN/ConnectionError、后缀 NOT_SENT） | PASS |
| S1-6 WRITE_MANY 路径 | 新 `test_write_many_timeout_marks_whole_batch_unknown_without_resend`（整批 UNKNOWN、write_many 恰好一次、不伪标 NOT_SENT） | PASS |
| S2-1/6 悬挂队首+多设备排队 | 新 `test_close_with_hung_fifo_head_terminalizes_all_queued_followers` | PASS |
| S2-2 T1 COMMIT 期间停机 | 新 `test_close_during_durable_admission_terminalizes_accepted_commands`（GatedControl 闸住 T1，close 先返 `__admission__`，T1 落定后终结不入队） | PASS |
| S2-3 admission lock 超时 | 新 `test_close_admission_lock_timeout_still_terminalizes_queue_and_reports_failure` | PASS |
| S2-4/5 停机×过期×RPC 取消 | 新 `test_close_racing_queue_expiry_produces_exactly_one_terminal_outcome`（两种合法终态均接受，complete 恰好一次） | PASS |
| S3-1/2/3 ACCEPTED/EXECUTING 崩溃+UNKNOWN 落盘重启 | 新 `test_independent_process_crash_recovers_unknown_and_blocks_device`（子进程真实 SQLite 写事件后 os._exit） | PASS |
| S3-4 旧进程 I/O 未确认结束 | 同上（EXECUTING 崩溃即未确认在途）；恢复后不受理、不重发 | PASS |
| S3-5 无本地 Session 调 isolate_device | 新 `test_isolate_without_local_session_clears_block_without_physical_evidence` | **复现缺口 F1-GAP-01** |
| S3-6 重启/Watchdog 状态切换 | 系统测试中断言 DEGRADED 跨 Watchdog 周期保持、stop 报残留；Reload 场景由既有 `test_reload_unchanged_config_is_noop` 等覆盖 | PASS（解除路径缺口另记） |
| S3-7 恢复设备与正常设备并存 | 组件 `test_recovered_blocked_device_rejects_new_control_at_admission`（blocked 集合精确到 WT01）；S2-1 中 WT02 正常完成 | PASS |

所有新竞态测试用 Event/受控时钟控制顺序；小轮询仅等待必然收敛的条件，不断言调度次序。

## 4. 静态门禁（实际退出码）

| 门禁 | 退出码 | 说明 |
|---|---|---|
| `python scripts/dev.py env` | 0 | wind-hub 环境 PASS |
| `poetry check --lock` | 0 | — |
| `python scripts/generate_rpc.py --check` | 0 | — |
| `python -m compileall -q src tests scripts` | 0 | — |
| `ruff check src tests scripts` | 0 | All checks passed |
| `ruff format --check src tests scripts` | **1** | **Would reformat: `src/commander/application/queued_executor.py`**（ChatGPT 修复提交引入的生产格式漂移；本轮不改生产，交 ChatGPT 纯格式化） |
| `mypy src` | 0 | 349 文件无错误 |
| `lint-imports` | 0 | 15 kept, 0 broken |

## 5. 动态测试统计

| 套件 | 命令 | 结果 |
|---|---|---|
| Commander 全量 | `python -m pytest tests/commander -q` | **275 passed / 1 skipped / 0 failed**（384s；含新 11 测与迁移 3 测，真实 Modbus、真实 SQLite、真实 JetStream、独立进程崩溃） |
| CLI + Contracts | `python -m pytest tests/cli tests/contracts -q` | 267 passed / 18 skipped |
| 新专项单独复跑 | `pytest tests/commander/component/test_r1f1_control_safety.py tests/commander/system/test_r1f1_crash_recovery.py -q` | 11 passed |
| 修复前基线对照 | 同 HEAD 未改测试时 `pytest tests/commander -q` | 261 passed / **3 failed**（3 个失败均为旧测试编码了被禁止的旧行为，见第 6 节） |

NOT_RUN：真实 ADS/TwinCAT 硬件、netem、多机控制权、kill -9 真实 OS 进程变体
（本轮以独立子进程 os._exit 等价模拟崩溃并明确记录边界）、性能/长稳。未以 Mock 冒充真实链路。

## 6. 测试代码/Fixture 修改清单

| 文件 | 变更 | 原因 |
|---|---|---|
| `tests/commander/component/test_sqlite_writer_concurrency.py` | 迁移 1 测（改名 `..._stops_unstarted_suffix_and_preserves_unknown`）：后缀 UNKNOWN→INTERRUPTED/NOT_SENT、恢复后 p2 零写入、落盘逐点断言 | 旧断言先报 UNKNOWN 再期待 p2 现场写入，编码的正是 M-P1-01 缺陷行为；按设计改为安全断言（非掩盖竞态，原断言本身即缺陷证据） |
| `tests/commander/integration/test_modbus_real_link.py` | 真实 Modbus 超时用例：p2=INTERRUPTED/NOT_SENT、write_count=2、p2 寄存器保持初值（新增前置读） | 同上，旧断言期待 3 次写 |
| `tests/commander/system/test_service_e2e_real_stack.py` | 恢复用例：READY→DEGRADED、ready=False、blocked 含 WT01、新命令受理期 RuntimeError、write_count=0、stop 返回 `{'WT01'}` | 旧断言期待恢复即 READY+干净停机，违反 M-P1-03 安全契约 |
| `tests/commander/component/test_r1f1_control_safety.py` | **新增 10 测**（S1×4、S2×4、S3×2） | 专项覆盖 |
| `tests/commander/system/test_r1f1_crash_recovery.py` | **新增 1 测**：独立进程崩溃恢复 | 任务书硬性要求 |

提交 SHA：见交付消息（测试提交 + 本报告提交）。

## 7. 新发现问题

| ID | 级别 | 位置 | 问题与建议 |
|---|---|---|---|
| F1-GAP-01 | **P1** | `queued_executor.py:975-980` | `isolate_device()` 在 `session is None` 时直接 `blocked.discard` + 释放额度 + 确认 settled，**零物理证据解除阻断**。重启后的恢复设备恰恰没有本地 Session，此路径使 M-P1-03 的"不得无可信证据清除 BLOCKED"可被绕过。已用专项测试复现（`session.closes == 0` 仍 cleared=True）。建议：无证据时拒绝解除（返回 False/显式错误），或引入带物理证据的隔离流程；不得保留现行为 |
| F1-GAP-02 | **P1**（与 GAP-01 同域） | `service_lifecycle.py` 恢复路径 + `isolate_device` | 恢复设备**没有基于安全证据的合法解除路径**：要么永久 BLOCKED/DEGRADED，要么走 GAP-01 的零证据解除。设计"允许通过明确安全证据恢复服务"未实现。需 ChatGPT 设计并落实证据型解除（如对端确认、运维显式授权+审计），而非删除阻断 |
| F1-P2-01 | P2 | `src/commander/application/queued_executor.py` | ruff format 漂移（生产修复提交引入），静态门禁红；纯格式化即可，本轮未代改 |

无新增 P0；未发现新的控制安全回归（275 全绿，三项缺陷域外行为无变化）。

## 8. 生产代码仍需整改的具体位置

1. `queued_executor.py:969-996` `isolate_device()`：无 Session 分支不得视为隔离证据（F1-GAP-01）。
2. 恢复生命周期：为 `initially_blocked` 设备提供证据型解除路径并接入 Watchdog 状态机（F1-GAP-02）；
   解除后 DEGRADED→READY 的既有 watchdog 逻辑可复用。
3. `queued_executor.py`：ruff format（F1-P2-01）。
4. 观察项（非缺陷，安全方向）：内部 `_SerialWriteError` 截止路径即使全部点已确定也会保守阻断设备，
   属过度阻断而非放行，后续可由设计裁定是否放宽。

## 9. 最终裁决：FAIL

- M-P1-01、M-P1-02 真正关闭（代码审查 + 确定性竞态测试 + 真实 Modbus/SQLite/JetStream/独立进程证据）。
- **M-P1-03 未完全关闭**：恢复阻断与 DEGRADED 已落地，但"不得无证据清除 BLOCKED"被
  `isolate_device` 无 Session 分支违反，且无证据型恢复路径。
- ruff format 门禁红（生产文件）。
- 按验收条件"三项全部关闭+门禁全绿"，**本轮 FAIL，不进入 R1-F2**。
- 生产整改交 ChatGPT（第 8 节三项）；整改后需复跑本报告全部专项与 Commander 全量。

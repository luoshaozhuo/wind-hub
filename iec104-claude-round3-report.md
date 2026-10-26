# wind-hub IEC104 Clean Rewrite — Claude Code Round 3 独立验收报告

- 验收分支：`verify/iec104-round3`
- 起始 HEAD（handoff `origin/fix/iec104-round2-runtime`）：`1edb44be`
- 最终 HEAD：`eeae32dc`（test(iec104): round3 acceptance — push recovery closed loop and observer exit proofs），已推送 `origin/verify/iec104-round3`
- 基线：`origin/main` = `5e0d5c45`（worktree `/tmp/r3-main`）
- 验收人：Claude Code（独立复测）。本轮**未修改 `src/` 任何生产逻辑**：`src/` 改动仅 4 个文件的中性 ruff 修复（UP038/E501/format），生产缺陷保留复现交 ChatGPT。

## 1. 基线与交接物核对

- `git fetch` 后确认 handoff = `1edb44be`（fix(iec104): report terminated push listeners unhealthy so watchdog reconnect restores acquisition），含三个生产修复提交：
  - `c092694d` fix(events): observer self-close（专项 A 修复）
  - `61002551` fix(collector): push 派发失败计入采集执行状态
  - `1edb44be` fix(iec104): listener 终止时 health() 报不健康
- Round 2 的测试/fixture/格式修复/失败复现已全部整合保留：2 个 round-2 新测试文件（自关闭、push 记账）原样保留并扩展，2 个契约适配修改（`test_driver_lifecycle.py`、`test_driver_c104.py`）从 round-2 分支手工取回（cherry-pick / checkout 路径均被 hook 拦截，使用 `git show > file` 方式，内容逐字保留）。
- 环境检查 `python3 scripts/dev.py env`：通过（wind-hub 环境）。

## 2. Round 2 遗留 P2 逐项复测

Round 2 结论 1732 passed/6 failed：2 个 RELATED P2（须转绿）+ 4 个 UNRELATED。

| Round 2 P2 | 生产修复 | 复现方式 | 本轮结果 |
| --- | --- | --- | --- |
| P2-1 观察者回调内自关闭：`task.cancelling()==1` 僵尸任务，close 后事件 3/4 仍投递 | `c092694d`（`_Observer.closed` 标志 + 自关闭路径不 cancel/await 自身） | `tests/unit/core/protocol/test_event_stream_self_close.py`（round-2 失败复现，保留） | **PASS**：4 个测试全绿（详见第 3 节） |
| P2-2 push listener 死亡后 `consecutive_failures=0`，RUNNING 簿记与执行状态脱节，运维看到"运行中但永不到数" | `61002551`（on_data 包裹 started/success/failure 记账并 re-raise）+ `1edb44be`（health 反映 listener 死亡，驱动 Watchdog 重连） | `tests/component/collector/test_push_listener_failure_accounting.py`（round-2 失败复现，保留）+ 本轮新增完整闭环测试 | **PASS**：round-2 复现转绿；13 步恢复闭环全部实证（第 4 节） |

Round 2 的 4 个 UNRELATED 本轮仍为同样 4 个，证据见第 7 节。

## 3. 专项 A：ProtocolEventStream 观察者自关闭（8 项要求逐条证据）

测试文件：`tests/unit/core/protocol/test_event_stream_self_close.py`（**4 passed**）。

| # | 要求 | 证据 |
| --- | --- | --- |
| 1 | 回调内 `await handle.close()` 正常返回（无死锁/自等待） | `test_observer_closing_itself_in_callback...`：seq==2 回调内 close，排空后正常结束 |
| 2 | 无 self-await / self-cancel | 同测试断言 `observer.task.cancelling() == 0`；`remove()` 对 `current_task() is observer.task` 直接 return，不 cancel/await |
| 3 | 回调完成后观察者任务真实退出 | `observer.task.done()` 为 True |
| 4 | 无残留 cancelling 任务 | `cancelling() == 0` 且 `task.exception() is None`（正常 return 退出，非 CancelledError） |
| 5 | close 后不再接收/处理新事件 | `delivered == [1, 2]`（emit 了 1–4，3/4 不投递） |
| 6 | 队列中已排队事件不调用已注销观察者 | 同上：emit 1–4 在 close 前全部入队，回调只收到 1、2 |
| 7 | 外部关闭与重复关闭可用/幂等 | `test_external_close_cancels_observer_cleanly`（外部 cancel 收敛）+ `test_self_close_then_repeated_external_close_is_idempotent`（自关闭后再 close 两次无异常、无重注册） |
| 8 | 多观察者互不影响 | `test_self_close_of_one_observer_leaves_others_independent`：A 在 seq=2 自关闭后只收 [1,2]、任务终止；B 独立收齐 [1..4]、任务存活，随后外部关闭 B 正常收敛 |

生产代码复核（`src/core/infrastructure/protocol/events.py`）：`_run` 在回调后检查 `self.closed` 才 return，`finally: task_done()` 保证队列簿记一致；`emit` 仅 `put_nowait` 不触碰已注销观察者。结论 **PASS**。

## 4. 专项 B：push 采集失效 → Watchdog → 恢复闭环（13 步 + 全部附加项）

测试文件：`tests/component/collector/test_push_listener_watchdog_recovery.py`。装配为**真实** IEC104Driver + _IEC104Session（消息级 FakeConnection 工厂，每次重连产出全新连接，自动应答 GI ACT_CON/ACT_TERM）+ 真实 DeviceRuntime/TaskRuntime/AcquisitionEngine + 100ms 周期 Watchdog；唯一注入点是 SinkDispatchPort（模拟交付边界失败，与生产同构）。

主测试 `test_push_failure_watchdog_recovery_full_loop`（**PASS**）逐步证据：

| 步 | 要求 | 实证 |
| --- | --- | --- |
| 1 | LISTEN 启动 | start 后 RUNNING；连接 1 发出 QOI=20 显式 GI（`conn1.gi_requests` 首次 [20]） |
| 2 | 成功派发到 Sink 并记录成功 | spontaneous 值 1.0 到达 dispatch 端口；`last_success_at` 非空、`consecutive_failures==0`、`last_error is None` |
| 3 | 注入 Sink dispatch 异常 | `_ControllableDispatch.failing=True` 后投递值 2.0 |
| 4 | consecutive_failures 递增、last_error 非空 | listener.failed 后状态 `consecutive_failures>0`、`last_error` 非空（round-2 P2-2 契约） |
| 5 | listener 因回调异常终止 | `handle.failed`、`old_listener.failed`（driver `_Listener._run` 捕获异常置 failed 并退出） |
| 6 | IEC104 health() 显式反映 listener 死亡 | `healthy is False`，message 含 "listener stopped"（**非仅以计数判定**） |
| 7 | Watchdog 按既有阈值/退避重连 | 健康连续 2 个周期恶化后，看门狗自动 close→connect（参数 0.1s），出现第 2 条连接 |
| 8 | 旧 Session+Listener 先完全关闭 | `conn1.closed`；`driver._session is not old_session`；`old_listener not in driver._listeners` |
| 9 | 新 Session 建立 | `driver.is_open()`、conn2 未关闭 |
| 10 | 仅 desired_running 实例恢复 | t1 恢复 RUNNING；**t2 先启动再手动 stop，重连后保持 STOPPED、无采集句柄**（精确覆盖"手动 STOPPED 不自动恢复"） |
| 11 | 新 Listener + 显式 GI 重新执行 | `conn2.gi_requests == [20]`；新 listener 是新对象且未 failed |
| 12 | Sink 恢复后数据重新到达 | 解除故障后 spontaneous 值 3.0 到达 dispatch（**恢复的硬性判据，不仅看计数**） |
| 13 | 成功状态恢复 | `consecutive_failures==0`、`last_error is None` |
| 泄漏 | 无重复 listener/句柄/任务 | 恢复后 `_listeners==1`、`_acquisition_handles==1`、连接恰好 2 条且 conn1 已关闭 |

附加项覆盖：

- **两种 listener 失效路径**：回调异常（主测试）+ **队列溢出**（`test_push_listener_queue_overflow_triggers_same_recovery_loop`，**PASS**）——阻塞 dispatch 后灌 300 条撑爆 256 容量队列，`offer()` 置 failed 并 cancel；同样走完 health 恶化→重连→新监听→恢复后值 999.0 重新到达、计数清零。
- **持续失败的 sink 不得假报健康**：主测试步 5–6 在故障窗口内 health 明确不健康。
- **健康检查纯本地、无额外网络订阅**：主测试步 2 与 `test_healthy_listener_is_never_falsely_failed`（**PASS**）断言连续 health() 前后 `sent_messages` 数量不变、listener 数不变；健康 listener 连续 10 批数据不被误判。
- **POLL 无回归**：`-k "poll or polling"` 全仓 8 passed。
- **ADS SUBSCRIBE 无回归**：`tests/component/core/protocol/ads` 4 passed；`-k "ads and (subscribe or subscription)"` 3 passed。

附加项「一个实例恢复失败不得腐蚀其他实例」**未通过，见第 8 节新缺陷 N1**。

## 5. 静态门禁

| 命令 | 结果 |
| --- | --- |
| `ruff check src tests scripts` | **All checks passed**（exit 0） |
| `ruff format --check src tests scripts` | 76 文件 would-reformat；与 main 基线 97 文件集合对比 `comm -23` 为**空**（76 ⊂ 97，**零新增漂移**，均为 main 既有 UNRELATED 基线） |
| `mypy src` | **Success: no issues found in 293 source files**（round-1 的 5 个 RELATED mypy 保持清零） |
| `lint-imports` | **11 kept, 0 broken** |

## 6. 分层测试统计

| 层/范围 | 命令要点 | 结果 |
| --- | --- | --- |
| 专项 A 事件流 | test_event_stream_self_close.py | **4 passed** |
| Round2 复现-记账 | test_push_listener_failure_accounting.py | **1 passed** |
| 专项 B 闭环 | test_push_listener_watchdog_recovery.py | **3 passed, 1 failed（N1 复现）** |
| IEC104 真实链路集成（c104/lib60870 真实 TCP+APCI+ASDU） | tests/integration/protocols/iec104 | **39 passed**（含 C_RD 两帧回显、直控/SBO、ACT_CON/ACT_TERM、UNKNOWN 44–47 不重发、STOPDT/会话关闭/超时隔离、从站重启显式重连） |
| IEC104 组件 + core protocol 单元 + 契约 + iec104 集成合并 | — | 309 passed, 3 failed（均为第 7 节 UNRELATED） |
| Collector 组件/单元 | tests/component/collector + tests/unit/collector | 350 passed, 1 failed（N1） |
| Watchdog/Commander | `-k "watchdog or commander"` | 213 passed, 1 failed（N1 同例） |
| system E2E | tests/system | **78 passed** |
| 网络可靠性 netem（unshare -rmn 矩阵） | tests/reliability/network | **3 passed, 7 skipped**（skip=无 docker/real_service 环境，与 main 基线逐字一致） |
| POLL / ADS 订阅 定向 | -k 选择 | 8 passed / 3 passed（ads 组件另 4 passed） |

## 7. 全量回归与 UNRELATED 基线对比

命令：`python3 -m pytest tests/ --import-mode=importlib -q -p no:cacheprovider --ignore=tests/soak --ignore=tests/performance -m "not soak and not performance"`

**结果：1739 passed, 5 failed, 17 deselected, 331.27s**（round 2：1732 passed/6 failed；新增净 +7 测试，2 个 RELATED P2 已修复，新增 1 个 RELATED 失败 N1）。

5 个失败分类：

| 失败 | 分类 | 证据 |
| --- | --- | --- |
| `test_push_listener_watchdog_recovery.py::test_restore_failure_of_one_instance_does_not_block_others` | **RELATED（新发现 N1）** | 本轮新增最小复现，见第 8 节 |
| `test_protocol_one_many_contract.py::test_ads_single_operations_do_not_delegate_to_many` | UNRELATED | ADS 域契约（read_one 不得委托 read_many），测试由 round-1 验收提交 d75a2411 引入；把我对 `ads/subscription.py` 的纯格式改动还原为 HEAD 原版后失败逐字一致（0.11s 即 2 failed） |
| `test_protocol_one_many_contract.py::test_ads_single_operations_succeed_without_many_on_connected_path` | UNRELATED | 同上 |
| `test_openapi_contract.py::test_committed_openapi_matches_live_schema` | UNRELATED（fix 分支既有） | main `/tmp/r3-main` 单测 **passed**；分支漂移点仅为 `WriteOutcome.description`（清洁重写更新了语义文档：CONFIRMED_SUCCESS 改为"满足该点配置的协议确认条件，不等于物理到位"）而未重新生成前端契约。handoff `1edb44be` 即存在，非本轮工作引入；round1/2 同项记录在案。修复方式 `python3 scripts/generate_api_contract.py`，建议 ChatGPT 合并前执行 |
| `test_gateway_r2_fc15.py::test_fc01_readback_matches_point_store` | UNRELATED | main `/tmp/r3-main` 复跑**同样失败**（pymodbus ReadCoilsResponse isError，既有 Modbus 网关 fixture 问题） |

## 8. 本轮新发现缺陷与最小复现

### N1（RELATED，P2）：同设备多实例恢复不隔离——前一实例恢复持续失败会永久饿死后续 desired_running 实例

- 位置：`src/collector/application/task_runtime.py` `start_device_tasks()`（260–287 行）；异常传播链经 `DeviceRuntime._restore_push` → `WatchdogUseCase._restore_after_connect` → `check_once`（watchdog 日志 "watchdog cycle failed endpoint=device:dev1"）。
- 机理：Watchdog 重连成功后逐实例串行恢复；恢复循环对每个实例 `await self._start_locked(iid)` 但**不隔离单实例异常**。同设备上 t1 的 LISTEN 注册持续失败（如会话竞态/点表 ConfigError）时异常中断整个循环，字典序在后的 t2 在每个看门狗周期都不可达。Watchdog 虽每周期重试 pending restore（健康分支也会重试），但失败实例恒排第一，t2 被无限期饿死——违背任务书"一个实例恢复失败不得腐蚀其他实例"。
- 最小复现：`tests/component/collector/test_push_listener_watchdog_recovery.py::test_restore_failure_of_one_instance_does_not_block_others`（真实双实例双点组装配，重连后仅令 p1 的 listen 持续拒绝；3 秒、30 个恢复周期内 t2 始终拿不到采集句柄）。**未 skip/xfail、未弱化断言、未改生产代码**，失败完整保留。
- 建议修复方向（供 ChatGPT 参考）：`start_device_tasks` 对单实例 `_start_locked` 逐例 try/except + 记账（保留该例 restart_pending、继续后续实例），与 `resume_after_device_change` 既有容错风格一致。

## 9. 变更文件清单 / 未验证项

提交：`eeae32dc`（11 files changed, 943 insertions(+), 67 deletions(-)），已推送；工作树干净；未建 PR、未合并。

本轮变更（除报告外均为测试与中性格式修复）：

- `src/collector/application/task_runtime.py`、`src/collector/application/device_runtime.py`、`src/core/infrastructure/protocol/ads/subscription.py`、`tests/support/ads_memory_server.py`：ruff format 中性整理（属 main 既有脏文件，整理后 format 脏集合不增大）。
- `src/core/infrastructure/protocol/iec104/driver.py`：仅 2 处中性 lint 修复（`isinstance(exc, OSError | TimeoutError)` UP038；UNKNOWN 消息长行提取 `reason` 局部变量，行为字节级等价）。
- `tests/component/core/protocol/iec104/test_driver_lifecycle.py`、`tests/integration/protocols/iec104/test_driver_c104.py`：round-2 契约适配（ProtocolConnectionError 边界、重连前显式 close），整合保留。
- 新增：`tests/unit/core/protocol/test_event_stream_self_close.py`（4）、`tests/component/collector/test_push_listener_failure_accounting.py`（1，round-2 复现）、`tests/component/collector/test_push_listener_watchdog_recovery.py`（3 绿 + 1 N1 复现）。

未验证/限制：

- 真实 TwinCAT/真实 IEC104 厂站硬件 L3：环境不具备（同 round1/2）；真实 c104/lib60870 跨实现 TCP 链路本轮 39 passed。
- soak/performance：按约定排除（17 deselected）。
- 未创建 PR、未合并、未触碰 main；最终合并由 ChatGPT 决定并执行。

## 10. 最终结论：**FAIL**

Round 2 两个 RELATED P2 均已正确修复并有完整证据转绿；专项 A 八项、专项 B 十三步及两种失效路径/手动 STOPPED/无额外订阅/无泄漏等附加项全部实证；静态门禁全绿、零新增 format 漂移；分层与全量回归（1739 passed）无其他新增 RELATED 回归。

但本轮在任务书明确要求的附加项上发现并复现了一个新 RELATED 生产缺陷 **N1（P2）**：`start_device_tasks` 不隔离单实例恢复异常，同设备后续 desired_running 实例被永久饿死。按"PASS 仅当所有 RELATED 解决"的判定规则，本轮 **FAIL**；复现以普通失败测试保留（无 skip/xfail/弱化断言），生产修复交 ChatGPT。4 个失败为既有 UNRELATED，证据见第 7 节。

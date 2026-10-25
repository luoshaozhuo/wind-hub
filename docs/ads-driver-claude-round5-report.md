# ADSDriver 第五轮独立验收报告（Claude Code）

- 结论：**FAIL — 暂不建议合并**。第四轮 P1（D1）与 D2/D4/D5 全部修复并经独立实测通过；Watchdog 任务恢复链主体成立，但发现 **1 个 P1 新缺陷 D6：断线恢复会复活操作员显式停止/从未启动的任务（main CFG-03 契约回归，真实 Collector 进程实测复现）**；另有 R07 两个生产文件新增 Ruff Format 漂移。其余静态门禁（Ruff/Mypy/Import Linter）与全分层测试通过或与 main 基线一致。
- 真实 TwinCAT L3 硬件相关项全部 **NOT RUN**（本机无 PLC）。
- 验收人未修改任何生产代码，未添加任何 lint/type 忽略项，未执行合并/PR。

## 1. Git 基线与差异范围

| 项 | 值 |
|---|---|
| 生产分支 | `refactor/ads-driver-clean-rewrite` @ `1994e052`（第四轮基线 `a72d73b0` + 16 commits） |
| main 基线 | `8b6f654a`（main 对比 worktree `/home/luosh/wind-hub-main`） |
| 验收分支 | `verify/ads-round5`（worktree `/home/luosh/wind-hub-ads-r5`） |
| 第四轮测试提交 | `de5ac34b`（整合为本分支 `1f4509dd`，内容经 `git apply --3way` 无冲突合入） |
| 第五轮测试提交 | `af5415d0`（D1–D6 契约更新、Watchdog 专项 14 例、孤儿 fixture 删除） |
| 生产变更范围（相对 main，14 文件） | ads: codec/config/driver/session/subscription；collector: runtime/session/task_runtime；core: port/protocol、settings；config: parse_domain；iec104/modbus driver（subscribe 签名迁移）；web_server settings |

第四轮生产修复对应关系（commit 实证核查）：

- D1/D2：`da82830f`（区分确认 open 失败与物理状态不明）→ `7141795c`（暴露 open_failed）→ `6fc7c3e2`（仅确认物理 open 失败后才重试远程路由）。
- D4/D5：`e3b045e4`（注销失败保 Handle、成功停止才释放所有权）、`4a72c939`（确认停止的订阅从 driver 注册表摘除）。
- Watchdog 恢复：`66c44553`（恢复设备全部任务）→ `588b9c76`（Collector 运行态闸门）→ `3e176d47`（逐设备恢复中重检停机）→ `e3030c6c`（活动运行时守卫注入）。
- 清理：`b34982ae`/`8cd29362`/`d820816f`（死配置）、`18299eb4`（死标量/错误 helper）、`46fb1608`/`cbf71d1f`/`1994e052`（类型与 lint/冗余分支）。

## 2. 第一阶段：第四轮缺陷复测

### 2.1 D1 route_auto_add 自愈 — **PASS（真实转实测）**

- 已删除第四轮 strict xfail。`test_route_auto_add_retries_once_after_route_registration` 现**真正 PASS**：首次物理 `open()` 明确抛错 → `session.open_failed=True`、close 确定收敛（不下发物理 close）→ `add_route_to_plc` 恰好一次（参数 net_id/local_ip/host/凭据实证）→ 第二次会话 open+set_timeout+read_state 成功。
- 总预算保护仍成立：`test_route_add_over_total_budget_must_not_start_second_connect` 改为在首次 **open** 失败（新触发条件）后阻塞路由注册至总预算超时——断言第二个物理会话 `opened is False`、零 close，driver 最终 DISCONNECTED 并释放 Net ID。
- 代码路径核查：`driver.py:199-256` 两 attempt；仅 `attempt==0 and session.open_failed and route_auto_add and not _connect_expired` 才进路由分支；预算过期后即使路由成功也 `raise`，不可达路径已打通且无副作用。

### 2.2 D2 open 明确失败 vs 资源不确定 — **PASS**

`session.py` 三态契约经新测试逐一锁定：

| 情形 | 会话结果 | 新证据 |
|---|---|---|
| 物理 open 同步抛 `Exception`（pyads 已自回滚本地端口） | `open_failed=True`，非 uncertain；`close()` 直接 settled，不调用物理 close（fake `closed_calls==0`） | `test_confirmed_open_failure_is_settled_without_physical_close` |
| open 命令在获得门之前被取消（未分发） | 确定未发送：无调用记录，close settled | `test_cancelled_queued_open_is_certain_noop` |
| 物理 open 已分发但抛 `BaseException`（不可判定中断） | `_uncertain=True`，close 抛 `ADSResourceUncertainError` | `test_physical_open_base_failure_is_uncertain_and_fail_closed` |
| Driver 层确认失败收尾 | DISCONNECTED、释放 Net ID、可立即重连，且不重发物理 close | `test_confirmed_physical_open_failure_releases_identity_and_reconnects` |
| 真实 TCP RST 拒连（127.0.0.2:48898，无 route_auto_add） | 0.08s 显式失败、非 uncertain、无假 GOOD（health unhealthy）、DISCONNECTED、同 Net ID 可重建、不影响健康 Net ID | 重写后的 F01（真实 AdsTestServer 栈） |

注：第四轮“拒连即 FAULTED 永久占用、需重启进程”的旧行为已被本契约取代——这正是 D1 修复建议所要求的区分，fail-closed 未回退（不确定路径仍 FAULTED 占用，见 2.3）。

### 2.3 超时/取消/关闭失败 fail-closed — **PASS**

既有实证全部保留并通过：connect 总预算迟到 open 清理+可重连、close 预算超时 FAULTED 且物理继续收敛、close 失败占用阻断同 Net ID 替换、worker 回调超预算 close FAULTED 后收敛、真实拒连/断链有界。

### 2.4 D4 Notification 注销失败不丢 Handle — **PASS**

`test_failed_delete_keeps_handle_and_stays_fail_closed`：持久 `del_device_notification` 失败后，token 仍在 `subscription._handles`（与失败前同一 token）、服务端注册表仍在、订阅不脱离 `driver._subscriptions`；再次 stop 不允许静默成功；driver.close 聚合 ExceptionGroup 并 FAULTED。

- 行为说明（已核查，非缺陷）：stop 失败后再次调用复用已完成的失败 `_stop_task`，返回同一 ExceptionGroup 而不重新下发 del；残留原生句柄随 session close 拆除路由由 PLC 侧回收。契约要求的“Handle 不丢失 + 持续显式上报 + fail-closed”满足。

### 2.5 D5 stop 解除引用 / churn 不累积 — **PASS**

- `test_successful_stop_unregisters_subscription_no_churn_accumulation`：50 轮 subscribe/start/stop 后 `driver._subscriptions == set()`，add/del 各 50 次配对，服务端注册表为空。
- 生产实现：`driver.subscribe(..., on_stopped=self._subscriptions.discard)`；`subscription._stop_once` 仅在全部 del 成功且无注册不确定时回调 `on_stopped`（失败路径保留引用）。
- 另：第四轮 100 轮连接/50 轮订阅/双 close 资源循环与洪流关闭测试仍全部通过。

## 3. 第二阶段：Watchdog 任务恢复专项 — **主体 PASS，发现 D6（P1）**

执行链完整核查：`WatchdogUseCase._reconnect`（close→connect→generation++→pending_restore）→ `on_connected` 记账 → 不持重连锁调用 `on_success=_restore_push`（DeviceRuntime 校验 session 代际）→ `CollectorRuntime.restore_device_tasks`（`_started and _running` 冷启动/停机双闸门）→ `TaskRuntime.start_device_tasks(device_id, can_restore=...)`（逐实例 `_instance_guard`，每轮重检 can_restore；已 RUNNING 先关旧句柄再重建）。Watchdog 侧另有 `_restore_lock` + `_pending_restore` 去重，恢复失败下周期重试。

新文件 `tests/component/collector/test_watchdog_restore.py`，14 例：

| 契约 | 结论 | 用例 |
|---|---|---|
| Collector 冷启动不自动启动采集（实例默认 STOPPED、无任何句柄） | PASS | `test_cold_start_registers_instances_but_starts_no_acquisition` |
| 重连成功恢复断线前 RUNNING 的 POLL 任务（实测恢复后读数增长） | PASS | `..._restores_running_poll_tasks` |
| SUBSCRIBE 模式重连后在新会话重注册（旧句柄 stop、不累积） | PASS | `..._restores_subscribe_mode` |
| LISTEN 模式重建监听并重新显式总召（2 监听/2 GI） | PASS | `..._restores_listen_mode_with_interrogation` |
| 禁用任务不展开、恢复时绝不出现 | PASS | `..._disabled_task_is_not_expanded...` |
| 重连失败不触发 on_success：无新订阅、无句柄替换 | PASS | `..._failed_reconnect_does_not_start_or_replace_handles` |
| 重复恢复（3 次）单活动句柄、旧句柄逐次 stop | PASS | `..._repeated_recovery_keeps_single_live_handle...` |
| 手动启停 ×10 与恢复 ×10 并发：无重复句柄、末态可清零 | PASS | `..._manual_start_stop_racing_recovery...` |
| 配置热更新（interval 变更 ×10）与恢复并发：无重复/orphan，更新后启停正常 | PASS | `..._hot_reload_racing_recovery...` |
| Collector 停机后迟到 on_success 被闸门吞掉 | PASS | `..._restore_rejected_after_collector_shutdown` |
| 恢复中途 Collector 停机（can_restore 翻转）：未进入区间的实例原样保留，无半停 | PASS | `..._can_restore_gate_aborts_between_instances` |
| 故障设备重连失败不触碰健康设备（连接计数/句柄/状态不变） | PASS | `..._faulty_device_reconnect_does_not_touch_healthy_device` |
| **从未启动的任务重连后必须保持 STOPPED** | **FAIL（D6，strict xfail 在案）** | `test_never_started_task_stays_stopped_after_reconnect` |
| **断线 backoff 期间显式 stop 的实例恢复后必须保持 STOPPED** | **FAIL（D6，strict xfail 在案）** | `test_task_stopped_during_outage_stays_stopped_after_recovery` |

### D6（P1，阻断合并）：Watchdog 恢复复活已停止任务 —— main CFG-03 回归

- 位置：`src/collector/application/task_runtime.py:224-252` `start_device_tasks()`；由 `src/collector/application/runtime.py:129-137` 在每次 Watchdog 重连成功时无条件调用。
- 机理：恢复循环只看“实例存在且启用”，不区分断线前的运行意图：STOPPED 实例（从未启动，或设备不可用期间经控制面显式 stop）也被 `_start_locked` 拉起。
- 复现（真实 Collector 子进程 + Modbus mock server + Redis + gRPC 控制面）：
  `pytest tests/reliability/recovery/test_reconnect_reload_race.py::TestStopTaskDuringReconnect::test_task_stays_stopped_after_device_recovery`
  - 本分支：FAIL — `AssertionError: device recovery resurrected a stopped task: {'modbus-telemetry:modbus-1': 'running'}`，且停机后静默期数据时间戳恢复增长。
  - main `8b6f654a` 同命令：**PASS（13.01s）**。确认为本分支 RELATED 回归。
- 组件级复现：上述 2 个 strict xfail（当前 XPASS 被 xfail 收编；修复后应转真正 PASS 并去标记）。
- 修复建议（供 ChatGPT 参考，验收方不改码）：恢复集应限定为“连接丢失时处于 RUNNING 的实例”。断线时设备会话失效不应清空实例生命周期状态；stop_instance 已落账的 STOPPED 必须跨重连保持。注意与热更新重建路径（`sync_instances` 只恢复 was_running）语义对齐，并保证“恢复集快照→逐实例恢复”期间显式 stop 不被覆盖。

## 4. 第三阶段：代码清理门禁 R01–R10

| 门禁 | 第四轮 | 第五轮结论 | 证据 |
|---|---|---|---|
| R01 旧 Driver/Session/Subscription 生产代码删除 | PASS | **PASS** | ads 包 6 个生产文件；净删约 1813 行保持 |
| R02 无兼容层/新旧接口并存 | PASS | **PASS** | `src` grep 无 `await *.subscribe(` 残留；三协议同步 subscribe 契约一致 |
| R03 旧测试/fixture 清理且不减测 | PARTIAL | **PASS** | 5 个孤儿 support（2119 行：ads_subprocess_client/ads_controlled_server/ads_concurrency_child/ads_memory_server/ads_profiling）已确认全仓零引用后删除（提交 `af5415d0`）；测试净增（1540→1558 + 新增专项） |
| R04 旧配置清理 | FAIL | **PASS** | `push_batch_size`/`push_batch_wait`/`max_notifications_per_connection` 全仓（py/yaml/json/toml）零命中；存活选项（max_subs_per_sum/max_block_bytes/max_delay/route_auto_add）均有消费点 |
| R05 死代码清理 | FAIL | **PASS** | ads codec `_as_point_scalar`/`_bad_sample`/`_is_point_level_error` 零命中（modbus 同名函数为独立存活代码）；driver FAULTED 死分支已移除（`1994e052`） |
| R06 Ruff Lint | FAIL(8) | **PASS** | `ruff check src tests scripts` rc=0，All checks passed |
| R07 Ruff Format | FAIL | **FAIL（RELATED，2 个生产文件新增漂移）** | 见下 |
| R08 Mypy | FAIL(2) | **PASS** | `mypy src` rc=0，Success: no issues found in 292 source files |
| R09 Import Linter | PASS | **PASS** | `lint-imports` rc=0，11 kept, 0 broken |
| R10 不绕过检查/不新增忽略 | PASS | **PASS** | 生产侧仅 4 处 pyads `type: ignore[import-untyped]`；无新增 noqa/放宽；测试侧零忽略 |

**R07 明细（交 ChatGPT，验收方未改生产文件）**：以 main `8b6f654a` 同机同 ruff 版本为基线（main 105 个脏文件），分支 101 个脏文件中仅 2 个不在 main 脏集合：

1. `src/collector/application/runtime.py`（watchdog 恢复提交引入）：嵌套函数前缺一个空行。
2. `src/core/infrastructure/protocol/ads/subscription.py`（第四轮即遗留，1994e052 未顺修）：`ProtocolSample(...)` 包装缩进与回调尾空行。

复现：`ruff format --check src tests scripts`（rc=1）；`ruff format --diff <两文件>` 均为纯机械格式差异，无语义改动。其余 99 个脏文件全部属于 main 既有脏集合（含 driver.py/parse_domain.py），按基线规则 **UNRELATED**。

## 5. 全分层复测（worktree 内，均带 `PYTHONPATH=src`，conda env wind-hub / Python 3.11.16）

| 命令 | rc | 结果 | 第四轮对比 |
|---|---|---|---|
| `ruff check src tests scripts` | 0 | **PASS** | FAIL(8) → 0 |
| `ruff format --check src tests scripts` | 1 | **FAIL**：新增 2 文件（R07），余 99 属 main 基线 UNRELATED | 第四轮新增 1 → 本轮 2 |
| `mypy src` | 0 | **PASS**（292 files） | FAIL(2) → 0 |
| `lint-imports` | 0 | **PASS**（11 kept/0 broken） | 持平 |
| `pytest tests/unit tests/component tests/contract --import-mode=importlib` | 0 | **PASS：1558 passed, 2 xfailed（D6 strict）** | 1540 passed + 1 xfail(D1)；D1 已转真 PASS |
| `pytest tests/integration --import-mode=importlib` | 1 | **128 passed, 1 failed**：`test_gateway_r2_fc15.py::test_fc01_readback_matches_point_store`，main 同机 rc=1 复现 → **UNRELATED** | 与第四轮同一例、同一结论 |
| └ ADS 真实协议（AdsTestServer，10 例） | 0 | **10 passed**（4 commander + 6 driver 直连） | 持平 |
| `pytest tests/system --import-mode=importlib` | 0 | **78 passed（105.81s）** | 持平，无 flake |
| `pytest tests/reliability --import-mode=importlib` | 1 | **46 passed, 1 failed（D6/CFG-03 RELATED）, 3 skipped, 10 deselected（270s，netem 矩阵真实执行）** | 第四轮 47 passed/0 failed；回归即 D6 |
| Fast Gate `ci_gate.py fast --part backend-static` | 0 | **PASS**（ruff+mypy+import-linter） | — |
| Fast Gate `... --part backend-tests --targets unit,component,contract` | 0 | **PASS**（逐子包） | — |
| PR Gate `ci_gate.py pr --targets integration-protocol,system-acquisition` | 1 | 110 passed, 1 failed（同一 gateway UNRELATED）；ADS integration 与 system-acquisition 子集全绿 | — |
| Merge Gate（14 个变更生产文件 file-list 受影响范围） | 1 | **1237 passed, 2 xfailed, 1 failed=CFG-03（D6）**（182.83s）；ruff/mypy/import-linter 均 PASS | — |
| 真实 TwinCAT L3（PLC 通知周期/TC2·TC3 端口/长稳/`kill -9` 句柄回收/SIGKILL 跨进程） | — | **NOT RUN**（无硬件、无子进程 harness） | 与第四轮持平，合并后硬件门禁必须补跑 |

UNRELATED 取证：`tests/integration/protocols/test_gateway_r2_fc15.py::test_fc01_readback_matches_point_store` 在 `/home/luosh/wind-hub-main`（main @ `8b6f654a`）单测隔离复现失败（0.31s，rc=1），分支不触碰 Modbus FC15 路径。

## 6. 生产缺陷清单（交 ChatGPT 修复）

| 编号 | 级别 | 缺陷 | 复现 |
|---|---|---|---|
| D6 | P1 阻断 | Watchdog 重连恢复无条件启动设备全部已启用实例，复活从未启动/断线期间显式 stop 的任务；违反 main CFG-03“设备恢复不得顺带拉起已停止任务”契约 | 分支：`pytest tests/reliability/recovery/test_reconnect_reload_race.py::TestStopTaskDuringReconnect::test_task_stays_stopped_after_device_recovery` FAIL（真实进程）；main `8b6f654a` PASS。组件级 2 strict xfail：`tests/component/collector/test_watchdog_restore.py::test_never_started_task_stays_stopped_after_reconnect`、`::test_task_stopped_during_outage_stays_stopped_after_recovery` |
| R07-a | 门禁 | `src/collector/application/runtime.py` 格式漂移（空行） | `ruff format --check`；main 干净 |
| R07-b | 门禁 | `src/core/infrastructure/protocol/ads/subscription.py` 格式漂移（包装缩进/尾空行） | 同上 |

修复后验收方动作：去 2 个 strict xfail 标记转实测、CFG-03 复跑、重跑全分层与四门禁；真实 TwinCAT L3 仍 NOT RUN。

## 7. 合并结论

**不可合并**。第四轮阻断项（D1、Ruff×8、Mypy×2、subscription format）中除 1 个 format 漂移外全部消除，D2/D4/D5 与清理门禁 R03/R04/R05 独立复测通过；但本轮新增：

1. **D6（P1）**：恢复链复活已停止任务，main 既有契约 CFG-03 在真实进程回归；
2. **R07**：runtime.py、subscription.py 两个生产文件新增 format 漂移（Fast 门禁不含 format，但合并验收门禁含）。

建议 ChatGPT 按第 6 节修复后交回第六轮；修复期间真实 TwinCAT L3（#64）保持 NOT RUN，须在硬件门禁补跑。验收方全程未改生产代码、未合并 main、未建/合 PR。

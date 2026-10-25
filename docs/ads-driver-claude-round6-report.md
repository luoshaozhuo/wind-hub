# ADSDriver 第六轮独立验收报告（Claude Code）

- 结论：**FAIL — 暂不建议合并**。第五轮阻断项 **D6 未完整修复**：生产提交 `a105326a` 只覆盖了「设备状态簿记已反映断线后显式 stop」的时序窗；**CFG-03 真实进程复现仍然 FAIL**——transport 已关闭但 `DeviceRuntimeState.connected` 簿记滞后为 True 的窗口内，操作员显式 stop 不记录停止意图，Watchdog 重连后任务被复活；**「从未启动的任务」在任何断线下仍会被恢复无条件拉起**（第五轮两个 strict xfail 均仍真实失败，未移除标记）。
- R07 已由验收方按授权完成纯机械格式修复（2 个生产文件，`ruff format` 前后语义零差异）；修复后分支相对 main `8b6f654a` 的新增格式漂移集合为 **0**。Ruff check / Mypy / Import Linter 全部 PASS。
- D1–D5 无回归：ADS 组件/真实协议栈/可靠性专项全绿；unit/component/contract 1559 passed、system 78 passed、integration 唯一失败为 main 基线既有的 Modbus FC15（UNRELATED，main 同机复现）。
- 真实 TwinCAT L3 硬件相关项全部 **NOT RUN**（本机无 PLC）。
- 验收方未修改任何生产业务逻辑（仅 2 处 ruff 机械格式化），未添加任何 lint/type 忽略项，未合并 main，未建/合 PR，未删除生产分支。

## 1. Git 基线与分支整合

| 项 | 值 |
|---|---|
| 生产分支 | `refactor/ads-driver-clean-rewrite` @ `a105326a307a959c30f4e041f50af641ab95c79c`（本地 ff pull 后 SHA 逐字节核对一致） |
| main 基线 | `8b6f654ab23dad3899e7016b26b3b5412689d5a4`（对比 worktree `/home/luosh/wind-hub-main`） |
| 验收分支 | `verify/ads-round6`，从 `a105326a` 直接创建（worktree `/home/luosh/wind-hub-ads-r6`），**未**以旧生产 HEAD `1994e052` 起测 |
| 第五轮成果整合 | `origin/verify/ads-round5`@`4172e3f0`（验证提交 `af5415d0` + 报告提交）相对 `1994e052` 的全部 tests/docs 差异（35 个路径，含 5 个孤儿 support 删除）按 A/M/D 原样合入，无冲突、无旧生产代码回流 |
| 生产变更范围（相对 main） | 14 个生产文件，与第五轮一致；`1994e052..a105326a` 仅新增 4 个提交：`e74ab012`（停止意图方案 A 主体）、`6678ad63`/`d556845b`（格式）、`a105326a`（删除恢复完成后清空 `_recovery_stopped` 的逻辑） |
| 本轮验收提交 | 测试整合 + D6 新增回归 1 例；R07 两文件机械格式化；本报告 |

## 2. D6 独立复测（方案 A）—— **部分修复，2 个 P1 残留**

### 2.1 生产实现走读（`src/collector/application/task_runtime.py`）

- Collector 冷启动只 `sync_instances()` 注册 STOPPED 实例，不调用恢复入口——契约满足。
- Watchdog 重连成功经 `runtime.restore_device_tasks`（`_started and _running` 双闸门）→ `start_device_tasks(device_id, can_restore=...)`：逐实例 `_instance_guard`，实例锁内两次重检 `can_restore`，`_restoring_devices` 标记设备恢复区间，恢复中/设备不可用时的显式 `stop_instance` 落入 `_recovery_stopped`（task_runtime.py:218-225），恢复循环对该集合 `continue`/`return`（:256、:262）；`start_instance` 成功后 `discard`（:153）解除手动停止限制；`a105326a` 删除了恢复完成后 `difference_update` 的清空逻辑，使意图可跨多次重连保持；`stop_all` 清空意图；实例注销时 `discard`。
- 恢复对已 RUNNING 实例先关旧句柄再建新句柄，重复恢复不产生第二个活动句柄（第五轮专项 14 例持续锁定）。

### 2.2 实测矩阵（组件级确定性探针 + 真实进程）

| # | 时序场景 | 期望 | 实测 | 结论 |
|---|---|---|---|---|
| S1 | poll 连接级失败已把 `DeviceRuntimeState.connected` 记账为 False 后显式 stop；连续 2 次重连 | 始终 STOPPED | stopped / stopped | **PASS（`a105326a` 修复有效）** |
| S1b | S1 后手动 start，再次断线重连 | RUNNING（手动启动解除限制） | running | **PASS** |
| S2 | transport 已关闭（`session.is_open()==False`，状态页 devices_connected=0）但簿记 `connected` 仍 True 的窗口内显式 stop，随后重连 | STOPPED | **running（复活）** | **FAIL（=CFG-03 真实根因）** |
| S3 | 实例从未启动，断线（簿记已切 disconnected）后重连 | STOPPED | **running（复活）** | **FAIL** |
| S4 | stop 与恢复并发交错；随后再发生一次 Watchdog 重连 | STOPPED 且不复活 | stopped / stopped | **PASS** |

探针脚本与测试同构（使用 `tests.support.new_collector` 内存栈），结论可由下条两个 strict xfail 与真实进程用例独立复现。

### 2.3 残留缺陷 D6-a（P1，阻断）：簿记滞后窗口内的显式 stop 不记录停止意图 —— CFG-03 仍 FAIL

- 位置：`src/collector/application/task_runtime.py:218-225`（`stop_instance` 的意图记录判定）。
- 机理：是否记录停止意图只看三个条件——`_restoring_devices` 命中、`device_state is None`、**`DeviceRuntimeState.connected is False`**。而 `DeviceRuntimeState.connected` 仅在「采集 tick 连接级失败」（`DeviceRuntime.report_read_failure` → `mark_read_failure(connection_level=True)`）或「连接尝试失败」时翻转；真实 transport 状态（`CollectorDeviceSession.is_open()`，即系统状态页 `devices_connected` 的口径）此刻早已是 False。设备被 kill/拒连后、下一个失败 tick 记账前存在窗口；POLL 周期越长窗口越大。操作员在状态页看到设备离线即刻 stop（正是 CFG-03 用例第 120-126 行的时序：先 `wait_status(devices_connected==0)` 再 stop），stop 走 RUNNING 分支正常关闭句柄，**但不写 `_recovery_stopped`**；随后服务器恢复、Watchdog 重连成功，`start_device_tasks` 无门控依据，把实例重新拉起。
- 真实进程复现（与第五轮同一命令，Modbus mock server + Redis + gRPC 控制面）：
  `pytest tests/reliability/recovery/test_reconnect_reload_race.py::TestStopTaskDuringReconnect::test_task_stays_stopped_after_device_recovery`
  → **FAILED（14.39s）**：`AssertionError: device recovery resurrected a stopped task: {'modbus-telemetry:modbus-1': 'running'}`，且停机静默期后时间戳恢复增长。main `8b6f654a` 同用例第五轮已取证 PASS，仍为本分支 RELATED 回归。
- 组件级复现：`test_task_stopped_during_outage_stays_stopped_after_recovery`（strict xfail，`--runxfail` 下真实 FAILED at test_watchdog_restore.py:170）。
- 修复建议（供生产方参考，验收方不改码）：意图判定应以**实时 transport 事实**为准——经 DeviceRuntime 暴露 `session.is_open()`（或统一「设备当前不可用」谓词：`not is_open() or state.connected is False or device in restoring`），而非仅轮询簿记；等价地，Watchdog 在 health 判定 unhealthy、决定重连的时刻即应把 `DeviceRuntimeState.connected` 置 False，使两处口径一致。注意保持 stop 与恢复并发时的实例锁线性化（现有 `_instance_guard` 已足够，不要绕开）。

### 2.4 残留缺陷 D6-b（P1，阻断）：从未启动的任务被恢复无条件拉起

- 位置：`src/collector/application/task_runtime.py:244-266`（恢复循环对所有启用实例 `_start_locked`，门控仅依赖显式 stop 才会写入的 `_recovery_stopped`；从未启动的实例没有任何信号可进入该集合）。
- 复现：`test_never_started_task_stays_stopped_after_reconnect`（strict xfail，`--runxfail` 下真实 FAILED at test_watchdog_restore.py:143）；探针 S3 同构。
- 修复建议（第五轮报告已给出，仍未落地）：恢复集应在**断线时刻快照「当时 RUNNING 的实例」**（或由 Watchdog close 路径/恢复入口即时计算），只恢复快照内且未被显式 stop 的实例；与 `sync_instances` 热更新只恢复 `was_running` 的语义对齐。该修复同时消除「热更新删除并同名重建任务后，停止意图被注销路径 discard、重连即被拉起」的边界（本轮走读确认该边界存在，随 D6-b 一并关闭）。

### 2.5 第五轮两个 strict xfail 处置

按验收要求「修复确认后才移除 xfail」：两例在 `a105326a` 上 `--runxfail` 均真实 FAIL，**xfail 标记全部保留**（套件口径 13 passed / 2 xfailed，未用 XPASS 粉饰）。另新增 1 例真实 PASS 的回归测试，锁定 `a105326a` 已修复的时序窗，防回退：

- `test_stop_after_disconnect_bookkeeping_persists_across_recoveries`：簿记断线后 stop → 连续 3 次重连保持 STOPPED → 手动 start 解除限制 → 再断线重连恢复 RUNNING。

其余 12 例第五轮专项（冷启动不采集、POLL/SUBSCRIBE/LISTEN 恢复、禁用不展开、重连失败不动句柄、重复恢复单句柄、手动/热更新并发无重复句柄、停机闸门、实例间中止、健康设备隔离）全部 PASS。

## 3. R07 格式与静态检查（conda env wind-hub / Python 3.11.16）

| 命令 | rc | 结果 |
|---|---|---|
| `ruff check src tests scripts` | 0 | All checks passed |
| `ruff format --check src tests scripts`（修复前） | 1 | 分支 100 个脏文件；与 main `8b6f654a`（105 个）集合对比，新增漂移 1 个：`src/core/infrastructure/protocol/ads/subscription.py`（第 140 行后缺空行）；另有 `src/collector/application/task_runtime.py` 虽在 main 脏集合内，但漂移点全部位于本分支新增方法（`start_device_tasks` 签名/包装行），按 R07 指定一并修复 |
| `ruff format --check src tests scripts`（修复后） | 1 | 余 98 个脏文件**全部**属于 main 既有脏集合，新增漂移集合 **0**（UNRELATED 基线，与第五轮规则一致） |
| `mypy src` | 0 | Success: no issues found in 292 source files |
| `lint-imports` | 0 | Contracts: 11 kept, 0 broken |

验收方格式修复（纯机械，无语义改动）：

1. `src/core/infrastructure/protocol/ads/subscription.py`：+1 空行（第五轮 R07-b 残留，`d556845b` 未覆盖该处）。
2. `src/collector/application/task_runtime.py`：ruff format 收敛 3 处包装行（净 −7 行），无任何 token 语义变化。

未新增/放宽任何 noqa、type-ignore、quality 配置；生产侧 ignore 仍仅 4 处 pyads `import-untyped`。

## 4. 全分层回归（worktree 内，均带 `PYTHONPATH=src`）

| 命令 | rc | 结果 | 对第五轮 |
|---|---|---|---|
| `pytest tests/unit tests/component tests/contract --import-mode=importlib` | 0 | **1559 passed, 2 xfailed（D6 strict），53.47s** | 1558 → +1（新增 D6 回归例） |
| ADS 组件/单元专项（ads unit + component ads + watchdog 专项） | 0 | **121 passed, 2 xfailed** | D1–D5 无回归 |
| ADS 真实协议栈（AdsTestServer integration + reliability core/recovery 三文件） | 0 | **21 passed** | 持平 |
| `pytest tests/integration --import-mode=importlib` | 1 | **128 passed, 1 failed**：`test_gateway_r2_fc15.py::test_fc01_readback_matches_point_store` | 同一例；main worktree 同机 0.27s 复现失败 → **UNRELATED** |
| `pytest tests/system --import-mode=importlib` | 0 | **78 passed，117.00s** | 持平 |
| `pytest tests/reliability --import-mode=importlib` | 1 | **46 passed, 1 failed（CFG-03，D6-a RELATED）, 3 skipped, 10 deselected，271.25s** | 与第五轮同一失败，未修复 |
| Fast Gate `fast --part backend-static` | 0 | PASS（ruff/mypy/import-linter） | 持平 |
| Fast Gate `fast --part backend-tests --targets unit,component,contract` | 0 | PASS | 持平 |
| PR Gate `pr --targets integration-protocol,system-acquisition` | 1 | 110 passed, 1 failed（同一 FC15，UNRELATED） | 持平 |
| Merge Gate `merge --file-list=<14 个变更生产文件>` | 1 | **1238 passed, 2 xfailed, 1 failed=CFG-03（D6-a），177.40s**；ruff/mypy/import-linter 均 PASS | 第五轮 1237+2xfail+CFG-03 → 新增回归例计入 |
| 真实 TwinCAT L3（PLC 通知周期/TC2·TC3 端口/长稳/`kill -9` 句柄回收/SIGKILL 跨进程） | — | **NOT RUN**（无硬件、无子进程 harness） | 与第四/五轮持平，合并后硬件门禁必须补跑 |

UNRELATED 取证：FC15 用例在 `/home/luosh/wind-hub-main`（main @ `8b6f654a`）单测隔离复现失败（0.27s，rc=1）；本分支不触碰 Modbus FC15 路径。netem 矩阵为真实执行（非 userns 空转），3 skipped/10 deselected 为环境选择口径，与第五轮一致。

## 5. R01–R10 门禁

| 门禁 | 结论 | 证据 |
|---|---|---|
| R01 旧 Driver/Session/Subscription 生产代码删除 | PASS | ads 生产包 6 文件，结构无回流 |
| R02 无兼容层/新旧接口并存 | PASS | 三协议 subscribe 契约一致，无旧调用残留 |
| R03 旧测试/fixture 清理且不减测 | PASS | 第五轮删除的 5 个孤儿 support 保持删除；测试净增 |
| R04 旧配置清理 | PASS | 死选项零命中（与第五轮同口径） |
| R05 死代码清理 | PASS | 走读 task_runtime 未见回退分支 |
| R06 Ruff Lint | **PASS** | rc=0 |
| R07 Ruff Format | **PASS（验收方机械修复后）** | 新增漂移集合 0；余 98 文件全在 main 基线 |
| R08 Mypy | **PASS** | rc=0，292 files |
| R09 Import Linter | **PASS** | 11 kept, 0 broken |
| R10 不绕过检查/不新增忽略 | PASS | 无新增 noqa/ignore/配置放宽 |

## 6. 生产缺陷清单（交回生产方修复，验收方不改业务码）

| 编号 | 级别 | 缺陷 | 位置 | 最小复现 | 修复建议 |
|---|---|---|---|---|---|
| D6-a | P1 阻断 | stop 的停止意图只认 `DeviceRuntimeState.connected` 簿记；transport 已断、簿记滞后窗口内的显式 stop 不入 `_recovery_stopped`，重连复活（CFG-03 真实进程回归） | `src/collector/application/task_runtime.py:218-225`；状态口径分叉于 `services.py` status 用 `session.is_open()` vs `device_state.connected` | 真实进程：`tests/reliability/recovery/test_reconnect_reload_race.py::TestStopTaskDuringReconnect::test_task_stays_stopped_after_device_recovery`（14.39s FAIL）；组件：strict xfail `test_task_stopped_during_outage_stays_stopped_after_recovery` | 意图判定改用实时 transport 可用性（经 DeviceRuntime 暴露 `is_open()`，或 Watchdog 判定 unhealthy 即刻翻簿记状态） |
| D6-b | P1 阻断 | 恢复集 = 设备全部启用实例，从未启动的任务无停止意图信号，重连即被拉起；同名任务删除重建后停止意图同样失效 | `src/collector/application/task_runtime.py:244-266` | strict xfail `test_never_started_task_stays_stopped_after_reconnect`（:143 真实 FAIL） | 断线时刻快照 RUNNING 实例集，恢复限定为「快照内 ∖ 显式停止」，与 `sync_instances` 的 was_running 语义对齐 |

修复后验收方动作：去 2 个 strict xfail 转真实 PASS、CFG-03 复跑、全分层与四门禁复跑；真实 TwinCAT L3 仍 NOT RUN。

## 7. 合并结论

**不可合并（FAIL）**。方案 A 的并发线性化（S4）、停止意图跨多次重连保持与手动启动解除（S1）已正确实现并有新增回归测试锁定；R07 已清零、静态门禁全绿、D1–D5 无回归。但方案 A 的两条核心契约——「断线期间用户显式停止的任务不得复活」（真实进程 CFG-03 仍 FAIL）与「Collector 冷启动不自动启动 → 从未启动的任务不得被重连顺带拉起」——均未满足，两个 strict xfail 不能移除。建议生产方按第 6 节修复后交第七轮。

验收方全程未改生产业务代码（仅 2 处授权的 ruff 机械格式化）、未合并 main、未建/合 PR、未删除生产分支；`verify/ads-round6` 已推送。

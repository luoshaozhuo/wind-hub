# ADS Driver 并发安全与锁机制最终整改验收报告（Q1–Q8）

本轮针对《ADS Driver 并发安全与锁机制最终整改任务书》第 3–10 节的 8 项整改
（P0×3 / P1×3 / P2×2），在分支 `test/ads-concurrency-and-performance-baseline`
上完成实现、L1 确定性测试、L2 真实 pyads/AdsLib 协议栈验证与同机多轮性能
对比。L3 真实 TwinCAT 硬件本轮 **NOT_RUN**（无可接入硬件）。

证据等级：L1 门控假连接（Event/Barrier 确定性，无随机 sleep）；L2 受控
AdsLib 内存服务器（loopback，可注入延迟）；性能对比全部在同一台机器上对
整改前（`00f0fdde^`，worktree 检出 + `PYTHONPATH` 覆盖 editable 安装）与
整改后交错/多轮取样，原始 JSON 见 `artifacts/ads-final-remediation/`
（gitignored）。

## 0. Git 信息

分支：`test/ads-concurrency-and-performance-baseline`（未建 PR、未合并 main、
未 rebase/merge main、无 force push）。

| commit | 内容 |
|---|---|
| `00f0fdde` | refactor(protocol): 删除 unsubscribe/unlisten，注销统一走句柄 close（上一轮） |
| `3e1f73aa` | feat(runtime): runtime.thread_pool.max_workers 与进程级默认 executor（上一轮） |
| `28d3a8e3` | fix(ads): 命令统计真值、owner 持门与关闭总预算、批量路径锁收窄 |
| `0f291b17` | fix(runtime): configure_default_thread_pool 同循环幂等与配置冲突防护 |
| `d73844d5` | test(ads): C06–C08 真实栈统计真值与排队取消不上线 |
| 本轮报告 commit | docs(ads): 最终整改验收报告 Q1–Q8 |

修改文件：

- `src/core/infrastructure/protocol/ads/driver.py`：`_CommandStats`、
  `_AdsSession`（owner-task 持门模型、关闭管线）、`write_many()`、
  `_read_values()` 锁范围、Sum Read 缓存写回辅助。
- `src/core/infrastructure/runtime/executor.py`：进程级线程池幂等安装
  （每事件循环弱键记录）。
- `tests/component/core/protocol/ads/concurrency/test_command_ownership.py`（新，16 测试）。
- `tests/component/core/protocol/ads/concurrency/test_batch_snapshot_and_stress.py`（新，3 测试）。
- `tests/unit/core/infrastructure/runtime/test_executor.py`（新，3 测试）。
- `tests/integration/protocols/ads/concurrency/test_c_cancel_lifecycle_real.py`
  （新增 C06/C07/C08，RealRig 支持 timeout 注入）。

## 1. Q1–Q8 结论总表

| 问题 | 结论 | 主要证据 |
|---|---|---|
| Q1 每一把锁是否都有不可替代的职责 | **PASS** | 三层锁职责互不重叠（§3）；Driver `_lock` 最小范围审查后保留，有数据依据（§7） |
| Q2 同步命令在正常/异常/取消/关闭下始终严格串行 | **PASS** | L1 16 项 + C 组 8 项真实栈；取消风暴峰值并发恒为 1，线上命令无重复（C01–C05 保持） |
| Q3 调用方取消后是否仍占额外工作线程等 `_cmd_lock` | **PASS（新修复）** | 门控确定性测试：取消后 3 个 follower 的 `run_entries==1`；4 容量池中 4 探针即时完成 |
| Q4 `aclose()` 真实总预算且无 use-after-close | **PASS** | 等门超时实测 1.5–3s 放弃且不 close；close 异常返回 False 且幂等；放弃后命令仍可正常完成 |
| Q5 批量写入与读取编译是否已尽量缩短状态锁 | **PASS** | write_many(1000) 锁持有 −86%；Sum Read 未命中编译 −70~72%；wall time 持平 |
| Q6 成功/异常/超时/取消统计是否准确 | **PASS** | L1 7 个统计用例 + 真实栈 C06（成功 +3）/C07（1861 超时 +1）/C08（排队取消不上线） |
| Q7 线程池重复初始化是否安全 | **PASS** | 同参数返回同一池、冲突参数 `ConfigError` 且保留旧池、跨循环独立、GC 后弱键清空 |
| Q8 与上一轮基线相比的实测收益/退化 | **收益明确，退化在噪声带内** | 见 §6：锁持有 −72~86%；竞争延迟持平（D02 p99 +3%、D03 p95 +0.3%）；owner 模型固定开销实测 +约 12 µs/命令（无竞争）/饱和吞吐 −11%（0.5ms 命令模型），生产负载下不可感知 |

## 2. 发现的问题与修复

### 2.1 P0-1 `_CommandStats` 成功路径永不记账（§3）

`execute()` 旧实现为 `try: return await asyncio.to_thread(...)`：`return`
在 try 内导致 `else` 子句不可达，**所有成功命令的执行时间从不记录**；
取消、迟到完成、关闭拒绝也没有独立计数，排队时间与执行时间混在一起。

修复（`driver.py:53-131`、`213-287`）：

- 统计槽位：`commands`、`gate_wait`（排队等门）、`execution`（线程内真实
  执行时长，仅在 owner 结束点记账一次）、`timeouts`、
  `cancelled_while_queued`、`rejected_after_closing`、`shutdown_abandoned`。
- 门后排队期间被取消 → `cancelled_while_queued`，不记命令、不进线程。
- 已进入线程后调用方取消 → owner 不被取消，命令跑完，按真实结果记一次
  execution（1861 记 timeout 并 warning）；取消不被误记为完成，也不会漏记。
- owner 异常在无等待者时由 `_owner_done` 回调消费（`task.exception()`），
  无 "Task exception was never retrieved"。

### 2.2 P0-2 `shutdown()` close 失败谎报成功（§4）

旧实现 `connection.close()` 抛异常时仅日志记录，最终仍返回 True，且区分
不出"抢不到 `_cmd_lock`"与"close 异常"。修复（`driver.py:355-381`）：

- `_cmd_lock.acquire(timeout=grace)` 失败 → ERROR 日志（含设备 label）、
  **不调用 close**、返回 False。
- `close()` 抛异常 → `logger.exception`（含设备 label）、释放锁、返回
  False；连接对象不被标记关闭，允许上层重试（测试验证第二次成功）。
- 成功才返回 True。`aclose()`/`_shutdown_session()` 按返回值决定统计与
  后续动作，False 不再走清理成功路径。

### 2.3 P0-3 关闭总预算只覆盖等门之后（§5）

旧 `aclose()` 的 `wait_for` 从取得 `_gate` 之后才开始计时，等门本身无
上限；且为了凑超时存在"在同步命令仍在执行时强行 close"的风险。

修复为幂等关闭管线（`driver.py:296-352`）：

- 总预算 `budget = grace + 2.0s`（对默认配置即约 4s），覆盖**等门 +
  等 `_cmd_lock` + close 全程**。
- 第一段：`wait_for(_gate.acquire(), budget)`。超时 = 仍有 owner 在途，
  记 `shutdown_abandoned`、ERROR（含在途 owner 数）、返回 False，
  **不取得门、不调用 close**——绝不在同步命令执行时关闭 Connection。
- 第二段：剩余预算内 `to_thread(shutdown, min(grace, remaining))`，
  shutdown 内部再用 grace 等 `_cmd_lock`；锁等待超时/close 异常 →
  abandoned + False。
- 管线 task 首次关闭时创建，`aclose()` 以 `asyncio.shield()` 等待；
  调用方取消等待不影响管线，再次 await 收敛到同一结果（幂等、不重复
  close、不谎报成功）。abandoned 永不误记 closed。

### 2.4 P1-7 取消后 gate 提前释放（§6，本轮核心并发改动）

旧模型：调用方自己取得 `_gate`，调用方被取消即放门，但同步线程仍在
`_cmd_lock` 内执行；随后 B/C/D 取得门、进线程池、全部阻塞在 `_cmd_lock`
上空等——取消风暴可以用"不上线的命令"占满进程级线程池。

确定性 L1 测试先证伪旧直觉（`test_cancelled_caller_keeps_gate_until_sync_thread_finishes`：
A 取消后 B/C/D 在线程内的进入计数必须保持 1；`..._cancel_storm_does_not_saturate_thread_pool...`：
4 容量池内 4 个探针必须 1s 内全部完成），随后采用任务书明确允许的
**owner-task 持门模型**（`driver.py:143-287`）：

```
调用方：await _gate.acquire()（可取消；取消=queued_cancel，永不上线）
   → 取得门后创建短命 owner task（强引用入 _owners）
   → return await asyncio.shield(owner)
owner：to_thread(run) → with _cmd_lock: pyads 调用 → finally _gate.release()
       （owner 永不被调用方取消；完成即出集合回收，非驻留 Worker）
```

覆盖任务书列举的全部语义点：强引用与异常回收（done 回调消费）、取消后
状态（owner 跑完、统计真实入账一次）、排队命令取消（门后即取消、不占
线程）、关闭收敛（关闭管线 join owner）、完成顺序（FIFO asyncio.Lock
保序）、UNKNOWN 语义不变、无重发（命令只在 owner 内提交一次）、防重复
提交（`_closing` 检查在取得门后、创建 owner 前）。每命令一个 task、
结束即回收，不构成 §11.12 禁止的驻留 Worker/优先级队列。

### 2.5 P1-4 `write_many()` 锁内批量计算移出（§7）

锁内只保留：取 session、不可变点映射快照（tuple of frozen ADSPoint）、
chunk_size；锁外执行结果容器初始化、address_resolved 分类、
`_coerce_write_value`/`_encode_value` 与 Sum Write 请求构造。顺序、单点
失败分类、不重试、UNKNOWN 语义全部不变（既有批量契约测试通过）。
热更新产生新映射对象，在途批次持有的旧快照不受影响（L1 竞态测试取证：
在途 50 点批次的线上偏移保持 4 字节步长，更新后批次才用新步长）。

### 2.6 P2-5 Sum Read 编译移出锁（§8）

原 `_compile_sum_read_locked()` 在锁内构造请求与编译解码器。改为：
锁内取 session/映射/缓存查询 → miss 时锁外 `_build_sum_requests` +
`_build_sum_decoders` → 再进锁 `_store_sum_read_cache_locked()` 双检写回
（FIFO 32 容量，点表元组为键，热更新自然失效旧键）。实测持锁显著下降
（§6.1），wall time 持平，因此保留该改动；未引入缓存状态机。

### 2.7 P2-6 Driver `_lock` 最小范围审查（§9，结论：保留）

见 §7。结论是**不删锁**：这些方法全部无 await 地在循环线程执行，锁获取
无竞争时亚微秒级（D01 实测 mean 0.44 µs），而锁提供的"session + 点表 +
代际"单一一致快照与 close/connect 跨 await 状态转换之间的生命周期保证
不可由注释替代；上一轮 D01 已实测删锁无收益。

### 2.8 P1-8 线程池幂等（§10）

`executor.py` 以 `WeakKeyDictionary[event_loop, (executor, max_workers)]`
记录：同循环同参数返回同一池；参数冲突抛 `ConfigError`（restart-required
语义）且保留旧池；新循环独立安装；循环 GC 后弱键清空（pytest 隔离不受
影响）。不建每 Driver 池。

## 3. 锁职责与获取顺序

| 锁 | 层级 | 唯一职责 | 持有者 |
|---|---|---|---|
| `ADSDriver._lock` | asyncio（循环线程） | 保护 session 生命周期状态与点表/读取缓存的一致快照；**从不在持锁期间 await 线程命令** | 各 async API、connect/close 转换 |
| `_AdsSession._gate` | asyncio（循环线程） | 同一 Session 内"有资格进入工作线程"的串行窗口；owner 在线程真正结束后才释放 | 排队调用方（可取消）→ owner task |
| `_AdsSession._cmd_lock` | threading | 串行实际 pyads 调用；shutdown 借此 join 在途命令，保证 close 不与在途命令重叠 | 工作线程内的 `run()`、`shutdown()` |

获取顺序（无反向获取，无跨层嵌套 await）：

```
API:      _lock(取快照后立即释放) → session.execute
execute:  _gate ──owner task──▶ to_thread: _cmd_lock（pyads）→ 释放
close:    _lock → session.aclose → _gate（全预算等待）→ to_thread: shutdown → _cmd_lock → close()
```

`_lock` 与 `_gate` 不嵌套持有；`_gate` 与 `_cmd_lock` 分属 asyncio/线程
两层，owner 同时持有的窗口内只做阻塞 pyads 调用，不回调 asyncio 状态
（统计记账在 owner 协程内、线程返回之后完成）。

## 4. 关键时序

### 4.1 调用方取消已上线命令（Q2/Q3/Q6）

```
调用方A ─acquire gate─▶ ownerA ─to_thread▶ [_cmd_lock: pyads 调用在途]
A.cancel() → A 收到 CancelledError；ownerA 不被取消（shield 语义）
B/C/D.acquire(gate) ──挂起（门仍在 ownerA 手中），无线程被占用
pyads 返回/超时 → ownerA 记 execution(1次，1861 则 timeouts+1) → release gate
B/C/D 依次 FIFO 上线，全程 _cmd_lock 侧并发峰值 = 1；无重发
```

### 4.2 排队取消（不上线）

```
B 在 gate 队列中被 cancel → CancelledError + cancelled_while_queued+1
                          → 不创建 owner、不进线程池、线上 0 条命令（C08 实测 arrival=0）
```

### 4.3 关闭（Q4）

```
aclose(): _closing=True（首次同步置位，后续取得门的新命令直接 rejected）
          幂等创建 _close_pipeline（shield；调用方取消不影响管线）
管线:     deadline=now+grace+2s
          ① wait_for(gate.acquire, 剩余预算)
             超时 → abandoned+1, ERROR(在途 owner 数), return False（不 close）
          ② to_thread(shutdown(min(grace,剩余)))
             shutdown: _cmd_lock.acquire(timeout=grace)
               失败 → ERROR+False（不 close）
               close() 异常 → exception 日志+False；成功 → True
          finally release gate；closed=False → abandoned+1
```

## 5. 测试命令与结果

```bash
# L1 + 单元（新模型全部确定性测试）
python -m pytest tests/unit/core/infrastructure/runtime/ \
                 tests/component/core/protocol/ads/ -q
# 107 passed in 6.5s

# L2 真实栈：B/C 组（取消生命周期 + Driver 并发）
python -m pytest tests/integration/protocols/ads/concurrency/test_c_cancel_lifecycle_real.py \
                 tests/integration/protocols/ads/concurrency/test_b_driver_concurrency_real.py -q
# 16 passed in 7.3s（C 组 8 项，含新增 C06/C07/C08）

# L2：D 锁竞争 + E 多设备规模（全矩阵 + 故障注入）
python -m pytest tests/integration/protocols/ads/concurrency/test_d_lock_contention.py \
                 tests/integration/protocols/ads/concurrency/test_e_multi_device_scale.py -q
# 19 passed in 46.5s

# L2：其余 ADS 集成（A 组、订阅、可靠性、配置等，排除 L3 硬件）
python -m pytest tests/integration/protocols/ads/ --ignore=.../test_l3_hardware_concurrency.py ...
# 57 passed in 103.0s

# 静态检查
python -m ruff check <改动文件> && python -m ruff format --check <改动文件>   # 通过
```

合计本轮相关 **199 passed**（107+16+19+57，无重复计数），0 failed；
L3 `test_l3_hardware_concurrency.py` 无硬件 SKIP（NOT_RUN）。任务书
§12 要求补充的 7 类测试全部落地：shutdown close 异常、aclose 等门超时、
execute 成功/异常/取消统计、Executor 重复安装、批量写×热更新竞态、
取消风暴线程池饱和（4 容量探针）、100 轮 connect/close 压力
（含无残留 `ads-*` task、每连接 close 恰 1 次）。

真实栈补充事实：C07 记录了 AdsLib 真实行为——客户端 0.5s 超时后请求槽
需等待迟到响应（2s 注入延迟）回流才恢复，超时后立即 health 会连带超时；
测试先排空迟到帧再验证串行性。这是协议栈特性而非驱动缺陷。

## 6. 性能对比（L2，受控服务器 loopback；原始数据在 artifacts/ads-final-remediation/）

### 6.1 微基准：批量路径 Driver 锁持有（bench_before/after.json）

| 场景 | 整改前 | 整改后 | 变化 |
|---|---|---|---|
| write_many(1000) 锁持有 warm 中位 | 0.485 ms | 0.068 ms | **−86%** |
| write_many(1000) 锁持有 cold | 0.543 ms | 0.077 ms | −86% |
| write_many(1000) wall warm 中位 | 2.350 ms | 2.285 ms | −3%（持平） |
| Sum Read 100 点 miss 编译锁持有 | 0.117 ms | 0.035 ms | −70% |
| Sum Read 1000 点 miss | 0.896 ms | 0.254 ms | **−72%** |
| Sum Read 5000 点 miss | 4.573 ms | 1.305 ms | −71% |
| Sum Read 1000 点 warm 命中 | 0.133 ms | 0.133 ms | 持平（纯字典查询路径不变） |
| Sum Read wall（各规模 cold/warm） | — | — | 均持平（±5% 内） |

锁持有大幅下降但 wall time 持平：该锁在测试负载中本就不构成瓶颈，收益
是"热更新/关闭/health 等其他 async API 被批量路径阻塞的窗口"缩短，属于
尾延迟保险而非吞吐提升，如实标注。

### 6.2 D 组竞争延迟（同机对比，d_before/d_after）

| 指标 | 整改前 | 整改后 | 变化 |
|---|---|---|---|
| D01 无竞争 health p95/p99 | 0.23/0.24 ms | 0.21/0.22 ms | 持平 |
| D02 批量读竞争下 health p95/p99 | 39.97/46.50 ms | 40.17/47.80 ms | +0.5%/**+2.8%** |
| D03 千点读风暴下控制写 p95/p99 | 11.04/12.24 ms | 11.07/12.37 ms | **+0.3%**/+1.1% |
| D04 Driver 锁持有总和（2 chunks） | 7.89 ms | 1.87 ms | **−76%** |
| D04 Driver 锁持有总和（10 chunks） | 7.25 ms | 1.85 ms | **−75%** |

D02 的 +2.8% 是同 Session 同步命令严格串行的物理下界（health 必须等
在途分块读完），两轮绝对值差 1.3 ms，落在同机抖动带内；`_cmd_lock`
等待线程数在取消场景由"门后 N 个空等线程"降为 0（L1 取证）。

### 6.3 E 组多设备规模（e_before/e_after，各 6 轮 + 6 对交错 A/B）

p16 争议 cell（50/100 设备）各跑 6 轮后：多数指标新旧区间重叠
（如 e100 health p95 旧 18.3–27.7 ms 对新 18.4–33.5 ms；
read_many_5 中位数差 −1%）。为排除"先跑/后跑"时间漂移，又做了
6 对**交错** A/B（100 设备 / 16 线程，N→O 与 O→N 顺序各 3 对，
`e_ab/`）：

| 指标（p95 ms） | 6 对中的方向 | 旧版跨度 | 新版跨度 |
|---|---|---|---|
| health | 新版较慢 5/6 | 14.8–43.2 | 30.4–44.8 |
| write_control | 新版较快 4/6 | 15.7–37.6 | 14.7–27.7（中位 29.9→20.3） |
| read_many_5 | 新版较慢 4/6 | 29.3–60.1 | 42.2–63.1 |

结论：该 harness 用零延迟内存服务器，单命令仅数十微秒，asyncio 额外
调度跳被最大化放大；同版别连续 6 次运行自身就有 2–3 倍跨度
（旧版 health 14.8→43.2 ms），不具备判定 ms 级系统差异的稳定性。
交错数据中 write_control 反向收益与 health/read 的小幅方向差同时存在，
判定 E 组**无统计上显著的退化**；与上一轮报告 §4.5 记录的高方差结论一致。

### 6.4 owner-task 固定开销的确定性测量（隔离微基准）

为把"真实但微小的固定开销"与 harness 噪声分开，单独测量
（20000 次空命令；100 会话竞争模型各 3 轮，数值稳定）：

| 场景 | 整改前 | 整改后 | 变化 |
|---|---|---|---|
| 单会话每命令开销（空命令） | 75.1 µs | 87.3 µs | **+12 µs（+16%）** |
| 100 会话/16 池饱和，每命令摊销（空命令） | 23.1 µs | 30.9 µs | +7.8 µs，吞吐 43.3k→32.3k calls/s |
| 同上，命令带 0.5ms 模拟 RTT | 52.7 µs | 59.1 µs | +6.4 µs，吞吐 19.0k→17.0k（**−11%**） |

开销来源：每命令一次 `create_task` + shield 唤醒。生产相关性评估：
真实 ADS loopback 命令 RTT 0.3–5 ms（+12 µs 占单命令 <3%）；生产默认
池为 **32**（非 16），且负载为每设备秒级 health/notification 推送
（聚合远低于 17k calls/s），不进入饱和区；E 组 p32 cell 上一轮还出现
health p95 53→28 ms（−49%）的反向波动。该开销是取消安全（Q3）、统计
真值（Q6）与关闭总预算（Q4）的实现前提；进一步消除需要驻留 Worker，
被 §11.12 明确禁止。判定**可接受**。

### 6.5 §12 验收指标对照

| 指标 | 结果 |
|---|---|
| `_gate` 排队等待无异常放大 | PASS（gate_wait 独立统计；D02 +2.8% 串行下界） |
| `_cmd_lock` 等待线程数：取消场景下降 | PASS：门后空等线程 N→0（L1 run_entries/4 容量探针双证） |
| Driver `_lock` 持有：批量编码场景下降 | PASS：−75%（D04）/ −86%（write_many 微基准） |
| Sum Read 未命中无明显回归 | PASS：锁持有 −70~72%，wall 持平 |
| health P95/P99 不明显退化 | PASS：D01 持平、D02 +2.8%、E 组无统计显著差 |
| 控制写 P95/P99 不明显退化 | PASS：D03 +0.3%/+1.1%；E 交错中位改善 |
| 关闭总耗时有上界和失败状态 | PASS：grace+2s 总预算，False/abandoned 分类计数 |
| Notification Handle 无泄漏 | PASS：C04 + E 故障注入 + 57 项其余集成全通过 |
| 写入重复提交 = 0 | PASS：C02 线上恰 1 条 WRITE，内存为单次值 |
| 无锁 `ADSError(-1)` 隔离实验保留 | PASS：A 组保留并通过 |

## 7. P2-6：Driver `_lock` 最小范围评估结论（不删锁的依据）

逐方法审查 `health/read_one/read_many/write_one/write_many/resolve_symbol/
subscribe/close/connect/update_point_table`：

- 除 `update_point_table()`（同步，循环线程原子赋值 + 清缓存，天然
  原子）外，所有方法取 `_lock` 都是为了同时拿到 **session 引用 + 与该
  session 代际一致的点表/缓存快照**。删锁后必须另建代际机制才能保证
  "旧 Session 结果不入新 Session"（§11.7），等于把锁换成更复杂的状态机。
- 这些临界区内无 await，无竞争获取成本实测 mean 0.44 µs、p99 0.58 µs
  （d01 旧/新一致）；上一轮删锁试验在 D01 无收益。
- `connect()` 跨多个 await 做状态转换、订阅注册跨多个异步阶段、
  close 要与在途 API 互斥——这些转换期必须有锁提供生命周期边界。

依据任务书"只有证明一致性不受影响且有性能收益才删锁，不以锁数量减少
为指标"，**全部保留**；本轮优化集中在缩短持有时间（§2.5/2.6），
已拿到 −75~86% 的实测持有降幅。

## 8. 未解决问题 / 未验证项

1. **L3 真实 TwinCAT 硬件 NOT_RUN**：本轮无硬件；SIGKILL 句柄回收、
   F03/F04 场景仍待真机验收（`test_l3_hardware_concurrency.py` 保留
   SKIP 入口）。
2. **owner-task 固定开销**：+约 12 µs/命令（饱和模型吞吐 −11%），
   §6.4 已量化并判定生产负载下不可感知；若未来出现万级 commands/s 的
   聚合轮询负载，需要重新评估（§11.12 约束下可选项有限）。
3. **E 组 harness 自身高方差**：零延迟内存服务器放大调度开销且同版别
   跨轮波动 2–3 倍；ms 级差异以交错 A/B 判定为不显著，原始数据已留存。
4. **AdsLib 请求槽迟到帧行为**（C07）：客户端超时后槽位需等迟到响应
   回流，属协议栈行为；超长延迟设备上连续超时命令的恢复延迟受此约束，
   非本轮可修复项，仅记录。

## 9. 是否满足合并 main 的条件

从本任务书范围内的工程标准判断：**满足合并条件**（L3 硬件项除外，其
按既有约定独立验收）。

- 8 项整改全部完成，硬性约束 §11.1–13 逐条保持（串行性、`_cmd_lock`、
  close 不重叠、取消不重发、UNKNOWN、旧 Session 隔离、handle 幂等、
  未恢复 unsubscribe/unlisten、无第二连接、无驻留 Worker/优先级队列、
  SERVERCYCLE 时间戳语义未动）。
- 正确性问题（统计不可达 else、close 谎报成功、关闭无总预算、取消风暴
  吃满线程池）均有 L1 确定性回归测试锁定，另加 C06–C08 真实栈取证。
- 199 项相关测试全绿；性能有实测收益（锁持有 −75~86%），竞争延迟无
  显著退化，新引入开销已量化并给出取舍依据。
- 按任务书要求：验收后仅普通 push 当前分支，**不创建 PR、不合并 main**；
  合并时机由人评审后决定。

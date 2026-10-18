# ADS 并发安全与性能优化验收报告

本轮在基线（`BASELINE_REPORT.md` / a4cba89f）之上实施 Driver 锁收窄、
Session 两级命令门、注销接口收敛与进程级线程池配置。本报告回答任务书
Q1–Q6，证据等级与基线一致：L1 确定性门控、L2 真实 pyads/AdsLib 帧栈 +
受控服务器、L3 真实 TwinCAT（本轮未运行）。

> 基线已证伪的归因不再使用：同一 pyads Connection 并发同步命令失败的
> 机理是 **AdsLib 本地 AMS 端口/请求槽竞争**（`ADSError(-1)`、
> `Port: 30001 already in use`），实验中 **未观测到 AMS 帧错配**
> （0 mismatch），但竞争命令多数未上线，故帧串扰只能标记
> INCONCLUSIVE 而非证伪。生产代码与注释中的措辞已据此修正。

## 0. Git 与范围

- 分支：`test/ads-concurrency-and-performance-baseline`（未新建分支、
  未切 main、无 PR、无 merge/rebase、无 force push）。
- 基线：`a4cba89f`（A–F 基线报告提交）。
- 本轮提交：
  - `7d93cc37` perf(ads): Session 两级命令门串行化，Driver 状态锁不再覆盖网络往返
  - `00f0fdde` refactor(protocol): 删除 unsubscribe/unlisten，注销统一走句柄 close
  - `3e1f73aa` feat(runtime): runtime.thread_pool.max_workers 与进程级默认 executor
- 原始测量数据（gitignored）：`artifacts/ads-optimization/`
  （D 组基线/优化各 2–3 轮、E 组基线 3 轮/优化 3 轮、故障 cell）。

## 1. Q1–Q6 结论总表

| 问题 | 结论 | 主要证据 |
|---|---|---|
| Q1 是否完整保留共享 Connection 同步命令串行安全 | **PASS** | A–E 真实栈 70 passed/2 skipped（L3）；所有 cell 服务端同步命令峰值并发 = 1；`_cmd_lock` 保留 |
| Q2 新锁结构是否更简洁、更易理解 | **PASS（定性）** | 每把锁职责单一且写入类/方法 docstring；无驻留 worker、无命令队列、无第二连接、无重试/优先级调度 |
| Q3 收窄 `_lock` 后 health/控制写 P95/P99 是否改善 | **部分改善（PASS with measured caveat）** | 单分块 health 排队持平（串行化物理约束）；千点风暴控制写 p95 −18%、p99 −17%；状态锁持有与 chunk 数脱钩（26/44 ms → 7.6/7.4 ms） |
| Q4 取消/关闭/重连/Notification 注销是否仍安全 | **PASS** | L1 8 + L2 C 组 5 + 组件 85 + 可靠性 38 全通过 |
| Q5 默认 32 线程在当前测试环境是否合理 | **PASS（L2 loopback）** | E 矩阵峰值活跃线程 ≤ 28；32 覆盖且无 64/128 的过度订阅；L3 未验证 |
| Q6 是否新增线程/Task/Handle/Session 泄漏 | **PASS** | 100 设备 cell 结束后 FD/线程/RSS 与基线同量级；C05/E-fault/38 项可靠性测试连接归零 |

## 2. 锁结构：调整前后

### 2.1 调整前（基线）

| 锁/门 | 粒度 | 职责与问题 |
|---|---|---|
| `Driver._lock`（asyncio） | 每设备 | **包住全部网络往返**：health、单点读写、Sum Read/Write 全分块循环、订阅注册。D02 135/209 次竞争等待，health p99 ×118.8 |
| `Session._cmd_lock`（threading） | 每连接 | pyads 同步命令串行 + 取消/关闭兜底 |
| `_REMOTE_ROUTE_LOCK` / `_LOCAL_AMS_LOCK` / `_lifecycle` | 进程/设备 | 远程路由互斥 / 本机 AMS 初始化 / connect-close 互斥（本轮不变） |

### 2.2 调整后

| 锁/门 | 粒度 | 职责 |
|---|---|---|
| `Driver._lock`（asyncio） | 每设备 | **只保护共享状态与订阅注册表的短临界区**：会话快照、点映射、Sum Read 编译（含缓存）、订阅登记/摘除。不再跨过任何 `await` 网络往返 |
| `Session._gate`（asyncio.Lock，新增） | 每连接 | asyncio 层命令排队点：取得资格后才 `to_thread`；Sum Read/Write 分块之间释放，health/控制写可在分块边界插入 |
| `Session._cmd_lock`（threading，保留） | 每连接 | 工作线程内串行 pyads 调用；调用方 Task 取消后在途线程仍受保护；`shutdown` 以 `acquire(timeout=grace)` join 到线程退出才关端口 |
| `_lifecycle` 等其余锁 | 不变 | 锁序：Driver `_lock` → Session `_gate`（仅 connect 路径）；IO 路径一律锁内取快照、锁外过门 |

每 Session 任一时刻至多一条同步命令在工作线程执行，由"门（资格）+
线程锁（取消窗口）"两级共同保证。

## 3. Session 命令执行与关闭时序

执行（正常）：

```
caller Task
  └─ execute(name): t0 排队计时
       async with _gate:                     # asyncio 层 FIFO 排队
         if _closing: 拒绝（计数 rejected）   # 关闭开始后新命令快速失败
         t1 执行计时
         await asyncio.to_thread(run, ...)
              └─ run: with _cmd_lock: pyads.<name>(...)   # 工作线程
         记录 wait=t1-t0 / exec
```

关闭：

```
aclose(grace):
  _closing = True                 # 门后所有新命令立即拒绝
  async with _gate:               # 等已过门的在途命令资格排空
    wait_for(to_thread(shutdown, grace), grace+2)
        └─ shutdown: _cmd_lock.acquire(timeout=grace)   # join 在途线程
                       connection.close()
  超时/获取失败 → shutdown_abandoned += 1，端口保持打开并 ERROR 日志
```

要点：

1. 排队发生在 asyncio 层，取消等待中的 Task 不产生线程、不上线；
2. 调用方取消**不**解除在途线程保护——`asyncio.shield` 单独做不到，
   线程锁 + 关闭 join 才能保证"无线程持连接时才 close"；
3. 关闭期间被拒绝的排队命令得到显式 `ProtocolError("session is
   closing...")`（C05 契约相应更新：未取消任务允许 CancelledError 或
   关闭拒绝，二者都是设计内收敛，不是假失败）；
4. 旧 Session 的迟到结果不可能写入新 Session：IO 在锁内只取快照，
   subscribe 注册返回后在锁内复核 `self._session is session`；
5. 订阅注册全程在 `_starting_subscriptions` 登记；取消后晚到成功的
   notification handle 由其后台清理接住并 DEL；driver.close 先在锁外
   幂等 deregister 再 `session.aclose`，与 handle.close 并发收敛到
   同一注销任务，服务端零注册残留（C04）。

## 4. Q3 性能数据（L2，受控服务器，loopback）

同机对比：基线提交 a4cba89f（worktree 内重跑 2 轮）与优化后 3 轮，
原始 JSON 见 `artifacts/ads-optimization/{d,e}_*`。受控服务器进程内
往返亚毫秒，D02 人工注入 2 ms/READWRITE 放大排队窗口。

### 4.1 D02 16 路 read_many(100) 夹 health（单分块场景）

| 版本（轮次） | health 无竞争 p99 | health 竞争 p95/p99 | driver 状态锁竞争次数 |
|---|---|---|---|
| 基线 1 | 0.25 ms | 39.7 / 39.9 ms | 135 |
| 基线 2 | 0.31 ms | 39.9 / 40.0 ms | 135 |
| 优化 1 | 0.29 ms | 39.4 / 39.6 ms | **0**（排队移至 gate，135） |
| 优化 2 | 0.20 ms | 39.5 / 47.1 ms | **0**（gate 135） |
| 优化 3 | 0.21 ms | 39.9 / 48.6 ms | **0**（gate 135） |

**health 尾延迟持平，未改善。** 机理：read_many(100) 是单条 Sum Read
单分块，门在分块之间没有释放点；同 Session 串行化是 A 组证明的硬约束
（无锁即 436× -1），排队点从状态锁移到命令门不改变队尾
≈ 队深 × 单命令耗时。这是测量给出的真实结论：**锁收窄对单分块命令
混合负载没有延迟收益，收益在结构解耦（注册表/关闭不再被网络往返挡住）
与多分块场景（4.2/4.3）**。本轮因此**不**引入优先级队列（任务书既定
"先测量、无收益不做"）。

### 4.2 D03 8 路千点读风暴（2 分块）夹 50 次控制写

| 版本（轮次） | 控制写 p50 | p95 | p99 | max |
|---|---|---|---|---|
| 基线 1 | 0.175 ms | 13.06 ms | 13.64 ms | 13.75 ms |
| 基线 2 | 0.140 ms | 13.81 ms | 14.45 ms | 14.56 ms |
| 优化 1 | 0.167 ms | 11.22 ms | 12.26 ms | 12.58 ms |
| 优化 2 | 0.158 ms | 10.53 ms | 11.02 ms | 11.12 ms |
| 优化 3 | 0.147 ms | 10.86 ms | 11.18 ms | 11.20 ms |

控制写 p95 −13%～−24%（中位 −18%），p99 13.6～14.5 → 11.0～12.3 ms
（约 −17%），max 同步收窄。2 分块只提供一个插入点，改善有界但三轮
稳定；chunk 越多插入机会越多（本轮不改 chunk 默认值）。

### 4.3 D04 分块持锁缩放（1000 点写，2 vs 10 分块）

| 版本 | 状态锁持有 2 chunk | 10 chunk | 命令门持有 2 / 10 chunk |
|---|---|---|---|
| 基线 1/2 | 26.0 / 26.1 ms | 41.4 / 44.4 ms（随 chunk ×1.7） | —（门不存在） |
| 优化 1–3 | **7.6 / 7.1 / 6.9 ms** | **7.4 / 7.2 / 7.0 ms（脱钩）** | 11.8/30.2、11.2/28.8、10.6/26.6（×2.5，随往返数线性） |

网络往返持有时间完整转移到命令门且随分块数线性；Driver 状态锁总持有
稳定在 ~7 ms 的纯内存开销，三轮 D04 红线（<50 ms 且不随 chunk 放大）
通过。

### 4.4 D01 无竞争基线

health p99 0.35 ms（基线 0.31）、read_many(1000) p99 14.6 ms
（基线 13.0）、write_one p99 0.33 ms；三把锁竞争均 0。无竞争路径无
回归（差异在 loopback 抖动内）。

### 4.5 E 组 10/50/100 设备 × 池 16/32/64/128（各 3 轮中位数）

| cell | health p99 基线中位（区间） | 优化中位（区间） |
|---|---|---|
| 10 设备 4 池 | 2.6–3.1 ms | 2.7–3.1 ms（持平） |
| 50 设备 4 池 | 9.6–12.2 ms | 11.0–12.7 ms（持平，区间重叠） |
| 100 设备 p16 | 31.1（26.9–57.7） | 19.8（19.0–24.6） |
| 100 设备 p32 | 25.2（22.8–27.7） | 45.7（39.6–48.7） |
| 100 设备 p64 | 22.8（18.0–23.4） | 20.9（18.8–26.0） |
| 100 设备 p128 | 49.4（43.3–65.4） | 22.9（21.6–24.3） |

10/50 设备持平；100 设备 cell 两轮代码都呈高方差，逐 cell 互有胜负，
**差异不具统计显著性，不作为收益声明**。机制上也符合预期：Driver 锁
是每设备的，fleet 聚合延迟的共享瓶颈是线程池容量而非设备内状态锁。
三轮共同稳定的结论：零失败、峰值活跃线程 ≤ 28、池 128 不带来收益。

资源（优化后 100 设备/p32，cell 结束时）：RSS 93.5 MB、FD 321、
线程 234，与基线（89–103 MB / 321 FD）同量级，关闭后回收。

## 5. Q4/Q6 安全与泄漏验证

- L1 确定性门控（`tests/component/core/protocol/ads/concurrency/`）：
  8 passed——取消在途读/写/health、关闭 join、双 close、跨 Session
  迟到结果隔离。
- L2 C 组 5 passed：C01 取消在途读后 health 排队不重叠（READ/READSTATE
  峰值各 1）；C02 取消写无重发（线上恰好 1 条 WRITE、内存为单次值）；
  C03 双 READSTATE 严格串行；C04 订阅注册取消后 close 服务端零残留；
  C05 24 任务半数取消 + close 有界收敛、连接归零。
- 订阅组件：关闭生命周期 11 场景（含调用方取消后后台清理继续、晚到
  handle 接住注销、注销失败记 unconfirmed）+ 投递/区块/队列丢弃语义。
- `test_ads_recovery_leaks.py` 38 passed：100 轮
  subscribe/handle.close、100 轮 connect/close、30 轮服务端重启与
  SIGKILL 循环后句柄/线程/连接不累积（SIGKILL 服务端残留的硬件归因
  仍为 L3 待验，见 §7）。
- E 组故障 cell：disconnect 代价仍为固定 ~5 s 原生超时（基线已知项，
  本轮不改），reject 12.7 ms、straggler 1.0 s 墙钟由掉队者支配——
  故障隔离正确，无 Task/连接泄漏。
- 新增观测：`_CommandStats`（门等待、同步执行、1861 超时、关闭后拒绝、
  shutdown 放弃计数），每会话关闭一行 INFO 汇总，含任一异常计数时
  WARNING；通知注销失败与队列丢弃 WARNING 带 `[host:port]` 设备标签。
  无点位等高基数标签，无新增 metrics 系统，无热路径 DEBUG。

## 6. 接口删除与线程池配置

### 6.1 已删除接口（无兼容垫片）

- `ProtocolPort.unsubscribe()` / `unlisten()`；
- ADS / Modbus / IEC104 Driver 对应实现（含 IEC104 专用 unlisten
  类型检查路径；句柄 `_ListenHandleImpl.close()` 与 listener 注册表
  保留）；
- 测试假件（new_collector/new_commander/registry 替身）中的同名桩；
- 相关测试改名（`test_unlisten_*` → `test_remove_*` /
  `test_listen_close_*`，可靠性循环
  `test_100_subscribe_handle_close_cycles`）。

采集侧注销统一为 `AcquisitionHandle.close()` / `SubscriptionHandle.close()`，
生产代码中无任何被删除接口的调用方。

### 6.2 runtime.thread_pool

- `system.yaml`：`runtime.thread_pool.max_workers`，正整数 1..256，
  默认 32；未配置段时保持"未配置"语义（配置保存无损往返），进程加载
  层回退 32；模板 `configs/system.yaml` 含注释与示例。
- Collector/Commander 入口在任何协议命令前各调用一次
  `configure_default_thread_pool()`（`loop.set_default_executor`，
  线程名 `wind-hub-cmd-*`）；Web 进程不执行协议同步命令，不安装。
- Driver 不创建/不销毁池；释放跟随 `asyncio.Runner` 对默认 executor
  的既有关闭。Collector 将字段列入 restart-required，Commander
  prepare 拒绝热变更。
- 15 个 schema/解析/往返单测；非法值（0/257/bool/字符串/浮点/未知键/
  标量替段）全部 ConfigError。

## 7. 测试环境、命令与结果

环境：WSL2 Linux、Python 3.11 wind-hub conda 环境、pyads 3.6.0、
受控 ADS 内存服务器（真实 AdsLib AMS/TCP 帧栈）。

```bash
python scripts/dev.py env                         # PASS
# L1 + 组件 + 契约 + 单元
python scripts/ci_gate.py fast --part backend-static                         # PASS
python scripts/ci_gate.py fast --part backend-tests --targets "unit,component,contract"  # PASS
# L2 并发/性能全量（约 145 s）
pytest -m "ads and real_service" tests/integration/protocols/ads/concurrency/
#   70 passed, 2 skipped（L3 无硬件）
# 可靠性
pytest tests/reliability/recovery/test_ads_recovery_leaks.py                 # 38 passed
# scope 相关 PR targets
python scripts/ci_gate.py pr --targets "integration-protocol,system-acquisition,system-command"
#   147 passed, 2 skipped
# 性能对比（原始 JSON 落 artifacts/，gitignored）
WIND_HUB_ADS_BASELINE_DIR=artifacts/ads-optimization/d_group_runN pytest ...test_d...
```

mypy（core/collector/commander 132 文件）0 错误；ruff 全绿；
import-linter 9 contracts kept。

## 8. 未验证的真实 PLC 场景（L3，NOT_RUN）

本轮未接真实 TwinCAT 硬件，以下结论仅由 loopback 真实帧栈支持，标记
NOT_RUN：

1. 真实 WAN/厂区网络 RTT（数 ms～数十 ms）下，分块间插入对控制写
   p95/p99 的改善幅度——RTT 越大队尾成本越高，插入收益预期更大，
   需 L3-01/02 实测；
2. 1861 客户端超时在真实 PLC 负载下的频次与恢复行为；
3. SIGKILL 后真实 TwinCAT 是否回收 notification 注册（testserver
   线性内存服务不回收，与硬件对应关系未建立）；
4. 32 线程默认在真实多设备站点（CPU 核数、AMS 路由拓扑）的适配性；
5. 真实 PLC 上同 NetID 多连接聚合（基线 F.2 第 4 条配置校验建议
   仍未实施，属独立任务）。

L3 入口（`test_l3_hardware_concurrency.py`）无环境变量时保持
SKIP/NOT_RUN，可在隔离受控窗口由人工运行。

## 9. 已知风险与剩余建议

1. **D02 类单分块混合负载无延迟收益**：若现场 health/watchdog 与
   大批量单分块读争用显著，优先级快通道仍是数据支持的候选项——本轮按
   任务书刻意不做，需要 L3 RTT 数据后单独立项。
2. **断连固定 ~5 s 尾部**：pyads 原生超时不变（基线已知）；快速探活/
   打断同连接等待队列是行为变更，需独立任务与硬件验收。
3. **shutdown 放弃**：在途命令超过 grace 时端口保持打开并 WARNING +
   `shutdown_abandoned` 计数（不做强制关闭、不谎称成功）；上层依赖
   进程退出来回收，现场应监控该日志。
4. **100 设备 cell 高方差**：性能回归判断应看多轮中位数而非单次
   p99；D/E 探针 JSON 已包含全部原始分布。
5. pyads 上游 -1/端口竞争缺陷候选仍建议以基线 A01 最小复现向上游
   报告；本轮代码注释已把"帧串扰"修正为已验证的本地资源竞争措辞。

# ADS 并发基线报告（受版本管理副本）

> 规范版本与全部原始 JSON/CSV 证据位于 `artifacts/ads-concurrency-baseline/`
> （`artifacts/` 按仓库约定被 gitignore，不入库）。本文件为同内容副本，
> 随测试代码一起进入版本管理，证据数值以 artifacts 下原始文件为准。


- 分支：`test/ads-concurrency-and-performance-baseline`
- 生成日期：2026-10-17
- 范围：仅新增/修改**测试代码、测试工具与测试报告**；`src/` 生产代码零改动（见 F.6 校验）。
- 原始证据：本目录下 `a_matrix/`（38 cell JSON + CSV/聚合）、`d_group/`、`e_scale/`、`l3_hardware/`（硬件运行后生成）。
- 说明：任务书指定的 `报告模板.md` 在仓库中不存在（全仓 `find` 无结果），本报告沿用 `artifacts/soak/soak_report_smoke_1h.md` 的既有住宅风格（摘要先行、环境块、原始 JSON 佐证、PASS/FAIL/INCONCLUSIVE/NOT_RUN 结论）。

---

## 摘要：三个首要问题的结论

### Q1：pyads 3.6.0 共享 Connection 的多线程并发 ADS 命令问题，是否复现？—— **复现（PASS）**

在 Linux AdsLib 真实协议栈（L2，pyads testserver 帧栈，非 mock）上，14 个无锁 cell 共观测到 **436 次 `ADSError(-1)`**，而全部 19 个加锁/顺序无故障 cell **零失败**。典型 cell A01（8 线程 × 3 次读共享一条连接）：24 次尝试只有 **3 次到达网线**，其余 21 次在客户端直接失败，子进程 stderr 伴随 AdsLib C 层告警 `Port: 30001 already in use`。

**关键定性（有帧证据支持的部分）**：全部 38 个 cell 的值域错配（mismatch）= **0**，通知残留 = **0**，服务端按命令统计的在途重叠峰值恒为 **1**，invoke-id 取证无错配。因此本基线**不得**把 -1 归因为「AMS 请求/响应帧串扰」——现象是 **AdsLib 客户端本地端口/请求槽竞争导致的客户端侧拒绝与饥饿**。是否存在帧串扰，本基线结论为 **未证实（INCONCLUSIVE）**：竞争方根本未上线，无从产生错帧。

### Q2：锁瓶颈是否被定量测到？—— **测到，但在 loopback 上量级温和；成本随 RTT × 排队深度线性放大**

- 无竞争基线（D01）：驱动 `asyncio _lock` 701 次获取、`_cmd_lock` 730 次获取，**竞争次数均为 0**；health p99 = 0.31 ms，read_many(1000) p99 = 13.0 ms。
- 人工 2 ms/命令负载（D02）：135/209 次 `_lock` 获取等待，health p99 从 0.34 ms 膨胀到 **40.3 ms（×118.8）**；`_cmd_lock` 竞争仍为 **0**，服务端峰值并发仍为 1。
- 千点读风暴夹 50 次控制写（D03）：控制写 p50 0.13 ms、p99 **13.9 ms**（排队等待，非执行变慢）。
- 分块持锁（D04）：1000 点写在 2 分块下持 `_lock` p50 2.0 ms，10 分块下 3.6 ms（**×1.79**），持锁时间随往返次数单调增长。

即：稳态排队全部发生在 **asyncio `_lock`**；线程层 `_cmd_lock` 在生产路径上测不到竞争，但它是取消/关闭语义的必要兜底（见 C 组）。绝对延迟是 loopback 值；真实 PLC 的 RTT 会以「排队命令数 × RTT」放大队尾延迟——此项须硬件 L3 才能定量，当前 **NOT_RUN**。

### Q3：下一步最高性价比的优化是什么？—— **不是删锁、也不是立刻重写异步调度；按收益排序：**

1. **控制命令优先/快通道（或 health 合并）**：D03 证明控制写会排在千点读风暴后面吃 14 ms 队尾；E 组 straggler 证明单台 1 s 慢设备支配整个 fleet 墙钟（无优先级路径）。让单点写/health 绕开大批分块读队列，是数据直接支持的最高收益项。
2. **fleet 线程池容量按 ~28–32 定型，不要放到 128**：客户端同步栈实测并行天花板 ≈ 26–28（池 64/128 的峰值线程不再增长），而 100 设备/池 128 时 health p99 反劣化到 43 ms（池 16 为 26 ms）——过度订阅只增调度抖动。
3. **调大 Sum 分块以减少往返链长度**（D04：分块越少持锁越短），在 PLC 单帧上限内取平衡。
4. **异步命令调度（pyads-async 类改造）暂缓**：单设备受 AdsLib 本地端口串行性约束，峰值并行必然为 1，换异步库不能提升单设备并发；它只可能改善多设备 fleet 的线程开销，而当前 fleet 墙钟在 loopback 上 ≤0.13 s、RSS ≤103 MB，没有数据驱动的紧迫性。建议 L3 硬件拿到 RTT 排队数据后再决策。

---

## A. 环境、方法与证据等级

### A.1 环境指纹

| 项 | 值 |
|---|---|
| 操作系统 | Linux 6.6.87.2-microsoft-standard-WSL2（WSL2） |
| Python | 3.11.16（conda env `wind-hub`） |
| pyads | 3.6.0（ctypes 封装） |
| AdsLib.so | sha256 `0b787b4f9058a217a648052b7b2ce9cc6f2c2e199c906402c9ee350c00775c9f`，444296 字节 |
| CPU | 20 逻辑核；默认 executor 容量 min(32, cpu+4)=24 |
| 对端 | L2 全部使用真实 AMS/TCP 帧栈：pyads testserver 经测试支持改造（见 B.0），loopback |
| 时间确定性 | 故障窗口用服务端到达计数/屏障确定，无随机 sleep；无随机种子需求 |

### A.2 三层证据

- **L1（确定性 Python）**：`tests/component/core/protocol/ads/concurrency/test_cancel_thread_lifecycle.py`，8 测试，门控假连接强制精确时序。
- **L2（真实 pyads/AdsLib over TCP）**：A/B/C/D/E 五组共 70 个测试/实验 cell，全部在本会话通过。
- **L3（真实 Beckhoff PLC）**：`test_l3_hardware_concurrency.py` 提供可直接运行的入口；无硬件环境变量时 SKIP，本报告记 **NOT_RUN**（运行方法见 E.4）。

### A.3 隔离与安全

- 所有无锁/破坏性实验在**隔离子进程**中运行（`tests/support/ads_concurrency_child.py`），硬超时 + 退出码校验，崩溃/挂起计入 cell 结果（本矩阵 `crashed=0, hung=0`）。
- 生产代码未做任何为测试让路的修改；测试侧仅在测试进程内替换锁/executor（探针），或改造**测试服务器**。
- L3 明确不对真实 PLC 运行无锁 A 组实验（防止污染设备状态）；硬件只验收生产驱动的串行化路径。

---

## B. A 组：pyads 共享连接并发矩阵（L2，38 cell）

文件：`tests/integration/protocols/ads/concurrency/test_a_pyads_connection_concurrency.py`
汇总：`a_matrix/a_matrix_summary.csv`、`a_matrix_aggregate.json`

### B.0 测试支持改造（非生产代码）

- `tests/support/ads_memory_server.py`：上游 testserver 单次 `recvfrom(4096)` 截断导致 >4 KB Sum 读写帧损坏（1000 点场景出现 struct.error/1861）。新增按帧长收全 + pipeline leftover + `sendall` 的连接循环；小帧行为与上游一致。改造后既有 112 个 ADS 测试全通过。
- `tests/support/ads_controlled_server.py`：按命令注入延迟/拒绝/屏障，记录每条请求 invoke-id、在途重叠、到达时刻（延迟规则应用**之前**记入口）。

### B.1 无锁 vs 加锁 vs 顺序（无故障 cell）

| 场景 | 内容 | 无锁结果 | 加锁/顺序 |
|---|---|---|---|
| A01 | 8 线程共享连接只读 | 21/24 失败（全 -1），仅 3 请求上线 | 24/24 成功，服务端峰值 1 |
| A02 | 读写混合 | 21/24（-1），3 请求上线 | 0 失败 |
| A03 | 单点读基线 | 21/24（-1） | 0 失败 |
| A04 | Sum ReadWrite | 21/24（-1） | 0 失败 |
| A05 | Sum ReadWrite 大负载 | 21/24（-1） | 0 失败 |
| A06 | 订阅注册/注销 ×12 | 10/12（-1），仅 2 请求上线 | 0 失败，18 请求全上线 |
| A07 | health 与订阅交错 | 5/12（-1），注册仍有 12 组上线 | 0 失败 |
| A08 | 240 次 IO + 4 订阅 + 通知流 | **211/240（-1）**，仅 37 请求上线，4 个订阅只有部分建立，132 次回调 | 0 失败；248 请求全上线；加锁 512 / 顺序 516 次回调 |
| A09 | READ/WRITE/READWRITE/READSTATE 全混合 | 20/24（-1），仅 4 请求上线 | 0 失败 |
| A10 | 6 条「独立」连接各 8 次 | 1×1861，p50 **120.8 ms** | 0 失败，p50 20.2 ms，48 请求全上线 |

聚合（`a_matrix_aggregate.json`）：

```text
unlocked ADSError(-1) 总数 = 436
locked/sequential 无故障 cell 失败数 = 0
全矩阵 mismatch（值域错配）= 0
全矩阵 residual_notifications = 0
每个 cell 服务端按命令 peak_active = 1（A03-reject 为 0：请求被对端规则拒绝）
```

### B.2 故障 cell

| Cell | 模式 | 观测 |
|---|---|---|
| A03 reject | locked | 24×1808（帧内符号错误），0×-1，无错配 |
| A03 reject | unlocked | 21×-1 + 3×1808（竞争方先被客户端拒绝） |
| A03 timeout（800 ms 不响应） | locked | 24×**1861**（同步超时），**0×-1**，墙钟 19.37 s（串行排队） |
| A03 timeout | unlocked | 21×-1 + 3×1861，墙钟 2.40 s（大部分请求根本没上线） |
| A03/A05 disconnect | 两模式 | 首命令 -1/1861 有界出现，后续无放大，无挂起 |
| A05 timeout | locked | 24×1861，0×-1，19.31 s |

### B.3 定性结论

1. **共享 Connection 并发不安全，且失败模式是客户端侧拒绝/饥饿**：C 层本地 AMS 端口（30001）与请求槽在多线程同时进入时冲突，输家拿到 `ADSError(-1)`，多数命令不上线。这解释了无锁 cell 墙钟极短（0.002–0.3 s）却大量失败——不是超时，是即时拒绝。
2. **没有观测到帧串扰**：0 值域错配、invoke-id 全部一一对应、服务端重叠峰值恒 1。「无帧串扰」仅能断言到「本矩阵无证据」；竞争命令未上线使得串扰在物理上不可能被这组实验触发，故记 **INCONCLUSIVE 而非「已证明线程安全/绝不串帧」**。
3. **加锁把故障语义变正确**：超时时锁模式只产生 1861（真实的同步等待超时），不产生 -1；代价是墙钟随队列线性增长（24 × 0.8 s）。
4. **A10 证伪「多连接即独立并行」**：同一 NetID/主机的多条 pyads 连接在 AdsLib 路由层被聚合复用，无锁时相互拖慢（p50 120 ms vs 20 ms）并出现 1861。多设备部署必须**每设备唯一 AMS Net ID**（E 组据此设计）。

### B.4 探索性极端节拍（不计入正式矩阵）

开发期曾在极激进通知节拍下目击一次原生层崩溃。本次以隔离子进程复跑（8 订阅/2 ms 节拍，`a_matrix/exploratory/A08_unlocked_extreme_8notes_2ms.json`）：**退出码 0，未崩溃**；209×-1、4/8 订阅建立、44 次回调、0 错配、stderr 仅 `connection closed by remote`。结论 **INCONCLUSIVE**：崩溃未稳定复现，留作遗留项（F.5），且它只在无锁路径出现——生产串行路径不可达。

---

## C. B/C 组：驱动公共 API 正确性与取消×线程生命周期

### C.1 B 组（L2，8 测试，全通过）

`test_b_driver_concurrency_real.py`：read_many×2 与 write_one 值域完整；25 点 + `max_subs_per_sum=8` 的分块交错；订阅与 health/read/write 并发；注销对抗 IO 风暴；read 循环中 close 有界；并发双 close 幂等；health 中重连；点表热切换（4↔8 点）共享点始终 GOOD。生产公共 API 在并发调用下无错值、无假失败。

### C.2 C 组 L1（8 测试）+ L2（5 测试，全通过）

L2 `test_c_cancel_lifecycle_real.py` 用服务端**到达计数**精确定位「命令已在途」：

| 测试 | 取证结果 |
|---|---|
| C01 取消在途读 | 线上恰好 1 条 READ；50 ms 后发起的 health 被 `_lock` 挡住（task 未完成），服务端 READ/READSTATE 峰值各 1；取消后读返回值正确（1.5 GOOD） |
| C02 取消在途写 | 线上恰好 1 条 WRITE，无重发；服务端内存恰为单次写入值 9.25；后继写到达计数变 2 |
| C03 取消 health 后立即再发 | 0.1 s 时到达数仍为 1，前命令结束后才变 2；峰值 1 |
| C04 订阅注册在途取消后 close | close 等在途 ADDDEVICENOTE 结束并注销迟到 handle；服务端零注册残留、通知计数 0 |
| C05 24 任务半数取消 + close | 仅允许 CancelledError；各命令峰值 ≤1；服务端活跃连接归零，有界收敛 |

L1 门控测试额外证明：排队者取消不杀在途命令；close 等待持有 `_cmd_lock` 的工作线程（不提前关连接）；shutdown 抢锁超时放弃 close 并返回 False（端口保持打开，有日志）；旧会话迟到结果不可达新会话；取消风暴后无线程残留在驱动调用内（executor 空闲线程被 CPython 保留属运行时行为，非泄漏）。

**回答任务书 Q（取消重叠）**：`asyncio.to_thread` 的 Task 被取消后，工作线程确实仍会跑完同步命令，但 `_lock` + `_cmd_lock` 保证（a）同一连接上不会有第二条命令重叠上线，（b）close 不会 use-after-close，（c）无自动重发。

---

## D. D 组：锁竞争定量分解（L2，4 测试，全通过）

文件：`test_d_lock_contention.py`；探针：`tests/support/ads_profiling.py`（按次记录 wait/held 的 threading/asyncio 锁替身、`ThreadPoolExecutor` 子类的排队/服务计时——3.11 要求 set_default_executor 必须给真实实例）。

### D.1 每把锁的职责与实测

| 锁 | 层级/作用域 | 保护内容 | 是否在稳态命令路径 | 实测 |
|---|---|---|---|---|
| `_LOCAL_AMS_LOCK` | threading.RLock，进程全局 | `open_port`/`set_local_address` 一次性本地 AMS 身份 | 否（仅 initialize/close_local） | 无稳态成本；重复初始化做身份一致性校验 |
| `_REMOTE_ROUTE_LOCK` | threading.Lock，进程全局 | connect 时添加远端路由（AdsLib 路由按 NetID 聚合） | 否（仅连接期） | 100 设备并发 connect 墙钟 ≤0.06 s |
| `_lifecycle` | asyncio.Lock，每 driver | connect/close 互斥，保证旧会话完全退出才建新会话 | 否 | 取消/重连测试中有界、无死锁 |
| `_lock` | asyncio.Lock，每 driver | **所有网络 I/O，含 Sum 分块循环全程持锁** | 是，唯一稳态排队点 | D02：135/209 竞争等待；D03：44/91 |
| `_cmd_lock` | threading.Lock，每 session | 单条 pyads 同步命令；close 抢它实现有界关停 | 是，但外层已串行 | 四场景 730/208/130 次获取**竞争恒 0**，max_wait <5 µs |

### D.2 场景数据

- **D01 无竞争基线**：health n=300 p50 0.14 ms / p99 0.31 ms；read_many(1000) p50 1.63 / p99 13.0 ms；write_many(1000) p50 2.28 ms。两把锁竞争均 0，executor 峰值线程 = **1**。
- **D02 health vs 批量读（人工 READWRITE 延迟 2 ms）**：health 基线 p99 0.34 ms → 竞争 p99 **40.26 ms（×118.8）**；read_many(100) p99 40.58 ms；`_cmd_lock` 竞争 0；服务端峰值 1。每个排队者恰好多等一个「命令 RTT」，队尾 ≈ 队列深度 × 单命令耗时。
- **D03 控制写 vs 8 路千点读风暴**：风暴读 p50 13.35 ms；控制写 p50 **0.13 ms**（多数时候抢在前）、p99 **13.94 ms**、max 14.1 ms；44 次锁等待；无失败。
- **D04 分块持鳞**：1000 点写，2 分块持 `_lock` p50 2.00 ms / max 2.34 ms，10 分块 p50 3.58 ms / max 4.24 ms（**比值 1.79**，单调；原 2× 假设修正为实测比值）；端到端 p99 分别 13.1 ms 与 4.5 ms（10 分块单次负载小，p99 反而低，但持锁链更长——两个口径都在 JSON 中）。

---

## E. E 组：10/50/100 设备规模与 L3 入口

文件：`test_e_multi_device_scale.py`（15 cell，slow，全通过）；每设备独立服务器、独立连接、独立 `_cmd_lock`、**唯一 NetID（IP 派生 `.1.1`）**；探针 executor 在服务器引导之后安装，避免污染。

### E.1 12 cell 健康矩阵（p50/p99，毫秒；loopback）

| cell | 峰值活跃线程 | 排队 p99 ms | health p50/p99 | read_many(5) p50/p99 | 控制写 p50/p99 | RSS 末值 MB | FD | 线程数 |
|---|---|---|---|---|---|---|---|---|
| 10/p16 | 8 | 1.17 | 1.75/3.60 | 3.37/4.16 | 1.82/2.59 | 65 | 51 | 33 |
| 10/p32 | 7 | 0.94 | 1.49/3.52 | 2.91/4.33 | 1.24/2.44 | 66 | 51 | 33 |
| 10/p64 | 7 | 0.94 | 1.80/2.32 | 3.29/3.93 | 1.82/2.23 | 66 | 51 | 32 |
| 10/p128 | 8 | 1.32 | 1.60/3.34 | 3.44/4.40 | 1.47/2.68 | 67 | 51 | 34 |
| 50/p16 | 16 | 27.38 | 8.03/10.83 | 12.89/16.80 | 6.77/6.90 | 73 | 171 | 118 |
| 50/p32 | 18 | 15.04 | 5.65/10.63 | 12.38/15.06 | 4.94/9.46 | 75 | 171 | 134 |
| 50/p64 | 18 | 5.25 | 6.64/7.87 | 13.77/16.79 | 7.33/9.67 | 78 | 171 | 152 |
| 50/p128 | 17 | 4.96 | 6.80/8.03 | 14.05/16.68 | 6.62/6.92 | 81 | 171 | 152 |
| 100/p16 | 16 | 23.49 | 11.69/26.07 | 27.34/30.97 | 18.32/18.92 | 89 | 321 | 218 |
| 100/p32 | **28** | 12.67 | 10.95/26.68 | 27.49/32.28 | 13.38/17.75 | 93 | 321 | 234 |
| 100/p64 | 26 | 10.37 | 13.35/21.67 | 27.47/31.81 | 11.78/16.57 | 98 | 321 | 266 |
| 100/p128 | 28 | 30.60 | 12.01/**43.12** | 29.93/**60.05** | 12.87/13.24 | 103 | 321 | 292 |

阶段墙钟（connect / 3 轮 steady / 控制轮）：10 设备 ≤0.01 s；50 设备 ≤0.05 s；100 设备 ≤0.13 s。零命令失败。

要点：

1. **同步栈并行天花板 ≈ 26–28**：池从 32 加到 128，100 设备峰值活跃线程停在 28；池 16 则被硬限制在 16。瓶颈在 AdsLib/本机 AMS 端口的原生处理并行度，不在 Python 池。
2. **池 128 过度订阅有反效果**：100 设备 health p99 43 ms、read p99 60 ms，劣于池 16/64；RSS 与线程数随池容量线性增长（每设备 FD ≈ 3.2，100 设备 321 个）。
3. 规模到 100 设备：RSS 仅 89–103 MB，loopback 命令 p99 ≤60 ms，无线性失败。

### E.2 故障注入（50 设备/池 32）

| 故障 | 注入方式 | fleet 读墙钟 | 隔离结果 |
|---|---|---|---|
| reject | 清空受害设备内存，Sum Read 帧内逐点 1808 | **0.010 s** | 受害设备全 BAD 即时返回；其余 49 台全 GOOD |
| disconnect | 读进行中 `_stop_tcp()` 杀受害 TCP | **5.002 s** | 仅受害设备付出完整客户端超时（≈5 s），其余 GOOD；失败有界 |
| straggler | 受害设备 READWRITE 注入 1 s 延迟 | **1.002 s** | 受害设备慢但 GOOD；fleet 墙钟被单台慢设备支配（无快路径） |

### E.3 多设备结论

- 故障隔离正确：错误不跨连接/跨设备传播；reject 走快速 BAD 路径（10 ms 级）。
- 两个真实尾部来源被定量：**TCP 死亡检测 = 固定 ~5 s 客户端超时**；**掉队者支配 gather 墙钟**。二者都指向 Q3 的「快通道/优先级」方向。
- 多设备扩展的合理池容量区间为 **28–32**；再大无收益且有抖动反效果。

### E.4 L3 真实硬件入口（NOT_RUN）

`tests/integration/protocols/ads/concurrency/test_l3_hardware_concurrency.py`，未设环境变量时 2 测试 SKIP：

```bash
WIND_HUB_TEST_ADS_HOST=192.168.x.y \
WIND_HUB_TEST_ADS_NET_ID=192.168.x.y.1.1 \
WIND_HUB_TEST_ADS_PORT=851 \
WIND_HUB_TEST_ADS_READ_SYMBOL=MAIN.readback \
WIND_HUB_TEST_ADS_WRITE_SYMBOL=MAIN.setpoint \
pytest -m hardware tests/integration/protocols/ads/concurrency/test_l3_hardware_concurrency.py
```

L3-01：100 轮并发 health + 符号读，p50/p99/max 落 `l3_hardware/`，health p99 上界 1 s（只卡数量级）。L3-02：配置写符号时 50 轮幂等 0.0 写 + health（+读）。无锁 A 组实验严禁对真实 PLC 自动运行；如需硬件复核无锁行为，须在隔离网段、受控停机窗口由人工按 A 组子进程流程执行。

---

## F. 结论、生产侧观察（仅建议，本轮不修）与遗留项

### F.1 逐条问题结论

| 问题 | 结论 |
|---|---|
| pyads 3.6.0 共享 Connection 能否多线程并发命令 | **不能（已复现）**：436×-1，命令在客户端侧被拒绝/饥饿；加锁/顺序零失败 |
| 是否发生 AMS 请求/响应错配、-1、假健康失败 | -1 大量复现；**错配 0 起、假健康失败 0 起**；无证据归因帧串扰（INCONCLUSIVE，竞争命令未上线） |
| `_cmd_lock` 是否必要 | **必要**：稳态零竞争，但它是取消后线程仍在途、close 有界等待、防 use-after-close 的唯一线程层兜底（L1+C 组取证） |
| 驱动 `_lock` 是否造成可测等待/控制延迟 | **是**：人工 2 ms 负载下 health p99 ×118.8；千点风暴下控制写 p99 13.9 ms；成本 = 排队深度 × 单命令 RTT |
| to_thread 取消后是否重叠同步命令 | 工作线程会跑完，但双锁保证线上不重叠、无重发、关闭安全（C01–C05） |
| close/reconnect/通知注销竞态 | 均有界：close 等在途命令、幂等双 close、迟到 handle 被注销、服务端零残留 |
| 10/50/100 设备行为 | 零失败；RSS ≤103 MB；并行天花板 ≈28；池 128 反劣化；故障隔离正确，断连代价 5 s、掉队者支配墙钟 |
| 是否值得引入异步命令调度 | **暂缓**：单设备并行被 AdsLib 钉死为 1，换异步库无单设备收益；fleet 现状成本低；待 L3 的 RTT 排队数据再决策 |

### F.2 生产侧观察与修复建议（本轮严格未改 `src/`）

1. **pyads 3.6.0 上游缺陷候选**：共享 Connection 并发进入触发本地端口/请求槽竞争（-1 风暴 + `Port: 30001 already in use`）。当前驱动以 `_lock`/`_cmd_lock` 全串行化正确规避；建议向上游 pyads/AdsLib 提交最小复现（A01 子进程可直接作为附件）。
2. **断连尾部固定 ~5 s**：TCP 已 RST，排队/新发命令仍等满原生超时。建议后续评估「传输错误后以 read_state 做廉价快速探活、失败快速标记会话死亡并打断同连接等待队列」——属生产行为变更，需独立任务与硬件验收。
3. **控制通道无优先级**：大批分块读/慢设备会拖住控制写与 health（D03/E-straggler）。建议为单点开路控制设独立快通道或队列插队，并考虑 health 合并。
4. **NetID 唯一性约束值得在配置校验中显式检查**：A10 证明同 NetID 多连接被 AdsLib 聚合（非独立、互拖延迟）。E 组依赖了每设备唯一 NetID；多设备配置若出现重复 NetID，当前仅表现为连接层诡异现象，建议配置期直接拒绝。

### F.3 结论总表

| 组 | 结论 |
|---|---|
| A 矩阵 38 cell | PASS（问题复现；无错配；锁/顺序全绿） |
| B 驱动 API 8 测试 | PASS |
| C 取消生命周期 L1 8 + L2 5 | PASS |
| D 锁竞争 4 测试 | PASS（取得定量基线） |
| E 规模矩阵 15 cell | PASS |
| L3 真实 TwinCAT | **NOT_RUN**（入口可运行） |
| 极端通知节拍崩溃 | **INCONCLUSIVE**（本次隔离子进程未复现；仅无锁路径可及） |

### F.4 复现入口

```bash
# L1
pytest tests/component/core/protocol/ads/concurrency/
# L2（A 矩阵约 95 s；C/E 带 slow）
pytest -m "ads and real_service" tests/integration/protocols/ads/concurrency/
# 原始证据目录
ls artifacts/ads-concurrency-baseline/{a_matrix,d_group,e_scale}/
```

### F.5 遗留与未覆盖

- L3 真实 TwinCAT（TC2 801 / TC3 851）未执行；所有绝对延迟为 loopback 值，跨真实网络的 RTT 放大未测。
- 原生层崩溃仅开发期目击一次，隔离复现未成功；建议 CI 之外保留该探索 worker 供长稳/硬件环境复跑。
- 仅验证 Linux AdsLib；Windows/macOS 原生栈行为未覆盖。
- 帧串扰在当前模型下不可达（竞争命令不上线）；若要严格证伪，需要能强制多命令同时上线的故障桩或真实硬件上的人工受控实验。

### F.6 零生产改动校验

```bash
git diff origin/main --stat -- src/   # 期望：空
git diff origin/main --name-only      # 仅 tests/ 与本报告
```

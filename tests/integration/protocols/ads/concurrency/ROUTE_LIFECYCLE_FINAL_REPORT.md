# ADS 路由生命周期并发安全最终收尾验收报告

分支 `test/ads-concurrency-and-performance-baseline`，接续物理关闭线程所有权
整改（d08ea21b/d67b7e14），闭环两项 P1 候选窗口：物理关闭登记过晚、
路由检查与 `Connection.open()` 的 TOCTOU。

## 0. Git 与范围

| commit | 内容 |
| --- | --- |
| 5bc63147 | fix(ads)：路由级生命周期协调（_RouteState/闸门/准入拒绝/闩锁） |
| 2f585085 | test(ads)：T1–T6 确定性测试 13 项 + ownership 测试适配 |
| 8ae60658 | test(ads)：AdsMemoryServer 多实例通知隔离 + 独立周期用例改用独立 Net ID/服务器 |

未建 PR、未合并 main、普通 push。命令锁架构（Driver `_lock`、Session
`_gate`/`_cmd_lock`、owner-task、分块调度、订阅、线程池、UNKNOWN 不重发）
未改动；无新 Worker/看门狗/轮询。

## 1. pyads 3.6.0 / Linux AdsLib 语义（源码核实，非注释推断）

- `Connection.open()`：`adsPortOpenEx()`（连接私有本地 AMS 端口）→ Linux
  上 `adsAddRoute(目标 Net ID, ip)`，失败回滚端口。
- `Connection.close()`：**先 `adsDelRoute(netIdStruct())`，再
  `adsPortCloseEx(私有端口)`**。
- AdsLib 目标路由按 Net ID 进程级共享、无引用计数：同 Net ID 的多个活动
  Connection 中任一 close 会无条件删除其他连接仍在依赖的路由。
- `pyads.open_port()/close_port()` 操作模块全局本地 AMS 端口；
  `add_route_to_plc()` 经 UDP 48899 改的是 PLC 远端路由表，与本地 AdsLib
  路由独立（继续由既有 `_REMOTE_ROUTE_LOCK` 保护）。

结论：同 Net ID 多活动会话在本进程内**天然不安全**，按任务书 §5 在准入期
显式拒绝，而不是开发引用计数锁协议。

## 2. 协调机制（driver.py）

- `_RouteState`（按路由键，惰性创建）：
  - `op_lock`（asyncio.Lock）：串行化同一路由的「建链段」与「物理关闭段」。
    close 路径跨异步预算持锁，物理关闭线程经 `_shutdown_settled` future
    真实收敛后在同一 loop 回调里摘除登记并释放——等待队列即 asyncio 锁
    FIFO 本身，无独立等待表。
  - `active: set[ADSDriver]`：open 成功即登记，保留到物理关闭收敛
    （覆盖「摘除 → adsDelRoute」整段窗口）。
  - `closing: set[_AdsSession]`：**先于底层 `connection.close()` 启动登记**。
- `_LoopRouteRegistry`：路由表、进程闸门 `barrier`、拆除闩锁
  `local_closed`，按事件循环弱引用持有（生产单循环；3.11 contended
  asyncio.Lock 校验循环归属，测试每用例独立循环必须隔离）。
- `_ams_barrier()`：原子化「闩锁检查 → 占用准入检查 → `to_thread(open)`
  → active 登记」；close_local 的「占用快照 → 拆端口」在同一把极短闸门内，
  闸门不包裹物理 close 线程（避免单设备故障全局串行）。
- 锁序恒为「路由 op_lock → Driver `_lock` → Session 门」，建链与关闭同序，
  无反向获取。建链失败的全新 Session 由 `shutdown_inline()` 在持锁同 task
  内当场收敛（不另起 owner 申请同一把锁）。

### 关键时序

物理关闭（含异步预算耗尽）：

```text
_close_all: op_lock.acquire
  → Driver _lock 摘会话（active 保留本 Driver）
  → barrier 内 closing.add(session)        # 先于 close 启动
  → 订阅 deactivate/stop/deregister
  → session.aclose(grace) → owner: to_thread(shutdown)
        shutdown: _cmd_lock → connection.close()（adsDelRoute → 关私有端口）
  → settled future done 回调：active.discard / closing.discard / op_lock.release
同路由建链者在 op_lock 上 FIFO 等待；其 adsAddRoute 不可能与迟到 adsDelRoute 交错。
```

准入：

```text
connect: op_lock.acquire（有界 6s，超时 ProtocolError「has not converged」）
  → Driver _lock
  → barrier：local_closed? → reject
  → active 内有他人? → ConfigError（重试循环之外，不重试）
  → 每次 attempt：barrier 内 to_thread(connection.open) + is_open + active.add(self)
```

## 3. I1–I7 对照

| 不变量 | 结论 | 主要证据 |
| --- | --- | --- |
| I1 同路由建链不与旧 adsDelRoute 交错 | 满足 | op_lock 跨预算持有到物理收敛；T1/T2 顺序断言 `close-end < open-start` |
| I2 登记先于物理 close 启动 | 满足 | closing 在 `_shutdown_session` 前、barrier 内登记；T1 启动瞬间断言 |
| I3 物理结束才解除阻塞 | 满足 | 锁释放在 settled 回调；T1/T6（取消等待者后所有权不变） |
| I4 异步超时 ≠ 物理完成 | 保持 | owner-task + settled future（上轮机制未动）；ownership T1/T3/T4 |
| I5 取消不转移所有权 | 满足 | T6 等待者取消后 close 方仍持锁/登记，迟到收敛后下一等待者成功 |
| I6 不同路由不被全局串行 | 满足 | 每键独立 `_RouteState`；闸门只裹登记瞬间；T5（慢关闭不阻塞他设备建链/读/健康 <1.5s） |
| I7 不靠关进程端口强行恢复 | 满足 | close_local 有占用即拒绝；建链等待超时只报错不强开；无杀线程/第二连接 |

## 4. Q1–Q7 结论

| 问题 | 结论 | 证据等级 |
| --- | --- | --- |
| Q1 物理 close 执行前是否已受路由级保护 | **是**。op_lock 在摘会话前取得，closing 先于底层 close 登记，active 覆盖到收敛 | 确定性 T1（Event 门控） |
| Q2 同 Net ID 多 Driver 是否安全/拒绝 | **不安全即拒绝**：第二活动 Driver 在准入期 ConfigError，不创建连接；顺序复用与同 Driver 重连正常；连接等待有界 | 确定性 T2/T3/T6；真实栈 C/A 组多设备均按设备分配独立 Net ID（E 组 `f"{ip}.1.1"`） |
| Q3 检查与 open 间是否仍有 TOCTOU | **无**。检查与 open+登记在 op_lock+barrier 内，持锁期他人物理关闭无法启动，占用状态在「检查→open」间不可变 | 确定性 T2（close/connect 并发两种先后手） |
| Q4 close_local 对活动/建链中/关闭中会话是否安全 | **是**。闸门内原子「占用快照（active∪closing）→拒绝/拆除」，拒绝点名路由；拆除后闩锁，建链快速失败，initialize_local 复位 | 确定性 T4 四用例 |
| Q5 迟到成败时登记/资源是否一致 | **一致**。成败都收敛 settled，回调一次性摘除 active/closing 并释放锁；重复 close 只发一次物理关闭 | 确定性 T6 + ownership T1 成败两变体（13+7 全过） |
| Q6 不同 Net ID 设备能否独立工作 | **能**。独立 `_RouteState`，闸门不裹物理段；A 慢关闭期间 B 建链+读+健康 <1.5s | 确定性 T5；真实栈 E 组多设备、fleet E2E 15 passed |
| Q7 是否引入死锁/无限等待/采集性能退化 | **未发现**。锁序单一无环；所有等待有界（路由 6s、管线 grace+2s）；稳态读写路径不获取任何新锁（op_lock/barrier 仅在建链/关闭瞬间）；D02 红线保持 | D/E 组（18 项）；静态门禁；下文 1 项 UNKNOWN 机敏 |

## 5. 测试证据（三层）

### 5.1 确定性测试（threading.Event/Barrier，无时间预算参与判定）

```bash
python -m pytest tests/component/core/protocol/ads/test_route_lifecycle.py -q
# 13 passed（T1–T6）
python -m pytest tests/component/core/protocol/ads/test_close_thread_ownership.py -q
# 7 passed
# 两文件连跑 6 轮：新文件 0 失败
```

### 5.2 真实 Linux AdsLib/AAMS-TCP 协议栈（AdsMemoryServer，real_service）

```bash
# A/B/C 组：54 passed（100.4s）
python -m pytest tests/integration/protocols/ads/concurrency/test_{a,b,c}*.py -q
# D/E 组：19 passed（含 D02 driver_lock contended_count==0 等性能红线）
# 全量 ADS 集成（除 L3）：92 passed
python -m pytest tests/integration/protocols/ads/ --ignore=*/test_l3_hardware_concurrency.py -q
# 可靠性/恢复（真实栈重连、SIGKILL 句柄残留、shutdown races）：57 passed
python -m pytest tests/reliability/core/test_ads_reliability.py \
  tests/reliability/recovery/test_ads_shutdown_races.py \
  tests/reliability/recovery/test_ads_recovery_leaks.py -q
# 多协议 fleet（含多 ADS host 采集 E2E）：15 passed
python -m pytest tests/system/test_web_protocol_fleet_e2e.py -q
# 组件目录全量：124 passed
```

门禁（`scripts/ci_gate.py`，scope=product：static + unit/component/contract +
PR target integration-protocol）：

```text
fast --part backend-static         GATE RESULT: PASS（ruff/mypy strict/import-linter 9 kept）
fast --part backend-tests          GATE RESULT: PASS（242 passed；首轮见 §6 UNRELATED 后重跑通过）
pr --targets integration-protocol  GATE RESULT: PASS（143 passed, 2 skipped）
```

### 5.3 仍需真实 TwinCAT 硬件（NOT_RUN，未声称通过）

`test_l3_hardware_concurrency.py` 2 项 skip：真实 PLC 上同 Net ID 多连接时
AdsLib 路由被连带删除的端到端表现、adsAddRoute/DelRoute 在物理设备上的时延
与失败码，仍需设置 `WIND_HUB_TEST_ADS_HOST` / `WIND_HUB_TEST_ADS_NET_ID`
验收。跨进程 Notification 投递（testserver 数据面走进程内 callback_store）
同样属硬件项。

## 6. 失败分类（与本次变更相关性）

- **UNRELATED（有基线实证）**：`test_close_thread_ownership.py` 的 T1/T2
  墙钟断言（~2.02s 管线预算窗口、4s 外层等待）在本机全量负载下约 1/3 轮次
  失败（线程池饥饿导致提前/滞后，曾观测 1.67s 提前返回与 5s gate 自检）。
  暂存本轮改动、在提交基线 d67b7e14 上连跑 6 轮同样失败 3 轮
  （`test_t1_late_close_failure...`、`test_t2_connect_waits...`）。新 T1–T6
  全部 Event 同步，不依赖预算时间，6 轮 0 失败。未放宽任何断言。
- **UNKNOWN（证据不足，最小诊断）**：D+E 同跑曾有 1 次
  `test_d02...contended_count == 0` 失败，伴随服务端 "connection closed by
  remote"；隔离运行与随后 3 轮连跑均通过，基线 2 轮亦通过。diff 中稳态
  读/健康路径无新锁（仅建链/关闭段落缩进一级），无因果路径，暂记环境调度
  机敏，继续观察。

## 7. 剩余风险

1. 同 Net ID 多活动连接从「时序侥幸可用」变为**准入硬拒绝**：若现网存在
   两台逻辑设备共用同一目标 Net ID 的配置，需在配置期区分 Net ID（报错信息
   已点名占用者 `host:port`）。
2. close_local 的直接调用契约收紧：上层仍须先等全部 Driver close 收敛
   （collector/commander assembly 既有契约）；绕过 runtime.stop 直接调用且
   有未收敛物理关闭时现在会收到 ProtocolError 而非被静默强拆。
3. 物理 close 永不补第二发：若 AdsLib 线程在极端情况下卡死，路由锁将挡到
   6s 等待超时并明确报错（不强行建链），恢复仍需进程/端口级处置——这是
   AdsLib 不可安全取消的物理约束，L3 硬件上需复验回收行为。

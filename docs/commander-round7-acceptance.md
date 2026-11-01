# Commander 第七轮独立验收报告 — Write-behind 架构

验收日期：2026-11-01
验收人：Claude Code（独立验收，仅修复权限内局部缺陷）
基线：远程 `origin/feat/commander-four-phase-rewrite` HEAD `8d5b1106`（执行前 fetch 核实）

## 1. Git 基线 / HEAD / 工作区

- 工作分支：`feat/commander-four-phase-rewrite`（未建 PR、未合并 main、未 rebase、未 force push）。
- 验收起点：ChatGPT write-behind 整改已落在分支提交（HEAD `8d5b1106`）+ 工作区未提交测试迁移。
- 本轮 Claude 变更（工作区，验收提交一并落入分支）：
  - 生产：`src/commander/infrastructure/event_repository.py`、`src/commander/application/persistence.py`；
    `control.py`、`service_lifecycle.py` 仅 ruff format（AST 等价）。
  - 测试：`tests/commander/conftest.py`、`component/test_sqlite_writer_concurrency.py`、
    `component/test_event_repository.py`、`component/test_queued_executor.py`、
    `component/test_persistence_service_health.py`（新增）、
    `integration/test_nats_jetstream_real.py`、`system/test_performance.py`。
- 提交 SHA：见分支最新提交（本报告随验收提交一并 push；完整 SHA 记录于提交记录）。

## 2. 静态四门

| 门禁 | 结果 |
|---|---|
| `ruff check src/commander tests/commander` | PASS（0 error） |
| `ruff format --check src/commander tests/commander` | PASS（41 文件全部达标） |
| `mypy src/commander tests/commander` | PASS（41 文件 0 error） |
| `lint-imports` | PASS（11 kept / 0 broken） |

本轮 ChatGPT 未执行检查，四门结论全部由本次验收独立跑出，未沿用上轮结果。
`control.py`、`service_lifecycle.py` 的 format 漂移经 AST dump 比对与基线等价。

## 3. Write-behind 一致性审查

生产主路径（`persistence.py` BufferedJournal/PersistenceWorker + `control.py` +
`event_repository.py` append_events + `service_lifecycle.py` 装配）逐行审查结论：

- **内存同步（T1/T2/T3）**：`CommandControl.accept/connecting/executing(_many)/complete(_many)`
  全部为 loop 内同步内存操作，不 await writer；占死唯一 writer 线程时 T1 仍 <50ms 返回且立即可查
  （`test_control_accept_is_memory_synchronous_even_when_writer_blocked`）。
- **FIFO/并发不变**：同设备串行、跨设备并发、WRITE_MANY 合并/无能力回退的 N1/N1-b 矩阵逻辑零改动，
  仅断言点迁移为"先内存后 flush_buffer 落盘"。
- **批量原子**：`transition_many` 先全量 `validate_transition`（任一非法整批不更新），
  再统一写内存；T2 观察测试在原生多写入口捕获三成员内存均为 EXECUTING、单次 many 调用。
- **落盘模型**：worker 单 asyncio 任务 → 唯一 max_workers=1 ThreadPoolExecutor → 单 SQLite 连接；
  snapshot 取 fields 快照（fields dict 写后不变），每批单事务 `INSERT OR IGNORE`（event_id UNIQUE）
  + 同事务高水位推进；成功才 `confirm(count)`，失败保留快照按原序重试，重试不产生重复审计
  （Nats-Msg-Id 去重在真实 JetStream 双发实证 1 条）。
- **触发口径**：空队列等 wake/interval；非空且 <batch_size 睡固定 50ms 合并窗口（新事件不重置计时，
  持续细流不会饿死落盘）；达到 500 阈值后**背靠背连续 flush 不再等 interval**
  （`test_batch_threshold_flushes_back_to_back_without_interval_wait`：8 事件/批 4，两批 <0.15s）。
- **默认参数**：50ms / 500 / 100000，settings 与 service_settings 双侧默认一致；
  非法值（0/负数/非正整数）在构造与配置加载处 fail-fast，边界测试 4 项通过。
- **查询**：内存命中优先（含按 request_id），缓存回收后回落仓储；批量历史可从 SQLite 查、
  未落盘可从内存查，无缺失/倒退/乱序（真实 JetStream 12 事件全局顺序实证）。
- **两种可靠性分层结构保证**：AuditDelivery 只从 SQLite `pending_events` 读 outbox，
  结构上不可能发布未落盘事件；停机顺序 executor→persistence 排空→audit→transport→journal→writer。
  新增真实 JetStream 实证：worker interval=60s 冻结落盘时命令已在内存终结，
  消费方 0.8s 窗口零消息；显式排空后四事件按序到达
  （`test_volatile_events_are_never_published_before_durable_flush`）。

## 4. 容量 / 内存 / 故障注入

- **容量预留**：受理时每命令预留 4 槽（T1/可选 CONNECTING/T2/T3），
  `pending + reserved + 4N > maximum` 即 OverflowError 拒新；预留与入队在同一同步段内，无 await 竞态。
- **满不阻塞终结**：已受理命令的 T2/T3 由预留槽保证入队，终态时 pop 剩余预留。
- **长期 SQLite 不可用内存有界**：append 永久失败 + maximum_events=8 实测：
  第 3 条命令 OverflowError；2 条已受理全部 SUCCEEDED、现场照写；
  worker.failure 可见；pending 恒为 8 不增长；再来受理仍被拒
  （`test_queue_full_rejects_new_commands_but_accepted_ones_terminate`）。
- **回收**：终态且整 request 全部确认落盘后才入 evictable，LRU 回收上限 10000 条结果缓存；
  未落盘/未终态不提前回收。
- **暂时故障**：前 5 批失败期间现场两条命令正常成功、failure 可见，恢复后 7 行（复用会话无 CONNECTING）
  按序补齐、accepted_total/SUCCEEDED 计数正确、failure 清零。
- **现场不被 SQLite 阻塞**：全故障注入在系统级服务（journal 子类永久失败/杀死 worker）下，
  DEGRADED 与 `__persistence_queue__` 停机报告两个服务级用例通过；
  性能口径见第 8 节（全故障期间 2345 批/s）。

## 5. 崩溃恢复与 UNKNOWN 安全（故障矩阵）

- 崩溃前未落盘事件随进程丢失（允许）：worker 任务取消后内存仍有 4 pending，
  重开库 recover() 为空、物理写仅 1 次，不重发 PLC。
- 已落盘子集恢复保守：EXECUTING→UNKNOWN/UNKNOWN；ACCEPTED/CONNECTING→INTERRUPTED/NOT_SENT；
  只写终态事件，绝不重发现场。仓储直接重开、服务级真实三栈重启
  （寄存器保持 0）双口径实证。
- 恢复幂等：第二次 recover() 为空。
- 写超时/链路异常 UNKNOWN、设备阻塞、许可保留与自动隔离（N1/N1-b）逻辑零改动，全部回归通过。
- **正常停机**：worker 合并窗口可被 stop 立即中断（本轮修复，见第 9 节），
  有界排空成功返回 True；排空超时/worker 死亡返回 False，服务映射 FAILED +
  `frozenset({"__persistence_queue__"})`，绝不伪装干净停机。

## 6. 真实三栈证据

真实 Modbus（pymodbus server + FC16/PDU 计数）6 用例全过：确认写回读、多点无 WRITE_MANY 逐点写、
不可达确证失败、无写权限拒于上线前、RST 中途 NOT_SENT 不沾线、写超时但可能已生效保持 UNKNOWN 不覆盖。
真实 JetStream（自管 nats:latest -js 随机端口容器）7 用例全过：PubAck 流校验、
Nats-Msg-Id 幂等、身份/类型 fail-closed、outbox 四事件有序、N1/N1-b 混合终态双载体 12 事件全局有序、
**未落盘零发布→排空后按序发布**。
系统级 E2E 3 用例全过：真实配置树 + 真实 Modbus + 真实 SQLite WAL/FULL + 真实 JetStream
全链路 4 事件按序；进程重启 EXECUTING→UNKNOWN 不重发；reload no-op。
真实栈测试期间启停容器独立，未复用 soak 4222 栈。

## 7. 回归统计

`pytest tests/commander`（100 设备性能文件 3 用例单独跑）：

- 主套件：**141 passed / 0 failed / 0 xfail / 0 error**（约 28s）。
- 性能文件：**3 passed**。
- 合计 **144 passed**。
- 1 条 warning：真实 JetStream 测试残留 pull 订阅在 event loop 关闭后 GC
  （nats-py 2.16 `Subscription._wait_for_msgs` unraisable，归因于 GC 时机的 Modbus 用例名），
  属既有客户端析构噪声（与已记录 aiokafka stop 泄漏同类），非生产代码路径，记 UNRELATED。
- 旧测试迁移原则遵守：未删除任何有效测试；旧"COMMIT 前不得 PLC"迁移为"内存 T2 前不得 PLC"
  （T2 观察用例）；旧 fail-closed 用例迁移为内存终态+故障期现场不阻塞+恢复后一致；
  受理取消用例按 write-behind 受理门重写（修复了迁移初版的门/取消顺序死锁）。
- 跨模块依赖核查：`CommandControl(` 构造点与新接口引用仅 commander 内部（service_lifecycle、测试），
  collector/web 无引用。

## 8. 性能对照（真实 SQLite WAL/FULL，100 设备 × 10 批 × 10 点，global_capacity=100）

三次正常口径（1000 批/s 目标）：

| 轮次 | 批/s | 点/s | e2e P50/P99 (ms) |
|---|---|---|---|
| 1 | 1160 | 11602 | 88.0 / 106.0 |
| 2 | 1191 | 11914 | 87.4 / 103.0 |
| 3 | 1172 | 11720 | 88.3 / 105.0 |
| 复测（停机修复后） | 1192 | 11925 | 86.6 / 99.4 |

持久化口径（复测）：30100 事件 / 61 个 SQLite 写事务、平均 493.4 事件/批、最大 500；
完成→落盘延迟 P50 553.8ms / P99 822.1ms（合并窗口+背压排空，属 write-behind 设计延迟）；
最大内存积压 29600/100000；writer 线程队列 max_depth=1、排队 P99 0.58ms。
四口径延迟：accept P50/P99=0.04/0.15ms，queue=0.06/0.16ms，write=1.07/1.73ms（注入 1ms），
e2e=1.22/1.97ms，四桶可解释；event loop 最大抖动 0.99ms。

对照 round6 同步基线 396–413 批/s、P99 256–267ms、3.10 事务/批：
现场吞吐 **约 2.9 倍**、超过 1000 批/s 设计目标；SQLite 事务数从约 9700 降到 61（约 160 倍合并）。

SQLite 三态：正常见上；**全程故障期间现场 2345 批/s**（failed_attempts=8、积压 30100 < 上限、
worker.failure 可见）；恢复后 61 批 30100 事件幂等补齐、终态全 SUCCEEDED、无丢失无重复。

## 9. Claude 修复记录（均属权限内局部缺陷，未改架构/安全语义）

1. **P1 `receive_seq` 重启归零（生产缺陷，已修）**：write-behind 首写全新库时
   `repository_meta` 无 `receive_seq` 行，而 `append_events` 用纯 `UPDATE ... MAX`
   不创建行 → 重启后 `latest_receive_seq()` 恒 0，受理序号归零并与历史 ACCEPTED 重复
   （实测复现）。改为 `INSERT ... ON CONFLICT DO UPDATE MAX` UPSERT；
   新增全新库两轮重开/乱序不回退回归测试。
2. **P2 停机不等完合并窗口（生产缺陷，已修）**：worker 非空合并等待原为裸 `asyncio.sleep(interval)`，
   stop 信号不能中断，停机最多多等一个 interval。新增 `_stop_event`，合并窗口
   `wait_for(stop_event, interval)` 固定计时且可被停机立即唤醒；
   新增 5s interval 下 stop <1s 排空回归测试。
3. **P2 worker 异常退出后 stop 抛 CancelledError（生产缺陷，已修）**：
   任务已 done 时 `await shield(task)` 重抛 BaseException 级 CancelledError，
   服务停机路径无法如实报告。`stop()` 增加 done 分支并返回 False，
   服务侧映射 FAILED + `__persistence_queue__`（既有映射）；新增两个服务级测试。
4. 测试/静态：conftest write-behind 夹具体系；SIM105；format 漂移；
   故障注入助手默认语义修正（原默认"不故障"与 docstring 相反，导致长期故障断言失效）；
   受理取消迁移测试的门控/取消顺序死锁；mypy 类型收窄；全部修复无 noqa / type: ignore 新增
   （并移除了迁移中遗留的 type:ignore）。

## 10. OPEN 缺陷与限制

| 级别 | 项 | 说明 / 建议 |
|---|---|---|
| P3 | `_cache_limit=10000` 硬编码 | 结果内存缓存回收阈值不可配置（maximum_events 可配）。建议 ChatGPT 后续与持久化参数一并配置化。 |
| P3 | 落盘可见性延迟 | 高负载下完成→审计落盘 P50≈0.55s/P99≈0.85s（50ms 合并+500 批量的设计代价）。outbox 至少一次可靠性不受影响；若运维要求更短审计可见 SLA，可调小 interval/batch_size，建议在设计书中写明权衡。合并 main 后设计书 v1_1 同步持久化章节仍由 ChatGPT 修订（本轮按任务书未改）。 |
| NOT_RUN | 真机 PLC / netem 全栈 / 长稳 soak | 本机环境无真实 TwinCAT/Modbus 硬件与 root netem；本轮未执行。 |
| UNRELATED | nats-py 析构 unraisable warning ×1 | 见第 7 节，客户端关闭后 GC 噪声。 |

无错误控制、无重复下发、无错误终态、无未受限内存增长；未发现需要交 ChatGPT 做架构调整的问题
（上述 OPEN 均非阻断）。

## 11. 最终裁定

**PASS WITH LIMITATIONS**

- 现场控制安全语义在允许崩溃丢失未落盘审计的新决策下完整保持：内存 T1/T2/T3 同步、
  保守恢复不重发、UNKNOWN 裁决/阻塞隔离零回归、容量有界拒新、停机报告诚实。
- 已落盘事件满足 outbox 至少一次 + 幂等，真实 JetStream 实证"未落盘绝不发布、落盘后有序投递"。
- 性能 1160–1192 批/s（P99 ≤106ms）**达到并超过** 1000 批/s 目标；SQLite 正常/故障/恢复三态实测齐备。
- 四门静态全绿、144 测试全过；无阻断级 OPEN 缺陷。
- 限制项（非阻断）：真机 PLC / root netem 全栈 / 长稳 soak 本机 NOT_RUN；
  `_cache_limit` 不可配置、落盘可见性延迟（P3）；设计书 v1_1 待合并后由 ChatGPT 修订。
  按裁定规则存在非阻断限制，故记 PASS WITH LIMITATIONS 而非无条件 PASS。
- 按任务书：不合并 main、不建 PR、不改正式设计书；验收提交 push 到现有分支，报告交回 ChatGPT。

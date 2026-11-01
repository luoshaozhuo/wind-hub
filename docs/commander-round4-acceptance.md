# Commander 清洁重写 — 第四轮独立验收报告

- 分支：`feat/commander-four-phase-rewrite`
- ChatGPT 整改 HEAD：`3b22c7eb`（8 commits：f15c5552→3b22c7eb）
- 验收提交：`1750bd97`（已推送，未合并 main）
- 设计依据：`docs/Commander_软件设计_v1_1.md`
- 验收日期：2026-10-31（本机 WSL2，wind-hub conda 环境）

## ① Git 基线

| 项 | 值 |
|---|---|
| fetch 后远程 HEAD | `3b22c7eb`（整改前本地=远程） |
| 验收提交 | `1750bd97` 已 push，无 force push、无 rebase、未碰 main |
| 工作区 | 验收后 clean（6 文件改动：4 生产文件纯 ruff format + 2 测试文件） |
| 生产语义改动 | 零。src 四个文件 `git diff` 逐行核对仅为换行/空行格式收敛（A 类） |

## ② 静态门禁

| 门禁 | 命令 | 结果 |
|---|---|---|
| ruff check | `ruff check src/commander tests/commander tests/support/new_commander.py` | **PASS**（All checks passed） |
| ruff format | `ruff format --check src/commander tests/commander` | **PASS**（39 files；ChatGPT 4 个生产文件漂移由验收方 A 类 format 收敛） |
| mypy | `mypy src/commander tests/commander` | **PASS**（Success: 39 source files） |
| lint-imports | `lint-imports` | **PASS**（11 kept, 0 broken，含"new commander application/domain 不依赖 protobuf"） |

附加：`ruff check/format` 对 `tests/support/new_commander.py`、`new_collector.py` 均 PASS。
对 `new_commander.py` 单文件 mypy 报 1 个 `ProtocolPort[Never]` 返回类型错，**origin/main 同文件同样存在（main 上 2 个，本分支减为 1 个）**，且该文件不在项目 mypy 门禁范围内，裁定 UNRELATED 基线。

## ③ Commander 回归

`pytest tests/commander -q`：**119 passed, 1 xfailed，0 failed**（36.36s）。

- 1 xfailed = 本轮新增 N1 strict xfail（见 ④）。
- 含真实 Modbus 链路用例 4 个、真实 NATS JetStream 用例 4 个、真实 Modbus+JetStream 全栈 e2e 2 个（含进程重启恢复）。
- 一条 `PytestUnraisableExceptionWarning`（NATS 客户端 `_attempt_reconnect`/`_wait_for_msgs` 任务在 event loop 关闭后被 GC）：源头是 JetStream 停机测试的第三方客户端任务销毁时机，GC 归因到后续运行的测试；单目录/单文件复跑不复现，非失败、非本轮生产改动引入（audit.py 本轮未改）。记为已知 teardown 卫生问题，不阻断。

## ④ 批量 T2/T3 原子性与故障注入 — CLOSED（含 1 个 OPEN P1 立案）

全部经真实 SQLite（WAL/synchronous=FULL）故障注入实证，位于 `tests/commander/component/test_sqlite_writer_concurrency.py`：

1. **非法转移整批回滚**（同步，writer 线程内）：c1=CONNECTING→EXECUTING 合法、c2=ACCEPTED→SUCCEEDED 非法，整批 ROLLBACK，c1 仍 CONNECTING、c2 仍 ACCEPTED；非 sqlite 错误不置 unavailable，仓储保持 operational。**CLOSED**
2. **T2 事务故障**：注入 `sqlite3.OperationalError`，三个等待方全部收异常；`many_calls==[] 且 one_calls==[]`（现场零写入，T2 先于现场被钉死）；执行器 fatal、新受理被拒；头命令 durable CONNECTING、其余 ACCEPTED。**CLOSED**
3. **T3 事务故障 fail-closed**：现场 `write_many` 真实发生且仅一次（`many_calls==[("p1","p2")]`），终态事务失败后两成员 durable 停在 EXECUTING（恢复语义 UNKNOWN、绝不重发现场），执行器 fatal。**CLOSED**
4. **无 WRITE_MANY 混合确认逐点裁决（成功路径）**：`test_serial_batch_mixed_confirmed_outcomes_recorded_per_point`，p0 CONFIRMED_SUCCESS→SUCCEEDED、p1 CONFIRMED_FAILURE→FAILED(outcome=CONFIRMED_FAILURE)、p2 SUCCEEDED；`one_calls==["p0","p1","p2"]`、`many_calls==[]`；共享逻辑批次的 T2/T3 仍各一个批量事务。**CLOSED**
5. **OPEN P1（N1，strict xfail 在案）：逐点写中途异常丢弃已确认证据**
   - 复现：`test_serial_batch_midpoint_exception_keeps_earlier_confirmed_evidence`（无 WRITE_MANY，脚本 p0 成功 / p1 设备拒绝 / p2 抛 OSError / p3 未触达）。
   - 实际：`queued_executor.py` 的 timeout/exception 路径对**全组** `complete_many(UNKNOWN)`，p0 的 CONFIRMED_SUCCESS 与 p1 的 CONFIRMED_FAILURE 被统一降级 UNKNOWN；证据被 `_write_group` 异常吞没。
   - 期望（v1_1 §业务规则/表 4-11，RR-03/UC-02）：p0=SUCCEEDED、p1=FAILED 按实际确认保留；p2=UNKNOWN（可能已发出）；p3 不得触达且不得标成功。
   - 严重度：**P1（错误终态）**——已协议确认的点被错误裁决为 UNKNOWN，并会错误触发额度保留/自动隔离。timeout、断连路径同根因。
   - 修复方向（B 类，未擅改生产）：`_write_group` 返回 `(已确认逐点结果, 中断点)`，异常时由执行器按点构造 changes 走单个 T3 批量事务；未尝试点保持 NOT_SENT/INTERRUPTED 语义。

receive_seq 全局递增/每请求新 request_id、重启恢复 EXECUTING→UNKNOWN、更早状态→INTERRUPTED/NOT_SENT 不重发现场，由 `test_event_repository.py` 两个恢复用例与 e2e `test_restart_recovers_executing_command_as_unknown` 在本轮全量中 PASS 覆盖。**CLOSED**

## ⑤ 有/无 WRITE_MANY 真实协议证据 — CLOSED（N1 除外的异常路径见 ④）

- 原生批量：`test_batch_points_coalesce_into_single_write_many` 钉死一批一次 `write_many`。
- 无能力回退：`test_driver_without_write_many_falls_back_to_write_one` + 真实 Modbus `test_real_modbus_batch_writes_each_point_separately_without_write_many`：10 点批量在真实链路上逐点 10 次 write_one、无 write_many，逐点持久化。
- 真实写入确认与回读链：`test_real_modbus_write_confirmed_and_persisted`、全栈 `test_full_command_chain_real_modbus_and_jetstream` PASS。
- 不可达/拒绝：`test_unreachable_modbus_connect_is_confirmed_failure`、无写权限点在发帧前拒绝 PASS。
- 单设备串行（FIFO）/多设备并发/额度上限：`test_repeated_accepts_under_serialized_writer_preserve_fifo`（one_calls 顺序 s0..s3、peak≤容量）、32 设备并发受理对账 PASS。
- 写超时：write_timeout 触发设备阻塞、终态 WRITE_TIMEOUT/UNKNOWN 的既有用例随全量 PASS。

## ⑥ Writer 线程与取消安全 — CLOSED

- repo 在唯一 writer 线程内创建/查询/关闭（`check_same_thread` 模型），所有跨线程断言经 `sqlite_writer.submit(...).result()`。
- `CommandControl._run`：`run_in_executor` + `asyncio.shield`，CancelledError 时 `await work` 等不可取消的 SQLite 工作落定再传播。
- close 与在途 SQLite 并发：8 设备在连接/写入窗口停机，close 后零 ACCEPTED/EXECUTING 残留，8/8 持久化终态。
- writer 积压插桩（饱和场景）：最大队列深度 **32**，入队→开跑 P99 **≈3.8–4.0ms**；事件循环心跳最大抖动 **1.04–1.09ms**。
- AuditDelivery shield 停机语义由真实 JetStream 用例与有界轮询停机用例覆盖，本轮 PASS。

## ⑦ 饱和吞吐（100 设备 × 10 批 × 10 点，global_capacity=100，真实 SQLite）

round3 strict xfail **已转正式测试**（不再 xfail），`test_dispatch_throughput_100_devices_real_sqlite` 正式 PASS，两次实测：

| 指标 | round3 基线 | ee92aee0 同步基线 | **round4 实测** | 设计目标 |
|---|---|---|---|---|
| 吞吐 | 180 批/s / 1800 点/s | 670 批/s | **383–419 批/s / 3832–4189 点/s** | 1000 批/s / 10000 点/s |
| E2E P50/P99 | 550 / 575 ms | — / 161 ms | **238–259 / 247–275 ms** | 受理 P99 ≤100ms |
| SQLite 事务/批 | ~22.1（逐点 T2/T3） | — | **3.10**（T1+T2批+T3批；CONNECTING 每设备 1 次，总计 3100/1000 批） | 每批 3 个批事务 |
| writer 队列 | — | — | **max_depth=32，等待 P99 3.8–4.0ms** | — |
| loop 最大抖动 | — | — | **1.04–1.09ms** | — |

四口径拆分（200 次单点）：accept P50/P99=0.21/0.45ms，queue=0.17/0.38ms，write=1.04/1.19ms（注入 1ms 在途全部由 write 桶承载），e2e=1.73/2.39ms；四桶可解释性断言全过。

结论：批量合并使吞吐较 round3 提升 2.1–2.3×、P99 缩短一半以上，事务数从约 22100 降到 3100；但**仍只有设计目标 1000 批/s 的约 40%，且仍低于 ee92aee0 同步实现 670 批/s**（每批 3 次跨线程 executor 往返 + connect 簿记事务是当前上限）。测试地板钉在 ≥300 批/s、P99 ≤400ms 防回归，**性能项保持 OPEN，不解释为达标**。

## ⑧ B7 入口点与 Collector 定向回归 — CLOSED（附 1 个 UNRELATED 基线阻断说明）

- `pyproject.toml` 已无 `wind-hub-commander`/`commander.main` 入口（3b22c7eb）；lint-imports PASS。
- `python -c "import tests.support.new_commander"` / `import tests.support.new_collector"`：**OK**；FakeProtocol/FakeRegistry/FakeListenHandle/FakeSubscriptionHandle 均可导入有效。
- `tests/collector/test_watchdog_restore.py`：模块直接导入 OK；`pytest --noconftest` 运行 **17/17 PASS**（其中 `FakeSubscriptionHandle` 正是从 `tests.support.new_commander` 导入，B7 夹具面被真实执行）。
- 扩大定向：8 个依赖 new_collector/new_commander 夹具的 Collector 测试文件 `--noconftest` 合计 **92 passed**。
- **UNRELATED 基线阻断**：`pytest tests/collector`（带目录 conftest）在收集期即报 `ImportError: cannot import name 'CollectorDocumentLoader' from 'core.infrastructure.config'`（conftest→collector.assembly）。已核实 origin/main（28f9b805）上同样断裂（core 已改名 Shared* 而 collector.assembly 未迁移），与本分支零关联（本分支相对 main 无 src/collector、src/core 改动）。故采用 `--noconftest` 定向执行，证据有效。
- 遗留 P2（文档化）：`tests/support/process.py:221` 的 `start_commander` 与 web 全栈 e2e 仍以字符串 `"wind-hub-commander"` 启动子进程；该入口系 ChatGPT 本轮有意删除（"until formal gRPC server exists"），遗留全栈 real_service e2e 在正式 gRPC server 落地前不可运行。属待建功能缺口而非回归，NOT_RUN。

## ⑨ Claude 小修与提交

仅 A 类，已在 `1750bd97` 推送：

1. `ruff format` 收敛 ChatGPT 4 个生产文件格式漂移（逐行核对无语义改动）。
2. 重写 round3 旧批量故障测试以对准 `transition_many`：非法转移回滚 / T2 故障零现场写 / T3 故障 fail-closed 三个真实 SQLite 用例。
3. 新增无 WRITE_MANY 逐点裁决成功路径用例（正式）与 N1 strict xfail。
4. 饱和吞吐 xfail 转正式测试并加事务计数（3.10/批）、writer 队列积压插桩；修 3 处失效 `# type: ignore`、mypy None 收窄。

未动任何批量事务/状态机/现场 I/O 生产语义；无 noqa、无删测试、无质量规则降级。

## ⑩ 未关闭 / 未验证项

| 项 | 状态 | 说明 |
|---|---|---|
| N1 无 WRITE_MANY 中途异常/超时/断连逐点裁决 | **OPEN（P1，B 类交 ChatGPT）** | strict xfail `test_serial_batch_midpoint_exception_keeps_earlier_confirmed_evidence` |
| 饱和吞吐距 1000 批/s 目标 | **OPEN（P2）** | 383–419 vs 1000 批/s；防回归地板已钉 |
| tests/collector conftest 收集阻断 | UNRELATED（main 基线） | CollectorDocumentLoader 改名迁移债，origin/main 同样存在 |
| 全栈 web e2e（start_commander 子进程） | NOT_RUN（P2 文档化） | 入口有意删除，等正式 gRPC server |
| 真实 TwinCAT/硬件 L3、netem 非 root、长稳 soak | NOT_RUN | 环境不具备，与上轮一致 |
| NATS 客户端 loop 关闭后 GC 警告 | 已知卫生项 | 第三方任务销毁时机，非失败 |

## ⑪ 裁定：FAIL

批量 T2/T3 单事务原子性、T2 先于现场、T3 fail-closed、唯一 writer 线程与取消安全、有/无 WRITE_MANY 的成功路径与真实 Modbus/JetStream 证据、 B7 夹具面与 92 项 Collector 定向回归全部闭环；四门静态全绿；119 passed + 1 strict xfail。

但 **N1 构成"错误终态"P1**：无 WRITE_MANY 协议共享批次在顺序写中途异常/超时/断连时，已经协议确认（CONFIRMED_SUCCESS / CONFIRMED_FAILURE）的前序点被整组错误裁决为 UNKNOWN，违反 v1_1 表 4-11"按实际现场证据逐点裁决"，并会错误触发额度保留与自动隔离。按验收规则"只要存在错误终态的 P1，整体必须 FAIL；现场写入正确性优先于吞吐量"，本轮裁定 **FAIL**，N1 交 ChatGPT 修复后转下一轮复测（修复后该 strict xfail 应自动转正式通过）。

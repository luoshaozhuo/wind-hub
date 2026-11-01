# Commander 第六轮独立验收报告 — N1-b 终态裁决

- 分支：`feat/commander-four-phase-rewrite`
- ChatGPT 整改基线：`454ae5bb`（`_SerialWriteError` 增 `current_unconfirmed`，区分抛异常中断点与返回 UNKNOWN 结果点）
- 验收提交：`bb1394c9`（已推送，本地=远程；未合并 main）
- 设计依据：`docs/Commander_软件设计_v1_1.md`
- 验收日期：2026-11-01（WSL2，wind-hub conda 环境）

## 1. Git 基线、提交与工作区

| 项 | 值 |
|---|---|
| fetch 后远程 HEAD（整改前） | `454ae5bb`，本地一致、工作区 clean |
| 验收提交 | `bb1394c9df96049042bf4c1dc33b8a49b0c53cbd` 已 push |
| 工作区 | 推送后 clean；无新分支/PR/rebase/force push，未碰 main |
| 生产改动 | 仅 ruff format queued_executor.py；与 `454ae5bb` **AST 比对完全相同**，零语义改动 |

## 2. 两项原 strict xfail 转正结果

| 用例 | round5 | round6 |
|---|---|---|
| 组件 `test_serial_batch_returned_unknown_outcome_suffix_is_not_sent` | strict xfail | **XPASS → 移除标记转正式**（PASS） |
| 真实 Modbus 用例 | strict xfail | **重写后转正式**（见下说明，PASS，连跑 5 次稳定） |

真实 Modbus 用例的重要修正：round5 设想"写往不存在 slave 3 → 从站无响应 → UNKNOWN"在本 mock 上不成立——从站实际回 Modbus 异常码 03，驱动按既有契约判 **CONFIRMED_FAILURE**（异常码 1/2/3 为可证失败）；且原测试在探测读取阶段就已抛错，xfail 从未真正验证目标语义。本轮改为确定性的真实连接消失注入：**p0 确认后经驱动公共 `close()` 物理拆除 TCP（从站保持在线）**，后续 write_one 在提交前即可证未发送。未通过改期望凑绿——终态断言来自对驱动既有契约的重新核实。

## 3. 故障矩阵逐项结果

| 场景 | 预期 | 实测 | 状态 |
|---|---|---|---|
| 第 2 点返回 UNKNOWN | SUCCEEDED/UNKNOWN/INTERRUPTED | 一致，one_calls=[p0,p1]，p2 outcome=NOT_SENT | **CLOSED** |
| 第 1 点返回 UNKNOWN | UNKNOWN/INTERRUPTED/INTERRUPTED | 一致，one_calls=[p0] | **CLOSED** |
| 第 2 点抛异常 | SUCCEEDED/UNKNOWN/INTERRUPTED | 一致（round5 参数化矩阵） | **CLOSED** |
| 第 1 点抛异常 | UNKNOWN/INTERRUPTED/INTERRUPTED | 一致 | **CLOSED** |
| 第 2 点 CONFIRMED_FAILURE | 前序成功、本点 FAILED、后续正常执行 | 组件混合用例 p0/p1/p2=S/F/S；真实 RST 用例 p1/p2 均 S/F/F 且不重试 | **CLOSED** |
| 中途超时、底层未收敛 | 已确认保留，其余 UNKNOWN | p0 SUCCEEDED、p1/p2 UNKNOWN；放行后物理全写、终态不覆盖（round5 正式用例持续 PASS） | **CLOSED** |
| 全部成功 | 全部 SUCCEEDED | 10000 点性能对账 + 真实 Modbus 批量逐点回读 | **CLOSED** |
| T3 事务失败 | 整批回滚、恢复 UNKNOWN、不重发 | 四点 durable EXECUTING、现场停在中断点、重开 recover 全 UNKNOWN（round5 正式用例持续 PASS） | **CLOSED** |

`current_unconfirmed` 语义核查：该标志仅决定"中断点自身（index==len(confirmed)）"是 UNKNOWN 还是 NOT_SENT，不参与任何已确认点的裁决；已确认前缀（含返回 UNKNOWN 的结果点本身，走 confirmed 分支拿 WRITE_UNKNOWN）不会被覆盖；确定未调用点无任何路径可标 SUCCEEDED；每点物理写恰好一次（异常/UNKNOWN 后立即中止批次）。隔离与额度：异常/UNKNOWN 路径 blocked+retain+auto-isolation；超时路径经 `_block_late` 持有 native 至物理收敛后才隔离——时序与 round5 一致，本轮生产逻辑零改动。

## 4. 真实链路证据

**真实 Modbus（pymodbus server，真实 FC06 帧）**，6/6 PASS：
- 新增 `test_real_modbus_rst_mid_batch_confirmed_not_sent_never_reaches_wire`：p0 后物理断连，p0=SUCCEEDED、p1/p2=FAILED/CONFIRMED_FAILURE（驱动"提交前会话已关"契约），**服务端写 PDU 计数=1**，wrapper.physical=[p0,p1,p2]（无重试），回读 p0=11.5、p1/p2 保持探测原值。
- round5 `test_real_modbus_write_timeout_but_applied_stays_unknown_no_overwrite` 持续 PASS：PDU 真实生效（3 个写计数、寄存器回读 111.25/222.5/333.75）但终态保持 UNKNOWN。
- 原有确认/逐点批量/不可达/无权限四用例持续 PASS。
- 说明：要在真实 wire 上得到"返回 UNKNOWN 结果点 + NOT_SENT 后缀"需流量黑洞（PDU 发出无应答，驱动 `_execute`→ProtocolConnectionError→UNKNOWN 而非提交前 CONFIRMED_FAILURE），本机非 root 无法 netem，**NOT_RUN**；该裁决分支由组件级正式用例（真实执行器+真实 SQLite WAL/FULL）覆盖。

**真实 SQLite WAL/FULL**：矩阵全部经唯一 writer 线程真实仓储；T2/T3 单事务原子回滚、T3 失败 fail-closed、重开恢复 UNKNOWN 不重发，round4/5 注入用例持续 PASS；INTERRUPTED 与 NOT_SENT outcome 的配对在仓储层逐点断言。

**真实 JetStream（nats:latest -js 容器）**，6/6 PASS：混合终态批次审计用例本轮**参数化两种载体**（p2 抛异常 / p2 返回 UNKNOWN），两者均产出 12 事件：4 ACCEPTED→4 EXECUTING→4 COMPLETED，event_id 全唯一，COMPLETED state/outcome 逐点为 SUCCEEDED/CONFIRMED_SUCCESS、FAILED/CONFIRMED_FAILURE、UNKNOWN/UNKNOWN、INTERRUPTED/NOT_SENT，与仓储 `get_command` 投影完全一致。

## 5. 静态检查

| 门禁 | 结果 |
|---|---|
| `ruff check src/commander tests/commander` | **PASS** |
| `ruff format --check src/commander tests/commander` | **PASS**（39 files；ChatGPT queued_executor.py 格式漂移已 A 类 format，AST 相同） |
| `mypy src/commander tests/commander` | **PASS**（39 source files） |
| `lint-imports` | **PASS**（11 kept, 0 broken） |

无 noqa/type: ignore/门禁改动/删测试。

## 6. 完整 Commander 回归

`pytest tests/commander -q`：**132 passed、0 xfailed、0 failed**（38.4s，含真实 Modbus/JetStream/全栈 e2e）。1 条 `PytestUnraisableExceptionWarning`（NATS 客户端任务 loop 关闭后 GC，跨测试归因到性能测试文件名）：round4/5 同性质已知卫生项，非失败、非生产代码引入。

## 7. 性能防回归（同口径 100 设备×10 批×10 点，global_capacity=100）

| 指标 | round5 | **round6（三次）** |
|---|---|---|
| 吞吐 | 419–433 批/s | **396 / 401 / 413 批/s（3964–4134 点/s）** |
| E2E P50/P99 | 230–238 / 239–249 ms | **242–251 / 256–267 ms** |
| SQLite 事务/批 | 3.10 | **3.10（3100，零变化）** |
| writer 队列 | depth 32 / P99 3.52ms | **depth 32 / P99 3.78–3.93ms** |
| loop 最大抖动 | 1.09ms | **1.08ms** |

四口径：accept 0.20/0.38、queue 0.16/0.38、write 1.07/1.12、e2e 1.68/1.95 ms。吞吐区间下沿较 round5 低约 5%，处于历轮 run-to-run 波动范围（round4 383–419、round5 419–433），P99/事务数/队列深度均无实质变化——**安全修复未引入明显性能退化**；防回归地板 ≥300 批/s、P99≤400ms 持续通过。设计目标 1000 批/s 仍只达约 40%，**P2 OPEN 保持**。

## 8. Claude 小规模修复（均在 `bb1394c9`）

1. 移除组件级 N1-b strict xfail 转正式；新增返回 UNKNOWN 两位置参数化矩阵（含 NOT_SENT outcome 与物理前缀断言）。
2. 重写真实 Modbus 用例：原 xfail 证伪（slave 不存在回异常码 03=CONFIRMED_FAILURE），改为 p0 后驱动 close 的确定性连接消失注入，PDU 计数/回读/无重试三重物理证据。
3. JetStream 混合批次审计用例参数化异常/返回 UNKNOWN 两载体。
4. ruff format 收敛生产文件（AST 验证零语义改动）。

未动状态机/事务/调度/I/O 所有权生产代码。

## 9. 剩余缺陷与未验证项

| 项 | 级别 | 状态 |
|---|---|---|
| N1 / N1-b 全部终态裁决 | — | **CLOSED**，0 xfail |
| 饱和吞吐 396–413 vs 1000 批/s | P2 | OPEN（唯一功能性差距） |
| 真实 wire 黑洞型 UNKNOWN（netem 非 root） | — | NOT_RUN（组件+真实 JetStream 覆盖裁决分支） |
| 真实 TwinCAT L3 / soak | — | NOT_RUN（环境不具备） |
| NATS 客户端 loop 关闭 GC 警告 | 卫生项 | 已知，第三方任务销毁时机 |
| tests/support/process.py start_commander 待建 gRPC 入口、全栈 web e2e | P2 | 沿用，正式 gRPC server 落地前 NOT_RUN |

## 10. 最终裁定：PASS WITH LIMITATIONS

N1-b 两项 strict xfail 均已转正（真实 Modbus 一项经证伪后按驱动真实契约重写，未放松断言）；8 行故障矩阵全部 CLOSED，无错误终态、无重复写入、无资源提前释放；真实 Modbus（PDU 计数+寄存器回读）、真实 SQLite（原子性/恢复）、真实 JetStream（两载体审计投影一致）证据齐全；四门静态全绿；**132 passed、0 xfailed、0 failed**；性能无退化但距 1000 批/s 设计目标仍差约 2.4×（P2 OPEN）。按裁定规则——安全语义与真实链路全部通过、仅性能目标未达——**PASS WITH LIMITATIONS**。建议交回 ChatGPT 决定吞吐专项整改（当前每批 3 次跨线程往返 + CONNECTING 簿记事务是主要上限）及最终合并。

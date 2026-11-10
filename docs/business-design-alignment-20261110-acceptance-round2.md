# 业务设计对齐分支第二轮独立复测报告（2026-11-11）

- 复测对象：分支 `docs/business-design-alignment-20261110`，远端 HEAD 与本地一致
- 复测起点（上轮验收提交）：`0bc47793`
- 本轮 HEAD：`34c24f5b2917efd9e709eff13fb75367d7dfb6d7`（= `origin/docs/business-design-alignment-20261110`，fetch 后核对一致）
- 基线（上轮）：`77746a3a` + 验收提交 `0bc47793`
- 原则：所有结果均在 `34c24f5b` 工作树上当日实跑取得，不复用旧 HEAD 结果；生产逻辑缺陷只记录不自行修改。

## 1. Git HEAD、变更边界与工作树

### 1.1 新范围提交（`0bc47793..34c24f5b`，10 个提交）

生产代码仅 2 个文件，全部属于 Data Generator；设计文档 3 份换版；无测试文件改动：

| 文件 | 类型 | 内容 |
|---|---|---|
| `src/data_generator/application/runtime.py` | M | 阻塞结算时保留 `terminal_state=FAILED` 的可见 FAILED 状态（两处，:125-132、:309-315），不再被 STOPPING 覆盖 |
| `src/data_generator/infrastructure/gateway.py` | M | 新增 `_lost_leases` 权威判失集合；RENEW `accepted=false` 入集合并隔离；发送短路返回本地 `LEASE_LOST`（不发 RPC）；新 ACQUIRE 清除 |
| `CLI_软件设计_v1_21.md` → `CLI_软件设计_v1_23.md` | R099 | 中间 v1_22 同范围退役 |
| `Commander_软件设计_v1_26.md` → `Commander_软件设计_v1_27.md` | R099 | 换版 |
| `Data_Generator_软件设计_v1_19.md` → `Data_Generator_软件设计_v1_20.md` | R099 | 换版 |

### 1.2 冻结边界核对（PASS）

`git diff --name-only 0bc47793..34c24f5b | grep -E "contracts|core|configuration|sink"` 无输出：Contracts、Core、Configuration、Sink Service 设计/代码/配置零变更。工作树初始干净；复测中仅产生本报告与 1 处授权格式修复（见 2.2）。

## 2. S1 静态门禁（逐项实跑）

| 门禁 | 命令 | 退出码 | 结果 |
|---|---|---|---|
| Ruff Check | `python -m ruff check src tests scripts` | 0 | All checks passed! |
| Ruff Format（首轮） | `python -m ruff format --check src tests scripts` | **1** | `src/data_generator/infrastructure/gateway.py` would be reformatted |
| Ruff Format（修复后复验） | 同上 | 0 | 740 files already formatted |
| Mypy | `python -m mypy src` | 0 | Success: no issues found in 331 source files |
| Import Linter | `lint-imports` | 0 | Contracts: 15 kept, 0 broken |
| Proto 漂移 | `python scripts/generate_rpc.py --check` | 0 | 无生成物差异 |
| API 漂移 | `python scripts/generate_api_contract.py --check` | 0 | DRIFT CHECK: PASS |
| 严格 Marker 收集 | `pytest --collect-only -q` | 0 | 2233 tests collected（与上轮一致，本轮无新增/删除测试） |

### 2.1 格式缺陷定位（ChatGPT 引入，已按授权修复）

`ruff format --diff` 显示仅 `gateway.py:345-348` 一处列表换行风格差异（3 行 ↔ 1 行），无语义变化。该文件正是本轮生产改动文件，属新提交引入的格式违规（上轮 HEAD 门禁全绿可证）。已执行 `ruff format` 单文件修复，diff 为纯换行（+1/-3），修复后 check/format 双绿。修复保留在工作树并随本报告提交，未触碰任何业务逻辑。

## 3. P1-09 重启失效与 FAILED 状态复测（任务点 3）

### 3.1 命令与结果

```
python -m pytest tests/data_generator/system/test_lease_system.py::test_p1_09_gateway_restart_invalidates_all_tokens -v
→ 1 passed in 5.67s（退出码 0）
python -m pytest tests/data_generator/system/test_lease_system.py
→ 11 passed in 43.93s（退出码 0，p1_01/03/04/05/08/09/10/11_14/12/13/15 全绿）
```

### 3.2 断言有效性审查（未被弱化）

测试代码未在本轮改动，三条关键断言保持严格：

1. **Gateway 重启 → 旧租约失效**：旁路客户端用重启前取得的 60s 令牌写入，必须抛 `FAILED_PRECONDITION`（test_lease_system.py:341-343）。
2. **Generation 必须转 FAILED**：10s 轮询窗口内 `actual_state == "FAILED"` 且 `last_error` 含 "lease"（:345-355）——上轮在此得到 RUNNING 而失败的同一断言，本轮真实通过。
3. **未收敛 I/O 不提前释放**（关联用例，同轮实跑全 PASS）：
   - `test_grpc_system.py::test_stop_retains_target_until_io_settled`：原生调用挂起时第二个 Generation 无法取得目标，DEGRADED 隔离可见；
   - `test_lifecycle_system.py::test_unsettled_io_shutdown`：SIGTERM 有界关闭（<10s、退出码 0），不把取消当底层完成；
   - `test_source_runtime.py::test_failed_output_marks_generation_failed`：失败态保持。
   命令：三用例合跑 `3 passed in 6.32s`。

### 3.3 生产代码机制核对

`runtime.py` 的两处修复使 `_reconcile_locked()` 与 `_run()` finally 在 `_blocked()`（仍持有 claim 或存在 unsafe/unsettled 网关）期间，对 `terminal_state=="FAILED"` 的 Generation 保持 `actual_state="FAILED"`，只有 I/O 安全收敛后才允许结算释放——与 DG v1_20 §9.3.2「StopGeneration 等待在途 I/O 安全收敛后释放」一致。**裁决：PASS。**

## 4. 续租 `accepted=false` 与网络超时的语义分化（任务点 4）

### 4.1 代码审查（`gateway.py:222-257, 344-355`）

| 情形 | 代码路径 | 状态后果 | 是否发 RPC/重发 |
|---|---|---|---|
| RENEW 收到 `accepted=false`（权威拒绝） | `_lost_leases.add()` + `_unsafe.add()`，renewer 退出（:245-248） | 后续 `_send_one` 短路返回 `DeliveryResult(FAILED, LEASE_LOST)`，**不产生任何 gRPC 调用**（:345-348）；runtime.py:478-479 据此抛 GeneratorError → Generation FAILED | 不重发 |
| RENEW 抛 `RpcError/TimeoutError`（网络不确定） | 仅 `_unsafe.add()`，`continue` 保持续租循环（:253-257） | 发送返回 `TARGET_QUARANTINED`（UNKNOWN 类，非致命）；下一次 RENEW 由 Gateway 存活令牌裁决，成功且无 unsettled 时解除隔离（:249-252） | 不重发旧批次 |
| 重新 ACQUIRE 成功 | `_lost_leases.discard()`（:196） | 新 Generation 才可能恢复 | — |

与 DG v1_20:1988 逐句对应：「Gateway 对 RENEW 明确返回 `accepted=false` 时视为当前租约失效……以 `LEASE_LOST` 将 Generation 转为 FAILED。网络超时/确认丢失仅标为不安全及 UNKNOWN，不得凭超时臆断租约失效，也不得重发未确认批次。」

### 4.2 实测证据

- 权威判失链路：p1_09 重启后 RENEW 对新注册表必然 `accepted=false/LEASE_LOST`，端到端验证 `_lost_leases` → 本地 LEASE_LOST → FAILED 全链（PASS，见第 3 节）。
- 超时隔离不重发：`test_output_backpressure.py::test_logical_timeout_is_unknown_and_quarantines_gateway`（DG 组件 31/31 全绿，本轮 `31 passed in 24.63s`）断言首次 UNKNOWN/RPC_TIMEOUT 后第二次 `send` 直接得 `TARGET_QUARANTINED`，无额外原生调用；`test_observability_system.py::test_obs2...` 验证受控 RENEW 探测恢复且指标口径与真实 RPC 数一致、无旧批次重放。
- **测试缺口（GAP，非失败）**：没有用例直接以 `claim_abort: {"RENEW": UNAVAILABLE}` 故障夹具验证「RENEW 网络超时后 renewer 存活、后续 RENEW 成功可恢复、Generation 不转 FAILED」这一独立分支。现有证据为代码审查 + 相邻语义覆盖。建议 ChatGPT 补组件级用例：可变 behaviors 的假 servicer 先对 RENEW 注入 UNAVAILABLE/超时再恢复，断言 `unsafe_gateways` 先置位后清除、`_lost_leases` 始终不含该网关、Generation 保持 RUNNING。**本项裁决：实现 PASS，直接测试 NOT_RUN（GAP）。**

## 5. Commander FIFO 独立语义审查（任务点 5）

### 5.1 本轮新鲜复跑

```
pytest tests/industrial_gateway/integration/test_command_outcomes.py::test_concurrent_same_point_writes_serialize_at_commander
→ FAILED（退出码 1），本次实测 [20, 30, 40, 10] == [10, 20, 30, 40] 断言失败
```

乱序排列跨运行变化（上轮 [30,10,20,40]、本轮 [20,30,40,10]），属调度非确定性，不是稳定逆序。industrial_gateway 全树本轮 `169 passed, 1 failed`，唯一失败即本项。

### 5.2 三层顺序的语义拆解

1. **并发客户端启动顺序**：测试以 `asyncio.gather` 并发发起 4 条独立 TCP 连接（test_command_outcomes.py:166）。连接建立与请求到达顺序不受事件循环创建顺序保证，因此 [10,20,30,40] 只是「启动顺序」，不是 Gateway 的接收顺序。
2. **Gateway 接收/捕获顺序**：协议适配器在请求实际处理时调 `forwarder.capture()` 入队（forwarder.py:157-196），`asyncio.Queue` 对**实际入队顺序**保持 FIFO；跨 4 条连接的入队顺序本身不等于启动顺序。
3. **Commander 实际受理顺序**：转发器默认 16 个 worker（v1_schema.py:128 `max_forward_inflight=16`）竞争 `queue.get()`，再以每 (device,point) 一把 `asyncio.Lock` 串行（forwarder.py:223-229）。Lock 只保证互斥（在途恒为 1，测试该断言通过），不保证「队首事件先获锁」——不同 worker 取出的同目标事件可因调度交错获锁，受理顺序可偏离捕获顺序。

### 5.3 裁决：生产缺陷成立，测试断言也需精化（均交 ChatGPT，未放宽任何断言）

- GW v1_23 §6.3 原文：「**每个控制目标的捕获顺序应保持，跨目标可并发**」；§5.1 转发组件职责含「事件顺序」。当前 worker 池+无公平键锁的结构没有保持同目标捕获顺序的机制，违反该冻结设计不变量——**生产缺陷 P2 成立**。
- 但测试把「客户端启动顺序」直接当作应保持的顺序，锚点错误：设计只承诺**捕获顺序 → 受理顺序**一致，不承诺跨连接启动顺序。`ForwardRecord` 已带 `captured_at`，正确断言应为「Commander 受理值序列 == 按 captured_at 排序的捕获值序列」，并保留现有三条有效断言（4 记录、4 个不同 command_id、max_inflight==1）。这是断言锚点修正而非弱化，顺序约束本身一条不少。
- 建议生产最小修复方向（供 ChatGPT 评估，不在本分支实施）：同目标事件键控串行——例如 per-key 有序 worker（dequeue 后按键委派给键专属队列）或键级在途许可（worker 持键直到该键队首事件提交完成才允许下一同键事件 dequeue），跨目标并发度保持不变。
- **本项裁决：FAIL（既有缺陷，两轮表现一致，与本轮 DG 改动无关）。**

## 6. pb2 越界最小重构方案（任务点 6）

本轮新鲜复跑 `pytest tests/core/architecture/test_rpc_boundary.py`：仍为 `1 failed, 3 passed`，7 个越界文件清单与上轮逐字一致，本轮 diff 不涉及其中任何文件——既有结构债务，未恶化。

冻结约束：不得扩大 `_ALLOWED_PB2_PREFIXES` 白名单。逐文件最小方案（按改动面从小到大）：

| 越界文件 | pb2 实际用法 | 最小重构 |
|---|---|---|
| `industrial_gateway/infrastructure/variable_leases.py` | 仅以 `pb.ClaimTargetsRequest` 作 `claim()` 入参类型 | 引入域内 dataclass（如 `LeaseClaimRequest`：gateway_id/owner_id/names/action/token/lease_ms），注册表只依赖域类型；由已在白名单的 `infrastructure/grpc/data_server.py` 做 proto↔域对象映射。单元测试改构造域对象（纯测试改动） |
| `collector/assembly.py` | 仅 2 行 `add_*Servicer_to_server` 装配 | 把服务端注册移入既有白名单包 `collector/infrastructure/grpc/`（新增 `build_server()` 工厂），assembly 只调工厂 |
| `industrial_gateway/infrastructure/observability.py`、`diagnostics.py`、`config/snapshots.py`、`runtime.py` | 直接构造/返回 pb2 消息（Metric/Event、DiagnoseResponse、SnapshotFile、ApplicationResult） | 整体迁入既有白名单包 `infrastructure/grpc/`（纯包移动+导入修正，wire 行为零变化）；若需保留域逻辑，则把 pb2 构造拆为 grpc 包内 mapper，域层只返回原生 dataclass/datetime |
| `industrial_gateway/infrastructure/commander/forwarder.py` | 构造 `commander_pb2` 请求、`Timestamp`、持有 stub | 拆分：队列/键锁/状态机保留原位且不依赖 pb2（提交入参改为域命令对象）；proto 映射与 stub 调用移入 `infrastructure/grpc/commander_client.py`，以 Protocol 接口注入转发器 |

每步均为行为保持型移动或类型替换，可逐文件独立提交并以 `test_rpc_boundary.py` 转绿为完成判据；import-linter 15 契约须同步复验。**本项裁决：FAIL 状态维持（基线既有），方案已就绪，实施归 ChatGPT。**

## 7. 三份换版设计文档核对（任务点 7）

### 7.1 第 1–2 章原文比对（脚本 `/tmp/extract_ch12_v2.py`，退出码 0）

| 比对 | 结果 |
|---|---|
| CLI v1_22 → v1_23 第 1–2 章 | IDENTICAL（14668 字符） |
| CLI v1_21 → v1_22 第 1–2 章（补全链前一跳） | IDENTICAL |
| Commander v1_26 → v1_27 第 1–2 章 | IDENTICAL（14250 字符） |
| Data Generator v1_19 → v1_20 第 1–2 章 | IDENTICAL（14250 字符） |

### 7.2 上轮文档缺陷复验

1. CLI v1_21:1753 残留「Contracts v1_9」：**已修复**。CLI v1_23:1753 现为「《Contracts 软件设计 v1_18》」，全文版本引用一致（v1_18/v1_31/v1_13，Commander v1_27、架构 v1_2 均为本轮有效版本）。
2. Commander 测试路径 `tests/src/commander/`：**部分修复（残留 P3）**。v1_27:2237 与 14.1 已显式声明该路径是「目标目录」、现存测试在 `tests/commander/`、不得把目标记为已存在；但 :2726、:2765、:2807、:2824 四处表级文字仍以陈述语气写「物理路径统一为 tests/src/commander/...」，依赖读者回溯前文 caveat。建议把四处统一改为「目标路径（现状 tests/commander/，迁移见 14.1）」。
3. DG v1_20 新增追溯缺口（**P2 新发现**）：第 14 章矩阵列了 10 个测试文件，但实际 `tests/data_generator/system/` 有 10 个测试文件，其中 `test_lease_system.py`（含 p1_09 等 11 个租约用例，是 §9.3.2 租约权威判失条款的主要可执行证据）、`test_observability_system.py`、`test_error_classification_system.py` 三个文件无矩阵行。建议补 UR→UC→ST 行，尤其把 p1_09 关联到 :1988 权威判失契约。

### 7.3 跨文档引用

三份文档对冻结基线（Contracts v1_18、Core v1_31、Configuration v1_13、main@36748125）的引用一致；DG v1_20:1943-1992 对 ClaimTargets/租约语义的描述与本轮生产实现逐项吻合；未发现指向已退役旧版文档的悬空引用。**文档裁决：PASS WITH LIMITATIONS（2 项 P2/P3 追溯/表述遗留）。**

## 8. 本轮测试执行总览（全部为 34c24f5b 工作树新鲜结果）

| 范围 | 命令 | 退出码 | 结果 |
|---|---|---|---|
| DG 全树 | `pytest tests/data_generator` | 0 | 170 passed in 249.08s |
| DG 组件 | `pytest tests/data_generator/component` | 0 | 31 passed |
| 租约系统 | `pytest tests/data_generator/system/test_lease_system.py` | 0 | 11 passed |
| Commander+CLI | `pytest tests/commander tests/cli` | 1 | 403 passed, 18 skipped, **3 failed**（tcp 探测时序、systemd×2，WSL2 环境，基线同名） |
| Industrial Gateway | `pytest tests/industrial_gateway` | 1 | 169 passed, **1 failed**（FIFO，见第 5 节） |
| RPC 架构边界 | `pytest tests/core/architecture/test_rpc_boundary.py` | 1 | 3 passed, **1 failed**（pb2 七处，见第 6 节） |
| 上轮失败节点新鲜复核 | `pytest tests/src/performance/test_batch_throughput.py tests/system/e2e/test_command_via_server.py tests/system/reload/test_server_config_apply.py` | 1 | 5 failed（command_via_server×4、reload×1，与上轮同名同因），throughput 合跑失败 1 次 |
| throughput 隔离刻画 | 同节点连跑 5 次 | 0 | 5/5 passed（0.52-0.53s） |
| web fleet 夹具 | `pytest tests/system/test_web_fleet_config_e2e.py` | 1 | 1 passed, **21 errors**（mTLS CA fixture，与上轮同文件同数量） |

关于 throughput 用例：本轮发现它在与其他进程并发争用时也会失败（不仅全量套件），隔离则 5/5 稳定通过；代码路径仅涉及 `collector.infrastructure.publisher`/`core`/`contracts`，与本轮两个 DG 改动文件零交集，维持 **UNRELATED**，但新增一条测试质量建议（P2）：该用例以墙钟等待管线排空（实测 220/300），应改为确定性排空同步（回调/事件），消除主机敏感抖动。

## 9. 缺陷与裁决汇总

| 编号 | 项 | 定性 | 处置归属 | 裁决 |
|---|---|---|---|---|
| R2-01 | gateway.py Ruff Format 违规（本轮新引入） | 格式 | 已由验收方修复（纯换行，行为不变） | PASS（修复后） |
| R2-02 | DG 重启后 Generation 滞留 RUNNING（上轮 U-2） | 本轮生产修复 | ChatGPT 已修，p1_09 及关联 14 用例实测通过 | PASS |
| R2-03 | RENEW 超时分支无直接测试 | 测试缺口 | ChatGPT 补故障夹具用例（设计见 4.2） | NOT_RUN（GAP） |
| R2-04 | 同目标转发捕获顺序不保持（FIFO） | 生产缺陷违反 GW v1_23 §6.3；测试锚点也需修正 | ChatGPT，禁止放宽顺序断言 | FAIL（既有，两轮一致） |
| R2-05 | pb2 七处越界 | 结构债务 | ChatGPT 按第 6 节方案逐步消除，禁止扩白名单 | FAIL（基线既有，未恶化） |
| R2-06 | Commander v1_27 四表行目标路径表述 | 文档 P3 | ChatGPT 加现状限定 | PASS WITH LIMITATIONS |
| R2-07 | DG v1_20 ch14 漏 3 个真实测试文件 | 文档追溯 P2 | ChatGPT 补矩阵行（含 p1_09→:1988） | PASS WITH LIMITATIONS |
| R2-08 | CLI/systemd/web fleet/command_via_server/reload 环境与既有失败 | UNRELATED | 环境/既有问题清单维持 | UNRELATED |
| R2-09 | throughput 用例主机争用抖动 | UNRELATED + 测试质量 P2 | 建议确定性排空改造 | UNRELATED |
| R2-10 | 真实 PLC/ADS/IEC104、Sink↔Gateway 持令牌集成、真实守护进程 | 环境不具备 | 维持 NOT_RUN；Sink P1 阻断仍待 Sink 分支改造 | NOT_RUN / BLOCKED |

**总裁决：PASS WITH LIMITATIONS。** 本轮生产修订（DG 租约权威判失与 FAILED 状态保持）经真实 Gateway 全栈测试验证达成设计契约，RELATED 失败为 0；静态门禁在修复 1 处新引入格式问题后全绿；冻结模块与 Sink 边界零触碰。两项既有 FAIL（FIFO、pb2 边界）与三项文档/测试遗留按上表移交 ChatGPT，未以任何断言放宽或 skip/ignore 掩盖。

## 10. 验收方改动清单

1. `src/data_generator/infrastructure/gateway.py`：仅 Ruff Format 换行修复（+1/-3，零行为变化）。
2. 本报告文件。
未修改其他任何生产代码、测试、夹具或配置；未 push、未 PR、未合并。

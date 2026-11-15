# R1-F2 五模块 Ports & Adapters 架构整改集中验收报告

- 日期：2026-11-14
- 仓库：`luoshaozhuo/wind-hub`
- 分支：`design/architecture-dfd-alignment-20261113`
- 验收起点：`cad077e0cec3a81d69a926c1b16323968dfcba21`
- 机械修复提交：`9c6282e8`
- 测试同步提交：`dcc9259c`
- 范围：Collector v1_24、Commander v1_43、Industrial Gateway v1_38、Data Generator v1_44、Sink Service v1_57，以及总体逻辑架构 v1_15 第 6 章
- 裁决：**FAIL**

本轮没有创建新分支、PR、合并 main、rebase 或 force push。生产修改仅为 Ruff 导入排序、格式化和既有 N802 例外路径同步；未开展业务状态机、Port 契约或目录职责的大规模重写。

## 1. 裁决摘要

不能依据目录已经名为 `adapters/inbound`、`adapters/outbound`、import-linter 全绿或多数单元测试通过而判定架构整改完成。五模块主要业务链仍存在 Application Port 缺失、业务编排/状态机滞留 Infrastructure、Inbound Adapter 承担应用流程、Application 直接执行文件/SQL/线上协议细节等问题。

此外，最终运行证据发现四组独立阻断：

1. Collector 不能接受仓库出厂共享配置树；
2. Data Generator 输出目标变量 ID 与真实 Gateway 目录变量 ID 不一致，50 项失败；
3. Commander 黑洞连接超时产生 `CONFIRMED_FAILURE`，违反保守 UNKNOWN 要求；
4. 共享配置多文件提交在真实独立进程读者中暴露跨文件代际不一致。

严格 S4 所需的“中间件独立启动、全部业务模块由 CLI 启动为独立进程、全部业务操作经 CLI 发出”完整 Harness 不存在，标记 **NOT_RUN**，不以进程内集成测试替代。

## 2. S1 架构矩阵

| 模块 | 已确认符合项 | 主要不符合项 | 结论 |
|---|---|---|---|
| Collector | Domain 未见外层依赖；正式启动入口和 Assembly 基本唯一；NATS 技术连接集中在出站实现 | 批次身份、组批/拆包位于 NATS Adapter；任务 START/STOP、revision、YAML Push/Apply 编排在 Infrastructure Config；Inbound gRPC 直接 TCP Diagnose、读取 Publisher 私有状态并裁决健康 | FAIL |
| Commander | Domain 命令状态机、FIFO、UNKNOWN 主模型、恢复与 Outbox 主体分层较清楚；SQLite/JetStream 技术实现独立 | Application 经 Core Registry 直接选择/构造真实 Session，缺统一 ControlExecutionPort 与协议 Outbound Adapter；完整 Get/Push/Apply 和请求聚合/Diagnose 位于 Inbound gRPC | FAIL |
| Industrial Gateway | pymodbus 集中在 Modbus Adapter；正式启动入口唯一 | `infrastructure/runtime.py` 同时构造 Adapter、应用配置、Start/Stop、ClaimTargets、UpdateVariables 和恢复调度；Forwarder 混合 FIFO、过期、UNKNOWN/REJECTED 与 gRPC 状态；正式租约/fencing 在 Infrastructure，另有仅测试使用的简化 Application 租约实现 | FAIL |
| Data Generator | gRPC/Proto 主体位于 Adapter；配置与 Domain 基础类型可分离 | 模型算法与两条生成路径在 Application；Replay 直接 CSV/SQLite/SQL/临时目录；Runtime 直接 JSONL；Gateway Outbound Adapter 合并 OutputCoordinator、租约、ACK/UNKNOWN/quarantine；Persistence 决定候选/restart_required/运行快照切换；缺独立 Assembly | FAIL |
| Sink Service | Redis/Kafka/Gateway/Modbus 出站 Adapter 基本唯一；Persistence generation/CAS 技术职责较合理 | RealtimeRuntime 持有共享引用、Semaphore、额度裁决和未决 I/O；ReliableRuntime 混合 Pull Subscribe、DLQ、补发、退避、ACK/NAK/InProgress 与业务终局；Worker 承担 watchdog、指标窗口和配置收敛；Application 直接生成 Kafka Record、读取 YAML；Domain 含 gRPC/NATS/证书技术 Schema；缺 Sink 专属三层 import-linter 合约 | FAIL |

### 2.1 重点文件裁定

- `industrial_gateway/infrastructure/runtime.py`：不是单纯 Composition Root 或技术运行时，包含正式应用编排。
- `sink_service/infrastructure/realtime_runtime.py`：并发额度、共享 Adapter 生命周期与 Delivery 协调属于应用职责，不能仅因使用具体 Adapter 就全部归入 Infrastructure。
- `sink_service/infrastructure/realtime_runtime.py` 与 `reliable_runtime.py`：应拆分“技术连接/消息语义”和“Delivery 应用策略”，本轮未擅自重写。
- `sink_service/infrastructure/worker.py`：进程 IPC 可留 Infrastructure，但 watchdog、指标窗口和配置收敛需明确应用归属。

### 2.2 架构缺陷分级汇总

源码审查形成 **P1=19、P2=9、P3=1**：

- Collector + Commander：P1=6、P2=2；
- Industrial Gateway + Data Generator：P1=7、P2=4；
- Sink Service：P1=6、P2=3、P3=1。

上述数量是架构问题台账，不包含下节运行时新发现的控制安全和跨模块接口缺陷。

## 3. S2 八项门禁

最终在机械修复与测试同步后的代码上执行；最后两项 Data Generator 文件格式化后，`ruff format --check` 复跑为 0。

| 门禁 | 命令 | 退出码 |
|---|---|---:|
| 环境 | `python scripts/dev.py env` | 0 |
| 锁文件 | `poetry check --lock` | 0 |
| RPC 生成物 | `python scripts/generate_rpc.py --check` | 0 |
| 语法 | `python -m compileall -q src tests scripts` | 0 |
| Ruff lint | `ruff check src tests scripts` | 0 |
| Ruff format | `ruff format --check src tests scripts` | 0 |
| mypy strict | `mypy src` | 0，373 source files |
| import-linter | `lint-imports` | 0，15 kept / 0 broken |

起点 Ruff 的 108 个错误主要来自迁移后 per-file-ignore 路径失效及导入排序。五个 N802 例外只同步到现存 Adapter 文件，没有新增规则类别、新增消红忽略或放宽检查。

## 4. S3 测试统计

| 范围 | 结果 | 说明 |
|---|---|---|
| Collector | **140 passed / 2 failed / 2 skipped** | 两项均为真实出厂配置准入失败 |
| Commander | **341 passed / 1 failed / 1 skipped** | 黑洞超时项单独复跑仍失败；另有 NATS event-loop-closed 警告 |
| Industrial Gateway | **202 passed** | 全绿不代表其 Runtime/Forwarder 架构符合设计 |
| Data Generator | **131 passed / 50 failed** | 主因为真实 Gateway 目标变量 ID 契约不一致，含独立 DG 子进程用例 |
| Sink Service | **798 passed / 7 skipped / 4 collection errors** | 缺失既有测试设施；真实 system 目录未收集 |
| CLI + Contracts | **279 passed / 18 skipped** | 全量通过 |
| `tests/src` | **342 passed / 2 failed / 1 collection error** | Commander deadline、共享配置代际、旧 NATS Consumer 导入 |
| 可收集迁移定向子集 | **22 passed** | Publisher、配置 reload、负载/吞吐、仿真授权链；不与上行重复累加 |

### 4.1 RT-P1：Collector 出厂共享配置启动失败

失败：

- `test_all_services_and_inventory`
- `test_full_snapshot_grpc_roundtrip`

共同错误为 `ConfigError: template reference must be a mapping`。测试复制真实 `configs/instances` 与 `configs/templates`；Collector `_validate_all_templates()` 对所有 device model 强制 `resolve_model_templates()`，而 Data Generator 物理型号没有 `task` mapping。不能删除共享模板或放宽断言消红，需由生产整改明确不同服务模型类型的准入边界。

### 4.2 RT-P1：Data Generator ↔ Gateway 变量 ID 契约断裂

`service_settings.py` 以源 `point_id` 构造 `OutputTarget(gateway_id, point_id)`；GatewayWriter 预检因此查找裸 `a02_dm_pitchangle1`。真实 Gateway Fixture 的目录和读写接口使用 `GW01.a02_dm_pitchangle1`（直接 Gateway 用例亦使用 `GW01.pitch_angle`）。结果为 `Gateway gw-a missing targets` 并级联 50 项失败。

曾尝试机械统一点表，实证表明只改测试无法同时满足两侧现行契约，已全部撤回。不得通过伪造目录、绕过 preflight 或放宽断言消红；需裁定变量身份究竟为裸 point ID 还是全限定 variable ID，并同步配置加载、目录、Claim、Update 与 Read。

### 4.3 RT-P1：Commander 黑洞连接被记为确定失败

`test_expiration_and_unknown[_tz01_to_blackhole]` 全量失败，单独复跑仍失败：

- 实际：`state=FAILED`、`outcome=CONFIRMED_FAILURE`、`error_code=CONNECT_TIMEOUT`；
- 测试允许：`NOT_SENT` 或 `UNKNOWN`。

连接黑洞没有现场写结果证据，不能宣称确定失败。这是控制结果保守语义问题，交 ChatGPT/生产整改，不由验收方修改状态机。

### 4.4 RT-P1：共享配置跨文件代际不一致

将陈旧 Fixture 的 `templates/units.yaml` 机械同步到实际 `templates/units_standard.yaml` 后，真实多进程测试继续失败：读者观察到 `devices=(1,2,0)`、`units=(1,1,0)`。这证明不是单纯路径问题，而是多文件提交/读取未保证同代快照。需检查提交锁、读锁及 manifest 代际协议。

### 4.5 测试迁移与未决项

- Sink 4 个收集错误：缺 `tests.fixtures.services.r5_stack` 和 `tests.src.unit.test_b2_batch_builder`。该问题在 R1 历史轮次已存在；本轮未伪造缺失设施或恢复废弃生产架构。
- `tests/src/integration/test_batch_nats_real_link.py` 仍导入已删除的 `sink_service.infrastructure.nats_consumer.NatsSinkConsumer`。现行 `NatsRealtimeIngress` API/职责不同，不能只改 import 冒充迁移完成。
- Commander `test_rpc_deadline_does_not_duplicate_field_write` 三次 50ms 调用均未超时，`DID NOT RAISE`。单独复跑仍失败；需用确定性服务端延迟注入区分测试时序假设与生产取消语义，不通过放大允许结果集消红。
- Data Generator 已机械同步 `instances/data-generator.yaml` → `services/data-generator.yaml` 及事务 journal 目录；这些修复不解决变量 ID 生产契约问题。

## 5. S4 独立进程 CLI E2E

**严格五模块 S4：NOT_RUN。**

已核查现有方案：

- Data Generator system 使用真实 DG 子进程，但 Gateway 在 pytest 进程内启动，操作使用 gRPC Stub；
- CLI real-Gateway 套件使用真实独立 CLI 子进程，但生产 Gateway 与 pytest 共用进程；
- Commander system 有真实中间件/协议栈，但不是全部业务操作经 CLI 的五模块流程；
- Sink system 被 `r5_stack` 缺失阻断收集。

未发现同时满足以下三项的现成 Harness：

1. NATS、Redis、Kafka、TDengine 等由测试设施独立启动；
2. 所有参与业务模块由 CLI 启动为独立进程；
3. 所有业务操作指令经 CLI 发出。

因此不把进程内直接调用、`dg_subprocess` 单项覆盖或 FakeRpcServer 结果描述为完整 E2E。真实生产设备、硬件和外部部署 fencing 同样 **NOT_RUN**，未向任何未经授权设备写入控制命令。

## 6. 机械修改边界

- `9c6282e8`：仅生产导入排序、格式化、未使用导入移除，以及五个既有 N802 例外路径同步。
- `dcc9259c`：测试导入、`services`/`units_standard` 路径同步，删除已被现行 Scenario Schema 淘汰且触发类型错误的重复准备步骤；保留真正的类型不匹配断言。
- 没有通过 `ignore`、`noqa`、`skip`、`xfail`、删测试或放宽断言制造通过。
- 没有修改业务状态机、Port 契约、Proto、跨模块接口或控制结果允许集合。

## 7. 移交整改优先级

1. **P0/P1 控制安全**：Commander 黑洞 `CONNECT_TIMEOUT` 不得产生 `CONFIRMED_FAILURE`。
2. **P1 跨模块契约**：统一 Data Generator 与 Gateway 变量身份，恢复真实 Update/Claim/Read 主链。
3. **P1 配置一致性**：修复共享配置多文件同代读取；明确 Collector 对无 `task` 物理模型的准入规则。
4. **P1 架构主链**：按五份模块设计拆分 Application Port、应用编排与 Concrete Adapter，优先处理四个重点 Runtime/Worker 文件。
5. **P2 测试资产**：迁移 Sink r5/b2 设施和旧 NATS Consumer 集成测试，恢复真实中间件系统覆盖。
6. **S4 设施**：建设或明确接入严格 CLI 独立进程 E2E，完成后再作完整五模块端到端裁决。

在上述控制安全、跨模块主链和架构 P1 未闭环前，R1-F2 不具备 PASS 或 PASS WITH LIMITATIONS 条件。

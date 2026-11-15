# R1-F2 五模块 Ports & Adapters 架构整改第二轮集中复测报告

- 日期：2026-11-14
- 仓库：`luoshaozhuo/wind-hub`
- 分支：`design/architecture-dfd-alignment-20261113`
- 复测起点：`bf91066f3684dcb90abb5033bfbea2e7426262f9`
- 第一轮对照：`6b07c18e54e4ebc03b2e01bdcaade97fecf12351`
- 范围：Collector v1_24、Commander v1_44、Industrial Gateway v1_40、Data Generator v1_46、Sink Service v1_58
- 裁决：**FAIL**

本轮没有创建 PR、合并 main、rebase、force push 或切换开发分支。生产修改只包含 Ruff 导入排序、格式化、未使用导入清理及一个 `UP038` 等价写法；没有擅自重写业务状态机、Port 契约、Proto 或公共配置契约。测试修复没有通过新增 `ignore`、`noqa`、`skip`、`xfail` 或放宽断言消红。

## 1. 裁决摘要

第一轮四组运行时主链阻断已复测关闭：Collector 出厂物理型号准入、DG/Gateway 变量身份及显式一对多映射、Commander 未收敛连接 UNKNOWN、共享配置共同代际读取。DG 全量、Gateway 全量、CLI/Contracts 和静态门禁均通过；Sink 收集错误、真实 NATS 迁移、缺失 R5 资产和 Commander deadline 非确定性已直接修复。

但以下条件仍不满足准出：

1. 五模块仍有重大应用编排、端口归属和运行时安全职责错位；
2. Sink 输出池对物理目标额度和取消后原生 I/O 收敛仍有 OPEN 安全项；
3. Web Server 仍调用已删除的旧 Sink RPC；
4. 严格 CLI 独立进程五模块 S4 未执行，且发现 DG → Gateway 与 CLI 的 mTLS 契约不能直连；
5. 真实生产设备、外部部署 fencing、硬件和 netem 多机环境未执行。

因此，主链测试变绿不能替代架构验收，最终仍为 **FAIL**。后续 P1 生产整改交 ChatGPT 集中处理。

## 2. 第一轮生产整改范围

`6b07c18e..bf91066f` 共 18 个提交，生产及设计变更约 26 文件，主要包括：

- Collector 接受无 Collector `task` 的共享物理型号；
- Commander 原生 Session 仅由组合根工厂创建，未收敛连接超时改 UNKNOWN；
- DG 默认 `<device_id>.<point_id>`，支持显式 `variable_mapping` 一对多及目标唯一性，新增 JSONL Recording Port/Adapter；
- Gateway 租约、FIFO、过期、UNKNOWN 和 gRPC 错误分类迁移；
- Sink Realtime Delivery 生命周期、入口 Port 和共享输出资源配额迁移。

## 3. S1 四组运行时专项

| 编号 | 复测证据 | 裁决 |
|---|---|---|
| F2-RT-01 Collector 无 `task` 物理型号 | 出厂共享配置两项原失败定向 `2 passed`；Collector 全量 `142 passed / 2 skipped` | **CLOSED** |
| F2-RT-02 DG/Gateway 变量身份 | DG 全量 `181 passed`；仿真系统 `9 passed`。Fixture 保留真实 variable ID，默认全限定名、显式一对多、目标唯一；ListVariables/ClaimTargets/UpdateVariables 均保留 | **CLOSED** |
| F2-RT-03 Commander 黑洞连接 | 原黑洞项 `1 passed`；未收敛连接断言 `state=UNKNOWN`、`outcome=UNKNOWN`、`CONNECT_TIMEOUT`，安全阻断与 release 断言保留 | **CLOSED** |
| F2-RT-04 共享配置跨代 | reader 启动前先提交共同 `1.0.1`，再提交至 `1.0.40`；最终 devices/units 均严格断言 `1.0.40`，`3 passed` | **CLOSED：旧失败为初始代际不同的测试误判，未证明生产跨代读取** |

Commander 测试中仍观察到 audit/NATS event-loop-closed 或 pending-task 警告，不能据此宣称所有异步资源释放完全无告警；该项保留为后续诊断，不扩大为已验证通过。

## 4. S2 架构 CLOSED / OPEN 台账

### 4.1 本轮关闭的 P1

1. **Commander 原生 Session 构造**：`commander/assembly.py` 与正式启动入口注入具体工厂，Application 不再经 Registry 直接选择 SDK Driver。
2. **Gateway 正式租约权威**：`application/variable_leases.py` 管理 Claim/Update 原子业务规则，gRPC Adapter 做请求转换。
3. **Gateway 转发状态机**：`application/forwarding.py` 管理 FIFO、deadline、撤销和 UNKNOWN；Infrastructure 仅做具体客户端与 gRPC 状态分类。
4. **DG JSONL 记录**：`application/ports/recording.py` 与 `infrastructure/adapters/outbound/recording.py` 分离，正式启动入口注入 `JsonlRecordWriter`。
5. **Sink Realtime Delivery 与共享池迁移**：`application/realtime_delivery.py`、`application/output_pool.py` 和 `application/ports/realtime.py` 接管本轮设计规定的生命周期和额度，`infrastructure/realtime_runtime.py` 保留具体技术装配。

上述 CLOSED 只针对对应职责，不表示整个模块通过。

### 4.2 OPEN P1（19 项）

| 模块 | 数量 | OPEN 项及精确位置 |
|---|---:|---|
| Collector | 4 | `infrastructure/adapters/outbound/publisher.py:27` 仍在 NATS Adapter 生成批次身份并组批；`infrastructure/adapters/inbound/grpc/config_service.py:80` 与 `infrastructure/config/task_control.py` 持有 Push/Apply、revision、任务控制应用流程；`config_service.py:495` 仍在 Inbound Adapter 做诊断编排/TCP I/O；`infrastructure/adapters/inbound/grpc/collector_service.py:313` 将 Apply 错误暴露为 `CONVERGENCE_PENDING`，与正式 `APPLY_FAILED` 语义不一致 |
| Commander | 2 | `infrastructure/adapters/inbound/grpc/server.py:132` 的提交/请求聚合仍含应用流程；同文件 `CommonRpc:403` 仍承载完整配置 Apply 与 Diagnose 编排 |
| Industrial Gateway | 1 | `infrastructure/runtime.py:69` 仍同时管理配置应用、重建、APPLIED/FAILED、写事件映射、Start/Stop、租约入口和具体 DeviceModbusServer/CommandForwarder 构造，缺 ProtocolServerPort 与纯 Composition Root 边界 |
| Data Generator | 4 | `application/runtime.py:23-25` 同时接 `physical_engine.py`、`model/random.py`、`models.py`，模型双路径未统一；`infrastructure/adapters/outbound/gateway.py:36` 的 GatewayWriter 仍合并 OutputCoordinator、租约、UNKNOWN/quarantine 与具体 gRPC；`infrastructure/persistence/snapshots.py:127` 决定候选比较、`restart_required` 和应用切换且缺独立 Assembly；`gateway.py:108,218,371` 只创建明文 Channel，与 `cli/runtime.py:186` 强制 mTLS 的严格业务链不能直连 |
| Sink Service | 8 | `infrastructure/reliable_runtime.py:44` 仍是完整 Reliable 消费状态机、ACK/DLQ/重试；`infrastructure/worker.py` 仍含 watchdog、指标窗口和配置收敛；`application/delivery_point.py:22` 仍生成 Kafka Record/JSON；Application 直接 YAML/文件 I/O、Domain 技术 Schema 未分离；无 Sink 专属三层 import-linter 合约；`application/output_pool.py:41-45,81-89` 在原生 I/O Task 被取消时也由 done callback 立即释放额度，未证明物理安全收敛；`output_pool.py:121-150` 按 `sink_id` 而非物理目标身份共享池，多 Sink ID 可绕开同一物理目标额度；`web_server/application/sink/service.py:121-124` 与 `web_server/adapter/outbound/grpc/sink.py:55,80` 仍调用 `StartSink/StopSink/ClearSink/ListSinks` 等已删除 RPC |

**P1=19**：第一轮 19 项中关闭 5 项；本轮新增/细化了 Collector Apply 错误语义、Sink 取消后额度释放、物理目标额度身份、Web 旧 RPC 和 DG/CLI 传输契约五项。

### 4.3 OPEN P2 / P3

P2=9：

- Collector：`infrastructure/config/task_control.py` 的细分应用流程、`config_service.py` 诊断与出版者私有健康状态耦合。
- Gateway：`application/target_leases.py` 与正式 `variable_leases.py` 双套租约；`ALREADY_EXISTS` 当前归为 REJECTED，需设计裁定。
- DG：`application/replay.py:79-198` 直接 CSV、SQLite、SQL 和临时目录；版本化测试辅助名称/旧工具 Fixture 路径。
- Sink：合法零匹配批次 ACK 缺配置代/跳过原因；`application/observability.py` 指标失败会清上一完成快照；状态机缺 DEGRADED。

P3=1：`infrastructure/realtime_runtime.py` docstring 仍有过时职责描述。

## 5. S3 最终门禁与测试

### 5.1 八门禁

| 门禁 | 命令 | 退出码 |
|---|---|---:|
| 环境 | `python scripts/dev.py env` | 0 |
| 锁文件 | `poetry check --lock` | 0 |
| RPC 生成物 | `python scripts/generate_rpc.py --check` | 0 |
| 语法 | `python -m compileall -q src tests scripts` | 0 |
| Ruff lint | `ruff check src tests scripts` | 0 |
| Ruff format | `ruff format --check src tests scripts` | 0 |
| mypy strict | `mypy src` | 0，379 source files |
| import-linter | `lint-imports` | 0，15 kept / 0 broken |

### 5.2 测试统计

| 范围 | 结果 | 说明 |
|---|---|---|
| Collector | `142 passed / 2 skipped` | 最终全量 |
| Commander | `342 passed / 1 skipped` | 最终全量；原四项已关闭，另有 11 个异步资源警告 |
| Industrial Gateway | `202 passed` | 最终全量 |
| Data Generator | `181 passed` | 最终全量；仿真系统 `9 passed` |
| Sink Service（普通全量） | `870 passed / 7 skipped / 33 warnings` | 974.92 秒；独立 600 秒容量文件单列执行。33 个警告均为未注册 pytest mark |
| Sink R14 600 秒持续容量 | `1 passed / 9 warnings` | 612.31 秒；2 Delivery、500 点/批、目标 120 批/s |
| CLI + Contracts | `279 passed / 18 skipped` | 最终全量 |
| `tests/src` | `347 passed` | 最终无端口争用串行复跑 |

R14 实测发布 70,403 批 / 35,201,500 值，Kafka 确认同为 35,201,500 值，零丢失；稳态 117.33 批/s（58,665.09 值/s），积压斜率 0，最终两 Durable 均排空，DLQ=0、无重投、587 次 gRPC 探针零失败。单核 CPU 68.1%，最大 RSS 219.5 MiB，写延迟 P99 最大 24.63 ms，事件环 lag P99 最大 13.11 ms。原始结果保存在执行机 `/tmp/r14_capacity_d2_r120.json`。

不重复累加以下定向证据：

- Kafka fake/attempt/encoded batch：`12 passed`；
- 真实 JetStream 旧 Consumer 迁移：`3 passed`；
- 真实 Redis Sink：`2 passed`；
- Commander deadline 与 RPC 架构：`5 passed`；
- Gateway 原两失败：`2 passed`；
- Commander 原四失败：`4 passed`。

### 5.3 本轮测试资产修复

1. 恢复 `tests/fixtures/services/r5_stack.py` 与 `docker-compose.r5.yml`：独立 Compose project、动态 loopback 端口，仅清理本 project，不做全局 prune。
2. 新增 `tests/sink_service/unit/kafka_producer_fake.py`，替代不存在的 `tests/src/unit/test_b2_batch_builder.py` 依赖；覆盖 create_batch、append、send_batch、confirmation Future 和并发门控。
3. `test_batch_nats_real_link.py` 迁移到现行 `ReliableDeliveryPort`，保留真实 CollectorPublisher、PubAck、Durable、重投与 DLQ；Kafka 下游仍为明确标注的 Fake，不冒充完整真实下游。
4. Commander deadline 在受理持久化后、RPC 返回前确定性阻塞，严格保持 SUCCEEDED、单次现场写和同 request_id 去重断言。
5. Sink system 旧进程启动参数迁移到当前 `--config-root` 真实子进程入口。
6. 并发 Reliable Fixture 改用共享 `template_path()` 写入 `sink_kafka_history_v<version>.yaml`，消除真实子进程启动时的模板路径漂移；对应配置单测 `20 passed`。

## 6. S4 严格 CLI 独立进程 E2E

**严格五模块 S4：NOT_RUN / BLOCKED。**

本机 user systemd 可用，R5 中间件栈也可独立启动；但调查正式实现后确认当前不能无替身建立所要求的完整拓扑：

1. CLI 对每个 gRPC 目标强制 `tls_profile`，只创建 secure channel（`src/cli/runtime.py:186,273-296`）；DG 的 GatewayWriter 在三处只创建 `insecure_channel`（`src/data_generator/infrastructure/adapters/outbound/gateway.py:108,218,371`）。因此无法让同一 Gateway 同时接受 CLI mTLS 操作和 DG 正式明文写主链。
2. Collector 单实例 `messaging.delivery` 只允许 realtime 或 reliable 二选一（`src/collector/infrastructure/config/validation.py:176-177`）。严格双通道拓扑需要部署两个独立 Collector 身份及相应 Sink source，不能用同进程内双 publisher 冒充。
3. 现成 CLI real-Gateway 测试的生产 Gateway 与 pytest 同进程；现有 system 测试虽有真实业务子进程，但并非全部由 CLI `service start/stop/restart` 管理，业务操作也不全经 CLI。

本轮没有以 TLS 代理、进程内 Fake、额外直接 gRPC 调用或测试专用生产兼容层掩盖上述差异，也没有声称完成启动、Apply、数据流、恢复和安全退出的严格全链。真实生产设备、外部部署 fencing、硬件、netem 与多机均 **NOT_RUN**，未向任何未经授权设备写控制命令。

## 7. 移交下一轮集中整改

1. Sink：先修取消后原生 I/O 收敛和物理目标额度身份，再拆 Reliable/Worker 应用状态机；同步迁移 Web 到现行 Delivery RPC。
2. DG/CLI：由设计明确 Gateway 统一 mTLS 客户端配置与证书注入，不能用测试代理代替生产能力；随后建设双 Collector 身份的严格 CLI 双通道 Harness。
3. Gateway/Collector/Commander：将 Runtime 和 Inbound gRPC 中剩余应用流程迁入 Application，定义真实 Port，清理双套租约和 Apply 错误语义。
4. DG：统一模型入口，拆 OutputCoordinator/Gateway Client，移出 Replay 技术 I/O，明确 Assembly 与 Persistence 边界。
5. 补齐 Sink 专属 import-linter 合约及 P2/P3，不以静态门禁全绿替代职责审查。

在重大架构 P1 与严格 S4 未闭环前，R1-F2 不具备 PASS 或 PASS WITH LIMITATIONS 条件。

## 8. 最终代码树与提交

本报告对应的代码与测试树为 `c7877d8ac0e5571694519a7b55c2125ef74b7eb2`（报告自身随后单独提交）。本轮复测与修复提交：

1. `75e0e8dc`：迁移后边界的机械格式化；
2. `22382e8b`：DG 显式 Gateway variable mappings；
3. `f480ff02`：共享配置共同代际；
4. `6722d8fb`：Commander Session 装配与确定性 deadline；
5. `5a0d1570`：Gateway Application 转发状态与 Adapter 路径；
6. `c7c16234`：Sink 真实中间件和当前进程 Fixture；
7. `c7877d8a`：并发 Kafka 模板规范路径。

未创建 PR、未合并 main、未 rebase、未 force push、未切换开发分支。

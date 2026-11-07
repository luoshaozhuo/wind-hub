# Sink Service v1 重写 S1–S4 统一独立验收报告（第二轮，覆盖版）

- 验收日期：2026-11-07
- 交付分支：`feat/sink-service-v1-rewrite`
- 验收绑定 HEAD：`66ccdcbbd15ad195e42f4d111aacfa951aff03f0`（短 SHA `66ccdcbb`）
- 设计基线：`docs/Sink_Service_软件设计_v1_20.md`（§3.1.2 容量门槛、§11 指标、§14.3/§14.4 测试与 UC 矩阵）
- 验收人：Claude Code 独立验收
- 上一轮报告：同路径 2026-11-06 版（绑定 `25d50cb3`，总体 FAIL），本版整体覆盖
- 约束遵循：未创建 PR、未合并 main、未 rebase / force push；未升级依赖、未降低校验、未新增任何 ignore / xfail / skip；未读取/输出 `.env.local` 敏感值。**验收期间生产代码零改动**，本会话仅在仓库外 `/tmp/qa_sink/` 编写 QA 脚本与证据，并覆盖本报告。
- 收口说明：验收完成后按所有者指令一次性收口工作树——失效的 v1_1x 双进程旧测试直接删除（不再以不收集目录保留），四层级新测试、配置 WIP 与本报告纳入单个 commit；本地 `.claude/settings.json` 的 `/tmp` 临时权限条目不入库。详见 §8。

> 工作区状态：验收命令针对 HEAD `66ccdcbb` 之上的会话前 WIP 工作树执行；收口提交内容见 §8，缺陷裁决与证据均对该工作树状态有效。

## 0. 总体结论（先行）

**总体裁决：FAIL。** 与上一轮相比，S1 mypy 从 27 错误收敛到 sink 2 + collector 7（共 9），S2 sink 套件从 73 failed/12 errors 翻转为 **135 passed / 0 skip / 0 xfail**，S3 在当前 HEAD 取得五套真实链路新鲜证据全部 PASS；但两条阻断项仍在：

1. **P1 装配断裂未消除**：Collector v2 gRPC 入口引用已退役的 `collector_v2_pb2` 生成物，7 个测试文件收集期 ImportError，且 `src/collector/main.py:20` 实际装配该模块，属运行时同断。
2. **S4 容量门槛实测 FAIL**：独立 Publisher 600s 持续供数达标（177,137 值/s，目标的 98.4%，零发布错误），sink 仅确认 **54,206 值/s（目标 30.1%）**，可靠通道结束时积压 148,358 批；全程零硬故障、gRPC 100% 可用，失败性质是**吞吐容量不足**，不是正确性或稳定性缺陷。

| 阶段 | 裁决 | 一句话依据 |
| --- | --- | --- |
| S1 静态 | **FAIL** | ruff check / format（61 文件）、lint-imports（13 kept/0 broken）全绿；mypy sink **2 errors/1 文件**、collector **7 errors/2 文件**、core 0 |
| S2 单元/组件/契约/集成 | **FAIL（P1 阻断）** | contracts 91 PASS、sink **135 PASS/0 skip/0 xfail**；collector **97 passed/2 failed/7 errors**（P1 ImportError 链）；MATRIX §14.3 35 项 = 6 IMPLEMENTED/7 PARTIAL/21 ABSENT/1 NOT_RUN，§14.4 42 UC 全 ABSENT（S3 在 pytest 外重建） |
| S3 集成/系统/可靠性 | **PASS（含 5 项环境性 NOT_RUN）** | smoke+SIGKILL、Main 重启、S3-07、S3-08、S3-10 五套真实三进程链路在 HEAD 66ccdcbb 全部 exit=0；S3-01/02/03/04/09 缺故障代理/配额夹具，按设计禁止 Mock，记 NOT_RUN |
| S4 性能（600s 满载） | **FAIL** | Publisher 供数 98.4% 达标（非 NOT_RUN）；sink 确认 54,206/180,000 值/s；可靠积压线性增至 148,358 批；failed/DLQ/rejected/retry 全 0，gRPC 121/121 可用 |

NOT_RUN / FAIL 项均不计通过；全程未使用任何 Mock 替代真实进程、真实 gRPC 或真实消息，未用输入速率冒充确认速率，未做多容器聚合。

## 1. S1 — 静态门禁（FAIL）

针对工作树实跑结果：

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| Ruff Lint | `ruff check src/sink_service src/collector src/contracts` | **PASS** — All checks passed! |
| Ruff Format | `ruff format --check src/sink_service src/collector src/contracts` | **PASS** — 61 files already formatted |
| Import Linter | `lint-imports` | **PASS** — 13 kept, 0 broken（含三层架构、sink 不导入其他进程包契约） |
| Mypy strict | `mypy src/sink_service` | **FAIL** — 2 errors in 1 file (checked 25 source files) |
| Mypy strict | `mypy src/collector` | **FAIL** — 7 errors in 2 files (checked 33 source files) |
| Mypy strict | `mypy src/core` | **PASS** — Success: no issues found in 75 source files |

### 1.1 sink_service mypy 2 错误（D-S1-1，P2，反馈 ChatGPT）

- `application/delivery_registry.py:168`：`entry = self._entries.get(identifier)` 把 `_Entry | None` 赋给 `_Entry` 类型变量；紧接 :169 行已做 `if entry is None: continue` 判空，运行期安全，但 strict 推断不达标。
- `application/delivery_registry.py:529`：`getattr(entry.port, "last_error", "")` 第三参报 `arg-type`（期望 bool）。`DeliveryPort.last_error` 契约与全部实现均为 `-> str`，独立最小复现为 mypy 推断噪声，但门禁口径下仍是错误。
- 对比上一轮 27 errors/9 files，其余 25 项已由分支内后续提交修复，本会话未插任何 `# type: ignore`。

### 1.2 collector mypy 7 错误（6 项归属 P1，1 项 P2）

- `infrastructure/grpc/v2_server.py:16-17`：`from contracts import collector_v2_pb2 / collector_v2_pb2_grpc` → `attr-defined`，并连锁 :192/:390/:497/:712 四个 `Class cannot subclass ... (has type "Any")`（共 6 错误）。根因与影响见 §5 D-S2-2。
- `infrastructure/config/loader.py:66`：`device_group_id=identifier` 把 `str` 传给 `core.domain.Task` 的 `DeviceGroupId` NewType（P2，D-S1-2）。

### 1.3 S1 配置闭包行为取证（自动化 + 只读核查，PASS）

`unit/test_config_validation.py`（39 节点）钉死：单 Service 恰好一个服务文件、显式 pathname 且拒绝相对/越界路径（无默认路径回退）、空/超限/重复快照拒绝、legacy `process_role`/`config_writer`/`config_sync` 转发字段拒绝、模板身份与引用一致性、未知/能力不匹配 sink 拒绝、Kafka 可靠通道强制去重策略、非并发适配器强制 inflight=1、风场身份一致、取值域、线装元数据大小与哈希、摘要内容寻址且与顺序无关。`component/test_config_snapshot.py`（14 节点）钉死 Push 仅暂存不改盘、CAS 过期中止、候选容量/过期、Apply 收敛才推进 runtime、收敛失败保留旧 runtime、revision 冲突不提交、同候选二次 Apply 拒绝。13 RPC 与 proto 三方一致：`common.proto` 9 + `sink_service.proto` 4，真实探针返回 13 rpc_names / 5 protocols / 7 features。

## 2. S2 — 测试体系（FAIL，P1 阻断；sink 侧已整体转绿）

### 2.1 契约层（PASS）

`pytest tests/contracts -q` = **91 passed in 0.17s**。SampleBatch v2（`schema_version=2`、固定主体 `wh.batch.rt.*`/`wh.batch.hist.*`、`MAX_SAMPLES_PER_BATCH=1024`、`MAX_BATCH_BYTES=1MiB`、strict/frozen pydantic、`to_samples()` 物化）由该层钉死，主体按 mode 固定使跨流重叠在类型层面不可达。

### 2.2 Sink Service 套件（PASS，135 节点零 skip/xfail）

`pytest tests/sink_service -q` = **135 passed in 22.96s**。四层级目录 unit（4 文件）/component（5）/integration（3）/system（仅 `__init__.py`），每个文件恰好一个层级 marker 与物理目录一致，专题 marker（contract/reliability/performance/modbus/file）叠加；全树 grep 无 `skip`/`xfail`。上一轮 73 failed/12 errors 所对应的 6 个 v1_1x 双进程/旧 Gate-5 测试，因其依赖的双 Service、`process_role`/`config_writer`、`--peer-service-file` 在 v1_20 已删除、无法原地迁移，收口时直接从仓库删除（非 skip 保留）；覆盖场景由本矩阵与 S3/S4 真实链路 harness 承接。

### 2.3 追溯矩阵（MATRIX.md，与代码同树维护）

- 设计 §14.3 的 35 个目标测试文件：**IMPLEMENTED 6 / PARTIAL 7 / ABSENT 21 / NOT_RUN 1**（现场目标类 1 项）。
- 设计 §14.4 的 42 个 UC（全部规定 system 层级 + 真实进程）：pytest 树无 system 节点，**42 项统一 ABSENT**，在 S3 由 `/tmp/qa_sink/` 真实三进程脚本族 pytest 外重建（矩阵内逐条标注低层可执行证据，不以低层替代计通过）。
- 11 个覆盖领域中，配置生命周期（63 项）、指标（11 项）、实时缓存（8 项）、交付运行时（11 项）低层覆盖充分；IPC、三进程隔离、真实协议适配、Diagnose、system 级 gRPC/mTLS 仍空白或仅 S3 覆盖。

### 2.4 Collector 装配断裂（D-S2-2，P1，OPEN）

`pytest tests/collector -q --continue-on-collection-errors` = **2 failed, 97 passed, 7 errors**。

- 7 errors 全部为 `ImportError: cannot import name 'collector_v2_pb2'` 收集链：`config_v2/`、`rpc/test_collector_contract.py`、`rpc/test_collector_metadata_contract.py`、`test_config_metadata.py`、`test_grpc_task_fencing.py`、`test_metadata.py`、`test_snapshot_config_loader.py`。
- 根因固定：`scripts/generate_rpc.py:25` 将 `collector_v2` 列入 `RETIRED_PROTO_NAMES`，codegen 成功后删除其 `_pb2*.py` 生成物；`src/contracts/` 现仅有 `collector.proto`/`collector_pb2*` 权威模块和 `__pycache__` 内陈旧 pyc。而 `src/collector/infrastructure/grpc/v2_server.py:16-17` 与 `src/web_server/adapter/outbound/grpc/collector.py:14-15` 仍 import 退役名；`src/collector/main.py:20` 与 `infrastructure/grpc/__init__.py:3` 实际装配 v2_server——**测试收集与 Collector gRPC 运行时启动同源断裂**，非纯测试问题。修复方向（迁用 `collector_pb2` 权威契约或正式退役 v2 入口并改装配）交 ChatGPT 裁定，本会话不改生产代码。
- 2 failed 为 `tests/collector/test_modbus_exception_response.py` 两个用例（ConfigError "invalid file_version None"）。该文件在 main 上已由提交 `40f5983f` 删除，本分支分叉点早于该删除而保留旧夹具，属**分支漂移 UNRELATED**，非 sink 重写缺陷，随合 main 消失；不计入阻断。

## 3. S3 — 真实三进程集成 / 系统 / 可靠性（PASS，5 项环境性 NOT_RUN）

栈：R5 真实 docker compose（NATS 47222/monitor 48222、Redis 46379、Kafka 49092、TDengine 46041，四容器 healthy）；S3-08/S4 另起**测试自有的一次性 NATS+Kafka 容器**（唯一命名、finally 删除），原因是共享流 LIMITS 保留策略下消费者 ACK 不删消息，无法保证空流起点。全部脚本/证据在仓库外 `/tmp/qa_sink/`。以下五套均为本会话在 HEAD `66ccdcbb` 上跑出的新鲜证据。

### 3.1 真实三进程冒烟 + Worker SIGKILL 隔离（PASS，exit=0）

证据 `/tmp/qa_sink/evidence.json`（root `run_smoke_head66cc`）。Popen `python -m sink_service.main` + mTLS admin 证书：

- **拓扑**：Main 2587519；Realtime/Reliable worker 2587565/2587566；resource_tracker 2587564 经 /proc cmdline 甄别为非 worker。符合单 Main + 两 Worker 三进程模型。
- **13 RPC 探针**：13 rpc_names、5 protocols、7 features（含 `dlq_puback_before_ack`、`three_process_isolation`）；稳态 5 依赖全 RUNNING；19 指标名（`write_latency_ms_p99` 冷态条件缺省，有写入后出现，组件测试 `test_metrics.py:117` 钉死该诚实语义）。
- **Realtime→Redis**：键 `windhub:farm-a:collector-1:WT001:p000` found，sample_id `s-29-0`、PTTL 57,729 ms、DBSIZE 100（30 风机工作集 + 60s TTL 实证）。
- **Reliable→Kafka + 整批 explicit ACK**：PubAck `WH_HISTORY_QA` seq=3282；独立 aiokafka 消费者实读 2,300 消息；durable `WH_SINK_e15046cb890082bf20d0c205`（确定性命名，sha256(deployment:delivery) 前 24 位）`ack_policy=explicit`、num_pending=0、num_ack_pending=0。
- **SIGKILL 隔离**：kill worker 2587565 后 survivor 2587566 存活、respawned 2588004，**`grpc_unavailable_ticks=0`**（1s tick，控制面零不可用秒）；Main 退出码 0，无残留进程。

### 3.2 Main 重启意图恢复（PASS，exit=0）

证据 `/tmp/qa_sink/restart_evidence.json`，复用同一 config root、同一不可变代 `df8ca873…155f`：重启后重生 2 worker（2588589/2588590），5 依赖全 RUNNING，两 delivery 均 desired=START/actual=RUNNING（期望状态从磁盘恢复）；重启后新流量下 20 指标齐（has_write_success_ratio=true、has_p99=true），exit=0。

### 3.3 S3-07 手动 STOP 不被 Watchdog 复活（PASS，exit=0）

证据任务 `br5jf75n7`（root `s3_07_run_head66cc`）。真实 gRPC StopDelivery（带 expected_revision）→ 轮询确认 STOPPED 后，**14s 内 7 次 GetDelivery 全部 STOPPED，`watchdog_revived_stop=false`**（watchdog 5s 周期，覆盖 ≥2 个完整间隔）；随后 StartDelivery 被接受并恢复 RUNNING。与 `delivery_registry.py:521-527` STOP 分支代码及组件测试 `test_watchdog_never_revives_manual_stop` 三方一致。

### 3.4 S3-08 下游 Kafka 中断积压与恢复重放（PASS，exit=0）

证据 `/tmp/qa_sink/s3_08_run14/s3_08_evidence.json`。测试自有一次性 NATS+Kafka，真实 `docker pause` 故障原语，同一 durable 重放：

- 故障前 1 批 sanity 排空（ack_floor=1）；pause 后发 5 批并保持中断 **65s（跨两个 30s ack_wait 周期）**。
- 中断末态：num_pending=4 + num_ack_pending=1（租约不是 ACK），**outstanding=5、ack_floor 仍为 1 → 中断期间零消息被 ACK**（`backlog_grew=true`、`no_ack_during_outage=true`）。
- unpause 后同一 durable 排空：num_pending=0、num_ack_pending=0、ack_floor=7（`replay_drained=true`）。
- **broker 侧独立确认**：新消费者手动 assign/seek 从 offset 0 全量扫 `wh.history`，total **3,092** 消息、按 `sample_id` 去重 **3,000 unique**（6 批 ×500 点全到达），92 条为 at-least-once 重投重复，单独记录不判缺失（`broker_confirmed_3000_unique_samples=true`）。
- 附证（run8）：夹具 DLQ 一度不满足生产能力契约（max_age 3600 < 要求 86400），`power_history` 直接 FAILED 且**不创建 durable**，报 `DLQ Stream retention/capacity is unsafe`——生产 fail-closed 行为正确（`reliable_runtime.py:224-237`），夹具修正后转绿。

### 3.5 S3-10 实时过期 TTL 与重启不重放（PASS，exit=0）

证据任务 `bnkeod7rn`（root `s3_10_run_head66cc`）。标记设备 WT099 三批实时消息 → Redis 10 个 marker 键、PTTL 57,828 ms；优雅重启整个 Main 后**键集合不变（`restart_replayed_old_points=false`，无旧点重放）**；无新流量下 60s staleness TTL 到期，marker 键全部消失（`ttl_expired_without_new_traffic=true`），重启 Main exit=0。

### 3.6 S3 逐项裁决

| 任务书要求项 | 裁决 | 证据 |
| --- | --- | --- |
| 真实 Main/Realtime/Reliable 三进程 | PASS | §3.1 worker_pids + cmdline 甄别 |
| 真实 NATS Core+JetStream/Redis/Kafka + mTLS gRPC | PASS | PubAck、explicit durable、Redis 键/PTTL、Kafka 独立消费、mTLS 探针 |
| Worker 崩溃隔离另一 Worker、Main 复活、控制面不断 | PASS | SIGKILL 后 survivor 存活/respawned/grpc 0 不可用 |
| Main 重启恢复期望状态 | PASS | §3.2 同代际双 delivery START/RUNNING |
| 可靠数据未确认不得 ACK | PASS | §3.4 中断期 ack_floor 不动 + explicit/`max_ack_pending=1` |
| 下游中断积压、恢复后同 durable 重放不丢点 | PASS | §3.4 四项布尔全 true + 3000 unique broker 确认 |
| 手动 STOP 不被 Watchdog 复活 | PASS | §3.3 14s/7 轮询全 STOPPED |
| 实时 TTL 过期、重启不重放旧点 | PASS | §3.5 |
| 13 RPC 真实 gRPC 可用 | PASS | §3.1 两轮探针 + proto 三方一致 |
| 配置持久化与重启恢复 | PASS | 不可变代 + 同 root 恢复 + 组件/磁盘层 63 测试 |
| S3-01 DLQ PubAck 失败不早 ACK | **NOT_RUN** | 需 NATS 故障代理拒绝 PubAck，本机无代理夹具；禁止 Mock |
| S3-02 UNKNOWN 不谎报成功 | **NOT_RUN** | 需不确定写故障注入代理 |
| S3-03 ACK 丢失 at-least-once 重投 | **NOT_RUN** | 需在线 ACK 丢弃代理（S3-08 实证了同 durable 重放机制，但未在线丢 ACK） |
| S3-04 IPC 超时无双重 Worker 所有权 | **NOT_RUN** | 需 Unix socket 延迟/卡顿夹具；仅有单飞锁+代际关联代码取证 |
| S3-09 DLQ 容量满不静默丢弃 | **NOT_RUN** | 需配额受限 DLQ 流 + 永久失败 sink 夹具；生产 fail-closed 能力契约已由 run8 旁证 |

## 4. S4 — 单实例双通道 600s 性能容量（FAIL，非 NOT_RUN）

### 4.1 方法与诚实性口径

- 拓扑：单 Main + Realtime + Reliable 三进程，测试自有一次性 NATS（JetStream FILE 存储，history 流 20 GiB）+ Kafka 容器，真实 mTLS gRPC；负载由**独立 OS 进程** `s4_publisher.py` 产生，其 CPU 不计入服务。
- 负载形状（设计 §3.1.2）：30 风机 ×10Hz；RT 100 点/批（295 批/s、29.5k 值/s）+ Reliable 500 点/批（295 批/s、147.6k 值/s），每 100ms tick 拆 10 微突发避免tick 边界人为制造覆盖；Reliable 逐批 JetStream PubAck 确认，RT 走 NATS Core + flush。
- 独立确认：服务 GetMetrics 5s 时序（121 点）+ Redis `cmdstat_eval` 增量/DBSIZE + Kafka topic end-offset 与独立消费 + /proc 采样 Main/两 Worker 的 CPU/RSS（5s）+ deadline 受限 gRPC 探针（5s）。
- Publisher 不达标即 NOT_RUN；本次 Publisher 达标，故产出真实 FAIL，不以输入速率替代确认速率。

### 4.2 600s 满载结果（证据 `/tmp/qa_sink/s4_full600/s4_evidence.json`）

| 维度 | 设计门槛 | 实测 | 判定 |
| --- | --- | --- | --- |
| Publisher 供数（offered） | ≥180,000 值/s、≥600 批/s | **177,137 值/s（98.4%）、590.5 批/s、0 发布错误、迟到率 1.04%** | 供数能力成立（≥95% 阈值） |
| sink 确认吞吐（delivered） | ≥180,000 确认值/s | **54,206 值/s（30.1%）**；sink input 53,436 值/s | **不达标** |
| 持续时间 | ≥600s | 600.044s 双通道同时满载 | 达标 |
| Reliable 通道 | ≥150,000 确认值/s | ACK 28,697/177,150 批（16.2%）；broker 14.40M 消息 ≈ **24.0k 值/s**；结束积压 **148,358 批**且线性增长（150s→36,780；300s→74,247；450s→112,268；600s→148,453），净缺口约 247 批/s | **不达标** |
| Realtime 通道 | ≥30,000 确认值/s | Redis EVAL 36,480 次（60.8 批/s）；工作集 DBSIZE 稳定 3,000；dropped 8,450,600（最新值合法覆盖，见下） | 供数时效下确认不足 |
| 硬故障 | failed/DLQ/rejected = 0 | **failed=0、dead_letter=0、rejected=0、retry=0、write_success_ratio 最低 1.0** | 达标 |
| 控制面可用性 | 满载 gRPC ≥0.99 | **121/121 探针成功（1.0），RTT 最大 3.86 ms、P99 3.83 ms** | 达标 |
| 资源 | 记录每进程 CPU/RSS | Reliable worker CPU 均值 89.6%/峰值 94.2%（近单核饱和）；Realtime 均值 61.9%/峰值 84.7%；Main 0.2%；RSS Main 66 MiB、两 Worker 峰值 82–84 MiB，600s 增幅 20–24 MiB 无泄漏式增长 | 已记录 |
| 事件循环延迟 | 设计要求记录 | **生产 20 指标词汇表无该指标，代码 grep 无任何 event-loop lag 埋点**；未伪造，以外部 gRPC 可用性/RTT 与 /proc CPU 替代观察 | 观察性缺口（D-S4-2，P3） |

补充说明：

1. **dropped 8.45M 不是可靠性故障**。`application/realtime.py:149/166/171/177` 定义 dropped 为过期/乱序/缓存容量/未刷盘即被更新值覆盖；实时通道语义是 latest-value 覆盖，设计 §3.1.2 明确「Realtime 的允许覆盖不使不足额的 delivered 自动达标」，故 dropped 合法但 RT 确认速率仍独立不达标。
2. `write_latency_ms_p99` 全程读数 0.0：该指标仅在特定写路径采样，热批路径未贡献延迟样本，测试只钉死「无样本则不出现」的冷态诚实，未覆盖满载下的可观测性——列入 D-S4-2 观察。
3. 30s 校准 run（`s4_calib30s`）已先行证明全链路无夹具性阻塞：同形状下 Publisher 175,915 值/s、sink 62,587 值/s、gRPC 8/8，结论方向与 600s run 一致。
4. 失败性质：**单实例容量不足**。可靠 worker 近单核饱和 + JetStream durable `max_ack_pending=1` 串行确认下，可靠通道确认速率被钉在约 48 批/s；无错误风暴、无 DLQ、无内存泄漏、控制面全程健康。达到 180k 门槛需要生产侧架构/并发优化（或经设计批准的分片方案），属 ChatGPT 侧生产工作，验收不以调参或聚合冒充。

## 5. 缺陷台账（反馈 ChatGPT；Claude 未改生产代码）

| 编号 | 级 | 位置 | 描述 / 最小复现 | 根因 | 状态 |
| --- | --- | --- | --- | --- | --- |
| D-S2-2 | **P1** | `src/collector/infrastructure/grpc/v2_server.py:16-17`（+`main.py:20`、`grpc/__init__.py:3`、`src/web_server/adapter/outbound/grpc/collector.py:14-15`） | `pytest tests/collector -q --continue-on-collection-errors` → 7 文件 ImportError `cannot import name 'collector_v2_pb2'`，含 4 个 Any 子类化 mypy 错误；Collector gRPC 运行时装配同断 | `scripts/generate_rpc.py:25` 已将 `collector_v2` 退役并在 codegen 后删除生成物，消费侧未迁移到权威 `collector_pb2`（或未正式退役 v2 入口） | OPEN |
| D-S4-1 | **P1（容量）** | Sink 可靠通道（`reliable_runtime.py` durable 串行确认 + worker 单核饱和） | 本报告 §4.2：600s 满载确认 54,206/180,000 值/s，可靠积压线性增至 148,358 批 | 单实例确认吞吐约为门槛 30%；近单核 CPU 瓶颈 + `max_ack_pending=1` 串行化，需生产侧优化/分片设计 | OPEN |
| D-S1-1 | P2 | `src/sink_service/application/delivery_registry.py:168,529` | `mypy src/sink_service` 2 errors（Optional 赋值、getattr 第三参类型） | strict 推断噪声为主，运行期有判空/str 契约兜底 | OPEN |
| D-S1-2 | P2 | `src/collector/infrastructure/config/loader.py:66` | str 实参传 `DeviceGroupId` NewType | 缺少显式构造 `DeviceGroupId(identifier)` | OPEN |
| D-S4-2 | P3 | `src/sink_service/application/observability.py` 词汇表 | 设计 §3.1.2 要求记录事件循环延迟，生产无该指标；满载 P99 读数恒 0.0 提示热路径延迟采样缺失 | 可观测性埋点未覆盖设计项 | OPEN |
| D-S3-1 | P3 | 验收环境 | S3-01/02/03/04/09 五项 NOT_RUN | 本机无 NATS 故障代理、在线 ACK 丢弃代理、IPC 卡顿夹具、配额 DLQ 夹具；设计禁止 Mock 冒充 | OPEN（环境） |
| — | UNRELATED | `tests/collector/test_modbus_exception_response.py` | 2 failed（file_version None 夹具漂移） | main 上 `40f5983f` 已删该文件，分支分叉更早而保留；合 main 即消失 | 不计阻断 |

## 6. 剩余风险

1. **容量缺口约 70%**：单实例确认速率 54.2k vs 180k 值/s，且可靠积压随时间线性无界增长（600s 14.8 万批），长稳运行将先撞 JetStream 20GiB 存储上限；当前 discard=NEW 下会转为 PubAck 503，需在容量整改后重跑本 harness 才能关闭 S4。
2. **Collector gRPC 装配断裂是运行时级 P1**：不仅阻断 7 个测试文件，Collector 进程启动 v2 gRPC 服务即 ImportError（pyc 陈旧缓存换环境后必现），建议优先于一切 sink 后续工作处理。
3. **system 层级自动化空白**：42 个 UC 与 21 个 §14.3 目标文件无 pytest 归属，三进程主通路目前仅由仓库外 `/tmp/qa_sink` 脚本守护，无 CI 回归网；脚本本身不入库，人员/环境切换后证据链脆弱。
4. **故障注入覆盖空白**：DLQ PubAck 失败、UNKNOWN 写、在线 ACK 丢失、IPC 超时、DLQ 容量五项关键可靠性语义只有代码取证或旁证，缺运行时实证。
5. **类型安全残余**：9 个 mypy 错误虽多为推断噪声，但 strict 门禁不达标即留隐性 NPE/类型混淆面。
6. **验收证据基于 WIP 工作树**：全部证据针对 HEAD `66ccdcbb` 之上的会话前 WIP 工作树，纯净 HEAD 未复测；收口提交已按所有者指令一次性完成（见 §8），但提交不改变任一缺陷裁决。

## 7. 证据索引（仓库外，本机 `/tmp/qa_sink/`）

| 证据 | 内容 |
| --- | --- |
| `evidence.json`（root `run_smoke_head66cc`） | 三进程冒烟/13 RPC/19 指标/Redis/PubAck/durable explicit/SIGKILL 隔离/exit=0 |
| `restart_evidence.json` | Main 重启恢复：同代际、2 worker、双 delivery START/RUNNING、20 指标/exit=0 |
| `s3_07_run_head66cc`（task br5jf75n7 输出） | STOP 14s/7 轮询零复活 |
| `s3_08_run14/s3_08_evidence.json` | 专用容器 Kafka pause 65s：四布尔全 true、3000 unique/3092 total、exit=0 |
| `s3_10_run_head66cc`（task bnkeod7rn 输出） | TTL 过期 + 重启零重放 |
| `s4_calib30s/s4_evidence.json` | 30s 同形状校准（Publisher 175.9k、sink 62.6k 值/s） |
| `s4_full600/s4_evidence.json` | 600s 满载：121 点 /proc 与 gRPC 采样、全计数器时序、JetStream 积压、FAIL 终判 |
| `s4_performance.py`/`s4_publisher.py`/`s4_infra.py`/`s3_08_dedicated_kafka.py` 等 | 全部 QA 脚本（ruff/py_compile 干净），含 NOT_RUN 字典与 fail-closed 旁证 |
| `report_notes.md` | 本报告组装用中间笔记 |

## 8. 收口提交清单

验收完成后按所有者「失效老旧删除、新的保留、一波 commit」指令，将工作树一次性收口为单个提交（分支 `feat/sink-service-v1-rewrite`，不 push、不建 PR、不合并 main、不 rebase / force push）：

**纳入提交（保留的新资产与 WIP）**

- 测试四层级重组：12 个可执行测试文件落位 unit/component/integration（旧 `test_stage*` 重命名并改写），新增三个层级 `__init__.py` 与空 `system/`（§14.4 UC 预留）；
- `MATRIX.md` 追溯矩阵、`_stage1_fixtures.py` 夹具适配；
- collector/core 配置 5 文件 WIP（ruff 格式归一、`transactions.py` 等价 if/elif 结构整理、v2_server import 排序）；
- 本验收报告。

**删除（失效老旧）**

- 6 个 v1_1x 双进程/Reader-Writer/旧 Gate-5 测试（旧 `test_stage1_dual_process`、`test_stage1_config_reader`、`test_stage3_real_infra`、`test_stage3_reliable_jetstream`、`test_stage3_modbus_tcp`、`test_stage4_performance`）：其依赖的双 Service、`process_role`/`config_writer`、`--peer-service-file` 在 v1_20 已不存在，无法原地迁移，不以 skip/停车目录形式保留；承接关系见 MATRIX §4。

**不纳入提交**

- `.claude/settings.json` 的改动仅为本机 `/tmp/qa_sink` QA 路径的临时 allow 条目与 additionalDirectories，属个人本地环境配置，保留在工作树不入库。

验收期间生产代码零改动；提交中的生产文件 WIP 均为会话前已存在内容，本会话未改写任一生产 hunk。

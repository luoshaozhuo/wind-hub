# Sink Service v1 重写 S1–S4 统一独立验收报告

- 验收日期：2026-11-06
- 交付分支：`feat/sink-service-v1-rewrite`
- 验收绑定 HEAD：`25d50cb3885be5cf8630dc0561fba1b7bc06d80c`（短 SHA `25d50cb3`）
- 设计基线：`docs/Sink_Service_软件设计_v1_20.md`（v1_17 已 `git rm`，v1_20 为当前未跟踪工作区文档）
- 验收人：Claude Code 独立验收
- 约束遵循：未创建 PR、未合并 main、未 rebase / force push；未升级依赖、未降低校验、未新增任何 ignore；未读取/输出 `.env.local` 敏感值。生产代码零改动，本会话仅新增 `/tmp/qa_sink/` 下 QA 脚本与证据（仓库外），报告与 memory 写入不触发 commit。

> 工作区状态提示：本报告运行的所有命令均针对**当前工作树**（HEAD `25d50cb3` 之上存在 21 个未提交修改文件，见 §7）。真实链路证据因此对工作树状态有效；纯净 HEAD 的行为未单独复测。

## 0. 总体结论（先行）

**总体裁决：FAIL。** S1 静态门禁因 mypy strict 27 错误不达标；S2 自动化测试体系与三进程/批量 v2 迁移全面脱节且 Collector 装配断裂（P1）。S3 在真实三进程 + 真实 NATS/JetStream/Redis/Kafka 栈上取得强实证（PASS WITH LIMITATIONS）；S4 性能因无 v1_20 口径可用夹具且供数能力不足，按设计 §3.1.2 记 **NOT_RUN**，不得判 PASS。

| 阶段 | 裁决 | 一句话依据 |
| --- | --- | --- |
| S1 静态 | **FAIL** | ruff check / format、import-linter 全绿；`mypy src/sink_service` = **27 errors / 9 files**，strict 门禁不达标 |
| S2 单元/组件/契约 | **FAIL** | contracts 91 PASS；sink 套件 **73 failed / 55 passed / 6 skipped / 12 errors**；Collector 全树 ImportError（P1）；批量契约 v2 零自动化测试 |
| S3 集成/系统/可靠性 | **PASS WITH LIMITATIONS** | 真实三进程冒烟 run6 + Main 重启恢复两套脚本 exit=0；Worker SIGKILL 隔离/复活、整批 explicit ACK、Redis 实时值、状态机、13 RPC 探针均真实闭环；3 项仅代码取证 |
| S4 性能（600s 满载） | **NOT_RUN** | 唯一 stage4 用例无条件 skip 且引用已删除的 `--peer-service-file`、断言为旧 Gate5 口径；无 v1_20（30 风机/30k+150k）夹具；历史单发布器上限 ~93k msg/s < 180k 供数要求 |

NOT_RUN / SKIP / FAIL 项均不计通过；本报告未使用任何 Mock 替代真实进程、真实 gRPC 或真实消息。

## 1. S1 — 静态门禁

实际执行命令与结果（针对工作树）：

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| Ruff Lint | `ruff check src/sink_service src/collector src/contracts` | **PASS** — All checks passed! |
| Ruff Format | `ruff format --check src/sink_service src/collector src/contracts` | **PASS** — 60 files already formatted |
| Import Linter | `lint-imports` | **PASS** — 13 kept, 0 broken（含 “sink service three-layer architecture”“sink service does not import other process packages”） |
| Mypy strict | `mypy src/sink_service` | **FAIL** — Found 27 errors in 9 files (checked 25 source files) |

### 1.1 mypy 27 错误分布（缺陷 D-S1-1，P2，反馈 ChatGPT，未自行修改）

- `reliable_runtime.py` 8 项：Optional 收窄缺失（`list[str] | None` 迭代/`in`、`int >= None`、`None * float`，行 209/218/219/223/229/327/496/498）。集中在重试/退避与主题名推导，属类型 narrowing 缺陷，运行期由配置非空约束兜底，但 strict 下不成立。
- `realtime_adapters.py` 5 项：`StreamWriter | None` 未判空即 `write/drain`（107/108）、元组类型不兼容（104）、pymodbus `async_setValues` 覆写签名不兼容（505）、调 untyped `shutdown`（546）。
- `realtime_runtime.py` 3 项：输出句柄变量被 `KafkaOutput`/`ModbusOutput` 覆写为声明 `RedisOutput`（272/300）、`no-any-return`（241）。
- `config_repository.py` 3 项：`str` 变量被赋 `Path` 后访问 `.parent/.open`（110-112），提示存在 str/Path 混用。
- `application/config.py` 2 项：`tuple[object,...]` 误调 `.get`（325/326）。
- `delivery_registry.py` 2 项：`_Entry | None` 赋给 `_Entry`（165）、`getattr` 第三参类型不符（524）。
- `main.py` 2 项：循环变量 `str` 被赋 `tuple[str,str]` 及后续非重叠比较（156/157）。
- `supervisor.py` 1 项：`SpawnProcess` 赋给 `Process` 声明（92）。
- `grpc_v1.py` 1 项：`no-any-return`（76）。

处理：按任务书“接口/并发/可靠性缺陷只记录不自行修改、不得加 ignore”，全部保留并台账化，未插入任何 `# type: ignore`。

## 2. S2 — 单元 / 组件 / 契约测试

### 2.1 契约层（PASS）

- `pytest tests/contracts -q` = **91 passed in 0.15s**。批量契约 v2（`SampleBatch` `schema_version=2`、`wh.batch.rt`/`wh.batch.hist`、`MAX_SAMPLES_PER_BATCH=1024`、`MAX_BATCH_BYTES=1MiB`、strict pydantic、`to_samples()` 物化）由该层钉死。

### 2.2 Sink Service 套件（FAIL）

- `pytest tests/sink_service -q --continue-on-collection-errors` = **73 failed, 55 passed, 6 skipped, 12 errors in 53.11s**。
- 收集期即报错文件：`test_stage1_config_reader.py`（error，中断全树收集，须 `--continue-on-collection-errors` 才能跑完）。
- 分组画像：config_validation 28、config_manager 14、delivery_registry 10、reliable_seams 8、config_repository 8、grpc_boundary 7 ERROR、perf_seams 4、real_infra 3 ERROR、dual_process 1 ERROR、config_reader 1 ERROR、observability 1。
- 缺陷 D-S2-1（**P1**）：测试体系与三进程（Main/Realtime/Reliable）+ 批量 v2 + 单 Service 配置闭包的生产迁移全面脱节，旧双进程/旧 peer 文件/旧配置布局假设遍布，导致 73 失败 + 12 错误。v1_20 §14.3 要求的 31 个目标测试文件在树中不存在对应实现；批量契约 v2 在 sink 侧**没有任何自动化用例**覆盖发布→路由→ACK 全链路。

### 2.3 Collector 装配断裂（D-S2-2，P1）

- `pytest tests/collector --collect-only` 全树 ImportError：
  `src/collector/assembly.py:22` → `from core.infrastructure.config import CollectorDocumentLoader, ConfigPaths`，而 `core.infrastructure.config` 仅导出 Shared* API（`shared_documents.py` 等），无 `CollectorDocumentLoader/ConfigPaths/collector_documents`。
- 同源断裂还见于 `infrastructure/config/loader.py:11-16`、`grpc/v2_server.py:19`、`transactions.py`。**Collector 测试一棵树都无法收集**，属装配级回归，反馈 ChatGPT。
- 反向核对：`src/collector/application/nats_dispatch.py` 的 `SamplePublisher` 已全面切到批量 v2（有界队列 2048、按设备分组、`MAX_SAMPLES_PER_BATCH` 分块），可独立导入——发布接口本身与契约一致，断裂集中在装配/加载层。

### 2.4 生产行为的代码取证（无自动化覆盖，人工只读核查）

- 13 RPC = 9 CommonService + 4 SinkService，`grpc_v1.GetCapabilities` 硬编码 13 `rpc_names`/5 protocols/7 features（含 `dlq_puback_before_ack`、`three_process_isolation`）；`grpc_auth.py` 权限矩阵对未授权返回 PERMISSION_DENIED；`GetConfigSnapshot` 三 scope 外 INVALID_ARGUMENT。
- IPC（`ipc.py`）：Unix socket + `SO_PEERCRED` PID/UID 校验、BOOT 握手、uuid `request_id` 关联、单飞锁；超时/错关联即置 `_broken=True` 并 close，不复用旧应答。
- Reliable（`reliable_runtime.py`）：`terminal=False` 绝不 `ack_sync`；`_dead_letter` 要求 `ack.stream == DLQ stream`（404-412），DLQ 全 part 取得 PubAck 后才 ACK 源消息，异常走 NAK + 指数退避；durable `max_ack_pending=1` + EXPLICIT。
- Watchdog（`delivery_registry.py:516-522`）：`reconcile_all` 对 `desired_state=="STOP"` 仅收敛 STOPPED，不走 START 复活路径。
- 配置（`config_repository.py`）：不可变代 `.generations/<64hex>` + stage fsync + `os.rename` + CURRENT 原子 `os.replace`；`config_manager.set_delivery_desired` 先验 disk revision == expected_revision，否则 RevisionConflictError；模板跨代字节不可变；`writer_lease.py` fcntl  flock 写者互斥。
- 指标（`worker_registry.py:131-185`）：任一 worker 非 APPLIED/代际不符即 `collect_metrics` 返回 None，不造数。

## 3. S3 — 真实三进程集成 / 系统 / 可靠性（PASS WITH LIMITATIONS）

栈：docker project `wind-hub-r5-manual`，NATS 47222 / monitor 48222、Redis 46379、Kafka 49092、TDengine REST 46041，四容器 healthy。QA 独立 stream `WH_HISTORY_QA`（subjects `wh.batch.hist.>`）/`WH_DLQ_QA` 只建不删，与生产隔离。脚本与证据位于仓库外 `/tmp/qa_sink/`。

### 3.1 真实三进程冒烟（run6，`evidence.json`，exit=0）

Popen `python -m sink_service.main --config-root <root>`，mTLS admin 证书 + `ssl_target_name_override=localhost`。

- **拓扑**：main PID 2160949；worker 2160989 / 2160997（另含 `multiprocessing.resource_tracker` 2160988，经 /proc cmdline 甄别为非 worker 第三子进程）。符合单容器 Main + Realtime + Reliable 三进程模型。
- **13 RPC 真实探针**：启动期与 6s 稳态各一轮，`rpc_names` 13 个齐、5 protocols、7 features；`GetConfigSnapshot(scope=DISK)` 返回 7 文件快照；`Diagnose(target_id=...)` 6 stages / overall PARTIAL；稳态 5 依赖全 RUNNING。
- **指标**：稳态 18 个指标名（含 R04 的 received/delivered/failed/dropped/dead_letter/rate/success_ratio/P99/backlog/io_inflight/covered_ms 及批维度 input/received/acknowledged/rejected/routed/retry/dead_letter_invalid）。
- **状态机**：`ems_power` STOPPED→RUNNING 经 `_wait_state` 轮询观察到；Stop/Start 携带 `expected_revision` 链（先 GetDelivery 取 revision，START 用 stop_result.revision），无 revision 时返回 ABORTED "configuration revision conflict"（run4 实证契约生效）。
- **Realtime→Redis**：键 `windhub:farm-a:collector-1:WT001:p000` found，`sample_id=s-29-0`、`value=29.0`、PTTL≈57.68s、dbsize=100（30 批×100 点最新值派发 + TTL 实证）。
- **Reliable→Kafka + 整批 ACK**：PubAck `WH_HISTORY_QA` seq=3；aiokafka 实消费 `wh.history` 1533 消息（含历次累积），首键 `farm-a:collector-1:WT001`；durable `WH_SINK_e15046cb890082bf20d0c205`，`num_pending=0`、`num_ack_pending=0`、`num_delivered=3`、`ack_policy=explicit` —— 整批 ACK 边界与零在租消息真实成立。
- **Worker 崩溃隔离/复活**：SIGKILL victim 2160989，survivors（tracker + 另一 worker 2160997）存活，respawned 2161373；**`grpc_unavailable_ticks=0`**（崩溃到复活期间 Main gRPC 零不可用秒，1 秒 tick）。
- 退出：SIGTERM 后 `main_exit_code=0`，无残留服务进程。

### 3.2 Main 重启恢复（`restart_evidence.json`，exit=0）

复用同一 config root，从 CURRENT→`.generations/<rev>/configs/services/sink_service.yaml` 读回端口后重新 Popen Main：

- 重启后重生 2 worker（2164231/2164232）；5 依赖全 RUNNING；两 delivery `desired=START / actual=RUNNING`（期望状态从磁盘恢复）。
- 重启后发 5 新批，`GetMetrics` 返回 20 指标且条件指标 `write_success_ratio`、`write_latency_ms_p99` 出现（has_write_success_ratio=true、has_p99=true）；`restart_exit_code=0`。

### 3.3 S3 项逐条裁决

| 任务书要求项 | 裁决 | 证据 |
| --- | --- | --- |
| 真实 Main/Realtime/Reliable 三进程 | PASS | run6 worker_pids + cmdline 甄别 |
| 真实 NATS/JetStream/Redis/Kafka | PASS | PubAck、durable explicit、Redis 键、Kafka 消费 |
| 任一 Worker 崩溃另一不受影响 | PASS | SIGKILL 后 survivor 存活、respawned、grpc 0 不可用 |
| Main 重启恢复期望状态 | PASS | restart 两 delivery desired/actual 一致 |
| 可靠数据未确认不得 ACK | PASS | explicit + num_ack_pending=0 + 代码 `terminal=False` 不 ack |
| 手动 STOP 不被 Watchdog 复活 | PASS（代码+状态机） | reconcile_all STOP 分支 + run6 STOPPED 观察；未做长窗口复活探针 |
| 13 RPC 真实 gRPC 可用 | PASS | run6 两轮探针 13 rpc_names |
| 配置持久化与重启恢复 | PASS | 不可变代 + restart 同 root 恢复 |
| DLQ 未取得 PubAck 不得 ACK | **LIMITATION** | 仅代码取证（`_dead_letter` stream 校验 404-412），未做 DLQ PubAck 失败的运行时故障注入 |
| IPC 超时不造成重复 Worker 所有权 | **LIMITATION** | 仅代码取证（单飞锁 + request_id/generation 关联 + broken 即 close），未做 socket 延迟故障注入 |
| 满载时 Main gRPC 可用 | **NOT_RUN** | 依赖 S4 满载场景，S4 整体 NOT_RUN；run6 仅在崩溃场景证 grpc 0 不可用 |

## 4. S4 — 单容器三进程性能（NOT_RUN）

v1_20 目标：30 风机 ×10Hz；Realtime 100 点/台/批 ≥30,000 确认值/s；Reliable 500 点/台/批 ≥150,000 确认值/s；连续 ≥600s；记录 offered/received/accepted/delivered/ACK/DLQ/丢弃/重投/P99/CPU/RSS/事件循环延迟/gRPC 可用性。

- 树中唯一性能用例 `tests/sink_service/test_stage4_performance.py`：
  - `--collect-only` = 1 test；实跑 **1 skipped**，跳过原因 `Gate-5 runs against an external pinned stack (SINK_R5_*)`（行 404），无外部固定栈即无条件 skip。
  - 即便供栈，其 `_popen` 仍传**已删除的** `--peer-service-file`（行 197），且 full_scale 断言为旧口径 `SINKS >= 20 ... SECONDS >= 600 / ≥100k 值/s`，与 v1_20 的 30 风机 / RT 30k + Reliable 150k 口径不符。
- 结论：**不存在可用于 v1_20 S4 的夹具**，无法产出 600s 单实例确认吞吐。按 v1_20 §3.1.2「独立 Publisher 须 ≥600 批/s、≥180k 值/s；供数不足记 NOT_RUN，不得判 PASS」及 local-qualification「环境不足 NOT_EXECUTED，不得 fallback mock」，本阶段记 **NOT_RUN**。
- 供数能力旁证（不作为 PASS 依据）：历史实测单 Python 发布进程上限约 93k msg/s，低于 180k 值/s 供数要求；在构建出 ≥180k 的独立 Publisher 与 v1_20 口径性能夹具前，S4 不具备运行条件。严禁以输入吞吐替代确认吞吐、严禁多容器相加充当单实例。
- 关联：任务书「满载时 Main gRPC 可用性」随 S4 一并 NOT_RUN。

## 5. 缺陷台账（反馈 ChatGPT，Claude 未自行改生产代码）

| 编号 | 级 | 模块 | 描述 | 状态 |
| --- | --- | --- | --- | --- |
| D-S2-2 | P1 | collector/assembly 等 | `core.infrastructure.config` 无 `CollectorDocumentLoader/ConfigPaths`，Collector 全树 ImportError，装配断裂 | OPEN |
| D-S2-1 | P1 | tests/sink_service | 73 failed/12 errors，与三进程+批量 v2+单 Service 配置迁移脱节；§14.3 的 31 目标测试文件缺失；批量 v2 零 sink 自动化覆盖 | OPEN |
| D-S1-1 | P2 | sink_service（9 文件） | mypy strict 27 错误，集中 Optional narrowing、str/Path 混用、输出句柄类型覆写 | OPEN |
| D-S4-1 | P2 | test_stage4_performance | 引用已删 `--peer-service-file`、旧 Gate5 口径、无条件 skip；无 v1_20 性能夹具 | OPEN |
| D-S3-1 | P3 | 可靠性验证 | DLQ PubAck 失败、IPC 超时重复所有权两项仅有代码取证，缺故障注入运行时实证 | OPEN |
| D-S3-2 | P3 | 可靠性验证 | 满载 gRPC 可用性随 S4 NOT_RUN；STOP 不复活缺长窗口探针 | OPEN |

## 6. 剩余风险

1. **测试护栏失效**：sink 73 失败/12 错误 + collector 无法收集，意味着三进程重写的主通路当前仅靠 QA 临时脚本（/tmp，仓库外）守护，回归无 CI 网。建议优先恢复 D-S2-1/D-S2-2。
2. **类型安全债**：mypy 27 错误中 `reliable_runtime` 的 Optional  narrowing 与重试/退避数值路径相关，虽运行期由配置非空兜底，strict 不达标即留隐性 NPE 面。
3. **性能未知**：单实例 30k/150k 确认吞吐与 600s 长稳完全没有数据；历史单发布器 93k 上限提示供数侧可能先于 sink 成为瓶颈。
4. **故障注入覆盖空白**：DLQ PubAck 失败、IPC 超时/错关联、STOP 长窗口三项关键可靠性语义缺运行时实证，仅有静态代码逻辑支撑。
5. **工作树未提交**：证据针对脏工作树，纯净 HEAD `25d50cb3` 未复测；合入前须先处置 §7 的 21 个未提交文件。

## 7. 工作区修改清单（未提交，验收未 commit）

`git status --porcelain`：21 个修改文件（含 `src/sink_service/**` 16 个生产文件、`src/collector/application/nats_dispatch.py`、`src/contracts/sample_batch.py`、`scripts/sink_dlq_replay.py`、`tests/tooling/test_merge_skill.py`），`git rm` 旧设计 v1_17，新增未跟踪 `docs/Sink_Service_软件设计_v1_20.md`。`git diff --stat` 合计 **748 insertions / 4036 deletions**。

- 这些修改在本会话开始前已存在于工作树；本会话未改任何生产文件，仅复跑四门与测试取证。
- 四门复跑结果（ruff 双绿 / import-linter 绿 / mypy 27）即针对该工作树；删除量较大，**不对每一 hunk 的纯格式性质做背书**，建议所有者逐文件审查后再决定提交或回退。
- 本报告与新增 memory 不创建 commit、不建 PR、不合并、不 rebase / force push。

## 8. 证据索引（仓库外）

- `/tmp/qa_sink/smoke_three_process.py` — 三进程真实冒烟（6 轮迭代修复后 run6 通过）。
- `/tmp/qa_sink/restart_recovery.py` — Main 重启恢复。
- `/tmp/qa_sink/evidence.json` — run6 全量证据（拓扑/13 RPC/18 指标/Redis/PubAck/durable explicit/SIGKILL 隔离/exit=0）。
- `/tmp/qa_sink/restart_evidence.json` — 重启证据（2 worker/全 RUNNING/20 指标含 ratio+P99/exit=0）。
- `/tmp/qa_sink/run_smoke.sh` 与 `run{2,4,5}_stdout.log` — 包装器与迭代失败轨迹（INVALID_ARGUMENT scope、ABORTED revision、SequenceInfo 字段差异等均留痕）。

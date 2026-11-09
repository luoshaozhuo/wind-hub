# Core v1_27 / Contracts v1_6 基础契约集中独立验收报告

- 验收对象：Core v1_27（ADS 原生订阅时间戳回退与通知丢失可观测性）、Contracts v1_6（线 ID 与 NATS 头校验语义、SampleBatch v2 严格线协议、scalar_codec 唯一归属）及其对 Collector / Sink / Gateway / Data Generator / CLI 的调用方影响
- 验收人：独立测试 / 静态检查 / 代码审查（Claude Code）；生产代码由 ChatGPT 负责，本验收未改任何生产业务逻辑
- 验收日期：2026-11-09
- 总体裁决：**PASS WITH LIMITATIONS**（S1 PASS、S2/S3/S4 见第 10 节分项裁决；限制项均为环境缺失或设计书已声明的交付边界，非缺陷）

## 1. Git HEAD、基线、变更文件及工作区状态

### 1.1 Git 身份

| 项 | 值 |
| --- | --- |
| 分支 | `docs/foundation-core-contracts-20261108` |
| HEAD | `41d366624ad47e295971ef3da5f68125fd8fbe0c` |
| 基线 | `origin/main`（独立 worktree `/tmp/wh-main-verify` 用于 UNRELATED 复现取证） |
| 工作区统计 | 26 modified（10 生产 + 16 测试/脚本）、26 deleted、1 rename+delete（RD）、5 untracked（tests/src 新目录） |

### 1.2 生产文件变更（10 个，本验收仅做 lint/格式修复，未改业务逻辑）

| 文件 | 变更性质 |
| --- | --- |
| `src/contracts/sample_batch.py` | SampleBatch v2 严格模型；本验收仅 SIM102 合并 + ruff format，git diff 确认无行为变化 |
| `src/contracts/scalar_codec.py` | 未改；encode/decode 双侧拒绝 NaN/Infinity（`math.isfinite` 守卫） |
| `src/core/infrastructure/protocol/ads/subscription.py` | 原生通知 trigger/时间戳回退/丢弃与回调计数；本验收仅 format |
| `src/collector/infrastructure/publisher.py` | v2 批次发布、背压/TTL/重试身份保持；本验收仅 format |
| `src/collector/infrastructure/grpc/collector_service.py` | ReadPoints 只走 submit_read，不触发 NATS 发布（只读审查确认） |
| `src/collector/infrastructure/config/validation.py` | v2 messaging 字段校验 |
| `src/sink_service/application/runtime.py` | write_batch 实时去重/权威裁决/可靠直通（本次补 10 个直接组件测试） |
| `src/sink_service/infrastructure/nats_consumer.py` | ACK/NAK/DLQ 责任边界；本验收仅 format |
| `src/sink_service/infrastructure/adapters/grpc_sink.py`、`nats_init.py` | subject 段 `.batch.` 与流装配适配 |
| `src/industrial_gateway/infrastructure/grpc/data_server.py` | 调用方导入路径适配 |

### 1.3 删除清单（用户明确授权「授权删除并补 v2 测试」）

v1 线协议链整体移除，共 26 路径 + 1 迁移：

- `tests/system/r5/`（8 测试 + conftest + r5_config，11 文件）
- `tests/system/soak/`（2 文件）、`tests/support/soak/`（4 文件）、`tests/tooling/test_soak_duration_override.py`
- `tests/support/new_collector.py`、`tests/support/perf_runner.py`、`scripts/run_benchmark.py`
- `tests/collector/unit/test_publisher_v1.py`、`tests/sink_service/test_nats_consumer_reliable.py`
- `tests/fixtures/services/r5_stack.py`、`tests/fixtures/services/docker-compose.r5.yml`
- 迁移：`tests/core/unit/rpc/test_scalar_codec.py` → `tests/src/unit/contracts/test_scalar_codec.py`（git rename，断言更新到 v1_6 语义）

`scripts/ci_gate.py` 的 soak/performance gate 路径同步改向 `tests/src/reliability` 与 `tests/src/performance`。

## 2. 设计符合性矩阵

| 设计要求（v1_27 / v1_6 / Collector v1_11 / Sink 设计） | 实现位置 | 验证手段 | 结论 |
| --- | --- | --- | --- |
| scalar_codec 唯一归属 `contracts.scalar_codec`，core rpc 包整体删除 | `src/contracts/scalar_codec.py`；`src/core/infrastructure/rpc/` 无 .py 文件 | grep 无 `core.infrastructure.rpc` 导入（仅架构测试禁用清单常量）；架构守护断言 `rpc == set()` | 符合 |
| 编解码双侧拒绝 NaN/Infinity，64 位整数边界、oneof、None | scalar_codec.py:30-31 及 decode 对称守卫 | 6 单测含直接构造 wire 消息的 decode 拒绝 | 符合 |
| SampleBatch v2：严格 schema_version=2、frozen/strict、extra forbid、canonical JSON、重复键拒绝、subject 匹配、容量限制 | sample_batch.py | 65 收集用例完整矩阵（见 5.1） | 符合 |
| Realtime（Core NATS `wh.rt.batch.`）/ Reliable（JetStream `wh.hist.batch.`）隔离，PubAck 与 Sink 确认分离 | publisher.py / nats_consumer.py / nats_init.py | 组件 18 测 + 真实 NATS 集成 3 测 | 符合 |
| Reliable 重试保持 batch_id 身份（Nats-Msg-Id 去重） | publisher.py model_copy 仅更新 publish_time | publisher 组件测 + 真实 JetStream 去重集成测 | 符合 |
| DLQ：解码失败→DLQ→ack；DLQ 不可用→nak(30)；handler 失败→指数退避 nak（cap 60s） | nats_consumer.py | 8 组件测逐条断言 | 符合 |
| ADS 触发模式 cyclic/on_change 映射 ADSTRANS_SERVERCYCLE(3)/SERVERONCHA(4)，热更新同模式稳定 | subscription.py `_notification_attrib` | 8 组件测（FakeAdsSession 记录注册/删除句柄） | 符合 |
| 设备时间回退：naive→本地时区、非法时钟 OverflowError→接收端 UTC、aware→转 UTC | subscription.py `_notification_callback` | 3 个时间戳组件测 | 符合 |
| 队列满丢弃最旧批次且 dropped_notifications 计数（2 的幂告警）；回调异常计数且 worker 存活；generation 代次隔离 | subscription.py `_enqueue`/`_consume` | 溢出、回调失败、stale generation 组件测 | 符合 |
| Realtime Sink：staleness 过滤、单 point 权威冲突拒绝、等时歧义拒绝、等时同 id 幂等去重、旧样本跳过 | runtime.py:170-226 `write_batch` | 本次新增 10 个直接组件测 | 符合 |
| 实时背压丢弃、TTL 过期计数、enqueue 超时 RuntimeError、超限拆分、stop 释放连接 | publisher.py | 10 组件测 | 符合 |
| WRITE_MANY 能力诚实性 | Modbus（无）、IEC104（无）、ADS（原生声明） | 能力清单只读审查 | 符合，不虚报 |
| Reliable 内存队列不承诺崩溃恢复 | 无 WAL/磁盘持久化 | 独立审查（见 8.2） | 与设计一致（已声明限制） |

## 3. S1 静态检查（命令与原始结果）

全部在 wind-hub conda 环境（Python 3.14.7、pydantic 2.13.5、nats-py、pyads 3.6.0）实跑：

| 命令 | 范围 | 原始结果 | 裁决 |
| --- | --- | --- | --- |
| `ruff check` | src/contracts、src/core、src/collector、src/sink_service、src/industrial_gateway、src/data_generator、src/cli | `All checks passed!` | PASS |
| `ruff format --check` | src、tests/src、scripts/ci_gate.py | `346 files already formatted` | PASS |
| `mypy src` | 全部生产源码 | `Success: no issues found in 329 source files` | PASS |
| `lint-imports` | 15 个 import 契约 | `Contracts: 15 kept, 0 broken.`（含「configuration 只依赖公共基础库」KEPT） | PASS |
| 残留 grep | `sample_message` 导入 / `core.infrastructure.rpc.scalar_codec` 导入 / rpc 包 .py | 均为零命中 | PASS |
| 架构守护 | `tests/core/architecture/` | crosscutting 全过；唯一失败 `test_rpc_boundary::test_pb2_only_in_allowed_zones` 在 origin/main 同样失败（UNRELATED，见 8.1） | PASS（带 UNRELATED 记录） |

测试侧 mypy（非 CI 门禁，本验收自加严格度）：新增 9 个测试文件以 `MYPYPATH=src mypy` 逐文件检查，0 错误；未使用任何 `type: ignore` 掩盖（原有 consumer 测试中的 2 处 ignore 为访问私有成员的既有惯例）。

## 4. S2 Core 测试结果

| 套件 | 命令 | 结果 |
| --- | --- | --- |
| ADS v1_27 组件（FakeAdsSession，mock_service） | `pytest tests/src/component/test_ads/` | 8 passed |
| Core ADS 协议（含 fault_injection，trigger 入参适配） | `pytest tests/core/protocol/ads/` | 与上合计 59 passed |
| tests/src 全量（单元+组件） | `pytest tests/src` | **159 passed** |
| Collector 单元（含 v2 配置校验 19 用例） | `pytest tests/collector/unit` | 39 passed |

覆盖点：trigger 映射与热更新句柄稳定、naive/aware/非法设备时钟三类回退、有界队列溢出丢弃计数、回调异常后 worker 存活、stale generation 忽略、v2 messaging 限制字段正反向校验与未知 key 拒绝。

**NOT_RUN**：`test_uc03_s01_subscribe_requires_ads`（hardware marker）需真实 TwinCAT PLC / ADS Router，WSL2 无环境，按任务书要求保留 skip 文案 `NOT_RUN: no ADS/TwinCAT environment`，未以 Mock 冒充硬件链路。

## 5. S3 Contracts 测试结果

### 5.1 SampleBatch v2 矩阵（tests/src/unit/test_contracts/，71 收集用例）

- schema_version 严格性（缺省/1/字符串 2 均拒绝，必须显式整数 2）
- Realtime/Reliable 模式隔离（realtime 必须带 max_staleness_ms、reliable 禁止该字段）
- Subject token 安全（非法字符、含点号 point_id、层级注入）
- 显式 null、naive 时间戳拒绝、UTC 归一、重复 JSON 键拒绝、extra forbid、frozen 不可变
- 重复 point_id/sample_id 拒绝、空批拒绝、max_samples/max_bytes 容量边界、回环一致
- 标量：None/False/0/空串/bytes、±2^63、2^64-1、越界拒绝、bool 不按 int 收纳、NaN/Infinity 双侧拒绝、unset oneof→None
- 大批量实测内存占用（真实构造，非 Mock 计数）

### 5.2 真实 Collector→Contracts→NATS/JetStream→Sink 链路

`tests/src/integration/test_batch_nats_real_link.py`（3 测，marker: integration/real_service/docker/end_to_end）：

- 真实 `nats:latest -js` 容器（随机端口、用例自管生命周期、结束自动 rm，复验无残留）
- 10Hz 三周期三条独立消息；PubAck + Durable + DLQ 真实路由；Nats-Msg-Id 真实去重
- 本轮复跑：**3 passed**；同轮 reliability+performance 合计 **8 passed**

Collector 系统测试（真实 Modbus/IEC104 slave + 独立 Collector 进程 + 真实 NATS）：schema_version=2 断言与 `point_docs()` 摊平 helper 更新后，`test_uc03_s01_poll_publishes_complete_envelope`、`..._listen_receives_spontaneous_iec104`、`test_uc07_s01_all_20_rpcs_real_invocation` 本轮实跑 **3 passed**（15.16s）。

## 6. S4 可靠性 / 性能结果

| 项 | 实测方式 | 结果 |
| --- | --- | --- |
| 30 风机负载计数闭合 | `tests/src/reliability/test_batch_pipeline_load.py`，30 设备真实批次对象、realtime（max_staleness_ms=30000 隔离 TTL 语义）与 reliable 双通道 | 2 passed，零静默丢失，任务释放无残留 |
| 背压 / TTL / 队列满 | publisher 组件测真实 asyncio.Queue：realtime 背压丢弃、reliable enqueue 超时 RuntimeError、TTL 过期 expired/dropped | 10 passed |
| DLQ 与重投义务 | consumer 组件测：DLQ→ack、DLQ 故障→nak(30)、handler 失败退避 2^min(attempts-1,6) | 8 passed |
| 资源释放 | publisher stop 关闭连接、SinkRuntime stop 持 write_lock 排空、集成容器 rm | 组件+集成实测通过 |
| 编解码吞吐 | `tests/src/performance/test_batch_throughput.py` 实测 p50/p95/p99（3 用例） | 建立 baseline 数据，**不虚构 SLA**；marker performance/load，与 soak 门禁分离 |

限制：本轮未在多进程完整栈上做 10Hz 持续长稳（原 r5/soak 链已随 v1 删除，新可靠性测试覆盖进程内队列与真实 NATS 链路两段）；长稳仍是独立可选流程。

## 7. ST/UC 与 Marker 覆盖矩阵

| 新增/修改文件 | 用例数（收集口径） | Marker |
| --- | --- | --- |
| tests/src/unit/test_contracts/test_sample_batch.py | 65 | unit, contract, invalid_input |
| tests/src/unit/test_contracts/test_scalar_codec.py | 6 | unit, contract（git rename 迁入） |
| tests/src/component/test_ads/test_subscription_v1_27.py | 8 | component, ads, mock_service, lifecycle |
| tests/src/component/test_collector/test_publisher_v2.py | 10 | component, mock_service, overload |
| tests/src/component/test_sink_service/test_nats_consumer_v2.py | 8 | component, mock_service, reliability |
| tests/src/component/test_sink_service/test_sink_runtime_v2.py | 10 | component, mock_service |
| tests/src/integration/test_batch_nats_real_link.py | 3 | integration, real_service, docker, end_to_end |
| tests/src/performance/test_batch_throughput.py | 3 | performance, load |
| tests/src/reliability/test_batch_pipeline_load.py | 2 | reliability, soak, load |
| **新增合计** | **115 收集用例** | 全部注册 marker，conftest 路径自动补标 |
| tests/collector/unit/test_config_service_validation.py | 扩展至 19（新增 v2 字段 2 函数 + 4 参数行 + 未知 key 拒绝） | unit |
| 3 个 collector 系统测试 + nats_verify helper | schema v2 适配 | system, real_service, docker |

UC 追溯：UC-03（采集与发布，含 realtime/reliable/listen/中断恢复）、UC-07（20 RPC 全量真实调用）由系统测试承载；ST 层 v2 线协议、标量、ADS 语义由 tests/src 新增套件承载。

## 8. 失败/缺陷清单与复现证据

### 8.1 全量回归（CI marker 口径，1194s）

命令：`pytest tests -q -m "not hardware and not performance and not soak"`，原始统计：
**15 failed, 1996 passed, 39 skipped, 45 deselected, 21 errors**。

| 失败项 | 数量 | 分类 | 证据 |
| --- | --- | --- | --- |
| tests/system/test_web_fleet_config_e2e.py | 21 errors | UNRELATED | 子进程 gRPC/mTLS PKI 与 WSL2 环境限制，main worktree 同现 |
| tests/system/e2e/test_command_via_server.py | 4 | UNRELATED | main worktree 同现（commander 子进程环境） |
| test_command_outcomes / test_lease_system / test_server_config_apply | 3 | UNRELATED | main worktree 同现（SQLite 时序/系统环境） |
| tests/cli（systemd dbus ×2、tcp_probe ×1） | 3 | UNRELATED | WSL2 无 systemd/dbus、TCP 探针环境 |
| test_rpc_boundary::test_pb2_only_in_allowed_zones | 1 | UNRELATED | **本轮在 /tmp/wh-main-verify 复现 FAIL**，offenders 为既有 collector 等文件 pb2 引用，与本次变更无关 |
| test_crosscutting_boundary::infrastructure_directories | 1 | RELATED（测试断言过时，已修复） | v1_6 删除整个 rpc 包（含 `__init__.py`），旧断言 `{"__init__.py"}` 改为 `set()` 并加注释；修复后 2 passed |
| collector 系统测试 ×3 | 3 | RELATED（回归任务启动于修复前，跑的是陈旧代码） | 日志实证 `assert message["schema_version"] == 1`（磁盘当前为 `== 2`）；当前代码复跑 3 passed |

RELATED 净残留：**0**。无 skip/xfail/noqa/type-ignore 掩盖手段；无弱化断言；无生产逻辑改动。

### 8.2 独立审查项：Reliable 崩溃恢复

publisher 无 WAL/磁盘持久化，进程崩溃时内存中未确认批次不可恢复。Collector 设计书 v1_11 明确声明「当前内存可靠队列不能保证崩溃后未确认批次恢复，不得宣称端到端无损」。**实现与设计一致，属已声明交付限制，非缺陷**；任何对外材料不得宣称端到端无损。ChatGPT 后续若引入持久化需单独走设计变更。

### 8.3 生产缺陷

本轮未发现生产业务逻辑缺陷。SinkRuntime 审查中发现的唯一测试缺口（write_batch/assemble_sinks 无直接测试）已由本验收补齐 10 个组件测试，过程中暴露的 2 个初始失败均为测试自身时间戳构造错误（误用未来时间，被生产 `_is_fresh` 正确拒绝），修正为过去时间偏移后通过——反向印证生产逻辑正确。

## 9. 环境限制 / NOT_RUN

1. 真实 TwinCAT PLC / ADS Router 缺失：ADS 原生 SUBSCRIBE 硬件链路标 NOT_RUN（保留显式 skip 文案），ADS 语义由 FakeAdsSession 组件测覆盖。
2. WSL2 无 systemd/dbus：CLI systemd 真实测试 UNRELATED 失败，不代表产品缺陷。
3. 子进程 gRPC/mTLS PKI、SQLite 时序、TCP 探针类套件在本机环境的既有失败均已在 main 复现取证。
4. 遗留测试容器 `collector-v1-acceptance-nats-281e19f6`（2026-11-07 中断遗留，随机端口 48325，不与当前 UUID 命名 fixture 冲突）：未擅自删除，建议人工 `docker rm -f` 清理。
5. mypy 基线噪声：`test_crosscutting_boundary.py:92` AST.lineno 报错在 origin/main 同现（行号 90），非本次引入；CI mypy 门禁仅覆盖 src（329 文件 0 错）。

## 10. 分阶段裁决与 ChatGPT 待修复事项

| 阶段 | 裁决 | 依据 |
| --- | --- | --- |
| S1 静态检查 | **PASS** | ruff/format/mypy(329)/lint-imports(15) 全绿，无旧协议残留，架构边界唯一失败为 main 既有 |
| S2 Core 测试 | **PASS WITH LIMITATIONS** | 59 协议/组件 + 159 tests/src 全过；真实 PLC 硬件链路 NOT_RUN |
| S3 Contracts 测试 | **PASS** | 71 线协议用例 + 真实 NATS/JetStream 集成 3 测 + 采集系统 3 测全过 |
| S4 可靠性/性能 | **PASS WITH LIMITATIONS** | 30 设备计数闭合、背压/DLQ/释放实测、性能 baseline 诚实；多进程长稳未覆盖、崩溃恢复为设计已声明限制 |
| **总体** | **PASS WITH LIMITATIONS** | RELATED 净残留 0，无生产缺陷 |

ChatGPT 待修复/后续事项（均不阻塞本次裁决）：

1. 无阻断性生产缺陷需要修复。
2. `test_rpc_boundary::test_pb2_only_in_allowed_zones` 为 main 既有失败（pb2 允许区清单与现存 collector 文件不符），建议单独立项处理，不在本任务范围扩大。
3. Reliable 崩溃持久化如要承诺端到端无损，须先走设计变更再实现。
4. 多进程完整栈长稳（原 r5/soak 定位）需要新的 v2 规格测试方案，另行立项。
5. 本机环境限制项（systemd/dbus、ADS 硬件）应在具备条件的 CI/硬件台架上补跑闭环。

---

## 11. 合并前测试工具整改与定向复测（2026-11-09，D-TOOL-01～04）

### 11.1 Git 起始与最终 HEAD

| 项 | 值 |
| --- | --- |
| 起始 HEAD（整改前） | `ce8b2affd4dd2e348e953aa9f2481a173c026417`（与 origin 一致，工作区干净） |
| 工具/CI 整改提交 | `f0c6c75bda650ce442e86d3261f7df6c528a8547`（6 文件，+98/-118） |
| 本章提交 | 即本报告章节所在提交（见 `git log` 最新 docs 提交） |
| 分支 | `docs/foundation-core-contracts-20261108`，未 rebase / force push / PR / 合并 main |

### 11.2 实际修改文件（6 个，零 `src/` 生产代码改动）

1. `scripts/ci_gate.py` — performance/soak 子命令参数契约
2. `.github/workflows/ci-performance.yml` — 整体重写为 pytest 性能基线工作流
3. `.github/workflows/ci-qualification.yml` — 整体重写为有界可靠性工作流
4. `README.md` — 工具表、测试分层表、Soak/质量门禁/性能基线章节
5. `ai_shared/agent_config/skills/local-qualification/SKILL.md` — 资格门禁说明
6. `pyproject.toml` — performance marker 描述

### 11.3 D-TOOL-01 修复内容与参数契约

- **删除无效选项**：`performance --mode quick|full` 与 `--duration` 在 pytest 化后不参与任何执行，已连同解析代码一并删除，未保留兼容层。argparse 对失效参数统一返回 **exit 2**（实测 `--mode`、`--duration`、未知子命令、未知选项均为 2）。
- **`--output` 契约真实化**：两个子命令均保留 `--output`，help 明确「JUnit XML 测试结果文件路径（透传 pytest `--junitxml`），不是性能 Markdown/JSON 报告」；soak 子命令同步获得同一选项，使工作流产物有真实生产者。
- **ci-performance.yml**：删除 root/CAP_NET_ADMIN/tc netem 前置（新基线是纯进程内 pytest，实测无 docker/nats 依赖）、`mode/duration` 输入、`perf_report_*.md/json/png` 死产物上传；改为 `performance --output artifacts/performance/perf-results-<sha>.xml`，runner 由专用 self-hosted 改为 `ubuntu-latest`，上传 JUnit XML（`if-no-files-found: error`）。
- **ci-qualification.yml**：删除已无任何 Python 消费者的 `WIND_HUB_SOAK_PROFILE`/`WIND_HUB_SOAK_DURATION_S` 环境变量与 profile/duration 输入、`artifacts/soak/` 死路径（无生产者，原配置必然失败）、1560 分钟 24h 超时；改为 `soak --output artifacts/soak/soak-results-<sha>.xml`，超时收敛到 30 分钟。
- 全仓调用方闭合：README、local-qualification Skill、两个 workflow 已同步；`.claude/settings.json` 仅为通配 ci_gate 权限，不涉及具体参数；无脚本再传递已删参数。

### 11.4 D-TOOL-02 全仓失效引用审查结果

| 审查项 | 方法 | 结果 |
| --- | --- | --- |
| 已删模块 import（r5/soak/perf_runner/new_collector/v1 测试等） | 全仓 grep 12 个旧符号 + 路径 | 有效代码零命中；仅历史报告 `docs/core-v1-s3-acceptance.md` 保留历史叙述（不改写历史） |
| pytest 失效收集路径 | `pytest tests/src/performance tests/src/reliability --collect-only` | 5 tests collected，无 error |
| CI/Actions/shell 旧路径 | 7 个 workflow 全量检查 | 两个失效 workflow 已重写，其余 5 个不引用旧入口 |
| Makefile/文档/配置旧入口 | 全仓 grep `run_benchmark`/`WIND_HUB_SOAK`/`tests/reliability/soak` | README 4 处、Skill 1 处、pyproject 1 处已修复；残留仅为说明性「已退役」文字 |
| ci_gate 各 Gate 可运行性 | fast/performance/soak 实跑 + 非法 target 负例 | 见 11.5，全部符合预期 |
| Marker 与执行路径匹配 | 对照 pyproject markers 与 tests/src pytestmark | performance 用例 marker `performance,load`、reliability 用例 `reliability,soak,load`，与 gate 的 `-m` 选择器闭合 |
| 被删公共辅助是否仍被使用 | grep tests/support 引用 | `perf_collector/perf_reporter/veth/netem/perf_servers` 仍被 tests/tooling 与 web e2e 使用，**不在删除清单且保持有效**（tooling 107 测试通过） |
| 本地未跟踪残留 | `git ls-files tests/perf` | `tests/perf/` 仅剩未被 git 跟踪的 `__pycache__`（已删模块的本地缓存），不进仓库、不影响收集，建议人工清理 |

### 11.5 D-TOOL-03 真实执行命令与结果

| 命令 | 退出码 | 结果 |
| --- | --- | --- |
| `python3 scripts/dev.py env` | 0 | RESULT: PASS |
| `ruff check scripts tests/src` | 0 | All checks passed!（首次 E501 1 处，折行修复后 0） |
| `ruff format --check scripts tests/src` | 0 | 41 files already formatted |
| `mypy src` | 0 | Success: no issues found in 329 source files |
| `lint-imports` | 0 | Contracts: 15 kept, 0 broken |
| `MYPYPATH=src mypy scripts/ci_gate.py` | 0 | no issues found in 1 source file |
| `git diff --check` | 0 | 无空白错误 |
| `ci_gate.py --help` / `performance --help` / `soak --help` | 0/0/0 | 帮助文本含真实 JUnit XML 说明 |
| `ci_gate.py performance --mode quick` | **2** | error: unrecognized arguments |
| `ci_gate.py performance --duration 10` | **2** | error: unrecognized arguments |
| `ci_gate.py soak --bogus` / 未知 gate | **2/2** | argparse 拒绝 |
| `ci_gate.py performance --output /tmp/tool-verify/perf.xml` | 0 | GATE RESULT: PASS，3 用例 |
| `ci_gate.py soak --output /tmp/tool-verify/soak.xml` | 0 | GATE RESULT: PASS，2 用例 |
| JUnit XML 解析（xml.etree） | 0 | perf: tests=3 failures=0 errors=0；soak: tests=2 failures=0 errors=0 |
| `ci_gate.py fast --part backend-static` | 0 | GATE RESULT: PASS |
| `ci_gate.py fast --part backend-tests --targets bogus` | 1 | GATE RESULT: FAIL（未知 target fail-closed） |
| `pytest tests/tooling -q` | 0 | **107 passed** |
| 两个 workflow YAML 解析 | 0 | yaml.safe_load OK |

### 11.6 D-TOOL-04 未覆盖系统级限制复核

逐条复核第 8～10 章，确认报告未把未验证能力写成 PASS：

1. 真实 TwinCAT PLC/ADS 原生通知：仍为 **NOT_RUN**（第 9 节），仅 FakeAdsSession 组件层覆盖，无任何硬件 PASS 表述。
2. 完整多进程/多目标长稳：第 6 节明确「未在多进程完整栈做持续长稳」；本章工作流改名 **Soak (bounded reliability)**，`tests/src/reliability` 的 2 个进程内有界负载测试不得等同于系统级 Soak，文档与 workflow 注释均已写明旧 8h/24h 框架退役。
3. Reliable 崩溃恢复：第 8.2 节维持「设计已声明限制、非缺陷、不得宣称端到端无损」，本轮无新证据改变该结论。
4. 性能基线：3 个进程内吞吐/延迟用例只建立 baseline，不虚构 SLA（第 6 节原文保留）。

### 11.7 新增及修改测试数量

- 本轮**未新增/修改任何 pytest 用例**（任务边界为测试工具与 CI 一致性）。
- 现有套件复验：tests/src 全量 159 passed 维持有效；tests/tooling 107 passed；performance 3 用例、soak 2 用例经门禁实跑并产出合法 JUnit XML。
- 未使用 skip/xfail/noqa/type-ignore，未放宽任何断言。

### 11.8 未解决问题与责任归属

| 问题 | 归属 | 处置 |
| --- | --- | --- |
| `test_rpc_boundary::test_pb2_only_in_allowed_zones` main 既有失败 | ChatGPT 单独立项 | 第 10 节已记录，与本轮无关（UNRELATED 已取证） |
| 真实 ADS 硬件、多进程长稳、崩溃恢复 | 环境/设计边界 | 维持 NOT_RUN/已声明限制，需硬件台架或设计变更 |
| 本地 `tests/perf/__pycache__` 未跟踪残留 | 本机环境 | 建议人工删除，不进仓库 |
| 历史报告中的 run_benchmark 叙述 | 历史记录 | 刻意保留，不改写验收历史 |

### 11.9 本轮最终裁决

**PASS**。

- D-TOOL-01：失效参数已删除且拒绝闭合（exit 2），`--output` 契约与 JUnit XML 实际产物一致，无兼容层、无沉默接受。
- D-TOOL-02：全仓失效引用闭合，两个原本必然失败的 workflow 重写为可成功的真实流水线，无失效收集路径。
- D-TOOL-03：ruff/format/mypy(329)/lint-imports(15)/tooling 107 测试/两个 gate 实跑/XML 校验全部通过，无新增 RELATED 缺陷。
- D-TOOL-04：三项系统级未验证能力在报告中均保持 NOT_RUN/限制表述，无过度宣称。
- 剩余事项均为已有证据支持的环境限制或历史问题，不阻塞合并前审查。

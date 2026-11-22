# Storage Service F6 C2 最终补充验收报告（2026-11-22）

- 分支：`feat/storage-service-v1-implementation`
- F5 交接 HEAD：`ed9bbd641`
- 最终 HEAD：见第 10 节提交清单（push 后本地=远端）
- 权威设计：`docs/design/Storage_Service_软件设计_v1_4.md`（§6.5、§7、§8、§14.2、§14.3、§15、§16）
- 上游报告：`docs/audit/storage_f5_final_acceptance_20261122.md`
- 验收人：Claude Code（独立执行；**生产代码、Contracts、Sink、Collector 零修改**）

## 1. Git 基线与工作原则

`git fetch` 后确认远端 HEAD = `ed9bbd641`，工作树干净，未新建分支、未建 PR、未合并 main、
未 rebase/force push。本轮全部变更为测试与测试文档。真实中间件由 F6 专用独立 Compose
project（`wind-hub-f6-*`，动态 NATS/Redis 端口，Kafka 9092、TDengine 6041 固定 loopback）
提供，结束 `down -v --remove-orphans`；自管栈测试用例各自独立起停，未复用任何遗留容器。

## 2. F6 四项验收逐项结果

| 项 | 矩阵 ID | 裁决 | 测试/证据 |
|---|---|---|---|
| F6-A 真实 CLI 重启激活 | T-63 | **PASS** | `integration/test_cli_restart_activation.py`（1 用例，真实双进程，连跑 3 轮稳定） |
| F6-B Kafka JSON v1 严格拒收 | T-15 | **PASS**（Storage 侧） | `integration/test_kafka_json_versions.py`（1 用例，真实 Kafka+TDengine）+ `unit/test_kafka_json_contract.py`（3 用例） |
| F6-C DDL 参数真实回读 | T-16 | **PASS** | `integration/test_tdengine_real.py::test_g03_real_ddl_parameters_readback_and_drift_detection` |
| F6-D data_level 跨模块契约 | T-44/T-59 | **PASS（代码实证一致）+ 文档残留 P3** | 代码审计 + `unit/test_sink_wire_contract.py`（4 用例） |

## 3. F6-A：T-63 真实 CLI 重启激活（最高优先级）

测试：`tests/storage_service/integration/test_cli_restart_activation.py::test_cli_restart_activates_staged_static_closure`
层级 marker：integration / real_service / docker / restart / reload / kafka。

真实组件：

- 中间件：r5 栈真实 NATS 2.10.29、Kafka 3.9.1、TDengine 3.3.6.13；
- Configuration：`write_live_configuration` + 正式 CLI `service start configuration` 独立进程；
- Storage：正式 CLI `service start/stop storage`（process manager，`python -m storage_service.main --bootstrap ...`，
  独立进程组、PID/tick 状态文件、私有 0700 state/log 目录），全程未调用 `watcher.refresh_now()`、
  未直接构造 Runtime；
- 发布：真实 gRPC `CreateCandidate/PublishCandidate` 完整八文档闭包（A=service 1.1.0/deployment 1.0.0，
  B=service 1.1.1/deployment 1.0.1，B 将 `query.max_page_size` 1000→999）；
- 观测：真实 gRPC `GetServiceStatus`（Bearer 鉴权）与 `QuerySamples`，以及快照文件 JSON 实证。

实测时序与退出码：

1. CLI start A：`returncode=0`，JSON 信封 `status=SUCCESS`，`process.confirmed=true`，
   `business.health=HEALTHY`（CLI 自身轮询 GetServiceStatus 确认）。
2. gRPC：`target_revision == active_revision`（64 hex），`dependencies[0].detail=READY`。
3. 持久化：`configuration-snapshots/accepted_bundle.json` 存在、`target_bundle.json` 不存在；
   accepted 快照 revision=A、`max_page_size=1000`。
4. 发布 B 后经真实 NATS 广播触发调和（`reconcile_interval_s=5` 为兜底），gRPC 轮询到：
   `target_revision=B`、`active_revision=A`、`detail=RESTART_REQUIRED`、`health=DEGRADED`。
5. 文件证据：`target_bundle.json` 持久化 revision=B 且 `max_page_size=999`；
   `accepted_bundle.json` 仍是 A（target_revision A、1000）。
6. CLI stop：`returncode=0`、SUCCESS（SIGTERM 进程组确认退出）；CLI start：`returncode=0`、SUCCESS。
7. 重启后 gRPC 轮询到 `target == active == B`、`detail=READY`、`HEALTHY`——B 由进程自身在
   启动期从可信 target 快照提升（main.py 待重启目标恢复路径），未借助测试进程。
8. B 静态参数真实生效：同样的 `QuerySamples(page_size=1000)` 在 A 期成功（空结果，
   range 校验先于 SQL），重启后返回 gRPC `INVALID_ARGUMENT`（QueryRejectedError 稳定映射）。
9. 历史绑定保持：重启后 accepted 快照含 `point_table_storage-points_v1.0.0` 与
   Device Binding `WTG01`，与发布闭包一致。

## 4. F6-B：T-15 Kafka JSON v1 严格拒收（真实链路）

### 4.1 真实 Kafka + 真实 TDengine 集成测试

`test_kafka_json_versions.py::test_legacy_json_v1_record_blocks_partition_without_commit_or_write`
（r5 栈自管，独立 topic/group/农场，结束清栈删库）：

1. 生产真实 v2 Record（含消息 key，RFC3339 时间戳）→ 经生产 `KafkaStorageConsumer +
   IngestionService + TDengineBackend` 全链路：admin 轮询到消费组提交 offset=1，
   独立 taosws 连接读回物理表 1 行。
2. 再生产同构的旧 v1 Record（仅 `schema_version=1`）：
   - 消费端日志实证 `Storage Kafka partition blocked`；
   - SQLite `blocked_partitions` 持久证据：`(offset=1, reason 含 "schema_version")`，
     `list_blocked` 同源可查；
   - 等待 3 秒真实 broker 窗口后消费组 offset 仍为 **1**（未提前确认）；
   - TDengine 物理表仍为 **1 行**（v1 未入库，无自动升级/静默映射/兼容解析）。
3. 阻断根因链：`StorageSample.from_kafka_json`（sample.py:164 仅接受 int 2）→
   ValueError → `IngestionService._block` 落 durable 证据并抛 `IngestionBlockedError` →
   consumer `pause(tp)` 置 `state.blocked`，Offset 不推进。

### 4.2 契约单元测试（`unit/test_kafka_json_contract.py`，3 用例）

- 三级权威值 raw/protocol/business 的 v2 记录全部正常解码；
- v1、字符串 "2"、v3 均以 `only Kafka JSON v2 is supported` 拒收；
- `engineering`、大写 `PROTOCOL`、空等级均以 data_level 错误拒收（T-44 Storage 边界证据）。

### 4.3 Sink→Storage 生产者格式一致性（如实区分）

- **已验证（编码器/解码器线契约级）**：见 F6-D，Sink 真实编码器输出可被 Storage 真实解码器接受。
- **未验证（NOT_RUN）**：真实 Sink 进程→Kafka→Storage 进程的在线 System E2E；
  归属 T-14 全链 E2E，不在 F6 扩范围。前者不得冒充后者。

## 5. F6-C：T-16 TDengine DDL 参数真实回读

`test_tdengine_real.py::test_g03_real_ddl_parameters_readback_and_drift_detection`
（真实 3.3.6.13，走生产 `TDengineBackend.ensure_tables` DDL 路径，非手写 SQL）：

- 配置非默认参数 `DURATION 5d / KEEP 30d / PRECISION us / CACHEMODEL last_row / CACHESIZE 2`；
- 建库后查询 `INFORMATION_SCHEMA.INS_DATABASES`（保留字反引号）实机回读：
  `duration='5d'`、`keep` 三级策略首档 `'30d'`、`precision='us'`、
  `cachemodel='last_row'`、`cachesize=2`，与配置逐项相等；
- **负向控制**：第二个后端实例对同一物理库声明 `DURATION 7d`，`ensure_tables` 经生产
  `_reconcile_database_retention` 抛出 `TDengineSchemaConflictError`（CREATE IF NOT EXISTS
  不是验证，漂移不得误判成功）；原后端仍可正常写入读回；用例结束 DROP 独立测试库。

## 6. F6-D：T-44/T-59 跨模块 data_level 契约审计

### 6.1 代码实证（已逐条复核行号）

| 环节 | 证据 | 结论 |
|---|---|---|
| Sink Kafka 编码 | `sink_service/infrastructure/adapters/outbound/kafka_wire.py:32,84` 硬编码 `schema_version: 2`、`data_level` 透传 | v2 |
| Sink 等级类型 | `application/delivery_point.py:15` `Literal["raw","protocol","business"]`；`from_batch` 白名单校验 | 无 engineering |
| Sink 配置守门 | `application/delivery_definition.py:41`、`application/config.py:336` 两处三值白名单 | 非法值无法进入运行态 |
| Contracts | `contracts/storage_service.proto:5-10` 枚举 UNSPECIFIED/RAW=1/PROTOCOL=2/BUSINESS=3，**无 engineering**；Sink/Collector proto 无 data_level 字段 | 一致 |
| Collector | `src/collector/` 全目录无 data_level；三级值在 SampleBatch 中并列（raw_value/value/business_value），由 Sink 选级 | 不产出等级枚举 |
| Storage 解码 | `domain/sample.py:18-23` StrEnum 三成员且注释明示 legacy engineering 非别名；`:164-170` 仅 v2、仅三值 | 确定性拒收 |
| Storage 阻断 | `application/ingestion.py:106-122` 解码失败即 block，无降级/映射 | fail-closed |
| TDengine 映射 | `tdengine_backend.py:80` 1/2/3；`tdengine_schema.py:20` TINYINT | 与设计 §6.5.2 一致 |
| 配置模板 | `configs/templates/deliveries/*.yaml` 注释 `# raw | protocol | business`，全 configs/ 无 engineering | 一致 |

### 6.2 新增线契约测试（`unit/test_sink_wire_contract.py`，4 用例）

用 Sink 真实 `encode_kafka_point` 编码三等级 Record，直接喂 Storage 真实
`StorageSample.from_kafka_json`：三级全部解码成功且等级/身份/单位断言一致；
并证明 Sink 编码器与 Storage 解码器两端都拒绝 `engineering`。

**运行态不兼容结论：不存在。** 生产代码已统一 raw/protocol/business，无需其他模块迁移，
不阻断 C2 Storage 单模块准出；完整在线全链仍按 T-14 归属后续 System 验收。

### 6.3 残留文档偏差（P3，不阻断，未修改冻结模块）

- 旧设计文档仍出现 engineering：`Contracts_软件设计_v1_38.md`（已被 v1_39 取代）、
  `CLI_软件设计_v1_49.md:2043`、`wind-hub_总体逻辑架构设计_v1_22.md:443`；
- `realtime_adapters.py:196-205` Redis Value payload 不含 schema_version/data_level，
  与 Contracts v1_39 §11.9"Redis 使用相同逻辑字段"的文档表述存在实现/文档偏差
  （Redis 非本 Kafka 链路，记录交 ChatGPT 裁定文档归属）。

## 7. 测试清单、层级与真实退出码

| 命令 | 层级 | 结果（真实退出码 0） |
|---|---|---|
| `pytest tests/storage_service/unit/test_kafka_json_contract.py` | unit | 3 passed |
| `pytest tests/storage_service/unit/test_sink_wire_contract.py` | unit | 4 passed |
| `pytest tests/storage_service/unit`（全量回归） | unit | **94 passed**（F5 为 87，+7） |
| `pytest tests/storage_service/component` | component | 11 passed |
| `pytest tests/storage_service/integration/test_tdengine_real.py` | integration/real | 8 passed（F5 为 7，+g03） |
| `pytest tests/storage_service/integration/test_kafka_json_versions.py` | integration/real | 1 passed |
| `pytest tests/storage_service/integration/test_cli_restart_activation.py` | integration/real | 1 passed ×3 轮 |
| 四定向文件合跑 | — | 12 passed（61s） |
| `pytest tests/configuration_service/integration/test_storage_deployment_flow.py` | 跨模块回归 | 3 passed |

## 8. 静态门禁（真实退出码全 0）

| 门禁 | 结果 |
|---|---|
| `python3 scripts/dev.py env` | PASS（wind-hub，Python 3.11.16） |
| `ruff check src/storage_service tests/storage_service` | All checks passed |
| `ruff format --check` 同范围 | 46 files already formatted |
| `mypy src` | Success: no issues found in 477 source files |
| `lint-imports` | Contracts: 22 kept, 0 broken |
| `python3 scripts/generate_rpc.py --check` | PASS |
| `git diff --check` | PASS |

## 9. 生产/测试修改清单与缺陷台账

修改文件（全部测试侧）：

1. `tests/storage_service/integration/test_cli_restart_activation.py`（新增，T-63）
2. `tests/storage_service/integration/test_kafka_json_versions.py`（新增，T-15 真实链路）
3. `tests/storage_service/unit/test_kafka_json_contract.py`（新增，v1/engineering 拒收）
4. `tests/storage_service/unit/test_sink_wire_contract.py`（新增，T-59 线契约）
5. `tests/storage_service/integration/test_tdengine_real.py`（新增 g03 DDL 回读+漂移负向）

缺陷台账：

| ID | 级别 | 内容 | 状态 |
|---|---|---|---|
| F6-D1 | P3（文档） | 旧设计文档 engineering 残留、Redis payload 与 v1_39 文档表述偏差 | OPEN，移交 ChatGPT，不阻断 Storage |
| — | P0/P1/P2 | 无生产缺陷、无测试阻断 | 0 |

测试编写中发现并消化的测试侧问题（未入台账）：bootstrap 必填 `deployment_id`、
Kafka 消费要求消息 key、查询必填时间戳且受 max_range_days 约束、CLI 健康检查需自身持有
Bearer 环境变量——均为测试装配问题，生产行为全部符合设计。

## 10. 提交清单与 F5 NOT_RUN 归属变化

提交（均在固定分支，push 后本地=远端、工作树干净）：

1. `test(storage): add T-63 real CLI restart activation, T-15 v1 rejection and T-16 DDL readback`
2. `test(storage): pin cross-module data_level wire contract`（如合并为单次提交以实际 git log 为准）

矩阵变化：

| 矩阵 ID | F5 裁决 | F6 裁决 | 依据 |
|---|---|---|---|
| T-63 | NOT_RUN | **PASS** | 真实双进程 CLI stop/start 全证据 |
| T-15 | NOT_RUN | **PASS（Storage 侧）**；在线 Sink→Storage E2E 仍随 T-14 NOT_RUN | 真实 v2 入库/v1 阻断+单元契约 |
| T-16 | PASS*（仅 DDL 字符串） | **PASS** | INS_DATABASES 真实回读+漂移负向 |
| T-44 | NOT_RUN | **PASS（边界）** | engineering 双端拒收实证；生产代码无别名 |
| T-59 | PASS | **PASS（增强）** | 新增 Sink 编码→Storage 解码线契约 |

维持原归属、F6 未扩展：T-46～T-53 性能/长稳、T-14 全链 E2E、D-11 真实 taosdump、
T-17～T-22 ALTER 生命周期、T-24/T-25 命名碰撞、T-37/T-38 并发 DDL。

## 11. 残留风险

1. Storage 进程级 CLI 重启虽已真实验收，但 Configuration 短暂不可用时的
   可信快照冷启动路径（main.py 降级分支）未做断网注入，仅代码评审与 F5 快照故障测试覆盖。
2. 真实 Sink 进程在线生产→Storage 消费的 System E2E 未执行（T-14 归属）。
3. F6-D1 文档/Redis 表述偏差待 ChatGPT 裁定，不影响 Kafka 链路。
4. 真实集成依赖显式环境变量与 Docker；无 Docker 时 skip 不得计 PASS。

## 12. 最终 C2 准出建议

**C2_READY_WITH_LIMITATIONS。**

F6 四项关键缺口全部关闭：T-63 真实 CLI 重启激活、T-15 v1 真实链路严格拒收、
T-16 DDL 实机回读与漂移负向、T-44/T-59 跨模块等级契约代码实证+线契约测试；
静态门禁全绿，Storage 单测增至 94 项，无 P0/P1/P2，生产与共享契约零修改。
剩余 NOT_RUN 项均为设计明确允许延期的 System/现场专项。最终准出裁决由 ChatGPT 作出；
未自行合并 main、未创建 PR。

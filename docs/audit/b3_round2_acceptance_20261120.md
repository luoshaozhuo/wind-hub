# B3 第二轮集中修复与独立复测报告（2026-11-20）

- 分支：`feat/config-businesspoint-alignment-20261118`
- 复测基线 HEAD：`386097591e053350509d967b642fd88149b702ca`（含 ChatGPT 的 P1-PROD-01 修复 `7b00b91de` 与 Reliable 取消修复 `5d315c84d`）
- 工作模式：仅在原分支工作，未新建分支/PR、未合并 main、未 rebase/force push、未恢复废弃接口、未加兼容层

## 1. 总结论：**已实现功能达到准出条件；存在 1 项系统级 E2E 迁移待办（不阻塞 marker 准出）**

- 五项静态门禁全部 exit 0；权威 marker 套件 **2582 passed / 0 failed / 0 collection errors**。
- 上轮 217 failed + 10 collection errors 全部清零（marker 口径）。
- 新增 BUSINESS 投影、历史 Manifest 编解码、gRPC 鉴权、Kafka 准入四类专项正反测试共 **39 个**。
- 唯一遗留：`test_r1f3_strict_cli_e2e` 及其共享的 17 个 sink system 测试仍基于已退役的 mTLS `SinkSystemHarness`，属系统测试基础设施整体迁移，标记 NOT_RUN（均为 system/docker/slow，不在权威 marker 套件内）。

## 2. 五项静态门禁（实际执行）

| 门禁 | 交付时 | 本轮修复后 |
|---|---|---|
| `generate_rpc.py --check` | 0 | **0** |
| `ruff check src tests scripts` | 1（UP038/I001） | **0** |
| `ruff format --check` | 1（14 文件） | **0** |
| `mypy src` | 1（grpc_auth None） | **0** |
| `lint-imports` | 0 | **0** |

生产侧仅 2 处小语义修：`grpc_auth` 对 `invocation_metadata()` 为 None 的守卫、`business_projection` 的 `int | float` 写法；其余生产文件经 token 级比对确认为纯格式重排，无意外语义变更。

## 3. 测试结果（实测计数）

### 3.1 权威 marker：`pytest -m "unit or component or contract"`

- **2582 passed / 0 failed / 1024 deselected / 0 collection errors**（278s）
- 全仓 `--collect-only`：3567 测试可收集，0 ERROR。

### 3.2 分组（修复前 → 修复后）

| 范围 | 上轮 | 本轮 | 处置 |
|---|---|---|---|
| sink unit/component | 90 failed + 3 error | **555 passed** | 编码器/harness/身份字段迁移 + 新模块测试 |
| sink integration | 8 failed/7 error/10 skip | **20 passed / 10 skip** | 10 skip 均为 `WINDHUB_NATS_SERVER_BIN` 未设置，环境前置明确 |
| collector | 25 failed/2 skip | **153 passed / 2 skip** | `pack()/publish()` 补真实点表身份 |
| tests/cli | 108 failed/13 skip | **242 passed / 1 failed / 13 skip** | 双端口 fake + Bearer 夹具；唯一失败为 r1f3 系统遗留 |
| configuration_service | 5 failed | **246 passed** | service/v2 schema 槽冲突迁移到空闲 v4 |
| web_server | 0 | **226 passed** | Bearer 客户端改造无回归 |
| contracts/core/tooling | 0 | **329 passed** | SampleBatch v2→v3 契约迁移 |

## 4. 关键修复证据

### 4.1 P1-PROD-01：Realtime Kafka 准入（任务 4.1）

生产修复 `config.py` 强制 kind↔mode 耦合。新增 `test_delivery_admission.py`（5 用例）实证：
- Kafka+Reliable 通过、非 Kafka+Realtime 通过；
- Kafka+Realtime 拒绝（`Kafka requires Reliable`）、非 Kafka+Reliable 拒绝（`requires Realtime`）。
不再存在“可启动但首次写入零投递”的 Delivery；未重新实现废弃单点编码器。

### 4.2 收集错误清零（任务 5）

`test_kafka_wire_parity.py`（236）、`test_kafka_wire_b3_bytes.py`（9）、`test_b1_encoded_kafka_delivery.py`（7）改为现行 `encode_kafka_points`，按 18 字段 Kafka JSON v2 重建 golden，覆盖 `data_level/device_type/point_table_id/business_point_id/unit/sample_id/batch_id` 与精确字节；旧 13 字段断言全部移除。

### 4.3 新增模块专项测试（任务 7）

- `test_business_projection.py`（15）：精确匹配、一对多、scale/offset、单位换算、int64/uint64 边界、>2^53 拒绝、非有限值拒绝、NULL、未知点、缺失映射、bool 非整数、fail-closed 不降级 PROTOCOL。
- `test_historical_bundle_codec.py`（8）：YAML/语义/Manifest 三层哈希、身份与 device_type 匹配、篡改拒绝、缺依赖、重复版本拒绝。
- `test_grpc_auth.py`（11）：caller 构造 fail-closed、缺失/重复/错误 token、权限不足、未知 RPC、授权通过（真实拦截器两步调用，非 mock 权限）。
- `test_delivery_admission.py`（5）：见 4.1。

### 4.4 Reliable 历史查询生命周期（任务 4.2）

生产 `reliable_engine.py` 心跳方法在 `finally` 取消未完成的只读历史查询任务（不触碰已提交的目标写入）。相关 reliable seam/evidence/ack 测试（`test_reliable_seams`、`test_reliable_sample_evidence`、`test_replay_header_ack_bypass`、`test_reliable_durable_ack_real_nats` 可执行部分）全部通过；JetStream 仅在 terminal 确认后 ACK、DLQ 阻断不 ACK 的行为由现有可靠投递测试保持。

## 5. 真实链路证据（任务 9）

- Configuration → NATS 通知：`test_revision_notification` 真实 NATS 进程 **passed**。
- Sink 集成：20 passed（真实 NATS/Redis 链路；JetStream 二进制深测因环境变量缺失 skip）。
- Collector → NATS 真实系统：`test_collector_acquisition_system`/`test_collector_grpc_system` 在修正 v3 schema 断言后 **passed**。
- CLI → Sink/Configuration：fake gRPC server 同时提供 secure 与 insecure 端口，Bearer metadata 经真实子进程验证（242 passed）。
- Kafka PROTOCOL：编码器字节/分区缓存/批量确认单测全绿；BUSINESS 投影与编码器 fail-closed 全绿。端到端 Kafka BUSINESS 投递依赖 sink system harness 迁移，见 NOT_RUN。

## 6. 缺陷台账

- **P0**：无。
- **P1（生产）**：无未关闭项。P1-PROD-01 已由生产修复 + 5 个准入测试关闭。
- **P2（测试基础设施，不阻塞 marker 准出）**：
  1. `SinkSystemHarness`（`_stage1_tls.write_mtls_bundle`、`certificate_identity` caller）整体仍是 mTLS 模型，与生产 token 模型不兼容；17 个 system/docker/slow 测试文件依赖它，需统一迁移到 Bearer 后才能恢复端到端 E2E（含 r1f3 六身份链路、真实 Kafka BUSINESS 端到端、CLI→Sink 真实 gRPC 系统级验证）。
  2. `ConfigurationSubscriber` 缺少独立的真实 NATS+Repository 集成测试（其依赖的 Manifest 校验、CAS、NATS 唤醒已分别由单测和 configuration 集成覆盖，但断线重连/轮询兜底/关闭收敛的端到端证据待补）。
  3. `HistoricalPointTableClient` 的网络异常/Token 失败/缓存路径缺直接单测（codec 与投影已覆盖，gRPC 客户端薄封装待补）。

## 7. NOT_RUN（未记为 PASS）

- 17 个 mTLS harness 体系的 sink system E2E（system/real_service/docker/slow）。
- `WINDHUB_NATS_SERVER_BIN` 驱动的 10 个真实 JetStream 持久化深测。
- 硬件/PLC/netem/root 权限、iSulad/containerd、多机部署。
- Storage（TDengine）端到端持久化：本分支无对应生产实现，列为独立待验收，未合并 main。

## 8. RAW 能力边界（明确未完成能力）

RAW 原始协议报文能力**尚未实现**：`kafka_wire._require_protocol` 对 `raw` 直接拒绝，SampleBatch v3 不含原始帧字节。当前没有把已解码标量或重建字节重命名为 RAW 的路径；该能力须以独立原始帧采集链路交付，不得通过重命名关闭缺陷。

## 9. 准出建议

**建议对“已实现功能”予以准出**：满足五项静态门禁全绿、权威 marker 0 failed/0 收集错误、新增模块具备正反测试、P1 生产缺陷关闭。前置跟进项为 P2 的 sink system harness token 化迁移（恢复 system E2E 与端到端 Kafka BUSINESS 证据）与 Subscriber/历史客户端集成测试补齐，这些不影响单元/组件/契约层的正确性结论，也不应在补齐前将相关 E2E 计为 PASS。

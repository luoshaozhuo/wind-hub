# B3 Sink/Storage 数据等级与配置同步 — 集中独立验证报告（2026-11-20）

- 分支：`feat/config-businesspoint-alignment-20261118`
- 交付 HEAD：`69c72f437afb4b3543ee8dc920c71e3638a1b261`（已核实本地=远端，工作树初始干净）
- 上轮 B2 收尾：`ade01693b`
- 验收机：conda wind-hub；中间件实测可用（docker r2-nats/r2-redis 与 wind-hub-r5 nats/redis/kafka/tdengine 容器在跑）
- 本轮不触碰 B3 之外的 Sink/Storage 数据等级契约设计，仅做静态修复与测试/夹具迁移及独立审查

## 1. 总结论：**FAIL（不具备准出条件）**

生产侧新增的 BUSINESS 投影、历史 PointTable 客户端、Configuration 订阅器、Manifest 编解码与 token 鉴权**架构本身设计稳健、fail-closed 且通过静态门禁**；但 B3 交付**未同步既有测试与夹具到新 v3/token 契约**，导致测试套件大面积红灯，并暴露 **1 个真实生产缺陷（P1，realtime Kafka 写入路径被自废）**。任务书明确要求“所有必要 Fixture、配置及断言必须同步新契约”，该项未达成，故裁决 FAIL。

## 2. 静态门禁（交付时 3 项红，本轮修复后全 0）

| 门禁 | 交付时 | 本轮修复后 | 处置 |
|---|---|---|---|
| `generate_rpc.py --check` | 0 | 0 | — |
| `ruff check src tests scripts` | 1（UP038、I001） | 0 | UP038 改 `int | float`；I001 自动排序 |
| `ruff format --check` | 1（14 文件） | 0 | 官方 formatter，纯格式 |
| `mypy src` | 1 | 0 | `grpc_auth.py:88` 对 `invocation_metadata()` 可能为 None 增加守卫 |
| `lint-imports` | 0 | 0 | 19 契约 kept |

生产语义改动仅 2 处小修（grpc_auth None 守卫、business_projection UP038）+ 1 处 import 排序；token 比对确认其余生产文件均为格式重排，无意外语义变更。

## 3. 测试结果（实测计数）

### 3.1 权威 marker 套件 `pytest -m "unit or component or contract" --continue-on-collection-errors`

- **2066 passed / 217 failed / 10 collection errors / 1024 deselected**（276s）
- 不加重试标志时，3 个收集错误会直接中断整轮（pytest Interrupted）。

### 3.2 分组计数（本轮部分迁移后）

| 范围 | passed | failed | error/skip | 说明 |
|---|---|---|---|---|
| contracts + core/config + tooling + src 契约 | 329 | 0 | — | 含本轮迁移的 SampleBatch v3（65） |
| configuration_service | 246 | 0 | — | 修复 5 个 service/v2 schema 槽冲突 |
| web_server | 226 | 0 | — | Sink Bearer 客户端改造无回归 |
| collector | 128 | 25 | 2 skip | 全部为 `pack()` 缺 device_type/point_table_id 的测试 kwargs |
| tests/cli | 135 | 108 | 13 skip | cli_env 夹具仍给 sink 配 tls_profile，Runtime 现拒绝 |
| sink_service/unit | 139 | 66 | 2 error | 迁移前 116 失败，本轮中心夹具/DeliveryPoint 修复后降至 66 |
| sink_service/component | 34 | 24 | 1 error | 含 P1 realtime-Kafka 红灯 |
| sink_service/integration | 5 | 8 | 7 error/10 skip | 共享夹具缺 data_level |

### 3.3 本轮已完成的测试/夹具迁移（未弱化断言）

1. `test_sample_batch.py` v2→v3：工厂补 device_type/point_table_id、schema 2→3、identity 元组扩为 8 字段；65 全绿。
2. `_stage1_fixtures.py`：service 文档 schema_version 1→2、security 由 mTLS（ca_cert/certificate_identity）迁到 token_env callers。
3. 6 个文件的 `DeliveryPoint(...)` 与 `SampleBatch(...)` 补齐 device_type/point_table_id/batch_id。
4. `test_schema_regressions.py`：通用嵌套数组校验从被保留的 service/v2 槽迁到空闲 v4，消除与内置 token schema 的不可变注册冲突（13 全绿）。

## 4. 新发现缺陷台账

### P1-PROD-01（真实生产缺陷，未自行修复，交集中整改）：realtime Kafka 点对象写路径被自废

- 证据链：
  - 提交 `4f2debea5 fix(sink): remove implicit legacy Kafka encoding bypass` 删除了 `_write_records` 中 `key is None` 分支的 `encode_delivery_point(sample)`，改为无条件 `raise ConfigurationError("Kafka requires explicitly encoded Reliable records and record key")`（`realtime_adapters.py:472-475`）。
  - 但 `KafkaOutput.write_batch(DeliveryPoint)` 仍以 `key=None` 调 `_write_records`（`:357`）；realtime 引擎 `realtime.py:335` 仍调 `writer.write_batch(batch)`；`realtime_runtime.py:49-50` 在 `kind=="kafka"` 且 channel=realtime 时仍构造 KafkaOutput；配置校验（`config.py:356`）只约束 data_level，不禁止 realtime+kafka 组合。
- 后果：可配置、可启动的 realtime Kafka 投递在首次写入即零投递；异常被内部捕获写入 `outcome.error`（不抛出），形成**静默不投递**。`test_kafka_device_stripes`/`test_kafka_output_partition_cache`/`test_b1_encoded_kafka_delivery` 等以 `delivered==()` 红灯真实复现（无本地 broker 也经 fake producer 证实）。
- 裁决选项（设计决策，不宜由验收方单方面定）：(a) 在配置校验显式拒绝 realtime+kafka；或 (b) 用新 `encode_kafka_points(protocol)` 重建 realtime 点对象编码路径。

### P2-TEST-01：3 个文件引用已删除的单点编码器（收集错误，阻断套件）

- `test_kafka_wire_b3_bytes.py`、`test_kafka_wire_parity.py`、`test_b1_encoded_kafka_delivery.py` import 已删除的 `encode_delivery_point`/`encode_kafka_point`（提交 `420d5d1c2`）。其中 b3 字节等价测试还锚定已不存在的 13 字段旧线格式（新 `_record` 为含 data_level/point_table_id/business_point_id/unit/device_type 的 16+ 字段）。需按新批量 API 重写而非机械改 import；当前**新 `encode_kafka_points` 无任何通过中的正向单测**。

### P2-TEST-02：新增 B3 生产模块缺专属通过测试

- `config_subscription.ConfigurationSubscriber`、`historical_point_table.HistoricalPointTableClient`、`business_projection.HistoricalPointTable`、`historical_bundle_codec.parse_point_table_bundle` 仅在失败/收集错误文件中被引用，无绿灯单测/集成测试覆盖其竞态、对账与哈希校验。

### P2-TEST-03：其余机械迁移债（按根因聚类）

- 内联 delivery 模板缺 `output.data_level`（~31）；strict-config 测试仍断言退役的 `certificate_identity` 形态与旧错误文案；`test_grpc_api` 等 mTLS 时代系统测试（certificate/plaintext 语义）需迁 token 模型；collector 25 处 `pack()` 缺新 kwargs。全部为测试侧契约漂移，非生产逻辑错误。

## 5. 生产契约独立审查结论（设计正确的部分，附代码证据）

- **RAW 显式拒绝**：`config.py:408-411`（raw 不在 protocol/business 允许集）+ `kafka_wire._require_protocol`（raw 直接 raise）。真正 RAW 原始帧采集链路**未实现**，当前不存在把解码标量/重建字节冒充原始帧的路径。
- **PROTOCOL/BUSINESS 隔离**：`encode_kafka_points` 强制 BUSINESS 须带完整逐点投影、PROTOCOL 禁带投影；realtime 仅允许 protocol。
- **BUSINESS 历史绑定**：`HistoricalPointTableClient` approved-manifest 白名单 + 双次 SHA-256/manifest 校验，fail-closed；`business_projection` 对 int64 有免浮点往返路径，>2^53 显式拒绝，非有限值拒绝。
- **Manifest/哈希**：`historical_bundle_codec` 逐文件 yaml/content 双哈希、manifest 重算、Units/BusinessPoint 单版本约束、父引用校验，全部 fail-closed。
- **订阅器**：NATS 仅唤醒、Repository revision 权威；轮询兜底（NATS 不可用不阻塞）；每轮独立 gRPC channel（无泄漏）；wake/stop task finally 取消；单消费者循环无并发重复提交；revision 去重。
- **JetStream ACK**：`reliable_engine` 仅在 `_deliver_batch` 返回 terminal（全部点确认）后 `message.ack`；DLQ 阻断则 retry 不 ACK；解码失败先入 DLQ 再 ACK。
- **Schema 不可变**：`config_schemas` 唯一约束 + `RepositoryConflictError`；service v2 token schema 经 deepcopy 派生，不污染 v1。
- **gRPC 鉴权**：`PeerAuthorization` 缺失/重复/伪造 token→UNAUTHENTICATED，权限不足/未知 RPC→PERMISSION_DENIED；Web/CLI 使用独立 Bearer 身份；Runtime 拒绝 internal 服务的 tls_profile。

## 6. NOT_RUN / 限制（未冒充 PASS）

- 硬件/真实 PLC/netem/root 权限、iSulad/containerd、多机部署。
- `test_grpc_api` 等 NATS 系统测试在当前夹具下启动超时，未取得真实进程鉴权绿灯（属 P2-TEST-03 迁移债）。
- 真实 Kafka 端到端 BUSINESS 投递（容器可用但相关系统测试夹具未迁移，未运行到成功路径）。
- 订阅器断连重放的端到端对账仅有代码级证据，无绿灯集成测试（P2-TEST-02）。
- B3 范围外的 Storage 持久化等级（TDengine 等）未评估。

## 7. 准出建议

**不予准出。** 前置整改项：
1. 裁决并修复 P1-PROD-01（拒绝 realtime+kafka 配置 或 重建其编码路径），并补回归测试。
2. 完成夹具/测试 v3+token 契约迁移，清零 217 failed 与 10 collection errors（本轮已示范 SampleBatch/中心夹具/schema 的迁移模式）。
3. 重写 3 个编码器测试到 16+ 字段新线格式，为 `encode_kafka_points` 与四个新 B3 模块补正向测试。
完成后需重跑本报告 §2/§3 全部门禁与 marker 套件再裁决。

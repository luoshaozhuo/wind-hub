# B3 第三轮系统级集中复测与收口报告（2026-11-20）

- 分支：`feat/config-businesspoint-alignment-20261118`
- 复测基线 HEAD：`eec495b5cadc4579d27f3b23692c8ba137d1d83d`（ChatGPT Harness token 化整改，20 提交）
- 工作约束：原分支集中工作，未新建分支/PR、未合并 main、未 rebase/force push、未恢复 mTLS/单点编码器/v2 兼容路径

## 1. 总结论：**条件性准出——已实现功能的单元/组件/契约/多数系统链路达标；存在 1 项生产传输架构 P1 阻断 Gateway Sink 端到端**

- 五项静态门禁全 0；全仓 3606 测试 0 collection errors；权威 marker **2593 passed / 0 failed**（较上轮 2582 增加 11 个新模块测试）。
- Sink 37 项系统测试中 **34 项实测通过**（真实 Docker R5 栈 + 真实三进程 + Bearer 拦截器）；剩余 S4 backlog 与 600s 容量长测按环境/时长口径 NOT_RUN。
- 阻断项：**Gateway Sink 传输架构冲突（P1）**——生产 `GatewayOutput` 仅支持明文 loopback 通道，而 r1f3/full_chain 的 Gateway 服务端强制 mTLS，二者在同一端口无法共存，导致 `gateway_output` Delivery 与全部依赖该 Sink 的 full_chain E2E 启动失败。这是生产接口/安全架构冲突，按职责保留证据交 ChatGPT 决策，未擅自改安全边界。

## 2. 五项静态门禁（实际执行）

| 门禁 | 结果 |
|---|---|
| `generate_rpc.py --check` | **0** |
| `ruff check src tests scripts` | **0**（修复 3 文件格式） |
| `ruff format --check` | **0** |
| `mypy src`（446 文件） | **0** |
| `lint-imports`（19 契约） | **0** |
| 全仓 collection | **0 ERROR，3606 collected** |

## 3. SinkSystemHarness 专项复测（任务 4，真实 gRPC + 生产拦截器）

`test_grpc_api.py` 4 项 + 三进程生命周期实测通过，逐项覆盖：
- 全权限 caller 可 query/control/config；query-only caller 被拒 control/config。
- 未注册 Bearer → PERMISSION_DENIED；缺失/重复 Authorization → UNAUTHENTICATED；错误 token → PERMISSION_DENIED；未知 RPC → PERMISSION_DENIED。
- 每实例独立 token（auth_tokens admin/readonly），子进程经环境变量 `WIND_HUB_SINK_*_TOKEN` 注入，gRPC metadata 附加 Bearer。
- sigkill/sigstop/主进程重启下无进程泄漏、无重复 durable 所有权（`test_lifecycle.py` 5 项）。

### 3.1 34 项系统测试逐项结果（均真实中间件，非 mock）

| 文件 | 项数 | 结果 |
|---|---|---|
| test_grpc_api | 4 | PASS |
| test_lifecycle | 5 | PASS |
| test_delivery_flow（Redis realtime + Kafka reliable 实际投递） | 3 | PASS |
| test_dlq_recovery | 4 | PASS |
| test_concurrent_delivery_e2e | 6 | PASS |
| test_kafka_partition_cache_e2e（真实 Kafka 分区/扩容/重启） | 3 | PASS（322s） |
| test_uc09_s03_config_port_restart | 3 | PASS（含本轮修复的端口冲突用例） |
| test_uc10_s03_version_ownership | 2 | PASS |
| test_uc11_s02_shared_sink_isolation | 1 | PASS |
| test_realtime_expiry_e2e | 1 | PASS |
| test_replay_header_ack_bypass_e2e | 1 | PASS |
| test_replay_starvation_e2e | 1 | PASS |
| **小计** | **34** | **PASS** |
| test_concurrent_throughput_s4::fresh | 1 节点（12 参数） | PASS（99s） |
| test_concurrent_throughput_s4::backlog | 1 | NOT_RUN（容量基准，本轮未排期） |
| test_capacity_r14_sustained | 1 | NOT_RUN（固定 600s 长稳，无缩短开关） |

本轮修复 1 个真实测试缺陷：第二实例子进程缺 `WIND_HUB_SINK_*_TOKEN` 环境变量，在 bind 前即失败；补传后正确走到“端口占用”fail-closed 路径。

## 4. P1 阻断项：Gateway Sink 传输架构冲突

### 证据链
1. 生产 `src/sink_service/infrastructure/adapters/outbound/gateway.py:59` 硬编码 `grpc.aio.insecure_channel`；提交 `ff5374ca7` 已删除 Gateway mTLS 通道与凭证契约。
2. 生产 Gateway 服务端 `data_server.py:817-834` 单端口二选一：`tls.enabled` 为真走 `add_secure_port`（require_client_auth），否则仅放行 loopback 明文。
3. r1f3（`write_config_tree(mtls_enabled=True)`）与 full_chain（conftest `grpc.tls.enabled=True`）都让 Gateway 监听 mTLS；Sink GatewayOutput 发明文连接被拒，sink 进程崩溃。
4. 实测复现：
   - `test_e2e_sink_outputs` setup 失败 `service start not confirmed`，sink.log：`ConfigurationError: sinks.gateway_sink.connection: unknown=['credential_ref']`（已修）后仍因 mTLS 握手 UNAVAILABLE 崩溃。
   - r1f3 `gateway_output` Delivery actual_state=FAILED：`failed to connect ... Connection refused`。
5. 设计书《Sink_Service》§4011 明确“内部 gRPC 不启用 TLS/客户端证书，受控隔离网络 + 受控令牌”，但 Industrial Gateway 当前仍以 mTLS 为默认，CLI Runtime 也仅豁免 configuration/sink 的 tls_profile（`runtime.py:222`），未含 gateway。

### 影响面
- `test_r1f3_strict_cli_e2e`（六服务严格 E2E）失败。
- full_chain 共享 conftest 的 sink 启动连带失败，13 个引用 gateway 的 E2E 文件受影响。
- 另发现并已修的测试残留：r1f3 与 full_chain 仍传已退役的 `credential_ref`/`WH_*_GATEWAY_TLS` 字段（full_chain 两处已移除）。

### 裁决选项（须 ChatGPT 定，属安全架构）
- (a) Gateway 内部管理面整体去 mTLS、改受控令牌（与 Sink/Configuration 对齐），CLI/DG/Sink 统一明文 loopback + token；或
- (b) Gateway 提供双端口（mTLS 管理面 + loopback 明文 Sink 面），GatewayOutput 维持现状。
验收方未擅自在 `runtime.py` 扩大内部豁免集，避免降低既有 mTLS 安全边界。

## 5. 新增模块补测（任务 7）

- 本轮新增 `test_historical_point_table_client.py`（11 项）：env token 引用、空 endpoint/manifest、非 SHA-256 digest、未批准 PointTable 在网络调用前拒绝、缓存键按 (point_table, device_type) 隔离、128 容量上限。
- 既有覆盖（上轮）：business_projection 15、historical_bundle_codec 8、grpc_auth 11、delivery_admission 5；本模块纯函数/拦截器层正反路径齐备。
- 仍缺：ConfigurationSubscriber 的真实 Repository+NATS 集成（断线重连/轮询兜底/关闭收敛/CAS 冲突端到端）、HistoricalPointTableClient 的真实 gRPC 成功/超时/断网路径（当前仅构造与缓存守卫，真实链路依赖 P1 解决后的 Repository 环境）。

## 6. 真实链路与 Kafka PROTOCOL/BUSINESS（任务 7.3、9）

- PROTOCOL 实际投递：`test_delivery_flow::reliable_batch_reaches_kafka` 真实 JetStream→Kafka，校验消息内容与 ACK；分区缓存 3 项验证真实 Broker 分区语义。
- BUSINESS 投影：单测覆盖 scale/offset/单位/int64/uint64/大整数拒绝；**真实 Kafka BUSINESS 端到端投递**依赖历史 PointTable 闭包经 gRPC 拉取，受 P1 同一 Repository/Gateway 通道治理影响，列为待补，未以编码器单测冒充端到端证据。
- Configuration→NATS 通知：`test_revision_notification` 真实 NATS PASS。
- JetStream 深度 10 项：`WINDHUB_NATS_SERVER_BIN` 未设置（环境无独立 nats-server 二进制，硬性 skip 条件），NOT_RUN；R5 Docker 栈上的 JetStream ACK/DLQ 已由 test_dlq_recovery 等覆盖。

## 7. 缺陷台账

- **P0**：无。
- **P1**：Gateway Sink 传输架构冲突（见 §4），阻断 gateway_output 与相关 E2E。
- **P2**：
  1. S4 backlog 与 R14 600s 容量长测未在本轮执行（时长/基准口径）。
  2. ConfigurationSubscriber / HistoricalPointTableClient 缺真实 gRPC+NATS 集成测试。
  3. r1f3 仍含 `credential_ref` 等已退役字段（full_chain 已清，r1f3 因 P1 未解未继续改）。
  4. 多处 `PytestUnknownMarkWarning`（nats/kafka/failure 等 mark 未注册），不影响结果，建议在 pyproject 注册。

## 8. NOT_RUN（未记为 PASS）

- r1f3 六服务 E2E、13 个 gateway 相关 full_chain E2E（P1 阻断）。
- S4 backlog、R14 600s 容量长稳。
- 10 个 WINDHUB_NATS_SERVER_BIN JetStream 深测（环境无二进制）。
- 硬件/PLC/netem/root、iSulad/containerd、多机、TDengine 端到端持久化。

## 9. RAW 能力边界

RAW 原始报文字节链路**仍未实现且持续被禁止**：`kafka_wire._require_protocol("raw")` 直接 raise，SampleBatch v3 不含原始帧；无任何把解码标量重命名为 RAW 的路径。须以独立原始帧采集链路交付，不得重命名关闭。

## 10. 准出建议

- **单元/组件/契约层（权威 marker 2593/0/0、五项门禁全绿、34 项 Sink 系统测试通过）建议准出。**
- **系统级全链路准出暂缓**，前置：ChatGPT 就 §4 P1 给出架构决策并修复 Gateway 传输，随后恢复 r1f3 与 full_chain gateway E2E、补齐 ConfigurationSubscriber/HistoricalPointTableClient 真实集成测试与 Kafka BUSINESS 端到端证据。
- 最终是否准出、是否合并 main，由 ChatGPT 根据本报告裁决。

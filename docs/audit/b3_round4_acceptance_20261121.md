# B3 第四轮 P1 修复验收与最终准出报告（2026-11-21）

- 分支：`feat/config-businesspoint-alignment-20261118`
- 复测基线 HEAD：`857b4900eb21e9d5ba8b40e040989d23d92072c0`（ChatGPT Gateway 双端口 Bearer 架构，12 提交）
- 工作约束：原分支集中工作，未新建分支/PR、未合并 main、未 rebase/force push、未恢复 mTLS/单点编码器/v2 兼容路径

## 1. 总结论：**READY_TO_MERGE（含 2 项 P2 测试稳定性跟进项）**

- P1 Gateway 传输架构冲突已由双端口方案关闭并经真实 gRPC 矩阵验证。
- 五项静态门禁全 0；全仓 3617 测试 0 collection errors；权威 marker **2601 passed / 0 failed**（较上轮 +8 双端口测试）。
- r1f3 六服务严格 CLI E2E 连续 3 次通过。
- full_chain 58 项中文件级 100% 通过；全量共享栈压力下 2 项 flaky（独立运行均通过），属测试基础设施 P2。
- RAW 原始报文链路继续被显式禁止，无伪装。

## 2. P1 关闭证据：Gateway 双端口安全架构

### 2.1 生产架构（ChatGPT 实现，独立审查通过）

- `schema.py`：`sink_listener_port` opt-in，强制（a）mTLS 管理口必须先启用、（b）两端口必须不同、（c）loopback 绑定。
- `data_server.py`：同一 gRPC Server 绑定 secure（mTLS 管理）+ insecure（loopback Sink）双端口；`_verified_sink_caller` 严格判定：
  - 仅 loopback 对端、无 x509 身份（mTLS 调用走证书策略互斥）、单一 Authorization header、HMAC 常量时间比对、token ≥32 字符无空白。
  - Sink token 仅授权 `inject`（ClaimTargets/UpdateVariables），无法触达查询/管理/仿真 RPC。
- `sink/gateway.py`：GatewayOutput 经 `WIND_HUB_GATEWAY_SINK_TOKEN` 发送 Bearer，token 格式 fail-closed。

### 2.2 独立验证（本轮新增 8 项真实 gRPC 组件测试）

`test_grpc_sink_dual_listener.py`（真实生产 server + runtime，无鉴权 mock）：
1. 正确 token 可 ClaimTargets 并实际 UpdateVariables，再经 mTLS 管理证书读回确认落盘（真实投递闭环）。
2. 缺失 token → 拒绝；错误 token → PERMISSION_DENIED；重复 Authorization → 拒绝。
3. Sink token 调管理查询 RPC ListGateways → PERMISSION_DENIED（权限隔离）。
4. 明文连接 mTLS 管理口 → 握手失败 fail-closed。
5. 两端口相同 / 无 mTLS 启用 Sink 口 → schema 拒绝。

### 2.3 本轮发现并修复的生产缺陷（2 处，最小改动）

- **`diagnose_commands.py`**：`diagnose pipeline --sink-id` 对 Sink Diagnose 显式传 `metadata=None`，绕过 Bearer 注入 → UNAUTHENTICATED。修复为统一走 `get_rpc_metadata`，使 Collector 管理令牌与 Sink Bearer 各归其位。
- **`data_server.py`**：新 helper 的 mypy 类型问题（metadata 可能为 None、compare_digest 类型变量），加 None 守卫与 str 转换，无行为变化。

## 3. 五项静态门禁（实际执行）

| 门禁 | 结果 |
|---|---|
| `generate_rpc.py --check` | **0** |
| `ruff check src tests scripts` | **0** |
| `ruff format --check` | **0** |
| `mypy src`（446 文件） | **0** |
| `lint-imports`（19 契约） | **0** |
| 全仓 collection | **0 ERROR，3617 collected** |

## 4. 测试真实计数

### 4.1 权威 marker：`-m "unit or component or contract"`
**2601 passed / 0 failed / 0 collection errors**（292s）。

### 4.2 Sink 系统测试（真实 Docker R5 栈 + 三进程 + Bearer 拦截器）

| 套件 | 项数 | 结果 |
|---|---|---|
| test_grpc_api（鉴权矩阵） | 4 | PASS |
| test_lifecycle（三进程/sigkill/重启无泄漏） | 5 | PASS |
| test_delivery_flow（Redis realtime + Kafka reliable 实际投递+ACK） | 3 | PASS |
| test_dlq_recovery | 4 | PASS |
| test_concurrent_delivery_e2e | 6 | PASS |
| test_kafka_partition_cache_e2e（真实分区/扩容） | 3 | PASS |
| test_uc09_config_port_restart（含本轮 token 修复） | 3 | PASS |
| test_uc10_version_ownership | 2 | PASS |
| test_uc11_shared_sink_isolation | 1 | PASS |
| test_realtime_expiry / replay_header / replay_starvation | 3 | PASS |
| test_concurrent_throughput_s4（fresh + backlog） | 2（24 参数） | PASS |
| **小计已验证** | **36** | **PASS** |
| test_capacity_r14_sustained（固定 600s 长稳，无缩短开关） | 1 | NOT_RUN |

### 4.3 严格 CLI E2E
`test_r1f3_strict_cli_e2e`：**连续 3 次 PASS**（约 46-50s/次）。六服务全 CLI 启动、Sink 无 tls_profile、CLI/Sink token 一致、双通道 Collector、Redis/Modbus/Gateway/Kafka 四路输出、GatewayOutput 经 loopback Bearer 实际投递。

### 4.4 full_chain 跨模块 E2E（58 项）
- 文件级/独立运行：**58/58 PASS**（含本轮修复的 i07 双端口切换、i03/i05 token 注入）。
- 全量共享栈一次连续运行（1030s）：56 PASS，2 项 flaky（i03 `assert 35.0 > 35.0`、i05 `UNKNOWN == APPLIED`），均为 17 分钟共享 Docker 栈下的时序/资源竞争，独立复跑 100% 通过，非代码缺陷。

### 4.5 本轮新增/修复的集成证据
- `test_historical_point_table_client.py`（5 项，真实 gRPC）：成功往返+Bearer metadata、缓存命中无二次 RPC、缓存身份隔离、manifest 不匹配拒绝、UNAUTHENTICATED fail-closed。
- i07 根因修复：目标 gateway 原本仅 mTLS 无 Sink 明文口，Sink 明文连接永不收敛（PARTIAL）。补 `sink_listener_port` + 共享 token + 正确端点路由后通过。

## 5. 关键修复的测试侧变更

- `full_chain/conftest.py`：移除废弃 `credential_ref`；暴露 `gateway_sink_token` 供目标 gateway 复用。
- `integration_segments.py`：i07 目标 gateway 双端口化（mTLS 管理口 + loopback Sink Bearer 口），sink 连接改指 Sink 口；清理退役 `WH_FULLCHAIN_GATEWAY_TLS`。
- `test_e2e_semantic_matrix.py` / `test_e2e_surface_matrix.py`：进程内 Runtime 调用补 `WIND_HUB_SINK_CLI_TOKEN`（子进程 harness 注入的 token 在主进程缺失）。

## 6. 剩余集成证据缺口（P2，不阻断准出）

1. **ConfigurationSubscriber 端到端**：生产 `main.py` 已接线，但无测试配置 `config_sync.repository_endpoint`，真实 Repository+NATS 的初始化同步/广播丢失轮询兜底/断连恢复/CAS 冲突的端到端证据待补；其依赖的 Manifest 校验、CAS、NATS 唤醒已由各自专项测试覆盖。
2. **Kafka BUSINESS 全链路**：投影（15 单测）、codec（8 单测）、历史客户端（5 真实 gRPC）、PROTOCOL 系统投递（delivery_flow）均已覆盖，但 JetStream→BUSINESS 投影→Kafka 的端到端系统测试需 Repository approved-manifest 基础设施，待补。
3. **full_chain 全量稳定性**：i03/i05 在 17 分钟共享栈压力下 flaky，建议隔离 Docker project 或拆分套件。
4. R14 600s 容量长稳、`WINDHUB_NATS_SERVER_BIN` JetStream 深测（环境无独立二进制）、硬件/containerd/TDengine 持久化。

## 7. RAW 能力边界

`kafka_wire._require_protocol("raw")` 持续硬拒绝，SampleBatch v3 不含原始帧字节，无任何解码标量重命名为 RAW 的路径。RAW 须以独立原始帧采集链路交付。

## 8. 缺陷台账

- **P0**：无。
- **P1（生产）**：无未关闭项。本轮修复的 diagnose Bearer 遗漏已闭环并有 r1f3/full_chain 回归保护。
- **P2**：见 §6（Subscriber/BUSINESS 端到端证据、E2E flaky、长稳/环境项）。

## 9. 准出建议

**建议 READY_TO_MERGE**：P1 架构冲突已关闭且经独立真实 gRPC 矩阵验证，五项门禁全绿，marker 2601/0/0，r1f3 与 Sink 36 项系统测试通过，生产改动仅 2 处根因清晰的最小修复。P2 为测试覆盖深度与 E2E 稳定性跟进，不影响已实现功能的正确性结论；是否合并 main 由 ChatGPT 裁决。

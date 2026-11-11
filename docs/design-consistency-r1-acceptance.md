# R1 设计—契约一致性整改 · 独立验收报告

- 验收分支：`audit/design-contract-consistency-20261111`
- 整改 HEAD（受测对象）：`10a21f7a622eb51ec8182928457937bb70145c42`
- 基线：`origin/main @ 567e30ffb1aa1c1a519c0156991ca22811f389cf`
- 验收方测试提交：`0e5adc29`（仅 `tests/`，零生产/Proto/设计规范性改动）
- 验收日期：2026-11-11
- 验收范围调整：按委托方中期指示，本轮**不执行多个 wind-hub 业务模块之间的真实集成测试**；跨模块调用以契约一致的 Stub/Fake/受控测试服务替代，跨业务模块真实集成统一标记 **DEFERRED**（不判 FAIL，亦不声称通过）。

## 0. 总体结论

| 裁决项 | 结论 |
|---|---|
| 设计间一致性 | **PASS（含 3 项 P2/P3 文档勘误，不构成公共契约冲突）** |
| 各模块独立符合性 | **PASS** |
| 静态门禁 | **PASS（1 项 UNRELATED 基线红灯、1 项 UNRELATED 环境缺包，均与本分支同态于 main）** |
| 跨模块真实集成 | **DEFERRED**（清单见 §6） |
| 总体裁决 | **PASS WITH LIMITATIONS** |
| 合并建议 | **建议有条件 Squash Merge 到 main**；三项实质裁决全部满足，合并阻断项为 0；3 项文档交叉引用勘误可合并后由文档责任人修正，不阻断。 |

三项 P1 全部在真实组件/真实进程/真实消息中间件下闭环（见 §3）。验收方未修改任何 `src/` 生产代码、Proto、Core/Configuration 公共契约或十份设计的规范性内容。

---

## 1. 受测对象与改动面

### 1.1 生产代码改动（相对 main，仅 3 文件，+21/−4）

```
src/industrial_gateway/infrastructure/grpc/data_server.py |  6 +++++-
src/industrial_gateway/infrastructure/runtime.py          |  5 ++++-
src/industrial_gateway/infrastructure/variable_leases.py | 14 ++++++++++++--
```

- `variable_leases.py`：`ClaimRequest` 新增内部字段 `principal`；`TargetLease` 绑定认证主体；空白主体 `INVALID_CLAIM`；`RENEW/RELEASE` 主体不符 → `LEASE_LOST`；`update(..., principal)` 主体/令牌/变量集合围栏，失败抛 `LeaseError("LEASE_LOST")`。
- `runtime.py`：`update_variables(..., principal)` 透传主体。
- `data_server.py`：`_trusted_identity()` 取 mTLS 证书 CN（明文 loopback 降级为 `local-loopback`）；`ClaimTargets/UpdateVariables` 注入可信主体并做 `inject` 授权；`LeaseError→FAILED_PRECONDITION`、`KeyError→NOT_FOUND`、`ValueError→INVALID_ARGUMENT`。
- **Proto 零 diff**（`git diff origin/main...HEAD -- src/contracts/` 为空）：`principal` 是服务端内部数据类字段，不增改任何线协议字段，符合《industrial_gateway v1_27》L815「不增改任何 Proto 字段」。

### 1.2 设计文档改动

旧版本文档已删除，`docs/design/` 恰为 10 份现行设计，代码围栏配平，无规范性 `--values` 残留：

| 模块 | 现行版本 |
|---|---|
| 总体逻辑架构 | v1_4 |
| Contracts | v1_20 |
| Core | v1_33 |
| Configuration | v1_15 |
| Collector | v1_20 |
| Commander | v1_30 |
| Industrial Gateway | v1_27 |
| Data Generator | v1_23 |
| Sink Service | v1_50 |
| CLI | v1_26 |

### 1.3 验收方新增/修改测试（提交 0e5adc29，全部绿）

| 文件 | 标记 | 用例数 | 验证对象 |
|---|---|---|---|
| `tests/industrial_gateway/test_variable_leases.py`（改） | unit | 10 | 含 4 个主体围栏新用例 |
| `tests/industrial_gateway/component/test_lease_principal_isolation_real_mtls.py`（新） | component/real_service/safety | 5 | 真实生产 Gateway + 双 CN mTLS |
| `tests/cli/test_gateway_real_gateway_e2e.py`（新） | system/contract/real_service | 12 | 真实 CLI 子进程 × 真实生产 Gateway |
| `tests/cli/test_gateway_e2e.py`（改） | — | +1 | RELEASE 未确认不得报成功 |
| `tests/sink_service/integration/test_reliable_durable_ack_real_nats.py`（新） | integration/reliability/real_service | 7 | 真实 NATS JetStream Durable 独立 ACK |

未降低任何断言、未加无依据 skip/xfail/noqa/ignore；NATS 用例以 `WINDHUB_NATS_SERVER_BIN` 环境变量门控，二进制缺失即 `skip`（NOT_RUN），不 mock Broker 语义。

---

## 2. S1 设计一致性核对（diff 矩阵）

核对方法：以 `src/contracts/*.proto` 描述符（RPC/消息/字段号/类型/ presence/绑定）为权威线协议，逐项比对设计的请求/响应**与运行时语义**，并比对 Core/Configuration 公共 API 与六业务模块的注册/客户端/配置/异常。

### 2.1 闭合的既有问题

| 编号 | 结论 | 证据 |
|---|---|---|
| 原 P2-01（Configuration 误引 §14.5） | **CLOSED** | 全文已无「14.5」，改为 §14.2，目标 L1067 存在 |
| 原 P2-02（Collector 误引 §12.4） | **CLOSED** | 全文已无「12.4」，改为第 13 章，L3489 存在 |
| 旧版文档残留 | **CLOSED** | 仅 10 份现行文档，旧 v1_49/v1_25 等已删 |
| Durable 独立 ACK 语义 | 一致 | Sink v1_50 R02-02/R02-05、Contracts v1_20、架构 v1_4 与生产 `reliable_runtime.py` 一致 |
| CLI 冻结接口 | 一致 | CLI v1_26 L2172 与 `plant_commands.py` 实现逐字对应 |
| Gateway 主体围栏 | 一致 | Gateway v1_27 L815/L1915 与生产及新测试逐条对应 |

### 2.2 本轮新发现的残留缺陷（验收方无权改设计规范性内容，移交文档责任人）

| 编号 | 设计文件/位置 | 不一致内容 | 严重程度 | 建议处置 |
|---|---|---|---|---|
| D-01 | `CLI_软件设计_v1_26.md` L638、L1753 | 两处仍称《Contracts 软件设计 **v1_18**》，但同文档 L952/L1769/L1835/L1839/L1926/L2535 均为 **v1_20**；现行 Proto 权威为 v1_20。文档自相矛盾且引用已废弃版本 | P2（文档交叉引用，不改任何绑定契约；Proto 与多数引用权威正确） | 将 L638、L1753 的 v1_18 改为 v1_20 |
| D-02 | `Core_软件设计_v1_33.md` L1471 | 上位依据写成 `wind-hub_总体逻辑架构设计_v1_0.md`，现行总体架构为 **v1_4**（v1_0 文件已不存在） | P2（文档悬空文件名） | 改为 `wind-hub_总体逻辑架构设计_v1_4.md` |
| D-03 | `Contracts_软件设计_v1_20.md` L630 | 称测试矩阵在「**第 14 章**」，但本文档仅 12 章（实际测试矩阵在第 11/12 章）。注：其余 8 份业务文档确有第 14 章，其 ch14 引用合法；悬空为 Contracts 独有 | P3（章节号悬空，模板套用瑕疵） | 改为实际承载测试矩阵的章节号 |

> 三项均为**编辑性交叉引用缺陷**，不产生重复或冲突的公共契约，不影响 Proto/Schema/公共 API 的唯一权威性，故不构成合并阻断；但须登记并由文档责任人修正。

### 2.3 模块独立符合性要点

- **Proto 描述符**：6 份 `*.proto` 的 RPC/消息/字段号与 Contracts v1_20 及各模块设计一致；`generate_rpc.py --check` 退出 0，工作树无生成物漂移。
- **Industrial Gateway**：`ClaimTargetsRequest` 6 字段、`UpdateVariablesRequest` 3 字段与设计 L1667/L1696 一致；主体来自证书 CN，`owner_id` 仅业务标识不可接管租约（L815）；TTL 1000–60000ms、目标上限 5000、重叠互斥、全成全败均在生产与测试中实证。
- **CLI**：`gateway update GATEWAY_ID ID=JSON_VALUE... --owner-id ... [--lease-ms 1000..60000] --yes`，必填 `--owner-id`、无 `--values`、重复 ID/非法 JSON/越界 lease-ms 本地退出 2；退出码表 0/1/2/3/4/130（L2310）与 `main.py` 映射一致；UNKNOWN「do not retry」、RELEASE 未确认不报成功。
- **Sink Service**：确定性 `_durable_id="WH_SINK_"+sha256(deployment:delivery)[:24]`，每 Delivery 独立 Durable、`max_ack_pending=1`、Limits/discard=new/有限容量启动校验、仅终局 `ack_sync`、DLQ PubAck 先于 ACK，与 R02 系列一致。
- **Data Generator**：使用 application 层 `target_leases.TargetLeaseRegistry`（独立于本次改动的 infrastructure `variable_leases`），owner_id 语义回归全绿。
- **Core/Configuration/Collector/Commander**：本分支未改其 `src/`，模块内测试与 main 基线同态（见 §4）。

---

## 3. S2 三项 P1 复测（真实组件/进程/中间件）

### P1-01 Sink 独立 Durable ACK（真实 NATS JetStream）— **CLOSED / PASS**

- 环境：离线静态 `nats-server v2.10.27`（文件存储），由 `WINDHUB_NATS_SERVER_BIN` 门控；目标 Sink 用契约一致的 `FakeKafkaAdapter/FakePool`，不引入真实 Collector/Kafka。
- 关键 Broker 实证（独立 NATS 连接读取，非 mock 计数）：
  - Limits 保留策略下 **ACK 不删除 Stream 消息**，结算权威判据为 consumer `num_pending==0 且 num_ack_pending==0`；
  - `num_redelivered` 非累计（NAK 后停留 0/1），重投权威计数为 `info.delivered.consumer_seq`（首投=1，每次重投 +1）；
  - 失效批次先 DLQ PubAck 再 ACK；DLQ 运行期不可用时保留责任、恢复后补发；stop/restart 同 Durable 继承未 ACK；主体/主题不匹配入 DLQ 而非零匹配跳过；合法零匹配安全跳过且 ACK、无 DLQ。
- 7 用例：带二进制 **7 passed（约 107s）**；无二进制 **7 skipped（NOT_RUN）**。文件 `test_reliable_durable_ack_real_nats.py`。

### P1-02 CLI gateway update（真实 CLI 进程 × 真实生产 Gateway gRPC/mTLS）— **CLOSED / PASS**

- 对端为生产 `GatewayGrpcService + GatewayRuntime`（`start_gateway_v1` 不替换生产对象），CLI 以独立解释器子进程 `python -m cli.main` 运行。
- 12 用例：`--help` 含 `--owner-id`/`variable_id=typed_value` 且无 `--values`；ACQUIRE→update→RELEASE 全闭环并真实落库（42、1.5），释放后可被另一主体再 ACQUIRE；7 项本地参数错误退出 2 且无写入；只读证书 PERMISSION_DENIED；租约冲突 `TARGET_CLAIMED` 退出 2；未知 Gateway 退出 1。
- 另在 FakeRpcServer 套件补 1 例：RELEASE 返回 `accepted=False` 时退出 1、不得报成功。
- 关键工程坑：生产 grpc.aio 服务器与测试共用事件循环，阻塞式 `subprocess.run` 会饿死循环造成假性 DEADLINE_EXCEEDED；已改用异步 `create_subprocess_exec`。
- 合计 13 用例全部 PASS。

### P1-03 Gateway 租约认证主体隔离（同 CA 双 CN）— **CLOSED / PASS**

- 两个同测试 CA 真实签发、均持 `inject` 权限的身份 `gw-injector-a/gw-injector-b`，外加只读 `gw-reader` 与引导 `gw-admin`。
- 5 个真实 mTLS component 用例：A 全生命周期成功，B 持相同 owner/集合/token 的 RENEW/RELEASE 均 `LEASE_LOST`、update `FAILED_PRECONDITION` 且值不变，A 租约不受破坏；只读身份 RPC 边界 `PERMISSION_DENIED`；过期 token 连原主体也被围栏；生产 `invalidate()` 使旧 token 失效；并发 ACQUIRE 仅一主体胜出、输家 update 被围栏、最终存储仅赢家值（11，输家 22 不落库）。
- 4 个单元用例补齐：空白主体 `INVALID_CLAIM`、RENEW/RELEASE 主体校验、异主体 update 围栏且存储不变、同 owner_id 异主体不能共享/劫持租约。
- 合计 9 用例全部 PASS。

---

## 4. S3 静态门禁与模块内回归（实跑命令与计数）

### 4.1 静态门禁

| 门禁 | 命令 | 结果 | 裁决 |
|---|---|---|---|
| Ruff check（验收方 5 文件） | `ruff check <5 files>` | `All checks passed!`，exit 0 | PASS |
| Ruff check（生产） | `ruff check src` | `All checks passed!`，exit 0 | PASS |
| Ruff format（验收方 5 文件） | `ruff format --check <5 files>` | `5 files already formatted`，exit 0 | PASS |
| Ruff format（src 漂移） | `ruff format --check src` | 分支 0 = main 0 | PASS（无新增漂移） |
| Proto 生成一致性 | `python scripts/generate_rpc.py --check` | exit 0，工作树 `contracts/` 无 diff | PASS |
| import-linter | `lint-imports` | **15 kept, 0 broken**，exit 0 | PASS |
| mypy（src） | `mypy src` | 327 文件，仅 5 错，全在 `core/.../tracing.py` 的 `opentelemetry.sdk.*` import-not-found | **UNRELATED**（见下） |

- mypy 5 错为环境仅装 `opentelemetry-api 1.45.1`、未装 SDK；`tracing.py` 与 main `git diff` 0 行，属 main 既有环境/基线问题。**本次改动的 3 个生产文件 mypy 零错误。**

### 4.2 模块内测试（`--import-mode=importlib -m "not hardware and not performance and not soak"`）

| 模块目录 | 结果 | 备注 |
|---|---|---|
| `tests/industrial_gateway` + `tests/cli`（合跑） | **343 passed, 18 skipped, 1 deselected** | 含验收方 42 用例（10+5+12+15） |
| `tests/sink_service`（unit/component/integration）¹ | **797 passed, 8 deselected** | 含真实 NATS 7 用例 |
| `tests/data_generator` | **156 passed, 16 deselected** | owner_id 语义回归通过 |
| `tests/core` | 508 passed, **1 failed**, 7 deselected | 失败为 UNRELATED（见下） |
| `tests/collector` | **96 passed, 46 skipped, 2 deselected** | — |
| `tests/commander` | **238 passed, 16 skipped, 4 deselected** | — |

¹ Sink 收集期 3 个收集错误（`test_redis_batch_contract.py`、`unit/test_kafka_attempt_counters.py`、`unit/test_kafka_encoded_batch.py`、`system/`）依赖**未被 git 跟踪的缺失源文件** `tests/fixtures/services/r5_stack.py` 与 `tests/src/unit/test_b2_batch_builder.py`（仅存过时 `.pyc`）。经 `git cat-file -e` 证实这两个文件在 `origin/main` 与本分支 HEAD **均不存在**，属 main 既有夹具缺口，与本分支无关，已按 UNRELATED 排除后跑其余可收集部分；`tests/sink_service/system`（17 文件，docker/r5 栈）整体 NOT_RUN。

#### UNRELATED 基线红灯（core）

- `tests/core/architecture/test_rpc_boundary.py::test_pb2_only_in_allowed_zones`：offender 为 `sink_service ... grpc_server.py` 在允许区外引用 `_pb2`。在独立 main worktree 复跑**完全相同失败**（即记忆在案的 rpc_boundary pb2 融合债）。本分支未触碰 sink_service/contracts，判定 **UNRELATED**，不为全绿扩大范围。

---

## 5. NOT_RUN 清单（环境/硬件缺口，未声称通过）

- 真实硬件/PLC（TwinCAT、真实 Modbus/IEC104 现场链路）。
- `netem` 网络损伤全栈、多机部署、长稳 soak、performance 容量门（按 marker 统一 deselect）。
- 基于 docker 的 Sink system 套件（TDengine/Kafka/Redis 真实容器；`r5_stack.py` 缺失 + 既往 6379 端口环境冲突）。
- NATS 用例在未提供 `WINDHUB_NATS_SERVER_BIN` 的环境下 7 项 skip（本轮验收机已提供二进制并真实跑通）。

## 6. DEFERRED 跨业务模块真实集成清单（按委托方指示，本轮不执行、不判 FAIL、不声称通过）

1. 真实 Collector → JetStream → 真实 Sink 交付/DLQ/重投端到端（Collector+Sink 联合）。
2. Data Generator → Industrial Gateway → Commander 现场控制联合链路。
3. 五服务 `test_grpc_contract_real` 全 mTLS 跨进程真实链路（本轮以受控 gRPC Server/真实单服务替代）。
4. Sink 可靠链路对接真实 Kafka/TDengine 的跨模块系统测试。
5. 跨模块进程重启/隔离/重复 clear 等多服务协同场景。

本轮各模块内部 Unit/Component/Integration/System 测试保持有效；System 测试不以真实拉起其他 wind-hub 模块为必要条件，跨模块边界以契约一致的 Stub/Fake/受控服务满足。

## 7. 合并建议与遗留项

- **建议：有条件 Squash Merge 到 main。** 三项必要裁决（设计间一致性、模块独立符合性、静态门禁）均满足，合并阻断项为 0。
- 合并前/后须处理的非阻断项：
  1. D-01/D-02/D-03 三项文档交叉引用勘误（移交文档责任人，验收方未改设计规范性内容）；
  2. UNRELATED 基线问题另行跟踪：`rpc_boundary` pb2 融合债、`r5_stack.py`/`test_b2_batch_builder.py` 夹具缺口、tracing.py opentelemetry-SDK mypy 环境缺包；
  3. §5/§6 的 NOT_RUN 与 DEFERRED 项在具备硬件/容器/多机环境后单独安排，不以本轮结果推定通过。
- 验收方未执行 rebase、force-push、合并或 PR 创建；仅在验收分支正常提交测试（0e5adc29）与本报告。

# D1 设计与源码一致性独立复测报告（2026-11-27）

## 1. 裁决总览

| 维度 | 裁决 | 说明 |
|---|---|---|
| STATIC_CONSISTENCY | **FAIL** | Ruff、mypy、import-linter、Proto 生成一致性均通过；但设计、配置和实现仍有 P1 契约冲突，静态门禁绿不等于设计一致。 |
| FUNCTIONAL_RELIABILITY | **FAIL** | Watcher 专项、消息/RPC 契约及局部控制测试通过；权限过宽、重放不稳定、确定拒绝误判 UNKNOWN、Web 客户端不可互操作等生产问题仍 OPEN。 |
| CAPACITY | **NOT_RUN** | 本 SHA 未执行容量、600 秒、长稳或 1000 台测试。既有容量 BLOCKED 结论不因本次功能测试通过而解除，门禁阈值未调整。 |
| DCEMS_E2E | **FAIL（测试现场）/ NOT_RUN（业务闭环）** | full-chain 控制在装配阶段因旧夹具报错；未执行三生产服务同场的完整 DCEMS 业务闭环。局部 RPC 和状态机证据不得替代 DCEMS PASS。 |
| PRODUCTION_READINESS | **BLOCKED** | 存在 OPEN P1，且容量与完整 DCEMS 资格未关闭。 |
| 总体 | **FAIL** | P0=0、OPEN P1=19、OPEN P2=12；设计和测试整改有效，但不能宣称 D1 全部一致或生产就绪。 |

- 交接基线：`bf68e6ec5fc59347dd71f9225daa25adbb77e5ae`，本地与远端一致，工作树干净。
- 最终测试源码/Fixture SHA：**`682b0b7cf5ad9ecad1fda20ddbfe76f2e56bceb1`**。本报告中的最终pytest和静态门禁日志均绑定此 SHA。
- 本报告和证据随后单独提交；最终分支 HEAD 以 Git 元数据与交付答复为准。报告提交不改动任何生产或测试源码。
- 分支保持 `feat/storage-wide-table-completion-20261125`；未创建分支、PR，未合并 main。
- 原始证据：[d1_design_source_independent_retest_20261127_evidence/](d1_design_source_independent_retest_20261127_evidence/)。

## 2. 环境、静态门禁与最终测试

环境：Python 3.11.16，`wind-hub` Conda 环境，WSL2。`python3 scripts/dev.py env` PASS。

| 检查 | 最终结果 | 证据 |
|---|---|---|
| Ruff `src tests scripts` | PASS | `static.log` |
| mypy `src` | PASS，494 source files | `static.log` |
| import-linter | PASS，22 kept / 0 broken | `static.log` |
| `scripts/generate_rpc.py --check` | PASS，无生成漂移 | `static.log` |
| 修改测试文件 Ruff format | PASS，6 files | `static.log` |
| Storage unit + component | **289 passed** | `storage_unit_component.log` |
| Proto/RPC + SampleBatch + Kafka v3 | **265 passed** | `messages_rpc_contracts.log` |
| Commander/Gateway/Generator 局部控制链 | **76 passed** | `control_chain.log` |
| 三套活动发布根、候选、版本冲突、E2E 预检 | **108 passed** | `configuration_closure.log` |
| Core 配置完整目录补充回归 | **4 failed / 22 passed** | `core_configuration_remaining_failures.log` |

补充已执行：Sink unit/component 430 passed；Configuration unit/component 444 passed；Storage 部署流 2 passed、1 skipped（真实 NATS 广播环境未具备）。这些补充结果不替代上述最终 SHA 证据矩阵。

## 3. 本次完成的测试与 Fixture 修复

仅修改测试和 Fixture，生产源码零改动。

1. **Watcher 专项扩展**：`tests/storage_service/unit/test_configuration_reconciliation.py:466-715`。
   - 不可变 DeviceModel 冲突同时断言 `snapshot.pending == []`，覆盖 RESTART_REQUIRED 路径不得写待重启快照。
   - 激活快照写失败保留旧 `_active/active_revision`，重试后才激活。
   - `refresh_now()` 和 `run()` 取消均传播 `CancelledError`。
   - NATS 正常停止和任务取消都关闭连接；连接失败及无广播 hint 时周期对账仍执行。
   - 原 9 项增至 **15 项，全部通过**。
2. **Repository 离线恢复**：新增 `tests/storage_service/component/test_offline_configuration_recovery.py:125-172`。真实快照/元数据与隔离的进程替身组合，证明 `ConnectionError` 时只恢复 SHA-256 校验通过的 accepted bundle，状态为 `CONFIG_RECONCILE_DEGRADED`，资源均关闭；**1/1 PASS**。
3. **版本化共享配置路径**：`test_source_manifest_and_closure.py`、`test_shared_config_immutability.py` 改用当前 Site/Device/Units/BusinessPoint 文件。前者与目录候选合计 **13/13 PASS**，后者 **9/9 PASS**。
4. **SampleBatch v4**：`tests/src/unit/test_contracts/test_sample_batch.py:49-82` 增加 `device_config_id`、批次 `sample_time`，版本断言与 identity 升级 v4。原 **44 failed / 21 passed**，修复后 **66/66 PASS**。
5. **当前 E2E 候选**：`tests/system/e2e/test_long_run_candidate_preflight.py:18,43-59` 从不存在的 Deployment v1.0.10 迁移到 v2.0.0；**4/4 PASS**。

未通过删测试、放宽断言、增加 skip/xfail 或修改容量阈值制造通过。

## 4. P0

**未发现可由当前证据支持的 P0。**

## 5. OPEN P1

### 5.1 生产、配置和测试现场

| ID | 位置与复现证据 | 影响与裁决 |
|---|---|---|
| P1-01 | `deploy/config_roots/sim-a/deployments/deployment_sim-a_v2.0.0.yaml:12-14` 选择 Collector 2.0.0；`deploy/config_roots/sim-a/services/service_cli-sim_v1.10.0.yaml:24-36` 仍启动 `service_collector-sim_v1.0.0.yaml`；`src/core/configuration/local_selection.py:134-142` 拒绝错版。`sim_a_selection_failure.log` 实际抛出 `Service file differs from selected Deployment version`。 | sim-a 按当前 CLI 无法启动所选 Collector。配置缺陷，交 ChatGPT；应新增 CLI 不可变后继版本，不改写历史版本。 |
| P1-02 | `src/data_generator/infrastructure/adapters/inbound/grpc_auth.py:29-37,64-68,100-103` 将 Start/Stop/UpdateVariables 合并为 `variables_update`；`configs/services/service_data-generator-sim_v1.3.1.yaml:39-46` 将该权限授予 Gateway。动态复现 `StartGeneration=OK`、`StopGeneration=OK`，见 `generation_permission_repro.log`。 | Gateway 可绕过 Commander 直接改变 Generation 生命周期，权限面过宽。生产权限缺陷，交 ChatGPT。 |
| P1-03 | Gateway 在 `src/industrial_gateway/infrastructure/adapters/inbound/grpc/data_server.py:465-488` 先做 revision/RUNNING/映射/期限校验，重放查找却在 `src/industrial_gateway/application/simulation.py:59-89`；Generator 在 `src/data_generator/application/runtime.py:233-251` 先要求 RUNNING 后查 history。 | 已受理请求在过期、停止或 revision 变化后重放会得到新拒绝，而非原 execution/effective_step，破坏跨层幂等和对账。静态确认，过期/停止后重放动态专项 **NOT_RUN**；交 ChatGPT。 |
| P1-04 | Gateway 对执行前确定拒绝使用 gRPC abort（`data_server.py:465-488`）；Commander `src/commander/infrastructure/adapters/outbound/gateway.py:93-113` 只处理响应体 REJECTED，异常经 `queued_executor.py:664-687,809-859` 进入 UNKNOWN/设备阻断。 | 权限、参数、revision、期限等确定未执行被误记为 UNKNOWN，与 `Commander_软件设计_v1_60.md:2287` 冲突。生产错误语义缺陷，交 ChatGPT。 |
| P1-05 | `tests/system/e2e/full_chain/conftest.py:212,740-747` 仍读取 `devices_sim.yaml` 并写旧 Commander 证书字段；`381-435` 仍写旧 Gateway/Generator TLS/permissions。实际执行 `test_e2e_04_ppc_control_applied[agc_enable-1]` 在 setup 报 `FileNotFoundError: configs/instances/devices_sim.yaml`。 | full-chain 当前不能作为 DCEMS 证据；后续旧安全字段仍未到达，属于继续迁移项。测试 Fixture 债务，OPEN。 |
| P1-06 | Core 配置目录仍有 4 个旧 schema Fixture 失败：`test_point_table_inheritance_frozen.py:226-293`、`test_reload_source_manifest.py:50-69` 派生的三个场景。最终日志为 4 failed / 22 passed。 | 旧 Gateway/Commander schema、安全字段和模板路径未迁移；不是本次生产回归，但对应测试 Gate FAIL，OPEN。 |
| P1-07 | Web `src/web_server/adapter/outbound/grpc/commander.py:55-83` 强制 mTLS，正式 Commander `src/commander/infrastructure/adapters/inbound/grpc_transport.py:88-92` 明文监听；Web 调用在 `commander.py:147-150,172,224-227,247,264-267` 未传 Bearer，服务端 `grpc/server.py:85-109` 强制鉴权。 | Web Commander 查询、诊断、控制在传输和鉴权两层均不可互操作。生产集成缺陷，交 ChatGPT。 |
| P1-08 | Storage 注册 CommonService：`src/storage_service/infrastructure/adapters/inbound/grpc_server.py:935-936`；GetConfigSnapshot/GetMetrics/GetEvents/Diagnose 在 `457-513` 显式 UNIMPLEMENTED；Contracts 通用能力承诺见 `docs/design/Contracts_软件设计_v1_48.md:793-805`。 | 通用 Storage 运维读取能力不可用。旧 Common push/apply 的拒绝是正确唯一发布边界，不计为缺陷。 |

### 5.2 正式 RPC 的 CLI 设计承诺缺口

| ID | 位置与复现证据 | 影响与裁决 |
|---|---|---|
| P1-09 | `docs/design/CLI_软件设计_v1_59.md:1895,1918,2646-2648` 承诺五服务 `monitor`；`src/cli/main.py:37-52` 未注册。实际 `monitor sink` 退出 2。 | 整个统一监视命令组缺失。 |
| P1-10 | `CLI_软件设计_v1_59.md:1784,1917,2576` 承诺 `gateway watch`；`src/cli/plant_commands.py:141-150` 只有单次 read。实际退出 2，见 `cli_missing_commands.log`。 | Gateway 周期监视不可用。 |
| P1-11 | Proto `src/contracts/configuration_repository.proto:177-184` 为 repeated roots；设计 `CLI_软件设计_v1_59.md:1747` 要求可重复 `--exact-root`；`src/cli/configuration_commands.py:312-368` 固定单根。 | 无法一次导出多根统一闭包/manifest。 |
| P1-12 | 设计 `CLI_软件设计_v1_59.md:2012,2019` 要求 Deployment 选版与 target/active 运营视图；`src/cli/configuration_commands.py:62-537` 无 `GetServiceConfigBundle` 调用。 | 内部消费方正常，但操作员无法通过 CLI 核验选定闭包。 |

### 5.3 设计文档与真实实现冲突

| ID | 位置与真实证据 | 影响与裁决 |
|---|---|---|
| P1-13 | `docs/design/Core_软件设计_v1_48.md:1514-1515` 的闭包摘要漏掉 deployment/service；真实 `src/core/configuration/deployment.py:143-151` 将两者写入摘要，`core/infrastructure/configuration_repository.py:126-128` 强制验证。 | 按文档实现的客户端会误拒合法 Bundle；同一文件闭包用于不同部署也会得到错误身份。文档 P1。 |
| P1-14 | `docs/design/Configuration_Service_软件设计_v1_26.md:1388` 写 ALREADY_EXISTS；真实 `repository_server.py:90-93` 和 `test_version_conflict.py:16-50` 为 FAILED_PRECONDITION。最终定向测试 1/1 PASS。 | 按文档处理错误码会误分类不可变冲突。文档 P1。 |
| P1-15 | `docs/design/Collector_软件设计_v1_47.md:3109`、`wind-hub_跨模块测试设计_v0_49.md:612` 仍写 SampleBatch v3；真实 `src/contracts/sample_batch.py:139,304-305` 严格 v4，Collector v4 测试 3/3、严格契约 66/66 PASS。 | 按旧 Golden Data 会被正式解码器拒绝。文档 P1。 |
| P1-16 | `wind-hub_跨模块测试设计_v0_49.md:396-402` 仍列 E2E 旧版本；活动 `deploy/config_roots/e2e-final/deployments/deployment_e2e-final_v2.0.0.yaml:10-31` 选择 Collector/Sink/Storage 2.0.0、CLI 1.1.8 等新闭包。 | 按表装配会选错文件并形成错误受测闭包。文档 P1。 |
| P1-17 | `wind-hub_跨模块测试设计_v0_49.md:407,732` 仍称 RAW 无真实载荷；实际 `collector/application/batch_builder.py:42-52` 写 `raw_payload_hex`，`sink_service/.../kafka_wire.py:88-106` 编码真实 RAW。 | 错误保留已解除的阻断。真实 NATS/Kafka RAW 用例本次 **NOT_RUN**，不据此宣称链路通过。 |
| P1-18 | `wind-hub_跨模块测试设计_v0_49.md:495` 写同键异值“不覆盖”，同文件 `510` 与 `Storage_Service_软件设计_v1_14.md:992` 及真实集成测试均要求较大 Kafka offset 完整覆盖（含 NULL）。 | 按错误断言会把正确实现判 FAIL。文档 P1。 |
| P1-19 | `wind-hub_跨模块测试设计_v0_49.md:587,591-592` 把整个 `configs/` 当单一发布根；`tests/configuration_service/component/test_shipped_yaml_repository.py:1-5,29-40` 明确只能按 `deploy/config_roots/<root>` 发布。 | 全历史目录直接发布会形成非确定选版或候选校验失败。文档 P1。 |

## 6. OPEN P2 与限制项

| ID | 位置与证据 | 影响/裁决 |
|---|---|---|
| P2-01 | `configs/services/service_storage-main_v2.0.0.yaml:18-22,70-77` 与 `deploy/config_roots/prod-a/...` 同版本字节不同；storage-e2e 同样漂移，差异为 `shutdown_drain_timeout_ms` 及注释。 | 违反不可变版本字节一致性；当前代码默认也是 120000ms，未证明行为差异，故 P2。交 ChatGPT 以后继版本修正。 |
| P2-02 | `docs/design/Collector_软件设计_v1_47.md:3348` 称无 candidate/CURRENT；真实 `src/collector/infrastructure/config/snapshot_repository.py:52-82` 持久化 candidate、generations、CURRENT。 | 离线恢复与运维文档错误。 |
| P2-03 | `wind-hub_跨模块测试设计_v0_49.md:526` 称 full_chain 只有包初始化；实际已有多份测试。 | 覆盖资产描述过期，不等于这些测试已通过。 |
| P2-04 | `CLI_软件设计_v1_59.md:1803-1808` 要求 RENEW；`src/cli/plant_commands.py:188-248` 只有 ACQUIRE/Update/RELEASE，服务端 RENEW 已存在。 | 长租约操作可靠性不符合设计，不会越权。 |
| P2-05 | Storage CLI 参数 `src/cli/storage_commands.py:68-81,105-120,154-167` 与设计 `CLI_软件设计_v1_59.md:1747` 的名称/形态不同。 | RPC 映射正确，但按设计生成的脚本不能执行。 |
| P2-06 | Configuration CLI 未暴露 Proto 已有 revision_id/page_size：`configuration_commands.py:173-211,446-463`。 | 历史枚举和分页能力不完整。 |
| P2-07 | 五服务 events CLI 只有分页，未暴露 `common.proto:105-109` 的 from_time/to_time/filter。 | 大事件流只能客户端自行筛选。 |
| P2-08 | `src/cli/diagnose_commands.py:58-77` 缺少设计 `CLI_软件设计_v1_59.md:2236` 的 `--output PATH.json`。 | 自动诊断取证能力缺口。 |
| P2-09 | `src/configuration_service/infrastructure/adapters/inbound/grpc/common_server.py:118-142` 的 rpc_names 漏掉已实现的 GetServiceConfigBundle。 | 能力发现误判。 |
| P2-10 | `docs/design/industrial_gateway_软件设计_v1_53.md:995,1007` RPC 数量自相矛盾；Proto 实际 10 个专属 RPC。 | 验收清单可能漏掉两个仿真 RPC。 |
| P2-11 | `docs/design/Contracts_软件设计_v1_48.md:1105,1108,1120` 仍使用旧 file_name 路径契约；Proto `configuration_repository.proto:12-17` 和服务端要求 relative_path。 | 按文档创建候选会被拒绝。 |
| P2-12 | `docs/design/CLI_软件设计_v1_59.md:1886-1899,2023-2028,2397` 命令树、selector 示例和 push/apply 声明过期。 | 文档示例不可直接执行；禁止旧 push/apply 是正确行为，不应恢复。 |
| 限制 | 现有 authorized chain 使用脚本化 Generator/无生产授权拦截器；Commander 侧又使用脚本化 Gateway。 | 只证明局部状态机，不是三生产服务同场，更不是 DCEMS 闭环。 |

已关闭的 Fixture 项（SampleBatch v3、E2E v1.0.10、版本化共享配置路径）不计入 OPEN P2。

## 7. 数据、消息、历史身份与 RPC 覆盖结论

### 7.1 已通过的局部契约

- **NATS SampleBatch v4**：严格 schema、device_config_id、统一批次 sample_time、RAW hex、Quality、显式 NULL、Subject/identity 和容量边界共 66 项通过；Collector 实际 BatchBuilder v4 3 项通过。
- **Kafka JSON v3**：Sink 编码与 Storage 严格解码对等，79 项通过。
- **RAW/PROTOCOL/BUSINESS 与 Quality**：Storage/Sink 单元组件覆盖通过；真实中间件三层链本次未重跑，不能从单元证据提升为 REAL_E2E PASS。
- **历史 PointTable 与设备身份**：不可变 pin、DeviceModel rebind 拒绝、同名多版本 archive fallback、多实例 DeviceBinding、快照 SHA-256 与待重启隔离均通过。
- **BusinessPoint 1:1**：活动配置静态展开和 Repository 发布 90 项通过；真实 OEM 审定和物理设备语义不是本轮证据。

### 7.2 正式 RPC 静态矩阵

8 个 Proto Service 共 **75 个不同 RPC 定义**：

- 66 个业务专属 RPC：**66/66 注册、66/66 有实质 Servicer、66/66 有生产调用方**。
- CommonService 9 RPC 在 7 个服务上共注册 63 个端点；Storage 上述 4 个显式未实现，2 个旧写入口按政策拒绝。
- 未发现 Stub 方法名或请求字段错映射。
- `GetServiceConfigBundle`、`GetInputExecution`、两个 Simulation RPC 由内部生产模块调用，无独立 CLI 本身不是缺陷；只有设计明确承诺的运营/监视入口才列为缺口。
- Proto 与服务职责契约测试 **114/114 PASS**。

## 8. 三套配置闭包与 Topic

| 发布根 | 结果 | Kafka Topic / 说明 |
|---|---|---|
| prod-a | 活动根发布 PASS | Sink 与 Storage 均为 `192.168.1.20:9092`、`windhub-history`。 |
| sim-a | 活动根 Repository 发布 PASS；进程启动闭包 FAIL | Topic/引用存在，但 P1-01 的 CLI→Collector 版本错配阻断实际启动。 |
| e2e-final | 活动根发布与当前 v2 预检 PASS | Sink 与 Storage 均为 `127.0.0.1:9092`、`windhub.e2e.history`。 |

Deployment→Service/Instance、Device→Model→Protocol/PointTable→Units/BusinessPoint、Delivery→Sink 的引用均存在且精确匹配。Kafka 实现统一 JSON v3，NATS 实现统一 SampleBatch v4。P2-01 的 configs/发布根同版本字节漂移仍单独保留，不能被“活动根可发布”掩盖。

## 9. StorageConfigurationWatcher 专项裁决

| 要求 | 证据 | 裁决 |
|---|---|---|
| NATS 正常关闭 | `test_nats_normal_stop_closes_connection_without_reconcile` | PASS，关闭一次且不多做 reconcile；使用受控 NATS 替身。 |
| 任务取消 | `test_nats_task_cancellation_closes_connection_and_propagates` | PASS，连接关闭且 CancelledError 传播。 |
| 连接失败 | `test_nats_connection_failure_keeps_periodic_reconciliation` | PASS，NATS 不可用时周期 gRPC 对账继续。 |
| 周期对账 | `test_connected_nats_without_hint_still_reconciles_periodically` | PASS，无 hint 仍对账。 |
| 不可变身份冲突不写待重启快照 | 强化 `test_rejected_device_model_rebinding_is_not_persisted` | PASS，stored/pending 均为空，旧 active 保留。 |
| 激活失败保留旧 active | `test_activation_failure_keeps_old_active_until_retry` | PASS，磁盘失败时旧 active/status 正确，重试成功才切换。 |
| Repository 不可用可信快照恢复 | `test_repository_outage_restores_only_trusted_snapshot` | PASS（组件级）；只恢复 trusted accepted closure。 |
| 取消不得吞掉 | refresh/run 两个专项 | PASS。 |

真实 NATS 服务、真实 Repository 进程故障和进程级 SIGTERM 组合 **NOT_RUN**；以上不冒称真实中间件可靠性资格。

## 10. 控制权限、幂等与 UNKNOWN 边界

最终 76 项局部测试通过，覆盖 Commander Bearer 权限、同 command_id 重放、APPLIED 才成功、确认超时 UNKNOWN、Gateway RPC 参数校验、Generator 执行证据、Data Generator 与真实 Gateway 的授权写入等。

但 OPEN P1-02/P1-03/P1-04 表明：

1. `variables_update` 目前不是足够细的生命周期权限边界；
2. 运行中立即重放通过，不代表过期、停止、revision 变化后的重放稳定；
3. 响应体 REJECTED 映射正确，不代表 gRPC 确定拒绝也正确；
4. 接口存在、描述符完整、局部 APPLIED/UNKNOWN 测试通过均不等于 DCEMS 闭环通过。

完整 Commander→Gateway→Data Generator 三生产服务同场、真实 DCEMS 操作闭环、PLC/硬件、netem、容量与长稳均为 **NOT_RUN**。

## 11. 移交与最终结论

- **交 ChatGPT（生产/核心/配置）**：P1-01～04、P1-07～08，P1-03/04 的跨层语义整改，以及 P2-01 不可变配置版本修正。
- **交 ChatGPT（D1 文档）**：P1-13～19、P2-02～03、P2-10～12。
- **测试/Fixture 后续**：P1-05～06 full-chain 与 Core 旧 schema 迁移；本轮已关闭的 6 个测试文件不重开。
- **CLI 设计与实现取舍**：P1-09～12、P2-04～09 需明确是实现补缺口还是正式修改设计基线，不能默默保留双契约。
- **资格边界**：容量 NOT_RUN，历史 BLOCKED 未解除；完整 DCEMS NOT_RUN；PRODUCTION_READINESS BLOCKED。

**最终裁决：STATIC_CONSISTENCY=FAIL，FUNCTIONAL_RELIABILITY=FAIL，CAPACITY=NOT_RUN，DCEMS_E2E=测试现场 FAIL/业务闭环 NOT_RUN，PRODUCTION_READINESS=BLOCKED。** 已通过的 Watcher、消息契约和局部控制测试保持独立有效，不被容量或 E2E 未通过反向否定。

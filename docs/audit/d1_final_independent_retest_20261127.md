# wind-hub D1 最终独立复测与收口报告（2026-11-27）

> 面向 ChatGPT 的 D1 最终裁决与 D2 准入依据。本文只按实际执行证据裁决，不将局部通过扩大为容量或生产资格通过。

## 1. Git、环境和范围

- 起始/指定生产 HEAD：`6816fcc46247ef292aa0acf5f95693f4abf10f2e`。Fetch 后本地、远端一致，工作树干净。
- 分支：`feat/storage-wide-table-completion-20261125`。未创建新分支、PR，未合并 main，未 force push。
- 环境：Python 3.11.16，`wind-hub` Conda 环境；`python3 scripts/dev.py env` PASS。
- 最终测试源码/Fixture SHA：**`e5a9bac039560d628a878b9f0e91c80f27dc59c0`**。报告与证据随后独立提交，最终 HEAD 以 Git 元数据为准。
- 原始证据：[d1_final_independent_retest_20261127_evidence/](d1_final_independent_retest_20261127_evidence/)。
- Web Server / Web Admin 未修改。旧 P1-07 Web Commander 互操作问题按任务要求单独保留，不纳入本轮生产整改关闭范围。
- 未修改冻结 Proto、容量阈值或权限标准；未删除测试、放宽断言、增加 skip/xfail。

## 2. 门禁裁决

| 门禁 | 裁决 | 依据/边界 |
|---|---|---|
| STATIC_GATES | **PASS** | Ruff check/format、mypy、import-linter、Proto --check 全绿。 |
| DESIGN_SOURCE_CONSISTENCY | **PASS（本轮范围）** | 旧 P1-13～19、P2-02/03/10/11/12 与实现一致；Web P1-07 单列。 |
| CONFIGURATION_CLOSURE | **PASS** | 当前 prod-a/sim-a/e2e-final 精确选版、活动根发布和字节一致性通过。 |
| STORAGE_COMMON_RPC | **PASS** | 真实 gRPC Common 14 项及 Storage 全量回归通过。 |
| STORAGE_WATCHER | **PASS** | 取消、NATS 关闭、不可变历史、回滚、离线恢复 31 项通过。 |
| GENERATOR_AUTHORIZATION | **PASS** | variables_update 与 generation_manage 正反矩阵、真实服务回归通过。 |
| SIMULATION_REPLAY_UNKNOWN | **PASS** | 过期/STOP/revision 重放、有效步、确定拒绝/UNKNOWN、APPLIED 证据通过。 |
| CLI_RPC_CONTRACTS | **PASS** | 新增 CLI 契约及完整 CLI 回归通过；跳过项另列。 |
| CORE_CONFIGURATION | **PASS** | 原 4 个旧 Schema 失败关闭，26/26。 |
| FULL_CHAIN_E2E | **PASS（选定 32 项）** | 控制安全 7、分段 6、安全恢复 9、语义矩阵 8、RPC/CLI 表面矩阵 2；其他文件 NOT_RUN。 |
| CAPACITY | **BLOCKED（继承，未重测）** | 未开展容量调参或长稳，既有容量阻断不解除。 |
| DCEMS_E2E | **PASS（控制/APPLIED/未授权范围）** | 三生产服务同场、真实 Bearer、真实中间件；未覆盖场景另列 NOT_RUN。 |
| PRODUCTION_READINESS | **BLOCKED** | 容量未关闭；Web 旧问题保留，且不能把本轮功能通过等同生产 READY。 |

**D1 范围结论：PASS。旧 P1/P2 除明确排除的 Web P1-07 外均有关闭证据，新发现问题均已修复；可提交 ChatGPT 决定是否进入 D2。该结论不代表 D5 容量通过或生产 READY。**

## 3. 实际命令、数量与日志

| 检查 | 实际命令（简写） | 结果 | 日志 |
|---|---|---|---|
| 静态门禁 | `ruff check src tests scripts`；`ruff format --check src tests scripts`；`mypy src`；`lint-imports`；`python3 scripts/generate_rpc.py --check` | PASS；mypy 495 文件、import 22/22 | `static.log` |
| Core + Proto/线协议 | `pytest -q tests/src/core/configuration tests/contracts tests/src/unit/test_contracts` | 212 passed（Core 26、Proto/线协议 186） | `core_proto_contracts.log` |
| Storage 全量 | `pytest -q tests/storage_service/unit tests/storage_service/component` | 297 passed | `storage_unit_component.log` |
| Watcher/历史身份 | `pytest -q tests/storage_service/unit/test_configuration_reconciliation.py tests/storage_service/component/test_offline_configuration_recovery.py tests/storage_service/unit/test_device_binding.py tests/storage_service/unit/test_storage_crash_faults.py` | 31 passed | `storage_watcher.log` |
| 三套配置闭包及 Common | `pytest -q tests/configuration_service/component/test_shipped_yaml_repository.py tests/system/e2e/test_long_run_candidate_preflight.py tests/src/core/configuration/test_source_manifest_and_closure.py tests/configuration_service/component/test_directory_candidate.py tests/configuration_service/integration/test_common_rpc.py` | 109 passed | `configuration_closure.log` |
| 控制专项 | Commander/Gateway/Generator 10 个定向文件 | 97 passed | `control_replay_unknown.log` |
| Commander + Gateway 回归 | `pytest -q tests/commander/unit tests/commander/component tests/industrial_gateway/unit tests/industrial_gateway/component` | 379 passed（Commander 224、Gateway 155） | `commander_gateway_regression.log` |
| Generator 非性能回归 | `pytest -q tests/data_generator/unit tests/data_generator/component tests/data_generator/integration tests/data_generator/system` | 203 passed | `data_generator_regression.log` |
| CLI 全量 | `pytest -q tests/cli` | 297 passed、13 skipped | `cli_full.log` |
| Full-chain 联合 | `pytest -q tests/system/e2e/full_chain/test_e2e_control.py tests/system/e2e/full_chain/test_e2e_control_security.py tests/system/e2e/full_chain/test_integration_segments.py tests/system/e2e/full_chain/test_e2e_security_recovery.py tests/system/e2e/full_chain/test_e2e_semantic_matrix.py tests/system/e2e/full_chain/test_e2e_surface_matrix.py` | **32 passed，0 skip/xfail** | `full_chain_all_selected.log` |

额外静态闭包脚本验证：prod-a 29、sim-a 23、e2e-final 37 个活动 YAML 均与 `configs/` 同相对路径逐字节一致；22 个 Service 的 CLI argv 与当前 Deployment 精确选版一致。

CLI 的 13 个 skip 不计 PASS；它们属于既有 containerd/iSulad 等环境资格缺口，并非新增跳过。真实硬件、netem、容量、长稳不在本轮执行范围。

## 4. 旧 P1 逐项复核

| ID | 裁决 | 当前 SHA 证据 |
|---|---|---|
| P1-01 | **CLOSED** | sim-a Deployment 2.0.1 选择 Collector 2.0.0，CLI 1.10.1 argv 同版；三套 22 Service 精确选版和 107 项闭包测试通过。真实进程由 Full-chain/严格 CLI 另证。 |
| P1-02 | **CLOSED** | `_LIFECYCLE_METHODS` 独立要求 generation_manage；variables_update Start/Stop 拒绝，合法 manager 可生命周期操作；DG 203 项回归通过。 |
| P1-03 | **CLOSED** | Gateway replay 先于可变状态校验；Generator history 先于 RUNNING 校验。过期/STOP/revision 重放、停止后有效步及异指纹拒绝已执行。 |
| P1-04 | **CLOSED** | GetGateway/Execute/GetResult 统一分类；确定拒绝含 ALREADY_EXISTS 为 CONFIRMED_FAILURE，I/O/超时 UNKNOWN，APPLIED 才成功。97 项专项通过。 |
| P1-05 | **CLOSED（控制及已迁移辅助 Fixture）** | Full-chain 已迁移版本化 Instance、security.callers、token_env 和最小权限；真实控制安全 7/7。辅助场景结果见第 8 节。 |
| P1-06 | **CLOSED** | Core 旧 4 失败已迁移当前 Commander/Gateway schema、Deployment 和版本化模板，26/26。 |
| P1-07 | **OPEN / OUT_OF_SCOPE** | Web Commander mTLS/Bearer 与正式服务端不兼容问题保留；未修改 Web 源码，不影响本轮非 Web 整改范围，但阻断整体生产 READY。 |
| P1-08 | **CLOSED** | Storage Common 读接口已正式实现，14 项真实 gRPC 专项和 297 项 Storage 回归通过；旧 push/apply 仍拒绝。 |
| P1-09 | **CLOSED** | 五服务 monitor 注册并实测；Collector 对象请求改发 device_id，其他服务发送 id。 |
| P1-10 | **CLOSED** | gateway watch 有界轮询、间隔/次数、鉴权与取消语义测试通过。 |
| P1-11 | **CLOSED** | bundle-exact 支持重复 --exact-root、重复身份拒绝、SHA/manifest 和私密目录导出。 |
| P1-12 | **CLOSED** | config service-bundle 调 GetServiceConfigBundle，输出 Deployment 选版/target_revision/manifest；service status 独立查询 active。 |
| P1-13 | **CLOSED** | Core 设计已明确 deployment_id/service_id 参与 closure digest，与实现和强制验证一致。 |
| P1-14 | **CLOSED** | 不可变版本冲突文档与 RPC 均为 FAILED_PRECONDITION。 |
| P1-15 | **CLOSED** | 文档、Collector、Golden Data 与解码器统一 SampleBatch v4。 |
| P1-16 | **CLOSED** | E2E 文档和当前 Deployment 2.0.2、Storage 2.0.1、CLI 1.1.9 等选版一致。 |
| P1-17 | **CLOSED** | 文档不再称 RAW 无实现；真实来源/编码路径存在，未把静态能力冒称所有中间件场景通过。 |
| P1-18 | **CLOSED** | 同键异值统一为较大 Kafka offset 完整覆盖（含 NULL）。 |
| P1-19 | **CLOSED** | 发布边界明确为 deploy/config_roots/<root>，configs 历史归档不得作单一活动根。 |

## 5. 旧 P2 逐项复核

| ID | 裁决 | 当前 SHA 证据 |
|---|---|---|
| P2-01 | **CLOSED** | 新 Storage 2.0.1 活动文件与 configs 字节一致；三套活动根全部 YAML 比对通过。 |
| P2-02 | **CLOSED** | Collector candidate/generations/CURRENT 的离线恢复职责已与设计一致。 |
| P2-03 | **CLOSED** | 跨模块设计承认 full_chain 实际测试资产，不以文件存在冒充执行通过。 |
| P2-04 | **CLOSED** | gateway renew 发送 RENEW，owner/lease/token_env 与 Bearer 映射经真实本地 gRPC 测试。 |
| P2-05 | **CLOSED** | Storage --data-level/--aggregation 等正式选项及请求映射通过。 |
| P2-06 | **CLOSED** | config list revision_id、版本/引用分页及 optional presence 测试通过。 |
| P2-07 | **CLOSED** | 五服务 events 的 from_time/to_time/filter 与分页均经本地真实 gRPC 测试。 |
| P2-08 | **CLOSED** | diagnose --output 使用 0600、O_EXCL，不覆盖、符号链接/父目录/写异常形成 JSON 错误。 |
| P2-09 | **CLOSED** | GetCapabilities 准确 24 项集合包含 GetServiceConfigBundle，测试不再只断言旧数量。 |
| P2-10 | **CLOSED** | Gateway 设计与 Proto 统一为专属 10 RPC、加 Common 共 19。 |
| P2-11 | **CLOSED** | CreateCandidate 文档与 Proto 均使用 relative_path 权威路径。 |
| P2-12 | **CLOSED** | 命令树和 push/apply 已修正；最后 selector 示例改为 `config get collector-realtime --scope disk --output ...`，与真实入口一致。 |

## 6. 本轮新发现与修复

| 编号/级别 | 位置、原因与复现 | 裁决 |
|---|---|---|
| N-01 / P2 | Storage CANDIDATE 在 pending 已激活后仍可读；原 export_target 不比较 accepted revision。新增激活后读取回归。 | **CLOSED**：仅允许 revision 严格晚于 accepted 且与当前 target 一致；14 项 Common 通过。 |
| N-02 / P1 | Commander 未把同 ID 异内容的 ALREADY_EXISTS 纳入确定拒绝，且 GetGateway/GetResult 异常未分类。 | **CLOSED**：补齐状态码和三类 RPC 参数化测试，97 项专项通过。 |
| N-03 / P1 | monitor collector 对象请求发送 id，Proto 需要 device_id，实际 ParseError。 | **CLOSED**：按服务构造请求并增加真实 gRPC 字段断言。 |
| N-04 / Fixture | 严格 CLI 的 Generator 新 manager caller 缺令牌；补后共享 _write_cli 又覆盖 Full-chain 合法 manager 身份。 | **CLOSED**：管理令牌改为显式可选映射；严格 CLI 1/1 与 Full-chain 7/7 均独立复测通过。 |
| N-05 / 静态 | 本轮三个生产文件 Ruff format 漂移。 | **CLOSED**：仅机械格式化，最终全量 format PASS。 |
| N-06 / Fixture | 固定批次仍为 SampleBatch v3；Kafka 读取仍按旧平铺 sample_id，而正式 JSON v3 是设备宽行 points。 | **CLOSED**：迁移 v4 和真实 device_config_id，按设备/点/时间/值读取 v3；I03/I04 通过。 |
| N-07 / Fixture | 仅编辑 Sink 源 YAML 后重启仍恢复 CURRENT 旧代际；旧 Common push/apply 又被唯一发布边界拒绝。 | **CLOSED**：停止唯一写进程后以 read_snapshot + GenerationStore.commit 原子提交再启动；I07 通过。 |
| N-08 / Fixture | 租约两通道复用同一 Bearer 主体；I05 又用 ops 查询 Gateway 命令，联合运行时还读到历史 ACCEPTED。 | **CLOSED**：使用不同 inject 主体、按命令 caller 查询，JetStream 采用 DeliverPolicy.NEW；联合 32 项通过。 |
| N-09 / Fixture | 语义/表面矩阵漏掉 9 个新 CLI 叶子，进程内 Runtime 缺 Bearer 环境，Common 写入口仍期望旧权限语义。 | **CLOSED**：Configuration 两项归拓扑外，七项补执行/证据；Common 保持 Repository 唯一发布者拒绝。 |

## 7. Storage Common 与 Watcher 证据边界

- **GetConfigSnapshot**：DISK/RUNTIME/CANDIDATE、accepted/target SHA-256、缺失、篡改、active mismatch、错误 candidate、过期/已激活 candidate 均经真实 gRPC 验证。
- **GetMetrics**：真实 rows/points/records/unknown/conflict/buffer 计数、采样时间、process scope；window/非零 window_ms 明确拒绝，不生成吞吐。
- **GetEvents**：258+ 事件验证 256 有界、顺序、过滤、时间范围、分页、过期游标和并发读取；8 线程×100 写入验证锁与事件 ID 完整性。
- **Diagnose**：合法/非法/重复 stage、target、timeout；配置状态有证据，Kafka/TDengine/ingestion 未主动探测时 UNKNOWN，不伪造 HEALTHY。
- **权限**：匿名和错误 Bearer 全 RPC 枚举；inspect/operate 交叉拒绝；合法 operate 调 push/apply 仍 UNIMPLEMENTED，不产生第二发布通道。
- **Watcher**：NATS 正常关闭、任务取消、连接失败周期对账、不可变身份不写 pending、激活失败保留旧 active、Repository 离线 trusted snapshot 恢复均通过。真实 NATS 故障组合未在本轮重复执行。

## 8. 控制、CLI 与 Full-chain

- 真实 Bearer 的 variables_update/generation_manage 矩阵通过；未降低权限。
- 过期、Gateway STOP、revision 变化后，原 request_id 重放/查询不重复执行；Generator 停止后保留原有效步，同 ID 异内容严格拒绝。
- 参数、权限、版本、执行前过期及 ALREADY_EXISTS 为确定失败；网络断连、查询失败和确认超时 UNKNOWN，不以新身份盲重试；只有 APPLIED 成功。
- Full-chain 真实控制 7 项由 Commander、Industrial Gateway、Data Generator 三生产实现和实际中间件共同参与，不是脚本 Servicer 替身。
- Full-chain 辅助场景：分段 6/6、安全恢复 9/9、语义矩阵 8/8、RPC/CLI 表面矩阵 2/2，并与控制安全 7/7 联合复测为 **32/32 PASS**。
- 其他未执行 Full-chain 业务文件、真实 PLC、硬件、netem、容量与长稳均为 **NOT_RUN**，不从 7 项控制通过推导全场景 PASS。

## 9. 最终移交

- 本轮生产修改仅：Collector monitor 请求字段、Commander RPC 错误分类、Storage CANDIDATE 有效性，以及机械格式化。
- 其余修改为测试、Fixture、证据和一条 CLI 设计示例修正。
- Web P1-07 单独保留；CAPACITY 维持 BLOCKED；PRODUCTION_READINESS 维持 BLOCKED。
- **D1 最终裁决：PASS（本轮非 Web 范围）。建议 ChatGPT 准许进入 D2 评审；D5 容量、Web 互操作和生产 READY 仍是独立阻断项。**

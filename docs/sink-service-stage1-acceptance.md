# Sink Service 阶段一（配置体系与控制面）独立验收报告

- 验收日期：2026-11-03
- 基线：`feat/sink-service-v1-rewrite` @ `fd6b76cd72e1b52938de9b514d31a0b00cca2158`
- 设计依据：`docs/Sink_Service_软件设计_v1_14.md`（工作树中 v1_15 仅章节编号与迁移矩阵增补，契约无变化；验收以任务书指定 v1_14 为准）
- main 对照：`origin/main` @ `0dfee3bb`（merge-base `bf9dc6d2`）
- 验收人：Claude Code 独立验收

## 1. 最终分支及 SHA

- 分支：`feat/sink-service-v1-rewrite`（已 push，起点 fd6b76cd，纯快进无覆盖）
- 生产修复：`708c1605` fix(sink): close stage-one control-plane contract gaps
- 测试与白名单/报告：`0a160670` test(sink): add stage-one control-plane acceptance coverage
- 报告 SHA 补录：见第三次 docs 提交（本节）
- 未创建 PR、未合并 main、未使用 force push/rebase；RPC 生成物（*_pb2）未入库。

## 2. 静态检查实际结果

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| Ruff Lint | `ruff check src tests/sink_service` | PASS（0 error） |
| Ruff Format | `ruff format --check src tests/sink_service` | PASS（350 files already formatted） |
| Mypy strict | `mypy src`（先 `python3 scripts/generate_rpc.py`，生成物不入库） | 83 error；与 main 基线（80 error）集合比对，阶段一新代码 RELATED=0；3 条分支独有错误全部位于 `src/cli/*`（collector_v2_pb2 改名与 core.infrastructure.config.adapter 删除，均为分支落后于 main 的既有漂移，UNRELATED） |
| Import Linter | `lint-imports` | PASS（13 kept, 0 broken，含三层架构与不跨进程包两条契约） |

基线比对方法：分支与 main worktree（`.claude/worktrees/main-baseline` @ 0dfee3bb）各自全量 mypy，错误行排序后 `comm` 集合比对。

## 3. 测试结果

### 3.1 阶段一新增测试（81 个，全部通过）

| 文件 | 层级 marker | 数量 | 覆盖 |
| --- | --- | --- | --- |
| test_stage1_config_validation.py | unit+contract | 40 | 完整文件集、双 Service 一致性、模板不可变、能力/模式/源/阈值校验、上下限保护（256 文件/2 MiB） |
| test_stage1_config_repository.py | integration+file | 9 | 代目录原子提交、CURRENT 指针 CAS、模板版本不可变/不可删、路径穿越拒绝、崩溃不可达 stage |
| test_stage1_delivery_registry.py | component+reliability | 11 | 角色归属、START 失败可查可重试、手动 STOP 不复活、签名不变不重建端口、绑定变更先旧后新、删除失败 tombstone 保留所有权且 Watchdog 不复活、shutdown BaseExceptionGroup、非法角色拒绝 |
| test_stage1_config_manager.py | component+contract | 14 | Push 不触盘、ABORTED/INVALID_ARGUMENT/NOT_FOUND/PERMISSION_DENIED、候选容量 8 与 TTL 300s、收敛推进 runtime、部分失败 runtime 停留旧代、restart_required、启停同 CAS、Reader mark_active |
| test_stage1_config_reader.py | component+reliability | 3 | 应用失败不推进有效版本且继续轮询、第二代可见、无变化返回 False |
| test_stage1_writer_lease.py | unit+reliability | 3 | flock 第二 Writer fail-closed、释放后可重获、同实例重复获取拒绝 |
| test_stage1_dual_process.py | system+contract+reliability | 1 | 真实双 OS 进程（sink_service.main）+ 真实 mTLS + Proto Stub，见 3.2 |

### 3.2 双进程系统测试（UC-01/07/10 阶段一边界）

单系统用例连续覆盖：

1. 双进程启动就绪，GetServiceInfo 进程角色 realtime/reliable 不混同、service_id 一致；
2. GetCapabilities 诚实公布（无数据面协议）；
3. GetMetrics/GetEvents/Diagnose 均返回 UNIMPLEMENTED（阶段四能力不虚报）；
4. mTLS：未登记 URI SAN → PERMISSION_DENIED；无客户端证书 → 握手失败 UNAVAILABLE；query-only 证书 StartDelivery → PERMISSION_DENIED；
5. Reader 进程 Push → PERMISSION_DENIED（唯一 Writer）；
6. 缺文件候选 → INVALID_ARGUMENT；陈旧 expected_revision → ABORTED；未知 candidate_id → NOT_FOUND；UNSPECIFIED scope → INVALID_ARGUMENT；
7. START 经 fail-closed 工厂：磁盘代提交成功、apply_results 逐对象 FAILED、effective_revision 停留旧代；Reader 因自身对象全部 STOPPED 而独立采纳该代（文件提交与运行收敛按进程独立裁定）；
8. 随后 STOP 提交：两进程均经 CURRENT 指针轮询收敛至同一修订；
9. Writer 对 reliable 通道 Delivery 执行 StartDelivery：accepted=true、actual_state=PENDING（不编造另一进程状态），Reader 侧 GetServiceStatus 如实 DEGRADED；
10. ListDeliveries 通道过滤/分页上限 500 拒绝/越权通道返回空、GetDelivery 未知 ID → NOT_FOUND；
11. 两进程 SIGTERM 退出码均为 0。

URI SAN 鉴权键实证：使用真实 TLS 握手探针确认 grpc-python `auth_context()` 对 URI SAN 给出 `x509_subject_alternative_name=[b'spiffe://...']`（另有 peer_uri/peer_spiffe_id 同义键），生产代码解码路径正确。

### 3.3 全量回归（`pytest tests/ --continue-on-collection-errors`，918.61s）

| 树 | passed | failed | skipped | 收集错误 |
| --- | --- | --- | --- | --- |
| 分支（含本次修复） | 1081 | 27 | 10 | 86 |
| main 基线 0dfee3bb | 1153 | 34 | 12 | 80 |

两树收集数不同（main 1224 vs 分支 1140），因此以**失败/错误集合 diff** 而非计数裁定：

- 共同失败 17 项（ADS soak、gateway r2 启动、seahorse 2 项、system/r5 Kafka×4/Modbus×5、web_server 架构×3）：两树同红，均依赖 Docker 真实服务或属已知基线套件，**UNRELATED**。
- 分支新增失败 10 项：
  - `tests/core/architecture/test_rpc_boundary.py::test_pb2_only_in_allowed_zones`（**RELATED，已修复**）：分支新建正式边界 `sink_service/infrastructure/grpc_v1.py` 未登记 pb2 白名单；按各服务 `infrastructure/grpc/` 同构方式补登记，修后该文件 18 项全绿。该缺陷在分支基线 fd6b76cd 即存在（非本次引入），本次顺带闭环。
  - `tests/cli/test_results_and_output.py` 9 项（**UNRELATED 分支漂移**）：该文件存在于 merge-base、已被 main 的 CLI 重写删除；失败源于 `cli.presentation/results` 主线演进，本次 diff 对 src/cli 零改动（`git diff --name-only | grep src/cli` 为空）。
- 收集错误差集恰为 +6（tests/cli 的 config_session/inventory/network_diagnostic/pipeline/protocol_diagnostic/shell，全部 `ModuleNotFoundError: core.infrastructure.config.adapter`），merge-base 即存在，同属分支落后 main 的 CLI 漂移；旧 `tests/sink_service/test_diagnostic_cli.py` 收集错误两树一致；`test_nats_consumer_reliable.py` 分支实跑 1 passed/10 skipped 与基线同态。
- 基线 17 项失败在分支未复现（seahorse/commander 真机栈）：真实栈用例的环境/序偶发翻转，不记为本次修复。
- 本次新增的 81 个阶段一测试在全量运行中全部通过。

## 4. 阶段一设计符合度矩阵

| 设计条目 | 符合度 | 证据 |
| --- | --- | --- |
| 6 Writer/Reader 双进程、唯一 Writer（flock 租约） | PASS | writer_lease.py + 3 单测 + 双进程测试第 5 步 |
| 6 Reader 轮询 CURRENT、失败不推进有效版本 | PASS | config_sync.py + 3 单测 + 双进程第 7/8 步 |
| 11.3 Get/Push/Apply 完整快照、候选暂存（TTL 300s/max 8） | PASS | config_manager.py + 14 组件测试 |
| 11.3 expected_revision CAS（Push/Apply/启停同一锁与修订号） | PASS | ABORTED 断言（组件+系统） |
| 11.4 原子代目录 + CURRENT 指针、临时文件/目录 fsync、模板版本不可变 | PASS | config_repository.py + 9 集成测试 |
| 11.5 文件提交与运行收敛独立裁定（disk vs runtime revision） | PASS | 双进程第 7/8 步；部分失败组件测试 |
| 11.6 Service 配置变更 restart_required，Reader 拒绝热生效 | PASS | 组件测试 + bootstrap apply_reader_snapshot |
| 9.1 13 RPC 全注册、无自定义 RPC | PASS | 双进程真实 Stub 调用 13 项 |
| 9.2 响应字段与语义（accepted≠actual、PENDING 不编造对端状态、SNAPSHOT_SCOPE 裁决） | PASS（限制：validation[] 未结构化填充，见台账 L-3） | 双进程第 9 步、UNSPECIFIED 断言 |
| 9.3.1 状态码 INVALID_ARGUMENT/ABORTED/PERMISSION_DENIED/FAILED_PRECONDITION/NOT_FOUND/UNIMPLEMENTED | PASS | 领域异常分类 + grpc_v1._status_for + wire 断言 |
| 13.1 mTLS URI SAN、query/control/config 三级权限 | PASS | grpc_auth.py + 真实证书双进程链路 |
| UR-03 生命周期：desired/actual 分离、Watchdog 只恢复 desired=START 的 STOPPED/FAILED | PASS | registry 11 测试（含 STOP 不复活、tombstone 不复活） |
| 删除/关闭失败保留资源所有权，下次收敛重试 | PASS | tombstone 组件测试 |
| 阶段一数据面 fail-closed，不虚报派发成功 | PASS | main._PendingDataPlane 启动即抛 ConfigurationError；系统测试 FAILED/PENDING/DEGRADED 全链路可观察 |
| 入口/配置/调用方一致性（正式入口为新控制面） | PASS WITH LIMITATIONS | main:main 为新入口；旧 management_cli/diagnostic_cli/assembly 残留入口仍在 pyproject（main 基线同态，台账 L-1） |
| 配置路径安全（service_file 穿越、代目录名） | PASS（本次修复） | _safe_managed_path + GenerationStore.load 校验 |
| 配置摘要分隔符 | PASS（本次修复 P1） | digest b"\\0" 字面量两字节 → b"\\x00"（见 5.1） |

不覆盖（阶段边界，非缺陷）：UR-01/02 数据派发、UR-04 指标、UR-07 诊断实现（接口 UNIMPLEMENTED 已验证）、真实 NATS/Redis/Kafka 链路、netem、长稳 soak。

## 5. 已修复问题及提交

### 5.1 生产缺陷（授权范围内的局部修复）

1. **P1 配置修订摘要碰撞缺陷**：`application/config.py` 摘要两处 `digest.update(b"\\0")` 实为字面反斜杠+0 两字节，分隔符可被路径/内容伪造，破坏修订号无碰撞前提。改为 `b"\\x00"`。
2. **P2 状态码契约不符**：领域异常只有单一 ConfigurationError，9.3.1 要求的 INVALID_ARGUMENT/ABORTED/PERMISSION_DENIED/NOT_FOUND 全部退化为 FAILED_PRECONDITION。新增 InvalidConfigurationError/RevisionConflictError/WriterRequiredError/CandidateNotFoundError 分类树，ConfigManager 与 Repository 改投精确异常，grpc_v1 增加 `_status_for` 映射并经 wire 测试钉死。
3. **P2 Reader 变更请求未 fail-closed 到正确状态码**：随 2 一并收敛为 PERMISSION_DENIED。
4. **P2 删除中 Delivery 被 Watchdog 复活风险**：DeliveryRegistry 增加 removing tombstone，reconcile_all 跳过；删除失败保留所有权，下次 apply_configuration 重试 stop 成功后释放（782ae7c6 的配套语义在本轮补齐测试）。
5. **P2 签名排除 desired_state 后启停成功被判未收敛**：签名不变路径也计入 changed 并按实际状态裁定，修复 START 成功 runtime 修订不前进。
6. **P2 GetConfigSnapshot 缺 application_id/apply_results/last_error 字段**：按 9.2 补齐，且仅在 latest_application.file_commit 等于所查修订时返回，范围间不混报。
7. **P3 service_file 路径穿越**：bootstrap `_safe_managed_path`（拒绝绝对路径/反斜杠/`.`/`..`/非 services 前缀）+ GenerationStore.load 同样校验。
8. **P3 关闭宽限硬编码 5.0s**：改为读取 Service 配置 `shutdown.grace_seconds`。
9. **P3 domain 层缺 `__init__.py`**：lint-imports "Missing layer 'sink_service.domain'"，补齐。

### 5.2 测试与基础设施

- 新增 81 个阶段一测试（见 3.1）与临时 CA/mTLS 证书夹具（EC P-256，CA+server+3 客户端身份）。
- 格式/类型/导入修复若干（ruff format 9 文件、mypy 变量遮蔽、contracts 拆分导入等）。

生产修复提交 `708c1605`；测试、白名单与本报告提交 `0a160670`（均已 push）。

## 6. 未关闭缺陷与台账（移交 ChatGPT）

### L-1（P2，超授权架构清理）旧 Sink 实现与旧 CLI 残留

- 位置：`src/sink_service/assembly.py`、`management_cli.py`、`diagnostic_cli.py`、`nats_init.py`、`infrastructure/{config.py,grpc_management.py,diagnostic.py,intent.py,nats_consumer.py,point_adapter.py,adapters/*}`；`pyproject.toml` 入口 `wind-hub-sink`、`wind-hub-sink-diagnostic`。
- 证据：残留模块引用 main 已删除的 `core.infrastructure.config.adapter/YamlConfigAdapter/sink_schema` 等；`tests/sink_service/test_diagnostic_cli.py` 收集即 ImportError（main 基线同态，UNRELATED）；旧模块贡献 15 条 mypy 错误，与 main 错误集合完全一致。
- 风险：正式 pip 安装后仍暴露两个旧入口，违反设计 7.4 的旧实现清退；但新正式入口 `wind-hub-sink-service` 已是 fail-closed 控制面，无错误接线。
- 建议：阶段二开工前由 ChatGPT 裁定删除或隔离旧入口与 adapters/，并迁移/删除对应旧测试；属架构清理，不在本次授权范围。

### L-2（P3）Watchdog 恢复间隔硬编码 5.0s

- 位置：bootstrap.py `recover_running` 的 `asyncio.wait_for(..., timeout=5.0)`。
- 建议：纳入 Service 配置（如 watchdog.interval_ms）并做上下限校验。

### L-3（P3）validation[] 结构化校验问题未填充

- 位置：grpc_v1.py Push/GetConfigSnapshotResponse。
- 现状：非法候选整体拒绝（INVALID_ARGUMENT + 错误文本），valid=true 时 validation 为空；设计 9.2 列出 validation[] 字段，未要求阶段一必须逐字段返回。
- 建议：阶段二按 ValidationIssue（字段路径/原因）结构化返回，替代纯文本 status message。

### L-4（观察项）grpc_auth 类型 ignore

- `class PeerAuthorization(grpc.aio.ServerInterceptor):  # type: ignore[misc]` 为 grpc aio 拦截器基类类型缺陷的既定规避，真实链路功能已实证；建议随 grpc-stubs 升级复查。

### L-5（环境项）未执行项

- 真实 NATS JetStream/Redis/Kafka/TDengine 链路、netem 损伤、长稳 soak、真机部署：阶段一无数据面，按设计属阶段二/三/四范围，NOT_RUN，不以任何形式标记 PASS。

### L-6（UNRELATED 基线，不修）

- src/cli 3 条 mypy 错误、tests/cli 6 个收集错误与 test_results_and_output 9 项失败：分支 merge-base bf9dc6d2 已存在，main 后续 CLI 重写已移除/修复，rebase 窗口外。
- 全量回归两树共同红灯 17 项（Docker 真实服务依赖/已知基线套件），明细见 3.3。
- `tests/tooling/test_merge_skill.py` ruff format 漂移在 main 基线同样存在，本次未触碰。

## 7. 阶段二编码准入结论

**PASS WITH LIMITATIONS（有条件通过）**

依据：

1. 阶段一控制面契约在真实双 OS 进程 + 真实 mTLS + Proto Stub 下全部验证：13 RPC 注册与响应结构、四级状态码、三级权限、唯一 Writer/Reader 轮询、原子代提交与 CAS、文件提交/运行收敛按进程独立裁定、Watchdog 不复活 STOP 与删除中对象、fail-closed 不虚报数据面成功、SIGTERM 零退出。
2. 新增 81 个测试全部通过；四门静态门禁对阶段一新代码全部为绿（mypy 83 错误经集合比对全部 UNRELATED；lint-imports 13 契约 KEPT）。
3. 全量回归中发现的唯一 RELATED 缺陷（rpc 边界白名单）已闭环；其余红灯经双树集合比对全部 UNRELATED。

限制与阶段二前置条件：

- **L-1 须在阶段二数据面接线前裁定**：正式入口已正确 fail-closed，但旧 management_cli/diagnostic_cli/assembly/adapters 仍随包发布且引用已删 core 模块（旧入口本身在主线也长期损坏，无错误接线证据）；ChatGPT 需决定删除或隔离，避免阶段二适配器被错误接入旧装配路径。
- L-2/L-3/L-4 为 P3 台账项，可在阶段二内并行消化，不阻断开工。
- 数据面（UR-01/02）、指标/事件/诊断实现、真实中间件链路与 netem/soak 按设计属后续阶段，本次 NOT_RUN。

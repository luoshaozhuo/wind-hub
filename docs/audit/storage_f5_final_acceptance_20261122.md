# Storage Service F5 最终验收报告（2026-11-22）

- 分支：`feat/storage-service-v1-implementation`
- 起始基线：`f497df9e9343bb6921b97ee2e9a0ab917408104b`
- 最终 HEAD：`1b1ebf06b5b09fecc5052e415c1560e0c09db6cd`（已 push，本地=远端，工作树干净）
- 权威设计：`docs/design/Storage_Service_软件设计_v1_4.md`
- 验收人：Claude Code（独立执行，生产代码零改动）

## 1. 真实服务栈与版本证据

本轮启动 F5 专用独立 Compose project（`wind-hub-f5-*`，动态 loopback 端口，
Kafka 固定 9092、TDengine 固定 6041），结束执行 `down -v --remove-orphans`，
未复用任何遗留容器；TDengine 测试库为每用例唯一 `c2-t5-*` 农场，
Kafka 无残留 topic，未触碰开发/生产数据。

| 组件 | 镜像/版本 | 就绪证据 |
|---|---|---|
| Kafka | `apache/kafka:3.9.1`（KRaft），`kafka-topics.sh --version` = 3.9.1 | compose healthcheck + 真实 produce/consume |
| TDengine | `tdengine/tdengine:3.3.6.13`，`SELECT SERVER_VERSION()` = 3.3.6.13（测试内强制版本断言） | taosAdapter REST 真实读写 |
| NATS | `nats:2.10-alpine`，`nats-server v2.10.29`，JetStream 开启 | 真实订阅 `wh.config.changed` 收到广播 |
| Redis | `redis:7-alpine` | healthcheck |

测试环境变量：`WIND_HUB_TEST_TDENGINE_ENDPOINT`、`WIND_HUB_TEST_TDENGINE_PASSWORD`、
`WIND_HUB_TDENGINE_USER=root`（生产后端强制要求显式 user，裸探测默认 root）、
`WIND_HUB_TEST_TDENGINE_CONTAINER`、`WIND_HUB_TEST_NATS_URL`。

## 2. 静态门禁（全部真实退出码 0）

| 门禁 | 命令 | 结果 |
|---|---|---|
| 环境检查 | `python3 scripts/dev.py env` | PASS（Python 3.11.16，wind-hub） |
| Ruff check | storage / configuration 两范围 | All checks passed |
| Ruff format | 43 + 54 文件 already formatted | PASS |
| mypy | `mypy src` | Success: no issues found in 477 source files |
| import-linter | `lint-imports` | Contracts: 22 kept, 0 broken |
| RPC 一致性 | `python3 scripts/generate_rpc.py --check` | PASS（rc=0） |
| 空白检查 | `git diff --check` | PASS |

## 3. 测试结果（修复后整体复测，非仅修复用例）

| 层级 | 命令 | 结果 |
|---|---|---|
| Storage Unit | `pytest -q tests/storage_service/unit` | **87 passed**（rc=0） |
| Storage Component | `pytest -q tests/storage_service/component` | **11 passed**（rc=0） |
| Storage Integration（真实 Kafka+TDengine） | `pytest -q tests/storage_service/integration -rs` | **7 passed**（rc=0，45.09s；上轮 6 skipped 全部实际执行，零 skip） |
| Configuration→Storage 部署流（真实 NATS+真实 Configuration 进程） | `pytest -q tests/configuration_service/integration/test_storage_deployment_flow.py` | **3 passed**（rc=0） |
| 故障注入 | `pytest -q tests/storage_service/unit/test_storage_crash_faults.py` | **4 passed** |

### 3.1 F5-B 真实 Kafka/TDengine 七用例

1. `test_commit_timeout_replay_restart_and_rebalance_recovery`（真实 Kafka）：
   提交超时重放、进程重启、Rebalance 恢复、连续 Offset。
2. `test_g01_schema_tags_lengths_levels_and_six_scalar_types`：真实建库建表、
   TAG/长度复核、六标量类型（i64/u64/f64/bool/text/bytes）与三级 data_level 保真。
3. `test_g02_composite_key_replay_conflict_and_same_timestamp_points`：
   复合键幂等、同时间戳多设备多变量、异值冲突不静默覆盖。
4. `test_g02_real_write_then_lost_response_readback_and_replay`：
   写入成功响应丢失后 readback 判定，重放不丢不重，不提前 ACK。
5. `test_g02_real_stmt2_timeout_recovers_and_readback_commits_offset`：
   STMT2 超时 UNKNOWN、连接丢弃、恢复后 readback 才提交 Offset。
6. `test_g02_old_generation_response_cannot_commit`：旧 Generation 迟到成功禁止提交水位。
7. `test_sqlite_mapping_reopen_reverifies_real_schema_and_replay`：
   重开真实库复核 Schema、复用既有表、重放幂等；含真实 `docker pause/unpause` TDengine
   暂停恢复 readback（`WIND_HUB_TEST_TDENGINE_CONTAINER` 指向 F5 专用容器）。

### 3.2 F5-C 真实 NATS 与 Configuration 激活（3 用例，全真实链路）

- `test_real_configuration_service_selected_closure`：CLI 启动真实 Configuration 进程，
  完整目录发布（templates/instances/services/deployments 八文档）产生真实 SQLite Revision；
  Storage gRPC 客户端 `GetCurrentRevision`/`GetServiceConfigBundle`/历史 PointTable 闭包
  与设备绑定（WTG01）全部断言通过。
- `test_real_nats_broadcast_after_configuration_publication`：真实 NATS 订阅
  `wh.config.changed`，发布后收到广播并校验 payload `{"revision_id": N}` 与实际 Revision 一致；
  同时实证幂等重放不推进 Revision、不广播的正式契约。
- `test_real_configuration_restart_target_does_not_replace_active`：静态参数
  （`query.max_page_size` 999）发布后 `target_revision` 更新、`active_revision` 保留旧值、
  状态 `RESTART_REQUIRED`、活动快照不变、目标快照持久化且含新限额。

NATS 触发路径以真实广播验收；`refresh_now()` 仅保留为辅助路径，未替代广播验收。
广播丢失的周期调和、重复广播不重复切换由 unit 层
（`test_concurrent_partitions_and_periodic_hint_share_one_pull`、
`test_unrelated_repository_revision_does_not_rewrite_storage_snapshot`）覆盖，
真实 broker 故障注入未做，记为限制项。

### 3.3 F5-D 故障注入与恢复（4 用例 + 相关 unit）

- 快照写入前 fsync 失败：活动快照保持；`os.replace` 失败：保留上一已接受快照；
  目录 fsync 失败：绝不暴露部分 bundle；SQLite 设备绑定事务回滚 semantic pin。
- 同版本不同内容拒绝：`test_fixed_version_cannot_be_rebound_to_different_content`
  及本轮真实 Configuration 的 `immutable configuration file_version cannot be redefined` 实证。
- 崩溃后配置恢复/历史绑定不丢/目标与活动不混淆：
  `test_sqlite_persists_historical_device_bindings_across_restart`、
  `test_snapshot_restores_all_historical_device_bindings_without_main_changes`、
  `test_restart_target_is_durable_without_replacing_active_snapshot`。
- Offset 不早于真实成功确认推进：`test_partition_offsets` 四用例 +
  component `test_failure_before_write_does_not_confirm_any_offset` 等。
- 原子性/持久性边界：测试分别断言替换前（旧版本仍为 CURRENT）与替换后状态，
  未把"文件已替换、目录未 fsync"假定为已持久化。

## 4. 本轮缺陷与修复（提交 1b1ebf06b）

| ID | 级别 | 缺陷 | 根因 | 处置 |
|---|---|---|---|---|
| F5-D1 | P2（测试） | 全文件执行时真实 NATS 广播用例 TimeoutError，单跑通过 | 三用例共用 module 级真实 Configuration 与 SQLite；广播用例重放基线闭包，而相同发布按契约幂等（`replayed=True` 时不广播，repository_server.py:303），顺序在前序发布之后无新 Revision | 广播用例改为发布真实差异化闭包（site 1.0.1 + deployment 1.0.2，避开 static 测试的 1.0.1 不可变版本），断言 Revision 前进且 payload revision 匹配；顺序无关性经重排/单跑双向验证 |
| F5-D2 | P3（测试） | `PytestUnknownMarkWarning: kafka` 未注册 | pyproject marker 清单缺项 | 注册 `kafka` marker |
| F5-D3 | P3（文档） | TDengine 真实测试头未说明 `WIND_HUB_TDENGINE_USER` 等必填变量 | 文档缺口 | 补全模块 docstring（随报告提交） |

无生产代码改动；未修改 Contracts/Core/Configuration 正式契约；未新增兼容层、
未关闭任何校验、未 skip 任何关键断言。

## 5. 性能与备份边界（F5-F）

- 仓库不存在 Storage 专用性能测试设施（`tests/src/performance` 仅覆盖 Sink/DG）；
  设计 §14.4 明确 10 分钟/60 分钟稳态为 System 级验收试验且"尚未执行"，
  禁止把短时峰值报告为持久化吞吐。**T-46～T-53 全部 NOT_RUN**，不做临时 ad-hoc 冲量。
- taosdump：unit 4 用例覆盖真实子进程配额/取消/超时/退出回收（测试用假可执行体）；
  设计 D-11 明确"真实 taosdump 恢复保持 NOT_EXECUTED，不计 C2 PASS，最终验收阶段执行"。
  **真实备份恢复 NOT_RUN，依据 D-11。**

## 6. 设计验收矩阵逐项裁决（§14.2，T-01～T-67）

PASS = 本轮执行的测试直接覆盖；PASS* = 覆盖但含注明的限制；NOT_RUN = 本轮未执行。

| ID | 裁决 | 证据/说明 |
|---|---|---|
| T-01 | PASS | g01 真实自动建库建表查询 |
| T-02 | PASS | g02 多设备同时间戳；device_binding 五类设备用例 |
| T-03 | PASS | 旧模型/旧点表绑定不可用新版本 unit |
| T-04 | PASS | 部署流用例 1 历史点表闭包 + configuration_grpc unit |
| T-05 | PASS | unknown point_table 调和后重试原身份；缺目标 fail-closed |
| T-06 | PASS | 缺版本阻断并保持原 Offset 系列 unit |
| T-07 | PASS* | 周期调和/无关 Revision 不重写 unit 全绿；真实广播已验；真实 broker 丢/重/乱序注入 NOT_RUN |
| T-08 | PASS | crash_faults 4 用例 + 快照重启系列 |
| T-09 | PASS | g02 复合键重放/冲突/同时间戳真实链路 |
| T-10 | PASS | STMT2 超时真实用例 + component UNKNOWN 三用例 |
| T-11 | PASS | reopen 真实 Schema 复核用例 |
| T-12 | PASS | 闭包 SHA/语义文档非法（重复键/别名/哈希）拒绝 unit |
| T-13 | PASS* | 聚合分页 component、SQL 过滤/解码 unit、taosdump 生命周期 unit；真实 taosdump 恢复 NOT_RUN（D-11） |
| T-14 | NOT_RUN | 完整 DG→…→TDengine 全链 E2E 本轮未执行 |
| T-15 | NOT_RUN | 无 Kafka JSON v1 拒收专用测试；v2 真实 Record 已由集成测试覆盖 |
| T-16 | PASS* | 真实 DDL 断言含 `PRECISION 'us'`；KEEP/DURATION 生成但未独立回读 |
| T-17 | NOT_RUN | 无 ALTER DATABASE 测试 |
| T-18 | NOT_RUN | 无 CACHEMODEL/CACHESIZE 热更新测试 |
| T-19 | NOT_RUN | 无 PRECISION/DURATION CONFLICT 测试 |
| T-20 | NOT_RUN | 无 KEEP 缩短授权测试 |
| T-21 | NOT_RUN | 无 ALTER 故障注入测试 |
| T-22 | NOT_RUN | 3.3.6.13 参数能力矩阵专项未执行（版本本身已强制断言） |
| T-23 | PASS* | 全部真实用例经同一命名函数生成物理库表名并回读稳定；无独立碰撞专项 |
| T-24 | NOT_RUN | 无大小写/同名设备碰撞用例 |
| T-25 | NOT_RUN | 无截断哈希碰撞/外部同名表注入 |
| T-26 | PASS | reopen 复核阻断不一致映射真实用例 |
| T-27 | PASS | g02 同时间戳多点 + 同键异值冲突真实用例 |
| T-28 | PASS | 响应丢失 readback/重放真实用例 + component |
| T-29 | PASS | 真实 Rebalance 用例 + 保留窗口缺口 unit |
| T-30 | PASS | reopen 复用既有表不 DROP 真实用例 |
| T-31 | PASS* | 分区阻断/证据隔离 unit（schema conflict isolated 等）；真实 broker poison record 注入 NOT_RUN |
| T-32 | PASS | 单分区阻断不影响其他分区 unit |
| T-33 | PASS | FIFO/连续确认 unit + 真实 Offset 用例 |
| T-34 | PASS | 跨分区并行 unit |
| T-35 | PASS | 旧代际真实用例 + rebalance 代际 unit |
| T-36 | PASS | 仅引用更新与在途写入重叠 unit |
| T-37 | NOT_RUN | 双 Worker 竞争首次 DDL 单飞无专用测试 |
| T-38 | NOT_RUN | ALTER 与写入交叠无测试 |
| T-39 | PASS | 退出/失败不认取消为成功 component + offsets unit |
| T-40 | PASS | g01 三级 data_level 真实写入精确读回 |
| T-41 | PASS* | 三级样本并存真实覆盖；同时间戳三级独立断言未单列 |
| T-42 | PASS | 非法等级/类型拒收 unit（含 decode 严格类型） |
| T-43 | PASS | 六类型无损真实存取；未实施的 Producer 格式按设计 NOT_RUN |
| T-44 | NOT_RUN | engineering/protocol 语义迁移无测试 |
| T-45 | PASS | 跨等级筛选/聚合分页 component + decode unit |
| T-46 | NOT_RUN | 150k 点/s × 10 分钟 System 容量，无设施 |
| T-47 | NOT_RUN | 60 分钟长稳 |
| T-48 | NOT_RUN | 阶梯回退 |
| T-49 | NOT_RUN | 450k Record/s 极限负载 |
| T-50 | NOT_RUN | 慢写积压/背压实测 |
| T-51 | NOT_RUN | 参数对比复测 |
| T-52 | NOT_RUN | 满载故障组合 |
| T-53 | NOT_RUN | 性能准出报告待性能专项后出具 |
| T-54 | PASS | 部署流用例 1：显式选择闭包，无隐式推荐版本 |
| T-55 | PASS* | 无关 Revision 不重写/周期收敛 unit + 真实广播；丢广播真实注入 NOT_RUN |
| T-56 | PASS | 重启目标用例：完整闭包 target、active 不动、RESTART_REQUIRED |
| T-57 | PASS | 设备→模型→点表固定版本与历史绑定系列 unit |
| T-58 | PASS | SQLite 回滚 crash 用例 + 不可变重绑拒绝 |
| T-59 | PASS | generate_rpc --check PASS；本轮未改共享 Contracts（diff 仅测试/pyproject） |
| T-60 | PASS | gRPC 分级鉴权 component 四用例（Bearer/风场范围/管理员/匿名脱敏） |
| T-61 | PASS | 匿名开关默认 false 与脱敏 component 用例 |
| T-62 | PASS | 仅 Deployment 选中版本影响 target；无关发布不切换 unit |
| T-63 | PASS* | target/active/RESTART_REQUIRED/目标快照持久化经真实 Configuration 验证；**Storage 进程 CLI 重启动作本身 NOT_RUN**（watcher 侧语义已验） |
| T-64 | PASS | 候选校验在 SQLite/CURRENT 变更前拒绝 + 故障注入系列 |
| T-65 | PASS | 历史闭包隔离/归档回退/缺证据保 Offset unit |
| T-66 | PASS | 跨分支 RPC 真实存在并被测试调用，无 mock、无 recommended_version 回退 |
| T-67 | PASS | 静态扫描全绿；无内部 TLS/mTLS 路径改动；共享契约零修改 |

合计：PASS/PASS* 46 项，NOT_RUN 21 项（T-46～T-53 性能 8 项、T-14/T-15 全链 2 项、
T-17～T-22 DDL/ALTER 6 项、T-24/T-25 命名碰撞 2 项、T-37/T-38/T-44 共 3 项），FAIL 0 项。

## 7. 残留风险与限制

1. 性能/长稳（T-46～T-53）与完整全链 E2E（T-14）未执行，150k 点/s 目标无实测结论。
2. 真实 taosdump 备份恢复按 D-11 留待最终验收阶段。
3. Storage 进程 CLI 重启应用目标配置的进程级动作未实测（watcher/快照语义已验）。
4. 真实 NATS broker 的丢广播/重复/乱序故障注入、真实 Kafka poison record 注入未做，
   现有证据为同生产代码路径的 unit/component 级。
5. DDL 生命周期（ALTER/KEEP/CACHEMODEL/碰撞）10 个矩阵项无测试设施。
6. 环境依赖：真实集成需显式提供 5 个环境变量（已补文档），CI 若无 Docker/TDengine
   仍会 skip——skip 不得计 PASS。

## 8. 最终裁决

**C2_READY_WITH_LIMITATIONS**。

依据：软件必需门禁（静态五门禁、Unit 87、Component 11、真实 Integration 7、
真实 NATS/Configuration 部署流 3、故障恢复 4）全部通过且零 skip；无 P0/P1/P2 残留
（本轮发现的唯一 P2 测试缺陷已修复并经顺序无关验证）；所有 NOT_RUN 项均有设计明确
依据（System 级性能/长稳、D-11 真实备份恢复、未建设施的 DDL 专项），属非阻断的
后续现场/专项验收范围。是否最终准出由 ChatGPT 裁定；未执行 main 合并、未建 PR。

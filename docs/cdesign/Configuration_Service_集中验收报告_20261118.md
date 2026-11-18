# Configuration Service 集中验收报告

日期：2026-11-18
对象：`feat/configuration-service-v1`
裁决：**FAIL**

## 1. 基线、环境和改动边界

- 起始/被测生产 HEAD：`c961abe48d6421550a5435a9f932fece48306336`。
- `git fetch origin feat/configuration-service-v1` 后，`FETCH_HEAD` 与交接 HEAD 完全一致，`HEAD...FETCH_HEAD = 0 0`。
- 未新建分支、未合并 main、未 rebase、未 force push、未创建 PR。
- 未修改 `src/`、`src/contracts/`、`configs/` 或其他冻结业务模块。
- 最终新增 158 项验收测试；生产代码缺陷保持 OPEN，交回集中整改。
- 环境检查：`python3 scripts/dev.py env`，退出码 0；Python 3.14.7。
- 环境缺陷：初次 Mypy 缺少 `types-grpcio`；按 `pyproject.toml` 安装 `types-grpcio==1.84.0.20261021` 后关闭，未改依赖文件。
- 测试证书、数据库、备份、CLI YAML 和进程状态均在 pytest 临时目录生成，不使用生产资产。

## 2. 静态门禁

| 门禁 | 实际命令 | 退出码 | 结果 |
|---|---|---:|---|
| Ruff Lint | `ruff check --output-format=json src` | 1 | FAIL，9 项 |
| Ruff Format | `ruff format --check src` | 1 | FAIL，14 个文件待格式化，408 个已符合 |
| Mypy strict | `mypy src` | 1 | FAIL，436 个源码文件，3 项错误 |
| import-linter | `lint-imports` | 0 | PASS，18 kept / 0 broken，515 文件、2800 依赖 |
| Proto 一致性 | `python3 scripts/generate_rpc.py --check` | 0 | PASS |
| 新增测试 Ruff | `ruff check tests/configuration_service tests/support/configuration_live.py` | 0 | PASS |
| 新增测试 Format | `ruff format --check tests/configuration_service tests/support/configuration_live.py` | 0 | PASS |
| 补丁检查 | `git diff --check` | 0 | PASS |

Ruff：4×N818、UP035、UP038、2×SIM105、I001。Mypy：`yaml/codec.py:38` 两项，`repository_server.py:499` 一项。未增加 `type: ignore`、`noqa`、规则关闭或排除范围。

架构：三层依赖及业务包隔离通过。Domain/Application 未发现 Protobuf 导入，CLI 未直接访问 Configuration SQLite。现有 import-linter 没有 Configuration Application/Domain 禁止 `*_pb2` 的显式 contract，覆盖缺口记 P2；人工导入审查未发现实际泄漏。

## 3. 测试执行总量

最终命令：

```bash
PYTHONPATH=src python3 -m pytest tests/configuration_service -q
```

- 退出码：1。
- 总数 158：**142 passed / 16 failed / 0 skipped / 0 xfailed / 0 errors**。
- Unit + Component：131 项，125 通过、6 个生产缺陷失败。
- Integration + Reliability：27 项，17 通过、10 个生产缺陷失败。
- 所有真实服务用例均由正式 `python -m cli.main ... service start configuration --yes` 启动独立进程，真实 SQLite 文件、TCP 监听和 mTLS；未使用 mock gRPC Server。
- 失败没有通过修改产品断言、放宽校验、skip 或 xfail 隐藏。

## 4. 设计符合性追溯矩阵

| 设计条款 | 实现位置 | 测试 | 结果 |
|---|---|---|---|
| 三层依赖、业务隔离 | `configuration_service/{domain,application,infrastructure}`、`pyproject.toml` | `lint-imports` | PASS，显式 pb2 contract 有缺口 |
| 严格 YAML、安全标签、重复键、别名、类型 | `outbound/yaml/codec.py` | `unit/test_yaml_acceptance.py` | PASS |
| 全量 Header 与嵌套 Schema | `application/schema_catalog.py`、`yaml/codec.py` | `test_nested_module_schema_drives_complete_header` | FAIL，CS-P1-001 |
| 业务 YAML 无损 SQLite 往返 | `service.py::_restore`、`projection.py` | `test_shipped_yaml_sqlite_yaml_roundtrip` | PASS，43 个可入库真实文档 |
| 自身 YAML 独立启动 | `settings.py`、`main.py` | shipped bootstrap、CLI start、Common restart | FAIL，未知嵌套字段被接受 |
| 23 表、索引、触发器、复合 FK | `sqlite/schema.py` | `test_initial_schema...`、`test_composite_foreign_keys...` | PASS |
| 真实 Device/Point 类型化投影 | `yaml/projection.py` | `test_shipped_relational_documents_create_typed_projection_rows` | FAIL，CS-P1-002 |
| Unit→Semantic→Business→Point 固定 FK | `service.py::_compile_versions`、`projection.py` | `test_unit_semantic_business_point_projection_chain` | PASS |
| 同类型多版本歧义 | `service.py::_compile_versions` | `test_ambiguous_two_unit_versions...` | PASS |
| Schema/user_version/迁移日志/漂移 | `sqlite/schema.py` | unknown version、trigger drift、metadata | PASS |
| 不可变历史、全量 revision、推荐切换 | `sqlite/repository.py` | history、recommend、immutable triggers | PASS |
| 候选、validate_only、TTL、CAS、回滚 | `service.py`、`repository.py` | candidate/CAS/concurrent/rollback/expiry | PASS |
| 持久幂等与跨重启恢复 | `idempotency_requests`、CLI restart | replay、request conflict、real restart | PASS |
| 菱形、共享、多版本闭包、循环、超限 | `repository.py::get_bundle_versions` | typed chain、diamond、cycle、limit | PASS |
| 旧 `templates.root` 精确绑定 | `service.py::_resolve_legacy_sources` | 静态控制流审查 | FAIL，root 值未参与筛选 |
| 废弃及禁止新绑定废弃依赖 | `repository.py` | deprecate/new binding | PASS |
| Online Backup、独立恢复、崩溃 INTERRUPTED | `sqlite/backup.py` | backup、restore、symlink、SIGKILL | PASS，写入期间时序另有空白 |
| 分页稳定并终止 | `repository_server.py` | exact-page ListConfigs | FAIL，CS-P2-001 |
| query/publish/admin | `_actor`、Common `_authorize` | 三身份、备份、发布、快照 | FAIL，Repository 权限错误映射 |
| Common 九 RPC 与 DISK/RUNTIME/CANDIDATE | `common_server.py` | success matrix、bad hash、真实 restart | 主路径 PASS；指标/诊断设计不完整 |
| CLI 全指令与目标拒绝 | `configuration_commands.py`、真实子进程 | CLI success/apply rejection | apply FAIL，其余正常路径 PASS |

## 5. 全部真实 YAML 覆盖矩阵

“解析/导出”指严格解析、完整 Header 输出、重解析、规范化 JSON 与语义哈希一致。“SQLite”指 validate_only、持久候选、原子发布、按 revision 读取、重新解析和全字段/类型比较。

| 文件 | 解析/导出 | SQLite 往返 |
|---|---|---|
| `configs/instances/deliveries.yaml` | PASS | PASS |
| `configs/instances/devices.yaml` | PASS | PASS；类型化 `devices` 行 FAIL |
| `configs/instances/devices_sim.yaml` | PASS | PASS |
| `configs/instances/sinks.yaml` | PASS | PASS |
| `configs/instances/site.yaml` | PASS | PASS |
| `configs/services/cli.example.yaml` | PASS | PASS |
| `configs/services/collector.yaml` | PASS | PASS，连同真实依赖发布 |
| `configs/services/commander.yaml` | PASS | PASS，连同真实依赖发布 |
| `configs/services/data-generator-service.yaml` | PASS | PASS，连同真实依赖发布 |
| `configs/services/data-generator-sim.example.yaml` | PASS | PASS，连同真实依赖发布 |
| `configs/services/industrial_gateway-sim.example.yaml` | PASS | PASS，连同真实依赖发布 |
| `configs/services/industrial_gateway.yaml` | PASS | PASS，连同真实依赖发布 |
| `configs/services/service_configuration_v1.0.0.yaml` | PASS | 按设计拒绝入库；本地启动 PASS，未知嵌套字段 FAIL |
| `configs/services/sink_service.yaml` | PASS | PASS，连同真实依赖发布 |
| `configs/templates/business_points_standard.yaml` | PASS | PASS |
| `configs/templates/deliveries/delivery_realtime_power_v1.0.0.yaml` | PASS | PASS |
| `configs/templates/deliveries/delivery_reliable_history_v1.0.0.yaml` | PASS | PASS |
| `configs/templates/device_models/device_model_bess_sim_physical_v1.0.0.yaml` | PASS | PASS |
| `configs/templates/device_models/device_model_grid_sim_physical_v1.0.0.yaml` | PASS | PASS |
| `configs/templates/device_models/device_model_plant_controller_sim_physical_v1.0.0.yaml` | PASS | PASS |
| `configs/templates/device_models/device_model_pv_sim_physical_v1.0.0.yaml` | PASS | PASS |
| `configs/templates/device_models/device_model_wtg_gw2mw_v1.2.0.yaml` | PASS | PASS |
| `configs/templates/device_models/device_model_wtg_sim_physical_v1.0.0.yaml` | PASS | PASS |
| `configs/templates/device_models/device_model_wtg_tz2mw_v1.1.0.yaml` | PASS | PASS |
| `configs/templates/observability_v1.0.0.yaml` | PASS | PASS |
| `configs/templates/point_tables/point_table_ads_wtg_4xems_v1.0.0.yaml` | PASS | PASS |
| `configs/templates/point_tables/point_table_ads_wtg_gw2mw_v1.0.0.yaml` | PASS | PASS |
| `configs/templates/point_tables/point_table_iec104_wtg_v1.0.0.yaml` | PASS | PASS |
| `configs/templates/point_tables/point_table_modbus_bess_sim_v1.0.0.yaml` | PASS | PASS |
| `configs/templates/point_tables/point_table_modbus_grid_sim_v1.0.0.yaml` | PASS | PASS |
| `configs/templates/point_tables/point_table_modbus_plant_controller_sim_v1.0.0.yaml` | PASS | PASS |
| `configs/templates/point_tables/point_table_modbus_pv_sim_v1.0.0.yaml` | PASS | PASS |
| `configs/templates/point_tables/point_table_modbus_wtg_sim_v1.0.0.yaml` | PASS | PASS；类型化 `point_tables` 行 FAIL |
| `configs/templates/point_tables/point_table_modbus_wtg_tz2mw_v1.0.0.yaml` | PASS | PASS |
| `configs/templates/protocols/protocol_ads_v1.2.0.yaml` | PASS | PASS |
| `configs/templates/protocols/protocol_iec104_v1.1.0.yaml` | PASS | PASS |
| `configs/templates/protocols/protocol_modbus_tcp_v1.1.0.yaml` | PASS | PASS |
| `configs/templates/sinks/sink_gateway_realtime_v1.0.0.yaml` | PASS | PASS |
| `configs/templates/sinks/sink_kafka_history_v1.0.0.yaml` | PASS | PASS |
| `configs/templates/sinks/sink_kafka_history_v1.1.0.yaml` | PASS | PASS |
| `configs/templates/sinks/sink_modbus_registers_v1.0.0.yaml` | PASS | PASS |
| `configs/templates/sinks/sink_redis_realtime_v1.0.0.yaml` | PASS | PASS |
| `configs/templates/tasks/task_turbine_all_v1.0.0.yaml` | PASS | PASS |
| `configs/templates/units_standard.yaml` | PASS | PASS |

合计：44/44 严格解析及导出重解析 PASS；43/43 可入库文档 SQLite 往返 PASS；1 个自身启动文件按设计不入库。该结果不抵消 Schema、Header 和类型化关系投影缺陷。

## 6. Repository RPC 矩阵

| RPC | 已执行成功路径 | 失败/边界结果 | 裁决 |
|---|---|---|---|
| CreateCandidate | validate_only、暂存、幂等 | 空 request_id、文档/候选配额、权限错误码 FAIL | FAIL |
| PublishCandidate | 原子提交、CAS、replay、跨重启 | 同 file_version 不同内容返回 INTERNAL，非 ALREADY_EXISTS | FAIL |
| ListConfigs | 过滤、快照、游标篡改拒绝 | 最后一页仍返回 next_page_token | FAIL |
| ListConfigVersions | 历史版本、字段、时间戳 | 精确末页和废弃水位分页未专项执行 | 覆盖不足 |
| GetConfig | version/revision、双哈希、缺 selector、不存在 | PASS |
| GetConfigBundle | 固定旧版本闭包、manifest、拓扑 | 包字节超限未执行 | 覆盖不足 |
| ListReferences | inbound/outbound、空边、精确版本 | 分页末页未专项执行 | 覆盖不足 |
| SetRecommendedVersion | previous presence、历史不变、跨修订 | PASS |
| DeprecateConfigVersion | 新 revision、引用计数 | publish 身份权限映射 INTERNAL | FAIL |
| GetRepositoryStats | 计数、Schema、存储 optional presence | PASS |
| CreateBackup | 真实任务、幂等、独立备份 | query/publish 权限映射 INTERNAL | FAIL |
| GetBackupTask | QUEUED→SUCCEEDED、哈希、大小、NOT_FOUND、INTERRUPTED | PASS |

## 7. Common RPC 矩阵

| RPC | 结果 |
|---|---|
| GetServiceInfo | PASS，身份、角色、版本、启动时间 |
| GetServiceStatus | 主路径 PASS；只检查 SQLite readable，未覆盖设计要求的完整依赖健康 |
| GetCapabilities | PASS，21 个 RPC 名称与特性 |
| GetConfigSnapshot | PASS，DISK/RUNTIME/CANDIDATE、坏 scope、坏 hash、query 拒绝 |
| PushConfigSnapshot | PASS，真实候选、有效/无效 YAML、revision CAS |
| ApplyConfigSnapshot | PASS，重启前 RUNTIME 不变，真实 restart 后 RUNTIME=新 DISK |
| GetMetrics | 基础计数 PASS；速率、延迟、容量、失败和备份指标未实现完整设计 |
| GetEvents | 查询及敏感内容检查 PASS；授权失败等设计事件未完整实现 |
| Diagnose | SQLite 主路径 PASS；未实现设计的完整存储/引用/WAL/路径诊断 |

## 8. CLI 矩阵

| 指令 | 结果 |
|---|---|
| candidate / publish | 正常路径、幂等、真实 mTLS PASS；服务端权限错误码见 CS-P1-004 |
| list / versions / get / bundle / references | PASS；文件 0600、bundle 目录 0700、哈希验证、禁止覆盖 |
| recommend / deprecate | PASS |
| stats | PASS |
| backup / backup-status | PASS，真实 SUCCEEDED、INTERRUPTED、幂等 task_id |
| apply | **FAIL**：目标返回 `valid=false`、`applied=false` 时 CLI 仍 `SUCCESS`、退出码 0；目标文件未改变 |
| service start / stop / restart | PASS，真实进程、健康确认、锁释放 |

## 9. SQLite、备份、安全与恢复

- 首次建库、23 表、36 个不可变触发器、复合 FK、WAL、`db_meta=user_version=1`、迁移日志、未知版本和漂移拒绝：PASS。
- 多版本、全量 revision、不可变历史、推荐切换、逻辑废弃、CAS 单赢家、整批回滚、重启读取：PASS。
- Unit/Semantic/Business/Point 固定版本投影、共享/菱形/循环/闭包超限：PASS。
- 真实 Device 与部分真实点表的类型化投影：FAIL。
- Online Backup、SHA-256、大小、Schema、integrity/FK check、独立数据库重新初始化及历史/审计读取：PASS。
- 备份根 symlink 拒绝、任务幂等/配额、临时文件清理：PASS。
- SIGKILL 后未完任务为 INTERRUPTED；候选及 publish replay 跨真实重启；损坏数据库启动 fail-closed：PASS。
- query/publish/admin 正常身份、未登记 CN、错误 CA、无证书、明文降级：传输拒绝 PASS；Repository 授权错误码 FAIL。
- 停止后 flock 可重新获取、临时 `.part` 清理、日志无私钥/游标密钥明文：PASS。

## 10. 生产缺陷台账（全部 OPEN）

### P0

无。

### P1

| ID | 位置与复现 | 期望/实际 |
|---|---|---|
| CS-P1-001 | `application/schema_catalog.py:34-137`、`application/service.py:115-161`；`test_nested_module_schema...`、`test_unknown_nested_service...` | 应执行嵌套 Schema 并生成全路径 Header；实际只校验顶层类型、接受未知嵌套字段、Header 无嵌套规则 |
| CS-P1-002 | `yaml/projection.py:121-183,218-221`；真实 devices/point table projection 测试 | 应生成 `devices`、`point_tables` 及复合 FK 行；实际真实样例只生成通用 JSON，目标表 0 行 |
| CS-P1-003 | `application/service.py:380-400`；静态控制流审查 | `templates.root` 应限定精确依赖；实际路径值不参与筛选，把所选 revision/staged 的全部模板加入闭包 |
| CS-P1-004 | `inbound/grpc/repository_server.py:81-94,117-126`；query/publish 权限测试 | 应为 PERMISSION_DENIED；`guarded` 将 `context.abort` 异常再次映射为 INTERNAL |
| CS-P1-005 | `cli/configuration_commands.py:398-410`、`cli/runtime.py:472-509`；`test_cli_apply...` | 目标拒绝不得成功；实际 `applied=false` 仍 SUCCESS/0 |
| CS-P1-006 | `sqlite/repository.py:507-517`、`repository_server.py:63-78`；`test_same_file_version...` | 同版本不同内容应 ALREADY_EXISTS；SQLite IntegrityError 未转换，实际 INTERNAL |
| CS-P1-007 | `application/service.py:203-206`、`repository_server.py:163-176`、`sqlite/repository.py:319-325`；quota 测试 | 超配额应 RESOURCE_EXHAUSTED；文档数返回 valid=false，候选容量返回 FAILED_PRECONDITION |
| CS-P1-008 | Ruff/Mypy 位置见第 2 节 | 静态门禁应全部 0；实际 Ruff 9、Format 14、Mypy 3，准出阻断 |
| CS-P1-009 | `settings.py:69-131`；`test_bootstrap_rejects_unknown_nested_fields` | 启动配置未知嵌套键应拒绝；实际只限制 root，未知 `grpc` 键被静默接受 |

### P2

| ID | 位置与复现 | 期望/实际 |
|---|---|---|
| CS-P2-001 | `repository_server.py:324-339`（同类代码 380-391、428-440）；精确末页测试 | `next_page_token` 空表示结束；实际有行即返回 token，末页多一次空请求 |
| CS-P2-002 | `repository_server.py:163-176`、`sqlite/repository.py:297-298`；空 request_id 测试 | 必填缺失应 INVALID_ARGUMENT；实际返回 valid=false/SCHEMA_INVALID |
| CS-P2-003 | `common_server.py:64-84,213-307`、`sqlite/repository.py:212-232` | 设计要求的完整指标、依赖健康、诊断及失败/授权审计未实现；当前只有基础计数、SQLite readable 和成功审计 |
| CS-P2-004 | `pyproject.toml` import-linter contracts | 缺少 Configuration Application/Domain 禁止 pb2 的显式静态 contract；人工检查当前无泄漏 |

### P3

无。

## 11. 设计冲突

- **CS-D-001 / OPEN**：Configuration v1.5 §13.1 写 `query/publish/admin/self_config` 四类权限；任务、Proto 适配和 `settings.py` 使用三类，Common 自配置复用 admin。需明确 `self_config` 是否独立角色。
- **CS-D-002 / OPEN**：Contracts §10.3.1 提供 `valid/issues` 候选校验响应，公共错误表又把 YAML/Schema 非法列为 INVALID_ARGUMENT。当前实现采用 `valid=false`；需统一错误表达后再修改测试或实现。

## 12. 测试缺陷和环境处置

已关闭 TEST_DEFECT：YAML bytes 断言、真实类型映射、带依赖服务的暂存前置、同批多版本 Fixture 去重、多文档返回顺序假设、apply CAS 前置、PKI 目录创建、测试变量类型/遮蔽。已关闭 ENVIRONMENT：安装项目声明的 gRPC stubs。无 UNRELATED 失败计入本模块。

新增测试文件：

- `tests/configuration_service/conftest.py`
- `tests/configuration_service/unit/test_yaml_acceptance.py`
- `tests/configuration_service/component/test_shipped_yaml_repository.py`
- `tests/configuration_service/component/test_repository_transactions.py`
- `tests/configuration_service/component/test_sqlite_backup.py`
- `tests/configuration_service/component/test_typed_references.py`
- `tests/configuration_service/component/test_schema_completeness.py`
- `tests/configuration_service/integration/conftest.py`
- `tests/configuration_service/integration/test_repository_rpc.py`
- `tests/configuration_service/integration/test_common_rpc.py`
- `tests/configuration_service/integration/test_cli_integration.py`
- `tests/configuration_service/integration/test_contract_validation.py`
- `tests/configuration_service/integration/test_security.py`
- `tests/configuration_service/integration/test_version_conflict.py`
- `tests/configuration_service/reliability/test_backup_process_restart.py`
- `tests/support/configuration_live.py`

未新增静态 Fixture 文件；运行时 YAML、SQLite、证书均由上述测试 Fixture 在临时目录生成。

## 13. NOT_RUN / 限制项

- 未对第二个独立业务服务执行成功的 `configuration apply`，因此“未列出的目标文件保持不变”只在拒绝路径验证，成功覆盖路径未验收。
- 未构造确定性的“高写入并发期间 Online Backup”时序；已验证 Online Backup、独立恢复和进程崩溃恢复。
- 未执行真实迁移脚本中途崩溃；首版无后续迁移脚本，已测未知版本及 Schema/触发器漂移。
- 未专项执行 ListConfigVersions/ListReferences 末页、GetConfigBundle 字节超限、备份任务 gRPC 配额和最大并发 RPC 饱和。
- 未做进程全部 OS 线程/FD 的穷举核查；已验证 flock、进程停止、临时文件和日志敏感内容。
- 未在生产目录执行恢复切换；恢复只在独立测试数据库完成。
- 以上均为 NOT_RUN，不计 PASS，也不掩盖已确认的 FAIL。

## 14. 最终裁决

**FAIL**。

准出条件不满足：P0=0，但 P1=9 个 OPEN；Ruff/Format/Mypy 非零；多个必需 RPC/CLI 负向契约失败；真实 Device/Point 关系投影缺失；仍有上述关键测试空白。不得判定为“基本通过”或生产可投入。

生产整改后，应在同一生产基线或明确的新 HEAD 上重跑第 2、3 节全部命令，并优先关闭 P1，再补齐 NOT_RUN 项。

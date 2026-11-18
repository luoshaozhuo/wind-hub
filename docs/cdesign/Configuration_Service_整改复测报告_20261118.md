# Configuration Service v1.8 集中整改复测报告

日期：2026-11-18
生产基线：`e893b64ae81e98edffb9926bf75484f484708e17`
最终裁决：**FAIL**

## 1. 基线与执行边界

- 分支 `feat/configuration-service-v1`；开始和复测时本地、远端均为 `e893b64a`，`HEAD...FETCH_HEAD = 0 0`。
- 上轮测试提交 `882ceee4` 已在祖先链中；未覆盖、rebase、新建分支、合并 main 或创建 PR。
- 未修改 `src/`、Proto、`configs/` 或冻结业务模块。仅修改测试、Fixture 和本报告。
- 环境检查 `python3 scripts/dev.py env`：PASS，Python 3.14.7。
- 真实链路均使用独立临时 SQLite、PKI、配置和正式 CLI 进程；没有 mock gRPC Server、skip 或 xfail。

## 2. 静态门禁

| 命令 | 退出码 | 结果 |
|---|---:|---|
| `ruff check src` | 1 | FAIL，3 项：2×I001、UP035 |
| `ruff format --check src` | 1 | FAIL，14 个文件待格式化，409 个已符合 |
| `mypy src` | 1 | FAIL，437 文件，6 项错误 |
| `lint-imports` | 0 | PASS，19 kept / 0 broken，包含 Configuration Application/Domain 禁止 Protobuf |
| `python3 scripts/generate_rpc.py --check` | 0 | PASS |
| `git diff --check` | 0 | PASS |
| `ruff check tests/configuration_service tests/support/configuration_live.py` | 0 | PASS |
| `ruff format --check tests/configuration_service tests/support/configuration_live.py` | 0 | PASS |

Mypy 位置：`schema_catalog.py:190-191` 两项、`projection.py:168`、`repository_server.py:114,554`、`common_server.py:61`。Ruff 位置：`common_server.py:3`、`repository_server.py:12`、`main.py:3`。生产异常重命名后的测试导入已同步；仓库中无旧异常类名引用。新增 Metrics、RepositoryPort、YAML Adapter 尚未通过 Mypy strict，CS-P1-008 不能关闭。

## 3. 测试数量与原有用例复测

最终命令：

```bash
PYTHONPATH=src python3 -m pytest tests/configuration_service -q
```

- 退出码 1；**179 项：165 passed / 14 failed / 0 skipped / 0 xfailed / 0 errors**。
- 原 158 项首次在新生产 HEAD 上运行：146 passed / 12 failed。
- 修正旧异常导入、专用容量异常、未绑定旧 YAML 语义、诊断顺序、分页空 token 后续调用后，原 158 项为 **151 passed / 7 failed**。
- 新增 21 项：14 passed / 7 failed。
- 失败均为生产缺陷或静态门禁阻断，不以放宽断言制造 PASS。

## 4. 13 项历史缺陷关闭矩阵

| 缺陷 | 实际证据 | 状态 |
|---|---|---|
| CS-P1-001 | `security.callers` 真实数组被声明 object；7 个真实服务 YAML 失败；数组 items/nested required 未执行；重复 Schema 注册抛裸 SQLite IntegrityError | **OPEN** |
| CS-P1-002 | 完整 6 版本链生成 Unit/Semantic/Business/PointTable/PointMapping/DeviceModel/Device 行；Device→Model/Table 复合 FK、共享型号、升级历史和中途回滚通过 | **CLOSED** |
| CS-P1-003 | `configs/other`、相对穿越、绝对 root 均拒绝；合法 `configs/templates` 用例通过 | **CLOSED** |
| CS-P1-004 | query/publish/admin、无权限、Common 权限路径通过；但受信 CA 的无 CN 证书在 Repository/Common 均返回 PERMISSION_DENIED，非 UNAUTHENTICATED | **OPEN** |
| CS-P1-005 | 拒绝应用非零且文件不变；真实 Commander 完整快照应用成功，devices 更新，未列出 site/model 字节不变 | **CLOSED** |
| CS-P1-006 | 同 file_version 不同内容真实 RPC 返回 ALREADY_EXISTS | **CLOSED** |
| CS-P1-007 | 文档、候选、备份配额返回 RESOURCE_EXHAUSTED；但 Bundle 字节/闭包超限仍返回 INVALID_ARGUMENT | **OPEN** |
| CS-P1-008 | Ruff 3、Format 14、Mypy 6，均非零 | **OPEN** |
| CS-P1-009 | 自身 YAML 未知嵌套字段拒绝，CLI 启动与重启用例通过 | **CLOSED** |
| CS-P2-001 | ListConfigs/ListConfigVersions/ListReferences 末页为空 token；历史分页中新增第三版本和废弃第二版本不污染水位 | **CLOSED** |
| CS-P2-002 | 空 request_id 真实 RPC 返回 INVALID_ARGUMENT | **CLOSED** |
| CS-P2-003 | 实测速率、平均/P95、失败数、备份五状态、权限审计通过；删除数据库文件后新空库仍报告 HEALTHY，缺少 Schema/元数据健康校验 | **OPEN** |
| CS-P2-004 | import-linter 显式 contract 存在并通过 | **CLOSED** |

汇总：**8 CLOSED / 5 OPEN**。当前 P0=0、P1=4、P2=1、P3=0。

## 5. 完整 Device/Point 外键链

测试 `test_full_device_point_dependency_chain_creates_composite_foreign_key_rows` 实际发布：

`Units → SemanticPoint → BusinessPoint → PointTable → DeviceModel → Device`

所有引用明确为 `config_type + config_id + file_version`，点表设备类型为无歧义 `turbine`。

- 6 个版本；2 个 Unit、2 个 Semantic、2 个 Business、1 个 DeviceModel、1 个 PointTable、2 个 PointMapping、2 个 Device。
- Semantic 的 `standard_unit_version_id`、Business 的 `semantic_config_version_id`、Mapping 的 Business/Unit 版本均指向固定目标。
- 两台设备共享同一型号和点表；`model_version_id` 与 `point_table_version_id` 均正确。
- `PRAGMA foreign_key_check=[]`、`integrity_check=ok`，历史闭包包含完整 6 版本。
- 型号 v1→v2 升级后，旧 revision 仍返回旧型号绑定，新 revision 使用新型号。
- 将 Device 的 model FK 在事务内改为不存在版本，发布抛 IntegrityError，revision=0、config_versions=0，整批回滚。
- 孤立真实 devices/point table 按 v1.8 保留 canonical JSON、类型化表为 0，不制造假 FK；与完整依赖场景分开裁决。

## 6. YAML、Schema 与 Header

- **44/44** 真实 YAML 严格解析、Header 导出、重解析、规范化结构及语义哈希一致。
- **36/43** 可入库真实 YAML SQLite 往返通过。
- 7 个失败文件：Collector、Commander、Data Generator 两份、Industrial Gateway 两份、Sink Service；共同根因 `document.security.callers` 实际为数组，Schema 却为 object。
- 自身启动 YAML 按设计不入库；未知启动嵌套字段拒绝。
- Unicode、显式 null、布尔/整数/浮点、数组和注释往返测试通过。
- 新增 Schema 测试证明数组 items 和嵌套 required 未实现；非法/重复 Schema 注册没有转换为领域冲突异常。CS-P1-001 保持 OPEN。

## 7. Repository RPC 矩阵

| RPC | 结果 |
|---|---|
| CreateCandidate | 主路径、validate_only、幂等、文档/候选配额、空 request_id 通过；真实服务 callers Schema 阻断 |
| PublishCandidate | CAS、ALREADY_EXISTS、幂等、跨重启、整批回滚通过 |
| ListConfigs | 过滤、快照、游标、精确末页通过 |
| ListConfigVersions | 精确末页、新增版本和废弃水位通过 |
| GetConfig | version/revision、双哈希、selector 和不存在错误通过 |
| GetConfigBundle | 固定历史闭包通过；字节与闭包超限错误码 FAIL |
| ListReferences | inbound/outbound、空边、精确末页通过 |
| SetRecommendedVersion | previous presence、历史不变通过 |
| DeprecateConfigVersion | 权限、引用计数、废弃规则通过 |
| GetRepositoryStats | 计数、Schema、存储 presence 通过 |
| CreateBackup | 幂等、真实执行、容量 RESOURCE_EXHAUSTED、权限拒绝通过 |
| GetBackupTask | SUCCEEDED、FAILED、INTERRUPTED、NOT_FOUND 和元数据通过 |

## 8. Common RPC 矩阵

| RPC | 结果 |
|---|---|
| GetServiceInfo / GetCapabilities | PASS |
| GetServiceStatus | 正常依赖检查 PASS；数据库文件丢失后的空库误报 HEALTHY |
| GetConfigSnapshot | DISK/RUNTIME/CANDIDATE、hash、scope、query 权限拒绝 PASS |
| PushConfigSnapshot | 候选、无效 YAML、CAS PASS |
| ApplyConfigSnapshot | restart_required、真实重启后 RUNTIME=新 DISK PASS |
| GetMetrics | 无样本、速率、平均/P95、失败数、SQLite 和备份计数 PASS |
| GetEvents | 成功、业务失败、Repository/Common 权限拒绝审计 PASS |
| Diagnose | sqlite/references/wal/storage 正常 PASS；数据库丢失故障 FAIL |

## 9. CLI 与真实第二业务服务

全部 candidate、publish、list、versions、get、bundle、references、recommend、deprecate、stats、backup、backup-status、apply 以及 service start/stop/restart 均实际执行。

- Configuration 与 Commander 均通过正式 CLI 启动，真实 mTLS、SQLite 和网络监听。
- 依次发布两个点表、两个型号及真实 devices 共 5 个版本。
- CLI 先读取 Commander DISK 快照，应用完整快照时只替换 devices；`applied=true`、退出码 0。
- 目标 devices 内容改变；未列出的 site 与 DeviceModel 文件字节不变。
- 被拒绝 apply 非零且文件不变；文件权限、bundle manifest/hash 检查通过。

## 10. 指标、审计、备份和故障恢复

- `RpcMetrics` 空窗口样本为 0，只表示未采样；并发 8 个成功请求加失败请求后，请求数、失败数、速率、平均和 P95 均为实测正值。
- QUEUED/RUNNING/SUCCEEDED/FAILED/INTERRUPTED 各构造一条持久任务，五个指标准确为 1；备份容量 RPC 为 RESOURCE_EXHAUSTED。
- CreateBackup 和 CommonService.Authorization 拒绝事件可检索；日志/事件不含私钥。
- 3000 条初始审计写入加备份期间 100 次 WAL 写入，Online Backup SUCCEEDED，备份 integrity/FK 通过，审计快照数在 3000–3100 内。
- SIGKILL 后任务 INTERRUPTED，候选及 publish replay 跨重启，锁释放；损坏数据库启动 fail-closed。
- 删除运行中数据库文件后，SQLite 自动新建空库，quick_check/FK/WAL/disk 仍全绿，健康状态错误为 HEALTHY；恢复临时文件后完成清理。

## 11. 原设计冲突

- CS-D-001：**CLOSED**。v1.8 明确只有 query/publish/admin，自身配置归 admin，不设 self_config。
- CS-D-002：**CLOSED**。Contracts v1.33 明确请求协议错误为 INVALID_ARGUMENT，YAML/Schema 内容为 `valid=false + issues`。

## 12. 测试与 Fixture 修改

- 同步异常后缀：`test_repository_transactions.py`、`test_sqlite_backup.py`。
- 完整依赖、未绑定、升级历史及 FK 回滚：`test_typed_references.py`。
- WAL 并发备份：`test_sqlite_backup.py`。
- Schema/数组/注册回归：新增 `test_schema_regressions.py`。
- 无样本指标：新增 `unit/test_rpc_metrics.py`。
- 真实指标审计和故障健康：新增 `test_observability.py`。
- 历史分页：新增 `test_pagination_history.py`；修正原 ListConfigs 空 token 调用。
- Bundle 配额：新增 `test_bundle_limits.py`。
- 无 CN PKI：`configuration_live.py`、`test_security.py`。
- 真实 Commander apply：`test_cli_integration.py`。
- 诊断阶段集合断言：`test_common_rpc.py`。

未修改任何生产文件。

## 13. NOT_RUN 与限制

- 未做传输层 maximum_concurrent_rpcs 的确定性饱和；已做 8 路真实并发 Repository 请求及备份任务配额饱和。
- 首版没有后续迁移脚本，未执行数据库升级中途崩溃；未知版本、Schema/触发器漂移和内置 Schema 冲突 fail-closed 已覆盖。
- 未执行长稳或性能 SLA 测试；本轮指标只验证真实测量语义，不设虚构性能阈值。
- Commander apply 后未强制重启 Commander；Configuration 自身 DISK/RUNTIME 的真实重启已验证。
- 上述项目不计 PASS；即使不计这些空白，现有 P1 和非零静态门禁已足以判 FAIL。

## 14. 最终裁决

**FAIL**。

13 项历史缺陷仍有 5 项 OPEN，P1=4；Ruff、Format、Mypy 非零；43 个可入库真实 YAML 有 7 个失败；Bundle 配额、无 CN 认证及数据库故障健康检查不符合契约。不得判为生产可投入。

生产整改后应在明确的新 HEAD 重跑本报告全部静态命令和 179 项测试，再补齐 NOT_RUN 项。

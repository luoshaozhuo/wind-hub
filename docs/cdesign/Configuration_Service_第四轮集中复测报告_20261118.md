# wind-hub Configuration Service 第四轮集中复测报告

日期：2026-11-18
裁决：**PASS（满足准出标准，限制项见第 8 节）**
交接 HEAD：`22cabe3db3dfc9cc478778b85d2ec6d9d2f54a81`
设计依据：Configuration Service v1_10、Contracts v1_33。

## 1. 基线、分支与执行边界

- 仓库：`luoshaozhuo/wind-hub`；固定分支：`feat/configuration-service-v1`；未新建分支、未创建 PR、未合并 main、未 rebase、未 force push。
- 开始时执行 `git fetch --all --prune`。本地 HEAD 原为 `f46b05b31`，落后远端交接 SHA；工作树干净，经 `git pull --ff-only` 快进至 `22cabe3db`，无合并提交。
- 环境检查 `python3 scripts/dev.py env`：PASS。解释器 `/home/luosh/miniconda3/envs/wind-hub/bin/python3`，Python 3.11.16，pytest 8.4.2 / ruff / mypy / importlinter 齐备。
- 本轮复测期间测试代码与 Fixture **零改动**：`git diff 3e6c24dd..22cabe3db -- tests/ tests/support/` 为空。第三轮失败的同 6 个用例、同 194 项集合在本轮原样重跑，转绿全部来自生产修复，不存在改断言、skip、xfail、放宽规则或 mock 替代。
- 验收方仅做一处生产文件的纯 Ruff 格式化（见 CS-P2-006），格式化前后 AST 转储全等；未触碰任何业务规则。
- 全部测试使用真实临时 SQLite（WAL 模式、真实 FK/触发器）、真实 PKI/mTLS、正式 CLI 进程与真实应用适配器；全树扫描无 `skip`/`xfail` 标记。

ChatGPT 本轮四个生产修复提交（均在交接 HEAD 内）：

| 提交 | 内容 | 对应缺陷 |
|---|---|---|
| `380dd0ef0` | 无 `items` 数组失败关闭；超限 YAML 在重解析前拒绝 | CS-P1-001A / CS-P1-007A |
| `8051f7574` | 内置集合补 `items` 契约；callers Header 三字段标必填 | CS-P1-001A / CS-P2-005 |
| `2e8edd28a` | Point Mapping 绑定已解析的 `point_table_device_type` | CS-P1-010 |
| `2ef40dbd7` | gRPC 发送上限 = YAML 配额 + Protobuf 传输预留，YAML 配额不放松 | CS-P1-007B |

## 2. 静态门禁

交接 HEAD 初次执行时 7/8 通过，唯一失败为 `ruff format --check src`：`schema_catalog.py` 存在换行漂移（修复提交 `8051f7574` 引入，详见 CS-P2-006）。验收方应用 `ruff format` 单文件纯格式化（AST 全等）后，在最终 HEAD 重跑全部命令：

| 实际命令 | 退出码 | 结果 |
|---|---:|---|
| `ruff check src` | 0 | All checks passed |
| `ruff format --check src` | 0 | 423 files already formatted |
| `mypy src` | 0 | Success: no issues found in 437 source files |
| `lint-imports` | 0 | Contracts 19 kept / 0 broken |
| `python3 scripts/generate_rpc.py --check` | 0 | PASS，Proto 生成物无漂移 |
| `git diff --check` | 0 | PASS |
| `ruff check tests/configuration_service tests/support/configuration_live.py` | 0 | All checks passed |
| `ruff format --check tests/configuration_service tests/support/configuration_live.py` | 0 | 22 files already formatted |

六项核心静态门禁全部退出码 0；另加测试侧两门同样为 0。

## 3. 全量回归

实际命令：

```bash
PYTHONPATH=src python3 -m pytest tests/configuration_service -q -p no:warnings --tb=short
```

- 退出码：**0**。
- **194 项：194 passed / 0 failed / 0 skipped / 0 xfailed / 0 errors**，耗时 78.22s。
- 用例集合与第三轮完全同集（194 项），当时 6 失败本轮全部转绿，无任何用例被删除或弱化。

## 4. 六个失败用例逐项复测证据

六项节点一次性实跑：`6 passed in 5.45s`，逐项如下。

### CS-P1-001A：缺失数组 `items` 的 Schema 必须拒绝 — CLOSED

- 用例：`component/test_schema_regressions.py::test_array_schema_without_items_fails_closed` — **PASSED**。
- 生产证据（`380dd0ef0`，service.py:142-148）：`items` 非 dict 时直接 `raise ValueError("array schema requires items ...")`，不再静默跳过元素校验，失败关闭。
- 内置 Schema 同源加固（`8051f7574`，schema_catalog.py）：`config_sources` 及所有 array 类字段显式声明 `items: {"type": "object"}`，`permissions.items` 保留枚举契约；44/44 真实 YAML 回归不受影响。

### CS-P1-007A：单文件导出超限返回 RESOURCE_EXHAUSTED — CLOSED

- 用例：`integration/test_bundle_limits.py::test_single_document_export_limit_is_resource_exhausted` — **PASSED**（真实 CLI 拉起的 mTLS gRPC 服务端到端断言）。
- 生产证据（`380dd0ef0`，service.py:519-520）：YAML  emit 后、重解析前先比较 `len(data) > limits.max_bundle_bytes`，超限抛 `RepositoryCapacityExceededError`，经 gRPC 映射为 `RESOURCE_EXHAUSTED`；不再先触发 `ValueError` 而被误映射成 `INVALID_ARGUMENT`。

### CS-P1-007B：配额精确边界 gRPC 正常发送、超限仍应用层拒绝 — CLOSED

- 用例：`integration/test_bundle_limits.py::test_grpc_transport_allows_bundle_at_exact_yaml_quota` — **PASSED**。
- 配套应用层证据：`component/test_bundle_exact_limits.py::test_exact_single_document_and_bundle_limits_succeed` — **PASSED**，覆盖单文件、Bundle 总字节、依赖闭包恰好等于上限成功及超一字节失败。
- 生产证据（`2ef40dbd7`，main.py:131-145）：`grpc.max_send_message_length = max_bundle_bytes + protobuf_headroom`，headroom 取 `max(1 MiB, min(max_dependencies*4096, 64 MiB))` 承载 manifest/版本元数据/哈希等 Protobuf 帧开销；**YAML 应用层配额数值不变**，超限仍由应用层拒绝（断言 `details == "configuration bundle exceeds response limit"`）。v1_10 设计已冻结该传输预留契约。

### CS-P1-010：固定版本复合外键链与两个投影回归 — CLOSED

- 用例（2 项，均 **PASSED**）：
  - `component/test_typed_references.py::test_unit_semantic_business_point_projection_chain`
  - `component/test_typed_references.py::test_full_device_point_dependency_chain_creates_composite_foreign_key_rows`
- 生产证据（`2e8edd28a`，projection.py:196）：Point Mapping 行改用已解析的 `point_table_device_type`，消除仅在其他分支赋值导致的 `UnboundLocalError`；完整 Device/Point 固定版本复合外键链发布恢复。

### CS-P2-005：callers 三必填字段 Header 标注 — CLOSED

- 用例：`component/test_schema_regressions.py::test_caller_header_declares_complete_required_item_fields` — **PASSED**。
- 生产证据（`8051f7574`，schema_catalog.py:216-220）：`caller_id`、`certificate_identity`、`permissions` 的 `_field()` 统一传 `required=True`，导出 Header 实测包含 `# security.callers.<item>.<field>: 必填`，与机器 Schema 的 `required` 三项一致。

汇总：第三轮 P1=3、P2=1 共 5 个缺陷、6 个失败用例，本轮 **全部 CLOSED**。

## 5. 生产修复真实性核查

- 四个修复提交仅触及 4 个生产文件（service.py、schema_catalog.py、projection.py、main.py），diff 小而聚焦，无任何测试/Fixture 伴随修改。
- `git diff 3e6c24dd..22cabe3db -- tests/ tests/support/` 为空：第三轮同一批测试在未改一字的情况下由 6 failed 转为 194 全绿，排除“改测试造 PASS”。
- 验收方唯一生产改动是 `schema_catalog.py` 的 Ruff 换行（3 行折行并 1 行），`ast.dump` 前后全等证明无语义变化，单独成提交。
- 未发现修复引入的新业务规则偏差；Proto `--check` 退出码 0，契约生成物零漂移。

## 6. 关键面复核证据

| 复核项 | 实际命令/范围 | 结果 |
|---|---|---|
| 44/44 真实 YAML 严格解析与无损 Header 往返 | `unit/test_yaml_acceptance.py::test_shipped_yaml_strict_parse_and_lossless_header_roundtrip`（44 参数化） | 两节点合计 **87 passed**，与 44+43 精确对应 |
| 43/43 SQLite 发布-导出-重解析往返 | `component/test_shipped_yaml_repository.py::test_shipped_yaml_sqlite_yaml_roundtrip`（43 参数化） | 同上 87 项内全绿；两文件全量 106 passed |
| 12 个 Repository RPC | `integration/test_repository_rpc.py`（3 项，真实 CLI+mTLS 服务，模块文档字符串 "All twelve Repository RPCs"） | **3 passed**，成功矩阵/失败与在场性/精确分页边界全绿 |
| 9 个 Common RPC | `integration/test_common_rpc.py`（2 项，"All nine Common RPCs"，能力断言 `rpc_names == 21` = 12+9） | **2 passed**，含 DISK/RUNTIME/CANDIDATE、陈旧候选与坏哈希拒绝 |
| 全部 CLI 指令 | `integration/test_cli_integration.py` 4 项 | **4 passed**，含拒绝覆盖、查询身份不能发布、目标拒绝时不谎报成功 |
| 真实 Commander apply | `test_cli_apply_devices_to_real_commander_preserves_unlisted_files` | **PASSED**，真实 Commander 进程 apply，未列出文件保持不变 |
| 真实 mTLS 与权限矩阵 | `integration/test_security.py` 17 项 | **17 passed**：viewer/ops/deployer 矩阵、无 CN/空白 CN 四组合 UNAUTHENTICATED、陌生 CN PERMISSION_DENIED、 Rogue CA/无客户端证书失败关闭、明文降级拒绝、备份管理员限定 |
| WAL 在线备份与备份语义 | `component/test_sqlite_backup.py` 9 项 | **9 passed**：23 表初始 Schema、复合 FK 与不可变历史、触发器漂移拒绝、中断备份清理、symlink 失败关闭、队列限额与幂等、WAL 并发写在线一致性 |
| 故障恢复 | `reliability/test_backup_process_restart.py` 3 项 | **3 passed**：SIGKILL 后未完成备份标记 INTERRUPTED 且锁释放、候选与幂等发布跨真实进程重启存活、损坏库 CLI 启动失败关闭 |

CLI/安全/备份/可靠性组合一次性实跑：**33 passed in 38.38s**。

## 7. 新增缺陷台账

### CS-P2-006：schema_catalog.py Ruff 格式化漂移 — P2 / 本轮发现并 CLOSED

- 来源：ChatGPT 修复提交 `8051f7574` 中 `_field(..., required=True)` 折行不符合项目 Ruff 行宽规则，致 `ruff format --check src` 退出码 1（1 文件待格式化，422 文件已符合）。
- 处置：验收方执行单文件 `ruff format`，仅 3 行折行合并为 1 行；`ast.parse` 后 `ast.dump` 前后全等，零语义变化。修复后两门 Ruff 均退出码 0。
- 无 P0、无 P1；无其他新增缺陷。

最终缺陷计数：**P0=0、P1=0、P2=0**。

## 8. NOT_RUN 与限制（沿用第三轮，均为独立资格项，不阻断本次准出）

- 未做传输层 `maximum_concurrent_rpcs` 的确定性饱和；已有真实并发请求与备份配额测试覆盖。
- 首版无后续迁移脚本，未执行数据库升级中途崩溃演练。
- 未执行长稳或性能 SLA 资格测试；本轮仅验证真实指标语义（RPC 计数/延迟/失败/审计）。
- Commander apply 后未额外重启 Commander；Configuration 自身 DISK/RUNTIME 真实重启用例已通过。
- 真实硬件、netem 多机网络损伤不在本任务环境内，NOT_RUN。

## 9. 最终裁决

**PASS。**

准出标准逐项满足：

1. P0=0、P1=0（第三轮 3 个 P1 全部关闭），本轮新增 P2 格式化漂移已当场修复并验证 AST 全等，最终 P2=0；
2. 六项核心静态门禁（含测试两门共八条命令）退出码全部 0；
3. 194/194 必需测试全部通过，零 skip/xfail/error，且测试集合相对第三轮零改动；
4. 44/44 YAML、43/43 SQLite 往返、12 Repository RPC、9 Common RPC、全 CLI、真实 Commander apply、mTLS 权限矩阵、WAL 备份与 SIGKILL/损坏库故障恢复均有真实链路实证；
5. v1_10 设计已冻结数组 `items` 递归校验、Protobuf 传输预留与 Point Mapping 设备类型解析契约，未发现阻断性设计冲突。

限制仅为第 8 节独立资格项（饱和、迁移崩溃、长稳/SLA、硬件/netem），按项目生命周期保持可选、不计入本轮准出阻断。

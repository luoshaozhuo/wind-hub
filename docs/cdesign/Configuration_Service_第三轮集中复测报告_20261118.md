# wind-hub Configuration Service 第三轮集中复测报告

日期：2026-11-18  
裁决：**FAIL**  
最终验证代码/测试 HEAD：`3e6c24dd4408ea34c1a0b8e29de124b5125b69c4`  
最终交付 HEAD：本报告提交；完整 SHA 随交付消息提供。

## 1. 基线、分支与执行边界

- 仓库：`luoshaozhuo/wind-hub`；分支：`feat/configuration-service-v1`。
- 开始时执行 `git fetch`。本地 HEAD、upstream 与交接 HEAD 均为 `f46b05b315b435d824c14fce8253d9dbe6945ca4`，工作树干净。
- 未新建分支、未创建 PR、未合并 main、未 rebase、未 force push。
- 环境检查 `python3 scripts/dev.py env`：PASS；解释器 `/home/luo/miniconda3/envs/wind-hub/bin/python3`，Python 3.14.7。
- 生产修改仅包括独立提交的 Ruff 纯格式化，以及有真实 Proto 类型依据的统计响应构造修复。未修改业务规则、Schema 语义、权限判断或数据库操作来规避失败。
- 测试均使用真实临时 SQLite、真实 PKI/mTLS、正式 CLI 进程或真实应用适配器；无 skip、xfail、ignore 扩大或断言弱化。

提交记录：

| 提交 | 内容 |
|---|---|
| `4891a754` | 14 个生产文件 Ruff 格式化及 2 个 import 排序；格式化前后 AST 等价（仅预期的 import 顺序不同） |
| `95b88d4e` | `GetRepositoryStatsResponse` 按 `.pyi` 定义显式传入 5 个统计字段，消除动态 `**dict[str, int]` 的 Mypy 错误 |
| `3e6c24dd` | 定向测试、回归测试及 PKI Fixture；与格式修改分开提交 |

## 2. 静态门禁

### 2.1 初始交接 HEAD 结果

- `ruff check src`：退出码 1；2 个 I001。上轮 UP035 已消失。
- `ruff format --check src`：退出码 1；14 文件待格式化，409 文件已符合。
- `mypy src`：退出码 1；437 文件中仅余 1 项，`repository_server.py` 统计响应动态参数类型不兼容。上轮其余 5 项已消失。
- `lint-imports`、Proto 检查、`git diff --check`：均退出码 0。

### 2.2 最终代码/测试 HEAD 结果

| 实际命令 | 退出码 | 结果 |
|---|---:|---|
| `ruff check src` | 0 | PASS，All checks passed |
| `ruff format --check src` | 0 | PASS，423 files already formatted |
| `mypy src` | 0 | PASS，437 source files，0 errors |
| `lint-imports` | 0 | PASS，516 files / 2814 dependencies，19 kept / 0 broken |
| `python3 scripts/generate_rpc.py --check` | 0 | PASS |
| `git diff --check` | 0 | PASS |
| `ruff check tests/configuration_service tests/support/configuration_live.py` | 0 | PASS |
| `ruff format --check tests/configuration_service tests/support/configuration_live.py` | 0 | PASS |

CS-P1-008 **CLOSED**。应用 `RepositoryPort`、SQLite 适配器、YAML Codec 与 gRPC 调用的检查未发现其他签名不一致；Proto 生成物无漂移。

## 3. 全量回归

实际命令：

```bash
PYTHONPATH=src python3 -m pytest tests/configuration_service -q
```

为保存完整日志，最终取证使用等价命令加 `-p no:warnings --tb=short`，未改变测试选择或断言。

- 退出码：**1**。
- **194 项：188 passed / 6 failed / 0 skipped / 0 xfailed / 0 errors**。
- 上轮 179 项全部保留并重新执行：176 passed / 3 failed。
- 新增 15 项：12 passed / 3 failed。
- 失败归属：CS-P1-001 一项、CS-P1-007 两项、新投影回归两项、新 Header P2 一项。

以下原有范围均实际包含在全量回归中：

- SQLite CAS、事务回滚、幂等、历史分页水位通过。
- 12 个 Repository RPC、9 个 Common RPC 主体矩阵通过；下述配额边界除外。
- CLI 全指令、真实 Commander `apply`、未列出文件保持不变通过。
- WAL 并发备份、SIGKILL 恢复、备份恢复通过。
- 实际速率、平均延迟、P95、失败计数和权限审计通过。
- 固定版本依赖、共享型号和型号升级历史相关用例通过，但两个 Point Mapping 投影用例因新增 `UnboundLocalError` 失败，完整 Device/Point 链不能判 PASS。

## 4. 五个遗留缺陷逐项裁决

| 缺陷 | 结果 | 证据与剩余问题 |
|---|---|---|
| CS-P1-001 | **OPEN** | 44/44 YAML、43/43 SQLite、7 个原失败 Service YAML、`security.callers` 数组、嵌套 required/properties/enum/未知字段、Schema 幂等及领域冲突均通过；但缺少 `items` 的数组仍被接受，见 CS-P1-001A |
| CS-P1-004 | **CLOSED** | 真实受信 CA 的无 CN、空白 CN 在 Repository/Common 均为 UNAUTHENTICATED；合法陌生 CN 均为 PERMISSION_DENIED；query/publish/admin 允许与拒绝矩阵通过 |
| CS-P1-007 | **OPEN** | 闭包超限和 Bundle 总字节超限返回 RESOURCE_EXHAUSTED；应用层单文件/总字节/闭包恰好等于上限成功；但单文件超限仍 INVALID_ARGUMENT，真实 gRPC 恰好等于 YAML 配额又被传输层拒绝 |
| CS-P1-008 | **CLOSED** | 六项静态门禁在最终代码/测试 HEAD 全部退出码 0 |
| CS-P2-003 | **CLOSED** | 正常 HEALTHY；文件移除、外部新建空库及文件替换均 UNHEALTHY / Diagnose FAIL；恢复后 HEALTHY；WAL、FK、停机、重新初始化、锁与连接释放检查通过；既有备份/SIGKILL 用例通过 |

因此上轮 4 个 P1 中有 2 个关闭、2 个保持 OPEN；1 个 P2 已关闭。本轮另新增 1 个 P1 投影回归和 1 个 P2 Header 问题。

## 5. YAML、Schema 与 SQLite 往返

精确执行：

```bash
PYTHONPATH=src python3 -m pytest \
  tests/configuration_service/unit/test_yaml_acceptance.py::test_shipped_yaml_strict_parse_and_lossless_header_roundtrip \
  tests/configuration_service/component/test_shipped_yaml_repository.py::test_shipped_yaml_sqlite_yaml_roundtrip -q
```

结果：**87 passed**，退出码 0。

- **44/44** 真实 YAML 严格解析、完整 Header 导出、重解析、字段值/类型/null/缺省语义及规范化哈希一致。
- **43/43** 可入库真实 YAML 经真实 SQLite 发布后导出、重解析和规范化内容一致。
- 上轮失败的 Collector、Commander、Data Generator 两份、Industrial Gateway 两份、Sink Service 共 7 份全部通过。
- 自身 bootstrap 按设计不入库，其拒绝用例另行通过。

上述 43/43 通过不抵消下述“未定义 items 的数组可绕过元素验证”和 Header 必填标注问题。

## 6. 生产缺陷台账与最小复现

### CS-P1-001A：数组缺少 `items` 时不失败关闭 — P1 / OPEN

- 位置：[service.py:143-150](../../src/configuration_service/application/service.py#L143-L150)；内置缺项还见于 [schema_catalog.py:37-136](../../src/configuration_service/application/schema_catalog.py#L37-L136)。
- 最小复现：`PYTHONPATH=src python3 -m pytest tests/configuration_service/component/test_schema_regressions.py::test_array_schema_without_items_fails_closed -q`。
- 输入：注册 `servers: {"type": "array"}`，提交 `servers: [123]`。
- 期望：依 v1.9 第 10 节，数组必须定义并递归验证 `items`，候选拒绝。
- 实际：`items is None` 时跳过全部元素校验，候选被接受。内置 `service.servers`、`service.scenarios`、`scenario.scenarios` 也存在未完整定义数组元素的规则。

### CS-P1-007A：单文件导出超限映射为 INVALID_ARGUMENT — P1 / OPEN

- 位置：[service.py:519-520](../../src/configuration_service/application/service.py#L519-L520)、[service.py:550](../../src/configuration_service/application/service.py#L550)、[repository_server.py:87-88](../../src/configuration_service/infrastructure/adapters/inbound/grpc/repository_server.py#L87-L88)。
- 最小复现：`PYTHONPATH=src python3 -m pytest tests/configuration_service/integration/test_bundle_limits.py::test_single_document_export_limit_is_resource_exhausted -q`。
- 期望：真实 `GetConfig` 返回 RESOURCE_EXHAUSTED。
- 实际：`_restore()` 先用 `max_bundle_bytes` 解析导出 YAML，`ValueError("YAML size is empty or exceeds limit")` 先于容量异常发生，被映射成 INVALID_ARGUMENT。相同前置路径也影响 Bundle 中任一单文件超限。

### CS-P1-007B：YAML 配额与 gRPC 整包发送上限相同，精确边界失败 — P1 / OPEN

- 位置：[main.py:136](../../src/configuration_service/main.py#L136)、[service.py:570](../../src/configuration_service/application/service.py#L570)。
- 最小复现：`PYTHONPATH=src python3 -m pytest tests/configuration_service/integration/test_bundle_limits.py::test_grpc_transport_allows_bundle_at_exact_yaml_quota -q`。
- 期望：两个 YAML 总字节和两个版本闭包都恰好等于配置上限时完整导出成功。
- 实际：应用层同边界测试通过，但真实 gRPC 返回 `RESOURCE_EXHAUSTED: SERVER: Sent message larger than max (3197 vs. 2626)`。应用只计算 YAML bytes，gRPC 上限还包含 Protobuf 响应字段与 manifest，二者不能直接共用同一数值。

### CS-P1-010：Point Mapping 引用未赋值的 `device_type` — P1 / 新增回归

- 位置：[projection.py:167-196](../../src/configuration_service/infrastructure/adapters/outbound/yaml/projection.py#L167-L196)。
- 最小复现：
  - `PYTHONPATH=src python3 -m pytest tests/configuration_service/component/test_typed_references.py::test_unit_semantic_business_point_projection_chain -q`
  - `PYTHONPATH=src python3 -m pytest tests/configuration_service/component/test_typed_references.py::test_full_device_point_dependency_chain_creates_composite_foreign_key_rows -q`
- 期望：Point Mapping 使用 PointTable 已解析的 `point_table_device_type`，完整复合外键链发布成功。
- 实际：PointTable 行改用 `point_table_device_type`，Point Mapping 仍引用仅在其他分支赋值的 `device_type`，发布抛 `UnboundLocalError`，两个原通过用例失败。上轮基线此处使用 `raw["device_type"]`，证明为本轮回退。

### CS-P2-005：Header 将 callers 必填字段写成“可选” — P2 / 新增

- 位置：[schema_catalog.py:207-220](../../src/configuration_service/application/schema_catalog.py#L207-L220)、[codec.py:127-140](../../src/configuration_service/infrastructure/adapters/outbound/yaml/codec.py#L127-L140)。
- 最小复现：`PYTHONPATH=src python3 -m pytest tests/configuration_service/component/test_schema_regressions.py::test_caller_header_declares_complete_required_item_fields -q`。
- 期望：`security.callers.<item>.caller_id`、`.certificate_identity`、`.permissions` 三项在 Header 均标为“必填”。
- 实际：机器 Schema 的 `required` 包含三项，但 Header `_field()` 未传 `required=True`，三项均输出“可选”。字段路径完整性已具备，仅必填说明不一致。

汇总：**P0=0、P1=3、P2=1**；没有以测试修改掩盖任何生产失败。

## 7. 测试与 Fixture 修改记录

- `test_schema_regressions.py`：Commander 依赖按身份去重；补合法数组、required、未知字段、enum、元素类型、缺 items、Header 必填、重复一致注册幂等。
- `test_repository_transactions.py`：闭包超限断言由旧 `RepositoryConflictError` 更新为 v1.9 对应的 `RepositoryCapacityExceededError`，异常匹配文本不变。
- 新增 `test_bundle_exact_limits.py`：应用层单文件、Bundle 总字节和依赖闭包精确上限及超一字节。
- `test_bundle_limits.py`：真实 RPC 拆分单文件、闭包、Bundle 总字节、gRPC 精确边界；通过停止/启动服务应用独立配额。
- `test_security.py`：补空白 CN、Repository/Common 无身份与陌生身份矩阵。
- `test_observability.py`：补数据库移除、替换、新建空库、恢复和 WAL/FK 检查。
- `configuration_live.py`：PKI Fixture 支持无 CN 和空白 CN 客户端证书。
- 未修改 `configs/` 中任何真实 YAML，未删除或跳过既有测试。

## 8. NOT_RUN 与限制

本任务明确要求的静态门禁、194 项全量回归、真实 mTLS、真实 SQLite 故障、44/44 YAML 和 43/43 SQLite 均已执行；无必需项 NOT_RUN。

以下独立资格项目未执行，不计 PASS：

- 未做传输层 `maximum_concurrent_rpcs` 的确定性饱和；已有真实并发请求与备份配额测试。
- 首版没有后续迁移脚本，未执行数据库升级中途崩溃。
- 未执行长稳或性能 SLA 资格测试；本轮只验证真实指标语义。
- Commander apply 后未额外重启 Commander；Configuration 自身 DISK/RUNTIME 真实重启用例已通过。
- 最终本地提交尚未推送；远端一致性需用户手动 push 后复核。此项不影响测试证据与 HEAD 绑定，但意味着当前本地 HEAD 与 origin 不一致。

## 9. 最终裁决

**FAIL**。

静态门禁已全部通过，44/44 YAML、43/43 SQLite、证书身份和数据库丢失整改已验证；但准出要求 P1=0 且全部必需测试通过，当前仍有 3 个 P1、194 项中 6 项失败，并存在 Device/Point 投影回归。不得判为可投入或准出。上述生产问题均保留最小复现、源码位置、期望和实际行为，交回 ChatGPT 整改。

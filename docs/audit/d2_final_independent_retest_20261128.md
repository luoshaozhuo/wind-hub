# wind-hub D2 最终整改与独立验收报告（2026-11-28）

> 面向 ChatGPT 的 D2 关闭与 D3 准入评审。本文只裁决废弃实现、引用闭包和本轮授权修复，不扩展为容量或完整生产资格结论。

## 1. Git 基线、环境与最终 SHA

| 项目 | 值 |
|---|---|
| 起始/预期远端 HEAD | `8ab623cb05bd3cd636cadad219ef536e41076f92` |
| D1 最终验收 HEAD | `b3273d00d615acf722cb5efef32fc669b28c302a` |
| 最终生产源码 SHA | `13955c2e06cd387589535aa52142a2604818905e` |
| 最终测试 SHA | `13955c2e06cd387589535aa52142a2604818905e` |
| 最终远端 HEAD | 报告提交及 push 后以 Git 元数据和交付摘要为准 |
| 分支 | `feat/storage-wide-table-completion-20261125` |

Fetch 后起始本地/远端一致，工作树干净。环境为 Python 3.11.16、`wind-hub` Conda 环境，`python3 scripts/dev.py env` PASS。未创建新分支、PR，未合并 main，未 force push；未修改 Proto、配置版本、容量门禁或 Web 源码。

原始证据：[d2_final_independent_retest_20261128_evidence/](d2_final_independent_retest_20261128_evidence/)。证据只含命令、计数、路径和测试错误摘要，不包含令牌、密码、私钥或机器码。

## 2. 最终门禁

| Gate | Result | 依据 |
|---|---|---|
| GIT_BASELINE | **PASS** | 起始 SHA 精确匹配，最终源码/测试 SHA 固定。 |
| DEAD_CODE_CLEANUP | **PASS** | 五个既有 D2 提交及本轮高置信非 Web 零调用项均完成；引用复核无残留。 |
| LEGACY_COMPATIBILITY_CLEANUP | **PASS** | 未恢复 snapshot_io/旧配置；必要历史读取、数据库迁移和冻结 Proto 均保留。 |
| REFERENCE_CLOSURE | **PASS** | 已删除活动配置无失效引用；87 份活动 YAML 闭包与 configs 字节一致。 |
| CLI_SNAPSHOT_LIMIT | **PASS** | 用户配置贯穿查询与导出，边界及安全回归 14/14。 |
| CONFIGURATION_CLOSURE | **PASS** | 三套精确选版、argv、发布和 Bundle 独立限额通过。 |
| STATIC_GATES | **PASS** | Ruff、mypy、import-linter、Proto、4350 项收集全绿。 |
| MODULE_REGRESSION | **PASS** | CLI/Configuration/Sink/其他非 Web 模块全部通过，环境 skip 单列。 |
| FULL_CHAIN_REGRESSION | **PASS** | D1 选定 32 项联合通过，无 skip/xfail。 |
| D2 | **D2_CLOSED** | 所有本轮 DEFECT 已关闭，无新增未解决 FAIL。 |
| D3_ADMISSION | **READY_FOR_D3_REVIEW** | 仅建议进入评审，未自行开展 D3。 |
| PRODUCTION_READINESS | **BLOCKED** | 容量、硬件、长稳和完整生产资格未因 D2 通过而解除。 |

**总体裁决：`D2_CLOSED + READY_FOR_D3_REVIEW`。**

## 3. 完整变更清单

### 3.1 基线已有的五个 D2 提交

1. `1a7b4caf3`：删除活动根未选中的 `service_cli-e2e_v1.1.7.yaml`。
2. `0f522c754`：删除活动根未选中的 `service_gateway-e2e-source_v1.1.2.yaml`。
3. `6109b8160`：删除 snapshot_io 专属重复测试，保留正式 snapshot_commands 覆盖。
4. `0f8ad94bb`：删除无引用 `src/cli/snapshot_io.py`。
5. `8ab623cb0`：Sink 错误提示由 NATS v2 修正为 SampleBatch v4。

### 3.2 本轮独立验收提交 `13955c2e0`

- `src/cli/snapshot_commands.py`：必传 `max_bytes`，删除固定 `_MAX_BYTES`；查询/导出共用累计内容上限。
- `tests/cli/test_snapshot_commands.py`、`test_snapshot_e2e.py`：新增边界、累计、清理及双模式真实 CLI 测试。
- 删除 Core `point_value.py`、Gateway `domain/point.py`；Gateway `domain/__init__.py` 仅保留真实使用的 `Scalar`。
- 删除 Configuration SQLite 三个零调用实现、Sink 两个零引用枚举、Storage 三个旧包装/清理/枚举实现。
- Sink v4 提示仅做 Ruff 格式化。
- 更新 CLI 设计目录和 `deploy/config_roots/README.txt`，消除 snapshot_io、旧 Deployment 版本及 37 份 YAML 的过期描述。

未恢复任何已删除模块，未添加兼容默认参数。

## 4. 废弃代码与兼容性逐项裁决

| 文件/符号 | 分类 | 引用关系与裁决依据 | 处理 |
|---|---|---|---|
| 活动根 CLI 1.1.7、Gateway Source 1.1.2 | DEAD | 当前 E2E 选择 CLI 1.1.9、Gateway 1.1.3；活动闭包无引用 | 已由基线提交删除，测试复核通过 |
| configs 中同名历史文件 | REQUIRED | 不可变历史归档及安全/Schema 回归仍精确读取 | 保留，字节未改 |
| `src/cli/snapshot_io.py` 及专属重复测试 | DEAD | 无 import；正式路径为 configuration_commands → snapshot_commands | 已删除，无残留引用 |
| Core `domain/point_value.py::PointValue` | DEAD | 无生产、测试或 __init__ 导出；Web 同名类型属于独立模块 | 删除 |
| Gateway `domain/point.py` 的 PointKey/Role/Definition/Write/Snapshot | DEAD | 仅自引用和 __init__ 再导出，无消费者 | 删除 |
| Gateway `domain.Scalar` | REQUIRED | Modbus codec 真实类型消费者；domain 是 import-linter 正式层 | 保留唯一别名，未删除架构层 |
| Configuration `_document_payload` | DEAD | 仅定义，无调用 | 删除 |
| Configuration `get_reference_closure` | DEAD | 非 RepositoryPort；现行调用直接使用 get_bundle_versions | 删除 |
| Configuration `expire_candidates` | DEAD | 非端口且无调用；发布读取路径按 expires_at fail-closed | 删除，不删除过期安全校验 |
| Sink `ProcessRole`、`domain.config.DesiredState` | DEAD | 零引用；Gateway 独立 DesiredState 为另一有效实现 | 仅删除 Sink 两项 |
| Storage `peek_wide_identity` | DEAD | ingestion 直接使用 parse_wide_envelope + WideRecord | 删除包装 |
| Storage `clear_blocked_partition` | DEAD | 已被带 request_id 的 request_partition_retry 取代 | 删除旧方法，保留幂等重试 |
| Storage `list_tables` | DEAD | 零调用且使用旧 tb_[0-9a-f]{24}；备份使用 list_ready_wide_tables | 删除旧枚举，保留宽表所有权校验 |
| Web `application/config/port.py::ConfigPort` | UNCERTAIN / OUT_OF_SCOPE | 静态未见调用，但本轮禁止 Web 整改 | 未删除，交后续 Web 范围裁决 |
| SQLite `legacy=True` 迁移 | REQUIRED | 真实 v1 数据库校验和迁移路径 | 保留 |
| Proto legacy 字段号/包名与测试 | REQUIRED | 冻结线契约，不是废弃实现 | 保留 |
| 防旧历史路径复活测试 | REQUIRED | 安全/架构拒绝测试 | 保留 |
| 带 v1/v2 名称的 API/契约测试 | REQUIRED | 当前版本化 API 或描述符职责，不是重复代码 | 保留 |
| CLI 设计仍列 snapshot_io | DEFECT | 与已删除模块冲突 | 改为 snapshot_commands，CLOSED |
| config_roots README 仍写 v2.0.0/37 YAML | DEFECT | 实际 2.0.2、2.0.1、2.0.2 和 29/23/35 | 已更新，CLOSED |

未发现带版本号的重复手写 Python 实现。搜索命中均为正式 API 命名空间、契约测试或第三方资产，不按文件名删除。

## 5. `snapshot_max_bytes` 缺陷修复

### 5.1 实现

- `Runtime.config()` 仍在 `src/cli/runtime.py:167-169` 将 `snapshot_max_bytes` 限制为 **1～67108864**，未改默认值或配置版本。
- `Runtime.channel()` 继续以该配置设置 gRPC 收发容量。
- `_verify_snapshot(files, *, max_bytes)` 和 `_write_snapshot(..., *, max_bytes)` 均要求显式上限，无兼容默认参数。
- `get_snapshot()` 从当前 Runtime 读取配置；有 output 与无 output 均执行完全相同的逐文件累计内容字节校验。
- 删除固定 `_MAX_BYTES`；保留 `_MAX_FILES=1000`、路径/重复路径、大小、SHA-256、目标不覆盖、0600 文件、0700 目录和临时目录原子改名。

### 5.2 边界证据

`pytest -q tests/cli/test_snapshot_commands.py tests/cli/test_snapshot_e2e.py`：**14 passed**，见 `snapshot_limit.log`。

| 要求 | 测试证据 |
|---|---|
| 小于上限成功 | 参数化 3 字节 / 上限 4 |
| 等于上限成功 | 单文件 4/4；CLI 两文件累计 4/4 |
| 超过一个字节拒绝 | 单文件 5/4 |
| 单文件未超限、多文件累计超限 | 2+2 字节 / 上限 3；CLI 2+2+1 / 上限 4 |
| 不创建目标、不留临时文件 | 写入前校验失败，destination 与 `.snapshot-*` 均不存在 |
| 有 output / 无 output | 真实子进程参数化两种模式，等值成功、超限 exit 2 + ValueError |
| 完整性与安全回归 | SHA、size、相对路径、重复路径、已存在目标、0600/0700、原子导出 |

## 6. 其他正式快照与 Bundle 路径

未发现另一条遗漏 `snapshot_max_bytes` 的正式 CLI 快照通路。该字段是 CLI 专属配置，CommonService 请求不携带它，服务端不应猜测某客户端配置。

Configuration Bundle 使用独立的 `max_bundle_bytes` 服务端契约：普通、exact-roots 和 Service Bundle 都有累计检查，超限映射 RESOURCE_EXHAUSTED，gRPC 另留元数据余量。CLI Bundle 导出继续校验 SHA、manifest 和总量，不错误套用 snapshot_max_bytes。

实际执行 `pytest -q tests/configuration_service/integration/test_bundle_limits.py`：**4 passed**，见 `bundle_limits_integration.log`。本轮未发现需要扩大授权的导出 DEFECT。

## 7. 静态门禁与收集

最终 SHA 执行：

```bash
ruff check src tests scripts
ruff format --check src tests scripts
mypy src
lint-imports
python3 scripts/generate_rpc.py --check
pytest --collect-only -q
```

结果：Ruff check PASS；1138 文件 format PASS；mypy **492 files / 0 errors**；import-linter **22 kept / 0 broken**；Proto --check PASS；**4350 tests collected，0 collection error**。原始日志为 `static.log`、`collect_only.log`。

首轮 Sink 两行 E501 和 Gateway Scalar/domain 清理产生的 mypy/import-linter 中间态均已修复后重跑，不以跳过或架构修改消除。

## 8. 配置闭包

当前活动版本：prod-a `2.0.2`、sim-a `2.0.1`、e2e-final `2.0.2`。

| 根 | YAML 数 | Deployment/CLI argv | configs 字节一致 | 发布闭包 |
|---|---:|---|---|---|
| prod-a | 29 | PASS | PASS | PASS |
| sim-a | 23 | PASS | PASS | PASS |
| e2e-final | 35 | PASS | PASS | PASS |

执行重点命令：

```bash
pytest -q tests/configuration_service/component/test_shipped_yaml_repository.py \
  tests/system/e2e/test_long_run_candidate_preflight.py tests/src/core/configuration
```

结果 **118 passed**，见 `configuration_closure.log`。独立脚本同时验证 87 份 YAML 字节、精确 argv 和已删除活动配置无引用，见 `active_config_identity.log`。未使用“自动选择最新版”，未修改历史不可变配置内容。

## 9. 模块与真实链路回归

| 范围 | 实际命令/日志 | 结果 |
|---|---|---|
| CLI | `pytest -q tests/cli -rs`；`cli_full.log` | **294 passed、13 skipped、0 failed/xfail** |
| Configuration unit/component（排除另行发布的 shipped 文件） | `configuration_unit_component.log` | **367 passed** |
| 配置发布、候选与 Core 闭包 | `configuration_closure.log` | **118 passed** |
| Bundle 真实 Integration | `bundle_limits_integration.log` | **4 passed** |
| Sink unit/component | `sink_unit_component.log` | **430 passed**，仅既有 marker warnings |
| 其他非 Web 业务模块联合 | `non_web_module_regression.log` | **1036 passed** |
| Full-chain D1 选定集合 | `full_chain_selected.log` | **32 passed、0 skip/xfail** |

非 Web 1036 项组成：Contracts 114、Core 26、Collector 116、Commander 224、Industrial Gateway 155、Data Generator 104、Storage 297。

CLI 全量包含真实子进程快照、严格 CLI 五服务数据/控制/重启链；Full-chain 复用控制安全 7、分段 6、安全恢复 9、语义矩阵 8、RPC/CLI 表面矩阵 2，验证 D2 未破坏数据与控制链。

## 10. Skip、xfail、NOT_RUN 与观察项

- CLI 13 个 skip：11 项 `test_runtime_e2e.py` 和 1 项 `test_system_real.py` 因 containerd socket/CLI 不可用；另 1 项因 iSulad 不可用。均为环境 NOT_RUN，不计 PASS，未新增 skip。
- 所有最终执行组 **0 xfail、0 failed**。
- Sink/收集日志存在既有未注册 `nats`、`jetstream` marker warnings；不影响测试结果，未为消除警告修改冻结测试语义。
- 初步 Commander component 独立运行曾打印 3 条 pending-task teardown 警告；最终联合 1036 项和 Full-chain 均通过，未形成失败。分类 **UNCERTAIN / 非 D2 阻断**，不在本轮扩大生产诊断。
- 未执行：其他 Full-chain 业务文件、真实 PLC/硬件、netem、容量、600 秒/长稳、containerd/iSulad 资格、Web Server/Web Admin 专项。均 **NOT_RUN**，不得推定 PASS。
- 未关闭问题：无本轮 D2 DEFECT；Web ConfigPort 仅为超出范围的静态候选，不在本轮删除。

## 11. 最终结论

D2 废弃实现、过期活动配置、孤立模块和冗余兼容代码已按证据清理；snapshot_max_bytes 查询/导出契约已统一，配置闭包与全部相关回归通过。

**最终总体裁决：`D2_CLOSED + READY_FOR_D3_REVIEW`。** 该结论不代表 Storage 容量通过、硬件/长稳资格通过或完整生产 READY。

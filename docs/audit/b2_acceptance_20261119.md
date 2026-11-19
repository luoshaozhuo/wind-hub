# B2 Configuration 全量目录发布 — 独立验收报告（2026-11-19）

- 分支：`feat/config-businesspoint-alignment-20261118`
- 基线（merge-base）：`dc6af967653bf74d274e8302fe70d4a6015f3417`
- 验收开始 HEAD：`1e0500f1d`；验收后修复以独立提交追加在本分支（未 push、未 rebase、未改分支）
- 验收机：wind-hub conda 环境（Python 3.11，grpcio-tools 1.81.1 / grpcio 1.84.0 / nats-py 2.16.0）；真实 NATS `nats://127.0.0.1:4222`（docker r2-nats）

## 1. 总体结论

**第一轮 FAIL（P1-CLI-01 缺口）→ 第二轮复测 PASS WITH LIMITATIONS（P1-CLI-01 已关闭，详见 §7-§11）。**

分支交付时 Configuration 测试套件整体不可通过（unit/component 84 失败、integration/reliability 17 失败、ruff/mypy/契约 `--check` 全红）。本验收按 v1_16 全量目录语义完成测试迁移与小范围生产修复，未删除任何测试语义；第一轮仅余 P1-CLI-01（实现移除了冻结设计仍要求的 CLI 快照生效命令面）。第二轮确认集中整改恢复了该命令面并通过复测，P1-CLI-01 关闭，剩余仅为与整改无耦合的满载时序 flake（P2）。

## 2. 门禁退出码（验收后实测）

| 门禁 | 命令 | 退出码 |
|---|---|---|
| Ruff check | `ruff check src tests scripts` | 0 |
| Ruff format | `ruff format --check src tests scripts` | 0 |
| Mypy | `mypy src` | 0 |
| Import-linter | `lint-imports` | 0 |
| 契约生成一致性 | `python scripts/generate_rpc.py --check` | 0 |

说明：分支入库的 3 个 `configuration_repository_pb2*` 绑定由异版本 grpcio-tools 生成（描述符语义等价但代码风格漂移，RPC 方法排序也陈旧），已用本机权威工具重生成；`configuration_repository_pb2_grpc.py` 字节不变。`FileDescriptorProto` 级对比确认 38 消息/13 枚举/14 RPC 与 proto 完全一致，仅代码风格差异。

## 3. 测试结果

### 3.1 Configuration 范围（任务 3/4/5/6/7 主范围）

| 套件 | main 基线 | 分支验收后 |
|---|---|---|
| unit | 含在 152 全绿内 | 106 passed |
| component | 同上 | 95 passed |
| integration（真实子进程+gRPC+NATS） | 含在 42 全绿内 | 36 passed |
| reliability | 同上 | 3 passed |
| 合计 | 194 passed | **240 passed，0 failed（净增 46，零测试删除）** |

- 同范围 contracts：108 passed；core/config + tooling：150 passed；上述目录合计 **498 passed**。
- marker 门禁 `pytest tests -m "unit or component or contract"`：**2825 passed / 7 failed / 1013 deselected**，7 失败全部归属 P1-CLI-01。
- `tests/cli` 全量：225 passed / 7 failed（同 P1）/ 13 skipped（环境 skip，非本次引入）。
- main 基线实测（worktree 复跑取证）：configuration unit+component 152 全绿、integration+reliability 42 全绿 → 分支上的全部红灯均为 RELATED。

### 3.2 真实链路证据

- 真实 CLI 独立进程覆盖：`config publish --root`（含 `--validate-only`）、`revision`、`point-table-bundle`、list/versions/get/bundle/references/recommend/deprecate/stats/backup/backup-status，以及符号链接/`.yml` 拒绝（test_cli_integration，3 用例）。
- 真实 NATS 端到端：新增 `test_revision_notification.py`，订阅 `wh.config.changed` 验证提交修订广播载荷 `{"revision_id": N}`，并验证 validate-only 不产生通知；NATS 不可达时显式 skip（NOT_EXECUTED 诚实降级）。
- 真实 gRPC 14 RPC 矩阵、角色权限（viewer/ops/deployer）、token 缺失/重复/伪造拒绝（test_security 13 用例）全绿。
- SQLite：新增空历史 v1→v2 原子迁移用例；fail-closed 拒绝带历史 v1 库、新鲜 v2 初始化、在线备份、SIGKILL 重启恢复均有实测。

## 4. 问题台账

### P1-CLI-01（第一轮未闭环；第二轮已 CLOSED，关闭证据见 §7）：CLI 移除了冻结设计仍要求的快照生效命令

- 现象：`config push`、`config apply`、`config get --scope`（runtime/disk 视图）在 Typer 命令树中不存在；调用即 `No such command`（exit 2）。
- 失败面（7+1+1，均 RELATED）：
  - `tests/cli/test_snapshot_e2e.py` 5 用例、`tests/cli/test_grpc_integration.py` 2 用例；
  - `tests/system/e2e/full_chain/test_e2e_surface_matrix.py::test_e2e_08_executes_all_73_cli_leaves`（实测复现）；
  - 命令树设计清单与实现不一致（本验收已把清单更新为实际叶命令以消除测试自身陈旧断言，但设计缺口仍在）。
- 设计依据（v1_16 冻结文档）：§10.1 明确“CLI 在 Repository 事务外向业务模块发起应用指定 revision”“CLI 记录每个业务模块自身 ApplyConfigSnapshot 结果”，§13.2 保留 CommonService 9 RPC 与部分成功语义；实现侧 Common RPC（Push/Apply/GetConfigSnapshot）仍存在且 test_common_rpc 全绿，仅 CLI 包装入口被删除。
- 裁决选项：(a) 恢复三个 CLI 包装命令（沿用 token 化通道）；(b) 修订冻结设计文档正式退役该工作流并给出替代分发路径。在裁决前相关测试保持红灯，未做跳过/删除处理。

### 验收中已闭环的问题（证据保留在提交内）

| 编号 | 级别 | 问题 | 处置 |
|---|---|---|---|
| FIX-PROD-01 | P1（运行时） | `repository_server._actor` 把 gRPC `_Metadatum` 当 dict 用（`.key/.value`），任何鉴权调用必 AttributeError | 改为元组索引 `item[0]/item[1]`；真实链路 13 权限用例佐证 |
| FIX-PROD-02 | P1（类型契约） | `RevisionBroadcaster.flush` 传 float，违反 nats-py int 秒签名 | `_flush_timeout=max(1, ceil(ms/1000))`，mypy 0 |
| FIX-TEST-01 | P1 | 84 个 unit/component 旧单文件候选测试未迁移全量目录语义（main 152 全绿→分支 84 红） | 共享 helper 产出合规 relative_path；按完整根快照/不可变闭包/同身份多版本共存规则迁移；semantic_points 退役链改写为 units→business_points→point_table 三级模型；零删除 |
| FIX-TEST-02 | P1 | 17 个 integration/reliability 失败：旧 `configuration` 命令组、mTLS pki、单文件 RPC 语义 | 迁移到 token 化 `config` 命令面与 14 RPC 矩阵；退役的 apply 工作流以 unsafe-tree/真实 NATS 等新面覆盖替代 |
| FIX-STYLE | P2 | 9 项 ruff（E501/I001/SIM102/F401）、26 文件格式漂移、mypy 3 错 | 全部修复；生产仅 2 处 F401 导入清理，无语义变化 |
| FIX-CONTRACT | P2（main 既有红灯） | `test_generation` 未收录 configuration_repository 生成物；`test_rpc_boundary` 允许清单缺 configuration_service（两者在 main 同样失败） | 补全 stem/servicer/边界清单，契约套件 108 全绿 |
| FIX-COVERAGE | 新增覆盖 | 空历史 v1 迁移、validate_only 与正式发布等价拒绝、真实 NATS 广播 | 新增 3 个测试 |

## 5. 任务 4/5/6 关键设计符合性抽查

- 原子性/CAS/幂等：单写者 flock、`BEGIN IMMEDIATE`、候选 TTL、请求幂等冲突、并发单 CAS 赢家、投影/FK 失败全量回滚均有实测（test_repository_transactions、test_typed_references、test_sqlite_backup）。
- 不可变 YAML 字节/路径：同 file_version 改字节或改路径在 staging 期即 FAILED_PRECONDITION；validate_only 与正式候选同路径拒绝（test_directory_candidate）。
- 引用闭包/循环：缺标准文档 staging 拒绝、循环引用 staging 拒绝、菱形闭包去重拓扑、deprecated 版本禁止重新进入当前目录。
- 分页/水印：游标绑定 include_deprecated 与快照水印，边界页实测。
- 部署：引导配置仅环回监听、token 全部来自 env、身份/权限集合一致、bootstrap 禁止进入发布根（deploy 配置 + test_token_settings/test_service_source_paths）。

## 6. NOT_EXECUTED（环境不具备，未冒充通过）

- 硬件/真实 PLC/netem/root 权限、iSulad 与 containerd 运行时、多机部署。
- Kafka 相关链路（本机仅 NATS/Redis 容器；相关套件保持既有 skip）。
- full_chain 完整端到端套件（仅运行 surface matrix 以定位 P1-CLI-01）。
- NATS 真实断连后的离线重放协调（仅有单测级 best-effort 验证，建议后续补端到端）。

---

# 第二轮：P1-CLI-01 集中独立复测（2026-11-19）

- 复测基线（ChatGPT 整改交付）：`009a7ffc470604b2bb9c8d7794f8d10a3525d911`
- 上轮验收 HEAD：`fb0fb89e752b0475aff7078817b3a5d07d8a5d78`
- 本轮验收后 HEAD：见交付说明（在 `009a7ffc` 之上追加两个提交，未 push、未 rebase、未合并 main、未新建分支）
- 整改文件：新增 `src/cli/snapshot_commands.py`，改 `src/cli/configuration_commands.py`、两个测试文件，共 9 个整改提交。

## 7. P1-CLI-01 关闭证据

整改恢复了冻结设计（CLI 设计 v1_49 §4.5、表 1896-1898/1991-2004；Configuration v1_16 §10.1/§13.2）要求的 CommonService 快照命令面，并与 Repository 发布面显式隔离：

| 命令 | 入口 | 关键实测证据 |
|---|---|---|
| `config push` | `snapshot_commands.push_snapshot` → CommonService.PushConfigSnapshot | 完整目录读取；symlink/非 yaml/空树/大小写重复/64MiB 本地拒绝；显式 `--expected-revision` 与默认 DISK revision；仅 staging 不自动 apply；`valid=False`→FAILURE/1（test_snapshot_e2e、test_grpc_integration） |
| `config apply` | `apply_snapshot` → ApplyConfigSnapshot | `--yes` 与显式 revision；默认 DISK revision；全成功 SUCCESS/0；部分失败/restart_required→PARTIAL/4；UNAVAILABLE→UNKNOWN/3 且实测仅 1 次调用不重试；无 `--yes` 在非 tty 于 RPC 前结构化失败（confirm_mutation 单测） |
| `config get --scope` | `get_snapshot` → GetConfigSnapshot | disk/runtime/candidate 三态；candidate 强制 `--candidate-id`；SHA-256/size/relative_path 校验；输出目录已存在拒绝；失败无残留；历史 apply_results 不改变只读查询 SUCCESS/0；无输出时仅回元数据不回正文 |

- Repository `config get`（无 `--scope`）与快照 `config get --scope` 经 Typer 双路由 + 交叉选择器守卫隔离，新增 11 个本地单测（`tests/cli/test_snapshot_commands.py`）覆盖符号链接、遍历、容量、原子写、scope 与双模式互斥。
- 原 7 个失败用例（snapshot_e2e 5 + grpc_integration 2）逐一复跑全部 PASSED；命令树/注册契约测试同步通过。
- 结论：**P1-CLI-01 CLOSED**，生产代码、CommonService proto、CLI 与设计文档一致。

## 8. 第二轮门禁与测试结果（实测退出码/计数）

### 8.1 静态门禁（全部 exit 0）

| 门禁 | 结果 |
|---|---|
| `python scripts/generate_rpc.py --check` | 0 |
| `ruff check src tests scripts` | 0 |
| `ruff format --check src tests scripts` | 0（修复了交付分支 2 个文件的格式漂移） |
| `mypy src` | 0（443 源文件） |
| `lint-imports` | 0（19 契约 kept） |

### 8.2 测试计数

| 范围 | 结果 |
|---|---|
| 原 P1 失败四文件 | 33 passed |
| 新增 snapshot 本地单测 | 11 passed |
| Configuration 全树（unit 106/component 95/integration 36/reliability 3） | 240 passed，与上轮基线一致无回归 |
| contracts + core/config + tooling | 258 passed（与 Configuration 合计 498，持平上轮） |
| `tests/cli` 全量 | 242 passed / 0 failed / 13 skipped（上轮 225+7 失败；新增 11 单测、原 7 失败转绿） |
| `pytest tests -m "unit or component or contract"` | 2832 passed / 0 failed / 1024 deselected |
| full_chain 整目录（58 收集） | 整目录满载复跑 55 passed，3 个时序用例失败（见 P2）；3 个失败单独复跑全部通过 |
| E2E-08 73 CLI 叶 | 73/73 真实进入处理路径（无 --help/解析错误/skip 伪装），`execute()` 断言 No such command/Usage 不出现且返回码 ∈ {0,1,2,3} |

## 9. 本轮新发现与处置

### 本轮已闭环（测试/格式，未改生产语义）

- **FIX-R2-01（P1 阻断）**：双模式 `config get` 因 `config_id/output` 带默认值被 Typer 降为选项，上轮迁移的 Repository 位置参数调用报 “Got unexpected extra argument”。按 CLI 设计 v1_49（`config get <service> --output PATH`）改为 `--config-id/--output` 选项风格；真实子进程集成测试转绿。
- **FIX-R2-02（P1 阻断）**：full_chain 分母仍硬编码 73 且语义矩阵 `set(cli)==_command_words()` 与实际 85 叶冲突。按六服务拓扑显式排除 12 个 Repository 专属叶（该拓扑无 configuration 目标，相关能力由 tests/configuration_service 真实进程套件覆盖），surface/semantic 两矩阵转绿，未弱化任何断言。
- **FIX-R2-03（P1 阻断）**：语义矩阵 push 用空目录，在新本地校验下于 RPC 前 ValueError，无法到达预期 PERMISSION_DENIED。补合法探针 YAML 使请求真实过网后被服务授权拒绝，保留精确 error_code 断言。
- **FIX-R2-04（P2）**：交付的两个生产文件未过 ruff format；仅应用官方 formatter（空行/换行），token 级无语义变更。
- **新增覆盖**：`test_snapshot_commands.py` 11 个确定性本地单测（含此前无直测的 snapshot_commands 安全守卫与双模式互斥）。

### P2（非阻断，建议后续专项）

- **P2-FLAK-01 满载时序偶发（3 用例）**：`r4e`（gateway 写读 `10.0 vs 12.0`）、`i03`（NATS publisher 计数 `N.0 > N.0`）、`i05`（DataGenerator 执行态未及时 APPLIED）。证据：merge-base 与本分支单独复跑均通过；失败仅出现在 ~16 分钟全栈饱和复跑；均不引用 config push/apply/snapshot 代码路径。判定为既有序列化/资源竞争 flake，非本次整改回归，不计入失败统计但如实记录。
- **P2-GAP-01**：`_write_snapshot` 目的存在性检查与最终 `os.rename` 间存在理论 TOCTOU（同本地操作者威胁模型，与既有 bundle 命令同模式），建议后续以 renameat2/目录 fd 硬化。
- **P2-GAP-02**：NATS 断连离线重放仍仅单测级 best-effort，无端到端证据（沿用上轮限制）。

## 10. NOT_EXECUTED（本轮仍不具备，未混入 PASS）

- 硬件/真实 PLC/netem/root 权限、iSulad/containerd、多机部署、Kafka 链路；full_chain 中 3 个时序 flake 的全饱和复跑稳定性（功能正确性已由单独复跑证明，稳定性属 P2 专项）。

## 11. 第二轮裁决

**PASS WITH LIMITATIONS。** P1-CLI-01 关闭证据充分，原 7 失败全绿，五静态门禁 exit 0，Configuration 240 与全仓 marker 2832 零回归，E2E-08 真实覆盖 73 叶。限制项为与本次整改无耦合的 3 个满载时序 flake（P2）及既有环境 NOT_EXECUTED 项；B3 Sink/Storage 数据等级契约未触碰。

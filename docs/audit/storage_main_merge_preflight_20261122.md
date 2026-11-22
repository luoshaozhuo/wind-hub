# Storage Service → main 合并前审查与冲突分析（2026-11-22）

- 来源分支 S：`feat/storage-service-v1-implementation`
- 目标分支 M：`origin/main`
- 审查性质：**只读独立审查，未执行正式合并、未创建合并提交、未改任何远端引用**
- 审查人：Claude Code；最终合并裁决权归 ChatGPT

## 1. 最新 Git HEAD 与工作树状态

| 引用 | SHA | 与任务给定 SHA |
|---|---|---|
| `origin/main` | `8eaa3ef842bf49d9a54e45c965ef3e24c11fa89d` | 一致，审查期间无更新（审查后再次 fetch 确认） |
| `origin/feat/storage-service-v1-implementation` | `a691e5a11578d2969e2544aa77f9cbd053f6faa4` | 一致 |
| merge-base | `b86a32534ce7af49d492cd220425c9fb27c7be50` | 一致 |
| 本地工作树 | 干净，当前位于 S 分支，未切换 | — |

分叉规模：M 独有提交 **216** 个、S 独有提交 **400** 个；M 改 166 个路径、S 改 284 个；
双边同改文件 **107** 个。未发现其他并发开发分支直接作用于本次合并范围。

## 2. 差异统计与冲突总览

### 2.1 文本冲突（`git merge-tree --write-tree` 内存三方合并，rc=1）

- 真实文本冲突文件：**80 个**（自动合并成功的双边文件 27 个，但其中存在语义潜伏点，见 4.2）
- 按模块分布：tests/sink_service 16、tests/configuration_service 14、src/sink_service 8、
  src/contracts 6、tests/cli 5、src/collector 5、src/configuration_service 4、src/cli 4、
  tests/system 4、tests/collector 3、tests/src 2、src/industrial_gateway 2、configs 2、
  tests/support 1、tests/contracts 1、src/web_server 1、src/core 1、docs/design 1。

### 2.2 权威文件保护情况

- `src/storage_service/**`（28 文件）、`tests/storage_service/**`（19 文件）、
  Storage 设计文档（v1_0→v1_4 重命名）、Storage 专属配置与 CLI：
  **M 全部零改动，均为 S 纯新增（47 个权威路径），无任何文本冲突面**。
- M 独有新增 49 个文件、S 独有新增 77 个文件，三方合并对单边新增天然保留。

## 3. 冲突矩阵（按簇汇总，逐项明细见各簇结论）

| 文件/簇 | M 变化 | S 变化 | 冲突类型 | 推荐处理 | 风险 | 验证 |
|---|---|---|---|---|---|---|
| `src/contracts/configuration_repository.proto` +4 生成物 | 加 relative_path/manifest_sha256/GetPointTableBundle/GetCurrentRevision | M 的严格超集，仅多 GetServiceConfigBundle 两 message+一 RPC，字段号无冲突 | 文本（实为超集） | **取 S 后重新生成** | P2 | generate_rpc --check、test_generation、F3 部署流 |
| `src/contracts/common.proto`+pb2（未在冲突清单，S 单边） | 未改 | 加 target/active_revision 两字段 | 无文本冲突 | 随 S 增量 | P3 | mypy/生成一致性 |
| `src/contracts/sample_batch.py` + 两测试 | v3 七字段、严格 subject 正则、B3 两轮加固 | 同为 v3 但独立分叉：SampleItem 多 5 个必填字段、正则放宽 | **契约/语义 P0** | **取 M，拒绝 S 方言** | P0 | test_sample_batch 40 测、Sink/Collector 全回归 |
| `src/core/configuration/publication_identity.py` | 无版本文件名约定、保留 tasks/devices_sim | 版本化前缀+deployments 解析、删 tasks | 语义互斥 | **手工融合**（M 为基，增量补 deployments/版本化并列分支） | P1 | M 目录测试 + F3 部署流 |
| `src/core/configuration/deployment.py`、`local_selection.py` | 不存在 | S 新增；deployment.py 仅依赖标准库；local_selection 依赖 `core.infrastructure.config.read_yaml`（M 同签名存在，已核实） | 纯新增 | 随 S 引入 | P2 | F3/F6、import 冒烟 |
| `src/configuration_service` 4 冲突（ports/service/repository_server/repository） | B3 全目录发布、token 鉴权、legacy sources、task 钉版 | 增量加 get_version_closure 与 GetServiceConfigBundle；但 service.py 删除 M 仍在使用的 `_resolve_legacy_sources` | 语义 P0/P1 | **M 为基手工嫁接 S 三方法+deployment 校验，保留 M legacy 通路** | P0 | config/collector/commander 回归 + F3 |
| `src/cli` 4 冲突 + main.py（自动合并但语义断裂） | config_app 扁平命令树（12 个 config 叶子）、TLS 双通道+内联 Bearer | 独立 configuration Typer/sync 命令、全量退役 TLS、共享 grpc_bearer、storage 接线 | 架构分叉 | **M 命令树为基，仅嫁接 storage Typer 与 storage token 映射** | P0 | test_command_tree、r1f3、storage 测试 |
| `src/sink_service` 8 冲突 + 6 潜伏文件 | B3 Bearer、业务投影带外、**编码器有意拒绝 raw**、删除单数 encode 入口 | 三值编码器、rich SampleItem、DataLevel 在 delivery_point | 语义 P0（局部） | **整体取 M**；Storage 线契约测试按 M API 改写 | P1 | Sink 全量、test_sink_wire_contract 改写 |
| `src/collector` 5 冲突 | protocol-only，业务转换明确划归 Sink | 业务闭包、rich item、publisher 四参 | 架构分叉（M 有意决策） | **取 M** | P1 | Collector 全回归 |
| `src/industrial_gateway`/`web_server` 3 冲突 | 双监听+管理面 mTLS 已验收 | 纯 Bearer 平行迁移 | 安全姿态互斥 | **取 M** | P2 | Gateway/E2E 回归 |
| `configs/services/cli.example.yaml` | v1.1.0、TLS、v1.1.0 配置路径 | 加 storage 块，但 file_version 倒退且**引用双方均已删除的 v1.0.0 YAML（实测缺陷）** | 配置 | M 为基嫁接 storage 块 | P2 | test_config_loader |
| `configs/services/sink_service.yaml` | schema 2、三 caller | 旧 schema 1 | 配置 | 取 M | P2 | Sink 配置测试 |
| `tests/support/configuration_live.py` | Bearer harness，工厂签名 `(root, port)` | 旧 PKI 签名 `(root, pki, port)` | 测试夹具 | 取 M，改 S 侧 6 处调用点 | P1 | F3/F6 |
| 80 个冲突中的测试文件（46 个） | B3/SampleBatch v3/新命令树夹具 | S 方言夹具 | 测试 | 绝大多数取 M；Storage 专属 47 文件全保留 | P1/P2 | 分模块回归 |
| `docs/design/Sink_Service_软件设计.md` | M 去版本化稳定文档（rename/add） | S 修改旧 v1_70 触发 rename/add 冲突 | 文档 | 取 M，S 的三值描述按需回写 | P3 | — |
| `pyproject.toml`/`poetry.lock`/`scripts/generate_rpc.py` | 未改 | 纯增量（storage 包、taos-ws-py、import-linter 契约、N802 豁免、aiokafka mypy 豁免、proto 条目） | 无文本冲突 | 随 S，合并后重生成校验 | P2 | 静态门禁 |
| `.github/workflows/ci-storage-f1-f4.yml` | 无 | S 新增，仅在特性分支触发 | 配置 | **不建议合入 main**（合入后永不触发），CI 纳入 ci-pr/ci-fast 另立任务 | P3 | — |

## 4. 语义/契约冲突清单（自动合并成功 ≠ 语义正确）

### 4.1 P0：SampleBatch 两种 v3 方言互斥（已实测代码）

- M 的 `SampleItem` 七字段、`extra="forbid"`；S 版多 `raw_value/business_value/unit/business_unit/business_point_id`
  五个**必填**字段。两版均 `extra="forbid"`，构造代码互不兼容。
- M 已用带外 `BusinessValue` 映射实现等价业务值传递（`kafka_wire.py` business_values 形参），
  Storage 生产代码 **不 import contracts.sample_batch**（grep 零命中），其 Kafka JSON v2 解码器独立。
- 结论：取 M 不阻断 Storage 生产；受影响仅 1 个 Storage 测试（见 4.3）。
- 架构岔路（需 ChatGPT 知晓）：M 的 Sink 编码器 `_require_protocol` **有意拒绝 raw**
  （`kafka_wire.py:28-33`，提交 463ff373f "reject fabricated RAW"），而 Storage 解码器接受 raw。
  Storage 侧 raw 入库能力由其自有集成测试（直接构造 JSON）保证；真实 Sink→Storage 的 raw
  全链属于 T-14，本就 NOT_RUN，不构成本次合并阻断。

### 4.2 P0/P1：自动合并后的潜伏崩溃（已实测，共 13 个 src 文件 + 若干测试）

S 单边修改、M 未改而自动并入 S 版本的文件，依赖在冲突解决后若取 M 则不存在的符号：

- `src/sink_service/application/reliable_policy.py`（自动合并）import
  `delivery_point.DataLevel` 并调用三参 `select_delivery_points`；M 版 delivery_point 无该符号、
  函数为两参 → **import 即崩**。同类：`output_pool.py`、`ports/output.py`、`realtime.py`、
  `realtime_delivery.py`、`nats_consumer.py`。
- 13 个 src 文件引用 S 独有的 `core.infrastructure.grpc_bearer`（commander/data_generator/
  industrial_gateway/web_server 的客户端与测试）；M 采用服务内联 Bearer，无此模块。
- `tests/src/component/test_collector/test_publisher_v2.py` 自动合并但 hunk 可能残留 S 四参调用。

处置原则：非 Storage 模块的 S 平行实现整体回退 M 版本，不得依赖自动合并结果逐 hunk 接受；
融合后必须跑导入冒烟与全模块回归才能发现全部潜伏点。

### 4.3 Storage 测试的适配面（实测，边界清晰）

在隔离 worktree（detached M + 仅拷贝 Storage 权威文件与新增 core 模块，未融合契约）实测：

| 验证项 | 结果 |
|---|---|
| `pytest tests/storage_service/unit`（90 项，剔除 1 项Sink 线契约） | **85 passed, 5 failed**，5 个失败全部为 `GetServiceConfigBundleResponse` 缺失（同一 RPC 根因） |
| `pytest tests/storage_service/component`（11 项） | **10 passed, 1 failed**，同样为该 RPC 缺失 |
| `test_sink_wire_contract.py` 收集 | ImportError：M 的 DataLevel 移至 kafka_wire、单数 `encode_kafka_point` 已删、SampleItem 拒 rich 字段 → **测试侧改写**（用 M 的 encode_kafka_points keyword API 与 BusinessValue 投影），生产 Storage 不受影响 |
| `mypy src/storage_service` | 6 错：4 个为缺失 RPC/common 字段（融合 proto 后消失），2 个 aiokafka stubs（S pyproject 豁免覆盖） |
| `ruff check` | 18 个 N802，全部由 S pyproject 的 per-file ignore 覆盖 |
| `ruff format --check` | 47 文件全部合规 |

另需适配的测试夹具：F3 `test_storage_deployment_flow.py` 与 F6 `test_cli_restart_activation.py`
对 `write_live_configuration(root, build_pki(...), port)` 的调用须改为 M 的两参签名
（全 storage 测试簇仅 1 处 build_pki 调用），token 环境变量改名对齐 M
（`WIND_HUB_CONFIG_CLI_ADMIN_TOKEN` 等）。

## 5. 共享文件融合建议（§2.3 重点项结论）

1. **Contracts**：configuration_repository 取 S（严格超集，已逐行核实 M 新增被完整包含），
   合并后执行 `python scripts/generate_rpc.py` 全量重生成 8 个 stem 再 `--check`；
   common 增量随 S；sample_batch 取 M。Storage 自身 4 个 proto 生成物纯新增。
2. **Core**：新增 deployment.py/local_selection.py（read_yaml 依赖 M 已满足）；
   publication_identity 手工融合，M 的 tasks/无版本约定与 S 的 deployments/版本化约定并列共存。
3. **Configuration Service**：M B3 实现为基，嫁接 S 的 `get_version_closure`（ports+repository
   各一小方法）、`GetServiceConfigBundle` RPC（server 约 26 行）、`_validate_deployments` 与
   `get_service_config_bundle`（service 约 150 行）；**必须保留 M 的 legacy sources 与 task 钉版**。
4. **CLI**：保留 M 命令树/TLS 双通道/内联 Bearer；仅追加 storage Typer 注册、
   service_rpc 映射、runtime 的 `storage → WIND_HUB_STORAGE_OPERATOR_TOKEN` 一项；
   main.py 须人工融合（自动合并会引入 S 的 configuration Typer 造成导入断裂）；
   S 的 configuration_directory/sync 体系与 M publish 体系冗余，建议不移植（P3 清理）。
5. **tests/support**：configuration_live 取 M；bearer.py（仅依赖自包含 grpc_bearer）与
   storage_live.py 为 S 纯新增可保留——建议同步引入 `src/core/infrastructure/grpc_bearer.py`
   （仅 stdlib+grpc，M 无此路径，零冲突），避免重写 F3/F6 认证夹具；是否接受该共享模块由 ChatGPT 裁定。
6. **configs/deploy**：cli.example 以 M 为基嫁接 storage 进程块；sink 配置取 M；
   Storage 三个专属 YAML 纯新增；S 对 commander/gateway/dg 示例配置的平行修改回退 M。
7. **依赖/工具**：pyproject/poetry/generate_rpc 随 S 增量；CI workflow 不并入，
   另建任务把 storage 测试纳入 ci-pr/ci-fast（当前 ci_scope/ci_gate 无 storage 分类，P2 跟进）。

## 6. M4 验证结果与真实退出码

| 验证 | 环境 | 结果（退出码） |
|---|---|---|
| 隔离 worktree 三方合并模拟 | `git merge-tree --write-tree`（只读，无工作树/引用污染） | rc=1，80 冲突，模拟树含冲突标记不可运行，未伪造可运行合并 |
| Storage 权威文件叠加纯 M 的单测 | 隔离 worktree + `PYTHONPATH=<worktree>/src` | unit 85/90、component 10/11（失败全部归因缺失 RPC，见 4.3） |
| 静态检查（叠加态） | mypy/ruff | mypy 6（4 契约缺失+2 stubs）、ruff 18 N802（配置豁免覆盖）、format PASS |
| 临时 worktree 清理 | `git worktree remove --force` | 已清理，`git worktree list` 仅余主工作树 |
| 当前 S 分支既有门禁（F6 证据，未重复全套） | 主工作树 | F6 已记录五门禁全 0、94 unit/11 component/真实集成全绿 |

未执行项（NOT_RUN）：融合后完整回归（冲突未裁决前无合法融合树）；真实 Kafka/TDengine/NATS
复测留待融合分支建立后按 F5/F6 用例定向执行。

## 7. P0–P3 风险清单

| 级别 | 风险 | 类型 | 处置 |
|---|---|---|---|
| P0-1 | sample_batch v3 方言二选一，取错则 Collector/Sink 全链 TypeError | 合并引入缺陷 | 取 M，S rich 字段不纳入 |
| P0-2 | Configuration service.py 融合若丢 legacy sources，M 的 collector/commander 测试簇失效 | 合并引入缺陷 | M 为基嫁接，双通路并存 |
| P0-3 | CLI main.py/runtime.py 自动合并语义断裂、storage token env 命名不一致致 401 | 合并引入缺陷 | 人工融合，storage 仅增量接线 |
| P0-4 | GetServiceConfigBundle 契约链（proto→pb2→server→service→repository→ports）任一环节漏融合，Storage 配置激活全断 | 合并引入缺陷 | 按 §5.3 整链嫁接并以 F3 用例验收 |
| P1-1 | publication_identity 目录约定互斥 | 契约 | 并列融合，双侧测试都须通过 |
| P1-2 | 13 个潜伏自动合并文件的 S 方言依赖 | 合并引入缺陷 | 非 Storage 模块统一回退 M + 导入冒烟 |
| P1-3 | configuration_live 6 处 PKI 旧签名调用、shipped_yaml 未排除 storage_bootstrap | 测试/Fixture | 取 M 后定向改测试 |
| P1-4 | Sink raw 级 E2E 与 M "reject fabricated RAW" 策略的长期关系 | 架构决策（非本次阻断，T-14 NOT_RUN） | ChatGPT 裁决 raw 全链路线图 |
| P2 | proto 重生成一致性、cli.example 融合、CI scope 未登记 storage、ports/repository 协议-实现配对 | 工程 | 融合后门禁 |
| P3 | S 孤儿模块 nats_revision_publisher/local_selection 零导入、cli.example 引用已删 YAML 的既有缺陷、CI workflow 入 main 即失效、Sink 文档 rename 冲突、token 正则差异 | 文档/清理 | 合并后清理 |

无证据表明 Storage 权威代码本身存在生产缺陷；全部 P0 均为**融合过程风险**而非 S 或 M 既有缺陷。

## 8. 推荐正式合并步骤（供 ChatGPT 裁决后执行）

1. 从最新 `origin/main` 建融合分支（不用 -X ours/theirs）。
2. 整体引入 Storage 权威 47 路径 + 三个专属配置 + storage proto 生成物 + 两份验收报告。
3. 契约：configuration_repository 取 S 超集并全量重生成；common 随 S；sample_batch 取 M。
4. Core：新增 deployment/local_selection/grpc_bearer（可选）；publication_identity 并列融合。
5. Configuration：M 基嫁接 ServiceConfigBundle 整链，保留 legacy/task；测试取 M + 移植
   F3 部署流与 revision_files_migration，按新 harness 改签名。
6. CLI：M 基嫁接 storage 命令树与 token 映射；其余 S CLI 平行重构不移植。
7. Sink/Collector/Gateway/Web 及其测试整体取 M；改写 test_sink_wire_contract 适配 M API；
   剔除 S 独有且与 M 不兼容的测试（test_gateway_output_bearer、_stage1_tls 等）。
8. pyproject/poetry/generate_rpc 增量合并；CI storage 任务单独立项。
9. 门禁序列：导入冒烟 → ruff/mypy/import-linter/generate_rpc --check →
   configuration/cli/collector/commander/sink 全回归 → storage 94+11 → F3/F6 真实栈定向复测。
10. 全部绿后再按仓库 `//merge-branch` 治理流程 squash 合并 main。

## 9. 需要 ChatGPT 裁决的事项

1. **P0 方言裁决**：确认 sample_batch 取 M 七字段 v3、放弃 S rich 五字段（本报告建议取 M）。
2. **raw 等级路线**：M Sink 有意拒 raw 与 Storage 接受 raw 的长期关系；本次仅保证 Storage 单侧能力，
   真实 raw 全链维持 T-14 NOT_RUN 是否认可。
3. **grpc_bearer 共享模块**：作为自包含增量引入（测试夹具改动最小）vs 把 F3/F6 测试改写为
   M 的服务内联 Bearer 风格。
4. **S 平行 CLI/Collector/Sink 重构**：确认整体不移植、以 M B3 架构为准。
5. **CI 纳入方式**：ci-storage-f1-f4.yml 不进 main，storage 测试纳入 ci-pr/ci-fast 的具体任务设计。

## 10. 最终结论

**MERGE_READY_WITH_FIXES。**

Storage 权威代码与 main 零重叠、无既有生产缺陷，且隔离实测证明 101 项 unit/component 中 95 项（94%）
在未融合契约的 M 基线上即可通过，剩余 6 项全部收敛到同一个待融合 RPC；
但三方合并存在 80 个文本冲突与 13+ 个自动合并语义潜伏点，4 项 P0 全部是需要按本报告配方手工
融合的契约/接线工作，不能依赖 Git 自动合并或目录级 ours/theirs。融合 recipe、验证序列与裁决点
已齐备，ChatGPT 完成第 9 节裁决后即可按第 8 节执行。Claude 未执行正式合并，等待最终裁决。

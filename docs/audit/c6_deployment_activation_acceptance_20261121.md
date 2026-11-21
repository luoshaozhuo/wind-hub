# C6 集中测试、审查与复测 — 独立验收报告

- 仓库：`luoshaozhuo/wind-hub`
- 固定分支：`design/config-activation-selection-20261121`
- 交接 HEAD：`dccea84e5f9edb7fdc73237f3b0739d3013bb222`（本地=远端，工作树起始干净）
- 验收起始基线：`origin/main`
- 验收日期：2026-11-21
- 验收后新增提交：见第 7 节
- **最终裁决：FAIL**

> 裁决理由（摘要）：main 静态门禁全绿，而本分支在交接 HEAD 上静态门禁全面红灯（ruff/mypy/format/
> Proto 绑定），并存在可运行的生产缺陷（NameError、TypeError、mypy 类型错误）。六个业务模块的
> 测试套件仍编码 C 系列之前的旧配置契约（非版本化文件名、无 `service_id`、`config_sources.templates.root`、
> YAML 任务覆写），与生产代码已统一实现的新 Deployment 选版契约系统性脱节，造成 300+ 用例失败/错误。
> 验收方已修复全部静态门禁与可明确定位的生产缺陷、解除两处整模块收集阻断，并实证生产选版逻辑正确；
> 但全量测试契约迁移属独立的大型工程，未在本轮闭环，故整体不予通过。

---

## 1. 环境与基线

命令：

```bash
python3 scripts/dev.py env
# Python 3.11.16 (env wind-hub)，pytest/ruff/mypy/importlinter 均 PASS
git fetch origin
# 本地 HEAD = origin/design/... = dccea84e5，与交接一致
```

基线对照（worktree `/tmp/c6-main` @ `origin/main`，`PYTHONPATH=<worktree>/src`）：

| 检查 | main 基线 | 本分支交接 HEAD |
|---|---|---|
| `ruff check src tests scripts` | 0 错误 | **21 错误** |
| `ruff format --check src tests scripts` | 0 脏文件 | **25 脏文件** |
| `mypy src` | 0 错误 / 446 文件 | **5 错误 / 451 文件** |
| `lint-imports` | 19 kept | 19 kept（PASS） |
| `generate_rpc.py --check` | PASS | **3 绑定陈旧，阻断 contracts 收集** |
| `generate_api_contract.py --check` | PASS | PASS |

结论：main 全绿，本分支全部静态红灯均为 **RELATED** 分支回归，无 UNRELATED 项。

---

## 2. 静态门禁明细（交接 HEAD，修复前）

### 2.1 mypy（5 错误，全部 RELATED，已修复）

| 文件:行 | 错误 | 性质 |
|---|---|---|
| `src/data_generator/infrastructure/config/service_settings.py:344` | `select_local_service` 未定义 (name-defined) | **生产 P1：运行时 NameError** |
| `src/sink_service/application/delivery_definition.py:22` | `config_sources()` 缺少位置参 `available` | **生产 P1：运行时 TypeError** |
| `src/sink_service/application/delivery_registry.py:97` | 同上 | **生产 P1：运行时 TypeError** |
| `src/configuration_service/application/service.py:522` | `str | None` 赋给推断为 `str` 的变量 | 类型 P1 |
| `src/collector/infrastructure/config/validation.py:282` | `read_yaml(Path | None)` | 类型 P1（缺空值守卫） |

### 2.2 ruff check（21 项）

- 7 × I001 import 排序、4 × F401 未用导入、1 × C413、8 × E501、1 × F821（同 DG NameError）。
- 全部位于本分支改动文件；main 基线 0。

### 2.3 ruff format

25 个文件不达标（main 基线 0），均为本分支提交未格式化。

### 2.4 Proto 绑定一致性

`generate_rpc.py --check` 报告 3 个陈旧绑定，并直接阻断 `tests/contracts` 整个目录收集：

- `src/contracts/common_pb2.pyi`
- `src/contracts/configuration_repository_pb2.pyi`
- `src/contracts/configuration_repository_pb2_grpc.py`

重新生成后核对：差异仅为 codegen 风格（`.pyi` 字段/空行排序、gRPC 桩 docstring），
**无任何 `_pb2.py` 描述符字节变化**，线上 wire 契约语义不变；属 protoc 工具版本风格漂移。

---

## 3. 模块测试台账（交接 HEAD，修复前实测）

| 模块 / 范围 | 命令 | 结果 |
|---|---|---|
| core 全量 | `pytest tests/core -q` | **14 failed, 503 passed** |
| configuration_service 全量 | `pytest tests/configuration_service -q` | **77 failed, 179 passed** |
| data_generator 全量 | `pytest tests/data_generator -q` | **106 failed, 81 passed, 7 errors**（456s） |
| commander unit+integration | `pytest tests/commander/unit tests/commander/integration -q` | **52 failed, 72 passed, 77 errors** |
| industrial_gateway unit/component/integration | `pytest tests/industrial_gateway/... -q` | **4 failed, 82 passed, 85 errors** |
| sink unit/component/integration | `pytest tests/sink_service/{unit,component,integration} -q` | **132 failed, 452 passed, 10 skipped, 7 errors** |
| collector unit+component | 同左 | **收集阻断**（导入已删除模块，1 error） |
| deploy | `pytest tests/deploy -q` | **1 passed, 35 errors**（导入已退役模块） |
| contracts | `pytest tests/contracts -q` | **收集阻断**（陈旧绑定） |

### 3.1 失败根因分类（高度统一）

1. **旧配置契约夹具（主导，约 90%）**：测试仍构造/引用
   - 非版本化文件名：`services/collector.yaml`、`sink_service.yaml`、`commander.yaml`、
     `instances/site.yaml`、`instances/devices.yaml`、`templates/units_standard.yaml` 等；
   - Service 文档缺 `service_id`/`file_version` 头；
   - 旧 `config_sources.templates.root` 结构（新契约已禁止，pydantic 报 extra_forbidden）；
   - 已删除的 `templates/tasks/` 类别与 YAML 任务覆写 `mutate_device_intent`。

   代表性报错：
   - sink：`ConfigurationError: configs/services/sink_service.yaml: unsupported schema_version`（136 处）
   - gateway：`ValidationError: service_id field required / config_sources.templates extra_forbidden`（89 处）
   - commander：`ValueError: Service must reside in configs/services`（64 处）
   - DG：`ValueError: Service identity and version are required`（28 处）
   - config：`ValueError: unsupported template directory: templates/tasks/...`

2. **生产 NameError**：DG `load_settings → config_sources → select_local_service` 整条链断裂
   （全量日志 84 处 NameError）。
3. **已删除/退役模块的残留引用**：
   - `collector.infrastructure.config.task_control.mutate_device_intent`（被 SQLite Task intent 取代）；
   - `deploy/prepare_collectors.py`（提交 `08e47178f` 退役）。

### 3.2 main 基线对照

14 个 core 失败用例在 main worktree 同环境复跑 **57 passed 全绿**，确认全部为 RELATED 回归。

---

## 4. 生产语义独立验证（不依赖破损夹具，直接实证）

针对需求 4，对 `core.configuration.local_selection.select_local_service` 用真实 `configs/` 与
合成临时树直接验证，全部符合设计：

| 场景 | 结果 |
|---|---|
| 真实 Service 无 env 自动定位唯一 Deployment | PASS（gateway→prod-a，instances 角色正确） |
| 显式 `WIND_HUB_DEPLOYMENT_ID/VERSION` 精确选版 | PASS（prod-a v1.3.0 清单） |
| 非法 Deployment 身份（`../escape`） | PASS，`invalid deployment identity` |
| 非法版本（`latest`，禁止 latest 猜测） | PASS，`invalid selected Deployment version` |
| 缺失引用（不存在的 deployment id） | PASS，`exactly one Deployment must select...` |
| 重复版本/多 Deployment 选择同一 Service | PASS，`exactly one Deployment...` 拒绝 |
| symlink 实例引用 | PASS，`symbolic-link configuration references are forbidden` |
| 跨部署隔离 | 结构确认：prod-a 与 sim-a 选择不相交 Service 集，显式 env 命中正确清单 |

需求 5–9 的生产代码结构审查：

- **target/active 不匹配降级**（提交 `5bed6a6e`）：collector/commander/sink 三服务 gRPC 状态面均在
  `target_revision != active_revision` 时标记降级，PASS（结构）。
- **Sink CURRENT Generation（需求 6）**：`main.py` 重启时经 `CURRENT` 指针重新选定已提交的
  Deployment Service 版本；`WorkerSupervisor._spawn` 以 `active_revision()` 绑定重启 Worker；
  `worker.py` 启动加载不可变 generation，APPLY 代际不匹配即拒绝。逻辑自洽。
  但专项系统测试 `test_uc09/uc10`、`test_config_repository` 因旧夹具 9/10 失败，**运行证据 NOT 闭环**。
- **Commander（需求 7）**：`config_manager.py` 仅在 `not blocked and not restart_required` 时才提交新
  settings 源，完整激活后才保留，后续 Reload 不回退旧版本，符合设计（结构）。
- **Gateway 并发（需求 8）**：`ConfigSnapshots.get`（RPC 快照）与 `apply`（Repository 激活）共用同一把
  `asyncio.Lock`，结构上杜绝混用配置，PASS（结构）。
- **render_cli / provision / Compose（需求 9）**：精确 `WIND_HUB_DEPLOYMENT_ID/VERSION` 环境变量与
  显式 `service_<id>_v<ver>.yaml` 启动参数，Compose 以 `:?` fail-closed；`tests/deploy` 修复后 34 passed。

> 标注「结构」者为代码审查结论；对应自动化用例因夹具旧契约未能提供运行证据，按规则记 NOT_RUN/未闭环，
> 不视为测试通过。

---

## 5. 缺陷分级台账

### P1（生产/阻断）

| 编号 | 位置 | 问题 | 处置 |
|---|---|---|---|
| P1-1 | `data_generator/.../service_settings.py:344` | 缺导入致 NameError，DG 配置链全断 | 已修复并提交 |
| P1-2 | `sink_service/application/delivery_definition.py:22` | 单参调用双参函数，TypeError | 已修复（补 `snapshot.documents`） |
| P1-3 | `sink_service/application/delivery_registry.py:97` | 同 P1-2 | 已修复 |
| P1-4 | `configuration_service/application/service.py:522` | 类型不合法赋值 | 已修复（`or` 同文件范式） |
| P1-5 | `collector/.../validation.py:282` | `Path | None` 未守卫 | 已修复（前置 ConfigError 守卫） |
| P1-6 | 六模块测试夹具 | 系统性旧配置契约，300+ 用例失败 | **未闭环（大型迁移工程）** |

### P2（测试/架构/覆盖）

| 编号 | 问题 | 处置 |
|---|---|---|
| P2-1 | `select_local_service`/`parse_deployment` 无专门单元测试（安全选版核心零直接覆盖） | 部分缓解：本轮以脚本实证；建议补正式用例 |
| P2-2 | `SqliteTaskIntentStore` 原零测试 | 已新增 `tests/collector/unit/test_task_intents.py` 4 用例 |
| P2-3 | `core/infrastructure/configuration_repository.py` 引用 pb2 却不在 `core/infrastructure/rpc/` 允许区 | 测试豁免已加；是否迁目录待架构裁定 |
| P2-4 | Proto 绑定无 CI 生成步骤，依赖人工 `generate_rpc.py` 再生成 | 已再生成并提交；CI 缺口仍在 |
| P2-5 | 4 个 23h 陈旧 sink 测试孤儿进程占用随机高端口（历史 aiokafka/multiprocessing 泄漏） | 环境记录，未杀用户进程 |

---

## 6. 验收方修复与验证

### 6.1 静态门禁修复后（全部 PASS）

```
ruff check src tests scripts        PASS（0）
ruff format --check src tests scripts PASS（0 脏文件）
mypy src                            PASS（0 / 451 文件）
lint-imports                        PASS（19 kept）
generate_rpc.py --check             PASS（重新生成，仅 codegen 风格）
generate_api_contract.py --check    PASS
```

### 6.2 测试层

- `tests/deploy`：1 passed + 35 errors → **34 passed**。
- `tests/contracts`：收集阻断 → **108 passed**。
- collector：整模块收集阻断解除；新增 intent 单测 **4 passed**；`tests/collector/unit` 49 passed / 5 failed（5 个为旧契约）。
- 全仓收集：修复后 **3640 测试零收集错误**（修复前 collector、contracts、deploy 三处阻断）。
- sink 快速层修复前后同范围对比：**NEW_FAILURES=0**（无回归），并以合成对象直接证明 P1-2/P1-3
  TypeError 已消除。

### 6.3 未完成项（如实披露）

- 六模块旧契约夹具的批量迁移（P1-6）未完成；本轮不做高风险的大规模夹具重写以免污染现有通过用例。
- 需求 6 的 Sink 真实进程崩溃恢复/重复 Apply、需求 7 Commander 部分应用、需求 10 全栈 CLI 进程 E2E：
  依赖真实中间件与已迁移夹具，本轮 **NOT_RUN/未闭环**，不计通过。
- sink 600s 容量/S4 吞吐等性能与长稳用例不在 C6 语义范围，本轮未执行（历史轮次已独立验收）。

---

## 7. 提交与远端一致性

验收方在固定分支上新增 2 个提交（未建 PR、未合 main、未 rebase、未 force push）：

1. `38d748648 fix(config): restore static gates and repair broken Deployment-selection call sites`
2. `17f3806d7 test(config): retire obsolete YAML-mutation tests and align deploy/rpc guards`

推送后本地与 `origin/design/config-activation-selection-20261121` HEAD 一致（见第 8 节核对）。

---

## 8. 最终裁决

**FAIL。**

- 静态门禁与 5 个可明确定位的生产缺陷已修复并实证，整模块收集阻断全部解除，生产选版核心逻辑经独立实证正确。
- 但 P1-6（六模块测试与新 Deployment 选版契约的系统性迁移，300+ 用例）未闭环，需求 6/7/10 的运行时与
  跨进程 E2E 证据缺失。在测试契约迁移完成并恢复各模块绿之前，不具备准出条件。
- 建议后续：以模块为单位迁移共享夹具至「版本化 Service + Deployment 清单 + 逻辑实例 ID」新契约，
  优先顺序 configuration_service → core → sink → collector → commander/gateway/DG，并为
  `select_local_service` 补正式单测后再复测。

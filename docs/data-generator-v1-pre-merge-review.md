# Data Generator v1_10 合并前清障与最终回归报告

- 日期：2026-11-04
- 执行人：Claude Code
- 仓库：`luoshaozhuo/wind-hub`
- 设计基线：`docs/design/Data_Generator_软件设计_v1_10.md`
- 第二轮独立验收：`docs/data-generator-v1-round2-acceptance.md`（PASS，P1/P2/OBS 全关闭）

> 结论先行：本轮在授权范围内完成全部可做的小修（3 个配置路径测试 + 1 个格式问题），未改动任何冻结设计与 Proto 契约。剩余失败经在 `origin/main` 与合并结果树上**独立实测**，全部为 main 既有债务或落后于 main 的分叉产物，Data Generator 功能本身零回归。最终裁定见第 10 节。

---

## 1. Git 基线与最终 HEAD

`git fetch origin` 后远程无变化，基线与任务书一致：

| 引用 | SHA |
|---|---|
| 功能分支起始 / `origin/feat/data-generator-v1` | `c52d431683b6fa45e86847f3243c38e050dcd03d` |
| 最终本地 HEAD | `9f111e651031b6effa97f5d0ead2cbd1ee07c787` |
| `origin/main` | `40f5983f508c3e3c98256c296445adf553480bec` |
| merge-base | `0dfee3bbbc864eae364fb214e447dea0a447820b` |

分叉情况：main 领先 1 个提交（`40f5983f` collector-v1-stage1-restart 的 squash），功能分支含 Data Generator / Gateway lease 大量提交。

本轮新增提交（均在功能分支，未 rebase、未 force push）：

| SHA | 类型 | 内容 |
|---|---|---|
| `f03740bc` | test | Gateway 出厂/出货配置测试路径修正到 `configs/services/`，修复 3 个失败 |
| `9f111e65` | style | 对 `tests/tooling/test_merge_skill.py` 执行 ruff format（纯格式） |

推送：`git push` 仍失败 `fatal: could not read Username for 'https://github.com'`，与 `c52d4316` 记录的凭证阻断相同。**两个提交目前仅在本地**，凭证可用后 `git push origin feat/data-generator-v1` 即可（fast-forward，无强推）。

---

## 2. G01 / G02 / G03 归因、整改与复测

### 2.1 G01 — Industrial Gateway 测试

任务表记录 17 个失败；本轮在 `c52d4316` 实测为 **18 failed / 61 passed**（较记录多 1，已如实记录）。

关键基线事实：在 `origin/main`（`40f5983f`）上 `pytest tests/industrial_gateway` **连收集都无法完成**——

```
ImportError: cannot import name 'gateway_pb2' from 'contracts'
7 errors during collection
```

main 的 contracts 已改名为 `industrial_gateway_pb2`，但 main 的 Gateway 代码/测试未迁移。功能分支通过 `8ff12bc8`（官方脚本重新生成 Gateway protobuf）将其恢复为可运行（61 passed），是净改善。

对 18 项逐一取栈、对比 main、核对权威 Proto 后，归为四类：

| 类别 | 数量 | 根因 | 处置 |
|---|---|---|---|
| A. 配置路径陈旧 | 3 | 测试仍引用已迁移到 `configs/services/` 的 `configs/industrial_gateway.yaml`，子进程 `--check` / 启动 / 出货配置加载 `FileNotFoundError` | **已修（授权范围内）** `f03740bc`，复测 3 passed |
| B. Commander 契约漂移 | 13 | 生产适配器 `GrpcCommanderWriter` 与测试夹具 `FakeCommanderService` 仍调用**已删除**的同步 `WritePoint` RPC、`WritePointRequest`、`COMMAND_OUTCOME_CONFIRMED_*` / `_UNKNOWN`；权威 `commander.proto` 已重设计为 `SubmitCommand` + `GetCommandResult` 异步模型 | 不修，**上交 ChatGPT**（见第 9 节） |
| C. 空存储读异常语义 | 1（`test_fc01_readback_matches_point_store`） | 未映射单元 `device_id=9` 期望 Modbus 异常响应，实测 pymodbus 返回零值成功；属 pymodbus 集成/协议栈装配行为 | 不修，随协议栈工作上交 |
| D. 非法帧关闭语义 | 1（`test_malformed_length_closes_connection`） | 期望优雅 EOF（FIN），实测 `ConnectionResetError(104)`（RST）；**非确定性**——在合并结果树重跑时该用例通过，属 TCP/pymodbus 时序敏感 | 记录为抖动型既有问题 |

**Commander 漂移的证据链（非本分支引入）**：

- `git diff origin/main...HEAD -- src/contracts/commander.proto src/contracts/commander_pb2*` → **空**，功能分支完全未触碰 Commander Proto；
- merge-base `0dfee3bb` 的 `commander.proto` 上已经只有 `ListDevices/GetDevice/ListPoints/SubmitCommand/GetCommandResult`，无 `WritePoint`；
- 功能分支对 Gateway 生产代码的改动仅限 `target_leases.py`（新增）、`assembly.py`、`data_server.py`（lease / 数据 RPC），未改 Modbus 命令路径；
- 4 个对应 mypy 错误在 `origin/main` 上逐字相同（见 2.2）。

因此 B/C/D 均为**先于本功能存在、main 至今仍带（且 main 更差）的历史债**。按任务书“禁止恢复已废弃契约/旧接口/兼容层”，正确修法是把 Gateway 命令路径迁移到 SubmitCommand/GetCommandResult（含三态、闭锁、恰好一次的异步状态机），属于“Commander Proto 契约变更 / Gateway 与 Commander 职责调整 / 跨模块状态机与并发语义修改”，**超出本轮授权**，未做任何掩盖式修改。

爆炸半径：陈旧的 `GrpcCommanderWriter` 仅在 `config.control.commander` 启用时于 `assembly.py:137` 装配；**出厂配置 `control.enabled = False`**（出货配置加载用例与实跑 `--check` 均确认），默认部署与 Data Generator 集成不触发该路径；失败的命令类测试显式开启了 control。

修复后（`f03740bc`）独立复测：

```
tests/industrial_gateway  →  15 failed, 64 passed   # 含 13(B) + 1(C) + 1(D)
```

### 2.2 G02 — Mypy Commander 接口（4）

`python -m mypy src` 在功能分支为 68 errors / 20 files。其中任务指定的 4 个：

```
src/industrial_gateway/infrastructure/grpc/commander.py:30  Module has no attribute "WritePointRequest"
src/industrial_gateway/infrastructure/grpc/commander.py:38  "CommanderServiceAsyncStub" has no attribute "WritePoint"
src/industrial_gateway/infrastructure/grpc/commander.py:42  ... COMMAND_OUTCOME_CONFIRMED_SUCCESS
src/industrial_gateway/infrastructure/grpc/commander.py:43  ... COMMAND_OUTCOME_CONFIRMED_FAILURE
```

以 `src/contracts/commander.proto` / `commander_pb2.pyi` 为权威核对：当前服务只有 `SubmitCommand` / `GetCommandResult`，**`WritePoint` RPC 与 `COMMAND_OUTCOME_*` 枚举在 Proto 中已不存在**。这不是“与 Proto 对齐的小类型修复”——被调用的 RPC 本身已删除，任何非 `type: ignore` 的修法都要改写命令交互契约与状态机。**未使用 `type: ignore` / `Any`，未降级检查，保持原样并上交。**

全量 mypy 归因（三套结果对比，均实跑）：

| 范围 | 功能分支 | origin/main | 合并结果树（含本轮修复） |
|---|---|---|---|
| mypy 错误数 / 文件数 | 68 / 20 | 68 / 20 | **50 / 16** |
| 工业 Gateway commander（本任务 4 项） | 4 | 4（逐字相同） | 4（main 既有） |
| seahorse（已被本分支删除） | 0 | 14 | 0 |
| Gateway `data_server` `gateway_pb2` | 0 | 3 | 0 |
| `src/collector/*`（collector_v2_pb2、core collector 模块缺失等） | 18 | 0 | **0** |
| web_server / sink_service / cli 历史债 | 32 | 43 | 42（main 既有） |

- 功能分支独有的 18 个 `src/collector` 错误是**落后于 main 的分叉假象**：merge-base 有 33 个旧 collector 文件，main 的 collector-v1-stage1 将其重写为 23 个，功能分支对 `src/collector` **零改动**，合并直接采用 main 版本 → 这 18 个在合并树上**全部消失**（实测）。
- 集合对比结论（排序后逐行 `comm`）：**合并树相对 main 新增 0 个 mypy 错误，消除 17 个**。
- 剩余 50 个错误（web_server 旧 commander/collector 适配器、sink_service 的 `gateway_pb2`/`sink_schema`、cli 缺 `prompt_toolkit`、工业 Gateway commander 4 个）在 main 上即存在，与 Data Generator 无关；其中缺失 `prompt_toolkit` 属可选依赖环境问题。

### 2.3 G03 — Ruff Format（1）

`ruff format --check` 报 `tests/tooling/test_merge_skill.py` 需重排。查看 `--diff` 确认仅为 tuple 的 magic-trailing-comma 展开，**无语义变化**。已 `ruff format` 修复（`9f111e65`），复测：

- `python -m ruff format --check src tests scripts` → `668 files already formatted`；
- `tests/tooling/test_merge_skill.py` → 3 passed。

---

## 3. 全量静态门禁结果

| 门禁 | 命令 | 结果 | 分类 |
|---|---|---|---|
| 环境 | `python3 scripts/dev.py env` | **PASS**（Python 3.14.7 / pytest / ruff / mypy / importlinter） | — |
| Ruff lint | `ruff check src tests scripts` | **All checks passed** | PASS |
| Ruff format | `ruff format --check src tests scripts` | **668 files already formatted**（修复后） | RELATED 已修复 |
| Import Linter | `lint-imports` | **14 kept, 0 broken**（分析 283 文件 / 743 依赖） | PASS |
| Mypy | `mypy src` | 功能分支 68 / main 68 / **合并树 50**；对 main 新增 **0** | 见 2.2，历史债 + 分叉假象 |

> 注：`python -m mypy src` 在本分支与 main 上退出码均为 1，不能写为“全仓全绿”。但其错误无一由 Data Generator 引入，合并后净减少 17 个。

---

## 4. 相关回归（Data Generator / Gateway / Contracts / Core）

### 4.1 功能分支独立结果（`PYTHONPATH=src`）

| 测试树 | 结果 |
|---|---|
| `tests/data_generator` | **169 passed**（与第二轮验收基线一致，零退化） |
| `tests/contracts` | **92 passed** |
| `tests/core/architecture` | **18 passed**（功能分支单独运行） |
| `tests/industrial_gateway` | 64 passed / 15 failed（13 命令链 + fc15 + malformed 抖动） |
| 四树合计 | 342 passed / 15 failed，另出现过 1 次跨套件收集 ERROR（见 4.3） |

未使用任何 skip / xfail 新增 / 删测试 / 放宽断言；既有 xfail 未重新激活。

### 4.2 P1 / P2 / OBS 显式确认

- **P1 跨实例租约（15 场景）全部 PASS**。测试函数 11 个（`test_p1_01/03/04/05/08/09/10/11_14/12/13/15`，其中 `p1_11_14` 合并覆盖 11/14 两场景），`P1-02/06` 位于 `test_simulation_system.test_conflicting_target_ownership`，`P1-07` 位于 `test_lifecycle_system.test_crash_unknown_delivery`（SIGKILL + TTL）。`-k p1` 实跑 11 passed；其余场景在 169 全绿集合内。
- **P2 四项保持 PASS**：P2-1（`test_error_classification_system`）、P2-2（`test_diagnose_system` 有界许可等待/取消不泄露）、P2-3（`test_metrics_system`/`test_metrics.py` 截断显式 `metrics_truncated`）、修订/预检相关 P2-4 项在仿真/配置系统测试内；三系统文件合跑 15 passed，全部属于 169 全绿。
- **OBS-1 / OBS-2 保持 PASS**：`-k "obs1 or obs2"` 实跑 2 passed（`test_obs1_unknown_quarantine_no_resend_and_recovery_probe`、`test_obs2_native_started_matches_actual_rpc_not_dispatch`）。
- **Gateway 8 RPC 与 Data Generator 15 RPC 契约一致**：`tests/contracts`（92 passed，含 service matrix / message semantics / proto descriptor）全绿；`industrial_gateway.proto` 的 lease/fencing 与 `data_generator.proto` 的描述符与类型桩一致，Import Linter 两条“业务层不得依赖 wire Proto / 不得跨进程包”契约 KEPT。

### 4.3 跨套件收集 ERROR（非确定性，非新增产品缺陷）

四树合跑曾出现 1 次 `test_half_frame_then_early_close_does_not_break_others` 收集/收尾 ERROR，单独运行与整树重跑均通过、稳定复现失败数为 15。判定为 pytest-asyncio 事件循环 / 端口在长套件后的时序型测试隔离抖动，非 Data Generator 确定性回归，记录但不作为阻断。

---

## 5. 合并冲突检查

未在功能分支或 main 上执行真实合并。采用只读 `git merge-tree --write-tree --name-only origin/main <feat>` 检测，并把结果树物化为**临时不可达提交**检出到一次性 worktree 实测（不更新任何分支引用；检查后已 `git worktree remove` 清理，未触碰 `/tmp/dg-main`、`/tmp/dg-mb` 两个历史 worktree）。

- **文本合并冲突：0**（merge-tree 退出码 0，无冲突文件清单）。
- 双方对关键区域的交集：功能分支对 `src/contracts` 仅改 `industrial_gateway.*`，对 `src/core` 仅改 `shared_documents.py`，对 `src/collector / src/sink_service / src/web_server / src/cli` **零改动**；main 的 collector 重写与功能分支改动不相交，自动合并干净。
- 合并结果树实测：
  - mypy **50 errors / 16 files，相对 main 新增 0、减少 17**；
  - 四树回归 **343 passed / 15 failed**（见第 6 节）。

本轮**未实际合并 main**。

---

## 6. 对最新 main 的兼容性影响

在合并结果树（`origin/main` + 含本轮修复的功能分支）上实跑四树：**343 passed / 15 failed**。

- Data Generator 169、Contracts 92 全绿保持；
- Gateway：合并树 65 passed / 14 failed（`test_malformed_length_closes_connection` 此次通过，进一步证明其抖动性）；
- 合并树相对功能分支单独运行**多出的 1 个失败**是
  `tests/core/architecture/test_rpc_boundary.py::test_pb2_only_in_allowed_zones`，报 `collector/assembly.py` 在禁止区域 import pb2。**该测试在 `origin/main` 基线上本就失败（实测 1 failed / 3 passed）**，由 main 的 collector-v1-stage1 引入；功能分支因保留旧 collector 而在独立运行时通过。这是**随 main 继承**的架构违例，Data Generator 既未引入也未恶化。
- 结论：合并对最新 main **无新增静态错误、无新增测试失败**，且净消除 17 个 mypy 错误、把 Gateway 从“整树无法收集”改善为“64–65 passed”。

---

## 7. 静态错误与失败的相关性分类汇总

- **RELATED（本轮引入/直接涉及，已修复）**：G03 格式 1 项（`9f111e65`）；G01-A 配置路径 3 项（`f03740bc`）。
- **UNRELATED（经 main / merge-base / 合并结果树独立实测证明）**：
  - 工业 Gateway `commander.py` 4 个 mypy 错误 + 13 个命令链测试失败（Commander `WritePoint→SubmitCommand` 历史漂移，merge-base 即如此）；
  - fc15 空存储读、malformed 非法帧关闭 2 个 pymodbus/协议栈既有行为（后者非确定性）；
  - mypy 中 web_server / sink_service / cli 共约 42 项（main 既有，含缺 `prompt_toolkit` 可选依赖）；
  - `core/architecture::test_pb2_only_in_allowed_zones`（main collector-v1-stage1 引入，main 上实测失败）。
  - 功能分支独有的 18 个 collector mypy 错误：落后 main 的分叉产物，合并后消失。
- **BLOCKED**：
  - 推送功能分支：HTTPS 凭证不可非交互读取（环境阻断，非代码问题）；
  - Commander 命令链路的正确修复：属 ChatGPT 授权域，本轮不实施。

---

## 8. 尚未解决的缺陷与风险

1. **Gateway 控制面（写命令）当前与权威 Commander Proto 脱节**：默认 `control.enabled=False` 不触发；任何启用 Commander 写控制的部署都会失效。需 ChatGPT 主导迁移到 `SubmitCommand/GetCommandResult`（同步三态/闭锁/恰好一次 → 异步提交 + 结果查询状态机），同步更新生产 `commander.py`、应用 `command_service` 与全部 `tests/industrial_gateway/test_gateway_r2_*` / `tests/support/gateway_harness.py`。
2. Gateway Modbus 协议栈两处 pymodbus 兼容行为：未映射单元空存储应回异常、非法帧应优雅关连接（RST/FIN 时序）。随协议栈工作一并处理。
3. main 自身的 `mypy src` 非零（50 项，集中在 web_server/sink_service/cli/collector 架构与可选依赖）及 `test_pb2_only_in_allowed_zones` 架构违例——独立于本功能，建议 main 侧单独清债。
4. 本地 2 个提交待凭证可用后推送。

以上均不影响 Data Generator v1_10 的功能、租约正确性与契约一致性。

---

## 9. 需 ChatGPT 处理的重大问题

- **Commander Proto 契约迁移**：Gateway 写命令路径从已删除的 `WritePoint` 同步模型迁移到权威的 `SubmitCommand` + `GetCommandResult`；恢复 13 个命令链测试与 `commander.py` 的 4 个类型错误。涉及契约/状态机/并发（在途 UNKNOWN、闭锁、断连、恰好一次），不得在本轮以兼容层或恢复旧 Proto 方式消除。
- **Gateway 与 Commander 职责边界**及上述 pymodbus 协议栈行为的正式处置。
- main 侧 collector 的 pb2 分层架构违例（`test_pb2_only_in_allowed_zones`）归属 main 的 collector 重构，建议其负责人处理。

---

## 10. 最终裁定

# READY WITH LIMITATIONS

**可以合入 Data Generator v1_10；限制项全部为 main 既有、且经实测证明不由本功能引入或恶化。**

依据：

- Data Generator 自身 **169 passed 零退化**，P1（15 场景）、P2（四项）、OBS-1/2 全部 PASS；Gateway 8 RPC / DG 15 RPC 契约一致，Contracts 92、Core Architecture（独立）18 全绿。
- 合并 **0 文本冲突**；合并结果树对 main **mypy 新增 0、净减 17**，测试**无新增失败**，并显著改善 Gateway 可收集性。
- 本轮授权小修全部完成并分别提交；未改冻结设计、未改 Proto 契约、未用 ignore/Any、未 skip/xfail。

限制（不阻断本功能，但不得表述为全仓全绿）：

- `mypy src` 在 main 与本分支均退出 1（历史债 + 可选依赖）；
- Gateway 控制命令面 13 个失败 + 2 个 pymodbus 行为，等待 ChatGPT 的 Commander 迁移；
- 合并后 `test_pb2_only_in_allowed_zones` 仍失败（main collector 既有）；
- 2 个提交因 HTTPS 凭证暂未推送。

**建议**：按仓库生命周期（合并不以测试/CI 为门禁、直接 Squash Merge、不建 PR），凭证恢复并推送后即可执行 `//merge-branch` 将 `feat/data-generator-v1` 合入 `main`；同时为 Commander 迁移与 main collector 分层债分别建立跟踪项。

# 交互诊断 CLI Round 3 独立复测报告（P1 收尾）

- 分支：`feat/interactive-diagnostic-cli`
- 起点：`5245e3b1`（Round 2 报告）
- 复测 HEAD：本报告提交（见仓库历史）
- 架构裁决：**旧 CLI 控制面正式废弃，不恢复、不提供兼容层**
- 日期：2026-10-30
- 结论：**PASS** —— 系统测试不再依赖已删除的 CLI 控制接口，全部控制/查询走 Collector gRPC，真实服务边界与业务断言保持不变

## 1. 调用方清查（P1 收尾范围）

`run_cli/run_cli_async/CliResult` 此前位于 `tests/support/process.py`，固定拼装
`wind-hub --rpc-timeout N collector --target H:P …`。旧调用方全部清查结果：

| 文件 | 旧用法 | 处置 |
|---|---|---|
| `tests/support/process.py` | `run_cli/run_cli_async/CliResult` 定义 | **删除**，无兼容函数；模块 docstring 改为「服务状态与任务控制一律经 gRPC（tests.support.control）」 |
| `tests/support/recovery_helpers.py` | `cli_status`/`cli_instance_states`/`wait_status` 经 CLI subprocess | 改为 gRPC：`status_snapshot(proc)` 与 `wait_status` 走 `GetRuntimeStatus`；`cli_instance_states` 删除，调用方直接用 `control.instance_states(target)` |
| `tests/system/recovery/test_restart_recovery.py` | `info`、`instance <id>`（3 处） | `collector_info()`、`get_task_instance()`，断言 `.collector_id` / `.state == "running"` 等 proto 字段 |
| `tests/system/recovery/test_process_kill.py` | `info` | `collector_info()` |
| `tests/system/recovery/test_reload_failure.py` | `task <id>` | `get_task()`，断言 `HasField("interval")` 且 `interval.value == 0.5` |
| `tests/system/recovery/test_protocol_disconnect.py` | `cli_status` | `status_snapshot` |
| `tests/system/recovery/test_reconnect_reload_race.py` | `cli_status`、`cli_instance_states` | `status_snapshot`、`control.instance_states` |
| `tests/system/recovery/test_resource_stability.py` | `cli_instance_states` + 注释 | `control.instance_states`，注释改述为 gRPC ListTaskInstances |
| `tests/system/recovery/test_endpoint_change.py`、`test_sink_disconnect.py`、`tests/system/network/test_modbus_network_degradation.py` | docstring/注释中的 "CLI status" | 改述为「gRPC Runtime status」 |

清查命令证据：

```text
rg "run_cli|run_cli_async|CliResult|cli_status|cli_instance_states" \
   tests/support tests/system tests/collector   → 0 命中
rg "rpc-timeout|collector --target" tests/support tests/system docs README.md
   → 仅历史报告 docs/cli-round2-acceptance.md 中作为问题记录保留
```

`tests/cli/conftest.py` 中同名的 `run_cli` fixture 是**诊断 CLI 本体**的子进程测试夹具
（验证 wind-hub 诊断命令的退出码与输出），与已删除的控制面包装无关，保留。

## 2. gRPC 控制接口（tests/support/control.py 新增）

全部直连 Collector 进程的 gRPC 服务（`CollectorRuntimeServiceStub`），不经 Server/CLI 中转，
与生产控制路径同一条 RPC：

- `collector_info(target)` → `GetCollectorInfo`（身份、配置代次）
- `runtime_status(target)` / `runtime_status_snapshot(target)` → `GetRuntimeStatus`
  （dict 快照仅含断言使用的标量；gRPC 暂不可用时返回 None，保持轮询语义）
- `get_task(target, task_id)` → `GetTask`（`DoubleValue interval` 直接读 proto）
- `get_task_instance(target, instance_id)` → `GetTaskInstance`
- `instance_states(target)` → `ListTaskInstances` 折叠为 instance_id → state 表

`wait_status` 的 30s 超时、`last status snapshot` 摘要语义原样保留；
**未延长任何测试超时，未修改任何生产代码**。

## 3. 真实服务边界保持

- 业务断言（Redis 最新值推进/冻结、精确点值、实例生命周期状态、FD 单调不泄漏、
  metrics reconnects 计数）全部原样保留，无一处以 Mock 替换真实 Collector/Redis/协议从站。
- Collector 仍以 `wind-hub-collector` console script 真实 subprocess 启动；
  变化仅在「测试如何观测/控制它」：废弃 CLI 子进程 → 同进程 gRPC client。
- README「快速开始」与「无 Server/Admin 的 CTL 操作」整段旧控制命名空间示例删除，
  改为诊断 CLI 用途说明，并注明控制面由 Server/Admin 经 gRPC 下发。

## 4. 质量门

| 门 | 结果 |
|---|---|
| `ruff check src tests scripts` | All checks passed |
| `ruff format --check src tests scripts` | 653 files already formatted |
| `mypy src` | Success: no issues in 312 files |
| `lint-imports` | 11 kept, 0 broken |
| 全树 collect-only | 2048 collected，0 错误 |

新增/修改仅涉及测试 harness、系统测试与 README；无 ignore/降级/排除新增。

## 5. 测试执行

| 范围 | 结果 |
|---|---|
| tests/cli | **125 passed** |
| tests/core + tests/cli | **877 passed** |
| tests/system/recovery（真实 Modbus 从站 + 真实 Collector 子进程 + 真实 Redis 容器） | **18 passed（122.41s）**，**复跑第二次 18 passed（120.18s）** |
| tests/system/network/test_modbus_network_degradation.py | 3 passed，7 skipped（非 root，netem 注入 NOT_RUN） |
| tests/collector | 358 passed，4 failed，3 setup errors（复跑后转 passed） |

### 关键结论：Recovery 18 项全绿（对 Round 2 分类的更正）

Round 2 报告曾将 recovery 16 红灯标记为「WSL2 watchdog 时序 UNRELATED」。
本轮证据表明该分类**不成立，应予更正**：真正根因就是 P1 本身——
`run_cli_async` 拉起的 `wind-hub` 子进程对每个控制/查询命令立即返回
`unknown command`（rc=2），于是 `wait_status`/`cli_instance_states` 等
所有轮询在 30s 窗口内只能得到 None，表现为「watchdog 没等到恢复」的超时报错，
与时序无关。迁移到直接 gRPC 后两轮全量 18/18、约 120s 干净通过，无需延长超时。

Round 2 基线对照被环境污染的机制：基线 worktree（`/tmp/wh-base`，cb380ddb）运行时，
editable 安装的 `.pth` 指向主树 `/home/luosh/wind-hub/src`，子进程执行的
console script `wind-hub` 实际加载的是主树新 CLI（旧已知坑：worktree 须显式
`PYTHONPATH` 覆盖），因此「基线 17 failed」同样是新 CLI 控制面缺失所致，并非基线红灯。

### UNRELATED / 环境项

- `tests/collector/test_watchdog_restore.py` 4 failed：引用已不存在的生产内部
  `_recovery_stopped`/`FakeSubscriptionHandle.stop_calls` 等，属 main 基线漂移
  （该测试文件与相关生产文件本分支零改动，且不 import 本次修改的任何 support 模块）。标记 **UNRELATED**。
- `test_grpc_server::test_sink_check_and_write_test` 等 3 个 setup error：
  复测中误手动 `docker compose up` 与 fixture 自管容器名冲突所致；删除手动容器、
  交由 fixture 管理后复跑 3 passed。属操作瑕疵，非产品缺陷。
- netem 7 项：本机非 root **NOT_RUN**；真机硬件与 soak 同前两轮 NOT_RUN。

## 6. 最终结论

**PASS**：

1. 旧 CLI 控制面在测试侧的全部调用方已清理干净（0 残留），无兼容包装；
2. Collector 状态/实例/任务查询全部迁移到 `tests/support/control.py` 的 gRPC 接口，
   且 recovery 18 项真实服务系统测试两轮全绿，实证真实 Collector gRPC 控制路径可用；
3. 真实服务边界与业务断言不变，未以 Mock 替代，未改生产代码，未延长超时掩盖问题；
4. 静态四门、125 CLI、877 core+cli、2048 collect 全绿。

报告交 ChatGPT 审核合并。未建 PR、未合 main、未 rebase、未 force push。

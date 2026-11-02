# CLI v1 — P1/P2 整改定向复测报告

- 复测对象分支：`feat/cli-v1-stage1`
- ChatGPT 整改基线：`10485514`（3e75fc48 SIGINT 处理器恢复、4f12247a containerd FIFO fail-closed、10485514 Gateway 模糊 ACK 归 UNKNOWN）
- 复测提交：`8da824f5`（已 push；测试 + 授权小修复 + 格式修复；未建 PR、未合并、未 rebase、未 force push）
- 日期：2026-11-02
- 复测人：Claude Code（独立复测）

## 1. HEAD / 工作区

- 复测起始 `git fetch origin` 后基线为 `10485514`；工作区另有未跟踪的 `src/contracts/*_pb2*.py` 生成物（按仓库约定不入库，未纳入提交）。
- 本轮提交 5 个文件：
  - 生产代码 2 个：[src/cli/main.py](../src/cli/main.py)（1 处授权小修复）、[src/cli/plant_commands.py](../src/cli/plant_commands.py)（仅 ruff E501/format 换行，零行为改动）。
  - 测试 3 个：[tests/cli/test_shell_e2e.py](../tests/cli/test_shell_e2e.py)、[tests/cli/test_runtime_e2e.py](../tests/cli/test_runtime_e2e.py)、[tests/cli/test_gateway_e2e.py](../tests/cli/test_gateway_e2e.py)。
- 临时探针 `tests/cli/probe_z_sigint.py` 已删除，未入库。

## 2. 静态门禁结果

| 门禁 | 命令 | 结果 |
| --- | --- | --- |
| Ruff lint | `ruff check src/cli tests/cli` | PASS（All checks passed） |
| Ruff format | `ruff format --check src/cli tests/cli` | PASS（43 files） |
| mypy | `mypy --no-incremental src/cli` | PASS（Success: no issues found in 18 source files） |
| import 契约 | `lint-imports` | PASS（11 kept, 0 broken） |

ChatGPT 在 10485514 中引入的两条 UNKNOWN 诊断串超 E501 行宽，已用纯换行方式修复，未改变任何判定逻辑。

## 3. 定向回归计数（仅三个指定文件）

| 文件 | 环境 | 计数 |
| --- | --- | --- |
| tests/cli/test_shell_e2e.py | 真实 PTY + 真实 CLI 子进程 + 真实 grpc.aio + 真实 mTLS | **4 PASS / 0 FAIL / 0 NOT_RUN** |
| tests/cli/test_runtime_e2e.py | 真实 containerd v1.7.25（dind `wh-ctr`，unix /tmp/wh-run/containerd.sock，busybox:1.36）+ 真实 systemd --user | **15 PASS / 0 FAIL / 0 NOT_RUN / 0 xfail** |
| tests/cli/test_gateway_e2e.py | 真实 CLI 子进程 + 真实 grpc.aio + 真实 mTLS | **11 PASS / 0 FAIL / 0 NOT_RUN** |
| 三文件合计 | — | **30 PASS / 0 FAIL** |

稳定性加测：SIGINT 用例连续 15 次整文件复跑全绿；三文件合并复跑首轮在既有的 `test_shell_history_navigation_and_redaction`（本轮未修改用例）出现 1 次负载相关 PTY/CPR 时序失败，单独复跑及随后整文件 5 连跑、合并复跑 1 次均全绿，判定为既有测试基础设施抖动（与本轮整改无关，UNRELATED）。未重复此前已通过的其他 156 项 CLI 测试（本轮改动不涉及）。

## 4. P1-SHELL-01 证据（真实 PTY + 真实在途慢 RPC）

strict xfail **已解除并转为正向用例** `test_shell_ctrl_c_cancels_inflight_rpc_and_continues`，八项要求逐条实证：

1. Shell 正常启动出现提示符：PASS。
2. 发起真实在途 RPC（服务端处理协程 `await asyncio.sleep(10)`，记账 `start-N`）：PASS。
3. RPC 未完成时向会话主进程发 SIGINT：PASS。
4. 输出 `"status": "INTERRUPTED"` 信封、中断退出码 130（one-shot 路径同样映射）：PASS。
5. **服务端处理协程真实捕获 CancelledError 并记账 `cancelled-N`（不是跑到 DEADLINE_EXCEEDED）**：测试在中断后轮询等待服务端事件，PASS。
6. Shell 不退出、继续下一条：连续两次中断后 `net ping 127.0.0.1` 返回 SUCCESS，PASS。
7. 无重复 RPC / 无后台僵尸任务 / 无失效 Channel：`len(server.calls) == before+1` 两次均严格成立；第三次 RPC（net ping 为本地命令，事件序列已证明第二次 RPC 正常）与最终 `exit` rc=0 证明会话与通道健康；输出中无 `Task exception was never retrieved`、`Unhandled exception in event loop`、`Press ENTER to continue`：PASS。
8. 连续中断两次仍正确：事件序列严格等于 `["start-1","cancelled-1","start-2","cancelled-2"]`，PASS。

**本轮修复的生产缺陷（授权小修复，src/cli/main.py）**：ChatGPT 的机制（每次 prompt 返回后、分派阻塞 Typer 命令前重装 asyncio SIGINT handler → 取消跨线程 `concurrent.futures.Future`）能正确取消在途 RPC，但跨线程取消把 `typer.Exit(130)` 链式带回事件循环 Task，产生“Task exception was never retrieved”孤儿异常并触发 prompt_toolkit 的 “Unhandled exception in event loop / Press ENTER to continue”。修复：Shell 模式下中断后只由工作线程抛 `Exit(130)`，事件循环 Task 不再二次抛 Exit（`cancelled_in_shell` 守卫，6 行）。修复前探针可稳定复现孤儿异常，修复后 15 连跑无复现。未加长任何超时、未改断言、未 Mock 取消。

## 5. P1-RUNTIME-01 证据（真实 containerd v1.7.25）

**strict xfail 已解除**，改写为 `test_s03_restart_real_fifo_task_refuses_before_delete`（fail-closed 正向验收）：

- 真实 FIFO 任务（`ctr run -d --fifo-dir /tmp/wh-run/e2e-fifo/<cid>`）记录 RUNNING 与 init PID。
- CLI `service restart rt --yes` 返回 **rc=1 / FAILURE**，消息含 `managed relay` 与 `refusing to delete the existing task`。
- 拒绝后实测：任务状态仍 **RUNNING**、`ctr tasks list` 的 init **PID 与拒绝前完全一致**——证明 SIGTERM/Kill、Tasks/Delete、Delete 后 Create 阻塞、自动重试均未发生。
- 失败信封不携带任何 restart 阶段记账（preflight 即拒绝，stop 未执行）。
- 代码顺序审查：编排层 [containerd_client.py:145-162](../src/cli/containerd_client.py#L145-L162) 先以 `preview_only=True` 跑预检，通过后才发 Kill；守卫 [containerd_restart.py:251-255](../src/cli/containerd_restart.py#L251-L255) 在 [Tasks/Delete（:272）](../src/cli/containerd_restart.py#L272) 之前抛错。破坏性缺陷（Delete 后卡死导致任务被删且不重建）认定 **已修复**。

**裁定边界（不冒充完整能力）**：本项仅验收“安全拒绝 + 任务保留”。CLI 没有受管 stdio relay，**FIFO 任务的完整 restart 能力仍未实现**；若产品设计要求 FIFO 任务必须可 restart，该项仍是功能缺口（见第 7 节 OPEN-1），不视为完整 restart PASS。

回归项：

- `test_s01_restart_null_io_confirmed`：null-io 任务完整 preflight→stop→delete_create_start→running 四阶段、新 PID、RUNNING 确认，真实 containerd PASS。
- `test_s03_restart_missing_fifo_fails_preflight`：原始 FIFO 路径消失 → FAILURE/1 且任务仍 RUNNING，PASS。
- 该文件 15 项全部 PASS（含 systemd --user 用例），无 xfail、无 skip。

## 6. Gateway P2 证据（UNKNOWN/3、不重发、信封完整）

| 场景 | 退出码/状态 | 单次调用证据 |
| --- | --- | --- |
| 全成功（闭环用例部分成功点） | 0 / SUCCESS（部分成功 4 / PARTIAL） | 请求字段真实（sint64/double oneof） |
| 全部失败 | 1 / FAILURE | — |
| 结果数量不一致 | **3 / UNKNOWN**，消息含 `UNKNOWN` 与 `do not retry` | `server.calls` 中 UpdateVariables 路径严格只有 **1** 条 |
| 结果 ID 错序/不匹配 | **3 / UNKNOWN**，同上 | 严格只有 **1** 条 |
| 在途超时（新增真实用例，handler sleep 30s，本地预算 1s） | **3 / UNKNOWN** | 严格只有 **1** 条（服务端真实收到，未触发任何重试） |
| 端点不可达（localhost:1） | 3，error_code ∈ {UNAVAILABLE, DEADLINE_EXCEEDED} | 连接不可能到达服务端；CLI RPC 路径无任何重试代码/grpc service_config |
| 结果不明后重发 | 代码路径仅一次 `invoke()`，无重试循环；Python grpc.aio 默认不启用透明重试（全仓 grep 无 retry/service_config 配置） | 计数断言钉死 |

10→11 项全 PASS（新增在途超时用例）。所有失败路径信封均为完整 JSON（status/error_code/message），无 Traceback 泄漏。

## 7. 独立审查七点结论与未关闭缺陷

1. **prompt_toolkit 每次提示覆盖 SIGINT 处理器**：确认 prompt_toolkit 在提示期间装自己的处理器；ChatGPT 在每次 `prompt_async` 返回后、`to_thread` 分派前重装前台 RPC handler（main.py:423-426），提示期 KeyboardInterrupt 走既有 `continue`。闭环。
2. **`_active_rpc` 赋值/取消/清理竞态**：Shell 命令串行分派（worker 阻塞在 `future.result()`），赋值→等待→finally 清空无线程交错；handler 在事件循环线程调用并发 Future.cancel（线程安全）。未发现缺陷。
3. **取消后残留后台 RPC/孤儿任务**：本轮发现并修复 1 个生产缺陷（孤儿 typer.Exit，见第 4 节）；修复后服务端真实取消、单次分派、后续 RPC 与 Channel 正常、无残留任务。
4. **FIFO 拒绝是否先于全部破坏性操作**：代码顺序 + 真实 PID 不变双重证据，闭环（见第 5 节）。
5. **null-io restart 回归**：真实 containerd PASS。
6. **Gateway UNKNOWN 误重试**：无重试实现、无 grpc 重试策略，三个模糊结果场景各有 1 次线上调用计数，闭环。
7. **架构级问题（只记录，不改）**：
   - **OPEN-1（P2，功能缺口，非缺陷）**：FIFO stdio 任务没有受管 relay，完整 restart 不被支持，目前按设计 fail-closed。产品若要求 FIFO restart 保真，需单独立项实现 relay。
   - **OPEN-2（P3）**：纯本地阻塞命令（如 `net scan`/`net industrial`）在途按 Ctrl+C 时无 `_active_rpc` 可取消，handler 为空操作，命令会跑到完成；不影响 Shell 存活。
   - **OPEN-3（测试基础设施，UNRELATED）**：高负载下 PTY/CPR 重绘可能让既有 history 用例的字面读取偶发抖动（本轮 1 次，复跑全绿）；与整改无关。
   - 真机 PLC/TwinCAT、netem 全栈、长稳 soak：本轮范围内 NOT_RUN（环境不具备/不在三文件范围），沿用最终验收报告的条件项，不冒充 PASS。

## 8. 最终裁定

- **P1-SHELL-01：PASS**（strict xfail 已关闭；八项要求真实链路逐条实证；另修复 1 个连带生产缺陷）。
- **P1-RUNTIME-01（破坏性缺陷维度）：PASS**——安全拒绝严格先于 Tasks/Delete，真实任务 RUNNING/PID 不变，无 Delete 后阻塞、无自动重试；null-io 完整 restart 回归 PASS；strict xfail 已关闭。**FIFO 完整 restart 能力明确不予认证**（OPEN-1，待产品裁定是否为需求）。
- **Gateway P2：PASS**——数量/ID 不一致、在途超时、断连全部 UNKNOWN/3，真实调用计数证明仅发一次，信封完整。
- 四门静态门禁全绿；30/30 定向用例 PASS；未做 PR、合并、rebase、force push。
- 整体：**本轮 ChatGPT 三项整改 PASS WITH LIMITATIONS**，限制项为 OPEN-1/2/3 与既有 NOT_RUN 硬件/网络条件项，均不阻断本轮整改验收。

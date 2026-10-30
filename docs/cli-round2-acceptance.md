# 交互诊断 CLI Round 2 独立复测报告

- 分支：`feat/interactive-diagnostic-cli`
- 复测起点（ChatGPT 生产提交）：`f4bcaf8f`
- 复测提交：`ce706866`（静态门禁修复）、`9bf09c53`（R2 回归测试）
- 基线：`cb380ddb`（本分支与 origin/main 的 merge-base；origin/main 已前进至 `e25294aa`）
- 日期：2026-10-30
- 结论：**FAIL**（4 项 P2 全部关闭且静态/单测/E2E 全绿；但存在 1 项 P1 生产契约回归，需 ChatGPT 修复后复判）

## 1. Git 整合

- Round 1 三个提交（`1a2059e2`、`c609b0a5`、`236bd5df`）与 ChatGPT 五个修复提交（`3ee26b63`…`f4bcaf8f`）全部保留在分支历史中，未 rebase、未 reset、未 force push。
- 本地与远程起点一致（`f4bcaf8f`），无需整合。
- 复测新增提交仅含静态修复（src/cli 3 文件 9 行）与测试（tests/cli 6 文件），未改任何生产业务行为。
- 未建 PR、未合 main。
- 遗留包 `src/sink_service/`（git-ignored，NATS 轮次遗留）在本分支与基线均不被 collector/core 动态发现（无 `walk_packages`/entry-point 加载，rg 零生产引用），不影响本次结论。

## 2. Docker 环境清理

经用户明确授权，复测开始前删除了全部 Docker 容器（含运行中与已停止）；未删除任何 volume、镜像或宿主机数据，未使用 `docker system prune --volumes`。复测过程未再因遗留容器/端口占用跳过任何测试；当前 `docker ps` 为空（real_service fixture 外部托管的 Redis 已在取证后关闭）。

## 3. 四项 P2 复测结果

全部 **PASS**，并以 16 个新测试钉死契约（tests/cli 由 109 增至 125）。

### P2-1 网络参数错误不得杀死 shell — PASS

- `diagnose tcp/ping/resolve/route` 缺 host、未知参数、非法端口均抛 `ValueError`（SafeArgumentParser.error，single-shot rc=2，table 走 stderr、json 输出结构化错误），不再 `SystemExit`。
- 交互 shell `default()` 捕获后输出 `FAIL … CLI_ERROR`，shell 存活，错误后下一条命令正常执行。
- 旧的"已知缺口"钉死测试已翻转为正向契约。
- 静态修复：`SafeArgumentParser.error` 标注 `typing.NoReturn`（与 argparse 超类 `Never` 兼容，mypy 0 错）。

### P2-2 Sink 检查完整性 — PASS

- 出站白名单 redis/kafka/tdengine/grpc 全部生成 `sink_endpoint` 步并实探（hermetic 替身记录到 `(127.0.0.1, 6379/6041/50073/…)`）；Modbus 入站 sink 生成 `sink_not_tested` → UNKNOWN，不发起 TCP 探测。
- disabled sink 排除；全 disabled 时无 sink 步且整体 PASS；混合配置整体 UNKNOWN 且只探出站端点。
- 未识别类型（nats `future_bus`）在配置加载阶段 `ConfigError` fail-closed。
- scope 明示 "outbound Sink TCP endpoints"，limitations 明示 "TCP availability does not verify data delivery"，无误报 PASS、无静默遗漏。
- 同步修复测试假绿：conftest 旧 fake_drivers 让所有 TCP 探针恒成功且依赖宿主 6379，现为 hermetic 替身并记录端点。

### P2-3 help 与解析一致 — PASS

- 分层 help 与实际 parser 词汇逐一核对；`--scope all` 与 `--scope=all` 在 single-shot 与 dispatch 双路径等价；非法 scope、多余参数拒绝。
- `--config/--format/--timeout` 接受任意前置顺序（4 种次序实证）；重复选项、缺值、值为 `--x`、未识别标志全部 rc=2。
- `gateways [ID]`：单 ID 透传 `gateway_id`，2 个参数 ValueError。

### P2-4 cli.client 残留 — PASS

- 生产代码、测试、pyproject 注释中 `cli.client` 引用零命中；新测试钉死 `importlib.util.find_spec("cli.client") is None`。

## 4. 质量门

| 门 | 命令 | 结果 |
|---|---|---|
| Ruff lint | `ruff check src tests scripts` | All checks passed |
| Ruff format | `ruff format --check src tests scripts` | 653 files already formatted |
| Mypy | `mypy src` | Success: no issues in 312 files |
| Import 契约 | `lint-imports` | 11 kept, 0 broken |

未新增任何 ignore/降级/排除。`do_EOF # noqa: N802` 经审查保留：`cmd.Cmd` 强制方法名，豁免正当。

## 5. 测试

| 范围 | 结果 |
|---|---|
| tests/cli | **125 passed**（基线 109 + 本轮 16） |
| tests/core + tests/cli | **877 passed** |
| 全树 collect-only | **2048 collected**，0 错误 |
| 真实 Redis（compose 6379） | Sink TCP 探针 2/2 PASS（真实服务，非 mock） |
| tests/system/recovery（18 项） | 分支 16 failed / 2 passed（462.69s）；基线 `cb380ddb` 同机同环境 17 failed / 1 passed（559.47s）——见 §8，全部 UNRELATED |

链路形态区分：CLI 单元/契约测试为纯 hermetic（fake driver + TCP 替身）；CLI E2E 为**真实 Modbus 从站 + 真实 collector 进程 + 真实 Redis**（§6）；硬件真机、netem 全栈、长稳 soak 本轮 NOT_RUN（本机 WSL2 无真实 PLC/RTU，属任务书允许项）。

## 6. 完整 CLI E2E（真实 YAML + 真实协议服务）

证据留存 `/tmp/cli-e2e/`（scenario_a/b1/b2a/b2b/c、out1.json/out2.json、site-good*、build_sites.py、run_modbus.py）：

1. `config validate/read` 真实 YAML → PASS 且指纹与逐文件 SHA-256 手算一致；
2. `devices/points/sinks` 清单与配置一致，凭证不泄露；
3. 后台真实 `ModbusMockServer`（127.0.0.1:15020，unit 1，float32 big-endian）+ 真实 `wind-hub-collector` 子进程；
4. `diagnose` 全链路：point_read 读到**精确值 1200.5 与 25**（非仅 TCP 连通）；
5. 故障停止从站后 diagnose fail-fast，FAIL 证据含端点；
6. 手动诊断、坏配置（float999）reload 失败：旧快照保留、`last_reload=FAIL`、`devices` 仍可用旧快照、`diagnose` 被阻断（"last config reload failed"）；恢复合法配置后 reload PASS、门禁解除；
7. 指纹变化（site-good→site-good-v2 端口 15020→15021）触发整表重诊，3/3 步重新执行；`diagnose result` 与 `diagnose report out.json`（2437 字节）指纹与运行一致；
8. 断点恢复（resume）指纹/失败索引/步骤哈希校验与 `--restart` 重跑路径实证；
9. 交互 shell 内网络参数错误不杀进程（rc0 留在 shell），single-shot rc=2；
10. collector 优雅停机、订阅句柄与后台任务回收在 E2E 中随 SIGTERM 干净退出验证（对应 system 级资源稳定性用例受 §8 环境问题影响未全绿）。

## 7. 阻断项（P1，需 ChatGPT 修复）

**P1：CLI 控制面命名空间整体缺失，生产运维契约断裂。**

新交互 CLI 删除了 `src/cli/client.py`（340 行）且未提供等价入口：

```text
$ wind-hub --rpc-timeout 5 collector --target 127.0.0.1:9 info
FAIL ValueError: unknown command: --rpc-timeout. Type help.   (rc=2)
$ wind-hub collector --target 127.0.0.1:9 info
FAIL ValueError: unknown command: collector. Type help.       (rc=2)
```

- `wind-hub --help` 中 `--rpc-timeout` / `collector` 零命中；
- tests/support/process.py 的 `run_cli/run_cli_async`（系统测试唯一的真实 CLI 控制路径，固定注入 `--rpc-timeout … collector --target …`）因此全不可用，直接波及 `test_restart_recovery`（2）、`test_process_kill`（1）、`test_reload_failure`（1）等用例的控制步骤；
- 这是相对 merge-base 的真实能力回退（基线 help/入口存在）。若"诊断 CLI 有意取代运维 CLI"是刻意范围决策，也需 ChatGPT 明确裁定并迁移/改造系统测试 harness，不能让生产入口静默消失。
- 按任务书要求未自行修改生产代码，证据见 `/tmp/cli-e2e/cli-regression{,2}.txt`。

无 P0；无新增 P2（R1 四项 P2 已全部关闭）。

## 8. UNRELATED 基线失败（CLAUDE.md §4）

tests/system/recovery 同机对照（相同 conda 环境、相同测试代码）：

- 基线 `cb380ddb` worktree：**17 failed / 1 passed**（559.47s）
- 本分支：**16 failed / 2 passed**（462.69s）
- 分支 16 个失败**全部是基线 17 个失败的子集**；分支额外转绿 1 个（`test_sigterm_exits_cleanly_after_output`），无任何基线没有的新失败。

失败形态统一为 watchdog 重连时序：故障注入后 30s 轮询窗口内未等到 reconnected（pymodbus 建链退避周期与 WSL2 调度下窗口边际化，日志可见固定 interval 周期的 "watchdog cycle failed / connection refused"）。属当前本机环境/依赖下的既有红灯，标 **UNRELATED**，未扩大范围修复。注：其中 run_cli 三个用例在基线下同为红（被时序问题掩盖），但 §7 的 P1 有独立直接证据，不依赖测试红绿。

## 9. 遗留与 NOT_RUN

- 真实 TwinCAT/PLC、netem 全栈、soak 长稳：本机 NOT_RUN（任务书允许，需硬件与 Linux 命名空间条件）。
- §7 P1 修复后须复判：恢复（或明确替代）控制面入口，并验证 run_cli 系 4 个系统用例在控制步骤上不再因 unknown command 失败（其时序红灯仍按 UNRELATED 单独治理）。
- §8 的 recovery 时序红灯是 main 基线债务，建议另立任务在 CI 同等环境复跑定性。

## 10. 复测提交与结论

- 提交：`ce706866`（style/cli 静态修复）、`9bf09c53`（test/cli R2 契约 16 项），已推送，本地与远程 HEAD 一致。
- 结论：**FAIL** —— 四项 P2 全部以代码+测试+真实链路实证关闭，静态四门、125 CLI/877 core+cli、2048 collect、真实 Modbus+Redis E2E 全绿；但 §7 P1（CLI 控制面 `--rpc-timeout/collector` 整体缺失）为生产契约回退，按验收标准必须先关闭。报告提交 ChatGPT 审核与修复，复测方不自行改生产代码、不合 main。

# CLI v1_0 四阶段集中独立验收报告

- 分支：`feat/cli-v1-stage1`
- 基线 HEAD：`d37bc48a2244e51d81d9e4964bc7490bce477e5a`
- 设计依据：`docs/CLI_软件设计_v1_0.md`
- 验收日期：2026-11-02
- 验收环境：WSL2 Linux 6.6（非特权用户），Python 3.11.16，conda 环境 `wind-hub`
- 最终裁定：**FAIL**（静态门、契约层与本机可执行链路通过；设计第 14 章强制 48 场景 System Test 矩阵大面积未落地，containerd/iSulad 真机验收环境缺失且被设计明令禁止以 Mock 顶替，另有多项 P1/P2 GAP）

---

## 1. 执行证据

| 检查项 | 命令 | 结果 |
|---|---|---|
| 环境 | `python3 scripts/dev.py env` | RESULT: PASS |
| Ruff check | `ruff check src/cli tests/cli` | All checks passed |
| Ruff format | `ruff format --check src/cli tests/cli` | 24 files already formatted |
| Mypy strict | `mypy --no-incremental src/cli` | Success: no issues found in 16 source files |
| import-linter | `lint-imports` | Contracts: 11 kept, 0 broken |
| 契约测试 | `pytest tests/contracts -q` | 87 passed |
| CLI 新测试 | `pytest tests/cli -q` | **74 passed, 2 skipped**（skip 均为 containerd/iSulad NOT_RUN） |
| 跨组件回归 | `pytest tests/core tests/contracts tests/cli --ignore=tests/core/protocol/ads/test_integration.py` | 696 passed, 1 failed, 2 skipped |

唯一回归失败 `tests/core/protocol/ads/test_stability.py::test_real_ads_session_soak` 与收集错误 `tests/core/protocol/ads/test_integration.py` 同源：`scripts/protocol_servers.py:41` 引用 main 基线已删除的 `core.infrastructure.config.YamlConfigAdapter`。本分支 diff 不触及 `src/core`（`git diff --stat main...HEAD -- src/core tests/core` 为空），裁定 **UNRELATED**，按治理规则不扩大任务范围。

### 真实（非 Mock）验收项

- 真实 grpc.aio 服务器（独立线程独立事件循环、真实 TLS 握手与 protobuf 序列化）上的 mTLS Unary RPC、错误码注入、连接复用、快照、轮询、诊断：`tests/cli/test_grpc_integration.py`、`test_diagnose_and_sinks.py`。
- 真实本机 TCP socket 的 OPEN/CLOSED、CIDR 有界扫描：`tests/cli/test_local_tools.py`。
- 真实 systemd D-Bus：`dbus.service` 只读 status → ACTIVE PASS；非特权 start → Polkit `Access denied` 真实拒绝 PASS（`tests/cli/test_system_real.py`）。
- 真实 CLI 子进程（`python -m cli.main` 独立解释器）JSON 信封与退出码 3 PASS。
- 不信任客户端证书 mTLS 握手被真实拒绝 PASS。

### NOT_RUN（明确未执行，不计通过）

- containerd 原生 Tasks/Containers/Snapshots gRPC 9 宫格：本机 `/run/containerd/containerd.sock` 不存在、无 `containerd/ctr` 二进制。
- iSulad 9 宫格：本机无 `isula`。
- systemd 对系统 Unit 的真实 start/stop/restart 状态机：非特权环境仅完成 status 与权限拒绝路径。
- 五服务生产服务端端到端：当前 `src/` 无 `CommonServiceServicer` 生产实现，真实五服务 System E2E 在本分支结构上不可执行，未以测试桩冒充。
- 硬件/netem/soak/性能项：未执行。

## 2. 验收中修复的缺陷（均属授权范围内的小型生产缺陷/测试/格式）

1. **P1：Typer 0.27 vendored Click 类型不一致致命令树枚举与 Shell 异常处理失效。**
   `isinstance(group, click.Group)` 对 `typer.core.TyperGroup` 恒 False：`help` 输出为空、补全词为 0、Shell 未知命令逃逸为 Typer 原始 UsageError。修复为鸭子类型枚举（67 个叶子命令），并在 `_dispatch_typer` 同时兼容独立 click 与 Typer vendored click 的 Exit/ClickException。
2. **P1：单次命令模式下确认门与写入预检裸 `ValueError` 逃逸成 traceback（退出 1），违反统一退出码 2/JSON 信封契约。**
   `safety.confirm_mutation`、`commander write`（`--yes` 缺失、scalar 非法、expires_at 过期）、`config get` 参数校验现在输出结构化错误并以退出码 2 终止；Shell 路径仍回 Shell 不退出。
3. **P2：目录 diff 对两侧均存在但内容不同的敏感文件（`.key/.pem/.p12/.pfx`）脱敏后 unified diff 为空，修改不可见。** 现在输出 `[SENSITIVE FILE REDACTED]` 占位并仍计入 `modified`。
4. 静态门修复：`UniqueKeyLoader.construct_mapping` 父类签名兼容、`ipaddress`/ping 类型、containerd 动态 descriptor 的 mypy closed-enum 处理（仅保留 1 条精确 ignore）；`pyproject.toml` 增加 `dbus_next` mypy override（库无 py.typed，D-Bus 方法运行时生成，正确性以真实 system bus 测试覆盖）。
5. 删除死代码 `src/cli/pipeline.py`、`src/cli/protocol_stream.py` 及引用已删除模块的 7 个旧测试文件；新增本报告第 4 节列出的 74 个新测试。

未自行改动：冻结 Contracts、任何架构/服务端实现、资源生命周期大改。

## 3. 需求—用例—RPC—测试覆盖矩阵（设计 14.2 全 48 场景）

状态：PASS=有可定位且执行通过的测试；PARTIAL=契约/组件级部分覆盖；GAP=无测试；NOT_RUN=有测试但环境缺失或无生产对端。

| 场景 | 主题 | 状态 | 证据 / 缺口 |
|---|---|---|---|
| UC-01-S01 | Shell 启动/退出/提示符 | GAP | 仅组件级 `help`/67 命令枚举（test_command_tree）；无 pty 级 Shell 生命周期 System Test |
| UC-01-S02 | 参数/历史/补全/JSON | PARTIAL | 非法命令/参数退出 2、JSON 信封、SafeHistory 脱敏 PASS；交互补全无 pty 测试 |
| UC-01-S03 | 不可达退出 3/Ctrl+C 回 Shell | PARTIAL | gRPC UNAVAILABLE/DEADLINE→3 PASS；Ctrl+C→130 自动化未实现（设计要求中断后继续执行命令） |
| UC-02-S01 | 三运行时 start/stop/restart | NOT_RUN | systemd status 真实 PASS；containerd/iSulad 环境缺失；系统 Unit 特权启停未执行 |
| UC-02-S02 | 重复操作/延迟就绪分离 | GAP | 代码有 already_running/already_stopped 分支与业务健康轮询，无测试 |
| UC-02-S03 | 权限/不可达/超时/异常退出 | PARTIAL | systemd 真实 Access denied PASS；containerd 预检失败线与超时无真机验证 |
| UC-03-S01 | 五服务身份/健康/能力 | PARTIAL | CommonService/GetServiceInfo 真实 mTLS 链路 PASS；五服务逐一身份字段未覆盖 |
| UC-03-S02 | 五服务输出一致性 | GAP | 无五服务生产对端 |
| UC-03-S03 | mTLS 拒绝/UNIMPLEMENTED/依赖异常 | PASS | 未信任客户端握手拒绝、UNIMPLEMENTED→3、错误码参数化矩阵（test_grpc_integration） |
| UC-04-S01 | Get/Push/Apply 主链 | PARTIAL | Push（expected_revision 自动取 DISK）、Apply 真实 RPC PASS；Get 导出落盘哈希未测 |
| UC-04-S02 | revision 冲突/候选失效 | GAP | 未注入冲突；候选失效应用未测 |
| UC-04-S03 | 部分生效退出 4/断连 | PARTIAL | apply_results 含 FAILED + restart_required → PARTIAL/4 PASS；断连恢复查询未测 |
| UC-05-S01 | 增删改/容量统计 | PASS | test_directory_diff_* 精确断言 added/removed/modified/unchanged |
| UC-05-S02 | 文本/二进制/空目录 | PARTIAL | 二进制跳过文本 diff 为代码既有行为，未单测；空目录未单测 |
| UC-05-S03 | 扫描中变更/权限/敏感 | PARTIAL | 敏感行/敏感文件脱敏 PASS、symlink 拒绝 PASS；CHANGED_DURING_SCAN 与不可读文件 INCOMPLETE 汇总未测（见 P2-1） |
| UC-06-S01 | 设备/测点列表详情 | PASS | ListDevices 真实 protobuf 往返 PASS；Get 详情命令未逐一覆盖 |
| UC-06-S02 | 分页/空列表 | GAP | resolve_point_ids 100 页/token 重复防护有实现无测试 |
| UC-06-S03 | 无权限/对象消失 | GAP | PERMISSION_DENIED→2 已在通用矩阵覆盖；NOT_FOUND 专项未测 |
| UC-07-S01 | 任务启停 + 自动 revision | GAP | 代码路径存在（无 revision 先 GetTask），无 StartTask/StopTask 往返测试 |
| UC-07-S02 | 幂等边界 | GAP | 无测试 |
| UC-07-S03 | revision 冲突无副作用 | GAP | 无测试 |
| UC-08-S01 | latest 一次/watch 多轮不碰 PLC | PARTIAL | watch count=3 三次 ReadPoints、ListPoints 解析 PASS；"不直接读 PLC" 由代码审查保证 |
| UC-08-S02 | 慢响应非重入/过期轮次 | PASS | 0.1s RPC + 0.2s interval 实测 max_inflight=1、总时长≥0.4s |
| UC-08-S03 | 断连重试预算/中断 | PARTIAL | 连续 2 次 UNAVAILABLE 后恢复 PASS、超预算→3 PASS；Ctrl+C 停轮询未自动化 |
| UC-09-S01 | Delivery 查询启停 | GAP | 命令存在，无 SinkService RPC 往返 |
| UC-09-S02 | 双 Sink 进程身份 | PARTIAL | sink→sink_realtime 别名与 sink_reliable 身份隔离 PASS（test_diagnose_and_sinks）；单侧故障不假报未测 |
| UC-09-S03 | 冲突/手动停止不恢复 | GAP | 属服务端语义，无对端 |
| UC-10-S01/S02/S03 | Gateway 变量读/质量/部分失败 | GAP | 命令树存在，零 RPC 覆盖 |
| UC-11-S01/S02/S03 | Data Generator 场景/启停/冲突 | GAP | 命令树存在，零 RPC 覆盖 |
| UC-12-S01/S02 | 五服务单服务诊断/缺证 | PARTIAL | pipeline 用例真实调用两侧 Diagnose 并保留阶段报告；单服务命令与 UNKNOWN/PARTIAL 缺证语义未单测 |
| UC-12-S03 | 阶段错误/权限退出 2 | GAP | 通用 PERMISSION_DENIED→2 已覆盖，诊断专项未测 |
| UC-13-S01 | 关联证据齐全才 PASS | PASS | 共享 message_id + 同一 correlation_id → PASS（test_diagnose_pipeline_*） |
| UC-13-S02 | 缺消息级关联不得 SUCCESS | PASS | 证据无交集 → UNKNOWN/3 + limitations 明写 |
| UC-13-S03 | 跨服务断连/报告缺失阶段 | PARTIAL | 一侧 FAIL→整体 FAIL/1 PASS；断连 UNKNOWN 与报告导出未测 |
| UC-14-S01 | 合法写入/结果查询 | GAP | SubmitCommand 仅在故障注入路径测试；accepted+command_id 主路径与 GetCommandResult 未测 |
| UC-14-S02 | 标量类型/终态边界 | PARTIAL | `_value` 边界有实现审查，非法 scalar 退出 2 间接覆盖；uint64/终态区分未测 |
| UC-14-S03 | UNKNOWN 不重发 | PASS | UNAVAILABLE→3、信封含 "do not resubmit blindly"、服务端仅收到 1 次 SubmitCommand |
| UC-15-S01 | tcp/industrial/scan | PASS | 真实 socket OPEN/CLOSED、预设工业端口标注、CIDR 扫描 bounded 全测 |
| UC-15-S02 | 4096 上限/端口范围/NO_RESPONSE 语义 | PASS | 参数化硬边界全测 |
| UC-15-S03 | 拒绝/超时/取消资源回收 | PARTIAL | CLOSED/TIMEOUT 分离 PASS；ping 子进程取消 kill+wait 有代码无测试 |
| UC-16-S01/S02/S03 | metrics/events/publishing | GAP | 命令存在（含 collector publishing），零往返与窗口/分页测试 |

汇总：**PASS 11 场景，PARTIAL 14 场景，GAP 21 场景，NOT_RUN 2 场景**（GAP 中 UC-03-S02/UC-09/10/11/16 的根因是无生产 CommonService/业务 servicer）。

## 4. 新增测试资产

| 文件 | 内容 |
|---|---|
| `tests/cli/conftest.py` | 临时 CA/服务器/客户端证书（cryptography 真实签发）、独立线程 grpc.aio mTLS 服务器（按六份冻结 proto 描述符注册全部 Unary 方法，行为/错误可注入）、cli.yaml 生成器、CliRunner 与真实子进程两种入口 |
| `test_config_loader.py` | cli.yaml schema、重复 YAML key、rpc_via 互斥/环/悬空、mTLS profile、containerd/iSulad/端口/主机数硬边界（15 用例） |
| `test_local_tools.py` | 真实 TCP 探针、CIDR 边界、目录 diff（增删改/symlink/脱敏/截断）、快照导出（穿越/重复/hash/size/base64/原子落盘/0600）、SafeHistory、确认门（21 用例） |
| `test_command_tree.py` | 67 叶子命令 vs 设计命令集、help、未知命令（单次+Shell 两路径）、退出码 2/3、JSON 信封（8 用例） |
| `test_grpc_integration.py` | mTLS 真实往返、channel 复用与 rpc_via 别名、RPC 白名单、gRPC 错误码→退出码矩阵、Commander UNKNOWN 不重发、watch 非重入/重连预算、快照 Push/Apply PARTIAL、未信任证书拒绝（17 用例） |
| `test_diagnose_and_sinks.py` | pipeline 关联门 PASS/UNKNOWN/FAIL、correlation_id 一致、双 Sink 身份（4 用例） |
| `test_system_real.py` | 真实 systemd status/权限拒绝、真实 CLI 子进程；containerd/iSulad 显式 NOT_RUN skip（5 用例） |

## 5. 遗留问题台账（移交 ChatGPT/后续阶段）

阻断项（本次 FAIL 依据）：

- **B1（P1，流程阻断）**：设计 14.2 表要求 48 个 `UC-xx-S0x` 场景各自有可定位 System Test，当前 21 个 GAP、14 个仅 PARTIAL，且设计指定的 `test_*_e2e.py` 系统级文件一个未建。
- **B2（P1，环境/架构阻断）**：containerd restart 的预检→Stop 确认→Delete/Create/Start→RUNNING 确认→失败恢复为用户点名验收重点，本机无 containerd/iSulad；同时仓库内无 CommonService 生产 servicer，五服务真实 E2E 无对端。需在具备 containerd/iSulad 与真实五进程的环境执行，不得以 mock daemon 替代。

非阻断 P2/P3：

- **P2-1**：`directory_diff` 对不可读文件/扫描中变更直接抛异常，设计 12.1 的 INCOMPLETE/CHANGED_DURING_SCAN 总体状态语义未完整实现。
- **P2-2**：Shell Ctrl+C→取消在途 RPC→退出码 130→回 Shell 继续执行，无自动化证据（需 pty harness）。
- **P2-3**：UC-14 合法写入主路径（accepted/command_id/GetCommandResult）、UC-07 任务启停、UC-09/10/11/16 业务 RPC 零覆盖。
- **P2-4**：`service status` 无参数时任一服务 UNKNOWN 即整体退出 3，是否符合"单服务故障不阻断独立命令"需产品裁定。
- **P2-5**：`config get --output` 完整导出（含原子落盘集成）与候选失效/冲突路径未测。
- **P3-1**：分页 100 页硬上限、重复 page token、ping 取消资源回收、history=false 时 DummyHistory、`watch` 逐点质量/错误展示均有实现无测试。
- **P3-2**：CI 未接 `scripts/generate_rpc.py`（生成物不入库约定下，干净检出需先手工生成）。

## 6. 复核注意

- `src/contracts/*_pb2*.py` / `.pyi` 为生成物，未纳入提交。
- `docs/CLI_软件设计_v1_0.md` 为本分支设计依据（此前 untracked），随本次验收一并纳入版本管理；如不应入库可在合并前摘除。
- 本报告与测试修复均在 `feat/cli-v1-stage1` 分支提交推送；未创建 PR、未合并 main。

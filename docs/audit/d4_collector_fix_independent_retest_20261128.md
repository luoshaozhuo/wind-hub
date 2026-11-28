# wind-hub D4 Collector 生产缺陷修复独立复测报告（2026-11-28）

> 面向 ChatGPT 的 D4 关闭与 D5 准入评审。结论按当前 SHA 的真实运行证据，覆盖三个 Collector 缺陷、Commander 资源生命周期和完整 D4 集中回归。

## 1. Git 基线、环境与最终裁决

| 项目 | 值 |
|---|---|
| 起始/预期远端 HEAD | `bda220788b42a6dbc500dc391475060717249ec6` |
| 最终生产源码 SHA | `c9bbe6e5ddba45d90a83891492d6db76398cd035`（仅在 ChatGPT 修复后追加 Ruff 机械格式化） |
| 最终测试 SHA | `c9bbe6e5ddba45d90a83891492d6db76398cd035` |
| 最终远端 HEAD | 报告提交后以 Git 元数据和交付摘要为准 |
| 分支 | `feat/storage-wide-table-completion-20261125` |

起始本地/远端一致、工作树干净。未创建分支、PR，未合并 main，未 force push；未修改正式错误契约、冻结 Proto、活动配置或容量标准。仓库未找到独立 `报告模板.md`，沿用既有 D4 审计报告结构。

环境：Python 3.11.16、wind-hub Conda、Docker 29.2.1、NATS 2.10、Redis 7、Kafka 3.9.1、TDengine 3.3.6.13、aiokafka 0.14.0、gRPC 1.84.0。

**最终裁决：`D4_PASS + READY_FOR_D5_REVIEW`。三个 Collector 缺陷全部关闭，Commander 资源残留直接探针三轮为零，最终 SHA 的集中回归全部通过。**

## 2. 最终 Gate

| Gate | Result | 证据 |
|---|---|---|
| COL_DIAGNOSTIC_DEADLINE | **PASS** | 连续 3/3；最终精确 NodeID 再 PASS。 |
| COL_OVERDUE_POLL | **PASS** | 连续 3/3；慢响应进入 RUNNING，skipped_slots 正确。 |
| COL_UNSETTLED_IO | **PASS** | 连续 3/3；未结算 I/O 为 TIMEOUT_UNCONFIRMED，Shutdown 安全。 |
| COLLECTOR_FULL_REGRESSION | **PASS** | 全层 **166 passed / 0 failed / 0 xfail**，2 个 D5 deselected。 |
| COMMANDER_RESOURCE_LIFECYCLE | **PASS** | 三轮直接探针 stop 后 5 秒无 NATS 任务残留；联合 168 passed。 |
| STATIC_GATES | **PASS** | Ruff、mypy、import-linter、Proto、4351 项收集。 |
| MODULE_FULL_REGRESSION | **PASS** | 非 Web 模块相关 Unit/Component/Contract/Integration/System 全绿。 |
| FULL_CHAIN_E2E | **PASS** | 最终 SHA 全部 **58/58**，无 skip/xfail。 |
| D4 | **D4_PASS** | 所有必需门禁通过。 |
| D5_ADMISSION | **READY_FOR_D5_REVIEW** | 仅准入评审，不代表 D5 或生产资格通过。 |

## 3. 三个 Collector 缺陷

### 3.1 D4-COL-01：诊断 deadline

连续命令（各 3 次）：

```bash
pytest -q tests/collector/system/test_collector_diagnose_system.py \
  -k test_uc05_s03_expired_deadline_marks_later_stages_not_run -vv
```

结果：R1/R2/R3 均 **1 passed**。日志：`deadline_r1.log`、`deadline_r2.log`、`deadline_r3.log`。

修复后诊断在各阶段入口和健康响应后检查 deadline；逾期阶段不再产生新的 FAIL，也不伪报 PASS。最终按精确 NodeID 与另外两项联合复核为 3/3（`collector_final_targets.log`）。

### 3.2 D4-COL-02：overdue poll

```bash
pytest -q tests/collector/system/test_collector_acquisition_system.py \
  -k test_uc03_s02_overdue_poll_increments_skipped_slots -vv
```

结果：连续 **3/3 PASS**，约 11.4 秒/轮。日志：`overdue_r1.log`～`overdue_r3.log`。

慢速但健康的 I/O 现在区分“调度槽目标”和“协议 I/O 超时”：允许不重叠的完整读取完成，后续槽位按 skipped_slots 记录；过期数据不进入有效实时周期，但健康响应可证明连接已恢复并进入 RUNNING。

### 3.3 D4-COL-03：unsettled I/O

```bash
pytest -q tests/collector/system/test_collector_lifecycle_system.py \
  -k test_uc08_s03_unsettled_io_shutdown_exits_nonzero -vv
```

结果：连续 **3/3 PASS**。日志：`unsettled_r1.log`～`unsettled_r3.log`。

重叠 I/O 被拒绝，未结算操作返回 `TIMEOUT_UNCONFIRMED`，Shutdown 不提前释放未结算连接。未修改正式错误语义或测试断言。

## 4. Collector 全层回归

执行：

```bash
pytest -q tests/collector -m 'not performance and not soak and not hardware' -ra
```

最终测试 SHA 结果：**166 passed、2 deselected、0 failed、0 skipped、0 xfailed**，`collector_full.log`。

覆盖正常/慢速采样、协议超时、周期跳过、重连、诊断截止、主动读取、取消停止、I/O 所有权、退出码、Socket/Task 清理。未通过修改预期行为或新增 skip 消除失败。

## 5. Commander 资源生命周期

### 5.1 联合回归与警告复现

连续三轮：

```bash
PYTHONASYNCIODEBUG=1 pytest -q tests/commander/component tests/commander/system \
  -m 'not performance and not soak and not hardware' -ra
```

结果均为 **168 passed、1 skipped、4 deselected**。Commander 现场窗口 skip 为硬件 NOT_RUN，不计 PASS。日志：`commander_resource_r1.log`、`commander_resource_r2.log`、`commander_resource_r3.log`。

NATS `_wait_for_msgs/_flusher` event-loop closed GC 警告可复现；Modbus `connect` GeneratorExit 警告在完整套件中间歇出现，单独复跑不稳定。未把这些警告直接等同于资源泄漏。

### 5.2 直接生产装配探针

使用原始 `_start_service` 连续启动/停止两个 Commander 代际，检查 `service.stop()` 后 5 秒的全部 NATS task：

- R1：`commander_pending_direct.log`；
- R2：`commander_pending_direct_r2.log`；
- R3：`commander_pending_direct_r3.log`。

三次均为：

```text
stopped=frozenset()
pending_nats_tasks_after_5s=[]
```

另有 `nats_original_baseline.log` 保留原始用例警告。结论：**没有仍运行的 NATS task 或未关闭业务进程，未证实生产资源泄漏。警告属于 nats-py 在事件循环关闭阶段的析构噪声，不阻断 D4。** 若后续出现非空 pending 或连接残留，应重新打开资源门禁，而不是以本报告永久豁免。

曾尝试统一强制取消，发现会干扰受 shield 保护的隔离/关闭流程，已移除，未作为修复提交。生产关闭语义未因此改变。

## 6. 最终 SHA 集中回归

| 范围 | 最终结果 | 日志 |
|---|---|---|
| Contracts + Core | 140 passed | `contracts_core.log` |
| Collector 全层 | 166 passed | `collector_full.log` |
| 其他非 Storage 模块 Unit/Component | 599 passed | `module_unit_component.log` |
| Commander component/system | 168 passed、1 现场 skip | `commander_resource_r*.log` |
| Configuration unit | 282 passed | `configuration_unit.log` |
| Configuration 全层 | 496 passed | `configuration_full.log` |
| 真实 NATS 广播 | 2 passed | `configuration_real_nats.log` |
| Storage unit/component | 297 passed | `storage_unit_component.log` |
| Storage 真实 Integration | 27 passed | `storage_integration_real.log` |
| Sink unit/component | 430 passed | `sink_unit_component.log` |
| Sink 真实 System | 38 passed | `sink_system_final.log` |
| CLI 非 Docker 最终复测 | 293 passed、13 环境 skip | `cli_non_docker_final.log` |
| 严格 CLI 五服务真链 | 1 passed | `cli_strict_real.log` |
| snapshot_max_bytes | 14 passed | `cli_snapshot_limit.log` |
| 八服务真实数据/控制链 | 8 passed | `eight_service_final.log` |
| Full-chain | **58 passed** | `full_chain_final.log/xml` |

CLI 首次套件运行出现一次 PTY 退出超时（`cli_non_docker.log`）；隔离连续 3/3（`cli_shell_r1.log`～`cli_shell_r3.log`），最终全量复测 293/293。分类为时序不稳定，不是稳定生产缺陷。

真实 RAW/PROTOCOL/BUSINESS 均完成 Kafka v3→Storage→TDengine 回读：FC04 字节保真、5 个 PROTOCOL 真值时刻、BUSINESS `sim_active_power` 映射与稳定物理列、Quality/NULL/offset 覆盖、聚合分页导出和 taosdump 恢复。控制链覆盖 APPLIED、UNKNOWN、幂等、权限、lease/fencing，未以 Commander RPC 成功替代 DG 物理步证据。

## 7. 静态门禁与收集

最终 SHA 执行 Ruff check/format、mypy、import-linter、Proto --check 和 `pytest --collect-only -q`：

- Ruff check PASS；1139 文件 format PASS；
- mypy 492 files / 0 errors；
- import-linter 22 kept / 0 broken；
- Proto 无生成漂移；
- **4351 tests collected / 0 error**。

日志：`static.log`、`collect_only.log`。

## 8. NOT_RUN、skip 与 D5 延期

- CLI 13 个环境 skip：containerd 11+1 项、iSulad 1 项，均不计 PASS。
- Commander 1 个真实现场窗口 skip：硬件 NOT_RUN。
- 性能、容量、soak、长稳、真实 PLC/OEM、netem、Storage 原生 opt-in 容器重启探针、八服务容量、Sink capacity/throughput、Web Server/Web Admin/DCEMS 独立专项：**NOT_RUN**。
- 所有最终执行组 **0 xfail、0 failed**。

## 9. 最终结论

D4-COL-01～03 已全部关闭；Collector 全层、Commander 资源生命周期、非 Web 模块、Configuration、Sink、Storage、CLI、八服务和 Full-chain 均在最终测试 SHA 下通过。资源探针没有 NATS 任务残留，GC 警告不再被误当作已证实泄漏。

**最终总体裁决：`D4_PASS + READY_FOR_D5_REVIEW`。D4 功能通过不代表 D5 容量、硬件、长稳或完整生产资格通过。**

# DCEMS 完整测量链路独立复测报告（2026-11-26）

> 报告模板说明：仓库与文件系统中仍未找到《报告模板.md》，本报告按交接任务
> 第 11 节要求的 15 项内容组织，未因模板缺失降低检查范围。

## 1. Git 基线、最终 HEAD 与修改范围

| 项 | 值 |
|---|---|
| Repository | `luoshaozhuo/wind-hub` |
| 开发分支 | `feat/dcems-gateway-smoke-20261126` |
| 预期 / 实测起始 HEAD | `ca3f682b2852a15ab813b3743eb27edec60b310d` |
| fetch 后远端 HEAD | 与起始 HEAD 一致 |
| 上一轮起点 | `48bfe50bbd8a0ddbfe326adfd65af7e62f0e02d2` |
| 本轮生产提交 | `5cbcd0ef`、`f77a401d`、`281f02d5`、`4d44c4cc`、`85909a88`、`a3c96527`、`a1c29b63`、`8589b1ba`、`c7a5596d`、`ca3f682b`（另含早前同主题提交） |
| 本轮整改提交 | 见交付消息（测试/配置/Fixture 与报告） |
| 工作树 / 分支 | 未创建新分支，未合并 main，未 rebase/reset/force push，未创建 PR |

修改范围仅限测试专项：`tests/third_party/dc_ems/`、`tests/third_party/dc-ems/config/`、
`docs/audit/`。未修改 `src/` 生产代码、`third_party/dc-ems/` 生产源码或发布产物。

## 2. 测试环境与中间件

| 组件 | 实测 |
|---|---|
| OS | WSL2，`6.18.33.2-microsoft-standard-WSL2` |
| Python | 3.14.7，`~/miniconda3/envs/wind-hub`；`scripts/dev.py env` = PASS |
| pytest / ruff / mypy | 8.4.2 / 0.8.6 / 1.20.2 |
| pymodbus / nats-py / grpc | 3.15.0 / 2.16.0 / 1.84.0 |
| Docker | 29.8.0（Docker Desktop 引擎），Compose v5.5.1 |
| NATS | `nats:2.10-alpine`，nats-server v2.10.29，独立容器 `dcems-measurement-nats` |
| 隔离端口 | NATS `127.0.0.1:14222`（监控 18222）；未复用 4222 或任何生产 NATS |
| 运行根 | `run/dcems-measurement/`（Git 忽略，令牌 0600，未进入证据） |

五个业务服务全部由正式 CLI（`python -m cli.main`）的 process manager 启动，
没有使用 Mock 或内存对象替代 Collector/Sink/Gateway。

## 3. 静态门禁

| 检查 | 命令摘要 | 退出码 | 结果 |
|---|---|---:|---|
| Ruff check（专项） | `ruff check tests/third_party/dc_ems` | 0 | All checks passed |
| Ruff format（专项） | `ruff format --check tests/third_party/dc_ems` | 0 | 24 files already formatted |
| mypy（专项） | `mypy tests/third_party/dc_ems` | 0 | 24 files，0 error |
| pytest 收集 | `pytest --collect-only -q tests/third_party/dc_ems` | 0 | 82 tests |
| 全部非实时测试 | `pytest -q ... --ignore=test_live_smoke.py --ignore=test_full_chain_live.py` | 0 | **33 passed** |
| 完整链路配置测试 | `pytest -q test_full_chain_config.py` | 0 | 4 passed |
| Gateway 官方 Loader | `--check` 内 `load_gateway` ×2 | 0 | 各 10 servers |
| DG 官方 Loader | `load_settings` / `--check` | 0 | 10 设备 + 2 场景 |
| Collector 官方 Loader | `validate_collector_snapshot` | 0 | configuration validated |
| Sink 配置快照 | `read_snapshot` + 逐点映射断言 | 0 | 10 delivery / 10 sink |
| Deployment 完整依赖 | `parse_deployment` + `select_local_service` | 0 | 5 services 闭包 |
| Bash 语法 | `bash -n run_full_chain.sh run_windhub.sh` | 0 | 通过 |
| Git whitespace | `git diff --check` | 0 | 无错误 |
| 仓库门禁 Ruff | `ruff check src tests scripts` | 1 | **5 个既有错误 / 3 个既有文件，UNRELATED** |
| 仓库门禁 format | `ruff format --check src tests scripts` | 1 | **7 个既有文件，UNRELATED** |
| 仓库门禁 mypy | `mypy src` | 0 | 483 files，0 error |

既有失败文件与本轮修改无交集：`src/storage_service/.../kafka_consumer.py`、
`.../tdengine_backend.py`、`tests/storage_service/integration/test_tdengine_wide_probe.py`、
`scripts/audit_business_points.py`、`src/configuration_service/.../schema_catalog.py`、
`src/sink_service/infrastructure/config_io.py`、`.../tdengine_schema.py`。
未扩大修改范围。

本轮引入并已修复的静态问题：

- `check_full_chain.py` 导入排序与格式；
- `prepare_full_chain.py` `_read` 返回 `Any`；
- `test_full_chain_live.py` 变量遮蔽、`STATION_POINTS` 隐式导出、`contracts` 类型缺口；
- 4 个新文件不符合 `ruff format`。

证据：`evidence/final-static-suite.log`、`evidence/repo-gate.log`、
`evidence/mypy-src.log`、`evidence/nonlive-full.log`、`evidence/bash-git.log`。

## 4. Deployment 与五服务配置验证

`run_full_chain.sh --prepare / --check / --up / --down` 均可重复执行
（`prepare` 拒绝覆盖既有根，`down` 幂等）。

| 服务 | CLI 名 | gRPC/管理端口 | 进程目标 | 认证 |
|---|---|---|---|---|
| Gateway2（源） | gateway | 52073 + Modbus 15221–15230 | industrial_gateway.main | generator/operator |
| Gateway1（目标） | gateway-target | 52074 + Sink 注入 52075 + Modbus 15121–15130 | industrial_gateway.main | operator(read/lifecycle/diagnose)、sink(inject) |
| Collector | collector | 52051 | collector.main | `WIND_HUB_COLLECTOR_ADMIN_TOKEN` |
| Sink Main | sink | 52053 | sink_service.main | CLI 最小授权 query/control/config |
| Data Generator | data-generator | 52061 | data_generator.main | CLI + Gateway generator |

关键配置修正（本轮修复，均为测试配置/启动脚本）：

1. **Collector 模板版本冲突**：e2e 样板带入 `business_points_standard_v1.5.0`，
   与点表引用的 1.6.0 并存，生产 Loader 要求模板根只有一个选中版本。
   派生时删除废弃 1.5.0，并增加回归断言。
2. **Collector 稀疏点表读取失败**：默认 `read_merge_max_gap=8` 会把
   机侧地址空洞（2–5）合并成一次读取，Gateway 按 Modbus 协议返回
   IllegalDataAddress，WTG02–10 全部 `PARTIAL`。设备实例显式设置
   `protocol_options.read_merge_max_gap=0`，只读取点表实际地址后恢复
   `RUNNING`。这是使用生产支持的配置项，不修改生产代码。
3. **CLI 服务身份**：Collector 管理命令固定指向服务名 `collector`，
   原 `collector-realtime` 导致 “service not configured”。改名并同步
   `--down`、静态断言与 README。
4. **CLI 启动瞬时健康**：`service start` 在进程已 RUNNING 但健康
   瞬时 DEGRADED 时返回非零，`set -e` 中断后续服务。启动助手只接受
   真实 RUNNING 进程并重试，业务就绪仍由独立查询判定。
5. **身份隔离场景缺失**：新增 `dcems-identity-ten`（每台不同常量），
   用于显式防串台；静态与运行时均校验。

## 5. 双 Gateway 独立性验证

| 检查 | 结果 |
|---|---|
| 源/目标 Modbus 端口 | 15221–15230 vs 15121–15130，完全不相交（运行时 20 个 listener） |
| gRPC 管理面 | 源 52073、目标 52074、目标 Sink 注入 52075，独立进程 |
| 设备实例与分组 | `dcems-source` / `dcems-target` 两组，设备 ID 相同但实例独立 |
| 变量存储 | 两个独立 Gateway 进程、独立配置根；无共享内存/状态目录 |
| 反向采集 | Collector 只配置 `dcems-source`；运行时 `collector devices` 的
  endpoint 集合 = 15221–15230，且断言不含 15121–15130 |
| 目标端写入来源 | 目标 Gateway 仅有 Sink `inject` 调用方；源 Gateway 无
  `sink_listener_port`，不接收 Sink 回写 |

判定：**PASS**（静态闭包 + 运行时端口/服务身份证据）。

## 6. Collector 采集与 NATS 发布验证

每轮业务就绪检查（正式 CLI）：

```text
collector task dcems_source_all → RUNNING, last_error=""
collector devices              → 10 台，endpoint 15221..15230，actual_state=RUNNING
collector publishing           → realtime: nats_connected=true, failed=0
```

NATS 为独立容器，Collector 与 Sink 使用同一 `nats://127.0.0.1:14222`。
发布量随运行持续增长（首轮就绪 410，故障注入前 3780，恢复后 5120，
NATS 重启后 6661），`failed=0`、`backpressure=0`。

跨服务因果证据使用生产 `diagnose pipeline`：仅当 Collector 与 Sink
共享同一已验证 `sample_id` 时才返回 PASS。三轮 × 10 台全部 PASS，
排除了“仅数值集合有交集”的偶然性。

## 7. Sink 十条 Delivery

| 检查 | 结果 |
|---|---|
| Delivery 数量 | 10（`dcems_wtg01..dcems_wtg10`） |
| 每条源设备 | 仅一台，`source.devices=[WTGxx]` |
| 目标 gateway_id | `dcems-target-01..10`，逐条唯一 |
| target_mappings | 覆盖该设备全部测点；WTG01 12 点，其余 9 点 |
| 跨设备串写 | 静态映射断言 + 运行时身份场景，均无 |
| Delivery 状态 | 三轮均 10/10 `RUNNING`，`last_error` 为空 |
| 静态测点写入 | 目标端工作模式/限值/转速/桨距/故障均与源端一致 |

## 8. 十机完整数据对账

每轮随机场景执行（真实 Modbus TCP 读取源/目标，45 轮 × 0.25s）：

| 设备 | 风速 m/s 源→目标 | 有功 W 源→目标 | 无功 var 源→目标 |
|---|---|---|---|
| WTG01 | 4.16–17.24 ↔ 4.16–17.24 | 222773–964068 ↔ 同区间 | -99399–96854 ↔ 同区间 |
| WTG02 | 4.85–17.96 ↔ 4.85–17.96 | 233196–964151 ↔ 同区间 | -99772–87238 ↔ 同区间 |
| WTG03–09 | 各自动态区间 | 各自动态区间 | 各自动态区间 |
| WTG10 | 4.11–16.72 ↔ 4.11–17.85 | 302649–996075 ↔ 229773–996075 | -95676–99931 ↔ 同区间 |

- 目标端每台每类动态量至少 2 个不同值（非冻结）；
- 源/目标 1 位小数集合存在真实对应；异步链路允许传播延迟；
- 因果性由第 6 节的共享 sample ID 证据补足；
- 失败集合为空（`source-target-reconciliation.json`）。

静态测点（源与目标逐台读取）：

| 测点 | 结果 |
|---|---|
| `sim_running_status` | 32 |
| `active_power_limit` | 2000.0 kW |
| `generator_speed` | 1500.0 rpm |
| `blade_1_pitch_angle` / `pitch_angle` | 5.0° / 0.0° |
| `device_fault_active` | false |

## 9. WTG01 站控 P/Q 与 PFR 模式

| 项 | 源端 | 目标端 | 结果 |
|---|---|---|---|
| 站控有功 W | 439929–1088283，动态 | 同区间，动态 | PASS |
| 站控无功 var | 动态区间 | 同区间，动态 | PASS |
| PFR 模式 FC02 | 45/45 false | 45/45 false | PASS |
| 站控点归属 | 仅 WTG01 点表包含 | 仅 WTG01 目标可读 | PASS |

站控 P/Q 是独立随机激励，不是十台功率物理求和，本轮不宣称物理模型正确。

## 10. 防串台与设备身份隔离

使用独立常量场景 `dcems-identity-ten`（停止随机场景后切换）：

| 设备 | 风速常量 | 有功常量 W |
|---|---:|---:|
| WTG01 | 7.0 | 350000 |
| WTG02 | 8.0 | 400000 |
| … | … | … |
| WTG10 | 16.0 | 800000 |

逐台 3 次采样（源与目标）全部精确等于本机常量（容差 ≤1.0 W）；
10 台常量两两不同，目标端出现任何其他设备常量都会直接失败。
同时断言 WTG02–10 的地址 2/4（站控）与 FC02 地址 0 在源/目标端均
不可读，证明 WTG01 站控/PFR 未泄漏到其他风机。

三轮均 **11 passed**（10 台身份 + 隔离用例）。未使用“随机分布不同”
作为串台判据。

## 11. 故障注入及恢复

| 编号 | 注入 | 观测 | 恢复 | 结论 |
|---|---|---|---|---|
| F1 | 停止 Gateway2（源） | Collector Task `FAULTED`；`diagnose` ACQUISITION “cached samples are stale”；发布停止增长（3940 保持 12s）；目标风速 15.01 冻结 | 重启 Gateway 后 Task `RUNNING`、发布恢复；DG 因租约丢失安全停止，需重新 `data-generator start` 后数据恢复 | PASS（恢复需显式重启生成） |
| F2 | 停止 Gateway1（目标） | 10 条 Delivery `FAILED` + 真实 RPC 错误；Sink 未报告虚假成功；Collector 发布正常 | 重启后 10 条自动恢复 `RUNNING`，目标数值恢复动态 | PASS |
| F3 | 停止 NATS | Collector `nats_connected=false`，`last_error=nats: no servers available`；Delivery `DEGRADED`；目标 14.4 冻结 | 重启 NATS 后连接恢复、发布继续（6661）、Delivery 自动 `RUNNING` | PASS |
| F4 | 错误 Bearer 访问 Gateway1 | gRPC 返回 UNAUTHENTICATED/PERMISSION_DENIED | — | PASS |
| F5 | 未授权客户端访问 Sink | gRPC 返回 UNAUTHENTICATED/PERMISSION_DENIED | — | PASS |
| F6 | Collector 采集目标端 | `collector devices` 仅 15221–15230 | — | PASS |
| F8 | 过期数据新鲜度 | 源端中断后 Collector 明确标记 stale 并停止发布；目标保留最后值但不产生新样本 | — | PASS |

所有注入仅作用于本专项进程与隔离 NATS，未影响其他项目。

## 12. 三轮稳定性

| 轮次 | 启动 UTC | 随机场景（7 tests） | 就绪 | 身份场景（11 tests） | 停机 | 遗留 |
|---|---|---|---|---|---|---|
| R1 | 06:57:35 | 06:59:14–06:59:32 **7 passed** | Task RUNNING / 10 Delivery RUNNING / 410 published | 06:59:55–07:00:32 **11 passed** | 07:04:53 | 进程 0，端口 0 |
| R2 | 07:05:06 | 07:05:35–07:05:54 **7 passed** | Task RUNNING / 10 Delivery RUNNING | 07:06:09–07:06:46 **11 passed** | 07:06:59 | 进程 0，端口 0 |
| R3 | 07:07:04 | 07:07:34–07:07:53 **7 passed** | Task RUNNING / 10 Delivery RUNNING | 07:08:03–07:08:39 **11 passed** | 07:08:55 | 进程 0，端口 0 |

R1 还包含第 11 节故障注入。三轮独立启停、独立对账，无轮次被平均值掩盖。
每轮 20 个 Modbus listener 在启动后 10 秒内全部就绪。
身份场景的 11 项为 10 个逐台“跨服务共享 sample ID”用例 + 1 个常量隔离用例；
随机场景的 7 项为 2 个协议对账 + 5 个业务/权限/回环检查。

## 13. 修复记录及回归

| 修复 | 回归 |
|---|---|
| 删除与点表冲突的 `business_points_standard_v1.5.0` 副本 | Collector Loader 通过；新增单版本断言 |
| 设备实例 `read_merge_max_gap=0` | WTG02–10 由 PARTIAL 恢复 RUNNING；三轮对账通过 |
| CLI Collector 服务名改为 `collector` | `collector task/publishing/devices` 可用 |
| 启动助手容忍瞬时 DEGRADED | `--up` 一次拉起 5 服务 |
| 新增身份常量场景与隔离断言 | 三轮身份测试 PASS |
| 新增 CLI 因果证据、权限拒绝、目标端排除测试 | 每轮 7 passed |
| 新增共享 sample ID 断言 | 每轮 10/10 PASS |
| 修复 ruff/format/mypy | 专项全部 0 错 |

未删除任何失败用例，未降低容差或断言强度。

## 14. 未解决生产缺陷与限制

| 编号 | 等级 | 问题 | 根因 | 最小整改建议 |
|---|---|---|---|---|
| P-1 | P2（生产） | Collector 默认 `read_merge_max_gap=8` 会跨越点表未定义地址合并读取，遇到严格 Modbus 服务器（含本仓库 Gateway）返回 IllegalDataAddress，整台设备 PARTIAL | `group_consecutive_reads` 只按地址间隔合并，不判断空洞是否在点表内 | 合并前要求区间全部被点表覆盖，或读取失败时回退逐组读取；补稀疏点表回归 |
| P-2 | P3（生产/运维） | Data Generator 在 Gateway 重启丢失目标租约后 fail-closed 停止输出，不自动重新建链 | 租约不可恢复即拒绝继续输出（安全设计） | 明确“需重新启动生成”的运行说明即可；如需自动恢复，需按生产流程实现租约重建 |
| P-3 | P3（CLI） | `service start` 在进程已 RUNNING 但业务健康瞬时 DEGRADED 时返回非零 | 健康状态与进程状态共用退出码 | 进程确认 RUNNING 时返回成功，业务健康单独输出；测试助手已适配 |
| L-1 | 范围限制 | 未运行 DCEMS Server/Web、历史落库、Commander、AGC/AVC/PFR 算法 | 本轮范围与缺少 DCEMS 镜像 | 不标记 PASS；待恢复后另立验收 |

## 15. 最终裁决

| 验收项 | 裁决 |
|---|---|
| `CONFIG_CLOSURE` | **PASS** |
| `DUAL_GATEWAY_ISOLATION` | **PASS** |
| `COLLECTOR_ACQUISITION` | **PASS** |
| `NATS_PUBLICATION` | **PASS** |
| `SINK_GATEWAY_DELIVERY` | **PASS** |
| `TEN_TURBINE_RECONCILIATION` | **PASS** |
| `STATION_PQ_PFR` | **PASS** |
| `CROSS_DEVICE_ISOLATION` | **PASS** |
| `FAILURE_RECOVERY` | **PASS**（DG 需显式重启生成） |
| `THREE_ROUND_STABILITY` | **PASS** |
| 总体 | **READY_FOR_NEXT_DEVELOPMENT_WITH_LIMITATIONS** |

限制：Collector 稀疏点表依赖显式 `read_merge_max_gap=0`（P-1 待生产侧
加固）；Gateway 重启后 DG 需人工重启生成（P-2）；DCEMS 侧与控制链路不在本轮范围。
本轮不得视为生产 READY 或 READY_TO_MERGE。

## 16. 证据路径

`run/dcems-measurement/evidence/`（Git 忽略，无令牌/密码）：

```text
baseline.txt                         Git/环境/NATS 基线
final-static-suite.log               专项 ruff/format/mypy/collect
repo-gate.log, mypy-src.log          仓库门禁（既有 UNRELATED 失败）
nonlive-full.log                     33 passed
r1-check.log / r1-up.log             配置检查与启动
r1-collector-task.json               Task RUNNING
r1-collector-publishing.json         NATS 发布
r1-sink-deliveries.json              10 × RUNNING
r1-live.log / r1-isolation.log       7 passed / 11 passed
f1-*.json f2-*.json f3-*.json        故障注入与恢复
source-target-reconciliation.json    45 轮源/目标原始样本与失败集合
r2-*.log r3-*.log                    第二、三轮同项证据
r3-grpc-listeners.txt                52051/52053/52073/52074/52075
```

本地与远端 HEAD 一致性、最终提交 SHA 由交付消息给出；本机无 GitHub 凭据，
push 结果如实报告，不宣称已同步。

# wind-hub C5 独立集中验收报告（2026-11-24）

## 0. 最终裁决

| 裁决项 | 结果 |
|---|---|
| **C5 集中验收（完整八服务生产资格）** | **BLOCKED** |
| 降额 Sink 容量专项长测（不含 Storage/Configuration 全链语义） | 可单独开展，不等于八服务生产资格 |

模块级测试与新建的八服务 CLI 驱动真实 E2E 已通过；阻断来自尚未解决的**结构性生产缺口**：RAW 无真实来源、单 Sink 无法达到 300 批/s 理想目标（150,000 点/s）、官方准入脚本未能在干净 HEAD 上完成（详见第 7、8 节）。

## 1. Git 基线、最终 HEAD 与远端状态

- 固定分支：`fix/production-readiness-closure-20261124`
- 交接 HEAD：`263bf6dd93bc3dd47858394439b36a09189ae72f`（本地/远端一致，工作树干净）
- `origin/main`：`79946cb3650c8023b46fcee282fa9a1435956ffe`
- 本轮提交：
  - `116219ebb` fix(c5): close static gates and narrow modbus raw word types
  - `<final>` docs(audit): c5 acceptance report and evidence（见交付消息）
- 未新建分支/PR、未合并 main、未 rebase、未 force push。

## 2. 静态门禁

| 门禁 | 交接结果 | 最终结果 |
|---|---|---|
| `python -m ruff check src tests deploy` | FAIL：语法错误与缺失导入 | **PASS：All checks passed** |
| `python -m ruff format --check src tests deploy` | FAIL：7 文件 | **PASS** |
| `python -m mypy src` | PASS | **PASS：483 files** |
| `python scripts/generate_rpc.py --check` | PASS | **PASS** |
| `lint-imports` | PASS | **PASS：22 kept, 0 broken** |
| `python -m pytest --collect-only -q` | FAIL：27 collection errors | **PASS：4083 tests collected** |

交接 HEAD 的直接缺陷：`tests/support/gateway.py` 中列表解包与生成器混用的 `SyntaxError`（27 个收集错误）、Commander 测试缺失 `CommanderService/build_commander/ConfigSnapshots/register_services` 导入。已在本轮修复。

## 3. 各模块测试统计

| 模块 | passed | failed | errors | skipped | 结论 |
|---|---:|---:|---:|---:|---|
| Contracts + Core + Configuration | 1080 | 0 | 0 | 2 | PASS |
| Commander（全量） | 370 | 0 | 0 | 1 | PASS |
| Industrial Gateway | 226 | 0 | 0 | 0 | PASS |
| Data Generator | 197 | 0 | 0 | 0 | PASS |
| Collector | 159 | 0 | 0 | 2 | PASS |
| CLI | 269 | 0 | 0 | 13 | PASS |
| Storage | 112 | 0 | 0 | 7 | PASS |
| Sink（unit/component/integration + system 子集） | 596 + 12 | 0 | 0 | 10 | PASS |
| 八服务 E2E（`tests/system/e2e/eight_service`） | 1 | 0 | 0 | 0 | PASS（真实 CLI 驱动） |

上一轮（`7dba5cf6c`）的红色套件 Commander integration/system、Gateway、DG integration+system、CLI、Storage 已全部转绿；Sink system 全量未单独跑完（R14 600s 与定向子集已单独执行），计 NOT_FULLY_RUN。

## 4. 旧 Fixture 迁移台账

| 类别 | 问题 | 处置 |
|---|---|---|
| Service schema 1 | 生产要求 2/3 | Gateway/Commander/DG/CLI fixture 全部迁移；`test_config_activation_selection` 的 TLS 候选改为 token 环境 |
| 证书 SAN/mTLS | 内部已改 Bearer Token | `tests/support/gateway.py`、`tests/commander/grpc_harness.py`、DG conftest、CLI conftest 与 `test_r1f3_strict_cli_e2e` 改为 Bearer；Commander fencing authority 的 HTTPS/mTLS 保留未删 |
| 已删除配置 | `devices_sim.yaml`、`data-generator-sim.example.yaml` | DG/CLI/Collector fixture 改用活动 `deploy/config_roots` 与 pinned `device_farm-a_v1.6.0`；删除 `tests/data_generator/integration/test_simulation_config_mtls.py`（基于已淘汰 mTLS 配对配置，由 `eight_service` 真实链替代） |
| PointTable pin | `business_points_standard: exactly one pinned catalog is required` | Commander/Collector 合成点表显式 pin `1.5.0`；fixture 目录与活动根一致只留一个 BusinessPoint 目录 |
| 缺失必填参数 | `ControlTarget.token_env`、`GatewayWriter.gateway_token_env` | 组件测试补齐并设置隔离 token 环境 |
| 权限语义 | 未登记身份应 UNAUTHENTICATED，已登记缺权限应 PERMISSION_DENIED | 区分 `stranger`（未知）与 `limited`（已登记空权限）调用方，未降低断言 |
| 进程环境 | Gateway/Collector/Storage 子进程启动失败 | 提供 `WIND_HUB_GATEWAY_*`、Collector admin token、Storage cursor secret；`test_factory_shipped_config_passes_native_check` 改用 `prod-a` 活动配置 |
| 循环地址 | `localhost` 被拒绝 | CLI/Gateway fixture 使用字面 `127.0.0.1` |
| 无超时调用 | Configuration 测试无限挂起 | gRPC/NATS 调用统一加 timeout，`max_reconnect_attempts` 有界 |

## 5. 完整八服务真实 E2E 覆盖矩阵

新增 Harness：`tests/system/e2e/eight_service/`（`conftest.py` + `test_e2e_eight_service_smoke.py`），活动候选 `deployment_e2e-final_v1.0.7` + `service_cli-e2e_v1.1.5`，真实 R5 Docker 栈（NATS/JetStream、Redis、Kafka、TDengine 由测试独立启动清理）。

| 服务 | 启动方式 | 真实进程 | 证据 |
|---|---|---|---|
| Configuration | CLI `service start` + `config publish --root` | 是 | SQLite 发布修订、健康 RPC |
| Gateway（source + target） | CLI `service start` | 是 | `service status` HEALTHY，Modbus 监听端口 |
| Commander | CLI `service start` | 是 | HEALTHY，控制链配置闭包 |
| Data Generator | CLI `service start` + `data-generator start e2e-plant-physical` | 是 | 真值 JSONL `artifacts/e2e-final/data-generator/*.jsonl` |
| Collector（realtime + reliable） | CLI `service start` | 是 | acquisition/nats 依赖 HEALTHY，采样持续 |
| Sink | CLI `service start` + `sink start <delivery>` | 是 | 4 个 Delivery（Kafka/Redis/Gateway/Modbus），`e2e_kafka_protocol`/`e2e_redis_power`/`e2e_gateway_power` RUNNING |
| Storage | CLI `service start` | 是 | `storage samples` PROTOCOL 逐条回读；`service restart storage` 后历史窗口仍可回读 |
| TDengine | R5 栈 | 是 | `SELECT COUNT(*)` 于 Storage 真实表（非 adapter 日志表）> 0 |

实测证据（`eight_service_smoke.json`）：PROTOCOL 样本 66、Kafka offset 67、TDengine 实际表 66 行、Generator JSONL 存在、Storage 重启后同窗口回读 84 条、匿名 Storage/Common 调用 `UNAUTHENTICATED`。

未覆盖项（本轮范围外，需按跨模块设计书逐步补回）：BUSINESS 等级全链逐值对账、RAW、断网/重投/去重/版本变更矩阵、Commander 控制写命令端到端反馈、全部 CLI/RPC 动态分母矩阵。

## 6. RAW / PROTOCOL / BUSINESS 独立裁决

| 数据等级 | 裁决 | 依据 |
|---|---|---|
| RAW | **BLOCKED** | 当前 Collector/Sink 链路未提供原始未解码协议载荷；Sink `kafka_wire` 明确拒绝 RAW。不得用 PROTOCOL 标量或合成 JSON 冒充。 |
| PROTOCOL | **PASS（全链实测）** | 上节八服务链路：Collector→NATS→Sink→Kafka→Storage→TDengine 真实样本与独立回读。 |
| BUSINESS | **NOT_VERIFIED（全链）** | 本轮仅显式选择 PROTOCOL 源；Business 映射与单位闭合未在八服务链上独立对账。 |

RAW 缺口与建议（交 ChatGPT）：需在 Collector 侧保留原始载荷并在 Contracts `SampleItem`/Sink `kafka_wire` 增加 RAW 真实来源与线格式；当前 `kafka_wire._require_protocol` 拒绝即说明该来源未闭环。

## 7. 容量短烟测结果

环境：真实 R5 栈，500 点/批，30 设备，60s，R14 bounded PubAck 发布（每设备顺序）。

| 工况 | 目标批/s | 发布实测 | Kafka 确认稳态批/s | 确认值/s | 积压斜率(批/s) | 丢弃/DLQ | 排空 | CPU(单核%) | RSS(MiB) |
|---|---:|---:|---:|---:|---:|---:|---|---:|---:|
| 30 设备 60s | 120 | 116.68 | **116.62** | 58,312 | 0.659 | 0 / 0 | 是 | 98.8 | 252.6 |
| 30 设备 60s | 300 | 274.98 | **122.38** | 61,192 | **152.81** | 0 / 0 | 是 | 46.7 | 253.2 |

- **120 批/s（60k 点/s）降额通过**：确认吞吐 ≥114，零丢失、零 DLQ，最终排空。
- **300 批/s（150k 点/s）未达标**：发布侧 274.98 批/s 已达，但 Kafka 确认稳态仅 122.38 批/s，稳态积压以 152.81 批/s 持续增长；说明 Sink 消费/编码与 Kafka 确认路径是瓶颈。停止发布后最终排空不代表可持续 300 批/s。
- 单 Sink 可信稳定容量约 **116–122 批/s（58k–61k 点/s）**，约为 300 目标的 40%；满足理想目标预计需 ≥3 个 Sink 分片，且须实测 Storage→TDengine 落库容量（本轮未测）。
- 官方 `soak_qualification.sh smoke 120` 在工作树含新报告/证据文件时被其自身的 `dirty worktree` 守卫拒绝（`refusing unverifiable long-run qualification`）；容量证据以同参数直接 R14 运行取得。

## 8. 缺陷台账

| ID | 级别 | 归属 | 缺陷 | 状态 |
|---|---|---|---|---|
| R-01 | P1 | 测试基础设施 | 交接 `gateway.py` 列表解包语法错误 + 缺失导入，27 收集错误 | **CLOSED**（717c3595a） |
| R-02 | P1 | 测试基础设施 | 旧 schema1/mTLS Fixture 与当前生产契约不符 | **CLOSED**（531079259/dd9b1be3c） |
| R-03 | P1 | 生产配置候选 | `sinks_e2e-final_v1.0.1.yaml` 在 `e2e_gateway/e2e_redis/e2e_kafka` 上仍带 `credential_ref`，与当前 Sink schema 不符；Collector 缺 `e2e-target` 组任务 | 记录给 ChatGPT：需产生不可变后继；Harness 在隔离副本中已做最小适配 |
| R-04 | P1 | 生产（RAW） | 原始协议载荷无来源 | **OPEN（结构性，交 ChatGPT）** |
| R-05 | P1 | 生产（容量） | 单 Sink 约 116–122 批/s，未达 300 目标 | **OPEN（结构性，需分片/容量设计）** |
| R-06 | P2 | 测试基础设施 | 八服务 Harness 反复调参暴露的启动就绪竞态（CLI 在 Sink/Collector 收敛前返回 UNKNOWN/ABORTED） | Harness 采用有界等待与重试，不改生产；建议 CLI 提供统一 readiness 语义 |
| R-07 | P2 | 配置/存储语义 | TDengine 时间戳与历史闭包（D-04） | ChatGPT 修复已在本轮真实 Storage 重启回读中通过；Storage 全量 112 passed |

无 P0。

## 9. 尚未执行项目与原因

- RAW 全链：结构性缺口（R-04）。
- BUSINESS 全链逐值对账：本轮只跑 PROTOCOL。
- Sink system 全量（非 R14）：未单独完整跑完，只覆盖 unit/component/integration 与定向子集。
- 300 批/s 持续容量（>60s）与 Storage→TDengine 落库吞吐：NOT_RUN。
- 官方准入脚本完整通过：dirty worktree 守卫拒绝（证据文件未提交时）。
- 断网/重投/去重/多版本历史回滚、全 CLI/RPC 动态矩阵、硬件/现场 PLC、iSulad、systemd 生产单元、netem：NOT_RUN。
- 正式 600s/3600s 长测：本轮未启动。

## 10. 长测准入结论

**BLOCKED**。虽然所有受影响模块、静态门禁与八服务真实 CLI 驱动 E2E 已实际通过，但 RAW 来源与单 Sink 300 批/s 理想目标仍是未解决的结构性缺口；因此不能宣称完整八服务生产资格，也不能把本结果当作正式 600s/3600s 长测的准入。

可区分次裁决：**允许开展降额 Sink 容量专项长测**（约 120 批/s、60k 点/s、单 Sink），须与完整八服务生产资格严格区分。生产收口与正式长测由 ChatGPT 依据 R-04/R-05 决定。

## 证据索引

`docs/audit/production_retest_20261124_evidence/`
- `eight_service_smoke.json`、`eight_service_logs/`：八服务 CLI 驱动真实链路证据
- `r14_degree30_120_60s.json`、`r14_degree30_300_60s.json`：容量实测
- `config_contracts_core.log`、`commander_full.log`、`collector_full.log`、`storage_full.log`、`gateway_full.log`、`data_generator_full.log`、`cli_full.log`：模块全量日志

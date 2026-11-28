# wind-hub FU-01 容量专项与 FU-02 日志独立复测报告（2026-11-28）

> 报告模板：仓库与文件系统未检索到《报告模板.md》（检索位置：仓库根、docs/、ai_shared/、
> 历史报告引用；历史多份报告均注明缺失）。本报告沿用既有 DCEMS/Sink 复测结构。

## 1. Git 基线

| 项目 | 值 |
|---|---|
| 分支 | `feat/dcems-gateway-smoke-20261126` |
| 起始 HEAD | `5bf4561bb502c2c8b4c60a932e5506ae9c04897a`（fetch 后本地=远端） |
| 上轮 Claude 提交 | `defe6ea1` |
| FU-02 整改（4fb629d6..5bf4561b） | `3a876d3b` CLI 子进程即时输出；`38001b6b` Reliable 启停/异常退出日志；`82f12ceb` 日志不含异常详情；`5bf4561b` Realtime 启停与 settle 超时日志 |

## 2. FU-02 生产日志复测

### 2.1 变更审查

- `src/cli/native_process.py`：CLI 托管子进程 env 注入 `PYTHONUNBUFFERED=1`。
- `src/sink_service/application/realtime_delivery.py`：started/stopped INFO（delivery/sink 身份）、
  settle 超时 WARNING（在抛出前记录）。
- `src/sink_service/infrastructure/reliable_runtime.py`：started INFO（delivery/sink/durable/revision）、
  stopped INFO、消费任务异常退出 ERROR（**消息不含异常详情**，详情仅入 `last_error` 状态）。

### 2.2 验证矩阵

| # | 检查 | 结果 | 证据 |
|---|---|---|---|
| 1 | 真实 CLI 启动 Sink Main+Worker | PASS：`cli service start sink` 启动，Worker 进程 stdout 经 CLI 落盘 `sink.log` | `fu02/sink-startup.log` |
| 2 | 启动日志含 Delivery/Sink/配置版本等非敏感身份 | PASS：Realtime ×10（`delivery=dcems_wtgNN sink=dcems_gateway_NN`）；Reliable（真实进程 `test_delivery_flow`）：`delivery=power_history sink=history_kafka durable=WH_SINK_… revision=<sha>` | `fu02/sink-startup.log`、`fu02/sink-reliable-process.log` |
| 3 | 正常停止/重启有日志 | PASS：live `sink stop/start dcems_wtg01` → stopped+started 各1行（周期净增2行）；Reliable stopped（revision） | `fu02/live-stop-start-log.txt`、`fu02/sink-reliable-process.log` |
| 4 | settle 超时存在 WARNING | PASS（单元，caplog 断言 WARNING 文案+语义不变） | `fu02/pytest-service-logging.txt` |
| 5 | Reliable Consumer 异常退出存在 ERROR | PASS（单元：异常退出→ERROR 含身份、详情不入日志；STOP 语境不误报；非 STOP 取消→ERROR） | 同上 |
| 6 | 无密钥/Token/密码泄露 | PASS：startup/stop-start/real-process 日志均0命中；单元断言消息不含 token/password/异常详情 | `fu02/*` |
| 7 | 无高频重复日志 | PASS：停启周期+2行；空闲10s +0行；进程日志全程5行 | `fu02/live-stop-start-log.txt` |
| 8 | 生命周期/所有权/超时/自动恢复语义不变 | PASS：`test_realtime_delivery_stop_settlement`5、`test_delivery_runtime`12、`test_output_pool_cancel_races`5、`test_service_logging`6、系统 `test_delivery_flow`3 全绿（见 §5 回归） | `regression/*` |

单元日志契约：`tests/sink_service/unit/test_service_logging.py`（6 passed，本轮新增）。

## 3. FU-01 容量实验

### 3.1 基线（沿用）

`docs/audit/dcems_t04_followup_retest_20261128_evidence/capacity/`：
degree2/p1/600s=59.43、degree30/p1/600s=52.01（目标120 batch/s、500点/批）。

术语：**业务批**=500点 Delivery 批；**Kafka 物理批**=Producer builder（至多 max_batch_size 字节，
可跨设备合并多业务批）；**Record**=单点 Kafka 记录；**Broker ACK**=分区确认。

### 3.2 单变量筛选（120s，独立 Docker project/topic/配置根，互不污染）

固定除变量外：`DEGREE/TARGET=120/POINTS=500/MAX_SAFE_INFLIGHT=4`。
（短时筛选不作为最终 PASS 依据。）

| 实验 | 参数 | confirmed batch/s | submit mean | confirm mean | 排空/零丢失 | 合并写 | pts/物理批 | 备注 |
|---|---|---|---|---|---|---|---|---|
| A1 | p=1, b=131072, d=30 | 56.62 | 51.4ms | 17.3ms | 未排空/否 | False | 419.8 | 30设备全落分区0 |
| A4 | **p=4**, b=131072, d=30 | **63.01** | 32.1ms | 28.4ms | **是/是** | False | 323.0 | **A 组最优** |
| A8 | p=8, b=131072, d=30 | 59.20 | 34.0ms | 30.2ms | 是/是 | False | 339.6 | 确认延迟随分区上升 |
| B | p=4, **b=524288**, d=30 | 62.99 | **7.9ms** | 46.3ms | 是/是 | **True** | **973.8** | 合并写生效但被更大 confirm 抵消 |
| C | p=4, b=131072, **d=2** | 59.28 | **6.6ms** | **12.5ms** | 是/是 | False | 342.0 | lag p99 仅18ms |

（完整 JSON/样本见 `capacity/`；broker 资源见 `exp-docker-stats.csv`。）

### 3.3 分层结论与合并写归因

| 类别 | 结论 |
|---|---|
| 环境瓶颈 | Kafka 确认往返仍主导：p4 confirm 28–46ms；分区数↑→并行度↑但单批 confirm↑（17→28→30ms）；单 Broker+本机盘为物理承载 |
| 配置参数 | **partitions=4 优于1/8**（+11%）；**batch=524288 使跨设备合并写真实生效**（cause2 证实：131072≈单业务批130KB 即填满 builder，pts/b=419<500；524288 下 pts/b=973.8>500、`cross_device_coalescing_observed=True`、submit 51→7.9ms），但单批 confirm 成本同涨（28→46ms），**净吞吐持平63.0** |
| 生产代码 | 合并写实现**有效**（非失效）：分区 FIFO drain + builder 跨设备填充按设计工作；`cross_device_coalescing_observed=False@b131072` 的原因为**容量不足（cause2）**，非分区不同（cause1：同分区才合并，A4 设备落4分区）、非提交不重叠（cause3：drain 快照聚合）、非配额（quota 等待≈0）。Producer 背压（cause4）体现为 submit 等待（p151ms→p432ms→b5242887.9ms） |
| 生产目标 | **所有组合均未达120**（最优63.01，需114）；单 Broker 分区确认吞吐≈63/s 为当前硬顶 |

### 3.4 600 秒最终验证 — FAIL

最优组合 **p=4 / b=131072 / d=30**（A4 筛选最优）执行完整600s：

| 指标 | 结果 | 阈值 |
|---|---|---|
| 发布 batch/s | 120.0 | ≥114 |
| **确认稳态 batch/s** | **58.02** | **≥114（0.95×120）** |
| 积压斜率 | +37.6/s | ≤2.4 |
| 排空 | 否（未排空） | 必须 |
| 零丢失 | 否（未确认余量存在） | 必须 |
| DLQ / gRPC 失败 | 0 / 0 | 0 |
| 分区确认分布 | p0..p3：5.73M/6.42M/2.87M/6.43M | — |
| submit/confirm mean | 35.1ms / 30.8ms | — |
| 事件循环 lag p99 | 109ms | — |

结论：最优参数仍只有目标的 **51%**（58.02/114），未排空；600s 长时确认吞吐较120s 筛选
（63.01）进一步衰减。**SINK_CAPACITY_600S = FAIL**。阈值、时长、负载均未改动。
证据：`capacity/r14_capacity_d30_r120_c4_b131072_p4_e42ddbaa6a4e.json`、`FINAL_p4_d30_600s.log`。

## 4. 回归检查

| 门禁 | 结果 |
|---|---|
| `ruff check src tests` | **PASS**（All checks passed） |
| `ruff format --check src tests` | **PASS**（1126 files） |
| `mypy src` | **PASS**（483 files, no issues） |
| `lint-imports` | **PASS**（22 kept, 0 broken） |
| Sink+Gateway 单元 | **604 passed / 0 failed**（含新增 `test_service_logging`6、转换合同、停止结算、配额/取消竞态、Kafka FIFO/ACK/重试族） |
| Sink+Gateway 组件 | **169 passed**（delivery runtime/watchdog、config snapshots、grpc updates&claims、输出池配额） |
| Sink+Gateway 集成 | **55 passed / 10 skipped** |
| Gateway 系统 | **22 passed** |
| Sink 系统（除容量专项，容量以 §3.4 独立裁决） | **62 passed / 1 deselected**（20m49s） |
| T04 配置契约 | **7 passed**（`regression/pytest-t04-config.txt`） |
| DCEMS 发布契约 | **2 passed**（release contract + preflight 子集） |
| MANIFEST / third_party | **133 OK**；`git status third_party/` 零改动 |

**F1/F2/F3 不回退专项**：
- Delivery 停止不错误隔离其他 Delivery：F3 live（停 wtg01 → wtg02-10 GOOD/CURRENT）+ 本轮 FU-02 live 停启后其余九机 RUNNING ✓
- 未完成 I/O 不提前释放所有权：`test_realtime_delivery_stop_settlement`5 + `test_delivery_runtime`12 ✓
- Kafka 设备内 FIFO / 设备分区稳定：`device_order_audit`（每设备单分区、序列单调）在全部6组实验通过 ✓
- ACK 后才确认源消息、未确认可安全重试、无静默丢失：全部实验 `dlq=0、grpc_fail=0`；p4/p8/b524288 组合 `drained+zero_loss=True` ✓
- 无重复控制执行：T04 live D6/稳定期 FC16 delta=1 ✓
- **T04 真实 DCEMS 联测（独立回归，本轮实际执行）**：**15/15 passed**（`t04/pytest-t04-15.txt`）——
  F1 故障0/1全链、F2 停启隔离恢复、F3 过期/隔离/恢复、十机9点对账、450kW/125kvar 物理反馈全部复测通过。

## 5. 证据路径

`docs/audit/sink_fu01_fu02_capacity_retest_20261128_evidence/`

```text
static/ruff-check.txt ruff-format.txt mypy-src.txt import-linter.txt manifest-check.txt
fu02/pytest-service-logging.txt        日志单元契约（6 passed）
fu02/sink-startup.log                  CLI 启动 Realtime×10 身份日志
fu02/sink-reliable-process.log         真实进程 Reliable started/stopped+revision
fu02/live-stop-start-log.txt           停启周期+2行、空闲10s+0行
fu02/pytest-delivery-flow.txt          真实进程投递测试（3 passed）
capacity/screening-matrix.json         A/B/C 筛选矩阵汇总
capacity/r14_capacity_*.json           各组完整结果+样本哈希
capacity/r14_capacity_*_samples.jsonl  逐秒样本
capacity/{A1_p1,A4_p4,A8_p8,B_p4_b524288,C_p4_d2,FINAL_p4_d30_600s}.log
capacity/exp-docker-stats.csv          实验期 Kafka/NATS 资源
regression/pytest-units.txt components.txt integrations.txt gateway-system.txt
  sink-system.txt t04-config.txt dcems-contract.txt
t04/pytest-t04-15.txt                  T04 15项真实联测
```

## 6. 结果裁决

| 项目 | 结果 |
|---|---|
| `FU02_LOGGING` | **PASS**（8项全过：CLI 真实启动、身份日志、停启日志、WARNING/ERROR 单元契约、无泄密、无高频、语义不变） |
| `STATIC_GATES` | **PASS**（ruff/format/mypy/import-linter 全绿；1例 I001 导入排序已修） |
| `KAFKA_PARTITION_EXPERIMENT` | **PASS**（A1/A4/A8 完成：p4 最优63.01，设备分区归属/确认延迟/资源齐全；未达114 属容量门禁裁决） |
| `KAFKA_BATCH_EXPERIMENT` | **PASS**（B 组完成：b524288 vs131072 对照，指标齐全） |
| `COALESCING_VERIFICATION` | **PASS**（合并写**真实生效**：b524288 下 `cross_device_coalescing_observed=True`、pts/物理批973.8、submit 51→7.9ms；b131072 下 False 的根因=物理批容量不足（cause2），非实现失效；其余4项原因逐一排除，见 §3.3） |
| `SINK_CAPACITY_600S` | **FAIL**（最优组合600s：58.02/114，未排空；阈值未动） |
| `SINK_REGRESSION` | **PASS**（单元604/组件169/集成55/网关系统22/契约9 + T04 live15/15；F1/F2/F3 无回退） |
| `VENDOR_IMMUTABILITY` | **PASS**（MANIFEST133、third_party 零改动） |
| `PRODUCTION_READINESS` | **BLOCKED**（SINK_CAPACITY_600S 未过） |

## 7. 生产配置/架构建议与未关闭缺陷

**可立即采纳的配置建议（实验支持，需 ChatGPT 走正式配置变更流程）**：
1. 生产/测试 Kafka topic `partitions` 1→**4**（+11% 确认吞吐、完全排空零丢失；8无额外收益且 confirm 延迟更高）。
2. Producer `max_batch_size` 131072→**524288**：使跨设备合并写生效（submit -85%）；需与 broker fsync 延迟权衡（confirm +65%），当前净收益≈0，建议在目标硬件复测后定稿。

**需 ChatGPT 架构决策（交回，不擅自改）**：
- **FU-01 仍 OPEN**：即使最优参数，单 Broker 确认吞吐硬顶≈63/s <114。第一阻断阶段=Kafka
  `confirm`（分区确认往返，p4 下28–46ms）。候选方向（需评估 ACK 语义/并发所有权/消息格式——
  均属交回范围）：① broker 侧（acks/linger/flush 调优、磁盘与 ISR 拓扑）；② 多 broker/分区
  与消费归属设计（C6-01 既定路线）；③ 业务批与物理批解耦（当前500点批≈130KB 与批容量强耦合）。
- 依据文件：`capacity/*.json`、`exp-docker-stats.csv`、历史 `c6_round1/round2` 与
  `t04_followup` §7 同口径对照。

**未关闭缺陷**：
| ID | 等级 | 描述 |
|---|---|---|
| FU-01 | P1 OPEN | 单 Sink 确认吞吐 58–63/114（分区/批/数量实验后仍不达标；架构级） |
| FU-02 | **本轮关闭** | 生产日志8项验证通过（若 ChatGPT 认可本报告即 CLOSED） |
| （沿用） | P3 | sink system Docker 端口瞬态竞态（r5 重试缓解） |

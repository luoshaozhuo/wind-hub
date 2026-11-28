# wind-hub T04 遗留问题整改独立复测报告（2026-11-28）

> 报告模板说明：仓库与文件系统仍未找到《报告模板.md》（历史报告均已注明），本报告沿用既有 DCEMS 复测报告结构与符号规范。

## 1. Git 基线与提交清单

| 项目 | 值 |
|---|---|
| 分支 | `feat/dcems-gateway-smoke-20261126` |
| ChatGPT 整改 HEAD（起始） | `67c5b08c11feae744623c507e41913d51601d70b`（fetch 后本地=远端） |
| 上一轮 Claude 提交 | `4fb629d6` |
| 本轮整改提交（4fb629d6..67c5b08c） | `c98f12f6`/`d77116f5`/`b52b2fb8`/`b3540678` Kafka 出厂 schema + 子进程日志；`fa35ab73`/`cfac6971`/`67c5b08c` Docker 端口竞态重试 |
| 本轮修改（Claude） | 见 §6 修改清单 |

## 2. 静态门禁 — PASS

| 门禁 | 命令 | 结果 | 证据 |
|---|---|---|---|
| Ruff 检查 | `ruff check src tests` | **All checks passed（exit 0）** | `static/ruff-check.txt` |
| Ruff 格式 | `ruff format --check src tests` | **1125 files already formatted（exit 0）** | `static/ruff-format.txt` |
| mypy | `mypy src` | **483 files, no issues（exit 0）** | `static/mypy-src.txt` |
| import-linter | `lint-imports` | **22 kept, 0 broken（exit 0）** | `static/import-linter.txt` |
| Kafka 模板专项 | `pytest tests/sink_service/unit/test_concurrent_kafka_config.py` | **21 passed** | `kafka/pytest-concurrent-kafka.txt` |
| 配置验证/激活 | `test_config_validation` + `test_config_snapshots` + `test_config_repository` | **80 passed** | `kafka/pytest-config-activation.txt` |
| DCEMS 基线四专项 + T04 配置 | （含 release contract/preflight/runtime/seed/t04 config） | **24 passed** | `static/pytest-dcems-baseline.txt` |
| MANIFEST | `sha256sum -c MANIFEST.sha256` | **133 OK**；`git status third_party/` 零改动 | `static/manifest-check.txt` |

首轮 `ruff check src tests` 暴露5例、`format` 6文件（全部位于 storage/configuration/r5_stack/tdengine，
均为2026-11-25 既有漂移，本轮整改未触碰）。**全部为机械修复**（E501 折行、F541 冗余 f 前缀、
ruff format、B017 按仓库既有先例加带理由 noqa——见 `test_kafka_wire_parity.py:243` 等6处同款），
未降低任何严格度、未删除测试。

## 3. Kafka 出厂模板 — PASS

| 检查 | 结果 |
|---|---|
| v1.0.0 / v1.1.0 `schema_version: 2` | PASS（`kafka/shipped-schema-heads.txt`） |
| 两份出厂文件通过生产快照校验 | PASS（`test_shipped_kafka_110`、`test_legacy_100` 载入真实文件走 `config_io.validate_snapshot`） |
| v1.0.0 串行能力 | PASS（`supports_concurrent_write=false`、`max_safe_inflight=1` 断言） |
| v1.1.0 设备级并发 | PASS（`supports_concurrent_write=true`、`max_safe_inflight=4` 断言） |
| 既有实例引用与配置激活 | PASS（prod-a v1.2.0→1.0.0 / v1.3.0→1.1.0 引用保持；`test_config_validation`60 + `test_config_snapshots` + `test_config_repository` =80 passed） |
| 错误 schema 仍被拒绝 | PASS（新增 `test_wrong_kafka_schema_version_is_rejected`：改回 schema1 → `unsupported schema_version`；系统级 `uc10` 断言同在） |

未增加任何历史 schema 豁免；门禁仍对全部 Kafka Sink 模板强制 schema2。

## 4. Docker 测试栈端口竞态 — PASS

整改（`fa35ab73`/`cfac6971`/`67c5b08c`）审查 + 针对性测试
`tests/fixtures/services/test_r5_stack_port_race.py`（**6 passed**，`static/pytest-r5-port-race.txt`）：

| 检查 | 结果 |
|---|---|
| 一次分配5端口互不相同 | PASS（注入重复 free_port 候选仍去重） |
| 非端口错误立即失败 | PASS（`cannot connect to the Docker daemon` → 1次 up、1次清理、直接抛出，不重试） |
| 端口占用→全新独立 Stack 重试 | PASS（3次 up =3个不同 project、失败 project 各清理1次、成功 project 不清理） |
| 最多3次、不无限循环 | PASS（持续冲突 → 第3次后抛出；up=3、down=3） |
| 每次失败清理对应 project | PASS（`down -p <failed project>`，失败路径在 `_up_stack_once` except 内） |
| 成功 Stack 不被误清理 | PASS（down 调用不含成功 project；真实并发测试中双方容器共存后分别 down） |
| 不清理其他项目 | PASS（仅 project 级 `compose down`；源码断言无 `docker system prune`） |
| 真实 Docker 并发启动 | PASS（双线程同时 `up_stack`：project/端口互异、双方 nats 容器同时存在于 `docker ps`、各自只清理自己） |

## 5. Sink 日志落盘 — PASS

整改（`tests/support/process.py` 注入 `PYTHONUNBUFFERED=1`）验证
`tests/support/test_process_logging.py`（**5 passed**，`static/pytest-process-logging.txt`）：

| 检查 | 结果 |
|---|---|
| 无缓冲配置实际生效 | PASS（子进程 `/proc/pid/environ` 含 `PYTHONUNBUFFERED=1`；**无 flush 无换行** 的 stdout 立即落盘） |
| stdout/stderr 持续落盘 | PASS（两路合并进同一日志文件） |
| 可恢复错误可追溯 | PASS（`logging.error(...E42)` 可在日志中检索） |
| SIGTERM 保留日志 | PASS（terminate 后标记内容仍在；异常退出同走合并落盘） |
| 不记录敏感信息 | PASS（env 中令牌值不出现在日志；`_start_service` 不写 env 到文件） |
| 不影响其他模块启动 | PASS（collector/commander/web/sink 四入口共用同一实现；Popen env/stderr 合并断言通过） |

**0 字节日志的归因（区分缓冲/配置/无输出）**：
- 缓冲：CLI 路径（`src/cli/native_process.py`）不设 `PYTHONUNBUFFERED`，stdout 块缓冲理论存在；
  但 Sink 的 `logging.basicConfig` 走 **stderr（Python3 行缓冲）**，任何 logger 输出本应即时落盘。
- 配置：Sink 正常运行与失败路径均**几乎不打日志**（仅 signal/IPC 异常 warning；Delivery 失败
  经 gRPC `last_error` 暴露，不走 logger）。
- 结论：T04 观察到的 `sink.log=0 字节` 主因是**实际无输出**（含事故当时），非缓冲阻塞；
  测试 harness 路径已由本轮修复保证无缓冲。遗留建议（交 ChatGPT）：为 Sink Main/Worker 增加
  启动 INFO 与 Delivery 状态迁移日志，使事故现场可回放。

## 6. 本轮修改清单（Claude）

| 文件 | 类型 | 说明 |
|---|---|---|
| `src/storage_service/.../kafka_consumer.py`、`tdengine_backend.py` | 机械格式 | E501 折行（无语义变化） |
| `tests/storage_service/integration/test_tdengine_wide_probe.py` | 机械修复 | F541 冗余 f 前缀、E501 折行、B017 带理由 noqa（仓库先例同款） |
| `src/configuration_service/.../schema_catalog.py` 等 | 机械格式 | `ruff format` |
| `tests/fixtures/services/test_r5_stack_port_race.py` | 新增测试 | 端口竞态7项验证（含真实并发） |
| `tests/support/test_process_logging.py` | 新增测试 | 日志落盘6项契约 |
| `tests/sink_service/unit/test_concurrent_kafka_config.py` | 新增测试 | 错误 schema 拒绝 |
| `docs/audit/dcems_t04_f1_f2_f3_retest_20261128.md` | 更正 | §8 “C6 曾通过”→ C6 实为失败（C6-01 P1 OPEN），附出处 |

生产结构性改动：**无**（容量根因属架构级，见 §7，交 ChatGPT）。

## 7. Sink 600 秒容量专项（进行中）

### 7.1 受控复现（默认参数 degree=2，隔离环境，600s）

- 复现成功：`confirmed_batches_per_s_steady=59.43`（目标≥114=120×0.95），与历史52–62一致；
  `published=120.0/s`（发布侧满分）、`backlog_slope=+36.7/s`、`final_pending≈7226`、
  `drained=false`、`zero_loss=false`（未排空）。
- 证据：`capacity/result.json`、`*_samples.jsonl`、`run.log`、`docker-stats.csv`。

### 7.2 分层测量（每写均值，34,994 次原生写）

| 层级 | 指标 | 结论 |
|---|---|---|
| Publisher 发布速率 | 120.0/s，PubAck mean 2.29ms | ✅ 达标（JetStream 摄入无瓶颈，NATS CPU ~5%） |
| JetStream 积压/消费 | 积压斜率 +36.7/s 持续增长 | ⚠️ 下游消费跟不上发布（消费侧瓶颈的果） |
| Reliable 编码 | fetch 2.40ms + encode 4.59ms + ack 1.36ms | ✅ 非瓶颈 |
| Reliable 写入 | **write 19.53ms**（占全程683s/600s≈1.14 并发） | ❌ 第一阻断阶段 |
| Sink 队列/配额 | global 0.012ms + physical 0.007ms | ✅ 配额等待≈0（非配额瓶颈） |
| Kafka metadata/submit/confirm | lock 0.002 / metadata 0 / **submit 5.67ms / confirm 12.99ms** | ❌ confirm=阻断根因（broker 应答/落盘往返） |
| Kafka Broker 资源 | 容器 CPU 峰值89%、均值~30–50%；**块设备 IO ≈11GB写+11GB读/600s**；内存~1GB；GroupCoordinator 错误1次 | ⚠️ 本机 WSL2 磁盘/单 broker 压力证据 |
| Sink 进程 | CPU 71%单核、RSS243MiB、事件循环 lag p99 24.7ms、写延迟 p99 80.3ms | ⚠️ 尾延迟显著 |
| 吞吐/积压/drain | 59.4/114、积压增长、未排空 | ❌ 生产目标未达成 |

### 7.3 结构性上限分析（degree=2）

- 每条 durable 单飞（`previous Sink I/O remains unsettled` 单在途写）；degree=2 → 仅2条 durable。
- 理论上限 = 2 / 19.53ms ≈ **102.4 batch/s < 114** —— 即使调度完美，在本机写延迟下默认参数
  **结构上无法达标**；达标需写延迟 ≤17.5ms 或更高并发度。
- C6（2026-11-25）通过的形态为 **`R14_DEGREE=30`**（证据文件名 d30_r120/d30_r300，
  `c6_round1_20261125.md:85`），默认 degree=2 从未有过通过基线。

### 7.4 degree=30 对照（完成）

同一隔离环境、`R14_DEGREE=30`（C6 形态）、600s：`confirmed=52.01/s`（**比默认 degree2 的59.43 更差**），
`submit mean 52.4ms`（degree2 为5.7ms）、`confirm mean 24.2ms`（13ms）、
`reliable_write mean 533.8ms`、事件循环 lag p99 94ms、分区仍=1、未排空。
→ 30条 durable 挤入**同一分区**导致 Producer 回压加深（submit 等待 inflight 窗口），
吞吐不升反降。证据 `capacity/result-degree30.json`、`run-degree30.log`。

### 7.5 分层结论

| 类别 | 证据 | 结论 |
|---|---|---|
| **环境瓶颈** | Kafka 容器 CPU 峰值89%、块设备 IO≈11GB写+11GB读/600s（WSL2 盘）、GroupCoordinator 错误；confirm13→24ms、submit5.7→52ms 随队列深度膨胀；历史 C5→C6→本轮确认吞吐116→98→52–60 递降 | 单分区 Broker 落盘/应答往返是第一阻断的物理承载；本机盘性能显著 |
| **配置参数瓶颈** | `R14_KAFKA_PARTITIONS=1` 默认（测试配置）；degree2 默认结构上限 2/19.53ms≈102<114；C6 已提出“分区数单变量实验”（c6_round2 §10.3）未做 | 分区数=1 使 Producer/Broker 无并行度；默认参数组合无任何通过基线（C5/C6/本轮全败） |
| **生产代码瓶颈** | 每 durable 单飞写（`previous Sink I/O remains unsettled`）+ 分区 FIFO drain 设计；`cross_device_coalescing_observed=False`（e3e77188 的合并写未在本工况生效）；事件循环 lag p99 24.7→94ms | 无单点 bug 证据；吞吐受“单飞×分区串行”结构限制——扩大分区/合并写生效属架构改动 |
| **生产目标是否达成** | 默认参数59.43/114、degree30 52.01/114 | **未达成** |
| **第一阻断阶段** | `reliable_write`（19.5ms 均值）→ 内部 Kafka `submit`(5.7ms)+`confirm`(13.0ms)；Publisher120/s、encode4.6ms、配额≈0 均健康 | Kafka 确认往返（单分区） |

**专项处置**：按任务要求未修改120批/s与600s阈值、未调参制造 PASS；分区架构与合并写生效问题
需大规模生产改动 → **停止专项修复，最小可复现证据移交 ChatGPT**
（`capacity/result-degree2.json`、`result-degree30.json`、两份 run.log、两份 docker-stats.csv、
历史 C6 报告 c6_round1/round2 同口径对照）。

## 8. T04 跨系统回归 — PASS

| 套件 | 结果 |
|---|---|
| Sink+Gateway 单元 | **598 passed / 0 failed**（出厂 kafka 模板修复后归零） |
| Sink+Gateway 组件 | **169 passed** |
| Sink+Gateway 集成 | **55 passed / 10 skipped** |
| Gateway 系统 | **22 passed** |
| Sink 系统（`-k "not sustained_capacity"`） | **62 passed / 1 deselected**（容量由专项门禁单独裁决） |
| 日志/端口专项 | logging **5 passed**、r5 **6 passed** |
| DCEMS 基线 + T04 配置 | **24 passed** |
| **T04 15项真实联测** | **15 passed（1m50s）**：`t04/pytest-t04-15.txt` |

T04 live 覆盖（本轮实际执行，非沿用旧结果）：
1. F1 故障位0/1：气象刺激跳闸 → 目标 `True/GOOD/CURRENT` → DCEMS `t`；恢复回 `False`；
2. F2 停止结算与共享恢复：`sink stop/start` 单机隔离→恢复，兄弟九机全程 RUNNING；
3. F3 过期/隔离/恢复：停止注入 >5s → `BAD/STALE`（仅该机）、恢复 `CURRENT`；
4. 十机测量9点逐点对账 + 设备身份不串号；
5. 有功450kW/无功125kvar：FC16→Commander `CONFIRMED_SUCCESS`→DG `APPLIED`→物理响应→DCEMS 反馈；
6. D3/D4/D5 拒绝、D6 断连恢复、FC03/FC04/FC16 抓帧断言全绿。

**VENDOR_IMMUTABILITY**：MANIFEST133 OK、`third_party/` 零改动、镜像/JAR 未动（`static/manifest-check.txt`）。

## 9. 第三方风险与未解决缺陷

### 第三方（DCEMS，仅记录）
- FactoryPoints/WindTurbo NPE、行写入器0–30s停顿、Web 忽略 control() 返回值、Modbus 无质量字段
  （沿用 T03/T04 记录，本轮未新增阻断；DCEMS 内部异常未影响15项断言）。

### 未解决缺陷（wind-hub，交 ChatGPT）

| ID | 等级 | 描述 | 证据 |
|---|---|---|---|
| FU-01 | **P1（OPEN，C6-01 延续）** | 单 Sink 确认吞吐默认参数59.4/114（degree30 反降至52.0）；单分区+单飞写结构上限；需分区拓扑/合并写生效性架构决策 | `capacity/result-degree2.json`、`result-degree30.json`、C6 报告 |
| FU-02 | P2 | Sink Main/Worker 正常与失败路径几乎无日志，事故不可回放（harness 路径已修，生产 CLI 路径建议补启动/状态迁移 INFO） | §5 归因 |
| FU-03 | P3 | sink system Docker 栈端口释放瞬态竞态仍可能出现（本轮6项专项通过；r5_stack 重试已缓解 harness 侧） | `static/pytest-r5-port-race.txt` |

## 10. 结果裁决

| 门禁 | 裁决 |
|---|---|
| `KAFKA_TEMPLATE` | **PASS** |
| `DOCKER_PORT_RACE` | **PASS** |
| `SINK_LOGGING` | **PASS** |
| `STATIC_GATES` | **PASS** |
| `SINK_GATEWAY_REGRESSION` | **PASS** |
| `SINK_CAPACITY_600S` | **FAIL**（默认59.43/114；对照 degree30 52.01/114；未调阈值，分层定位完成） |
| `T04_E2E` | **PASS**（15/15 实测） |
| `VENDOR_IMMUTABILITY` | **PASS** |
| `T05_ENTRY` | **READY** |
| `PRODUCTION_READINESS` | **BLOCKED**（容量专项未过；按任务规则完整生产资格阻断） |

## 11. 进入 T05 的建议

1. **T05 联测可以启动**（`T05_ENTRY=READY`）：遗留整改项 Kafka 模板/端口竞态/日志均已验证通过，
   T04 跨系统15项全绿，无未解释 P0/P1 新缺陷。
2. **完整生产资格仍 BLOCKED**（`PRODUCTION_READINESS`）：唯一阻断为 FU-01 容量 P1。
   建议 ChatGPT 按 C6-01 既定路线决策：① topic 分区数单变量实验与生产分区/消费归属设计；
   ② 评估合并写（e3e77188）在单分区下的生效条件；③ 以本报告 `capacity/result-*.json`
   为基线复测，达标前不得宣称容量通过。
3. FU-02/FU-03 为可观测性与测试基建项，不阻断 T05。

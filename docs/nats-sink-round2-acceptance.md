# NATS / Sink Service 独立服务化 — Round 2 独立验收报告

- 分支：`feat/nats-sink-service`
- Round 2 起始远程 HEAD（ChatGPT 32 commits 基线）：`d311c798`
- 验收方工作提交：`6418f3b4`（测试/夹具/治理迁移）、`7170a6da`（JetStream reliable 语义实测 + P1 复现）
- 最终 HEAD：`7170a6da`（已推送）
- 验收人：Claude Code（独立验收 + 测试修复；不直接改生产逻辑）
- 日期：2026-10-29
- 结论：**FAIL（存在 1 个 P1 阻断：reliable Sink `ClearSink` 后无法恢复消费）**

> 分工遵循任务书：生产逻辑缺陷由 ChatGPT 修复；本报告仅提供复现证据与修复建议，
> 未通过 ignore/skip/xfail/降低断言掩盖问题。复现 P1 的测试刻意保留为
> 未加 xfail 的失败用例，阻断合并直到生产修复。

---

## 1. Git HEAD 与改动边界

| 项 | 值 |
|---|---|
| 远程基线（实际 fetch） | `d311c798` |
| 验收后 HEAD | `7170a6da` |
| 验收提交数 | 2（仅测试 / Fixture / 治理脚本 / 测试支撑） |
| 生产代码（`src/`）改动 | **0**（验收方未触碰） |
| 禁止动作 | 未建 PR、未合并 main、未切分支、未 rebase、未 force push |

验收方提交内容均属允许范围：测试重写、Fixture 与共享支撑迁移、`scripts/ci_gate.py`
与 `scripts/generate_virtual_site.py` 的目录重构机械同步、新增真实链路测试。

---

## 2. 静态门禁

| 门禁 | 范围 | 结果 |
|---|---|---|
| `ruff check` | `src/ tests/ scripts/` | **PASS**（All checks passed） |
| `ruff format --check` | 本轮改动文件 | **PASS**（已格式化） |
| `ruff format --check` | 全仓 | main 既有 format 漂移基线仍在（UNRELATED，见既有记忆 [[ruff-format-preexisting-baseline]]），非本分支新增 |
| `mypy src` | 324 文件 strict | **PASS**（Success: no issues found） |
| `lint-imports` | 11 契约 | **PASS**（11 kept, 0 broken） |

门禁权威口径与 `scripts/ci_gate.py` 一致：mypy 仅跑 `src`；测试支撑不在 mypy 门禁。

测试迁移关键点：
- `Task(sink_ids=)` → 无 sink_ids 的新 Task；`CollectorConfig(sinks=)` → 必填 `messaging`；
  `tasks.yaml.outputs` → `sinks.yaml.outputs`（以 task_id 为键）。
- Collector gRPC 契约对齐：`CollectorRuntimeService` 移除 `ListSinks/VerifySink/WriteTestSink`；
  `PointMetadata` 移除 `sink_ids`（字段号 1–8 锁定不变）。
- RPC 边界白名单把 `core/infrastructure/sink/grpc_sink.py` 旧路径替换为
  `sink_service` 的 `management_cli.py / grpc_management.py / adapters/grpc_sink.py`，
  并把 `sink_service/application/` 纳入「禁 pb2」区。
- ci_gate：`integration-sinks` → `tests/sink_service`；`src/sink_service/` 加入后端前缀、
  路径映射与保守全量回退。

---

## 3. 测试分类与结果

### 3.1 默认套件（非 soak / 非 real_service）

```
1335 passed, 178 deselected, 0 failed（48s）
```

- collector / core / cli / sink_service(diagnostic) / web_server / system(fleet E2E)
  / tooling 全绿；测试收集零 error（1513 collected）。
- 旧 Redis 直写系统测试 `tests/system/acquisition/test_modbus_data_flow.py` 重写为
  **Collector 子进程 → 真实 NATS → sink_service 子进程 → 真实 Redis** 双进程主链路，
  标记 `real_service`，默认不选入 fast gate。
- perf 框架：`NullSink` 替换为内存 `_NullTransport`（装配后替换 SamplePublisher 传输，
  runtime 与 engine 同一实例），点号点号 ID 改为 subject 安全 token，去除已删除的
  `queue_maxsize/backpressure_policy`。

### 3.2 真实服务测试（real_service，本机 Docker）

环境：Docker 29.2.1；`nats:2-alpine -js`（4222/8222，JetStream 已启用）、
`redis:7-alpine`（6379）。不可达时 fixture 明确 `pytest.skip`，不以 mock 冒充。

| 测试 | 结果 |
|---|---|
| `test_modbus_data_flow.py`（Collector→NATS→Sink Service→Redis，2 用例：出数/停任务止流） | **2 passed（5.9s）** |
| `test_nats_consumer_reliable.py::test_reliable_message_acked_without_redelivery` | **PASS** |
| `test_nats_consumer_reliable.py::test_failed_write_is_redelivered_with_backoff_then_acked` | **PASS**（NAK 2 次退避重投，第 3 次成功，最终仅落 1 次） |
| `test_nats_consumer_reliable.py::test_distinct_durables_each_receive_the_full_stream` | **PASS**（两 durable 各自独立收全量） |
| `test_nats_consumer_reliable.py::test_clear_own_backlog_then_resume_consumption` | **FAIL（P1，见 §5）** |

### 3.3 未执行（NOT_RUN，明确标注，未以 Mock 冒充）

- TDengine Sink 真实链路（本机无 tdengine 镜像；reliable 唯一允许的 durable 幂等目标）。
- Kafka / gRPC 下行 Sink、Modbus 下行 Sink 的真实跨进程验证（镜像/对端未就绪）。
- 真实 netem 长稳 / soak（soak marker，全栈串行，本环境未跑）。
- 真实 TwinCAT / 真实 PLC 硬件链路。

---

## 4. D-1 ~ D-9 运行时复测矩阵

逐项以**运行时**复测为准，不仅凭代码审查。

| # | 验收点 | 结论 | 证据 |
|---|---|---|---|
| D-1 | realtime Collector→NATS→Sink 主链路（subject `wh.rt.…`、stale 丢弃） | **PASS** | 双进程真实 E2E 2 passed；realtime 信封必填 `max_staleness_ms`，`runtime.write` 过期 return 有静态+链路印证 |
| D-2 | reliable PubAck / JetStream / EXPLICIT ack 不重投 | **PASS** | 真实 JetStream：成功后超过 ack_wait 仍仅 1 次 |
| D-3 | 写入失败 NAK 指数退避（cap 60s）重投，最终成功不重复 | **PASS** | 前 2 次失败 NAK，第 3 次成功；attempts==3，delivered==1；退避 `min(60, 2**min(attempts-1,6))` |
| D-4 | durable 命名空间积压隔离 + `ClearSink` | **FAIL** | 隔离 PASS；但 clear() 后无法恢复（P1，§5） |
| D-5 | Redis CAS / PXAT 过期 / sample_id 等时冲突 | **PARTIAL** | 主链路 E2E 验证真实 SET 落库与持续更新；`_CAS_SCRIPT`（nt<ct 拒、等时 sample_id 不同返回 -1→SinkError、PXAT 绝对过期）为代码级核实，**等时冲突/迟到旧值未做真实 Redis 运行时断言** |
| D-6 | TDengine reliable 幂等 | **NOT_RUN** | 无 tdengine 镜像；解析层 `reliable 仅允许 tdengine` 的约束存在（assembly 启动期 ValueError） |
| D-7 | 管理面 Stop/Start/Restart/Clear + 意图持久化 | **PARTIAL/FAIL** | 管理 gRPC 已接线 Start/Stop/Restart/Clear，意图 marker 原子替换；但 Clear→恢复链路因 D-4 P1 不通；Start/Stop/Restart 未做跨进程运行时实测 |
| D-8 | gRPC Sink 部分写拒绝（updated != len 失败闭合） | **NOT_RUN**（静态确认） | `grpc_sink.py:78` 有 `reply.updated != len(values)` 拒绝路径；无真实 gRPC 对端做运行时验证 |
| D-9 | Modbus 下行 32/64 位防撕裂 | **NOT_RUN**（静态确认） | 解析层有寄存器区间重叠校验、位宽 `MODBUS_WORD_WIDTH`、coil/discrete 强制 bool；pipeline 串行写。无真实 Modbus 下行对端验证 |

修复是否引入新竞态/重复写/错误 ACK/资源泄漏：在已实测的 realtime 主链路与 reliable
ack/NAK/隔离路径中**未观察到**重复写（每样本最终落 1 次）或错误 ACK；consumer close 走
drain。未覆盖的真实 TDengine/gRPC/Modbus 路径不下结论。

---

## 5. P0 / P1 / P2 缺陷清单

### P1（阻断合并，须 ChatGPT 修复）

**[P1] reliable Sink 在 `ClearSink` 后永久无法恢复消费（含进程重启）**

- 位置：`src/sink_service/infrastructure/nats_consumer.py:117-138`（`clear()`）配合 `open():71-88`。
- 现象：`clear()` 删除 durable 后用 `add_consumer(config=ConsumerConfig(durable_name,
  filter_subject, deliver_policy=NEW, ack_policy=EXPLICIT, ack_wait, max_ack_pending))`
  重建，**未设 `deliver_subject`**。随后 `open()` 的 `jetstream().subscribe(...)` 走
  nats-py 的 `subscribe_bind`，对 push consumer 要求 `config.deliver_subject`，直接抛
  `TypeError: config.deliver_subject is required`。
- 影响：reliable Sink 执行一次 Clear（或任何复用该重建路径的重启恢复）后无法重新订阅，
  JetStream 消息停止投递，数据面静默中断。
- 复现：`WIND_HUB_TEST_NATS=nats://127.0.0.1:4222 pytest
  tests/sink_service/test_nats_consumer_reliable.py::test_clear_own_backlog_then_resume_consumption -m real_service`
  （真实 `nats -js`）。
- 建议方向（供参考，不由验收方改生产）：重建 push durable 时提供 `deliver_subject`
  （或改用 bind 既有 push consumer 与首次 `subscribe` 一致的构造方式），保证 clear 后的
  ConsumerConfig 与 `open()` 订阅路径兼容；修复后该测试应转为绿。
- P0：无。

### P2（不阻断，建议后续）

1. **前端仍调用已删除的 Collector Sink 校验/试写 API**：`web_admin/src/api/sinks.ts:16`
   `verifySink`、`mocks/handlers.ts:179` `POST /sinks/:name/write-test` 仍存在，
   后端 Collector 已无 `VerifySink/WriteTestSink`；MSW mock 掩盖了真实 404/unimplemented。
   应对齐到 sink_service 管理面或下线入口。
2. **Redis CAS 边界缺真实运行时断言**：等时间戳不同 sample_id 的 `-1` 冲突、迟到旧值
   `0` 拒绝、PXAT 绝对过期仅代码级核实（D-5 PARTIAL）。
3. **`SinkRuntime.latest()` 无任何调用方**（Round 1 已记）：realtime 新鲜度查询为死路径，
   确认是否应接入管理面 `GetSinkStatus` 或删除。
4. **TDengine / gRPC / Modbus 下行 / Kafka 真实链路 NOT_RUN**：CI 无对应镜像/对端，
   建议纳入 release/硬件门禁。
5. sink 诊断 DTO 已迁 commander（见 §6），但 sink_service 自身诊断 CLI 仅有 CLI 层测试，
   `check_sink/benchmark_sink` 对真实 Redis 的执行路径未做 real_service 验证。

---

## 6. Commander 诊断 DTO 迁移专项（8 条）

| # | 检查项 | 结论 | 证据 |
|---|---|---|---|
| 1 | `core/domain/diagnostic.py` 已删除 | PASS | main 上存在，本分支文件不存在；提交 `0b76a82a` |
| 2 | 七类型迁入 `commander/application/diagnostic_model.py` | PASS | `DiagnosticCode/DiagnosticSeverity/DiagnosticStage/DeviceVerifyResult/PointVerifyResult/PointsVerifyResult/ResolvedAddress` |
| 3 | 字段/枚举/序列化不变 | PASS | 与 `main:core/domain/diagnostic.py` 归一化逐行 diff：类/字段体 63 行 **IDENTICAL** |
| 4 | 补 DTO 契约测试 | PASS | `tests/commander/test_diagnostic_model.py`（重定位提交 `8e1c5f14`），连同 `test_diagnostic.py` 共 12 passed |
| 5 | gRPC 响应迁移前后一致 | PASS（静态+契约） | `commander/infrastructure/grpc/server.py` 改用新 DTO，wire 字段体未变；无逐字节线上抓包（属生成物字段号契约覆盖范围） |
| 6 | 无旧 `core.domain.diagnostic` 残留引用 | PASS | 全仓 grep 零命中 |
| 7 | 新 DTO 归属正确（仅 commander probe/grpc server 引用） | PASS | 仅 `commander/infrastructure/probe.py`、`commander/infrastructure/grpc/server.py` import |
| 8 | ADS/Modbus/IEC104 三协议驱动零改动 | PASS | `git diff --name-only main..HEAD -- src/core/infrastructure/protocol/` 为空 |

Sink 诊断 CLI 归属：`sink_service.diagnostic_cli:app`（check/benchmark），
entry point `wind-hub-sink-diagnostic`；其契约测试 9 项全绿（注意 CLI 经 from-import
绑定实现函数，monkeypatch 必须打在 `sink_service.diagnostic_cli` 模块）。

---

## 7. Round 1 P2 逐项复核

| Round1 P2 | Round2 结论 |
|---|---|
| `schema_version` 缺省 | 已修为字面量默认 `1`（`Literal[1]`） |
| 未来时间戳 sample | realtime 排序在 `runtime.write` 以 sample_time 比较；未来时间不被特判拒绝（仍按序），行为符合「单调最新值」语义，未单列风险 |
| `latest()` 无调用方 | **仍无调用方**（P2-3，死路径待裁定） |
| 订阅过滤粒度 | assembly realtime 以 `(task,device,point)` 权威去重 + consumer 订阅 subject 与 `allowed` 白名单双层；实测隔离 PASS |
| `sample_id` 长度 | 仅校验非空 strip；无长度上限（NATS header/JSON 可接受范围），维持观察 |
| Kafka task 过滤 | NOT_RUN（无真实 Kafka 断言） |
| web_admin mocks/handlers.ts write-test / SinkRuntime 残留 | **仍残留**（P2-1），mock 掩盖 |

---

## 8. 符合性矩阵（任务书硬约束）

| 约束 | 符合 |
|---|---|
| 先 fetch 以实际远程 HEAD 为准（`d311c798`） | 是 |
| 不建 PR / 不合 main / 不切分支 / 不 rebase / 不 force push | 是 |
| 不直接改生产逻辑（仅测试/Fixture/格式/生成物/治理脚本机械同步） | 是（`src/` 0 改动） |
| 不用 ignore/skip/xfail/降断言/删测试掩盖问题 | 是（P1 以未 xfail 失败用例留证；skip 仅用于真实服务不可达，非掩盖） |
| D 项逐项运行时复测 | 是（可运行项均实测；不可用项标 NOT_RUN） |
| 真实服务 Docker 可用必跑 | 是（NATS/Redis 已跑；TDengine/gRPC/Modbus/Kafka 无镜像标 NOT_RUN） |
| 独立代码审查新竞态/重复写/错误 ACK/泄漏 | 已在已覆盖路径检查，未发现；未覆盖路径如实标注 |
| 提交并推送当前分支 | 是（`7170a6da` 已推送） |

---

## 9. 总结论

**FAIL — 暂不建议 Squash Merge。**

- 默认 1335 项全绿、静态四门全绿、DTO 迁移 8 条全部满足、realtime 主链路与 JetStream
  ack/NAK/durable 隔离在真实 NATS/Redis 下实测通过。
- **唯一阻断为 P1**：reliable Sink 的 `clear()` 重建 durable 缺少 `deliver_subject`，
  导致 ClearSink 后（含进程重启恢复）push 订阅必抛 `TypeError`、消费永久中断。
  复现测试已随分支推送，待 ChatGPT 修复后 Round 3 复测该单点即可评估合并。
- 其余为真实 TDengine/gRPC/Modbus/Kafka 链路 NOT_RUN 与前端旧 Sink API 残留等 P2，
  不阻断但应排期。

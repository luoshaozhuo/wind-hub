# NATS / Sink Service — Round 3 独立复测报告（P1 复测）

- 分支：`feat/nats-sink-service`
- 日期：2026-10-29
- 验收人：Claude Code（独立复测；不直接改生产业务逻辑）
- 结论：**PASS WITH LIMITATIONS**
  - Round 2 唯一 P1（reliable `ClearSink` 后无法恢复 JetStream 消费）**已关闭**，
    真实 JetStream 11 项用例（含 6 个必查场景 + 进程重启重绑）全部通过且可重复。
  - 限制：全量 `pytest -q`（无 marker 裁剪）暴露分支既有的 system/soak 测试夹具
    迁移漂移 31 项（`tasks.outputs` 旧 schema、点号点号），在 Round 2 基线
    `7170a6da` 上即失败、与本 P1 修复无关；另 2 项为**本机** Docker 容器名冲突
    （外部真实服务变量下 PASS），1 项为 main 既有 UNRELATED。
- 本轮生产代码改动（验收方）：**0**。仅测试文件变更（1 个提交）。

---

## 1. 远程状态、SHA 与工作区

| 项 | 值 |
|---|---|
| `origin/main`（fetch 后） | `cb380ddb3bee249defb75e5922e2b70987a18211`（`refactor(core): v06 三协议适配 Core 重构、诊断体系与测试补齐 (#105)`） |
| merge-base(origin/main, HEAD) | `cb380ddb`（main 为分支祖先，无回合入需求） |
| Round 2 验收 HEAD | `7170a6da`（`test(sink_service): verify real JetStream reliable semantics, reproduce clear() P1`） |
| 待测修复提交 | `8fd9e21727e9af75fb924cbe5958956b66d1a222`（`fix(sink-service): create push durable with deliver subject after ClearSink`） |
| Round 2 报告提交 | `b7409a0d`（FAIL 报告） |
| Round 3 HEAD（测试变更，已推送） | `5a2f86da41b109cae44fc806d5fe1c14d8f4feca` |
| 工作区 | 干净（报告提交后） |
| 禁止动作 | 未 rebase、未 force push、未合 main、未建 PR |

`7170a6da..HEAD` 仅 3 个提交：Round2 报告（docs）、P1 生产修复（2 行）、
本轮测试强化（测试）。该区间内 `src/`、`tests/`（除本轮测试文件外）与
`src/contracts/` 的文件同一性举证：

```text
$ git diff --name-only 7170a6da..HEAD -- src/ tests/ src/contracts/
src/sink_service/infrastructure/nats_consumer.py
```

即 parser、SampleMessage 契约与全部 system/soak 夹具自 Round 2 基线起零改动
——第 6 节的失败集合在基线上必然同态。

---

## 2. 修复代码审查（8fd9e217）

修复点：[nats_consumer.py:140](../src/sink_service/infrastructure/nats_consumer.py#L140)
`clear()` 重建 push durable 时补 `deliver_subject=f"_INBOX.{uuid4().hex}"`
（+ `from uuid import uuid4`）。

| 审查点 | 结论 | 证据 |
|---|---|---|
| open()/clear() consumer 配置兼容 | **PASS** | open() 的 `subscribe` 对已存在 durable 走 `consumer_info → subscribe_bind`；push consumer bind 硬性要求服务端 config 的 `deliver_subject` 非空（nats-py 2.16.0 `js/client.py` `subscribe_bind`，None 即 `TypeError`）。clear() 删除后以随机 inbox 重建，bind 成立。 |
| deliver_subject 满足 nats-py push 订阅 | **PASS** | 删除重建是**新** push consumer，新随机 `_INBOX.<hex>` 正确；服务端持久化，跨进程 bind 取回（§3 重启用例实证）。 |
| deliver_policy=NEW 符合清积压 | **PASS** | clear 前的 seq2 积压不再投递，clear 后 seq3 正常；A/B 隔离用例验证只跳过执行 clear 的 durable。 |
| ack_policy / ack_wait / max_ack_pending 不变 | **PASS** | 重建 config 与 open() 一致：EXPLICIT、同一 `ack_wait_seconds`、同一 `max_ack_pending`（diff 仅 +2 行）。 |
| close()/drain() 释放 | **PASS（静态+运行）** | reliable 路径 `client.drain()` 后 `close()`；重启/重复用例无残留订阅报错、无重复投递。 |
| 管理面能否感知 clear 失败 | **PASS** | 运行中 clear 抛 `RuntimeError`；NATS 不可达 / stream 缺失时 consumer_info/连接异常原样抛出，无 try/except 吞错、无假成功返回（两个 fail-closed 用例）。 |
| 新竞态/重复写/错误 ACK | **未发现** | 11 个真实用例中每样本最终恰好投递/落库一次；重启重绑不重放已确认消息。 |

---

## 3. Clear 生命周期矩阵（真实 JetStream，无 Mock）

命令：

```bash
WIND_HUB_TEST_NATS=nats://127.0.0.1:4222 \
  pytest tests/sink_service/test_nats_consumer_reliable.py -m real_service -v
# 11 passed in ~8s（连续两轮重复执行结果一致）
```

| # | 任务书必查场景 | 用例 | 结果 |
|---|---|---|---|
| ① | Clear→Start 消费新消息（旧积压跳过） | `test_clear_own_backlog_then_resume_consumption` | **PASS**（收到 `[1.0, 3.0]`，clear 前已投递 seq1、积压 seq2 跳过、clear 后 seq3 投递） |
| ② | Clear→**进程重启**→Start（durable 持久化、新实例恢复） | `test_clear_then_new_process_instance_resumes_durable` | **PASS**（全新连接的第二个 NatsSinkConsumer 实例重绑同名 durable：`[1.0, 3.0, 4.0]`，不重放、不漏投） |
| ③ | 积压隔离（清 A 不影响 B） | `test_clear_backlog_isolation_between_durables` | **PASS**（A=`[1,3]` 跳过 seq2；B=`[1,2,3]` 全收） |
| ④ | 历史消息边界（clear 前跳过、clear 后正常） | 同 ①，断言精确到值序列 | **PASS** |
| ⑤ | 重复 Clear 不损坏配置/残留 | `test_repeated_clear_leaves_healthy_consumer` | **PASS**（连续 clear 均返回 0，恢复后收新消息 `[1.0, 2.0]`） |
| ⑥ | 异常路径（断连/consumer 创建失败不得假成功/静默） | `test_clear_requires_stopped_consumer`、`test_open_fails_fast_when_nats_unreachable`、`test_clear_fails_when_stream_missing` | **PASS**（分别抛 RuntimeError / 连接异常 / 管理异常；`connected` 不为真） |

回归项（同文件真实 NATS）：

| 回归点 | 用例 | 结果 |
|---|---|---|
| ACK 不重复投递 | `test_reliable_message_acked_without_redelivery`（超 ack_wait 1.6s 后仍 1 次） | **PASS** |
| NAK 指数退避重投，最终仅落一次 | `test_failed_write_is_redelivered_with_backoff_then_acked`（attempts==3，delivered==1） | **PASS** |
| 两 durable 独立收全量 | `test_distinct_durables_each_receive_the_full_stream` | **PASS** |
| realtime/reliable 互不影响 | `test_realtime_core_subscription_independent_of_reliable`（Core NATS 收 1.0、JS 收 2.0，subject 隔离） | **PASS** |

### 3.1 复测首轮失败的定性（重要，非生产缺陷）

修复后首轮保留测试仍失败，根因有二，均已查明并以测试卫生解决，**未改生产、
未用 skip/xfail/降断言**：

1. **服务端残留 Round 2 坏 durable**：`durable_clear2` 是 Round 2 旧（缺陷）
   `clear()` 在 NATS 服务端创建的 `deliver_subject=None, policy=new` 残留
   consumer，open() bind 到它必抛 TypeError。删除残留后 TypeError 消失。
   独立复现（全新 durable）显示修复序列 open→close→clear→resume 全部成功，
   服务端重建后 `deliver_subject='_INBOX.<新 hex>'、policy=new`。
2. **持久化 stream 的历史污染**：WH_HISTORY 跨运行保留消息，固定 subject +
   首次 `DeliverPolicy.ALL` 会让用例收到历史运行的 seq1/seq2（实测
   `[1,2,1,2,1,3]`）。修复方式为测试夹具卫生：每个用例唯一 point（唯一
   filter subject）+ 唯一 durable 名 + 开始前尽力删除同名 durable。断言强度
   不变（仍精确断言完整值序列），连续两轮执行 11/11 可重复。

---

## 4. 双进程主链路（真实 NATS + 真实 Redis）

```bash
WIND_HUB_TEST_NATS=nats://127.0.0.1:4222 WIND_HUB_TEST_REDIS=127.0.0.1:6379 \
  pytest tests/system/acquisition/test_modbus_data_flow.py -m real_service -v
# 2 passed in 6.35s
```

- `test_collected_points_reach_redis_via_nats`：真实 collector 子进程 →
  Core NATS → 真实 sink_service 子进程 → 真实 Redis，四点位 good、
  rotor_speed≈1200.5。**PASS**
- `test_stop_task_prevents_further_redis_updates`：停任务后 Redis 时间戳稳定。
  **PASS**（同时覆盖管理面 Start/Stop 不产生重复投递）。

环境：Docker `nats:2-alpine -js`（4222）、`redis:7-alpine`（6379）。

---

## 5. 静态四门

| 门禁 | 命令 | 结果 |
|---|---|---|
| Ruff lint | `ruff check src/ tests/ scripts/` | **PASS**（All checks passed） |
| Ruff format | `ruff format --check src/ tests/ scripts/` | **PASS**（610 files already formatted） |
| Mypy | `mypy src` | **PASS**（Success: no issues found in 324 source files） |
| Import Linter | `lint-imports` | **PASS**（11 kept, 0 broken） |

本轮唯一 format 漂移即新测试文件自身，已 format 后提交（RELATED，已清零）；
未做全仓格式化。main 既有 format 基线问题本轮未在上述三路径复现。

---

## 6. 全量回归与 RELATED / UNRELATED 区分

`pytest -q`（无 marker 裁剪，1524 collected）：**1477 passed, 5 failed,
29 errors, 3 skipped, 10 deselected**。逐项定性：

| 类别 | 数量 | 性质 | 与 8fd9e217 关系 |
|---|---|---|---|
| system 子进程夹具旧 schema（rpc 11 + recovery 16） | 27 ERROR | 夹具仍写 `tasks.yaml.outputs: [{sink_id: ...}]`，新 parser 正确拒绝：`ConfigError: task 'modbus-telemetry' unknown keys: ['outputs']`（迁移到 `sinks.yaml.outputs` 的机械迁移漏网，子进程日志举证） | **UNRELATED（对本次修复）/ RELATED（对分支迁移）**。parser 与夹具在 `7170a6da..HEAD` 零改动（§1 同一性举证），Round 2 基线同态失败；Round 2 报告的 1335 全绿来自带裁剪的门禁选择（178 deselected），并非全收集。 |
| soak 目标负载 | 4 FAILED | 夹具点 ID 含点号（`rotor.speed` 类），新 SampleMessage subject token 校验抛 `ValueError: invalid NATS subject token`。同一夹具迁移漂移类。 | 同上（基线同态）。 |
| industrial_gateway FC15 | 1 FAILED | `test_fc01_readback_matches_point_store`；main 既有 flake/基线项（多轮验收在案）。 | **UNRELATED（main 基线）** |
| 双进程主链路（无外部服务变量时） | 2 ERROR | fixture 自动 `docker compose up` 与本机已在运行的 `wind-hub-test-redis` 容器名冲突（ComposeError）；指定 `WIND_HUB_TEST_REDIS` 外部实例时 **2 passed**（§4）。 | **本机环境工件，非代码缺陷** |

未通过项均非 P1 修复引入；未以任何 skip/ignore 将其在本轮静默。

---

## 7. 新增/修改测试与提交 SHA

| SHA | 内容 |
|---|---|
| `5a2f86da41b109cae44fc806d5fe1c14d8f4feca`（已推送） | `test(sink_service): harden JetStream clear() suite with unique subjects and lifecycle coverage`：唯一 subject/durable + 预清理卫生；新增 A/B 积压隔离、Clear 后新进程实例重绑、重复 clear、运行中 clear/断连/缺 stream 三条 fail-closed 路径、realtime/reliable 独立性；P1 保留用例转为修复后契约断言（值序列精确匹配）。 |

生产代码：0 改动。未建 PR。

---

## 8. Round 2 P2 状态（仅记录，本轮不改生产）

1. 前端仍调已删 Collector `verifySink`/`write-test`（MSW mock 掩盖）——**状态不变**。
2. Redis CAS 等时冲突/迟到旧值/PXAT 仅代码核实——**状态不变**；本轮双进程
   真实 SET 链路继续 PASS。
3. `SinkRuntime.latest()` 死路径——**状态不变**。
4. TDengine / gRPC 下行 / Modbus 下行 / Kafka 真实链路——**NOT_RUN**（本机无
   镜像/对端；未以 Mock 冒充）。
5. sink 诊断 CLI 对真实 Redis 的 real_service 验证——**NOT_RUN**（仅 CLI 层测试）。
6. （新记录，建议 ChatGPT 排期）system/soak 子进程测试夹具的 sink 拆分迁移漂移：
   27 项 `tasks.outputs` 旧写法 + 4 项点号点号，全量收集下红，门禁裁剪选择外不可见。

---

## 9. P1 是否关闭

**关闭。** Round 2 P1（`clear()` 重建 push durable 缺 `deliver_subject` →
subscribe_bind TypeError、ClearSink 后含进程重启永久断消费）由 `8fd9e217`
两行修复闭合：open/clear 配置兼容、NEW 策略边界正确、ACK 参数不变、跨进程
durable 重绑成立、异常路径 fail-closed，全部以**真实 JetStream 运行时结果**
判定（11/11，双轮可重复），并由真实 NATS+Redis 双进程主链路回归佐证。

---

## 10. 总结论与合并意见

**PASS WITH LIMITATIONS：P1 复测通过，修复可以接受；是否 Squash Merge 取决于
对分支既有测试债务的处置策略。**

- 就 Round 3 任务书范围（P1 单点复测 + 六项生命周期 + 回归 + 四门）：**全部
  满足**，真实服务均实测（NATS/Redis），无 Mock 冒充，四门全绿。
- 限制项不来自本修复：全量收集下 31 项 system/soak 夹具迁移漂移在 Round 2
  基线同态存在（建议 ChatGPT 作为独立测试修复任务，按 sinks.yaml/点 token
  机械迁移，不应由验收方代改以维持职责边界）；FC15 为 main 基线；TDengine/
  gRPC/Modbus 下行/Kafka/soak 长稳仍 NOT_RUN，按既定发布/硬件门禁处理。
- 本轮停止在报告阶段：未改生产业务逻辑、未合并、未建 PR，等待 ChatGPT 审核。

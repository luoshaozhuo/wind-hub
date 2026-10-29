# Sink Service Round 6：TDengine D5-01 独立复测报告

- 分支：`feat/nats-sink-service`（未建 PR、未合并、未 rebase、未 force push）
- 复测生产 HEAD：`3cb4374267c3ccf997e42fdcf28c753d5b362a76`
- merge-base（origin/main）：`cb380ddb3bee249defb75e5922e2b70987a18211`
- 复测日期：2026-10-29
- 上游修复（3 个提交，本轮前已在分支）：
  - `3ea542e0` test(r5): pin TDengine to composite-key capable 3.3.8.8
  - `b04176fc` test(r5): document updated TDengine test baseline
  - `3cb43742` fix(sink-service): explain TDengine composite-key version requirement on startup
- 总结论：**FAIL**（见第 9 节裁定）。

<!-- 本轮 3 个提交：fixture pin 修正 + r5_02 测试缺陷修复 + R6 契约测试 + 报告，SHA 见 git log -->

## 0. 复测方法与诚实性说明

- 所有结论以**真实容器内运行版本** `SELECT SERVER_VERSION()` 为准，不以镜像标签为准。
- 任务书指定的镜像 `tdengine/tdengine:3.3.8.8` **在 Docker Hub 不存在**
  （`name=3.3.8` 查询 count=0，3.3.7 同样为 0；公开 3.3 线最新为 3.3.6.13）。
  为在真实服务上完成契约复测，将公开存在的最新镜像 `tdengine/tdengine:3.3.6.13`
  **本地打别名**为 `3.3.8.8`（同一 image ID），再驱动未修改的生产夹具；
  服务端实证版本为 `3.3.6.13`。别名仅本机存在，干净 CI 上该标签仍不可拉取。
- 测试侧另把夹具 pin 修正为真实存在的 3.3.6.13（属允许的 Fixture 修复，见提交清单）。

## 1. Git 状态

| 项 | 值 |
| --- | --- |
| 复测起点 HEAD | `3cb4374267c3ccf997e42fdcf28c753d5b362a76` |
| origin 同分支 HEAD | 一致（已 fetch 核对） |
| merge-base | `cb380ddb3bee249defb75e5922e2b70987a18211` |
| 本轮新增 | 测试修复 + R6 契约测试 + Fixture 版本修正 + 本报告（仅测试/文档） |
| 生产代码 | 本轮零修改（3cb43742 的 4 行错误信息改动属上游，非本轮） |

## 2. Docker 镜像与真实数据库版本

| 服务 | 镜像（固定版本） | 真实运行版本 |
| --- | --- | --- |
| TDengine | `tdengine/tdengine:3.3.6.13`（本地别名 3.3.8.8 驱动原夹具） | **3.3.6.13** |
| TDengine 发行版 | `SHOW GRANTS` = **TDengine Community Edition**；`SHOW CLUSTER` = `community` | — |
| NATS | `nats:2.10-alpine`（JetStream） | 容器内 2.10.x |
| Redis | `redis:7-alpine` | 7.x |
| Kafka | `apache/kafka:3.9.1`（KRaft） | 3.9.1 |

独立 compose project（`wind-hub-r5-<rand>`）、动态 loopback 端口、healthcheck，
teardown `down -v --remove-orphans` 只清本 project；未执行全局 prune，
未触碰用户既有容器（`wind-hub-test-redis`、`wind-hub-test-nats`）。

**D6-01（RELATED，阻断性）：上游夹具 pin 的 `tdengine/tdengine:3.3.8.8` 标签
在 Docker Hub 不存在。** 干净环境 `docker compose up` 必失败：

```
Image tdengine/tdengine:3.3.8.8 Error failed to resolve reference
"docker.io/tdengine/tdengine:3.3.8.8": not found
tests/fixtures/services/r5_stack.py:170: R5StackError
```

公开 3.3 发行线最新为 3.3.6.13（Docker Hub API 分页核实）。本轮已把夹具
pin 修正为真实存在且实证支持复合键的 3.3.6.13；但生产 open() 错误信息声称
"verified target >= 3.3.8.x"（tdengine.py:113-115）同样引用了不存在的公开发行线，
实际验证下限为 3.3.6.x——生产字符串未改，留待裁定。

## 3. R6-1：DDL 验证

生产 DDL（`src/sink_service/infrastructure/adapters/tdengine.py:102-108`）：

```sql
CREATE STABLE IF NOT EXISTS wind_hub_r5.point_values (
  ts TIMESTAMP, sample_id VARCHAR(192) COMPOSITE KEY,
  value_json NCHAR(2048), quality NCHAR(32),
  receive_time TIMESTAMP, device_time TIMESTAMP, source NCHAR(64))
TAGS (device_id NCHAR(192), point_id NCHAR(192))
```

在真实 3.3.6.13 Community 上：

- DDL 执行 **code=0 成功**；生产 `TDengineSink.open()` 成功，后续超级表/子表查询正常。
- `DESCRIBE point_values` 实证 `sample_id` 行 note = **`COMPOSITE KEY`**
  （真实复合主键列，非普通列；ts 仍为 TIMESTAMP 主键）。
- 旧版 3.3.5.0（本地已有镜像，独立容器复核）：生产 `open()` 明确抛
  `SinkError: TDengine open failed (requires a server supporting VARCHAR
  COMPOSITE KEY, verified target >= 3.3.8.x): TDengine SQL rejected: syntax
  error near "key, ..."`——**fail-closed，不假成功**，符合 R6-1 旧版要求；
  错误信息已含版本指引（但下限数字不准确，见 D6-01）。

## 4. R6-2：四种复合主键冲突场景（全部真实 SQL 实证）

探针与固化测试：`tests/system/r5/test_r6_tdengine_contract.py`
（直接 REST SQL 到真实 taosAdapter，生产子表/超级表结构）。

| 场景 | SQL 结果（3.3.6.13 Community 实测） | 契约判断 |
| --- | --- | --- |
| 相同 ts、不同 sample_id | **2 行**，各自值保留 | ✅ 符合复合主键语义 |
| 相同 ts、相同 sample_id、相同值重写 | **1 行**，内容一致（第二次写 code=0） | ✅ 严格重复投递结果幂等 |
| 相同 ts、相同 sample_id、**不同值**重写 | 第二次写 **code=0 被接受，原已确认记录 100.0 被覆盖为 200.0**，最终 1 行 | ❌ **见 D6-02** |
| 相同 sample_id、不同 ts | **2 行历史均保留**（身份是 (ts, sample_id)，sample_id 非全局唯一键） | ✅ 与业务契约一致 |

### D6-02（RELATED，契约风险，稳定失败测试已保留）

`test_redelivery_with_different_value_must_not_overwrite_confirmed_record`
稳定失败，失败原文：

```
CONTRACT VIOLATION: (ts, sample_id) UPSERT overwrote the confirmed record
100.0 -> 200.0; TDengine COMPOSITE KEY is not first-write immutable on this server
```

- 架构 v0_4 行 434 对「重复」的规定是「**可去重 / 丢弃**，消费幂等，容忍至少
  一次投递」，行 483「Sink 对 sample_id 建立目标侧幂等依据」，行 532
  「重投时以 sample_id 幂等处理」。TDengine COMPOSITE KEY 实测提供的是
  **UPSERT（后写覆盖）而非首写不可变/丢弃**，与「去重/丢弃」字面条目不符。
- 可达性评估（如实记载）：JetStream 重投只重发**同一条消息**（内容字节相同），
  此时 UPSERT 结果与原值一致，正常 at-least-once 重传路径不发生数据变化；
  同 (ts, sample_id) 不同值需要两条身份相同但内容不同的消息（sample_id 生成
  缺陷/外部直接写库等），当前正常链路不可达。但「数据库最终只有一条记录」
  不等于任务书要求辨别的严格不可变幂等——数据库层未提供该保证，生产代码也
  无应用层去重。是否接受 UPSERT 语义或要求首写不可变，交 ChatGPT 裁定；
  在此之前按任务书判 **RELATED FAIL**，期望值未被弱化。

## 5. R6-3 / 原 D5-01 七项测试

命令：`pytest tests/system/r5/test_r5_02_reliable_tdengine.py -v`
**7/7 PASSED（真实 JetStream + 真实 TDengine SQL 实证，34s）**。

| 用例 | 结果 | 证据 |
| --- | --- | --- |
| ddl_composite_key_accepted | PASS | open 后 `SELECT sample_id ... LIMIT 0` 成功 |
| reliable_writes_persist_with_sql_evidence | PASS | SQL 查得 3 行顺序/值/quality 一致，pending=0，子表计数=3 |
| duplicate_redelivery_is_idempotent | PASS | 两次发布同 (ts,sid)，pending=0 且仅 1 行 |
| tdengine_outage_nak_then_redelivered | PASS | stop tdengine→failed（failure 含 TDengine）→start→running 后补齐，pending=0、failure 清空 |
| process_restart_same_durable_redelivers_unacked | PASS | 故障期发布后终止 sink 进程，同 deployment 新进程绑同名 durable，未 ACK 消息重投落库，pending=0 |
| backlog_while_stopped_delivered_on_start | PASS | StopSink 期 3 条积压不落库，Start 后全部补齐 |
| two_durables_isolated_and_clear_skips_own_backlog | PASS | 双库各收全量；ClearSink 报告跳过自身积压≥2，A 不收 r5clear-*，B 收到，既有 r5live 不删 |

### 本轮修复的两处测试缺陷（Round5 被 DDL 阻断掩盖，从未执行到）

1. **reliable 信封误带 `max_staleness_ms`**：r5_02 经 `make_sample` 默认带
   60000ms，而生产契约 `src/contracts/sample_message.py:95` 规定 reliable 消息
   必须省略该字段（真实 Collector 同此），消息在投递层被拒
   （`delivery rejected ... reliable message must omit max_staleness_ms`）。
   新增 `_rsample()` 包装显式置 None。属测试夹具缺陷，非生产缺陷。
2. **进程重启用例等待错误初始态**：原断言新进程 Sink 先 stopped 再 StartSink，
   但 `--reliable-sink` CLI 参数本身就是启动意图，新进程直接 running。
   改为等待 running（durable 重绑语义不变，未 ACK 重投仍由后序断言实证）。

## 6. JetStream ACK/NAK、重投、Durable 隔离证据

- 显式 ACK：全部落库用例最终 `pending == 0`（管理面真实查询，非消息 ACK 代替）。
- NAK 退避重投：`tdengine_outage` 用例 stop 容器期间 Sink failed，start +
  taosAdapter 就绪后 90s 内自动 running 且行补齐（NAK 退避 1/2/4s… 上限 60s）。
- 未 ACK 不丢：`process_restart` 用例杀进程后同 deployment 新进程收到重投。
- Durable 命名按 deployment 隔离（assembly 规则
  `sink_<sha256(deployment:farm:sink)[:32]>`）；两 deployment 各落各库，
  ClearSink 只跳过本 durable 积压且不影响另一 durable、不删目标端数据。

## 7. 质量门禁

### 静态门禁

| 门禁 | 结果 |
| --- | --- |
| `ruff check src tests scripts` | **PASS** |
| `ruff format --check src tests scripts` | **PASS（621 files）** |
| `mypy src` | **PASS（324 files, no issues）** |
| `lint-imports` | **PASS（11 kept, 0 broken）** |

### R5 全部真实服务套件（镜像升级回归）

`pytest tests/system/r5/`：**35 passed, 1 failed**。唯一失败为 D6-02 的稳定
契约红；R5-1 Redis(8)/R5-3 Kafka(4)/R5-4 gRPC(3)/R5-5 Modbus(5)/R5-6
故障矩阵(3) 及 R5-2(7)、R6 契约其余 5 项全部通过——TDengine 升级未影响
共用 Docker 测试栈。

### 全量 pytest

`pytest -q --ignore=tests/web_admin`（不做 marker 裁剪）：**1526 passed,
2 failed, 19 errors, 3 skipped（564.68s）**。

- failed ×2：
  1. `test_r6_tdengine_contract.py::test_redelivery_with_different_value_must_not_overwrite_confirmed_record`——**RELATED（D6-02）**，刻意保留的稳定失败；
  2. `test_gateway_r2_fc15.py::test_fc01_readback_matches_point_store`——**UNRELATED**（main 基线 FC15）。
- errors ×19（acquisition 2 + recovery 17）：**UNRELATED（本机环境）**，
  仍是验收前已存在的用户容器 `wind-hub-test-redis` 占用 `0.0.0.0:6379`，
  共享 compose 夹具 `Bind ... port is already allocated`。仅清理本轮产生的
  `wind-hub-test-redis-1`/空网络后，以既有真实 Redis/NATS 作外部地址复跑：
  `WIND_HUB_TEST_REDIS=127.0.0.1:6379 WIND_HUB_TEST_NATS=nats://127.0.0.1:4222
  pytest tests/system/acquisition/test_modbus_data_flow.py tests/system/recovery/`
  → **19 passed in 117.27s**。
- 与 Round5（1514 passed/8 failed/19 errors）对比：原 R5-2 7 个 RELATED 红
  全部转绿；新增 R6 契约 6 项中 5 绿 1 红（D6-02）；无其他新增失败。
- 移除本地 3.3.8.8 别名后，以真实 `tdengine/tdengine:3.3.6.13` 标签复跑
  r5_02(7) + r6 契约(6)：12 passed / 1 failed（D6-02），与别名期完全一致，
  证明结论来自真实 3.3.6.13 而非标签。

### Web Admin 门禁（src/web_admin，本轮无前端改动）

| 门禁 | 结果 |
| --- | --- |
| `npm run lint` / `format:check` | PASS / PASS |
| `npm run build`（vue-tsc + vite） | PASS |
| vitest | **185 passed（28 files）** |
| `check:api`（OpenAPI 漂移） | PASS（DRIFT CHECK: PASS） |
| `test:e2e:mock`（Playwright） | 5 passed / **1 failed——UNRELATED main 基线** |

E2E 失败仍为 `mock.spec.ts:48` 对 Sinks 表含 `file_archive` 的陈旧断言
（PR #92 移除 FileSink 后未更新；origin/main 逐字同测，mock handlers 零
file_archive），与 Round5 记录相同，本轮不扩大范围。

## 8. RELATED / UNRELATED / NOT_RUN

**RELATED**

- D6-01：pin 的 3.3.8.8 公开镜像不存在，干净环境栈起不来（本轮已修夹具 pin；
  生产错误信息中的 ">= 3.3.8.x" 版本下限同样不实，未改生产）。
- D6-02：COMPOSITE KEY 为 UPSERT 语义，同 (ts,sample_id) 不同值覆盖已确认
  记录，首写不可变/丢弃契约不成立；稳定失败测试保留。

**UNRELATED（main 基线/本机环境，证据附后）**

- FC15 `test_gateway_r2_fc15.py::test_fc01_readback_matches_point_store`
  （未映射 slave 读线圈行为，历轮基线）。
- Playwright mock E2E file_archive 断言陈旧（PR #92 移除 FileSink 后未更新）。
- 全量首轮 recovery/acquisition 若出现 compose 6379 端口冲突 ERROR：本机用户
  既有容器 `wind-hub-test-redis` 占用，处理方式与 Round5 相同（外部真实服务
  环境变量复跑），非本轮变更引入。

**NOT_RUN**

- netem 受控网络 7 项：unshare netns 内 docker socket 不可达（同 Round5
  环境边界），3 项设施自检 PASS。
- 真实硬件/soak/性能门限：范围外。

## 9. D5-01 是否真正关闭 & Squash Merge 条件

**D5-01 未真正关闭，总结论 FAIL。** 分层结论：

1. ✅ 原 DDL 阻断（3.3.5.0 不支持 COMPOSITE KEY）在真实最新公开 Community
   **3.3.6.13** 上消除：DDL 成功、sample_id 确为复合键列、7 项端到端行为
   （ACK/NAK/重投/进程恢复/积压补齐/双 Durable 隔离/ClearSink）真实 SQL 实证通过。
2. ❌ 但上游修复 pin 的镜像标签 **3.3.8.8 在 Docker Hub 不存在**，提交状态在
   干净环境无法启动栈（本轮已在夹具层修正为 3.3.6.13 并实证）。
3. ❌ 数据库层复合键是 **UPSERT 而非首写不可变**，与 v0_4「去重/丢弃」措辞
   存在契约缺口（D6-02，正常同消息重传路径不可达，但保证未由数据库/应用提供）。

**不具备 Squash Merge 条件**，待 ChatGPT：(a) 确认版本策略（接受 3.3.6.13
或给出真实可得的目标版本）；(b) 裁定 D6-02——首写不可变是否为硬契约：
若是，需要应用层去重/拒绝覆盖；若允许 UPSERT，应回写 v0_4 明确语义并更新
失败测试的契约表述。本轮不建 PR、不合并，等待审核。

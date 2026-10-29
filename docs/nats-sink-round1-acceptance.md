# wind-hub 数据分发与 Sink Service 独立验收 · Round 1

- 日期：2026-10-29
- 验收方：Claude Code（独立验收；仅改生成物/格式/测试，不改生产逻辑）
- 分支：`feat/nats-sink-service`
- 生产基线：`52017f70f1a5db5ce0a95c40cc3da6ee7d5fcb9f`（= origin HEAD）
- merge-base：`cb380ddb`（origin/main，PR #105）
- 验收提交：`c000a83f`（RPC 生成物同步）、`813c30e9`（ruff/mypy 机械修复，含本报告与设计文档入库）、`e786ebc1`/`95084a7b`/`b863ae25`（报告与仓库清理）、`1f21f5eb`（OpenAPI 生成物同步）
- **结论：FAIL**——主数据通路不可用、测试门禁中断、多处迁移残留。

---

## 1. 工作区与静态检查

| 项目 | 命令 | 基线 52017f70 结果 | 处置后 |
|---|---|---|---|
| 环境 | `python scripts/dev.py env` | PASS（3.11.16 wind-hub） | — |
| RPC 生成物 | `python scripts/generate_rpc.py` | **漂移**：sink_pb2 四件套未提交；collector_pb2* 陈旧（旧 Sink RPC 已从 proto 删除但生成物仍含符号） | c000a83f 同步，二次执行无漂移 |
| Ruff Lint | `ruff check src tests scripts` | **22 errors**（I001×15、E501×4、B904×2、SIM105、F401） | PASS（813c30e9） |
| Ruff Format | 分支相关 .py 逐个比对 merge-base | **30 文件漂移，全部 RELATED**（22 个新文件脏 + 8 个改旧文件引入新漂移），无 main 基线文件 | 全部格式化 |
| Mypy | `mypy src`（strict） | 装 nats-py 后 19 errors → 机械修复后 **6 errors 残留**（均为生产逻辑，D-1/D-6） | 不掩盖，出报告 |
| Import Linter | `lint-imports` | 11/11 KEPT | — |
| pytest 收集 | `pytest --collect-only -q` | 1443 collected，**5 errors 中断** | D-2 |

环境注记：`pyproject.toml` 新增 `nats-py ^2.10`，但 **`poetry.lock` 未更新**（grep 零命中）；验收机临时 pip 安装 nats-py 2.16.0 供静态检查。见 D-7。

全量幸存测试（`--continue-on-collection-errors`，25 分钟）：**188 failed / 1150 passed / 100 errors**。定向重跑（collector/core/web_server/system/tooling/cli，25 分钟）：190 failed / 873 passed / 100 errors（范围不同，数字不直接可比）。抽样定性：主导签名是陈旧测试契约（`Task(sink_ids=)`、`CollectorConfig(sinks=)`、fixture 写旧 `outputs`、ci_gate 指向已删 tests/collector/sink、rpc_boundary 未含 sink_service）——RELATED，属下一轮测试重建；另有 D-1b 同源模板测试 7+ 项；openapi 生成物漂移已由提交 1f21f5eb 修复（3 passed）。system/recovery 约 20 项已抽样闭环：test_watchdog_restore 直接 `Task(sink_ids=)`（:45）；test_graceful_shutdown 等子进程类失败表现为 grpc ready 30s 超时，根因是 tests/support/new_collector.py:363、functional_config.py:52 及测试内联仍写旧 `outputs` 字段 → 加载期 ConfigError 进程不起。**全部 RELATED**，随测试/夹具重建消除；未见独立生产缺陷信号。

## 2. 设计符合性矩阵（v0_4 第 4～13 章）

| 设计条款 | 结论 | 证据 / 缺陷 |
|---|---|---|
| §4 Collector 只采集发布，不持有 Sink | 符合 | 旧 SinkRuntime/SinkRegistry/collector sink RPC 均删净；collector/infrastructure 仅剩 nats_publisher/config/grpc |
| §4 Sink Service 独立进程、同构代码支持单/双进程 | 符合 | main.py `--delivery realtime/reliable/both` + `--sink-ids` 不相交校验；compose/Dockerfile 自洽 |
| §4 协议适配器零改动 | 符合 | `src/core/infrastructure/protocol/{ads,iec104,modbus}` diff 为空 |
| §5.1 实例单一 delivery XOR | 符合 | settings.py Literal + SamplePublisher 构造校验 |
| §5.1 realtime 不补历史/不旧覆新 | 部分符合 | runtime 按 sample_time 拒旧；但**未来时间/旧值拒绝全部静默无报告**（P2）；Redis 物理过期删除而非"保留+标记失效"（P2） |
| §5.2 配置阶段拒绝同点多权威 | **部分违反** | 仅单 Sink 内两 Task 被拒（且检查代码重复两遍）；跨 Sink、跨 Collector 漏网 → D-4 |
| §5.2 有效期随消息携带、正值上限、无消息读时失效 | 部分符合 | 信封 gt=0/le=86400000 校验齐备；`latest()` 读时判定 API **无任何调用方**，Redis 用 PXAT 删除替代显式 stale |
| §5.3/§7.2 发布以 PubAck 为界、失败继续采集可丢当前采样 | **违反** | publish 根本不存在 → D-1；无 JetStream publish、无 Nats-Msg-Id |
| §6.2 版本化单样本信封 | 基本符合 | UTC 归一、NaN/Infinity 拒绝（pydantic + allow_nan=False 双保险）、extra=forbid、错误版本拒绝；schema_version 有默认值可缺省（P2，设计列必要字段） |
| §6.2 sample_id=collector+boot+单调序号、重启不复用、重试复用 | 部分符合 | boot uuid4 + 单调序号正确；无重试路径（D-1 下无从谈起），未来实现若按当前结构重试会发新 ID（P1 提示） |
| §6.2 等时不同 sample_id 拒绝覆盖并报告 | 部分符合 | runtime 拒绝正确，但只落日志、无状态/指标上报 |
| §6.3/§10.3 地址与身份分离、不兼容组合配置期拒绝 | **部分违反** | reliable 非 tdengine 拒绝（好）；realtime 不拒 kafka/modbus 等 append-only/非最新值目标；显式 key 别名冲突无校验（P2） |
| §7.1 wh.rt/wh.hist 隔离，stream 只绑 hist | 符合 | nats_init 只建 `wh.hist.>`，现存 stream subjects 不符直接报错 |
| §7.1 token 安全校验 | 符合（小缺口） | 发布侧启动期全 token 校验、信封字段校验；消费侧未再校验 subject token（P2） |
| §7.1 每实时 Sink 独立订阅（无 queue 组） | 符合 | Core NATS subscribe，无 queue |
| §7.1 durable 独立稳定含部署命名空间 | **部分违反** | `sink_+sha256(farm:sink_id)` 稳定唯一但无部署命名空间 → D-5 |
| §7.1 订阅过滤到 collector/device/点 | 偏差（P2） | 实订风场级 `wh.rt.<farm>.>`，过滤在应用层；放大流量，不影响正确性 |
| §7.2 写成功才 ACK、失败/不确定不 ACK | **符合** | nats_consumer.py:96-103，CancelledError 穿透不 ACK；无错误 ACK 路径 |
| §7.2 write 全成功或抛错 | 基本符合 | 运行时恒单点 batch + 单写锁 + 首错即抛；**gRPC sink 丢弃 updated 计数** D-8；Modbus 批内撕裂 D-9 |
| §7.2 AckWait/重投/人工阈值，不得静默终结 | **违反** | 毒消息无限 1s NAK + max_ack_pending=1 队头阻塞，状态面仍健康 → D-3 |
| §8 不凑批、不本地持久化 | 符合 | 无 BatchProcessor；故障期采集不阻塞（但全部计 dropped） |
| §9.1 端口归属 sink_service | 符合 | application/port + infrastructure 分层，import-linter KEPT |
| §9.2 write 返回前完成确认 | 部分符合 | Redis/TDengine/Kafka 网络层确认真实；TDengine 不核对影响行数（P2） |
| §9.4 生命周期与写入互斥 | **违反** | close 非 drain、直接 cancel 在途回调 → D-2（消费侧） |
| §10 sinks.yaml 不定义 delivery | 符合 | 由进程启动参数决定 |
| §11.1 gRPC 只读状态（含失败原因/积压） | 部分符合 | pending/connected/state 有；decode 失败与拒绝类不进状态（D-3） |
| §11.2 Stop 等在途、Clear 只清自己 durable 并返回跳过数 | 部分符合 | Clear 语义正确（删己 consumer + DeliverPolicy.NEW 重建，无 stream purge）；**Stop 不 drain** D-2(消费侧) |
| §11.2 用户 Stop 不被监督复活 | 符合 | intent marker + desired_running 双保险，supervisor/repair/启动三处尊重 |
| §13.3 多 Sink 隔离 / Web 读真实状态 | 符合（有缺口） | durable/consumer/锁各自独立；前端生成物仍引用已删 write-test（P2） |

## 3. 缺陷清单

### P0

#### D-1　Collector NatsPublisher 是未完成桩：无 publish()/close()，主数据通路全断
- 位置：src/collector/infrastructure/nats_publisher.py:16-52（全文件仅 `__init__/open/_connect_loop`）
- 证据：
  - mypy：`assembly.py:143: NatsPublisher is missing PublisherPort protocol members: close, publish`
  - 运行时复现（/tmp/wh_acceptance/repro_p0_publisher.py，-I 执行）：
    `REPRO publish: AttributeError -> 'NatsPublisher' object has no attribute 'publish'`
    `REPRO close: AttributeError -> ... no attribute 'close'`
  - `SampleMessage.subject()`（sample_message.py:72）全 src 零调用；realtime/reliable 传输层无差异；无 JetStream publish/PubAck/Nats-Msg-Id。
- 后果：nats_dispatch.py:91-99 中异常被捕获 → **所有样本恒 dropped、points_published 恒 0**，Sink 端永远收不到消息；runtime.py:244 停机调 close 同样 AttributeError，Collector 停止流程失败。
- 违反：§5.3、§6.2、§7.2、§8.2。

#### D-2（测试门禁）　pytest 收集中断 + 契约测试失败，fast/PR gate 必红
- 5 个收集 error（均 RELATED，本分支删除了被测依赖）：
  - tests/collector/test_engine.py:10 import `SinkDelivery`（已删）
  - tests/core/diagnostic/test_cli_diag.py:18 import `core.infrastructure.diagnostic.sink`（已删）
  - tests/support/soak/runner.py:34 import `tests.support.soak.sinks`（已删）→ 连带 test_long_running/test_target_load/test_soak_duration_override
- tests/collector/rpc/test_collector_contract.py:29-32 `_RUNTIME_METHODS` 仍固化 ListSinks/VerifySink/WriteTestSink，实跑 **1 failed, 7 passed**。
- 全量基线（幸存测试）：188 failed / 1150 passed / 100 errors（25 min），定性分类见第 1 节末段。

#### D-1b（配置 P0）　出厂 configs/tasks.yaml 是非法 YAML，默认部署必启动失败
- 位置：configs/tasks.yaml:17——注释行被写入**字面 `\n` 转义序列**（单行长文本包含 `\n# 输出目标…Source→Address…\n# 实例注册…`）。
- 复现：`python -c "import yaml; yaml.safe_load(open('configs/tasks.yaml'))"` →
  `yaml.scanner.ScannerError: mapping values are not allowed here, line 17, column 6`；全 configs/ 仅此一文件损坏（10 个 YAML 逐一校验，其余 9 个 OK）。
- 影响：任何用出厂配置树的启动在配置加载期失败；deploy/sink-service/compose.yaml:57/87/115 默认以只读挂载 `${WIND_HUB_CONFIG_DIR:-../../configs}`，文档化默认部署直接不可用。测试侧 test_template_configs 复制该文件做模板种子，7 项失败同源。
- 属生产配置文件，按分工不代为修改；修复仅需将字面 `\n` 还原为真实换行。

### P1

#### D-2c（消费侧 P1）　StopSink 不等待在途写入：close() 非 drain，直接取消写回调
- nats_consumer.py:151-160：realtime `unsubscribe()` 后所有路径 `client.close()`。nats-py 2.16 实测 Client.close 立即 cancel 订阅 wait 任务（aio/client.py:836-844），只有 drain() join pending 队列。
- runtime.py:129-133 先 close consumer（取消在途 write）再取写锁关适配器，§9.4 互斥与 §11.2"等待在途写入完成"双违反。
- 缓解：可靠链路靠 TDengine sample_id 幂等兜底，最坏是重复写而非丢数；realtime 在途写被取消无兜底。

#### D-3　毒消息/确定性失败导致 durable 永久队头阻塞，状态面显示健康
- nats_consumer.py:96-103 固定 `nak(delay=1.0)`，无 max_deliver/退避/DLQ/计数；assembly.py:126 `max_ack_pending=1`。
- 一条不可解析消息即 1Hz 永久重投并阻塞该 sink 全部积压；decode 在 runtime 之前，`_failure`/gRPC 状态不被触及，supervisor 只看 FAILED。
- FAILED 状态下同样 1Hz NAK 空转（runtime.py:159-161），与 supervisor 5s 修复节奏不匹配。
- 违反 §7.2"达到人工介入阈值必须告警……而非静默终结"、§11.1。

#### D-4　多权威拒绝只在单 Sink 内成立，跨 Sink/跨 Collector 漏网
- assembly.py:64-83 权威检查以单 sink 的 allowed 为作用域，且两段循环完全重复（死代码）。同点映射到两个 sink 时各自通过；collector_id 仅运行时存在于消息，配置期无跨 Collector 检查。
- 运行时 authorities 表也是逐 sink 独立（runtime.py:168-170），冲突仅在 nats_consumer 回调里 `logger.exception` 后被吞，状态仍 RUNNING。
- 违反 §5.2/§6.2"配置阶段禁止多个 Collector/Task 成为权威"。

#### D-5　Durable consumer 名称缺部署命名空间
- assembly.py:92-96 `sink_+sha256("{wind_farm_id}:{sink_id}")`。同集群双环境/双资源隔离实例同名 sink 将共享 durable 互相分摊消息，直接违反 §7.1。

#### D-6（迁移）　wind-hub CLI 三个 sink 命令必崩；迁移残留 mypy 红
- src/cli/client.py:116,123-129 调已删 `ListSinks/VerifySink/WriteTestSink/SinkRequest`（mypy 5 个 attr-defined）；main.py:55-62 仍注册命令。实测 collector_pb2 中 Sink 符号为零。
- 新入口 wind-hub-sink（management_cli.py）只有 status/list/get/start/stop/restart/clear，**两套入口断裂，旧命令未删未转调**。

#### D-7　poetry.lock 未包含新增依赖 nats-py
- 干净环境 `poetry install` 不会装 nats-py，collector/sink_service 直接 import 失败（mypy 11 个 import-not-found 同源）。需 `poetry lock`（仅 nats-py 一个新包）。

#### D-8　gRPC sink 丢弃 WriteManyResponse.updated，服务端部分成功会被 ACK
- grpc_sink.py:67-77 返回值整个丢弃；gateway.proto:16-17 `WriteManyResponse.updated` 承载实际更新点数。当前唯一服务端实现全成功或 abort（今天不触发），但跨进程契约下 `updated < len` 即静默丢数+ACK。违反 §9.2/§7.2。

#### D-9　Modbus 多寄存器点无写入原子性，主站可读到撕裂的 32/64 位值
- store.py 逐寄存器写 dict（:82-83），server.py async_getValues 无锁并发读，codec.py:59-64 一个 float32/int64 拆 2~4 word；批内编码异常留下半更新状态且仅 NAK。违反 §9.2/§9.4。

### P2（摘要）

1. **TDengine**：sample_id COMPOSITE KEY 幂等（tdengine.py:103-108）需 3.3+ 且全仓零测试证据；成功只看 code 不核对 rows（:79-85）；HTTPError 吞掉服务端 desc；非有限值静默 continue（:133-134，现被信封校验拦截不可达）；生产链路退化为每点一次 HTTP POST（批量分片逻辑不生效）；sample_id 长主机名下可超 192 字节被永久拒。
2. **Redis**：CAS `>=` 允许等时异 sample_id 覆盖且 payload 无 sample_id（redis_record.py:48-56），sink 重启丢内存防线后违反 §6.2；显式 key 别名冲突无配置期校验；allow-set 外点静默过滤返回成功；非有限值静默丢。
3. **Kafka**：write_task 忽略 task 过滤（与其他适配器不一致，handler 预过滤兜底）；消息体无 sample_id（装配仅允许 realtime，目前相容）；health 不探活。
4. **gRPC sink**：open/close/write 无锁（重复 open 泄漏 channel）；非 GOOD/未映射点静默跳过无计数。
5. **Modbus**：失败/跳过点无状态上报，last_written 照常更新；task_refs 漏配时空写成功；server serving 任务死后 health 不察；并发 start 非幂等窗口。
6. **实时语义可观测性**：未来时间/旧值/等时冲突/多权威拒绝全部只落日志无计数无状态；`latest()` 读时失效 API 无调用方；Redis 物理过期删除而非保留+stale 标记（§5.2 偏差）。
7. **nats_dispatch.py:68-71** 一次 publish 失败短路丢弃同批剩余全部样本（扩大了"允许丢当前采样"的范围）。
8. **max_staleness_ms 热变更** Collector 侧需重启才生效（构造期固化），与"从新消息生效"有差距（Sink 侧符合）。
9. **schema_version 可缺省**（有默认值），与 §6.2 必要字段表述不一致；消费侧未再校验 subject token。
10. **前端/测试残留**：OpenAPI 生成物已由 1f21f5eb 重新生成（write-test 已消失，契约测试转绿）；但 web_admin mocks/handlers.ts:179、mocks/state.ts 仍保留 write-test mock 与 SinkRuntime mock 模型（测试/开发夹具，下轮清）；tests/support/perf_runner.py:34 import 已删 null_sink（run_benchmark.py 仍引用它，一引即崩）。
11. **Web 契约变更需知会**：write-test 端点删除；verify 降级为 GetSinkStatus 状态判定且异常返回 200+success=false；**Admin 批量保存的 task→sink targets 被静默丢弃**（admin_state.py 保留字段但不写 outputs），新 outputs 映射只能手编 sinks.yaml；sinks.yaml 变更对 Sink Service 不热生效（需重启，USAGE.txt 已写明，digest 报 pending_restart）。
12. **nats_consumer**：durable close 不显式退订；ACK 自身失败再 NAK 可致重复写；AckWait 30s 硬编码与适配器超时无联动；连接参数硬编码（0.5s/3 次）；Clear 对从未启动的 durable 返回底层 NotFound 而非清晰前置条件错误，skipped 统计窗口漏新消息；realtime Clear 不报清缓存点数。
13. **陈旧文档/死代码**：collector 多处 docstring 仍称 SinkRuntime/Sink 派发；assembly 重复检查块；src/core/infrastructure/sink/ 剩两个 __pycache__ 空目录。

## 4. 真实服务验证

NOT_RUN。本轮按分工先出缺陷报告：D-1 使 Collector→NATS 端到端验证不可能成立；真实 NATS/JetStream（Docker 29.2.1 可用，compose 已备 nats:2.11）、TDengine/Redis/Kafka/Modbus/gRPC Sink 的 E2E 与双进程部署、P99 性能、soak 全部留待 ChatGPT 修复 P0/P1 后下一轮。

## 5. 修复建议（交 ChatGPT，不代为修改生产代码）

0. 修复 configs/tasks.yaml:17 的字面 `\n`（还原为真实换行），恢复出厂配置树可加载，并加配置树 YAML 合法性冒烟测试。
1. 实现 NatsPublisher.publish/close：realtime Core publish（失败返 False）、reliable JetStream publish 并以 PubAck 为成功边界、Nats-Msg-Id=sample_id、有限受控重试复用同 ID；close 走 drain。
2. 修复 5 处测试残留 + RPC 契约方法表（属 Claude 侧测试职责，下一轮随测试重建处理）。
3. Consumer：close 先停投递再 drain 在途回调；配置 max_deliver/退避，毒消息计入 sink 失败状态并经 gRPC 暴露，达阈值告警；FAILED 下停止/拉长 NAK。
4. 多权威：在 Sink Service 配置加载的全局视角（所有 sink 合并后）校验 (device,point) 唯一 Task 来源；跨 Collector 约束写入部署/配置契约并在运行时冲突时置 sink 失败可见。
5. durable 名加入部署命名空间参数。
6. CLI 删除/转调旧 sink 命令至新 Sink Service；poetry lock 更新。
7. grpc_sink 校验 updated==提交数；Modbus store 加读写锁/双缓冲保证多点寄存器原子可见。

## 6. 最终结论

**FAIL**。阻断项：D-1（发布桩，主数据通路不可用，运行时与 mypy 双证据）、D-1b（出厂 tasks.yaml 非法 YAML，默认部署启动失败）、D-2（门禁收集/契约测试红）。消费侧 ACK 纪律、Clear 语义、Stop 意图持久化、信封校验、Subject/Stream 隔离、协议边界等核心设计落地质量良好，但必须在 D-1/D-2 修复并补齐测试金字塔后重开 Round 2。

# Sink Service v1 重写 阶段二（数据面逻辑 / 边界 / 真实链路缺口）独立验收报告

- 验收日期：2026-11-05
- 交付分支：`feat/sink-service-v1-rewrite`
- 交付 HEAD：`55ff36983610d6a85100a28e680e870fef1c81fe`（== `origin/feat/sink-service-v1-rewrite`，未新增提交）
- 设计基线：`docs/Sink_Service_软件设计_v1_17.md`
- 验收人：Claude Code 独立验收
- 约束遵循：未创建 PR、未合并 main、未 rebase / force push；未升级依赖、未降低校验、未新增任何 ignore；未读取/输出 `.env.local`。本阶段所有改动仅测试/Fixture 与静态修复，生产代码零行为变更。

## 0. 本阶段结论（先行）

**总体：FAIL（Gate 5 吞吐验收目标未达成）；其中 Gate 1/2/4 为 PASS WITH LIMITATIONS，Gate 3 部分真实化。**

- **Gate 1（静态四门禁）PASS**：ruff check / format、mypy strict（src/sink_service 22 文件 0 错）、lint-imports（13 kept / 0 broken）全绿，无新增 ignore、无依赖升级、无规则降级。
- **Gate 2（单元/组件，§14.3）PASS**：`pytest tests/sink_service -m "not system"` = **128 passed, 10 deselected**。
- **Gate 3（13 RPC / 12 R04 指标 / 事件环游标 / 只读 Diagnose）部分真实化**：R04 指标词汇与 13 RPC 错误/角色契约由 128 套件中的 11 observability + 8 grpc_boundary 契约测试钉死；真实双进程 mTLS 上已实证 GetServiceInfo/GetServiceStatus/GetMetrics（每个 stage3/4 用例都在真实进程上调这三个 RPC）。但阶段一 `test_stage1_dual_process.py` 全量 13 RPC 真实双进程套件基于旧替身工厂假设、不接外部栈，在真实工厂下因 S2-1 无界重连挂死（见 §7.4），**完整 13 RPC 真实进程矩阵仍 NOT_RUN**。
- **Gate 4（真实双进程 + 真实 NATS JetStream/Redis/Kafka/Modbus，无 Mock）PASS WITH LIMITATIONS**：本会话在用户授权的 r5 固定镜像栈上新建并通过 **8 个真实链路系统用例**（见 §7），覆盖实时 NATS→Redis CAS、NATS 暂停恢复、同刻冲突保持首值、可靠 JetStream ACK→Kafka、畸形消息→DLQ+ACK、durable 重启重绑、Kafka 停容器重投不进 DLQ、Modbus TCP 真实帧。14 UC/42 场景追溯矩阵见 §9：实时/可靠主链路与关键故障场景已真实闭环，配置热生效全链路、Diagnose 真机、DLQ 满/PubAck 失败等仍 NOT_RUN。
- **Gate 5（20 sink×10Hz×500 点×10min，≥200 批/s、≥100000 值/s）FAIL（吞吐维度）**：全拓扑实测写入路径仅 **15.6 值/s、2.1 批/s、99.86% 丢弃、p99 写 2634 ms**，距目标约 4 个数量级；单 realtime 进程写入饱和点约 **6000 值/s（≈12–15 个 500 点批/s）**。子饱和健康工作点（2 sink×5Hz）10 分钟浸泡 **稳定**（0 失败、p99≈9.9 ms、RSS 有界、1000 键全活、无 DLQ）。详见 §8。该容量差距属架构级问题，**反馈 ChatGPT 裁定，Claude 不自行重构**。
- 生产代码本会话仅一处小型兼容性修复（P1，`reliable_runtime._js_name`，nats-py 2.16 枚举/字符串兼容，见 §7.5），其余均为测试/Fixture；未创建 PR、未合并 main、未 rebase/force push、未提交。

## 1. 环境与静态门禁（Gate 1）

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| Ruff Lint | `ruff check src tests scripts` | PASS（All checks passed） |
| Ruff Format | `ruff format --check src/sink_service tests/sink_service` | PASS（37 files already formatted） |
| Mypy strict | `mypy src/sink_service` | PASS（Success: no issues found in 22 source files） |
| Import Linter | `lint-imports` | PASS（13 kept, 0 broken；含 “sink service three-layer architecture”“sink service does not import other process packages”） |

- 工具版本：ruff 0.8.6、mypy 1.20.2、import-linter 2.15、Python 3.11.16。
- 全量 `mypy src` 仍有 67 错误，全部位于 `web_server / seahorse / collector / industrial_gateway`，与本分支 sink 新代码无关（main 既有基线，UNRELATED）。
- 阶段初分支新文件曾有 16 ruff / 13 format / 32 mypy 漂移，已全部以**行为保持**方式清零（无 ignore、无抑制）。

## 2. 测试资产与执行结果

### 2.1 阶段一控制面套件迁移（81 个；80 个免基础设施 + 1 个 system）

阶段一 Fixture 已整体迁移到 v1 严格 `configs/` 布局（2 Service + 扁平精确版本模板 + 2 Instance，共 8 文件），与生产 `config_sources`/`template_path` 契约同构，无法漂移成 Writer/Reader 拒绝的布局。三处旧契约测试按 v1 真实语义重写（不弱化生产校验）：

- `test_failed_removal...`：对齐新看门狗语义——删除中 STOP 失败会被 `reconcile_all` 重试，但绝不 `start()` 复活；断言改为“不复活 + 失败期保留所有权 + stop 成功后才释放”。
- `test_published_template_version_cannot_be_deleted`：v1 生产（`config_repository.py:75-76`）允许活动快照移除模板引用、旧代目录保留不可变记录，**不再拒绝删除**。测试重写为钉死真实契约：新代不含 0.9.0、旧代字节不变、同路径异字节重新引入仍被 `immutable published template version modified` 拒绝。
- `test_apply_with_failed_convergence...`：1.1.0 模板改用扁平路径并按严格闭包删除被孤立的 1.0.0。

### 2.2 阶段二新增免基础设施测试（48 个，全部通过）

| 文件 | marker | 数量 | 覆盖的生产模块/契约 |
| --- | --- | --- | --- |
| test_stage2_observability.py | unit+contract | 11 | R04 十二指标词汇表；冷启动仅 `metrics_snapshot_ready=0` 不虚报；窗口不支持拒绝；成功率无分母不造数；P99 最近秩 `ceil(N*.99)-1`、无样本省略；半开窗口与 overflow 抑制 P99；Delivery 作用域不含进程级就绪指标；事件环有界/游标分页/失效游标/查询与时间过滤/字段截断 |
| test_stage2_realtime_engine.py | unit+component | 12 | `RealtimeEngine` 三策略（periodic/on_change/change_with_heartbeat）、绝对/相对阈值与 quality 翻转、来源路由、陈旧/乱序/同刻异 ID/重复样本、跨 task 权威冲突隔离点、缓存容量、500 点分批、写失败重试后恢复 |
| test_stage2_reliable_seams.py | unit+contract | 8 | `ReliableDeliveryPort` 免 broker 接缝：Kafka `require_dedup` 精确双字段身份、未知重复策略拒绝、可靠 Redis 原子幂等要求、非可靠/未知 kind 拒绝、durable ID 确定性与 deployment 隔离、`_matches` 源过滤、冷态可观测性诚实 |
| test_stage2_modbus_image.py | unit+modbus | 9 | `_RegisterImage`：float32 大端双寄存器、quality 码、乱序不回滚、同刻重复确认/冲突报错、非数值拒绝、陈旧只 settled 不写、读取边界/有效性/到期、未映射点报错不中断批次 |
| test_stage2_grpc_boundary.py | unit+contract | 8 | 13 RPC 错误分类映射、角色化能力、HEALTHY 裁定、指标窗口/单位/冷就绪、事件页限与过滤、Diagnose 未知 stage 拒绝/配置裁定/NOT_SUPPORTED 诚实限制/未知 target NOT_FOUND、List 通道校验/GetDelivery NOT_FOUND |

### 2.3 执行命令与数量

```
python -m pytest tests/sink_service -q -m "not system"
# 128 passed, 1 deselected in 3.3s   （80 阶段一免基础设施 + 48 阶段二）
```

13 个 RPC 的真实 mTLS 端到端方法级覆盖由 `test_stage1_dual_process.py` 承担（真实双 OS 进程 + 真实 TLS + Proto Stub），但该 system 用例当前受 Gate 4 阻塞，见 §3。

## 3. Gate 4 / Gate 5 阻塞与复现证据（NOT_RUN，不计 PASS）

### 3.1 宿主无真实 broker

`ss -ltn` 无 4222/6379/9092 监听；`docker ps` 仅有无关 `wh-ctr`（2375/2376）。尝试启动官方 `nats:2.11-alpine` 被本机权限策略拦截（外部镜像需用户显式授权），未绕过。

### 3.2 双进程 system 用例挂死的确定性根因（已复现）

用独立脚本（复刻 Fixture，子进程 stderr 落盘）得到两条确定性事实：

1. **全 STOP 基线（与 pytest env fixture 一致）：两进程 ~1s 内 gRPC 均 READY，writer.err 为空。** 证明无 broker 时控制面引导本身健康。
2. **存在 START 意图时：realtime 进程 gRPC 端口全程拒绝连接、进程存活，writer.err 持续刷 NATS `ConnectionRefused 127.0.0.1:4222`。**

根因链（生产代码）：`bootstrap.serve_control_plane` 在 `server.start()`（bootstrap.py:170）**之前** `await reconcile(initial)`（:156）；START 交付经 `RealtimeDeliveryPort.start → ingress.add → nats.connect(max_reconnect_attempts=-1)`（realtime_runtime.py:54-61），无 broker 时该协程永不返回，导致：
- gRPC 服务器永不绑定（启动期 START）；或对运行中进程的 `ApplyConfigSnapshot` RPC 永久挂起（运行期 START）；
- reconcile 全程持有 `DeliveryRegistry` 异步锁，看门狗 `reconcile_all` 同锁阻塞，配置收敛整体停摆。

阶段一报告 §3.2(7) 描述的 “START fail-closed 工厂快速 FAILED” 基于旧的替身工厂；`main.py` 现装配的是**真实 RealtimeFactory**，无 broker 时表现为无限重连而非快速失败。

### 3.3 缺陷台账

| ID | 级别 | 项 | 证据/位置 | 处置 |
| --- | --- | --- | --- | --- |
| S2-1 | P2（待设计裁定） | broker 不可达时 realtime START 无界阻塞：启动期 gRPC 不绑定、运行期 START RPC 挂起、reconcile 锁长期持有。**已实证 broker 可达时该路径正常（§6：START→RUNNING、gRPC 即时就绪），故根因确为无 broker 的无限重连而非死锁** | 本节 3.2；realtime_runtime.py:54、bootstrap.py:156/170、delivery_registry.py:147；反证见 §6 | 无 broker 时应否有界 fail-closed / 受 `apply_timeout_ms` 约束，属重大生产行为，**反馈 ChatGPT**，Claude 不自行重构 |
| S2-2 | P1（测试资产缺口） | 分支删除全部 r5（6 文件）/r6 真实基础设施套件与 13 个旧测试文件；`deploy/sink-service/compose.yaml` 仍指向已删除的旧 CLI（`sink_service.nats_init`、`--config/--delivery`、tdengine） | `git diff --diff-filter=D main...HEAD`；compose 头部 | Gate 4 需基于仍存在的 `tests/fixtures/services/r5_stack.py`（NATS2.10/Redis7/TDengine3.3.6.13/Kafka3.9.1，独立 project 动态 loopback 端口）按 v1 `main.py` 三参数进程模型重建 42 场景 |
| S2-3 | P2 | 可靠链路 ACK/NAK/InProgress/DLQ PubAck-before-ACK/进程重启、Redis Lua CAS、Kafka 幂等、Modbus TCP 帧级链路零自动化覆盖（仅免 broker 接缝已覆盖） | reliable_runtime.py / realtime_adapters.py 无对应真实链路测试 | 随 Gate 4 在真实栈上补齐，禁止以 Mock 计 PASS |
| S2-4 | P2 | Gate 5 吞吐/长稳（≥20 sink×10Hz×500 float32/批×10min，≥200 批/s、100000 值/s）NOT_RUN | 无 broker/无压测夹具 | 随真实栈搭建后执行并采资源指标 |

## 4. 回归与边界

- 免基础设施 128 测试全绿；新增测试仅引用生产公共接口，未改生产校验阈值或错误分类。
- `mypy src/sink_service` 0 错误；两条 import-linter 架构契约保持 KEPT。
- 未执行全仓 `pytest tests/`：本次聚焦 sink 目录；跨仓既有失败与历史验收记录一致，需在 Gate 4 联调时统一按 UNRELATED/RELATED 裁定。

## 5. 下一步（解除 Gate 4/5 阻塞所需）

1. 用户显式授权运行 r5 compose 固定的外部镜像（`nats:2.10-alpine`、`redis:7-alpine`、`apache/kafka:3.9.1`、`tdengine/tdengine:3.3.6.13`），或提供可达的同名真实服务。
2. 基于 `R5Stack` + v1 `sink_service.main --config-root/--service-file/--peer-service-file` 重建 14 UC / 42 场景系统套件（无 Mock）。
3. S2-1 反馈 ChatGPT 裁定 START 在 broker 断连时的有界 fail-closed / 超时语义后，再用真实 NATS 复验 dual_process 全 13 RPC。
4. 真实栈就绪后执行 Gate 5 性能与 10min 长稳。

## 6. Gate-4 首批真实链路结果（2026-11-05，用户授权固定镜像）

### 6.1 真实栈与无 Mock 约束

经用户显式授权，用仓库内 `tests/fixtures/services/docker-compose.r5.yml` 以独立 project `wind-hub-r5-manual` 拉起全部固定版本镜像，宿主端口仅绑 127.0.0.1：

| 组件 | 镜像 | loopback 端点 | 应用层就绪证据 |
| --- | --- | --- | --- |
| NATS JetStream | `nats:2.10-alpine`（`-js`） | nats://127.0.0.1:47222（监控 48222） | `/healthz` status=ok、`/jsz` 账户存在 |
| Redis | `redis:7-alpine` | 127.0.0.1:46379 | `redis-cli ping`=PONG |
| TDengine | `tdengine/tdengine:3.3.6.13` | http://127.0.0.1:46041（REST SQL） | `SERVER_VERSION()`=3.3.6.13，建库 wind_hub_r5/wind_hub_r5b |
| Kafka(KRaft) | `apache/kafka:3.9.1` | 127.0.0.1:49092 | 宿主 aiokafka 0.14.0 真实 produce 成功（partition 0/offset 0） |

容器内 kafka-topics 连 49092 失败属预期：advertised listener 是宿主映射端口，按 r5 设计应用层就绪只由宿主侧真实收发保证。生产 Redis 适配器为内联 RESP+Lua CAS（asyncio 原始 socket），不依赖 redis-py，故环境无 `redis` 包为正常。

### 6.2 首个端到端系统用例（PASS，真实双进程 + 真实 NATS + 真实 Redis）

新增 `tests/sink_service/test_stage3_real_infra.py`（marker: system/integration/reliability），支持两种栈生命周期：环境变量 `SINK_R5_*` 指向外部已起栈时测试内部不编排 docker（本次采用），否则用 `up_stack()/down_stack()` 自管隔离 project。

用例 `test_realtime_start_unblocks_and_nats_sample_lands_in_redis`：
1. 两真实 `sink_service.main` OS 进程（realtime=Writer / reliable=Reader）共享一配置根，mTLS + 真实 protobuf stub；
2. 配置注入真实端点：service `nats.servers=nats://127.0.0.1:47222`、sink `ems_redis.connection=127.0.0.1:46379`，`ems_power` 意图 START、`power_history` STOP；
3. **S2-1 反证**：NATS 可达时两进程 gRPC 在亚秒级就绪（此前无 broker 时 START 永久阻塞、端口拒连），`GetServiceStatus` 依赖 `ems_power=RUNNING`；
4. 用真实 nats 客户端向契约 subject `wh.rt.farm-a.collector-1.WT001.active_power` 发布一条 `SampleMessage`（带 `max_staleness_ms=5000`）；
5. 独立经 `docker exec redis-cli GET` 取证，键 `windhub:farm-a:collector-1:WT001:active_power` 真实落地，`PTTL=2518ms`（印证 PXAT=sample_time+5000 的过期语义，也解释了稍后查询为空），值：
   `{"wind_farm_id":"farm-a",...,"sample_id":"s-0001","value":35000.0,"quality":"good",...}`；
6. 两进程 SIGTERM 均退出码 0。

结果：`1 passed`；复跑取证 `pytest rc=0` 且 Redis HIT。静态门禁保持全绿，免基础设施套件复跑 `128 passed, 2 deselected`（新增 1 个 system 用例）。生产代码零改动。

### 6.3 Gate-4 剩余范围（仍 NOT_RUN，不计 PASS）

reliable JetStream 全链路（ACK/NAK/InProgress/DLQ、PubAck-before-ACK、进程重启恢复）、Kafka 幂等去重、Modbus TCP 帧级 server、故障注入（pause/stop/network disconnect）、14 UC/42 场景追溯矩阵，以及 Gate 5 性能长稳，尚待在本栈上继续补齐。

## 7. Gate-4 真实链路完整结果（2026-11-05，授权固定镜像栈，无 Mock）

真实栈同 §6.1（project `wind-hub-r5-manual`，四容器健康，仅绑 loopback）。全部系统用例经 `SINK_R5_*` 环境变量接外部栈，pytest 内部不编排 docker；合并复跑命令与结果：

```
SINK_R5_* 指向外部栈 \
  pytest tests/sink_service/test_stage3_real_infra.py \
         tests/sink_service/test_stage3_reliable_jetstream.py \
         tests/sink_service/test_stage3_modbus_tcp.py -q
# 8 passed in 117.98s
```

### 7.1 八个真实链路系统用例（全部 PASS）

| # | 测试节点 | 真实组件 | 验证的契约/证据 |
| --- | --- | --- | --- |
| 1 | `test_stage3_real_infra.py::test_realtime_start_unblocks_and_nats_sample_lands_in_redis` | 双进程 mTLS + NATS Core + Redis | S2-1 反证（broker 可达 gRPC 亚秒就绪、ems_power=RUNNING）；`wh.rt.…` 样本经 Lua CAS 落 `windhub:…:active_power`，PTTL=sample_time+staleness |
| 2 | `test_stage3_real_infra.py::test_realtime_recovers_after_nats_broker_pause` | 双进程 + NATS 容器 `docker pause/unpause` | broker 冻结 5 s 两进程存活、gRPC 全程应答；恢复后新样本 `s-recover-1` 重新端到端落地、delivery 回到 RUNNING（UC-04-S03） |
| 3 | `test_stage3_real_infra.py::test_realtime_same_timestamp_conflict_keeps_first_identity` | 双进程 + NATS + Redis | 同 `sample_time` 异 `sample_id` 第二身份被引擎单调守卫拒绝，Redis 保持首值 `s-cas-first`，`dropped_total≥1`（UC-04-S02） |
| 4 | `test_stage3_reliable_jetstream.py::test_reliable_jetstream_acks_and_kafka_receives` | JetStream WH_HISTORY + 真实 Kafka | 可靠样本 `r-0001` 进 Kafka topic wh.history；durable 消费后 `num_pending=0/num_ack_pending=0`、`delivered.stream_seq≥1`（显式 ACK，UC-05-S01） |
| 5 | `…::test_reliable_malformed_message_is_dead_lettered_and_acked` | JetStream + WH_DLQ | 不可解析消息转 DLQ（pull 消费实证 `classification=PERMANENT`、含 delivery_id），源消息仍 ACK 不无限重投（UC-05-S03 子集） |
| 6 | `…::test_reliable_durable_consumer_survives_process_restart` | 仅重启 reliable 进程 | 按确定性 durable 名 `WH_SINK_+sha256(prod-a:power_history)[:24]` 重绑；重启后新消息 restart-2 仍被拉取投递（UC-10-S01） |
| 7 | `…::test_reliable_kafka_outage_redelivers_without_dlq` | Kafka 容器 stop/start + JetStream | 停 Kafka 后发布，持续 >ack_wait(30 s) 消息不被 ACK、不进 DLQ、进程存活；Kafka 恢复后重投 `fault-gap-1` 入 Kafka 并最终显式 ACK，DLQ 全程为空（瞬时故障不永久化，UC-05-S02/UC-11） |
| 8 | `test_stage3_modbus_tcp.py::test_modbus_server_serves_register_image_over_real_tcp` | 真实 pymodbus 主站 + sink 内 Modbus server | 功能码 3 读 float32 大端双寄存器≈35000.0、quality 线圈 good=0；外部写 `write_register` 被只读上下文拒绝（ILLEGAL） |

### 7.2 真实流预配（运维角色，非 sink 自建）

生产 sink 只校验/绑定 Stream、绝不创建。测试以运维身份经真实 JetStream 管理 API 预配 `WH_HISTORY`（subjects `wh.hist.>`、FILE、LIMITS、discard NEW、max_bytes 256 MiB、max_age 7200 s）与 `WH_DLQ`（subject `wh.dlq`、FILE、NEW、max_bytes 1 GiB、max_age 86400 s，满足 sink 的容量/保留期下限校验）。

### 7.3 本会话发现并修复的生产兼容性缺陷（P1，待 ChatGPT 复核）

- **现象**：锁定依赖 nats-py 2.16.0 下，可靠 Delivery START 与 DLQ 校验整体崩溃，gRPC `DependencyHealth.detail="'str' object has no attribute 'name'"`；无 broker 的单元测试因不触达真实 `stream_info()` 而完全看不见此缺陷。
- **根因**：nats-py 2.16 的 `stream_info().config.storage/discard/retention` 返回**小写字符串**（`"file"/"new"/"limits"`）而非枚举成员，旧代码 8 处 `.name.lower()` 对字符串取 `.name` 抛 AttributeError。
- **修复（Claude 职责内的小型明确缺陷）**：`src/sink_service/infrastructure/reliable_runtime.py` 新增模块级 `_js_name(value)`（有 `.name` 取 `.name.lower()`，否则 `str(value).lower()`），替换全部 8 个调用点；接受的契约值集合零变化。修复后四门静态全绿、128 免基础设施测试不回归、可靠真实链路 4 用例全过。
- 该文件工作区在会话开始前已有其它未提交改动（`_capacity_alarm`/`replay_starvation`/健康明细等），**本会话仅新增 `_js_name` 及其调用点**，未触碰其余改动。

### 7.4 Gate-4 仍 NOT_RUN 的范围（不计 PASS）

- 阶段一 `test_stage1_dual_process.py`（全 13 RPC 真实双进程方法级矩阵）基于旧 fail-closed 替身工厂、不接外部栈；在真实工厂下 START 触发 S2-1 无界重连导致挂死（本会话实测 >300 s 不返回，已 TaskStop）。其残留孤儿进程与 S2-1 复现脚本进程见 §11。**完整 13 RPC 真实进程矩阵、UC-07 配置 Get/Push/Apply 热生效全链路仍 NOT_RUN。**
- InProgress 心跳续约的**主动强制**路径未直接触发（Kafka 中断用例已证明超过 ack_wait 30 s 不 ACK/不 DLQ 的关键安全属性，但长 I/O 期间的 `message.in_progress()` 续约仅由代码路径覆盖）；NAK 退避重投未单独构造。
- reliable 模式 Redis CAS 同刻冲突→DLQ、Kafka `require_dedup` 双字段身份的真实 broker 幂等（仅免 broker 接缝测试覆盖）、DLQ 满/PubAck 失败、UC-13 Diagnose 真机分阶段诊断、network disconnect（仅用 pause/stop）未覆盖。
- TDengine 真实写入路径未在本批系统用例中启用。

## 8. Gate-5 性能与长稳（真实 NATS + 真实 Redis + 双进程，无 Mock）

夹具 `tests/sink_service/test_stage4_performance.py`（marker performance/system），规模/时长由 `SINK_PERF_SINKS/HZ/POINTS/SECONDS` 环境变量控制；每个 Delivery 持 500 个设备 key，使一个 100 ms 调度周期恰好产出一个 500 点 `write_batch`（即一次 Redis EVAL，Lua 内最多 500 key CAS）。独立证据：gRPC R04 计数 + Redis `INFO commandstats` 的 `cmdstat_eval.calls`（1 EVAL=1 批）+ 负载发生器节拍达标率 + `/proc/<pid>/VmRSS` + 运行中 DBSIZE 探针。

### 8.1 实测数据（宿主 20 核 WSL2）

| 拓扑 (sink×Hz×点) | 时长 | 发生器速率 (目标) | 引擎 received | 实测值/s | 实测批/s | failed | dropped | p99 写 | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1×5×500 | 15 s | 2466/s（2500） | =sent 零入口丢 | 2962.8 | 13.1 | 0 | 75 | — | 健康 |
| 1×10×500 | 12–15 s | 4928/s（5000） | =sent | 2620–3106 | 11.7–14.0 | 0 | ~39k（相位竞争） | 6.35 ms | 10 Hz 满批心跳退化到 ~6.3 Hz |
| 2×5×500 | 15 s | 4938/s（5000） | =sent | 5932.1 | 24.8 | 0 | 400 | — | 健康工作点 |
| 2×10×500 | 20 s | 9908/s（10000） | =sent | 5740 | 13.2 | 0 | 113250 | 15.92 ms | 过载、缓存覆盖丢点 |
| **20×10×500（全拓扑）** | **120 s** | **50263/s（100000，迟到槽 84.19%）** | 5.76M（入口少量背压） | **15.6** | **2.1** | **0** | **5.75M（99.86%）** | **2634 ms** | **吞吐 FAIL** |
| 2×5×500（浸泡） | **600 s** | 4972/s（5000，迟到 0%） | 2983700 | 5017.3 | 15.1 | **0** | 152613（5.1%，最新值语义） | 9.93 ms | **长稳 PASS（子饱和）** |

### 8.2 结论与瓶颈定位（反馈 ChatGPT，不自行重构）

1. **吞吐验收目标未达成（G5-1，P1 容量）**：全拓扑要求 100000 值/s、200 批/s；实测写入仅 15.6 值/s、2.1 批/s，差距约 4 个数量级，且 99.86% 入引擎样本在最新值缓存相位竞争中被覆盖丢弃。**failed_total=0**——无写入失败，是吞吐/调度容量不足而非正确性错误。
2. **单 realtime 进程写入饱和点约 6000 值/s（≈12–15 个 500 点批/s）**，与 sink 数量基本无关（1 sink 与 2 sink 同量级），说明瓶颈在**单进程单事件循环**：一个 NATS 入站订阅、20 个引擎按 (farm, collector) 分组扇出（本拓扑 20 Delivery 同采集器，offer 20× 扇出是真实生产形态）、同步逐消息 `offer` + 串行 500-key Redis EVAL（实测单次 ~1.8 ms）共享同一 asyncio 循环。10 万 msg/s 的 offer 路径先打满单核，写批次排队把 p99 推到 2.6 s。
3. **10 Hz 满批心跳在 offer 饱和时退化到 ~6.3 Hz**（G5-2，P2）：缓存"未确认即覆盖"是设计的最新值语义（非缺陷），但意味着高 offer 速率下 offered≠delivered；建议设计书明确性能口径是 **offered rate 还是 delivered rate**，并评估批处理流水线化/连接分片/进程水平拆分。
4. **可观测性缺口（G5-3，P2）**：引擎内部累计 `write_success_total`（真实批次数）未纳入 GetMetrics 进程指标词汇表（仅导出 `write_success_ratio`）；批次吞吐只能从 Redis `cmdstat_eval` 旁路获得，gRPC 无法直接读出批/s。
5. **子饱和长稳达标**：2 sink×5Hz×500 持续 600 s，0 失败/0 发布错误/p99 稳定 9.93 ms/RSS 66→75 MiB 有界无泄漏/1000 个活跃键齐全/无 DLQ。设计 v1.17 §本已注明该指标"尚未经实际压力测试证明""不允许以单一 Redis 结果推定全部目标协议达标"，本次 Redis 实测**证伪了当前单进程形态在目标负载下的达标假设**，其它协议（Kafka/Modbus/TDengine） Gate-5 仍 NOT_RUN。

## 9. UC → 测试节点追溯矩阵（14 UC / 42 场景）

状态：**R**=真实链路系统用例 PASS（本会话）；**C**=仅免 broker 单元/契约测试 PASS（128 套件）；**N**=NOT_RUN。R 与 C 都不计为对方；N 不计 PASS。

| UC | S01 主流程 | S02 关键边界 | S03 异常与恢复 |
| --- | --- | --- | --- |
| UC-01 启动就绪 | **R**（每个 stage3 双进程引导+GetServiceInfo） | C（service 变更/单进程未就绪） | C（非法配置/Writer 冲突）；进程恢复部分 R（#6） |
| UC-02 启动 Delivery | **R**（#1/#4 RUNNING 裁定） | C（重复启动/共享 Sink 引用） | N（目标不可达待收敛，关联 S2-1） |
| UC-03 停止释放 | **R**（全部用例 SIGTERM 退出码 0） | C（重复停止/共享 Sink） | C（未收敛 I/O/关闭超时） |
| UC-04 实时派发 | **R**（#1 NATS→Redis 三策略引擎另有 12 单测） | **R**（#3 同刻冲突+过期/乱序/容量 C） | **R**（#2 NATS 暂停恢复）；慢目标/背压 N |
| UC-05 可靠交付 | **R**（#4 ACK→Kafka） | **R 部分**（#7 重投/不确定）；重复投递、require_dedup 真实幂等 C/N | **R 部分**（#5 畸形→DLQ）；DLQ 满/PubAck 失败 N |
| UC-06 查询指标事件 | **R 部分**（GetServiceStatus/GetMetrics 真机）+C | C（无数据/窗口/分页/不可用指标） | C（无权限/对象不存在/部分进程不可用） |
| UC-07 配置热生效 | C（Get/Push/Apply 契约） | C（候选校验/版本冲突/重启要求） | N（提交后应用失败/Reader 不可达真机，dual_process 挂死） |
| UC-08 正常停止 | **R**（SIGTERM 干净退出） | C（重复关闭/待处理/共享资源） | C（关闭超时/安全退出） |
| UC-09 重启加载 | **R 部分**（#6 durable 重绑） | C（Service 参数/手动 STOP 保持） | C（非法配置/端口占用/旧资源） |
| UC-10 异常恢复重交付 | **R**（#6 持久意图+重绑、#7 重投） | **R 部分**（实时缓存语义 C）；可靠重复 C | C（Reader/Writer 版本不一致/旧资源冲突） |
| UC-11 故障隔离恢复 | **R 部分**（#2 NATS、#7 Kafka 断连恢复） | N（多 Delivery 共享同 Sink 的真机隔离） | **R 部分**（重连）；手动 STOP/未收敛 I/O 真机 N |
| UC-12 死信交接 | **R 部分**（#5 DLQ pull 实证记录/积压/证据） | C（无死信/分页/证据缺失） | N（Broker 不可达查询/外部工具交接） |
| UC-13 诊断链路 | C（grpc_boundary Diagnose 分阶段/UNKNOWN/NOT_FOUND） | C（阶段跳过/证据缺失/UNKNOWN） | C（超时/权限/不可达/无副作用）；**真机 N** |
| UC-14 周期指标 | **R 部分**（perf 用例真机 GetMetrics）+C（12 R04 词汇 11 单测） | C（空窗口/未填满/无延迟/窗口不一致） | C（非法配置/重启重置/热变更/跳周期不重入） |

合计：**真实链路 R 覆盖 8 个系统用例、直接闭环约 14 个场景的主流程/关键异常**；其余场景由 128 个免基础设施测试以契约/单元级别覆盖（C），明确 NOT_RUN（N）项见 §7.4 与本矩阵，不以 C 冒充实机 R。

## 10. 缺陷台账（更新）

| ID | 级别 | 项 | 证据 | 处置 |
| --- | --- | --- | --- | --- |
| S2-1 | P2（待设计裁定） | 无 broker 时 realtime START 无界重连阻塞 gRPC/锁 | §3.2；本会话 #2 反证 broker 可达即正常，且 pause 5 s 可存活恢复 | 反馈 ChatGPT 定有界 fail-closed/超时语义；Claude 不重构 |
| S2-2 | P1（测试资产） | r5/r6 旧套件与 13 旧测试被删、deploy compose 指旧 CLI；dual_process 替身假设在真实工厂挂死 | §7.4；本会话已重建 8 真实系统用例，但 13 RPC 真机矩阵仍缺 | 持续重建；dual_process 需按真实工厂改造（ChatGPT 定 S2-1 后） |
| S2-3 | P2 → 大部分关闭 | 可靠 ACK/DLQ/重启/重投、Redis CAS、Modbus 真机 | §7.1 八用例 | InProgress 强制路径、reliable Redis 冲突→DLQ、require_dedup 真机幂等、TDengine 仍 N |
| S2-4 | **FAIL（吞吐）/PASS（子饱和长稳）** | Gate-5 | §8 | G5-1/G5-2/G5-3 反馈 ChatGPT |
| G5-1 | **P1 容量** | 单进程写入饱和 ~6000 值/s；全拓扑 15.6 值/s、99.86% 丢、p99 2.6s，距目标 4 个数量级 | §8.1 全拓扑行 | 架构级（流水线/分片/多进程），ChatGPT 裁定 |
| G5-2 | P2 | 10Hz 满批心跳 offer 饱和时退化 ~6.3Hz；offered≠delivered 口径待明确 | §8.1 | 设计书补性能测量口径 |
| G5-3 | P2 可观测性 | `write_success_total` 未进 GetMetrics 词汇，批/s 无法经 gRPC 直读 | §8.2(4)，observability.py/grpc_v1.py | 建议补指标导出 |
| P1-fix | P1（本会话已修，待复核） | nats-py 2.16 枚举/字符串不兼容致可靠 START+DLQ 全崩 | §7.3；reliable_runtime.py `_js_name` 8 处 | 已最小修复，静态/128/4 可靠真机全绿，待 ChatGPT 复核 |

## 11. 环境残留与复现入口

- 真实栈 `wind-hub-r5-manual` 四容器仍在运行（健康），供复验；收尾命令：
  `docker compose -f tests/fixtures/services/docker-compose.r5.yml -p wind-hub-r5-manual down -v --remove-orphans`。
- 检测到 4 个**非本会话工具创建**的历史孤儿 `sink_service.main` 进程（PID 1649580 及 `/tmp/reprodual_*` 三个，均为早前 S2-1 复现/挂死残留，做无界 NATS 重连）。按安全约束未代杀；用户确认后可按精确 PID 终止。
- 本会话新增/修改文件（均未提交）：`tests/sink_service/test_stage3_real_infra.py`、`test_stage3_reliable_jetstream.py`、`test_stage3_modbus_tcp.py`、`test_stage4_performance.py`、生产 `src/sink_service/infrastructure/reliable_runtime.py`（仅 `_js_name`）、本报告。

---

# 12. Gate-5 性能修复定向复测（2026-11-05，独立 QA）

## 12.1 版本与工作区

- 基线 SHA：`55ff3698`（上轮 FAIL 时的 origin HEAD）；复测目标 SHA：`41fff0aa`（七个远程提交，含六个性能修复 + `ba96fba2` nats-py 2.16 枚举兼容）。
- 六个性能修复提交：
  1. `50463da2` 变更触发合并微批 + 入口共享新鲜度时钟（realtime.py）
  2. `571f99ea` 有界点路由缓存 8192 + 每 NATS 样本单时钟（realtime_runtime.py）
  3. `1914f53b` Redis 批内 Lua TIME 单次求值（realtime_adapters.py，2 行）
  4. `8543acba` 饱和后心跳节拍合并（realtime.py，`_saturated`）
  5. `fee9596e` 入口路由后跳过重复 source 过滤（`offer(routed=True)`）
  6. `41fff0aa` Delivery 扇出使用已验证缓存路由（1 行）
- 本会话生产改动仅 1 处（行为保持）：`src/sink_service/application/realtime.py` 三处 `try/except TimeoutError: pass` 改为 `contextlib.suppress(TimeoutError)`，消除新 `_saturated` 块引入的唯一新增 SIM105。
- 测试侧改动（均未提交）：恢复 stash 中符合 v1_17 契约的 `_stage1_fixtures.py`（见 12.3）；新增 `test_stage2_perf_seams.py`（8 项确定性单测/组件测）、`test_stage3_redis_batch_contract.py`（2 项真实 Redis 系统测）；适配四个 stage3/4 文件到 fixture 常量；stage4 增加 `SINK_PERF_POLICY` 策略参数化。

## 12.2 静态门禁（四门，对比基线 worktree 55ff3698）

| 门禁 | 基线 55ff3698 | 目标 41fff0aa（本会话改动后） | 结论 |
|---|---|---|---|
| ruff check src/sink_service tests/sink_service | 15 errors（含本会话测试外的旧集合） | 13 errors，全部为基线同规则族（B/SIM/UP 等），新增测试文件 0 | 无新增债务 |
| ruff format --check | 13 文件漂移 | 同一 13 文件集合 | UNRELATED 基线 |
| mypy src/sink_service | 32 errors（7 文件） | 24 errors（7 文件），`_js_name` 消除 8 处，无新错误类别 | 改善 |
| lint-imports | 13 kept / 0 broken | 13 kept / 0 broken | PASS |

无新增 ignore、无依赖升降、无门禁放宽。

## 12.3 远程 HEAD 自有测试债务（UNRELATED，P1 移交 ChatGPT）

远程在案 `_stage1_fixtures.py`（最后修改 `0a160670`）与在案 stage1 测试使用**裸路径**（`services/...`、`instances/...`、`templates/<family>/<id>/<version>.yaml`），而生产侧在其后的 `afb6dcb0`（"use declared v1_17 config paths"）明确强制 `configs/` 前缀，且 v1_17 设计文档 §11.2/迁移边界明确**禁止** `templates/<family>/<id>/<version>.yaml` 版本目录布局、要求扁平 `{id}_{version}.yaml`。提交时序证明 fixture/测试未跟随生产迁移：

- 基线 worktree（55ff3698）在案套件实测 **34 failed / 37 passed**，根因 31×`ConfigurationError: unsupported configuration path` + 3×`ConfigReader.__init__() 缺 apply 参数`；
- 目标 HEAD 同集合同根因（换用 v1_17 fixture 后异常点变为测试侧硬编码 KeyError，失败身份同属 stage1 四个文件）；
- 我负责的 stage2/3/4 测试与恢复后的 v1_17 fixture 自洽并全绿，未改写在案 stage1 测试（不扩大任务范围）。

**P1 台账 G5-F1（移交 ChatGPT）**：统一 fixture/stage1 测试到 v1_17 `configs/` + 扁平模板契约，消除 34 个基线红灯；本会话以 stash 内修正版 fixture 维持测试可运行性，未提交。

## 12.4 十个定向测试（任务书 §III 强制项）

新增 `tests/sink_service/test_stage2_perf_seams.py`（8 项，unit/component，无 broker，4 轮重复全绿）：

1. 单/多 Delivery 扇出路由（缓存命中 `routed=True`、共享同一 `now`）
2. source/device/point 过滤与负缓存（空 tuple 缓存不被静默重解析）
3. 8192 身份容量满后全量清空并正确重建
4. Delivery remove/热更新后无陈旧路由（生产 `remove()` 路径验证缓存失效）
5. 孤立变更走低延迟即时路径（实测 <50 ms，远低于 100 ms 心跳；微批地板 5 ms）
6. 持续饱和下合并到心跳节拍（300 ms 窗口 ≤5 批，对照每 2 ms 触发的 ~150 次潜在写）
7. 安静两个周期后 `_saturated` 自动清除并恢复即时触发
8. 在途写期间 STOP 不产生额外批次（阻塞写夹具确定性验证）

新增 `tests/sink_service/test_stage3_redis_batch_contract.py`（2 项，真实 Redis，外部 R5 栈，无 Mock；唯一 namespace 隔离，不 FLUSHDB）：

9. 单 EVAL 内共享 `TIME` 时钟：过期点 code 0  settled-not-delivered、存活点 code 1、边界点 PTTL 正确，`cmdstat_eval` 增量恰为 1
10. 整 500 点批混合：首写 code 1 / 重复确认 code 2 / 乱序 code 0 / 同刻异 sample_id 冲突 code -1，Redis 内业务版本不回滚，501 点硬守卫抛 `ConfigurationError`

回归命令 `pytest tests/sink_service -m '(unit or component) and not field'`：**84 passed / 43 failed**，43 个失败全部落在在案 stage1 四文件（12.3 的 UNRELATED 债务），我的 stage2 全部 56 项（48 旧 + 8 新）零失败。

## 12.5 八个真实链路系统用例复跑（§VII）

外部栈 `wind-hub-r5-manual`（四容器 healthy），命令同 §7 环境变量；结果 **8 passed in 118.61s**（上轮 117.98s）：实时 NATS→Redis Lua CAS、NATS pause/unpause、同刻冲突保持首值、JetStream 显式 ACK→Kafka、畸形→DLQ+ACK、durable 重启重绑、Kafka 中断 NAK 重投无 DLQ、Modbus TCP float32/quality/拒写。六个性能修复未破坏任何既有真实链路语义。

## 12.6 五级负载矩阵（change_with_heartbeat，每单元 60s 稳态，10 Hz × 500 点）

口径严格区分：Offered=发布器 `samples_sent/elapsed`（独立负载侧，含 late slots）；Received=引擎 `received_total`；Delivered(窗口)=gRPC `delivered_rate_per_second`；Batch=Redis `cmdstat_eval` 增量/时间（1 EVAL=1 个 ≤500 点批）；Drop=`dropped_total`（最新值缓存未确认覆盖/过期，非写失败）。全部单元 `failed_total=0`、`publish_errors=0`、DBSIZE 与预期存活键一致、RSS 有界。

| Sinks | Offered /s | late slots | Delivered 窗口 /s | Batch /s | 平均批大小（值） | p99 写 ms | dropped 累计 | RSS KiB |
|---|---|---|---|---|---|---|---|---|
| 1 | 4,967 | 0% | 4,182 | 10.6 | ~395（含预热批） | 8.36 | 33,100 | 65.5→71.1 MiB |
| 2 | 9,927 | 0% | 8,526 | 37.0 | ~230 | 14.85 | 88,900 | 66→73.8 MiB |
| 5 | 24,833 | 0% | 22,483 | 75.4 | ~298 | 45.98 | 572,910 | 67.4→86 MiB |
| 10 | 49,672 | 0% | 23,408 | 55.8 | ~420 | 174.35 | 2,977,750 | 69.3→108.6 MiB |
| 20 | 58,748（**发布器封顶**，目标 100k） | 74.72% | 375 | 3.1 | ~121 | 93.66 | 3,351,546 | 73.6→130.7 MiB |

关键观察：

- 单进程 Delivered 天花板约 **23–25k 值/s**（5 sink 附近见顶，10 sink 不增反降），较上轮 ~6k 值/s 提升约 **4 倍**，但距 100k 目标仍差 4 倍；Batch 天花板 **75.4 批/s**，距 200 批/s 目标差 2.7 倍。
- 10 sink 起调度器被消息回调饿死：p99 写延迟跃升至 174 ms，Batch/s 从 75 跌到 56；20 sink 时几乎完全饿死（3.1 批/s）。writer.log 无 slow consumer/断连/异常，NATS 订阅未触发服务端丢消息——瓶颈在单事件循环内的 Python 处理（decode→路由缓存→offer→20 个调度器簿记），不在 broker 也不在 Redis（`io_inflight=0`，低载 EVAL p99 仅 8 ms）。
- 20 sink 单元的发布器自身仅达 58.7k msg/s、74.7% 节拍迟到，按夹具规则该单元的 100k 硬断言应判 **NOT_RUN（负载侧能力不足）**，不得宣称 sink 100k 能力；Sink 侧在该 offered 水位下同样崩溃，两个瓶颈并存。

## 12.7 三策略对比（60s 单元）

| 策略 | Sinks | Offered /s | late | Delivered 窗口 /s | Batch /s | 平均批大小 | p99 写 ms | dropped |
|---|---|---|---|---|---|---|---|---|
| periodic | 1 | 4,966 | 0% | 4,418 | 10.5 | ~420 | 8.11 | 33,075 |
| periodic | 2 | 9,931 | 0% | 8,696 | 21.0 | ~414 | 16.47 | 90,200 |
| periodic | 20 | 60,182（封顶） | 73.8% | 928 | 4.2 | ~221 | **1,644.72** | 饥饿 |
| on_change | 1 | 4,966 | 0% | 4,410 | 119.5 | **~37** | 1.37 | 50 |
| on_change | 2 | 9,934 | 0% | 8,322 | 111.7 | ~75 | 2.73 | 150 |
| on_change | 20 | 59,679（封顶） | 77.5% | 8 | 2.3 | ~3 | 27.62 | 3,428,500 |
| change_with_heartbeat | 见 12.6 五级矩阵 | | | | | | | |

结论：

- 三策略在低载的**值/s 天花板相同（~23–25k）**，证明瓶颈与触发策略无关，在每样本处理主路径。
- on_change 把批/s 推到 119.5（接近 200 目标的 60%），但每批仅 37–75 点，EVAL 固定开销（连接往返+脚本）被小批放大；`change_with_heartbeat` 的合并修复在饱和时正确保住大批（平均 230–420 点/批），这正是六个修复的设计意图，行为正确。
- periodic 在 20 sink 饥饿时 p99 写达 1.64 s，而 on_change 仅 27.6 ms（后者几乎不发起写）；延迟劣化是饥饿的果不是因。
- 低载三策略 p99 写 1.4–16 ms，Redis 侧（单串行 RESP2 连接、单 EVAL/批）不是低载瓶颈。

## 12.8 负载侧独立天花板（§IV 独立采样，无 sink 参与）

同一 NATS 主题/编码/平滑节拍，仅启动发布器、不接任何订阅者：

| 配置 | 速率 | 迟到槽 | 吞吐 |
|---|---|---|---|
| 20 微块/拍（夹具默认，200 flush/s） | 86,333 msg/s | 70.68% | 28.1 MiB/s |
| 5 微块/拍（100 flush/s，节拍准确） | 93,000 msg/s | 0% | 30.2 MiB/s |

**单 Python asyncio + nats-py Core 发布进程在本机的物理上限约 93k msg/s**（编码+publish+flush 主路径开销），无论 sink 是否存在。Gate-5 要求的 100k 值/s offered 无法由单发布进程产生；要在 100k offered 水位证明 sink 容量，必须多发布进程分流。20-sink 单元的 74.7% 迟到由此解释，按夹具规则该单元硬断言记 **NOT_RUN（负载侧能力不足），不计 PASS 也不伪造通过**。

## 12.9 六个性能修复逐项验收

| # | 提交 | 修复意图 | 验证手段 | 结论 |
|---|---|---|---|---|
| 1 | 50463da2 | 变更触发合并微批（5 ms 有界窗）+ 入口共享新鲜度时钟 | 定向测试 #5（孤立变更 <50 ms）、#6（饱和合并）；真实链路 8/8 回归 | **PASS**：低载即时性保留，高载正确合并；每样本单 `now` 消除重复时钟调用，语义不变 |
| 2 | 571f99ea | 有界点路由缓存 8192 + 每 NATS 样本单时钟 | 定向测试 #1/#2/#3（扇出、过滤负缓存、8192 重建） | **PASS（行为）/ 容量见 G5-F2**：正确性与失效逻辑全绿；但 8192 < 目标拓扑工作集 10000 身份，目标规模下每拍全量清空重建，缓存命中率结构性受限 |
| 3 | 1914f53b | Redis Lua 批内单次 `TIME` 求值 | 定向测试 #9（真实 Redis：单 EVAL、共同时钟、过期/存活同批裁决、PTTL 边界） | **PASS**：1 EVAL/批实证，CAS/过期语义不变；每 500 键批从 N 次 TIME 降为 1 次 |
| 4 | 8543acba | 饱和后心跳节拍合并（`_saturated`） | 定向测试 #6/#7（饱和合并、安静恢复）、#8（STOP 无额外写） | **PASS**：标志翻转/恢复确定性验证，STOP 路径无残留写、无调度器泄漏 |
| 5 | fee9596e | 入口路由后 `routed=True` 跳过重复 source 过滤 | 定向测试 #1/#2 + 代码审查（`offer(routed=...)` 仅跳过已验证的同一谓词，无新接受面） | **PASS**：过滤在缓存构建处仍完整执行一次，外来样本不计数语义保留 |
| 6 | 41fff0aa | Delivery 扇出使用已验证缓存（1 行接线） | 定向测试 #1/#4（remove/热更新后无陈旧路由） | **PASS**：`add/remove` 均清空缓存，热更新路径无陈旧引擎引用 |

并发与生命周期审查（无新增缺陷）：

- 路由缓存与 `_routes/_engines` 全部在同一事件循环线程访问，`add/remove` 持 `_lock` 且双清缓存；`_on_message` 无 await 跨缓存修改点，无竞态。
- `_saturated` 仅由调度器在写循环末尾赋值，无跨任务写；STOP 时 `_stop` 优先返回，测试 #8 钉死无额外批次。
- 最新值缓存的覆盖计数、quality 变更触发、过期丢弃、同刻冲突隔离、版本不回滚语义在 8 真实链路用例与定向测试 #10 中保持不变。
- 无忙等：所有等待仍走 `wait_for(Event, timeout)`；5 ms 微批为有界 sleep，非自旋。

### 观察项（不阻断正确性，移交 ChatGPT 评估）

- **G5-F2（P2 容量结构）**：点路由缓存容量 8192 小于 Gate-5 目标工作集（20×500=10000 身份）。每个 100 ms 拍内缓存填充到 8192 即全量清空，目标规模下不存在稳态命中，每拍 ~10000 次全量路由重算。建议容量与配置工作集对齐或改 LRU 淘汰。
- **G5-3（沿用上轮）**：`write_success_total` 仍未进入进程域 GetMetrics 词汇，批/s 只能旁路 Redis `cmdstat_eval`。

## 12.10 强制 Gate-5 全规模 600s（20 Sink × 10 Hz × 500 float32，真实 NATS/Redis/双进程）

结果（`gate5-full-600s.out`，606.08s，**1 skipped**——按夹具规则因负载侧能力不足跳过硬断言，NOT_RUN 不计 PASS）：

- 发布器 offered 54,599/s（目标 100k，late slots 81.74%；12.8 已证单发布进程上限 ~93k）；引擎 received 31,233,006，占 sent 32,761,500 的 **95.3%**——NATS 订阅全程存活、无服务端断连、无 slow consumer 日志、无 DLQ。
- Delivered 窗口 475 值/s、全程均值 47.7 值/s；Batch 2.2/s（1,320 EVAL/600s）；`failed_total=0`（零写失败，正确性未破坏）、dropped 31,179,102（饥饿导致最新值缓存未确认覆盖）；p99 写 942.40 ms；DBSIZE 峰值 10,000 = 目标存活键，TTL 机制正常；RSS 73.5→132.6 MiB 有界。
- 独立 gRPC 探针在 324–564s 的 240s 窗口内**全部连接被拒**（Connection refused），但 604s 主测试成功取到 GetMetrics：事件循环被回调占满，内核 accept 队列得不到服务而发 RST，循环在末尾负载停止后恢复——**目标负载下控制面 gRPC 不可达（生产看门狗/RPC 将超时），记 P1（G5-F3）**。
- 三质量保持：本批负载全 good；三质量（good/bad/uncertain）语义由 stage2 Modbus 寄存器映射与引擎单测持续钉住，本批无质量回退路径改动。
- Redis 业务版本零回滚：#10 定向测试与 8 真实链路用例共同保证；600s 全程 `failed_total=0`。

**Gate-5 判定：FAIL。** 目标 ≥100,000 值/s、≥200 批/s 均未达到（实测单进程稳态天花板 23–25k 值/s、75.4 批/s，全规模下因饥饿坍塌到 ~500 值/s、2–4 批/s）；且 100k offered 本身超出单发布进程能力，目标容量在当前单进程架构下**不可证明**。

## 12.11 最终结论与移交

- 六个性能修复**正确性全部 PASS、无新增静态债务、无回归**：单进程稳态容量由上轮 ~6k 值/s 提升至 23–25k 值/s（约 4 倍），子饱和低延迟路径与饱和合并行为均符合设计；但 Gate-5 容量目标（10 万值/s、200 批/s）仍差 4 倍/2.7 倍，且目标拓扑下出现控制面饥饿，**总体验收 FAIL（容量），正确性面 PASS**。
- P1 台账（移交 ChatGPT，QA 不自行重构）：
  - **G5-F1**：在案 fixture/stage1 测试与生产 v1_17 `configs/` 路径契约漂移，基线 34 红灯（12.3）。
  - **G5-F3**：目标负载下 gRPC 控制面被事件循环饥饿打瘫（12.10），需架构级隔离（独立控制循环/线程或分片）。
  - **容量架构票**：单事件循环承载 ingest+20 引擎调度+串行 Redis 写的模型在 100k 目标下不成立，需按 `(wind_farm_id, collector_id)` source_identity 做进程间分片（现成分组键，当前无分片机制）；按实测单进程 ~24k 值/s 推导，达标需 ≥4 分片、建议 5 片留余量；批/s 维度需 ≥3 片。
- P2：G5-F2 路由缓存 8192 < 目标工作集 10000（结构性抖动）；G5-3 `write_success_total`/p50/p95 未进 GetMetrics 词汇；单发布进程 93k 上限使 100k 负载需多发布器分流。

## 12.12 分段瓶颈度量（§IV，独立负载侧采样，Offered/Received/Delivered/Batch/Drop 严格分口径）

75s 饱和诊断（20 Sink）独立 procfs/ss 采样 66.4s 窗口：

- **writer 进程 CPU = 100.70% 单核**（66.87 CPU-s / 66.4 wall-s）——asyncio 单事件循环在一个核上打满，这是首个饱和组件的直接证据；RSS 稳定 130.1→132.5 MiB（无泄漏）；监听 socket 全程存在，本诊断无 gRPC 客户端连接故 accept_recvq=0。
- Redis EVAL 速率 2.0/s（与批坍塌一致），Redis 容器 healthy、`io_inflight=0`——Redis 始终在等待，不是瓶颈。
- NATS 侧：received/sent = 4,210,110/4,415,500 = **95.3%**（600s 运行同为 95.3%，可复现），无 slow consumer 断连、无 DLQ；差距为发布侧迟到槽与 Core NATS 客户端缓冲，非 broker 丢消息。
- 发布侧物理上限（无 sink）：93,000 msg/s、30.2 MiB/s（粗 flush、0 迟到）；默认 200 flush/s 节拍下 86,333 msg/s、28.1 MiB/s。

分段口径汇总（20 Sink 全规模，600s）：

| 阶段 | 指标 | 实测 | 说明 |
|---|---|---|---|
| 发布（Offered） | msg/s | 54,599（迟到 81.7%） | 单 Python 发布进程上限 93k，100k 不可达 |
| NATS 接收（Received） | msg/s | ~52,058（sent 的 95.3%） | 无断连/无 DLQ |
| 路由+引擎 | 回调占满单循环 | CPU 100.7% 单核 | 路由缓存目标规模下每拍清空重建（G5-F2） |
| 调度输出（Batch） | EVAL/s | 2.2 | 健康水位可达 75.4/s（5 Sink） |
| 确认（Delivered） | 值/s | 窗口 475 / 全程 47.7 | 目标 100k |
| 丢弃（Drop） | 累计 | 31,179,102 | 最新值缓存未确认覆盖，非写失败（failed=0） |

生产代码未暴露逐阶段（decode/路由匹配/调度唤醒）内部计数器，进程内仅有聚合计数与 5 万容量延迟 deque；不在 QA 侧加装生产埋点，细粒度分段记 NOT_RUN，建议 ChatGPT 以架构票方式统一埋点。P50/P95 同理：GetMetrics 仅导出 P99（G5-3），进程域 P50/P95 NOT_RUN。

## 12.13 §VIII 八问（FAIL 分析，逐条作答）

1. **单进程最大稳态值吞吐**：23–25k 值/s（5 Sink 单元窗口 22,483、10 Sink 23,408 后转跌；60s 稳态、0 failed）。超过 ~25k offered 后 Delivered 不升反降，全规模坍塌至 ~0.5k。
2. **首个饱和组件**：Python 单事件循环的 NATS 回调主路径（JSON decode → 点路由解析 → 20 引擎 offer 簿记）。证据：CPU 100.7% 单核；NATS 95.3% 接收无断连；Redis `io_inflight=0`、健康水位 EVAL p99 仅 8 ms；三策略天花板相同，与触发策略无关。
3. **事件循环利用率与调度延迟**：利用率 100.7%（一核）；调度延迟代理指标 p99 写：健康 8–16 ms → 10 Sink 174 ms → 20 Sink 942 ms（600s）/1,645 ms（periodic 60s）；600s 中控制面 gRPC 连续 240s 不可达（accept 饥饿，机制为循环无法服务 accept，监听 socket 本身存活）。
4. **每次 EVAL 平均点数**：change_with_heartbeat 121–420（饱和合并生效，大批保住）；periodic 221–420；on_change 3–75（碎片化，批/s 高但值/s 不增）。
5. **200 批/s 瓶颈在 Python 还是 Redis**：Python。同一 Redis 在 on_change 低载实测 119.5 EVAL/s、p99 1.4–2.7 ms；坍塌时 Redis 侧仅 2–4 EVAL/s 且 healthy，容量闲置。
6. **本地优化能否达 10 万值/s**：不能证明可达，且单进程结构性不可能——单循环同时承担 100k msg/s 摄入（单发布进程自身上限仅 93k）与输出。本地优化（缓存容量对齐工作集、LRU、减少字典操作、更快 JSON）可抬升天花板但无法闭合 4 倍差距，需架构变更，按 P1 票移交，QA 不自行重构。
7. **是否需要水平分片**：需要。实测单进程 ~24k 值/s、75 批/s；100k 值/s 需 ≥4 分片（100/24≈4.2），建议 5 片留余量；200 批/s 需 ≥3 片。现成就绪的分片键是引擎已使用的 `(wind_farm_id, collector_id)` source_identity 路由分组；当前代码无任何分片机制，需新增配置级源分区与多进程部署形态。
8. **退化是否与发送模式/TTL/重复身份有关**：发送模式有关但非根因——独立探测证明 93k 上限属于单 Python 发布进程，与 sink 无关；TTL 无关——所有单元 DBSIZE 峰值精确等于预期存活键（500→10,000），3s max_age 钳制下无 TTL 类丢失；重复身份无关——负载 sample_id 逐拍唯一，冲突/重复语义由定向测试 #10 与真实冲突用例单独钉死。dropped 全部是饥饿窗口内最新值缓存的未确认覆盖。

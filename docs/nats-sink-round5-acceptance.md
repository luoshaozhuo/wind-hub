# Sink Service Round 5 真实服务集成与可靠性独立验收报告

- 分支：`feat/nats-sink-service`
- 验收基线 HEAD：`e380fb3c03cb47e7eb416dfa8c15bdcdf5b4d1f0`
- merge-base（origin/main）：`cb380ddb3bee249defb75e5922e2b70987a18211`
- 验收日期：2026-10-29
- 验收方式：全部目标服务经 Docker 真实部署，Collector 与 wind-hub-sink-service
  均为真实生产代码进程，真实网络通信；无任何 Mock 替代目标服务
  （R5-4 在测试侧实现真实 gRPC 服务端；R5-5 测试侧为真实 pymodbus 主站）。
- 总结论：**FAIL**（1 个 RELATED 阻断级生产缺陷，R5-2 reliable 持久化链路
  在固定版本 TDengine 上整体不可用；详见第 7 节）。

> 状态口径：本报告严格区分「行为断言已通过（真实链路实证）」「真实已验证」
> 与「未验证/NOT_RUN」。失败用例不使用 skip/xfail/ignore，不降低断言。

<!-- 报告提交后附最终状态：4 commits，工作区干净 -->

## 1. 基线、工作区与提交范围

| 项 | 值 |
| --- | --- |
| 验收基线 | `e380fb3c03cb47e7eb416dfa8c15bdcdf5b4d1f0` |
| merge-base origin/main | `cb380ddb3bee249defb75e5922e2b70987a18211` |
| 新增内容（仅测试/夹具/文档，零生产改动） | 见下方提交清单 |
| 工作区 | 4 commits 推送后干净 |

新增提交（基线 e380fb3c 之上）：

| SHA | 内容 |
| --- | --- |
| `e090234e` | test(system): R5 隔离真实服务 Docker 栈 fixture/compose |
| `2e090dfb` | test(system): R5-1/3/4/5/6 共 23 个真实链路用例 |
| `602d9a6e` | test(system): R5-2 TDengine COMPOSITE KEY 阻断钉死（7 失败） |
| 本提交 | docs: nats-sink-round5-acceptance.md（即本报告） |

新增/修改文件均为测试支撑：

- `tests/fixtures/services/docker-compose.r5.yml`：R5 独立 compose 栈；
- `tests/fixtures/services/r5_stack.py`：栈生命周期与容器级故障注入；
- `tests/system/r5/`：R5-1 ~ R5-6 测试套件与配置夹具；
- 本报告。

生产代码 `src/` 本轮零修改。

## 2. Docker 真实服务版本与隔离

- Docker server 29.2.1，Docker Compose v5.0.2。
- 镜像（全部固定明确版本，无 latest）：
  - `nats:2.10-alpine`（开启 JetStream `-js`，监控 8222）
  - `redis:7-alpine`
  - `tdengine/tdengine:3.3.5.0`（taosd + taosAdapter REST SQL 6041 同容器；
    `SHOW GRANTS` = **TDengine Community Edition**，`SHOW CLUSTER` = community）
  - `apache/kafka:3.9.1`（KRaft 单节点）
- 隔离：compose project 名 `wind-hub-r5-<8 位随机 hex>`，每次会话独立；
  宿主端口全部动态分配且只绑定 `127.0.0.1`；与既有 `wind-hub-test-*`、
  用户业务容器（`dc-ems-*` 等）无名称/网络/端口共享。
- healthcheck：NATS `/healthz`、Redis `PING`、TDengine `taos SELECT
  SERVER_VERSION()`、Kafka L4 探测；测试侧另做宿主侧 TCP + TDengine REST
  应用层就绪确认，并 `CREATE DATABASE wind_hub_r5{,b}`。
- 清理：夹具 teardown 仅对本 project 执行
  `docker compose down -v --remove-orphans`（清理本 project 容器/网络/匿名卷），
  未执行任何全局 `docker prune`，未停止/删除任何既有业务容器。
- 故障注入全部为容器级（`docker stop/start`、`pause/unpause`、
  `network disconnect/connect`），未对宿主机网卡执行 `tc`。

## 3. R5-1 Realtime → Redis（8/8 PASS，全部实读 Redis）

文件：`tests/system/r5/test_r5_01_realtime_redis.py`，marker real_service/redis/system。

| 用例 | 实证结果 |
| --- | --- |
| 全链路 Collector→NATS Core→Sink→Redis | PASS；独立 Redis 连接读到 good 质量值，持续采样时间戳推进 |
| 多 Sink 路由 | PASS；两个独立 prefix 各落一份 |
| 过期样本拒绝 + 新鲜样本接受 | PASS；`max_staleness_ms` 过期值不落库，新值落库 |
| 迟到旧值不覆盖新值 | PASS；5s 前旧值被拒，现值 sample_id/值保持 |
| 等时间戳歧义冲突 | PASS；等时间戳不同 sample_id 消息级拒绝（state 保持 running，failure 记账含 `equal-time`），Redis 现值不覆盖，**随后合法消息正常落库**（消费链路未被毒化） |
| PXAT 绝对过期 | PASS；`PTTL` 满足 0 < ttl ≤ max_staleness，6s 内键消失 |
| Stop/Start/Restart | PASS；Stop 期消息不落，Start/Restart 后续消息落库 |
| Redis 容器重启 | PASS；故障窗口 Sink 转 failed 且 failure 非空（realtime 允许窗口内丢失但必须报错），`start` 后 supervisor 5s 周期自动恢复 running、failure 清除、新消息落库 |

测试过程中发现并修正一处**测试自身缺陷**（非生产问题）：冲突恢复消息若使用
`exact_time + 1s`，会在亚秒级观察到拒绝后落入「未来时间戳」窗口，被生产
`_is_fresh` 的时钟漂移防护正确静默拒绝；改为发布当时取墙钟后稳定通过。

## 4. R5-2 Reliable → TDengine（0/7；1 个 RELATED 阻断缺陷，见第 7 节）

文件：`tests/system/r5/test_r5_02_reliable_tdengine.py`。

- `test_ddl_composite_key_accepted_by_real_tdengine`：**FAIL（阻断，RELATED）**——
  生产 `TDengineSink.open()` 的建表 DDL 被真实 TDengine Community 3.3.5.0
  拒绝（syntax error，code 9728）。
- 其余 6 个行为用例（PubAck+显式 ACK 落库 SQL 实证、sample_id 幂等、
  NAK 退避重投、同 durable 名进程重启重投、Stop 积压 Start 补齐、
  双 Durable 隔离与 ClearSink 只清自身）因同一 open 阶段失败全部 **ERROR/FAIL**，
  非独立缺陷；在阻断修复前这些语义在固定镜像上 **NOT_RUN（不可验证）**。

额外实证（手动 REST SQL 探针，用于刻画缺陷影响面）：在该社区版上对同一
(ts, sample_id) 重复 `INSERT` 不报错且**覆盖旧值**（count=1、LAST=新值）——
即生产代码依赖 COMPOSITE KEY 提供的 (ts, sample_id) 幂等语义在该镜像本身
也不成立。探针仅为只读诊断，未修改生产代码。

## 5. R5-3 Kafka（4/4 PASS，独立真实 consumer 读）

文件：`tests/system/r5/test_r5_03_kafka.py`。

| 用例 | 实证结果 |
| --- | --- |
| topic/key/value/身份 | PASS；独立 `AIOKafkaConsumer`（独立 group、earliest、seek_to_beginning）读到 key=address.field，value JSON 含 device/point/value/quality/timestamp/source=`collector-r5`；topic 名与配置一致 |
| 未映射点过滤 | PASS；gen_power 全程零消息，映射点正常 |
| Broker 断连恢复 | PASS；`docker stop kafka` 期间 send_and_wait 失败使 Sink failed 且 failure 含 Kafka（无错误不假成功）；`start` 后 supervisor 恢复，502.0 续写同一 topic |
| Stop/Start/Restart | PASS；停止期 601.0 确定不出现，Start 后 602.0、Restart 后 603.0 均可独立消费到 |

## 6. R5-4 gRPC 下行（3/3 PASS，测试侧为真实 gRPC 服务端）

文件：`tests/system/r5/test_r5_04_grpc_downlink.py`。

- 单/多点写与标量编码：PASS；double 12.5 与 int 300 经生产 GrpcSink 发往真实
  `GatewayDataService`，服务端解码后类型/值一致。
- `updated != len(points)` 部分写拒绝：PASS。诚实性说明——NATS realtime
  每消息仅一个采样，消息路径无法构造多点批；该用例**直接驱动生产
  `GrpcSink.write_task`** 向真实 gRPC 服务端发双点批（服务端/协议均真实，
  仅绕过单样本消息路径），断言 `RuntimeError: gRPC partial write: confirmed 1 of 2`，
  且服务端确实收到 2 点请求（拒绝发生在确认计数核对阶段）。
- 超时/对端重启：PASS；服务端 delay=5s + client timeout=1s 时 Sink failed，
  failure 含 deadline/timeout；对端在**同一端口**重启后 supervisor 自动恢复
  running、failure 清除、9.0 续写成功。

## 7. R5-5 Modbus TCP（5/5 PASS，独立 pymodbus 主站核对；未改 Protocol Adapter）

文件：`tests/system/r5/test_r5_05_modbus_tcp.py`，只驱动 sink_service。

- 编码与字序：PASS；float32(big/big)=3.14159、uint64(little/little)
  =1234567890123、int32(big/little)=-12345、int16=-42、coil=True，
  由独立主站按稀疏声明区间分别读回，struct 严格互逆解码核对。
- 主站写拒绝：PASS；write_register/write_coil 均返回错误帧
  （Server 型只读 Sink，ILLEGAL_FUNCTION 语义）。
- 非法/重叠配置：PASS；float32@100 与 int16@101 重叠在解析层抛
  `ConfigError`（match overlap）；slave_id=300 经 parse_domain 包装为
  `ConfigError`（原因含 less than or equal to 255）。
- 监听端口冲突：PASS；占用端口时 Sink failed，释放后 supervisor 恢复 running，
  新值 7 可读。
- 进程终止重启：PASS；sink_service 进程终止后同配置同端口新进程重新监听，
  内存寄存器清空，新样本 22 可读。

## 8. R5-6 故障矩阵与资源（3/3 PASS）

文件：`tests/system/r5/test_r5_06_reliability_matrix.py`（marker reliability）。

- NATS 不可达/恢复：PASS。实测可见性结论——容器 `network disconnect`
  对空闲 TCP 连接不可见（30s 内 nats_connected 仍为 True，无保活探测）；
  确定性注入方式为 `docker stop nats`（FIN 即时触发 nats_connected=False，
  随后重连尝试转 failed），`start` 后 supervisor `repair_subscription`
  自动恢复，恢复后新消息落 Redis。
- Redis 连续三轮 stop/start：PASS；每轮 failed→自动恢复→续写成立；
  `fd_count()` 增长 ≤ baseline+4，连接/FD 无线性泄漏。
- Collector 与 Sink Service 进程独立：PASS；真实 ModbusMockServer +
  Collector 进程终止期间 Sink 全程 running 且进程存活；新 Collector 进程
  复用同一配置后时间戳继续推进。
- JetStream 重绑/多 Durable 积压隔离/ClearSink：用例在 R5-2 套件中，
  因第 11 节阻断缺陷 **NOT_RUN（不可验证）**。
- TDengine/Kafka 容器重启：Kafka 侧 R5-3 已实证；TDengine 侧受同一阻断
  缺陷限制 NOT_RUN。
- Task/FD 泄漏其余面：见全量回归结果（待回填节）。

## 9. Round4 遗留 netem 7 项（NOT_RUN）

- 设施自检：`tests/system/network/test_netem_selfcheck.py` 3 项 PASS，
  证明 veth pair + tc netem 注入设施本身有效（非 root euid=1000 下经
  `unshare -rmn` 用户命名空间可建立链路并施加延迟/丢包）。
- 7 个降级用例在 unshare 挂载/网络命名空间子进程内执行时，docker socket
  不可达且未配置真实服务环境变量，夹具按设计 skip——本轮本机不满足
  「受控网络 + 真实服务」同时成立的执行条件，结论 **NOT_RUN**，
  非失败、非跳过测试债务（用例本身未打 skip，是运行环境门槛）。
- 本轮故障注入严格遵守授权边界：未对宿主机网卡执行任何 tc。

## 10. 质量门禁

### 10.1 静态门禁

| 门禁 | 命令 | 结果 |
| --- | --- | --- |
| Ruff lint | `ruff check src tests scripts` | **PASS（All checks passed）** |
| Ruff format | `ruff format --check src tests scripts` | **PASS（620 files already formatted）** |
| mypy | `mypy src` | **PASS（324 files, no issues）** |
| import-linter | `lint-imports` | **PASS（11 kept, 0 broken）** |

### 10.2 全量 pytest（不做 marker 裁剪）

命令：`pytest -q --ignore=tests/web_admin -p no:cacheprovider`

首轮结果：**1514 passed, 8 failed, 19 errors, 3 skipped（679.69s）**。
逐条裁定：

- 7 failed = R5-2 `test_r5_02_reliable_tdengine.py` 全部用例：**RELATED**，
  收敛于 D5-01（第 11 节），为刻意保留的稳定失败测试。
- 1 failed = `tests/industrial_gateway/test_gateway_r2_fc15.py::test_fc01_readback_matches_point_store`：
  **UNRELATED（main 基线）**。失败点为未映射 slave（device_id=9）读线圈
  预期异常帧却读到成功响应；与历轮验收记录的 FC15 既有失败同型，本轮
  零 src 改动、未触碰 industrial_gateway，不扩范围。
- 19 errors = `tests/system/acquisition`（2）+ `tests/system/recovery`（17）
  在 session 夹具 `_compose_stack` 启动阶段失败：**UNRELATED（本机环境）**。
  错误原文 `Bind for 0.0.0.0:6379 failed: port is already allocated`——
  本机存在验收开始前 9 小时启动的用户容器 `wind-hub-test-redis`
  （无 compose project 标签、挂默认 bridge）占用 6379，共享 compose 栈
  无法再绑定。与本轮 R5 栈无关（独立 project + 动态 loopback 端口）。
  以既有**真实** Redis/NATS 容器作为外部服务地址复跑全部 19 项：
  `WIND_HUB_TEST_REDIS=127.0.0.1:6379 WIND_HUB_TEST_NATS=nats://127.0.0.1:4222
  pytest tests/system/acquisition/test_modbus_data_flow.py tests/system/recovery/`
  → **19 passed in 114.49s**。

因此基线外新增失败 = 仅 R5-2 的 7 个 RELATED（同一根因 D5-01）。

清理核对：首轮失败的 compose 夹具留下本轮创建的
`wind-hub-test-redis-1`（Created 态）与空网络 `wind-hub-test_default`，
已精确删除；未对用户既有容器（`wind-hub-test-redis`、`wind-hub-test-nats`）
执行任何 stop/rm，未执行全局 prune。

### 10.3 Web Admin 门禁（src/web_admin）

| 门禁 | 结果 |
| --- | --- |
| `npm run lint`（eslint） | **PASS** |
| `npm run format:check`（prettier） | **PASS** |
| `npm run build`（vue-tsc --noEmit + vite build） | **PASS**（8.74s，仅 chunk 体积提示） |
| `npm run test`（vitest） | **PASS：185 passed（28 files）** |
| `npm run check:api`（OpenAPI 契约漂移） | **PASS（DRIFT CHECK: PASS，44 paths）** |
| `npm run test:e2e:mock`（Playwright mock） | 5 passed / **1 failed——UNRELATED main 基线** |

E2E 失败为 `mock.spec.ts:48 › mock 模式：Sinks 文件可用 / Modbus 不可达`，
断言 Sinks 表含 `file_archive` 行（mock.spec.ts:54），实际 mock handlers 只返回
redis_main/modbus_main。根因是 PR #92（deeee58e，已在 main）移除 FileSink 后
该 E2E 断言未同步更新：origin/main 上该测试文件逐字相同，而
`origin/main:src/web_admin/src/mocks/handlers.ts` 中 file_archive 出现次数为 0，
即 main 上同样必现失败。与 Round4 报告记录的 file_archive UNRELATED 为同一条，
本轮不扩大范围修复。

## 11. 新发现生产缺陷（RELATED，未修复，交 ChatGPT 裁定）

### D5-01（阻断）TDengineSink 建表 DDL 使用 TDengine Community 不支持的 COMPOSITE KEY

- 位置：`src/sink_service/infrastructure/adapters/tdengine.py:102-108`
  （`open()` 中 `CREATE STABLE ... sample_id VARCHAR(192) COMPOSITE KEY ...`）。
- 现象：任何 reliable/tdengine Sink 首次 open 即失败，Sink 进入 failed；
  reliable 历史持久化链路在固定镜像 `tdengine/tdengine:3.3.5.0`
  （Community Edition）上**完全不可用**。
- 服务端错误原文（taosAdapter REST，code 9728）：
  `syntax error near "key, value_json nchar(2048), quality nchar(32),
  receive_time timestamp, device_time timestamp, source nchar(64)) tags
  (device_id nchar(192), point_id nchar(192))"`
- 排除过程：在全新栈上逐一验证行内 `VARCHAR COMPOSITE KEY`、表级
  `COMPOSITE KEY(sample_id)`、`PRIMARY KEY(ts, sample_id)`、BINARY 类型变体
  均被同一社区版拒绝；对比默认 opener 与禁重定向 opener 行为一致，
  排除 HTTP 客户端层；`SHOW GRANTS` 确认 Community Edition。
- 影响面（确定性快速复现）：
  `tests/system/r5/test_r5_02_reliable_tdengine.py::test_ddl_composite_key_accepted_by_real_tdengine`
  ——仅构造生产 `TDengineSink` 并 `open()`，随后
  `SELECT sample_id FROM point_values LIMIT 0`。
  手工最小复现：启动 `tdengine/tdengine:3.3.5.0` 后向
  `http://127.0.0.1:<port>/rest/sql/wind_hub_r5`（Basic root:taosdata）
  POST 该 DDL 即得 code 9728。
- 关联设计风险：生产幂等设计依赖 (ts, sample_id) 复合键去重；实测该版本
  无复合键时同 (ts, sample_id) 重复 INSERT 覆盖旧值（count=1, LAST=新值），
  「已确认不重投/重复投递幂等」契约即使绕过 DDL 也不成立。
- 处置：按验收约束**未修改生产代码**；以稳定失败测试钉死，附带文件行号与
  错误原文。修复方向（供裁定，不在本轮执行）：改用该版本支持的去重机制
  （如以 sample_id 参与子表/标签建模或应用层幂等表），或将固定镜像改为
  支持 COMPOSITE KEY 的发行版并在 compose/文档中显式声明版本要求。

无其他新增 RELATED 生产缺陷。

## 12. NOT_RUN / 未验证清单

1. R5-2 全部 6 项 reliable 行为语义（ACK/NAK/重投/重启恢复/积压补齐/
   Durable 隔离与 ClearSink）——受 D5-01 阻断，固定镜像上无法验证。
2. netem 受控网络 7 项——unshare 命名空间内 docker 不可达且无真实服务
   环境变量，本机不满足执行条件；3 项设施自检 PASS。
3. TDengine 容器重启故障注入——同 D5-01。
4. 真实硬件/长稳 soak/性能门限——本轮范围外，NOT_RUN。

## 13. 结论

**FAIL。** R5-1/R5-3/R5-4/R5-5/R5-6 共 23 个真实链路用例全部通过且静态
门禁全绿，但架构 v0_4 的 reliable 持久化半壁在任务指定的固定版本真实
TDengine（Community 3.3.5.0）上因 D5-01 整体不可用，且该版本不具备设计
依赖的 (ts, sample_id) 幂等能力。按约束不修改生产代码、不以 skip/降断言
掩盖，保留 7 个稳定失败用例（1 阻断 + 6 被连带）与完整复现证据，
交 ChatGPT 裁定后复测。

本轮不创建 PR、不合并、不 rebase、不 force push。

# wind-hub Storage / Sink 容量整改独立复测（2026-11-27）

## 最终裁决

| 项目 | 结果 | 证据与边界 |
|---|---|---|
| STATIC_GATES | **PASS** | Ruff、format、mypy、import-linter、protobuf、配置与全仓收集通过。 |
| STORAGE_STMT2_RELIABILITY | **PASS** | 五 TAG、复用、缓存/连接隔离、UNKNOWN 与 offset 防护基线通过。 |
| STORAGE_PUBLISHER_300 | **PASS** | 64/128/256 在途独立 Kafka ACK 均显著高于 2850 行/s。 |
| STORAGE_CAPACITY_30 | **PASS** | 299.929 行/s，18000 行完整。 |
| STORAGE_CAPACITY_100 | **BLOCKED** | 本轮 925.434 行/s，低于 950；不能沿用前轮通过覆盖本轮回归。 |
| STORAGE_CAPACITY_300 | **BLOCKED** | 发布器 2999.778 行/s，Storage 仅 1132.980 行/s。 |
| SINK_PARAMETER_MATRIX | **BLOCKED** | 六组合法参数均未达 285 行/s。 |
| SINK_CAPACITY_600S | **BLOCKED** | 最优候选 p4/c16 为 205.75 行/s且未排空。 |
| SINK_SHUTDOWN | **PASS** | 内部 fetch 取消被显式恢复，实际 client/socket 关闭；外层取消不吞掉。 |
| CROSS_MODULE_E2E | **BLOCKED** | 功能回归 11/11，但强制容量未通过。 |
| PRODUCTION_READINESS | **BLOCKED** | Storage 100/300 与 Sink 600 秒未关闭。 |

总体 **BLOCKED**。发布器不是此前 300 台不达标的唯一原因；Storage 300 台和 Sink 持续容量仍需生产整改。

## Git、范围与证据

- 分支：`feat/storage-wide-table-completion-20261125`。
- fetch 后本地/远端 HEAD：`ba44818292b7b481bc5263657de06cc822b7ed7a`，工作树干净；自上一轮 `132d5ac2c7515f54a5156b72a254cfc491743b1b` 起共 11 个提交。
- 最终源码/测试 HEAD：`8bf47bbf1`，完整 SHA 见 `source_head.txt`；报告和证据随后独立提交，最终 HEAD 以交付答复和 Git 元数据为准。
- 环境：Python 3.11.16 / WSL2；TDengine 与 taosdump 3.3.6.13、taos-ws-py 0.7.1、Kafka 3.9.1、NATS 2.10、aiokafka 0.14.0。
- 证据目录：[evidence](storage_sink_capacity_retest_20261127_evidence/)，含 `test_results.json`、`capacity_summary.json`、`environment.json`、原始日志、JUnit 和采样 JSONL。

## 阶段 A：静态与回归

最终静态：Ruff check PASS、format PASS、mypy 494 文件 PASS、import-linter 22 kept/0 broken、protobuf PASS、pytest collection 0 error。Storage 282、Sink 428 项单元组件回归通过，项目 performance gate 7/7。

初始唯一 Ruff 错误是容量测试漏导入 `WideRecord`；`71d587c42` 修复并整理格式，未改变生产行为。已有 STMT2、多表缓存、跨模块功能与生产备份基线保留。

## 阶段 B：生产 STMT2 观测

本轮继续验证五 TAG、多设备一次 execute、连接内 statement 复用、Schema/数据库隔离、连接关闭/超时/UNKNOWN、部分成功后 offset 不提前提交、500 点完整回读及重复开关无泄漏，结果均通过。

300 台正式生产日志保存于 `storage_300_i128/storage.log`。9 个完整 256-write 窗口共 2304 次写入、166919 行、157534 个设备绑定；按行加权均值：

| 阶段 | 平均耗时(ms) |
|---|---:|
| Prepare | 0.0023 |
| Column 构造 | **34.4173** |
| Bind | 8.0708 |
| Execute | 10.6136 |

完整窗口平均每次 execute 72.45 行、68.37 个设备绑定。单个窗口 columns 为 29.857–45.108ms，bind 6.748–10.409ms，execute 6.473–17.323ms。生产 CPU 平均 91.42% 单核，列构造是当前最大的已测生产阶段；这些是生产日志，不是原生 STMT2 代理计时。详细窗口见 `stmt2_stage_summary.json`。

## 阶段 C：Storage 容量

### C1. 发布器独立验收

独立测试不启动 Storage，300 台 × 600 周期，180000 行、90000000 点，Kafka acks=all、幂等、严格 WideRecord 解码抽检：

| 在途上限 | ACK 行/s | Points/s | MB/s | ACK P95/P99(ms) | 峰值 pending |
|---|---:|---:|---:|---:|---:|
| 64 | 16447.80 | 8223899 | 289.54 | 5.63/7.72 | 64 |
| 128 | **25580.70** | 12790350 | 450.31 | 5.62/8.18 | 128 |
| 256 | 18667.66 | 9333832 | 328.61 | 25.53/31.27 | 256 |

三档均 PASS，峰值 pending 严格有界，异常清理和 2000 个质量位正确。128 档最高；256 档吞吐下降且尾延迟增大。证据为 `publisher_64/128/256.json/.xml/.log`。

### C2. 正式 Storage 梯度

所有正式工况 writer_count=2、producer max in-flight=128：

| 设备 | 发布行/s | Storage 端到端行/s | 排空(s) | 数据与裁决 |
|---|---:|---:|---:|---|
| 30 | 299.981 | **299.929** | 0.010 | 18000 行完整，PASS |
| 100 | 999.898 | **925.434** | 4.828 | 60000 行完整，低于 950，BLOCKED |
| 300 | **2999.778** | **1132.980** | 98.869 | 180000 行完整，低于 2850，BLOCKED |

300 台发布器达到要求后，Storage 才作正式裁决，因此归类 `STORAGE_CAPACITY_BLOCKED`，不是 `PUBLISHER_BLOCKED`。300 台逐设备 count=600，首末行各 500 值和 GOOD 质量通过；最终完整不能替代持续容量。100 台由前轮 954.699 降至 925.434，需作为本轮回归失败保留，不选择性取高。

300 台 Kafka ACK P95/P99=6.649/7.094ms；Storage CPU 平均/峰值=91.42%/111.86% 单核，VmHWM=317.38MiB。当前主要量化瓶颈为列构造及 Storage 消费/调度链路。1000 台 **NOT_RUN**。

## 阶段 D：Sink 参数矩阵与 600 秒

### D1. 六组合法短矩阵

每组 60 秒、30 台、500 点、linger 10ms。全部工况最终 drained=true、identity_closed=true、consumer_closed=true，但稳态均低于 285 行/s：

| partitions / inflight | 行/s | 积压斜率(行/s) | CPU%(单核) | RSS MiB | fetch/encode/write(ms) | submit/confirm(ms) | global/physical/native quota(ms) |
|---|---:|---:|---:|---:|---|---|---|
| 1 / 16 | **225.02** | 75.173 | 69.0 | 247.2 | 30.193/0.443/96.429 | 25.675/37.426 | 7.363/9.786/66.087 |
| 1 / 32 | 214.18 | 85.943 | 63.7 | 247.3 | 54.573/0.444/77.402 | 16.225/8.776 | 18.327/10.712/26.224 |
| 1 / 64 | 215.80 | 84.021 | 64.2 | 248.1 | 51.318/0.445/80.795 | 17.486/12.849 | 17.087/10.962/31.426 |
| 4 / 16 | **229.04** | 70.647 | 73.3 | 248.0 | 28.316/0.436/93.811 | 29.995/31.427 | 9.157/9.409/64.064 |
| 4 / 32 | 228.98 | 70.969 | 70.4 | 249.2 | 36.357/0.438/87.193 | 25.113/24.380 | 10.173/8.644/52.365 |
| 4 / 64 | 210.30 | 89.585 | 66.0 | 248.6 | 38.112/0.461/93.927 | 26.420/26.132 | 12.328/8.319/56.275 |

最高 p4/c16 仅 229.04 行/s。编码已缩短到约 0.44ms，但 fetch/write/native quota 和 submit/confirm 仍占主要耗时；提高 inflight 并未改善吞吐，部分档位反而增加 fetch/global 等待。完整分区、builder、顺序、重复和 Event Loop 数据见各工况 JSON/JSONL。

### D2. 600 秒正式长测

短测最优 p4/c16 完整执行 600 秒：

| 指标 | 本轮 p4/c16 | 前轮原 c16/p1 基线 |
|---|---:|---:|
| 稳态 Kafka 确认行/s | **205.75** | 210.27 |
| 积压斜率行/s | **+94.214** | +89.742 |
| drained / zero_loss / identity_closed | false / false / false | false / false / false |
| 审计独立身份 / 重复身份 | 151071 / 0 | 157726 / 0 |
| CPU / RSS 峰值 | 92.5% / 249.6MiB | 90.5% / 249.1MiB |
| gRPC P95/P99(ms) | 5.000/5.862 | 5.196/5.883 |
| 写入窗口 P99 最大值(ms) | 110.772 | 116.473 |

fetch/encode/write 均值 31.739/0.464/104.549ms，submit/confirm 35.879/32.344ms，global/physical/native quota 11.531/9.524/71.108ms。设备顺序与分区无违规，无重复身份；未确认数据不直接认定为永久丢失。短测最优未形成持续容量，生产瓶颈交 ChatGPT。

### D3. CancelledError 与实际关闭

根因为 aiokafka 0.14.0 `Fetcher.close()`：取消 pending fetch 后逐个 await，内部取消会以 `CancelledError` 冒出，不是测试调用方被取消。容量 helper 现区分外层取消与内部取消，取消剩余内部任务，显式关闭 client，并检查 reader/writer 释放及 closed future。

长测 `audit_stop_error=aiokafka_internal_fetch_cancelled`，但 `audit_consumer_closed=true`、`order_consumer_closed=true`，证明实际完成关闭；未把异常字符串清空。外层调用方取消的单元测试仍传播 `CancelledError`，两项专项测试通过。identity_closed=false 来自持续容量和缺失身份，不再由关闭状态本身造成。首轮无效矩阵因误用不存在的 `closed()` 保留在 `sink_matrix_invalid/`，修正后六组全部重跑。

## 阶段 E：跨模块功能回归

`cross_module_e2e.xml` 为 **11/11**：Data Generator → Gateway → Collector → NATS/JetStream → Sink → Kafka → Storage → TDengine，覆盖 FC03/FC04、RAW/PROTOCOL/BUSINESS、版本/设备身份、配置发布激活、Kafka 重放、Storage 重启、查询聚合导出和备份基线。

功能回归通过不等于容量通过，CROSS_MODULE_E2E 与 PRODUCTION_READINESS 均保持 **BLOCKED**。1000 台、三等级同时容量和完整 DCEMS 控制闭环为 NOT_RUN。

## 修复提交与未关闭问题

- `71d587c42`：容量测试 WideRecord 导入与格式门禁。
- `9a49654c0`：独立发布器三档验收；aiokafka 内部取消恢复与外层取消区分。
- `8bf47bbf1`：以真实 reader/writer/closed-future 验证 socket 关闭，保留无效矩阵和重跑证据。

未关闭：

1. **P1 STORAGE_CAPACITY_BLOCKED**：100 台 925.434、300 台 1132.980 行/s，低于目标；列构造和 Storage 调度需继续优化。
2. **P1 SINK_CAPACITY_BLOCKED**：六组短测及 p4/c16 600 秒均失败；fetch/write/native quota 和 submit/confirm 是主要量化瓶颈。
3. **P2 OBSERVABILITY**：writer queue 内部时延和更细阶段分布仍需低开销补充；当前不估算。
4. **QUALIFICATION**：1000 台、三等级同时容量、真实 PLC、完整 DCEMS 控制闭环 NOT_RUN。

**最终结论：BLOCKED。** 发布器、Storage 30 台和关闭实际清理通过；Storage 100/300 台与 Sink 600 秒强制容量未关闭，不能进入 READY_FOR_PRODUCTION_REVIEW。

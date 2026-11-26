# Storage 宽表 D5 第三轮独立复测（2026-11-26）

最终裁决：**BLOCKED**。D5R2-01 缺失表语义已用真实 TDengine 关闭；生产 PTY 备份、原生多表 STMT2 和单实例容量仍未达标。并行写入没有取得有意义的吞吐收益，不能用模块全绿或最终持久化补齐认定生产 READY。本轮未创建 PR、未合并 main。

## Git、环境与证据

- 分支：`feat/storage-wide-table-completion-20261125`。
- fetch 后本地/远端 HEAD 均为 `484e23d2a4962cf195594c0355455a1f979a454d`，工作树干净；其后恰为本轮 14 个提交。
- 最终源码/测试 HEAD：`31ee6b8ff997c21c067b7b23d04f2d6555a56158`，已推送；报告与证据随后独立提交，最终 HEAD 以交付答复和 Git 元数据为准。
- 权威证据目录：[evidence](storage_wide_table_d5_round3_20261126_evidence/)，含 `test_results.json`、`capacity_summary.json`、`environment.json`、原始日志、JUnit、采样 JSONL 和派生结果。
- Python 3.11.16 / WSL2；TDengine 与 taosdump 均为 3.3.6.13，Kafka 3.9.1，NATS 2.10，aiokafka 0.14.0，taos-ws-py 0.7.1。真实 taosdump 与 libtaos 来自同版本镜像，不是 wrapper。
- 性能工况串行执行；单 Sink、单 Storage，未通过多应用实例提升吞吐。

## 静态检查与模块回归

| 检查 | 结果 | 证据 |
|---|---|---|
| Ruff check | PASS | final_ruff.log |
| Ruff format --check | PASS，1133 文件 | final_format.log |
| mypy src | PASS，494 文件 | final_mypy.log |
| import-linter | PASS，22 kept / 0 broken | final_imports.log |
| protobuf --check | PASS | final_protobuf.log |
| pytest collection | PASS，4232 项、0 错误 | final_collection.log |

| 模块/专项 | tests | failed | errors | skipped | 证据 |
|---|---:|---:|---:|---:|---|
| Contracts / Core | 667 | 0 | 0 | 0 | contracts_core.xml |
| Configuration | 487 | 0 | 0 | 2 | configuration.xml |
| Collector | 120 | 0 | 0 | 0 | collector.xml |
| Sink unit/component | 425 | 0 | 0 | 0 | sink.xml |
| Storage unit/component | 268 | 0 | 0 | 0 | storage_final.xml |
| CLI | 287 | 0 | 0 | 13 | cli_final.xml |
| 项目 performance | 7 | 0 | 0 | 0 | performance.xml |
| 真实 Kafka 重放/重启/重平衡 | 1 | 0 | 0 | 0 | kafka_real_final.xml |

Configuration 2 个 skip 为 NATS 广播环境不可用，CLI 13 个 skip 为 containerd/iSulad 不可用，均不计为通过。新增代码重点审查了 gather 异常优先级、同设备分组、offset 提交条件、PTY 子进程取消/超时及描述符关闭。

## D5R2-01：缺失历史表

**PASS，已关闭。** `missing_table_real.xml` 为 14/14 真实集成用例，普通查询与 Quality 过滤各覆盖一次：

1. Device Instance 存在但子表未创建，返回空页。
2. Farm 同时含已建表与未建表设备，只返回已建表设备的 2 行 GOOD 数据。
3. 物理表存在但缺失可信登记，返回 schema conflict。
4. 登记为 CREATING，返回 schema conflict。
5. 登记 Schema 指纹不一致及实际 TAG 不一致，分别失败。
6. replay `read_available=False` 时，普通与 Quality 查询均返回 `HistoryReplayUnavailableError`，没有借 absent 分支绕过。

后台、SQLite 元数据和独立测试数据库均关闭清理。该证据不覆盖未来新 Schema，但当前实现的缺失/冲突区分成立。

## D5R2-03：生产 PTY 备份

**FAIL，未关闭，生产备份不得启用。** `backup_pty_real.xml` 使用正式 CLI 启动 Configuration 和 Storage，真实写入并回读 2 行 × 500 点，随后执行 CreateBackup/GetBackupTask：

- 实测 `stdin_isatty=true`，但 taosdump `tty_nr=0`、`controlling_tty=false`，打开 `/dev/tty` 返回 `ENXIO(6)`。
- 仅把 slave PTY 接到 stdin 未建立 controlling terminal；没有 `setsid`/`TIOCSCTTY` 或等价步骤。合法 30 秒超时后任务为 `STORAGE_TASK_STATE_FAILED`、`NATIVE_BACKUP_FAILED`。
- 超时 PID 已回收；`backup_fd_unit.xml` 另有 11/11，精确验证正常、取消和启动失败路径的进程 FD 数不增加。
- argv、任务错误、Storage 日志、失败 artifact 和证据日志均未发现密码。

由于备份失败，可信 artifact、清单、历史配置、taosdump 重命名恢复及恢复后 500 点回读均为 **NOT_RUN**。该结构性问题交 ChatGPT，不以原生工具能力替代生产 RPC。

## Storage 并行写入

`parallel_ingestion_unit.xml` 为 17/17，覆盖 writer_count=1/2/4/8 的并发上限、同设备 last-offset-wins、混合设备/Schema、单设备失败、多组部分成功、UNKNOWN 优先、取消清理和连续 offset 确认。任意物理组未确认时不会提交整个 Kafka 批次。

原生多表 `INSERT INTO ?` 仍在 prepare 阶段以 `0x022B Table does not exist` 失败，`native_multitable.xml` 为 2 failed；未恢复未经验证的 API。原生固定表名 STMT2 为 **1798.30 行/s、899148 点/s、7.418 MB/s**，批 P95/P99=38.90/55.46 ms，见 `native_single_table_baseline.xml`。

## Sink 容量

### 短矩阵

12 个单元均为 30 台、500 点/行、60 秒、单 partition。全部 drained=true、identity_closed=true，但没有单元达到 300 行/s。

| 并发 / linger(ms) | 行/s | 点/s | MB/s | 积压斜率(行/s) | CPU%(单核) | RSS MiB |
|---|---:|---:|---:|---:|---:|---:|
| 8 / 0 | 243.98 | 121988 | 1.915 | 56.047 | 74.8 | 247.6 |
| 8 / 1 | 248.46 | 124228 | 1.996 | 51.432 | 78.2 | 246.6 |
| 8 / 5 | 242.98 | 121492 | 1.908 | 56.743 | 74.7 | 247.4 |
| 8 / 10 | 236.57 | 118287 | 1.843 | 63.452 | 70.3 | 246.5 |
| 16 / 0 | 240.85 | 120426 | 1.906 | 59.055 | 75.2 | 247.2 |
| 16 / 1 | 248.85 | 124423 | 1.939 | 51.016 | 76.3 | 247.0 |
| 16 / 5 | 249.42 | 124708 | 1.939 | 50.214 | 75.0 | 248.8 |
| 16 / 10 | **260.14** | 130072 | 2.008 | 39.684 | 78.0 | 247.4 |
| 32 / 0 | 252.70 | 126350 | 1.949 | 47.475 | 76.8 | 248.1 |
| 32 / 1 | 222.94 | 111472 | 1.783 | 77.083 | 69.4 | 246.9 |
| 32 / 5 | 222.24 | 111118 | 1.755 | 77.994 | 67.2 | 247.8 |
| 32 / 10 | 218.98 | 109491 | 1.746 | 80.905 | 67.5 | 247.8 |

最优短测组合 c16/l10 的物理 builder 平均 5044.843 点，native quota 平均等待 57.080 ms；合批增强存在，但未形成 300 行/s 容量。各单元 P95/P99、global/physical/native quota 见 `sink_short_matrix_metrics.json`。

### c16/l10 600 秒正式验收

**FAIL**。完整 600 秒发布、受控停止和审计后：

| 指标 | 实测 |
|---|---:|
| 发布 / Kafka 确认行 | 180000 / 180000 |
| 发布 / Kafka 确认点 | 90000000 / 90000000 |
| 稳态 Kafka 确认 | 250.38 行/s、125190 点/s |
| 稳态积压斜率 | +49.616 行/s |
| 实际 Kafka payload 端到端 | 2.451 MB/s |
| drained / zero_loss | true / true |
| identity_closed | **false：`audit_stop_error=CancelledError`** |
| CPU / RSS 峰值 | 91.6% 单核 / 250.6 MiB |
| gRPC 探针 P95/P99 | 3.941/4.441 ms |
| 写入窗口 P99 最大值 | 87.027 ms |
| global/physical/native quota 均值 | 4.582/2.133/59.182 ms |

相较第二轮 218.29 行/s提升约 14.70%，但仍低于 300 行/s且稳态积压增长。审计有 180000 个不同身份、零重复；consumer stop 取消异常也使身份闭合门禁失败。前三次启动/清理异常保留为基础设施证据，未混作业务容量结果。

## Storage 容量与 writer_count

所有工况均为真实 Kafka v3 → 单 Storage → TDengine、500 点完整设备行，逐设备 600 周期。

| 设备 / writer_count | 发布行/s | 端到端行/s | 排空(s) | 完整性与裁决 |
|---|---:|---:|---:|---|
| 30 / 1 | 299.983 | 278.446 | 4.641 | 18000 行完整，低于 300×95%，FAIL |
| 30 / 2 | 299.983 | 278.507 | 4.627 | 18000 行完整，低于门槛，FAIL |
| 30 / 4 | 299.982 | 278.077 | 4.727 | 18000 行完整，低于门槛，FAIL |
| 30 / 8 | 299.980 | 278.067 | 4.729 | 18000 行完整，低于门槛，FAIL |
| 100 / 2 | 999.905 | 261.527 | 169.416 | 60000 行完整，低于 1000×95%，FAIL |
| 300 / 2 | 2446.839 | 163.580 | 900.332 | 仅 159310/180000 行，0 台完成 600 行，FAIL |

writer_count=8 时连接和 SQL 上限同步为 8；初始 ValidationError 是配置校验正确拒绝，修正后执行。30 台首末行各 500 个 float32 值及 GOOD 质量均回读通过。300 台首末行资格为 NOT_RUN，未确认 20690 行不直接认定为永久丢失。

并行写入没有有意义收益：30 台四档差异不足 0.2%；100 台由第二轮 292.07 降至 261.53 行/s，300 台由 170.80 降至 163.58 行/s。原生固定表名 STMT2 的 1798 行/s说明主要瓶颈在 Storage/消费/调度/确认链路，不是 TDengine 原生写入上限。**300 台未达标，1000 台探索 NOT_RUN。**

## 未关闭生产问题与移交

| ID | 状态 | 影响与负责人 |
|---|---|---|
| D5R2-01 | **CLOSED** | 缺失表/所有权冲突已由 14 个真实用例关闭。 |
| D5R2-02 | OPEN_CHATGPT | 原生多表 STMT2 prepare 失败，多表复用和部分成功路径未取得能力证明。 |
| D5R2-03 | OPEN_CHATGPT | PTY 无 controlling TTY，生产备份超时；需结构性子进程会话/认证修复。 |
| D5R2-04 | OPEN_CHATGPT | Sink/Storage 单实例容量不达标；并行写入无明显收益，需生产级瓶颈整改。 |
| D5R2-05 | OPEN_CHATGPT | aiokafka consumer stop 可抛 CancelledError，现保留证据并使 identity_closed=false；需评估生产关闭语义。 |

没有发现可据当前证据确认的 P0。生产备份、完整 Collector→Sink→Storage 容量、1000 台、恢复后数据回读及三等级同时容量均为 NOT_RUN。

## 本轮提交

- `bf7b9154a`：严格 CLI revision 与 Ruff 小修复。
- `6d8a9cdfd`：writer_count 容量参数化。
- `ef34d97e8`：真实缺失表 14 用例。
- `b9230ce57`：并行设备写入单元覆盖。
- `70545fa4c`：真实 PTY controlling-TTY 失败证据。
- `c818ec240`：PTY FD 精确清理测试。
- `6a62f58a2`：Kafka 启动 offset 有界重试。
- `7645988e0`、`30929b234`：审计停止异常与取消语义证据保留。
- `31ee6b8ff`：writer_count 与连接/SQL 上限对齐。

**最终裁决：BLOCKED。** D5R2-01 已关闭，其他生产缺陷和容量资格未关闭。不得以模块测试全绿、合批增强、最终补齐或原生单表性能代替生产资格。

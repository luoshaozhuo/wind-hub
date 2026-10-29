# Sink Service Round 7：TDengine 契约收敛与独立验收报告

- 分支：`feat/nats-sink-service`（未建 PR、未合并、未 rebase、未 force push）
- 复测起点生产 HEAD：`e00d3988057daea544a6a3423132d958da81ef76`
- merge-base（origin/main）：`cb380ddb3bee249defb75e5922e2b70987a18211`
- 验收日期：2026-10-30
- 上游本轮前提交：`e00d3988` fix(sink-service): remove incorrect TDengine minimum version claim
- 总结论：**PASS WITH LIMITATIONS**（D5-01/D6-01 关闭、D6-02 按已批准契约收敛；限制项见第 8 节）。

- 本轮提交：`18566fb4`（架构契约文档）、`75c97c43`（契约测试收敛）、
  报告提交（见 git log）。

## 1. Git SHA、merge-base、工作区

| 项 | 值 |
| --- | --- |
| 验收起点 | `e00d3988057daea544a6a3423132d958da81ef76`（fetch 后与 origin 一致） |
| merge-base origin/main | `cb380ddb3bee249defb75e5922e2b70987a18211`（main 未移动） |
| 验收前工作区 | 干净 |
| 本轮改动范围 | 仅架构文档（契约调整，任务书授权）、测试、本报告；**生产代码零改动** |

## 2. D6-01：版本兼容与启动错误信息

生产改动（上游 `e00d3988`）：[tdengine.py:113-116](../src/sink_service/infrastructure/adapters/tdengine.py)
的错误信息移除了不实的 `verified target >= 3.3.8.x`，改为
`requires a server supporting VARCHAR COMPOSITE KEY; verify TDengine server
compatibility`。复测结论：

| 验证项 | 结果 |
| --- | --- |
| 生产代码残留 `3.3.8` 字样 | **无**（静态测试 `test_production_code_has_no_incorrect_version_claim` 守护） |
| 真实版本实证 | `SELECT SERVER_VERSION()` = **3.3.6.13**；`SHOW GRANTS` = TDengine Community Edition |
| 复合主键 DDL | code=0 成功；`DESCRIBE point_values` 中 `sample_id` note = **COMPOSITE KEY** |
| 数据写入/查询 | 超级表与哈希子表写查正常（七项 E2E 全部 SQL 实证） |
| 旧版（3.3.5.0）行为 | 独立真实容器（`tdengine/tdengine:3.3.5.0`，动态 loopback 端口）驱动生产 `open()`：抛 `SinkError`，消息含 `COMPOSITE KEY` 兼容性指引且**不含** `3.3.8`——fail-closed，不假成功 |
| Fixture pin | `docker-compose.r5.yml` pin 公开存在的 `tdengine/tdengine:3.3.6.13`（Round6 已修） |

**D6-01 关闭。**

## 3. D6-02：幂等契约收敛与真实数据库行为

本轮按任务书已批准决策收敛契约：**Sink Service 保证相同采样消息重复投递时的
最终存储幂等，不承诺同一主键下冲突内容的首写不可变。**

真实 TDengine Community 3.3.6.13 SQL 实测（`test_r6_tdengine_contract.py`）：

| 场景 | 实际行为 | 契约判断 |
| --- | --- | --- |
| 同 ts、不同 sample_id | 两条均保留 | ✅ 复合主键生效 |
| 同 ts、同 sample_id、同内容重复写 | 最终一条，内容一致（两次写均 code=0） | ✅ **相同消息最终存储幂等**（承诺项成立） |
| 同 ts、同 sample_id、**不同内容** | 第二次写 code=0，旧值 100.0 被覆盖为 200.0，最终一条 | ⚠️ **UPSERT 覆盖**（契约第 3 项明确允许，禁止宣称不可变） |
| 同 sample_id、不同 ts | 两行历史均保留 | ✅ 主键为 `(ts, sample_id)`，sample_id 非全局唯一 |

- 冲突覆盖的真实证据以**正向期望**保留：`test_conflicting_value_redelivery_follows_documented_upsert_contract`
  断言第二次写成功且最终值为 200.0，把 UPSERT 行为钉死，防止数据库行为静默漂移。
- Round6 的 `pytest.fail("CONTRACT VIOLATION ... first-write immutable")`
  已按新契约删除——这是**业务契约调整**（任务书第四节明文授权），不是把失败
  伪装成修复：行为证据仍在，且新增文档披露守护测试。
- 正常重投路径（JetStream 重发同一消息字节）只会产生同内容 UPSERT，结果幂等；
  丢数据、重复行、错误 ACK 均未观察到。

**D6-02 按新契约关闭；首写不可变明确为非承诺项。**

## 4. 文档、生产注释、测试一致性

架构文档 [wind-hub_sink_architecture_v0_4.md](wind-hub_sink_architecture_v0_4.md)
更新（遵循该文档既有表格/分节风格；仓库中《报告模板.md》《符号体系.md》不存在，
无其他适用规范文件）：

1. 表 5-1 reliable「重复」单元格：改为「相同消息重投最终存储幂等；同主键冲突
   内容按目标 UPSERT 语义处理，不承诺首写不可变（见 6.2）」。
2. 6.2「历史去重」扩写为**历史去重与存储幂等契约**四条：幂等键 `(ts, sample_id)`、
   上游同 `sample_id` 内容不可变、相同消息最终一条、同主键异值 UPSERT（不新增
   去重库/状态机/读前检查）、ACK 只能在目标确认写入后发出。
3. 新增 **9.5 TDengine 历史适配器契约**：复合主键 DDL、UPSERT 语义、版本兼容
   （已验证 Community 3.3.6.13，旧版 open fail-closed，不声称未经核实的版本号）。
4. 表 12-1 新增两行已确定决策（历史幂等语义、TDengine 版本）。
5. 9.3「具体幂等策略在详细设计中落实」的悬置措辞改为引用 6.2/9.5。

一致性守护测试：`test_architecture_doc_discloses_upsert_contract` 断言文档包含
`(ts, sample_id)`、`UPSERT`、`首写不可变`、上游不可变要求、ACK 边界、
`3.3.6.13`、`COMPOSITE KEY` 七个关键披露要素。

## 5. 原 R5-2 七项 Reliable 测试

`pytest tests/system/r5/test_r5_02_reliable_tdengine.py -v`：**7/7 PASSED（45s）**，
全部以真实 SQL 查询为断言：持久化落库（顺序/值/quality/子表计数/pending=0）、
重复投递单行、TDengine stop→failed→NAK 退避→start 后补齐且 failure 清空、
进程终止后同 deployment 重绑同名 durable 重投未 ACK 消息、StopSink 积压
StartSink 补齐、双 Durable 分库隔离、ClearSink 只跳过自身积压（skipped≥2、
不影响另一 durable、不删既有数据）。

## 6. 其他四种 Sink 真实链路回归

`pytest tests/system/r5/`：**39 passed in 252.65s，零失败**。

- R5-1 Realtime→Redis 8 项；R5-3 Kafka 4 项；R5-4 gRPC 3 项；R5-5 Modbus TCP
  5 项；R5-6 故障矩阵 3 项；R5-2 7 项；R6/R7 TDengine 契约 9 项。
- TDengine 镜像/契约变化未影响共用 Docker 栈（NATS 2.10 / Redis 7 / Kafka 3.9.1
  KRaft 均为独立 project、动态 loopback 端口，teardown 只清本 project）。

## 7. 质量门禁

### 静态门禁

| 门禁 | 结果 |
| --- | --- |
| `ruff check src tests scripts` | **PASS** |
| `ruff format --check src tests scripts` | **PASS（621 files）** |
| `mypy src` | **PASS（324 files）** |
| `lint-imports` | **PASS（11 kept, 0 broken）** |

### 全量 pytest

`pytest -q --ignore=tests/web_admin`（不做 marker 裁剪）：**1530 passed,
1 failed, 19 errors, 3 skipped（589.53s）**。

- failed ×1：`test_gateway_r2_fc15.py::test_fc01_readback_matches_point_store`
  ——**UNRELATED**（main 基线 FC15，未映射 slave 读线圈）。
- errors ×19（acquisition 2 + recovery 17）：**UNRELATED（本机环境）**，
  同 R5/R6——用户既有容器 `wind-hub-test-redis` 占用 `0.0.0.0:6379`，共享
  compose 夹具 `port is already allocated`。清理本轮残留
  `wind-hub-test-redis-1`/空网络后，以既有真实 Redis/NATS 作外部地址复跑：
  `WIND_HUB_TEST_REDIS=127.0.0.1:6379 WIND_HUB_TEST_NATS=nats://127.0.0.1:4222
  pytest tests/system/acquisition/test_modbus_data_flow.py tests/system/recovery/`
  → **19 passed in 115.71s**。
- 与 R6（1526 passed / 2 failed / 19 errors）对比：D6-02 稳定契约红按批准
  的契约调整转绿（新增 3 项一致性/fail-closed 测试）；FC15 与环境 ERROR 集合
  完全同态；**基线外零新增失败**。

### Web Admin 门禁（src/web_admin，本轮无前端改动）

| 门禁 | 结果 |
| --- | --- |
| `npm run lint` / `format:check` | PASS / PASS |
| `npm run build`（vue-tsc + vite） | PASS（5.09s） |
| vitest | **185 passed（28 files）** |
| `check:api`（OpenAPI 漂移） | PASS（DRIFT CHECK: PASS） |
| `test:e2e:mock`（Playwright） | 5 passed / **1 failed——UNRELATED main 基线**（file_archive 陈旧断言，同 R5/R6） |

## 8. RELATED / UNRELATED / NOT_RUN

**RELATED**：无新增。D5-01、D6-01 关闭；D6-02 按批准契约收敛（UPSERT 为
明示允许行为，非缺陷）。

**UNRELATED（main 基线/本机环境）**

- FC15 `test_gateway_r2_fc15.py::test_fc01_readback_matches_point_store`（若失败）：
  未映射 slave 读线圈基线问题，历轮同型。
- Playwright mock E2E `file_archive`：PR #92 移除 FileSink 后陈旧断言，
  origin/main 同测必现。
- 全量首轮 recovery/acquisition 的 6379 端口冲突 ERROR（若出现）：本机用户
  既有容器 `wind-hub-test-redis` 占用，使用 `WIND_HUB_TEST_REDIS` /
  `WIND_HUB_TEST_NATS` 指向既有**真实**服务复跑，不停止用户容器。

**NOT_RUN**

- netem 受控网络 7 项：unshare netns 内 docker socket 不可达（同 R5/R6 环境
  边界），设施自检 3 项 PASS；未对宿主机网卡执行 tc。
- 真实硬件 / soak / 性能门限：范围外。
- TDengine 企业版/集群版复合键行为差异：仅验证 Community 3.3.6.13 单节点。

## 9. 修改文件与提交

| 文件 | 性质 |
| --- | --- |
| `docs/wind-hub_sink_architecture_v0_4.md` | 契约文档收敛（任务书授权） |
| `tests/system/r5/test_r6_tdengine_contract.py` | S3 改 UPSERT 正向期望；新增文档披露/版本声明/3.3.5.0 fail-closed 三项真实测试 |
| `docs/nats-sink-round7-acceptance.md` | 本报告 |

生产代码：**本轮零修改**。

提交 SHA：`18566fb4`（文档）、`75c97c43`（测试），报告随本提交。

## 10. Squash Merge 建议

**有条件满足（PASS WITH LIMITATIONS）。**

- 功能正确性：reliable 七条端到端 + 四场景存储语义 + 旧版 fail-closed 全部真实
  服务实证；ACK 仅在目标确认后、失败不 ACK 重投幂等的契约边界清晰且有文档/测试
  双重守护；静态门禁全绿；基线外零新增失败。
- 合并前建议（非阻断）：在部署文档/Release Note 中同步「TDengine ≥ 支持
  VARCHAR COMPOSITE KEY 的版本，已验证 Community 3.3.6.13」这一部署前置条件，
  避免运维侧继续使用 3.3.5.0 类旧实例。
- 遗留限制（同前几轮）：netem/真实硬件/soak NOT_RUN；FC15 与 file_archive
  为 main 基线 UNRELATED。

本轮不创建 PR、不合并，等待 ChatGPT 审核。

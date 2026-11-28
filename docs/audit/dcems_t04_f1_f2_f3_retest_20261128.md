# wind-hub F1/F2/F3 生产整改独立复测报告（2026-11-28）

## 1. 目标与 Git 基线

| 项目 | 值 |
|---|---|
| 分支 | `feat/dcems-gateway-smoke-20261126` |
| 整改 HEAD（起始） | `865f78a41eb412210bedce23965c234e5e43c54c`（fetch 后本地=远端） |
| 上游 T04 提交 | `5fdd369a` |
| 整改范围 | `git diff 5fdd369a..865f78a4`：11 文件 +308/−11（见 evidence/environment/remediation-files.txt） |

范围：F1 int→bool 显式转换、F2 RealtimeDelivery 有界停止/恢复、Gateway 读取新鲜度 `read_stale_after_ms`。
DCEMS 发布包不可变（MANIFEST 133 OK，`third_party/` 零改动）。

## 2. F1：int→bool 显式转换 — PASS（合同）+ PASS（E2E）

整改：`target_mappings.conversion: int01_to_bool`（可选字段）→ 配置校验（`application/config.py`）
→ `SinkOutputPool` 绑定并计入 `config_signature`（`gateway_conversions`）→
`GatewayOutput._target_value` 严格类型检查（`type(value) is int and value in (0,1)`）。

### 2.1 转换合同测试（新增/扩展，全绿）

| 输入/场景 | 结果 |
|---|---|
| 整数0→False、1→True | PASS（`type(converted) is bool`） |
| 2、-1、0.0、1.0、True、False、"0"、"1"、None | PASS 拒绝（`ValueError: integer 0 or 1`） |
| 无 conversion 的整数点 | PASS 保持原数值（int passthrough） |
| 不支持的 conversion（bool_to_int01） | PASS 配置拒绝 `unsupported conversion` |
| 批内部分失败（坏点+好点） | PASS：`delivered` 只含好点；坏点入 `permanent`、`error=GATEWAY_CONVERSION_INVALID`；**绝不写 False**；客户端只收到好点变量 |
| 重复目标变量 | PASS 配置拒绝 `duplicate Gateway variable` |
| 签名含转换 | PASS：仅改 conversion 即改变输出池签名；`adapter._conversions` 正确绑定 |
| 配置重新激活 | PASS：签名差异触发适配器重建（既有 config_snapshot 组件测试 169 全绿） |

证据：`static/pytest-specified.txt`、`live/pytest-f1f2f3-live.txt`、`f1/fault-status-e2e.json`。

### 2.2 真实 E2E（PASS）

- 基线：源 `fault_status=0` → 目标 `device_fault_active=False/GOOD/CURRENT` → DCEMS `ems_fault_state=f`。
- 故障刺激（安全、可恢复）：CLI 气象输入 `wind_speed_mean=26 ≥ cut_out_speed_m_s=25` →
  物理机粘性跳闸 → 源 `fault_status=1` → **目标 `True/GOOD/CURRENT`** → DCEMS `ems_fault_state=t`
  （真实 FC02 轮询落库，非直写数据库）。
- 隔离：WTG02-10 `device_fault_active=False` 不受刺激影响。
- 恢复：Generation 重启（跳闸在 Generation 生命周期内粘滞）+ 安全 setpoint/mode 重初始化 →
  目标故障位回 False/CURRENT。
- 未直写 DCEMS 数据库；刺激经完整测量链。

## 3. F2：停止结算与自动恢复 — 单元/组件 PASS；共享拓扑缺陷已最小修复

### 3.1 整改语义验证（PASS）

- `stop()` 对 `settle()` 施加 `io_wait_timeout_ms`（3s）界：超时 → `ConfigurationError
  "settlement timed out"`，`_stopping` 保持 True，**不释放 Sink/配额、不清引擎/写入器**（所有权保留）。
- 超时后连续 stop 继续超时直到原生 I/O 结算；结算后 stop 成功清状态、释放配额（单测
  `test_realtime_delivery_stop_settlement.py` 5 项 + 组件 `test_delivery_runtime.py` 12 项全绿）。
- `start()` 在 `_stopping` 时先重试 `stop()`（清理完成后才继续）；引擎/写入器均空才清过时标记
  （对应整改提交 `865f78a4`）。
- 看门狗恢复链（组件级）：RUNNING+`port.last_error` → stop 超时 → FAILED（保留所有权、不重复
  start）→ I/O 结算 → 下轮经 start 重试清理 → **RUNNING 自动恢复**（无进程重启）。
- 既有生命周期/取消竞争/配额测试全绿（`test_delivery_runtime`、`test_output_pool_cancel_races`、
  `test_output_pool_quota`、system lifecycle 首轮62过）。

### 3.2 复测发现并最小修复的生产缺陷（必要小范围生产修复）

**缺陷**：`SharedSinkOutputPool.release()` 的结算检查遍历**共享** `physical.tasks`——同一物理
目标（T04 十机共用 `127.0.0.1:52075`）下任一 Delivery 停止时，兄弟 Delivery 的在途写几乎
必然存在 → 误判为“未结算 I/O” → 将**共享** `physical.unsafe` 置真 → 全部10条 Delivery
`FAILED: cancelled native Sink I/O requires external fencing`，只能进程重启恢复。

**证据链**：
- 实测复现（修复前）：`sink stop dcems_wtg01` → 30s 内10/10 FAILED（两轮独立复现）；
- 归属：该检查在 T04 基线（`5fdd369a`）已存在（`git show` 对照），非整改引入，但直接阻断
  任务要求的“正常停止/再启动”与“共享 Sink 多 Delivery”验收（整改新增单测未覆盖共享拓扑）；
- **最小修复**（`src/sink_service/application/output_pool.py`）：`_SinkOutput` 增加自有
  `tasks` 集合；写入登记/完成回调双写；`release` 只检查**本 Sink 自有**在途写。
  取消→物理级隔离语义保持不变（`_finish` 仍置 `physical.unsafe`）。
- 回归测试：`test_release_of_one_owner_does_not_fence_sibling_inflight_writes`
  （兄弟在途写期间释放一方：成功、物理目标不污染、兄弟写正常完成、可再次释放）。

修复后实测：`sink stop/start dcems_wtg01` 全链 STOPPED→STARTED→RUNNING，兄弟九机全程
RUNNING；F3/F1 live 2/2 过；15项 live 全量过。

### 3.3 F2 裁决依据

- 正常启动/停止/再启动、settle 界、`_stopping` 语义、配额不提前释放、看门狗自动恢复：
  单元+组件+live 全部通过（含共享拓扑停启）。
- 未完成 I/O 不盲目恢复：取消仍隔离至进程级栅栏（既有设计、任务允许的“继续隔离”）。
- 全程无人工重启才能恢复的永久 FAILED（曾出现的 fencing 级联经上述最小修复消除；
  期间的一次 sink 服务重启为修复前旧代码现场清理，修复后未再需要）。

## 4. F3：Gateway 数据新鲜度 — PASS

整改：`ServerConfig.read_stale_after_ms`（可选，`ge=1` + before 校验拒绝 0/负/布尔/非整数；
None=未配置保持原语义）；`ReadVariables` 以 **Gateway 最后注入时间 `snapshot.updated_at`**
（非 DG 源样本时间）计算：`age ≥ read_stale_after_ms` → `BAD/STALE + STALE_VALUE`，
值仍随响应返回；注入恢复 → `GOOD/CURRENT`；从未注入 → `UNKNOWN/UNINITIALIZED`。

| 场景 | 结果 |
|---|---|
| 从未注入 | PASS（既有质量态测试） |
| 持续更新 | PASS `GOOD/CURRENT` |
| 超期（10s>5s） | PASS `BAD/STALE/STALE_VALUE`，值保留 |
| 恰好到达期限（age=5000ms，`>=` 判定） | PASS `STALE` |
| 恢复注入 | PASS `GOOD/CURRENT`、error_code 清空 |
| 未配置（None） | PASS 保持旧语义（60s 旧样本仍 GOOD/CURRENT） |
| 0/-1/True/False/5000.0/"5000" | PASS 配置拒绝；None 合法 |
| 十机中一台停更 | PASS live：`sink stop wtg01` → 仅 wtg01 `BAD/STALE`，wtg02-10 `GOOD/CURRENT` |
| 恢复注入后 | PASS live：`sink start` → `GOOD/CURRENT`（同时覆盖 F2 手动停启） |

**DCEMS 限制分层记录**（`f3/dcems-reaction-to-frozen-value.json`，裁决 `RISKS_RECORDED`）：
- Gateway gRPC：`BAD/STALE/STALE_VALUE`（可表达）；
- Modbus 对外：仍返回冻结值（协议无质量字段）；
- DCEMS：`comOk` 保持 t、行继续落库但值冻结——**不等于** DCEMS 已识别过期值。

## 5. 静态与模块回归

| 门禁 | 结果 |
|---|---|
| ruff check/format（5 目标目录） | PASS（228 文件） |
| mypy `src` | PASS：483 files, no issues |
| import-linter | PASS：22 kept, 0 broken |
| 任务指定4文件 | **40 passed** |
| sink+gateway 单元 | **596 passed / 1 failed**（唯一失败=出厂 kafka1.1 模板 schema1 与 f5823592 门禁矛盾，先于整改存在，UNRELATED，见 §8） |
| sink+gateway 组件 | **169 passed** |
| sink+gateway 集成 | **55 passed / 10 skipped** |
| gateway 系统 | **22 passed** |
| sink 系统（修复前首轮） | 62 passed / 1 failed（仅 `test_sustained_capacity_600s` UNRELATED） |
| sink 系统（修复后整轮） | 57 passed / 1 failed（capacity 同上）/ 5 errors（Docker 端口释放竞态 `127.0.0.1:48565 already allocated`，环境瞬态，UNRELATED） |
| sink 系统 lifecycle（修复后单跑） | **5 passed**（`static/pytest-sink-system-lifecycle-final.txt`） |
| T03 基线四专项 | 17 passed |
| T04 配置契约 | 7 passed（含 conversion、read_stale_after_ms=5000 断言） |
| MANIFEST / third_party | 133 OK；`git status third_party/` 零改动 |
| bash -n run_t04_unified.sh | PASS |

测试侧修复（详见 §8 失败记录）：stage1 夹具对齐生产门禁（service schema6、kafka 模板 schema2、
dedup_key=batch_id）、两处单测/组件本地 kafka 模板 schema2、N802 noqa、历史证据写入路径重定向。

## 6. T04 真实全链回归 — PASS

**15/15 passed（1m55s）**：`live/pytest-t04-full-15.txt`
= 原13项（十机9点对账、FC03/04/02 抓帧、时间戳链、Gateway↔DCEMS 一致、质量态、非法输入、
断连隔离恢复、D1-D6 控制+物理反馈）+ 新2项（F3 单机停更隔离/恢复、F1 故障0/1全链转换）。

关键证据：
- D1：450kW → FC16 → `CONFIRMED_SUCCESS` → `APPLIED` → 源回读450000W → 物理响应 →
  DCEMS `ems_pow_act` 反馈（见 T04 报告数值，本轮复测同样通过）；
- D2：125kvar → DCEMS 物理无功反馈；
- FC16/FC03/FC04 原始帧持续捕获：探针全量计数由稳定性采样归档（165,672→269,851 单调，`stability/samples.jsonl`），控制期 FC16 增量断言（`stability/injection-events.json` fc16_delta=1）与 live 断言为证；原始帧文件随运行根清理（复现方式：`modbus_request_probe --listen-base 15321 --target-base 15121`）；
- 十机身份不串号（风速常量+物理连续值）。

## 10. 最终裁决

| 验收项 | 裁决 |
|---|---|
| `F1_CONVERSION_CONTRACT` | **PASS** |
| `F1_FAULT_STATUS_E2E` | **PASS** |
| `F2_STOP_SETTLEMENT` | **PASS**（含共享拓扑停启；见 §3.2 最小生产修复） |
| `F2_AUTO_RECOVERY` | **PASS**（看门狗恢复 + gateway 中断自动恢复 + 两轮手动停启；无永久 FAILED 需重启） |
| `F3_FRESHNESS` | **PASS** |
| `F3_DCEMS_LIMITATION` | **RISKS_RECORDED** |
| `SINK_GATEWAY_REGRESSION` | **PASS**（单元596+组件169+集成55+gateway系统22+lifecycle5；2 例 UNRELATED 既有/环境失败已记录） |
| `T04_CONTROL_FEEDBACK` | **PASS**（15/15，含450kW/125kvar 物理反馈与稳定期 FC16 delta=1） |
| `STABILITY_30MIN` | **PASS**（30m32s、60采样、4类注入全部按预期恢复） |
| `STATIC_GATES` | **PASS**（ruff/mypy483/import-linter22/指定40；UNRELATED 失败见 §8） |
| `VENDOR_IMMUTABILITY` | **PASS**（MANIFEST133、third_party 零改动） |
| `T05_ENTRY` | **READY** |


## 11. 遗留问题（交 ChatGPT）

| # | 级别 | 问题 |
|---|---|---|
| 1 | P2（既有矛盾） | 出厂 `configs/templates/sinks/sink_kafka_history_v1.1.0.yaml` `schema_version:1` 与 `f5823592` Kafka-Sink-模板必须 schema2 的门禁互斥 → `test_shipped_kafka_110...` 长期失败（UNRELATED）。需裁决：改出厂文件或门禁对历史版本豁免 |
| 2 | P1（既有 OPEN，非本轮整改引入） | `test_sustained_capacity_600s` 在本机三连败（confirmed≈52–62/120，伴 Kafka GroupCoordinator 错误）；整改零交集。历史 C6 同样失败（97.9–102 批/s，C6-01 P1 OPEN；前轮“C6 曾通过”有误，已更正）。完整分层诊断见 `docs/audit/dcems_t04_followup_retest_20261128.md` §7，交 ChatGPT |
| 3 | P3（测试基建） | sink system Docker 栈存在端口释放竞态（`48565 already allocated` 致 lifecycle 5 errors，单跑即过）；建议 harness 启动前等待端口释放或重试 |
| 4 | P3（观测） | sink 进程 stdout/stderr 落盘为0字节（日志缓冲/未刷新），事故现场无法回放——建议 sink CLI spawn 使用 `-u` 或 logging 直写文件 |

本轮 Claude 侧小范围生产修复：`src/sink_service/application/output_pool.py`（共享 Sink release
误栅栏，diff 见 `environment/claude-fix-output-pool-release.diff`、对照
`environment/fix-before-after.txt`），附回归测试；其余均为测试/夹具修复。

## 12. 最终 Git 状态

| 项目 | 值 |
|---|---|
| 起始 HEAD | `865f78a41eb412210bedce23965c234e5e43c54c`（=远端） |
| 本轮提交 | 见提交记录（报告交付时填写） |
| push | 按约定本机无凭据，交用户手动 push；本地/远端 SHA 差异随交付消息报告 |

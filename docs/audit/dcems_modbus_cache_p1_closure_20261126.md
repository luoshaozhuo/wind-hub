# Modbus 读取计划缓存 P-1 最终关闭复测（2026-11-26）

> 报告模板说明：仓库与文件系统中仍无《报告模板.md》，本报告按任务第 8 节
> 要求组织，不声称符合未读取的模板。

## 1. Git 基线及最终 HEAD

| 项 | 值 |
|---|---|
| Repository | `luoshaozhuo/wind-hub` |
| 分支 | `feat/dcems-gateway-smoke-20261126` |
| 预期 / 远端 / 本地起始 HEAD | `c74db343b1d99f1c04d4fdf969a8f75c19964c38` |
| 修复前 HEAD | `690e1dd1be7af6819aedc8b606fbfa6978822810` |
| 生产修复提交 | `c74db343`（仅 `driver.py`，+5/−2） |
| 本轮交付提交 | 见交付消息（仅验收报告） |
| 工作树 | 起始干净；本轮未建分支、无 PR、未合并 main、未 rebase/reset/force push |

## 2. 修复差异审查

```python
self._read_plan_cache.pop(key, None)
if self._config.read_plan_cache_size > 0:
    contiguous = tuple(... group_consecutive_reads(unique, max_gap=0) ...)
    # Persist the safe plan even when this call already used it.
    self._read_plan_cache[key] = contiguous
    self._read_plan_cache.move_to_end(key)
raise
```

核对结论：

- 每次 `_ModbusIllegalDataAddressError` 都写回连续计划，不再有
  `contiguous != groups` 交替消失条件；
- `read_plan_cache_size == 0` 时不写回（保持无缓存设计）；
- `pop` 后原位写回，键数量不增加；`move_to_end` 保持 LRU 顺序，
  容量上限逻辑不变；
- 仍不返回部分成功样本（异常先于 `return` 抛出）；
- 仅专用 Code 02 异常进入该分支；`ProtocolError`、连接/超时/取消、
  长度与功能码错误不触发；
- 未改动协议公开接口或其他模块生产代码。

## 3. 20 周期真实请求证据

`test_twenty_persistent_rejections_send_merged_request_only_once`（真实线级
严格服务器，地址 6 永久不可读）：

```text
Cycle 1 : FC03 (0,7) → Code 02 → FC03 (0,1) OK → FC03 (6,1) → Code 02
Cycle 2–20:                   FC03 (0,1) OK → FC03 (6,1) → Code 02
```

- `(0,7)` 全程只出现 **1 次**；其余 19 周期不再重复；
- 20 周期全部抛出 `_ModbusIllegalDataAddressError`，无 GOOD 部分样本；
- 该用例本轮为普通测试并通过（未 xfail、未 skip）。

## 4. 缓存命中、驱逐与保留

| 场景 | 结果 |
|---|---|
| 成功回退后缓存连续计划 | PASS（第二次起只发连续请求） |
| 允许跨空洞的服务器 | PASS（单次合并保留，WTG01 全表） |
| 回退仍失败：整批失败且计划被驱逐 | PASS |
| **持续失败 20 周期：安全计划始终保留** | **PASS（合并请求仅 1 次）** |
| 缓存容量 0 | PASS（每次重新规划，无跨调用状态） |
| 缓存淘汰/LRU | PASS（既有 `test_modbus_read_plan_cache_reuse_eviction_and_reload`；新分支 `move_to_end` 保持顺序） |
| 并发两个稀疏键 | PASS（各自独立连续计划） |
| 重复 Point ID `["a","b","a"]` | PASS（顺序与重复值保持） |
| 在线 PointTable 更新成功/失败 | PASS（失败保留旧表，缓存清理受影响键） |
| 非 Code 02 / 断链 / 超时 / 长度 / 功能码 | PASS（不拆分、不重试） |

## 5. Core Modbus 测试统计

```text
pytest -q tests/core/protocol/modbus
62 passed  (62 collected)
pytest -q tests/core/protocol/modbus/test_strict_server_fallback.py
17 passed
pytest -q ... -k twenty
1 passed
```

无 failed、error、xfail、skip（`grep xfail/pytest.mark.skip` 无结果）。

## 6. Ruff、format、mypy

| 检查 | 退出码 | 结果 |
|---|---:|---|
| `ruff check src/core/.../modbus tests/core/protocol/modbus` | 0 | All checks passed |
| `ruff format --check` 同范围 | 0 | 14 files already formatted |
| `mypy src` | 0 | 483 files，0 error |
| `git diff --check` | 0 | 无错误 |
| `pytest --collect-only -q` | 0 | 4288 tests |
| 仓库 `ruff check src tests scripts` | 1 | 5 个既有无关错误（kafka_consumer、tdengine_backend、test_tdengine_wide_probe） |
| 仓库 `ruff format --check src tests scripts` | 1 | 7 个既有文件，均不在本轮范围 |

未引入新的门禁错误，未扩大修复范围。

## 7. 一轮 gap=8 十机完整链路回归

环境：隔离 NATS `127.0.0.1:14222`；五业务服务由正式 CLI 启动；
Collector 源端口经透明探针 15231–15240 转发到 Gateway2 的 15221–15230。

| 检查 | 实测 |
|---|---|
| 五服务启动 | 全部 SUCCESS / RUNNING（瞬时 DEGRADED 保留） |
| Collector Task | RUNNING，last_error 空 |
| 10 台设备 | 全部 RUNNING，0 PARTIAL / 0 FAULTED |
| NATS 发布 | realtime，`nats_connected=true`，published 420/0，failed=0 |
| Sink Delivery | 10/10 RUNNING，无 last_error |
| 十机对账 | `source-target-reconciliation.json` failures=[] |
| 共享 sample ID | `diagnose pipeline` 10/10 PASS（随机场景 7 项测试含此断言） |
| WTG01 站控 P/Q、PFR false | PASS（随机与身份场景均断言） |
| 身份隔离 | 11 passed（10 台共享 sample + 常量隔离，含站控/PFR 泄漏检查） |
| 真实请求 | WTG01 合并 `(4,0,20)` 188 次全部成功；WTG02–10 各 **1 次**，后续稳态为 `(0,2)`+`(6,14)`+discrete |
| 结束清理 | 进程 0、监听端口 0、NATS 容器已删除 |

探针总记录 6176 条；稀疏设备重复合并请求 0 次。

## 8. 失败、跳过与环境阻断

- 本轮无失败、无跳过、无环境阻断；
- 唯一既有无关失败是仓库门禁中的 5 个 ruff / 7 个 format（未触碰文件），
  与 P-1 无关；
- 未重复三轮容量或稳定性测试（本轮无回归信号，按任务要求只做一轮）。

## 9. 最终裁决

| 项目 | 裁决 |
|---|---|
| **P-1** | **P1_CLOSED** |
| 下一阶段 | **READY_FOR_COMMANDER_INTEGRATION_REVIEW** |

依据：20 周期真实严格服务器用例转为 PASS 且非法合并请求恰好 1 次；
Core Modbus 62 项全部通过且无 xfail/skip；Ruff/format/mypy 通过；
默认 gap=8 一轮十机真实链路（Task、设备、NATS、Delivery、对账、因果链、
身份隔离）全部通过；结束无残留资源。

该裁决仅表示允许审查和测试既有 Commander 控制链路，不表示必须修改或
重新开发 Commander 生产代码；也不代表整个 DCEMS 跨系统生产 READY。
本轮不合并 main。

## 10. 证据路径

`run/dcems-modbus-cli-p1-closure/evidence/`（Git 忽略、无令牌）：

```text
core-and-gates.log        Core 62 passed、20 周期、mypy/ruff
repo-gates.log            仓库既有无关门禁
check.log / up.log        配置检查与启动
task.json / devices.json / publishing.json / deliveries.json
live.log / isolation.log  随机 7 passed / 身份 11 passed
probe.jsonl / probe-analysis.json  真实 Modbus 请求与合并计数
down.log / cleanup.txt    停机与资源清理
```

最终提交 SHA、本地与远端一致性见交付消息。

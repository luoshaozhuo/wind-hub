# Modbus 读取计划缓存 P-1 最终独立复测（2026-11-26）

> 报告模板说明：仓库内仍无《报告模板.md》，本报告按交接任务第 9 节要求组织。

## 1. Git 基线、最终 HEAD

| 项 | 值 |
|---|---|
| Repository | `luoshaozhuo/wind-hub` |
| 分支 | `feat/dcems-gateway-smoke-20261126` |
| 预期 / 实测起始 HEAD | `dfe92e4f8301b64ff65a60d19860b379d8445afb` |
| fetch 后远端 HEAD | 与起始 HEAD 一致 |
| 修复前 HEAD | `d85be7be8ee9fbb317826ddb115126d9ac8565d6` |
| 被审查的生产提交 | `dfe92e4f`（`driver.py` + 测试断言调整） |
| 本轮整改提交 | 见交付消息（测试、探针与报告） |
| 工作树/分支 | 未建分支、无 PR、未合并 main、未 rebase/reset/force push |

## 2. P-1 修改审查

`src/core/infrastructure/protocol/modbus/driver.py` 的 `read_many()`：

```python
except _ModbusIllegalDataAddressError:
    self._read_plan_cache.pop(key, None)
    if self._config.read_plan_cache_size > 0:
        contiguous = tuple(... group_consecutive_reads(unique, max_gap=0) ...)
        if contiguous != groups:
            self._read_plan_cache[key] = contiguous
    raise
```

- 失败时驱逐合并计划，且不返回已读到的部分样本，符合“整批失败”；
- 成功回退路径仍以 `len(safe_groups) > len(groups)` 替换缓存；
- 原 `xfail(strict=True)` 标记被删除，改为 2 个周期的断言。

**发现缺陷**：驱逐后再写入连续计划的判断使用 `contiguous != groups`。
当上一周期已经把连续计划写入缓存时，下一周期 `groups == contiguous`，
异常路径只执行 `pop` 而不重新写入；再下一周期缓存缺失，重新按
`read_merge_max_gap=8` 生成合法/非法合并计划，非法合并请求再次发出。
实际表现为“每两个周期发送一次非法合并请求”，未满足“只在首次出现一次”。

## 3. 静态检查

| 检查 | 退出码 | 结果 |
|---|---:|---|
| `ruff check src/core/.../modbus tests/core/protocol/modbus` | 0 | All checks passed |
| `ruff format --check` 同范围 | 0 | 14 files already formatted |
| `mypy src` | 0 | 483 files，0 error |
| `pytest --collect-only -q tests/core/protocol/modbus` | 0 | 62 tests |
| `pytest --collect-only -q`（全仓库） | 0 | 4287 tests |
| `git diff --check` | 0 | 无错误 |
| 仓库 `ruff check src tests scripts` | 1 | 5 个既有无关错误（kafka_consumer、tdengine_backend、test_tdengine_wide_probe） |
| 仓库 `ruff format --check src tests scripts` | 1 | 7 个既有文件，均不在本轮范围 |
| 新增 xfail / skip | — | 无（原 xfail 已删除，未新增） |

## 4. 原失败用例结果

- `test_persistent_address_rejection_evicts_invalid_cached_plan`（2 周期）
  **已转 PASS**：第二次读取只发送 `(0,1)`、`(6,1)`，`(0,7)` 仅出现一次。
- 该用例不再带 xfail。

## 5. 20 次持续失败请求序列

新增 `test_twenty_persistent_rejections_send_merged_request_only_once`：
严格线级服务器只定义地址 0，地址 6 永久不可读。

实际请求序列（每周期两个请求）：

```text
cycle  1: FC03 (0,7) ERROR   FC03 (0,1) OK  FC03 (6,1) ERROR
cycle  2:                    FC03 (0,1) OK  FC03 (6,1) ERROR
cycle  3: FC03 (0,7) ERROR   FC03 (0,1) OK  FC03 (6,1) ERROR
cycle  4:                    FC03 (0,1) OK  FC03 (6,1) ERROR
...
cycle 19: FC03 (0,7) ERROR   FC03 (0,1) OK  FC03 (6,1) ERROR
cycle 20:                    FC03 (0,1) OK  FC03 (6,1) ERROR
```

- `FC03 (0,7)` 共 **10 次**（周期 1、3、5…19），不是 1 次；
- 每次失败后整批抛出 `_ModbusIllegalDataAddressError`，无部分成功样本；
- 测试以普通失败用例保留，未删除、未降级为 xfail。

结论：**持续失败场景仍会重复发送已被拒绝的合并请求，P-1 未关闭。**

## 6. 缓存成功、失败及淘汰测试

| 场景 | 结果 |
|---|---|
| 成功回退后缓存连续计划，第二次不再合并 | PASS |
| 允许空洞时保留单次合并（WTG01 全表） | PASS |
| 回退仍失败：整批失败、计划被驱逐 | PASS（本周期） |
| 持续失败 20 周期：合并请求只出现一次 | **FAIL（10 次）** |
| 缓存容量 0（`read_plan_cache_size=0`） | PASS：每次调用重新规划（2 次调用各发合并+回退），无跨调用状态 |
| 缓存淘汰 | PASS（既有 `test_modbus_read_plan_cache_reuse_eviction_and_reload`） |
| 并发两个稀疏键 | 各得独立连续计划，值/顺序正确 |
| 重复 Point ID `["a","b","a"]` | 顺序 `["a","b","a"]`、值 `[100,106,100]`，请求序列正确 |

## 7. Modbus 全量回归

`pytest -q tests/core/protocol/modbus`：**61 passed, 1 failed**（62 collected）。
除下节缺陷外，异常分流矩阵、在线 PointTable 更新、真实 TCP 严格服务器、
响应长度/功能码错误、断链/超时/取消、Unit ID 与功能码独立性均保持通过。
`test_functional.py` 26 passed；`test_strict_server_fallback.py` 16 passed + 1 failed。

## 8. gap=0 与 gap=8 十机对账

### gap=0 基线

- 五服务 `SUCCESS/RUNNING`，Task RUNNING，10 Delivery RUNNING；
- 随机场景 7 passed，身份隔离 11 passed；
- 十机逐台对账与共享 sample ID 全部通过。

### gap=8（三轮，经透明请求探针 15231–15240）

每轮：Task RUNNING、10 台设备 RUNNING（0 PARTIAL/0 FAULTED）、
10 Delivery RUNNING、随机 7 passed、身份 11 passed、WTG01 站控 P/Q
与 PFR false 通过、共享 sample ID 10/10 PASS。

探针实测 Modbus 请求（FC04）：

| 轮次 | 记录数 | WTG01（12 点）合并 (0,20) | WTG02–10 合并 (0,20) 每台 | 稀疏端口重复 |
|---|---:|---:|---:|---:|
| R-A | 7921 | 267（全部成功，优化保留） | 1 | 0 |
| R-B | 6359 | 214 | 1 | 0 |
| R-C | 6257 | 211 | 1 | 0 |

稀疏设备首次合并被 Code 02 拒绝后即缓存连续计划
`(0,2)` + `(6,14)` + discrete，后续周期不再发送 `(0,20)`；
稳态请求数与 gap=0 相同（每台 3 个请求），无性能退化。

## 9. 故障恢复

| 注入 | 结果 |
|---|---|
| Gateway2 停止 | Collector Task 转入 CONNECTING/FAULTED，目标值冻结；恢复 Gateway2 后 Task RUNNING |
| DG 租约丢失 | FAILED 并保留所有权；`data-generator stop <旧 generation>` 后重新 start 恢复动态数据 |
| Gateway1 停止 | 10 Delivery FAILED + 真实 RPC 错误（无假成功）；重启后自动恢复 RUNNING |
| NATS 停止 | connected=false、`nats: no servers available`；Delivery DEGRADED/FAILED；重启后发布与交付恢复 |
| 过期数据 | 源端中断后 Collector 标记 stale 并停止发布，不冒充新数据 |

三轮均包含“源端停止 → 重启 → 停止失败 generation → 重新 start”的完整恢复，
恢复后 Gateway1 十机数据重新动态。

## 10. 三轮稳定性

| 轮次 | 启动 UTC | 随机 | 身份 | 故障恢复 | 停机后 |
|---|---|---|---|---|---|
| A | 10:07:46 | 7 passed | 11 passed | 源端停止/恢复 PASS | 进程 0 / 端口 0 |
| B | 10:10:40 | 7 passed | 11 passed | 源端停止/恢复 PASS | 进程 0 / 端口 0 |
| C | 10:13:08 | 7 passed | 11 passed | 源端停止/恢复 PASS | 进程 0 / 端口 0 |

每轮独立判定，无残留进程、监听端口或临时 NATS 资源（NATS 容器在收尾删除）。

## 11. 新增及既有缺陷

### P-1（未关闭，生产 `driver.py` `read_many` 异常分支）

- 根因：驱逐后仅在 `contiguous != groups` 时写回连续计划；当当前计划
  已是连续分组时只 pop 不回写，下一周期缓存缺失重新生成合并计划；
- 影响：对持续不可读的点，每两个采样周期重复一次被拒绝的合并请求；
  数据仍然整批失败，无误数据；
- 最小整改：

```python
except _ModbusIllegalDataAddressError:
    if self._config.read_plan_cache_size > 0:
        self._read_plan_cache[key] = contiguous
        self._read_plan_cache.move_to_end(key)
    else:
        self._read_plan_cache.pop(key, None)
    raise
```

即无条件保留连续计划（容量不变），并把现有 20 周期用例转为 PASS。

### 既有无关

- 仓库 ruff/format 既有错误与本轮无关，未扩大范围；
- 本轮未发现传输/超时/取消被误拆分的新缺陷。

## 12. 运行证据路径

`run/dcems-modbus-cli-p1/evidence/`（Git 忽略、无令牌）：

```text
final-gates.log / repo-gates.log       静态与仓库门禁
core-static-and-tests.log / core-tests-final.log   Core 61 收集、60+1 失败
gap0-*.log/json                         gap=0 基线
gap8rA/B/C-*.log/json                   三轮 gap=8（任务/设备/交付/测试）
gap8rA/B/C-probe.jsonl / -analysis.json 真实 Modbus 请求与合并计数
gap8f1/f2/f3-*.json                     故障恢复
```

## 13. 最终裁决

| 项目 | 裁决 |
|---|---|
| **P-1 专项** | **P1_BLOCKED** |
| 整体下一阶段 | **BLOCKED** |

原 2 周期失败用例已转 PASS，但 20 次持续失败仍产生 10 次非法合并请求，
不满足“最多首次一次”。Core 回归与真实十机链路（gap=0 基线、gap=8 三轮、
身份隔离、故障恢复）均通过，未发现其他协议缺陷。P-1 修复后应仅重跑
`tests/core/protocol/modbus` 与一轮 gap=8 链路即可复核。
本轮不宣称生产 READY，不合并 main。

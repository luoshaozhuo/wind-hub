# Modbus 稀疏点表与 CLI 启动状态生产整改独立复测（2026-11-26）

> 报告模板说明：仓库与文件系统中仍未找到《报告模板.md》，本报告按交接任务
> 第 11 节要求的 18 项内容组织，不声称符合未读取的模板。

## 1. Git 基线、最终 HEAD 与修改范围

| 项 | 值 |
|---|---|
| Repository | `luoshaozhuo/wind-hub` |
| 分支 | `feat/dcems-gateway-smoke-20261126` |
| 预期 / 实测起始 HEAD | `a0e73b36d7453098f3625446c3dad44184f9481f` |
| fetch 后远端 HEAD | 与起始 HEAD 一致 |
| 上一轮起点 | `69889aba85e97547919e92266dee716ab4b6c76c` |
| 被审查的生产提交 | `1fe5f296`、`7533ca03`、`8923c016`、`036ee6b2`、`32d3c8fa`、`8c4096b9`、`a0e73b36` |
| 本轮整改提交 | 见交付消息（测试/配置/报告与命名修正） |
| 工作树 / 分支 | 未建新分支、无 PR、未合并 main、未 rebase/reset/force push |

## 2. 测试环境

| 组件 | 实测 |
|---|---|
| OS | WSL2，`6.18.33.2-microsoft-standard-WSL2` |
| Python | 3.14.7（wind-hub Conda），`scripts/dev.py env` = PASS |
| pytest / ruff / mypy | 8.4.2 / 0.8.6 / 1.20.2 |
| pymodbus / nats-py / grpc | 3.15.0 / 2.16.0 / 1.84.0 |
| NATS | `nats:2.10-alpine`，nats-server v2.10.29，隔离 `127.0.0.1:14222` |
| 运行根 | `run/dcems-measurement`（gap=0）、`run/gap8/dcems-measurement`（gap=8） |

## 3. 生产代码差异审查

### 3.1 Modbus 会话与异常分流（`session.py`）

- `response.isError()` 且 `exception_code == 2` 时抛专用
  `_ModbusIllegalDataAddressError`（明确按数值代码，不看异常消息字符串）；
  其他 Exception Code 仍抛通用 `ProtocolError`；
  功能码响应不匹配、长度不符、连接/超时错误均走原 `invalidate()` +
  `ProtocolConnectionError` 路径。
- 本轮仅做一处命名修改：`_ModbusIllegalDataAddress` →
  `_ModbusIllegalDataAddressError`（原命名触发 ruff N818，会成为本轮新增门禁错误）。

### 3.2 稀疏回退与读取计划（`driver.py`）

- `_read_group()`：单次合并读取；捕获专用异常后按 `max_gap=0`
  生成连续分组，逐组读取；分组数为 1 时原样抛错（无法拆分）。
- `read_many()`：成功回退后以 `len(safe_groups) > len(groups)` 为条件
  用连续分组替换读取计划缓存；后续采样不再发送合并请求。
- `update_point_table()` 的在线校验改用同一 `_read_group()`；
  校验失败返回 `VERIFICATION_FAILED`，旧表继续生效。
- 未发现跨 Unit ID / 功能码合并（分组键包含 slave 与 register type）；
  长度上限仍由 `group_consecutive_reads` 的 125 寄存器 / 2000 位限制约束。

### 3.3 CLI 状态语义（`runtime.py`）

- `service start/restart`：确认阶段与 `returncode`/`confirmed` 之后，
  只要进程 `running: true` 或 `state: RUNNING` 即返回 SUCCESS；
  业务健康仍原样保留在 `business` 字段。
- `service stop`：继续与业务健康解耦。
- `service status` 列表：进程未运行或业务 UNKNOWN/UNAVAILABLE/FAILED
  仍降级为 UNKNOWN（退出码 3）。
- 未发现返回码常量与既有约定冲突；成功仍为 0，拒绝/冲突为 1，未知为 3。

## 4. Modbus 异常分流测试矩阵

| 场景 | 服务端实现 | 预期 | 结果 |
|---|---|---|---|
| 正常连续读取 | 真实 PyModbus/线级服务器 | 单请求，不拆分 | PASS |
| 合法跨空洞读取 | 线级服务器允许任意跨度 | 单请求，不拆分 | PASS |
| Exception Code 02 | **真实 PyModbus 稀疏从站** + 线级严格服务器 | 拆分连续地址 | PASS |
| Exception Code 01 | 线级服务器返回 FC|0x80,01 | 不拆分，ProtocolError | PASS |
| Exception Code 03 | 线级服务器返回 FC|0x80,03 | 不拆分，ProtocolError | PASS |
| 其他异常 | 线级服务器（无数据响应）经会话归类 | 不拆分 | PASS |
| TCP 中断 | 读取请求后立即断链 | 1 次请求后 ProtocolConnectionError | PASS |
| 超时 | 服务器不响应（客户端 0.5s） | 1 次请求后 ProtocolConnectionError | PASS |
| 请求取消 | 既有并发用例（原生事务不孤立） | 不自动重发 | PASS |
| 响应长度错误 | 服务器多返一个寄存器 | 会话失效，1 次请求 | PASS |
| 功能码响应不匹配 | FC04 请求返回 FC03 | 会话失效，1 次请求 | PASS |

全部用例使用真实 TCP 服务端（PyModbus 稀疏数据区或线级 Modbus 帧），
未使用 Mock 成功分支，也未用驱动内部对象替换会话。

## 5. 稀疏点表回退及缓存证据

| 证据 | 结果 |
|---|---|
| 严格服务器合并读（0,7）→ Code 02 | 回退为 (0,1) 与 (6,1)，值 100/106，质量 GOOD、时间戳带时区 |
| 二次采样 | 仅 (0,1)、(6,1)；合并请求全程只出现 1 次 |
| 允许空洞的服务器 | 仅 1 次合并请求 (0,7) |
| 回退后仍 Code 02 | 整次读取抛专用异常，无部分 GOOD |
| 并发两个稀疏键 | 各自 2 值正确，缓存两条独立连续计划 |
| 在线更新成功 | 校验请求 (3,7) → (3,1)、(9,1)，旧值不变，应用成功 |
| 在线更新失败 | `VERIFICATION_FAILED`、`applied=false`，旧表仍可读出 [100,101] |

## 6. 在线 PointTable 更新结果

- 校验阶段使用相同回退策略（成功/失败用例见 §5）。
- 失败时 `_point_table` 未被替换，`point_table_id` 仍为旧值；
  计划缓存不含未验证分组。
- 成功时 `_active_version` 递增并清理受影响缓存键。

## 7. Core 静态检查及测试

| 检查 | 结果 |
|---|---|
| `ruff check src/core/.../modbus tests/core/protocol/modbus` | 0 |
| `ruff format --check ...` | 0 |
| `mypy src` | 483 files，0 error |
| `pytest -q tests/core/protocol/modbus` | **58 passed, 1 xfailed** |
| 新增测试进入 collection | 是（`test_strict_server_fallback.py` 11 项） |

## 8. CLI 生命周期及退出码结果

| 情形 | 实测 | 结果 |
|---|---|---|
| 进程 RUNNING + 业务 DEGRADED | `service start` SUCCESS(0)，payload 保留 DEGRADED | PASS |
| 进程 RUNNING + 业务 HEALTHY | SUCCESS(0) | PASS |
| 进程 RUNNING + 业务 UNKNOWN/UNAVAILABLE/FAILED | SUCCESS(0)，payload 原样 | PASS |
| 进程未运行 | UNKNOWN(3) | PASS |
| 管理器未确认 / 阶段未确认 | UNKNOWN(3) | PASS |
| gRPC 端口被外部进程占用 | `FAILURE` 退出 1，"configured local service endpoint is already occupied" | PASS |
| 启动阶段失败（阶段 confirmed=false） | UNKNOWN(3) | PASS |
| 业务健康持续失败 | `service status` 返回 UNKNOWN(3)，未掩盖 | PASS |
| 服务停止确认 | SUCCESS(0)，与业务健康无关 | PASS |

五服务完整启动（gap=0 与 gap=8 两个根）均为 5 × `SUCCESS / RUNNING`，
其中 DEGRADED 为启动瞬时状态且保留在响应中。

补充观察：占用 **Modbus** 15221（非 gRPC）时 `service start gateway`
返回 SUCCESS、进程 RUNNING、`business=DEGRADED`（`dcems-gw-01` 未监听），
网关随后自动重试并在占用释放后恢复 HEALTHY。新契约把进程生命周期与
业务健康分开报告，该场景属业务降级而非进程启动失败；调用方必须以
业务健康/协议回读判定可用性，不能只看退出码 0。

## 9. CLI 进程身份和权限安全测试

`tests/cli/test_native_process.py`（24 项含 service 状态用例）全部通过，
覆盖：外部端点被占用不得确认、健康端点不属于受管进程、监听 inode 变化、
早退子进程不得确认、陈旧 PID 不得发信号、并发启动只创建一个进程、
状态保存失败回收进程组。错误 Bearer 与无权限注入在本轮完整链路中
继续返回 UNAUTHENTICATED/PERMISSION_DENIED（见 §13）。

## 10. gap=0 完整测量链路回归

- 五服务 CLI 启动、Task RUNNING、10 Delivery RUNNING、NATS published 420/0；
- 随机场景 7 passed，身份隔离 11 passed；
- 逐台对账与共享 sample ID 证据全部通过。

## 11. gap=8 完整测量链路验证（本轮关键准入）

配置：设备实例 `read_merge_max_gap=8`（WTG01 12 点、WTG02–10 稀疏 9 点）。

| 检查 | 结果 |
|---|---|
| Collector Task | RUNNING，last_error 空 |
| 10 台设备 | 全部 RUNNING（0 PARTIAL / 0 FAULTED） |
| WTG02 diagnose | NETWORK/PROTOCOL/ACQUISITION/NATS/BOUNDARY 全 PASS |
| Collector 发布 | realtime，`nats_connected=true`，failed=0 |
| Sink 10 Delivery | 10/10 RUNNING |
| 十机对账 | failures=[]；WTG01 风速 5.4–17.2、有功 221225–987015；WTG02/10 同区间对应 |
| WTG01 站控 P | 439929–1147730，两端一致 |
| PFR FC02 | 源/目标 45/45 false |
| 身份隔离 | 三轮 11 passed |

## 12. 十机数据对账与身份隔离

- 随机场景 45 轮源/目标采样，1 位小数集合存在真实对应；
  共享 sample ID 的 `diagnose pipeline` 10/10 PASS 提供因果证据。
- 身份场景常量（7.0…16.0 m/s、350000…800000 W）逐台精确匹配，
  WTG02–10 的站控/PFR 地址不可读，无泄漏。
- 三轮 gap=8 均通过，无跨设备串写。

## 13. 故障恢复结果（gap=8）

| 注入 | 观测 | 恢复 |
|---|---|---|
| 停止 Gateway2 | Task FAULTED；发布 2340→2350 后停止增长；目标 8.74 冻结 | 重启 Gateway2 → Task RUNNING；DG 租约丢失处于 FAILED 且持有所有权（RESOURCE_BUSY），需 CLI **停止失败 generation** 后重新 start；恢复后源/目标恢复动态、发布 3450 |
| 停止 Gateway1 | 10 Delivery FAILED + 真实 RPC 错误，无假成功 | 重启后 10/10 自动 RUNNING，目标恢复动态 |
| 停止 NATS | connected=false、`nats: no servers available`；Delivery DEGRADED；目标 11.52 冻结 | 重启后 connected=true、发布 4861、Delivery RUNNING、目标恢复 |
| 错误 Bearer | 两轮均被拒绝（随实时套件 7 passed） | — |
| 过期数据 | 源端停止后 Collector 标记 stale 并停止发布 | — |

DG 仍保持 fail-closed，未改为静默自动重试。

## 14. 三轮稳定性

| 轮次 | 配置 | 启动 UTC | 随机场景 | 身份场景 | 停机后 |
|---|---|---|---|---|---|
| 基线 | gap=0 | 09:04:26 | 7 passed（09:04:58–09:05:16） | 11 passed（09:05:33–09:06:09） | 进程 0 / 端口 0 |
| R1 | gap=8 | 09:06:42 | 7 passed（09:07:21–09:07:39） | 11 passed（09:07:57–09:08:33） | 进程 0 / 端口 0 |
| R2 | gap=8 | 09:13:14 | 7 passed（09:14:13–09:14:32） | 11 passed（09:14:41–09:15:18） | 进程 0 / 端口 0 |
| R3 | gap=8 | 09:15:32 | 7 passed（09:15:53–09:16:11） | 11 passed（09:16:29–09:17:05） | 进程 0 / 端口 0 |

R1 另含第 13 节故障注入。每轮 20 个 Modbus listener 在 10 秒内就绪。

## 15. 性能比较

读取计划请求数（真实点表静态计算，`evidence/read-plan-counts.txt`）：

| 点表 | gap=0 | gap=8 首轮 | gap=8 缓存后 |
|---|---:|---:|---:|
| WTG01（12 点） | 2 | 2（0–19 连续） | 2 |
| WTG02–10（9 点） | 3 | 4（1 次合并被拒 + 连续 2 + discrete 1） | 3 |

- 回退只发生一次，随后缓存连续计划，稳态请求数与 gap=0 相同；
- 链路观测（R2，gap=8）：20 秒发布 430 批（21.5 批/s），failed=0；
- Collector `latest WTG02` 9 个样本 quality=good、freshness=LIVE，
  sample_timestamp 与 queried_at 差 < 2 ms；
- 采集延迟、Sink 交付状态与 NATS 错误在三轮均无异常；0 PARTIAL/FAULTED；
- 未发现明显性能回归。

## 16. 已修复问题及回归证据

| 问题 | 修复 | 回归 |
|---|---|---|
| 本轮生产提交触发 ruff N818 | 异常类改名 `_ModbusIllegalDataAddressError`（2 个生产文件、1 个测试） | Core/仓库 ruff 0 新增错误 |
| 新提交文件格式不合规 | `ruff format`（driver.py、test_functional.py） | format 通过 |
| 既有测试注入假会话类型不匹配 | 5 处 `# type: ignore[assignment]` | mypy 专项 0 |
| 缺少真实严格服务器覆盖 | 新增 11 项线级/PyModbus 严格服务器测试 | 58 passed + 1 xfailed |
| CLI 缺“进程未运行”用例 | 补 running=False 断言 | 8 passed |
| 测试配置固定 gap=0 | `prepare --read-merge-max-gap` + `DCEMS_MEASUREMENT_ROOT/READ_GAP` | 静态 5 passed；gap=8 链路通过 |
| README 未说明失败生成需先停止 | 补充 RESOURCE_BUSY 停止步骤 | 故障恢复按文档执行成功 |

## 17. 未关闭问题及生产代码根因

### P-1（P2，生产）：持续 Address 拒绝时无效合并计划保留在缓存

- 复现：严格服务器对 (0,7) 返回 Code 02，且地址 6 本身不可用；
- 现象：首次失败后 `_read_plan_cache` 仍保留合并分组，下一次采样会再次
  发送已被证明不可用的合并请求（每周期 1 次额外拒绝请求）；
  整次读取仍失败，不会返回部分成功。
- 根因：`read_many()` 在读取前写入合并计划（`driver.py` 计划缓存分支），
  仅在回退成功时替换；异常路径没有驱逐键。
- 最小整改建议：在 `_ModbusIllegalDataAddressError` 向上抛出时
  `self._read_plan_cache.pop(key, None)`（或回退失败分支驱逐），并保留
  成功回退的缓存替换逻辑；补“持续拒绝不重复合并读”的回归用例
  （当前以 `xfail(strict=True)` 记录）。
- 影响：仅错误配置/不可读点场景，多一次请求，不产生错误数据。

### P-2（P3，运维）：DG 失败生成需显式停止后重启

- Gateway 重启后 DG 租约丢失，生成 FAILED 但保留目标所有权；
  不停止旧 generation 直接 start 会 RESOURCE_BUSY。
- 已在 README 记录为安全恢复步骤；不建议改为自动重试。

### P-3（P3，观测）：进程级启动成功不等于每个 Modbus listener 已绑定

- 占用 Modbus 端口时 `service start` 仍 SUCCESS，业务 DEGRADED；
  网关自动重试并在端口释放后恢复。调用方须以业务健康与协议回读判定。
  与任务要求的 gRPC 端口冲突拒绝无关（后者已正确 FAILURE）。

### 既有无关失败

- `tests/cli/test_r1f3_strict_cli_e2e.py::test_strict_cli_five_service_data_command_restart_e2e`
  在 `69889aba` 基线 worktree 中同样失败（collector 启动未确认监听归属），
  与本轮改动无关，归类 UNRELATED。
- 仓库门禁 ruff/format 的 5 + 7 个既有错误未扩大修改范围。

## 18. 最终裁决

| 验收项 | 裁决 |
|---|---|
| `MODBUS_EXCEPTION_CLASSIFICATION` | **PASS** |
| `MODBUS_SPARSE_FALLBACK` | **PASS** |
| `MODBUS_READ_PLAN_CACHE` | **FAIL**（成功回退缓存 PASS；持续拒绝未驱逐无效计划，见 P-1） |
| `MODBUS_POINT_TABLE_UPDATE` | **PASS** |
| `CLI_PROCESS_LIFECYCLE` | **PASS** |
| `CLI_BUSINESS_HEALTH_SEPARATION` | **PASS** |
| `TEN_TURBINE_GAP_ZERO` | **PASS** |
| `TEN_TURBINE_GAP_EIGHT` | **PASS** |
| `CROSS_DEVICE_ISOLATION` | **PASS** |
| `FAULT_RECOVERY` | **PASS**（DG 需停止失败 generation 后重启） |
| `THREE_ROUND_STABILITY` | **PASS** |
| `PERFORMANCE_REGRESSION` | **PASS** |
| 总体 | **READY_FOR_NEXT_DEVELOPMENT_WITH_LIMITATIONS** |

默认 `gap=8` 的十机真实链路已通过，CLI 生命周期未产生新的安全或退出码契约
问题；唯一未关闭的验收项是 P-1 缓存驱逐缺陷。该缺陷不影响本次整改主路径，
建议在进入 Commander 控制闭环前由 ChatGPT 修复并复跑本报告的 Core 用例。
本轮不宣称 DCEMS 跨系统生产 READY，不合并 main。

## 19. 证据路径

`run/dcems-modbus-cli/evidence/`（Git 忽略，无令牌）：

```text
baseline.txt                       Git/环境基线
final-gates.log  mypy-src.log      仓库门禁与 mypy
pytest-collect.log                 4282 tests collected
final-test-logs.log                Core 58+1xfail / CLI 24 / e2e 既有失败 / dc_ems 34
read-plan-counts.txt               gap 0/8 请求数
gap0-*.log/json                    gap=0 基线
gap8r1/2/3-*.log/json              gap=8 三轮（任务、设备、发布、交付、对账）
gap8f1/f2/f3-*.json                故障注入与恢复
gap8r1-*-5 个服务日志              进程日志归档
cli-grpc-conflict.json             gRPC 端口占用拒绝
cli-port-conflict.json             Modbus 端口占用观察
```

本地与远端 SHA 一致性、最终提交与 push 结果见交付消息。

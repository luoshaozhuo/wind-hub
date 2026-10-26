# IEC 60870-5-104 适配器清洁重写 —— Round 1 独立验收报告（Claude Code）

- 验收分支：`verify/iec104-round1`
- 开发分支：`refactor/iec104-adapter-clean-rewrite`
- 基线：已先 `git fetch`；远程 HEAD = 交接 commit `3696d263`（已核对一致，未以旧交接点之外的状态为准）
- 验收提交：`d75a2411`（测试/fixture/格式；`src/` 仅行为中性的 ruff 修复）
- 代码基线声明：`refactor/ads-driver-clean-rewrite`
- 结论：**FAIL**（3 项 P1 生产缺陷，含真实链路必现；5 项 RELATED mypy 严格门禁错误）

---

## 1. 验收范围与基线方法

- 范围：IEC104 新驱动（driver/session/codec/mapping/config/events 端口）的单测、组件、契约、真实链路集成、可靠性、E2E；并对 ADS/Modbus/Collector/Commander/Watchdog/ProtocolRegistry/Assembly/config 做全量回归。
- 基线对比使用三个 worktree：
  - `/tmp/iec-dev-base`：开发分支交接点 `3696d263`（RELATED 判定基线）；
  - `/tmp/iec-main-base`：`origin/main` `5e0d5c45`（UNRELATED 双重取证）。
- 失败分类原则：交接点即失败、且与本次重写无关的项标 UNRELATED；其余在开发分支上新引入或暴露的生产问题标 RELATED。
- 全程未修改任何 `src/` 生产逻辑；未使用 xfail/skip/ignore 掩盖缺陷；14 个失败全部是保留原断言的**生产缺陷最小复现**。

## 2. 环境

- `python3 scripts/dev.py env`：PASS（conda `wind-hub`，Python 3.11.16）。
- `hat.drivers.iec104` 与 `c104`（lib60870）均可导入；真实链路测试在本机 TCP loopback 上实跑，无 skip/xfail。
- 探针脚本留存于仓库外 `/tmp/iec104_probe/`（read_probe.py、connect_refused_probe.py、sbo_probe.py、sbo_ok_probe.py）。

## 3. 静态检查

| 门禁 | 结果 | 说明 |
|---|---|---|
| ruff check（src 改动范围 + 全部改动测试） | **PASS** | 交接点有 37 个 RELATED lint，已全部修复（import 排序、`isinstance(x, (a,b))`→`a \| b`、contextlib.suppress 等，均行为中性） |
| ruff format | **PASS（本次改动范围）** | 38 个改动文件全部达标；其中 7 个文件在交接点即整体 format 脏，已格式化（与基线逐 hunk 核对为行为中性重排） |
| import-linter（11 契约） | **KEPT 11/11** | |
| mypy（`src/core/infrastructure/protocol` + `src/collector`） | **FAIL：5 个 RELATED 错误** | origin/main 同范围 0 错误，属本开发分支引入，交 ChatGPT 修复 |

mypy 错误明细（全部 RELATED）：

1. `src/core/infrastructure/protocol/iec104/driver.py:123`：`asyncio.current_task()` 在同一条件表达式中调用两次，mypy 不跨调用窄化，第二次判为 `Task | None` → `[union-attr]`。修法：先取局部变量 `task = asyncio.current_task()` 再判空。
2. `src/core/infrastructure/protocol/ads/driver.py:180`、`:318`：`result = await self._connect_impl()/_close_impl()`，impl 标注返回 `None`，末尾 `return result` → `[func-returns-value]`。
3. `src/core/infrastructure/protocol/modbus/driver.py:158`、`:229`：同上。

pyright：环境未安装（门禁配置中亦未启用），未运行。

## 4. 测试资产改造（删除/重写/新增）

旧测试在 src 全量重写后 diff 为 0（测试仍在用 `_Session`/`ListenerRegistry`/字节 APCI seam/`_KeyLocks` 等已删除的内部结构），整体不再适用，按任务要求删除并重写：

- **删除**：`tests/component/core/protocol/iec104/test_receive_resilience.py`、`tests/unit/core/protocol/iec104/test_key_locks.py`、`tests/unit/core/test_iec104_timeout_isolation.py`、旧字节级 `tests/support/iec104_hat_fake.py`（删除前已确认全仓无引用）。
- **新增**：
  - `tests/support/iec104_fake.py`：消息级内存假连接（STARTDT 使能回调、STOPDT、报文投递/抓取、物理关闭失败注入），不含任何旧内部 API；
  - `tests/component/core/protocol/iec104/test_single_receiver.py`（5）：唯一 receive 循环无并发、混合批次路由、未知 IOA 丢弃不杀循环、异常收敛、无在途读时 REQUEST 数据当样本；
  - `tests/unit/core/protocol/iec104/test_timeout_isolation.py`（4）：读/写/总召超时隔离语义。
- **重写**：IEC104 组件 7 文件（生命周期 10、主动读 10、写控 11、总召 8、监听 7、健康/热更 6，含上述新增共 57）；单测 codec 时间戳/质量（40）、mapping（31）、listener（4）；真实链路集成 `test_driver_c104.py`/`test_remote_control.py`、可靠性、E2E config-to-wire、控制服务器 fixture（新增 `sbo_ioas` SBO 支持）。
- **共享替身/契约适配**：`new_commander.py`/`new_collector.py`（subscribe 同步 start/stop、InterrogationRequest、FakeListenHandle）、4 个 protocol 契约文件、collector session、ADS mapping/凭据、modbus 白盒补 `_events`。
- 所有新断言直接对应任务书行为契约；失败复现用例均断言**正确行为**，而非迁就实现。

## 5. 测试结果总览

全量（`tests/`，排除 soak/performance；importlib 模式仅用于规避 main 既有的同名文件收集冲突 `test_point_store.py`，见 §8）：

```
1717 passed, 18 failed, 17 deselected, 319.22s
```

- **14 failed = RELATED 生产缺陷复现**（见 §6），无一是测试本身问题。
- **4 failed = UNRELATED**（origin/main 与交接点均失败，见 §8）。
- 关键分层：
  - IEC104 组件 57：55 passed + 2 失败复现（P1-a/P1-b）；
  - IEC104 单测 79：78 passed + 1 失败复现（Bitstring）；
  - 真实链路 46 collected：35 passed / 11 failed（11 全部为 §6 的 RELATED 复现，无 skip）；
  - ADS 单测+组件 39、Modbus 191、Collector 346、Commander 151、config 81、recovery 可靠性 17、契约（除 2 个 UNRELATED ADS 用例外）全绿；网络劣化在 unshare 真实子进程 8 passed。

## 6. 生产缺陷清单（阻断）

### P1-a　C_RD 主动读在真实链路上必然失败（协议回归）

- 位置：`src/core/infrastructure/protocol/iec104/driver.py:698-704`（`_dispatch`）。
- 现象：对**任何**入站 `ReadMsg`，只要有在途读且非 test 帧，一律 `set_exception("IEC104 read rejected: {cause}")` 并随后隔离会话。
- 实证（裸 hat 主站 × c104/lib60870 从站，`/tmp/iec104_probe/read_probe.py`）：从站对 C_RD_NA_1 的标准应答是两帧——先回 `ReadMsg(cause=7 ACTIVATION_CONFIRMATION)`（ACT_CON 回显），随后才是 `DataMsg(cause=5 REQUEST)` 带真实值。驱动在第一帧即判死 future 并隔离，第二帧永无机会完成读取。
- 旧实现对照：origin/main `driver.py:868 _dispatch_read_negative` 仅在 cause 为 `iec101.ReadResCause`（44–47）时判否定，放行 cause=7 回显。
- 修复方向（建议，不代为修改）：ReadMsg 分支只对 44–47 否定 cause 置异常；cause=7 回显忽略，由后续 DataMsg(cause=5) 关联完成。
- 复现（9）：组件 `test_read_act_con_echo_ignored_and_data_completes`、`test_negative_read_response_fails`（否定 47 路径，当前通过，保证不回退）；真实链路 `test_driver_c104.py` 6 例（真实 C_RD 事务、新鲜值非缓存、多 CA 同 IOA、服务重启显式重连、短时标回退本地接收时、点表热更）、可靠性 `test_many_active_reads_leave_stable_resources`、E2E `test_yaml_config_drives_real_iec104_session`。

### P1-b　连接失败路径泄漏内置异常，破坏端口边界契约

- 位置：`src/core/infrastructure/protocol/iec104/session.py:73`（`open` 直接 `await iec104.connect(...)`）与 `driver.py:239`（`asyncio.wait_for(session.open(), timeout=t0)`）。
- 现象：未监听端口时裸抛 `ConnectionRefusedError`（OSError）；STARTDT/t0 超时时裸抛 `asyncio.TimeoutError`。均不是端口契约要求的 `ProtocolConnectionError`（实证探针 `connect_refused_probe.py`：`isinstance(exc, ProtocolError)=False`）。Modbus 驱动有包装先例。失败后可再 connect 成功，无状态卡死，故为错误分类/边界缺陷。
- 复现（3）：组件 `test_startdt_never_arrives_times_out_and_allows_reconnect`（期望 ProtocolConnectionError）、真实链路 `test_connect_failure_then_retry_succeeds`、可靠性 `test_connect_refused_fails_explicitly`。

### P1-c　BitstringData（M_ME_ND_1 比特串）解码必然 ValueError

- 位置：`src/core/infrastructure/protocol/iec104/codec.py:168`：`return int(data.value.value), quality`。
- 现象：`BitstringValue.value` 是 `bytes`（如 `b'\x01\x00\x00\x00'`），`int(bytes)` 必抛 `ValueError: invalid literal for int() with base 10`。任何比特串测点入站都会走解码异常路径（监听丢样本/主动读失败）。
- 修复方向：`int.from_bytes(bytes(data.value.value), "little")`（hat BitsValue 为小端 4 字节）。
- 复现（1）：`tests/unit/core/protocol/iec104/test_codec_time.py::test_bitstring_data_decoded_as_unsigned_int`。

### P2　写超时 UNKNOWN 的 message 为空串（诊断质量）

- 位置：`driver.py:499`：`ProtocolWriteResult(..., WriteOutcome.UNKNOWN, str(exc))`；`asyncio.TimeoutError` 的 `str()` 为 `''`。
- 影响：outcome 分类正确（UNKNOWN，未误判 CONFIRMED_FAILURE，见 §7），但丢失"命令可能已执行"的运维语义，旧实现有 "may have executed" 文案。
- 复现（1）：真实链路 `test_command_timeout_with_transport_acks_only_is_unknown`（outcome 断言通过，仅 message 断言失败）。

### P3（非阻断，建议清理）

- `codec.py:268 negative_read_message()` 为死代码，无任何调用方（新 `_dispatch` 直接内联文案）。建议删除，或在修复 P1-a 时复用它生成 44–47 的否定原因。

## 7. 命令安全 / UNKNOWN 专项结论

按任务书重点单独核验，以下行为**正确**（真实链路 + 消息级双层证据）：

- 写超时/STOPDT 隔离：结果为 UNKNOWN 且**不自动重发**（只发一帧命令，会话隔离）——组件 `test_timeout_is_unknown_no_automatic_resend`、真实黑洞链路一致。
- 否定 ACT_CON / UNKNOWN_IO_ADDRESS 等 44–47：归类 CONFIRMED_FAILURE，**不误标 UNKNOWN**，且不隔离会话。
- ACT_CON 提前成功后 ACT_TERM 未到期间，重复控制在 unsettled 表处被拒（CONFIRMED_FAILURE，命令不上 wire）。
- SBO：select 否定不发 execute；execute 否定补发 DEACT 再收敛；真实从站的直控拒选、SBO 全链路成功均验证。
- 确认关联按 (CA, IOA, command value) 三元组：异 CA/异值确认不串单。
- GI/同站控制互斥（station_lock），GI 未完成控制不上 wire；GI 只收 INTERROGATED_* COT，SPONTANEOUS 不入结果。
- Watchdog 重连固定 close()→connect()；对端 STOPDT 后 `_closing` 置位，裸 connect() 被 fail-closed 拒绝（与 watchdog.py 实现一致，经验证非缺陷）。
- 物理 close 失败：会话 unsettled 保留并对外 FAULTED，不丢状态（3696d263 已含修复，组件覆盖）。

## 8. UNRELATED 基线失败（4，经授权记录，不扩大范围）

四项在 `origin/main 5e0d5c45` 与交接点 `3696d263` 均失败，与本重写无关：

1. `tests/contract/core/test_protocol_one_many_contract.py::test_ads_single_operations_do_not_delegate_to_many`
2. 同文件 `test_ads_single_operations_succeed_without_many_on_connected_path`
   —— ADS 生产 `read_one` 一直委托 `read_many`（main 与本分支实现一致），与该契约"单点不得委托批量"断言冲突，属契约 owner 决策项；本轮已顺手修复其工厂注入漂移，但未弱化断言。
3. `tests/contract/web_api/test_openapi_contract.py::test_committed_openapi_matches_live_schema`（main 即失败，schema 漂移）。
4. `tests/integration/protocols/test_gateway_r2_fc15.py::test_fc01_readback_matches_point_store`（main 即失败；历轮已在案的 FC15 基线项）。

另：`tests/unit/{web_server,industrial_gateway}/test_point_store.py` 同名文件在默认 prepend 导入模式下收集冲突，main 即存在；全量回归使用内置 `--import-mode=importlib` 规避，未改任何测试或配置。

## 9. 已验证正确的新设计面（抽样）

单接收循环（最大并发 receive=1）、有界事件流（256 溢出判失效而非静默丢数、回调异常只杀坏监听者不影响健康监听者）、按址锁/站锁有界不随操作增长、close 幂等与无后台任务残留、多 CA 同 IOA 独立寻址、CP56Time2a 世纪翻转与夏令时限定符（9876a8ca 语义，单测 40 例）、collector 重建先关全部旧句柄（含此前 stop 失败的句柄，3696d263）。

## 10. 结论与后续

**FAIL。** 存在 3 项 P1（其中 P1-a 使真实从站上的主动读功能完全不可用，P1-b 破坏端口异常契约，P1-c 使比特串测点必崩）与 5 项 RELATED mypy 严格门禁错误；按任务书 P0/P1 存在即 FAIL 的规则，不具备合并条件。本轮未合并、未建 PR、未触碰 main。

交 ChatGPT 修复（均已给出最小复现与定位）：

1. driver `_dispatch` ReadMsg 分支按 cause 44–47 才判否定，放行 cause=7 回显（P1-a，9 个复现转绿即主要解锁项）；
2. session.open/connect t0 路径把 OSError/TimeoutError 包装为 ProtocolConnectionError（P1-b，3 复现）；
3. codec BitstringData 改 `int.from_bytes(..., "little")`（P1-c，1 复现）；
4. UNKNOWN 超时补非空语义消息（P2，1 复现）；
5. 5 项 mypy（§3）；P3 死代码顺手清理。

修复后请回到本分支重跑：IEC104 组件/单测（11 个复现中的 4 个本地用例）+ 真实链路 46 例 + 全量回归（319s）。真实硬件/真实主站 L3 本轮未覆盖（环境无 PLC）。

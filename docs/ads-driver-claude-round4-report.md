# ADSDriver 第四轮独立验收报告（Claude Code）

- 结论：**FAIL — 当前不满足合并门禁**。1 个 P1 功能缺陷（route_auto_add 重试不可达，D1），静态门禁 8 个 Ruff、2 个 Mypy、1 个新增 Format 漂移未清；另发现 4 项 P2 清理残留。P0 八项中七项 PASS，P0-2/3/4/5/8 的并发与数据安全在真实 AMS/TCP 栈与替身双层测试下通过。
- 真实 TwinCAT 硬件相关项全部 **NOT RUN**（本机无 PLC）。
- 验收人未修改任何生产代码，未添加任何 lint/type 忽略项，未执行合并。

## 1. Git 基线与环境

| 项 | 值 |
|---|---|
| 验收分支 | `refactor/ads-driver-clean-rewrite` @ `a72d73b0` |
| 基线 | `main` @ `8b6f654a` |
| 验收工作分支 | `verify/ads-round4`（worktree `/home/luosh/wind-hub-ads-r4`） |
| Python | 3.11.16，conda env `wind-hub`（`scripts/dev.py env` 通过） |
| pyads | 3.6.0（API 结论全部对安装源码逐行核查） |
| 服务端 | 进程内 pyads `AdsTestServer`（真实 AMS/TCP 线协议，`real_service`） |

生产侧改动范围（分支相对 main）：`driver.py`/`session.py`/`subscription.py` 重写（+552/−1813），`config.py`/`codec.py`/`mapping.py`、`port/protocol.py`、`collector/application/session.py`、配置解析与 Web Settings 迁移。

## 2. 命令、退出码与测试分层

| 命令（worktree 内，`PYTHONPATH=src`） | 结果 |
|---|---|
| `ruff check src tests scripts` | **FAIL（rc 1）**：8 errors，全部在 `src/core/infrastructure/protocol/ads/` |
| `ruff format --check src tests scripts` | **FAIL（rc 1）**：新增漂移 1 个生产文件（subscription.py）；其余脏文件与 main 基线集合一致（main 105、分支 101，逐文件集合比对） |
| `mypy src` | **FAIL（rc 1）**：2 errors（session.py:81、subscription.py:128） |
| `lint-imports` | **PASS（rc 0）**：11 kept, 0 broken |
| `pytest tests/unit tests/component tests/contract --import-mode=importlib` | **PASS**：1540 passed, 1 xfailed（D1 strict xfail） |
| `pytest tests/integration --import-mode=importlib` | 128 passed, 1 failed |
| `pytest tests/system --import-mode=importlib` | 78 passed（首轮 1 个 Modbus flake，复测 78 全绿） |
| `pytest tests/reliability --import-mode=importlib` | 47 passed, 3 skipped, 10 deselected（netem 矩阵真实执行） |
| ADS 真实协议集成（10 例） | 10 passed（4 commander + 6 新 driver 直连） |

两个非 ADS 失败的 UNRELATED 取证：

- `tests/integration/protocols/test_gateway_r2_fc15.py::test_fc01_readback_matches_point_store`（Modbus 读线圈）：在 `origin/main` 临时 worktree 同命令复现失败 → **UNRELATED（main 既有）**。
- `tests/system/test_configured_protocol_servers_e2e.py::test_modbus_values_change_over_real_protocol`：单测隔离通过、全量 system 复跑 78 全绿 → **UNRELATED flake**（顺序/端口时序，分支不触碰 Modbus 路径）。

本轮新增/重建测试（均为测试与 fixture）：

- component：`test_session.py`(9)、`test_driver_lifecycle.py`(12+1 xfail)、`test_sum_read.py`(7)、`test_sum_write.py`(6)、`test_subscription_lifecycle.py`(7)、`test_subscription_delivery.py`(6) 及 conftest/`tests/support/ads_fake.py`。
- integration：`test_driver_ads_real.py`(6，真实 AdsTestServer)。
- reliability：`core/test_ads_reliability.py`(4，拒连/断链并发/重启/50 轮循环)、`recovery/test_ads_recovery_leaks.py`(4，100 轮连接/50 轮订阅/双 close/10 轮重启)、`recovery/test_ads_shutdown_races.py`(3，通知洪流 close/20 轮 start-close 竞争/connect-close 竞争)。
- 契约与调用方迁移：protocol registry/port/one-many 契约、new_collector/new_commander fakes、IEC104 两处去 `await subscribe`、Web Settings、ads credentials/mapping 单测、`test_ads_timeouts.py`(8)。

## 3. P0 八项核查结论

1. **真实 pyads API 签名 — PASS。** 对 pyads 3.6.0 源码核查并用真实 AdsTestServer 实证：`Connection(net_id,port,host)`（TCP 固定 48898）；Linux `open()=adsPortOpenEx→adsAddRoute`，失败回滚本地端口；`close()=adsDelRoute+adsPortCloseEx`，未 open 早退；`set_timeout(ms)` 每连接；`read_state()→(adsState,devState)`，RUN=5；`read_write(ig,io,read_type,value,write_type,return_ctypes,check_length)`，Sum Read=0xF080（4N 错误码+数据），Sum Write=0xF081（12N 头+数据，4N 错误码）；`get_symbol` 构造即远程解析（未知符号真实返回 ADSError 1861）；`notification(datatype)` 装饰器回调四参 `(handle,name,timestamp:datetime,value)`；`add_device_notification→(h,uh)`、`del_device_notification(h,uh)`，真实 del 后变更不再投递（有测试断言服务端注册表清零）。
2. **connect 超时/取消 — PASS（含一处 P1 失败，见 D1）。** wait_for+shield 总预算；迟到 open 由 done_callback 清理，实测不发布 CONNECTED 且可重连（`test_connect_deadline_cleans_late_open_and_allows_reconnect`）；总预算过期后即使路由注册成功也不发起第二次物理 connect（`test_route_add_over_total_budget_must_not_start_second_connect`，断言第二个替身 `opened is False`）。
3. **Session single-lock — PASS。** 物理命令零重叠（峰值并发=1）；排队未发送命令取消不下发；已提交 to_thread 抗外层 wait_for 超时，close 持 gate 等物理线程真实结束；open 失败即 `_uncertain`、close 不可证时 fail-closed（9 个 session 组件用例）。
4. **Subscription 竞态 — PASS。** start 取消迟到成功由独立 stop 任务补 del；注册不确定/ del 失败聚合 ExceptionGroup 并 FAULTED 保留占用；stop 反序、幂等；driver.close 收敛订阅；真实协议栈下通知洪流（2ms/次）close 在预算内收敛且服务端注册表为空，20 轮 start/close 竞争无挂起。
5. **Writer UNKNOWN — PASS。** 已下发通信失败=UNKNOWN 且物理只发一次（显式断言 calls==1，无重发）；后续未发送块=CONFIRMED_FAILURE("not sent")；NotSent=CONFIRMED_FAILURE；本地编码失败不联网；真实 testserver 写后读回 CONFIRMED_SUCCESS；逐点设备错误 CONFIRMED_FAILURE。
6. **资源与重连 — PASS（设计行为已记录）。** 同 Net ID 互斥直到确证释放；close_local 有 owner 时拒绝；open 失败（含拒连）一律 FAULTED 占用为**有意的 fail-closed**（真实拒连 F01：<3s 收敛、FAULTED、不影响其他 Net ID），运维含义是拒连后需重启进程，建议在文档明示。多 Driver 并发 initialize_local 幂等/mismatch 拒绝有测试；进程退出/SIGKILL 残留 NOT RUN。
7. **Connector/shared contract — PASS。** `subscribe()` 同步返回 handle（async start/stop）；Collector `NativeSubscriptionAcquisitionHandle.close→stop`，start 失败走 stop 清理；Modbus/IEC104 保留同步 subscribe 并抛 ProtocolCapabilityError；`src` 全量 grep 无 `await *.subscribe(` 残留；契约测试已迁移并通过。
8. **数据 — PASS。** Sum Read 顺序/逐点 BAD/非法 UTF-8/短应答；Symbol 在 connect 时真实解析、点表热更新 open+symbol 变更拒绝；通知批量解码顺序、naive 时标补 UTC、非 datetime 标 local、载荷尺寸不符整块 BAD、1005 批灌入 1000 队列丢最旧 5 保最新、回调异常 worker 存活、stop 后迟到回调忽略。

## 4. 缺陷清单（交 ChatGPT 修复，附复现）

### P1（阻断合并）

**D1：route_auto_add 路由注册重试路径完全不可达，配置功能失效。**

- 位置：`src/core/infrastructure/protocol/ads/session.py:98-102,147-150` + `src/core/infrastructure/protocol/ads/driver.py:230-251`。
- 机理：`_AdsSession.open()` 中 `connection.open()` 抛错即置 `_uncertain=True`；driver 第一次 attempt 失败后执行 `await session.close()` 收尾，而 `_close_once` 在 `_uncertain` 时必抛 `ADSResourceUncertainError`（session.py:147）。driver.py:233-235 捕获后直接 `FAULTED; raise`，第 237-250 行的"注册路由→重试一次"代码永远不可达。route_auto_add 是"PLC 侧未预置路由时首次连接"的唯一自愈手段，该选项当前在任何 open 失败下都不会生效。
- 复现：`pytest tests/component/core/protocol/ads/test_driver_lifecycle.py::test_route_auto_add_retries_once_after_route_registration` — strict xfail（xfail）即缺陷在案证据；修复后应转 XPASS，请同时去掉 xfail 标记。
- 修复建议（仅供参考，不由验收方改码）：区分"open 明确失败且 pyads 已自回滚本地端口"与"物理状态不明"。pyads 3.6.0 的 `open()` 在 `adsAddRoute` 失败分支已自行 `adsPortCloseEx`（见 pyads connection.py 源码），该路径下 close 不应升级为 uncertain；或让 driver 在 attempt 收尾时对"从未 opened"的 session 不走 uncertain 中止而进入路由注册分支。修复后需保证失败 open 仍不释放 Net ID 占用（fail-closed 语义不回退）。

**静态门禁 10 项（Ruff 8 + Mypy 2，main 同目录全绿，全部 RELATED）：**

| 文件:行 | 规则 | 说明 |
|---|---|---|
| driver.py:94 | SIM300 | `if _LOCAL_IDENTITY != config` Yoda 条件 |
| driver.py:237 | E501 | 102 字符 |
| driver.py:239 | B904 | except 内 `raise ConfigError` 缺 `from` |
| driver.py:292/301/412 | E501 | 行过长 |
| subscription.py:164 | SIM105 | 可改 `contextlib.suppress(Exception)`；注意此处 await 的是 shield(start_task)，改写后请保留取消传播语义 |
| subscription.py:177 | SIM105 | worker cancel 汇合同上 |
| session.py:81 | mypy no-any-return | `notification()` 返回 Any，需显式包装类型 |
| subscription.py:128 | mypy arg-type | `timestamp_source=source` str 不满足 `Literal['device','local']`，给 `source` 显式 Literal 类型即可 |

新增 format 漂移：`subscription.py`（main 干净、分支脏）。`driver.py`/`parse_domain.py` 虽也不达标，但两者在 main 上即在既有 105 文件脏集合内，逐 hunk 比对无新增格式段；按基线规则记 UNRELATED，但建议本轮顺手修复。

### P2（不阻断但属清理门禁）

- **P2-1 死配置项**：`config.py:17-20,39-42,93-115` 仍解析、校验并存储 `push_batch_size`、`push_batch_wait`、`max_notifications_per_connection`，全仓 grep 无任何消费点（旧推送循环机制已不存在）。配置面接受但无效，属 R04 旧配置残留。
- **P2-2 死代码**：`codec.py:151 _as_point_scalar`、`:159 _bad_sample`、`:169 _is_point_level_error` 三个函数全仓零调用。
- **P2-3 孤儿 fixture**：`tests/support/ads_subprocess_client.py`(72)、`ads_controlled_server.py`(268)、`ads_concurrency_child.py`(773)、`ads_memory_server.py`(775)、`ads_profiling.py`(231)，共 2119 行，引用关系只剩彼此互引，无任何存活测试/代码使用。
- **P2-4 订阅对象生命周期累积**：`ADSSubscription.stop()` 不把自己从 `driver._subscriptions` 移除，集合只在 `driver.close()` 清空（subscription.py 无 discard、driver.py:293 clear）。采集句柄高频 start/stop churn 的长生命周期进程会无限累积 Subscription 对象与闭包。原生 notification 句柄本身已平衡（50 轮 add/del 各 50 次实测），故仅 P2。
- 另：`driver.py:157-159` FAULTED 分支不可达（155-156 行已先行拦截），死分支，可一并清理。

## 5. 64 项功能与可靠性逐项追踪

### A. pyads 真实 API 与 _AdsSession（1–12）

| # | 要求 | 结论 | 证据 |
|---|---|---|---|
|1|Connection 构造/48898 固定端口|PASS|源码核查 + 10 个真实用例|
|2|open 端口/路由分配与失败回滚|PASS|pyads 源码；F01 拒连行为|
|3|close del route+关端口，未 open 早退|PASS|test_session close 幂等|
|4|set_timeout(ms) 每连接|PASS|test_connect..._sequence 断言 timeouts|
|5|read_state RUN=5 判定|PASS|lifecycle 用例|
|6|Sum Read 0xF080 线格式/短应答|PASS|真实读 + 组件短应答|
|7|Sum Write 0xF081 线格式/逐点错误|PASS|真实写读回 + 6 个组件用例|
|8|notification 四参回调|PASS|真实通知 + 6 个投递用例|
|9|add/del notification 配对|PASS|真实服务端注册表清零断言|
|10|get_symbol 远程解析/1861|PASS|真实未知符号用例|
|11|单锁零重叠/排队取消/已提交抗取消|PASS|test_session 9 例|
|12|一次性 open、close 幂等|PASS|test_session|

### B. 五态生命周期与超时（13–26）

| # | 要求 | 结论 | 证据 |
|---|---|---|---|
|13|五态迁移|PASS|12 lifecycle 用例|
|14|并发 connect 合并单次|PASS|test_concurrent_connects_coalesce|
|15|正常失败释放身份可重连|PASS|test_connect_failure_closes_session...|
|16|总预算迟到成功不发 CONNECTED|PASS|deadline cleans late open 用例|
|17|超预算不发第二次 connect|PASS|route budget 用例（second.opened is False）|
|18|route_auto_add 重试一次|**FAIL**|D1 strict xfail|
|19|同 Net ID 互斥至释放|PASS|owner 用例|
|20|close 超时 FAULTED+物理继续收敛|PASS|close deadline 用例|
|21|close 失败占用阻断替换|PASS|holds faulted identity 用例|
|22|initialize 幂等/mismatch/端口回滚|PASS|initialize_local 用例 + 源码|
|23|close_local 有 owner 拒绝|PASS|initialize/close gate 用例|
|24|local close 后拒 connect|PASS|connect_after_local_close|
|25|拒连有界 fail-closed|PASS|F01（真实 127.0.0.2，<3s）|
|26|服务端重启同端点重连|PASS|F11（真实重启）|

### C. Subscription（27–38）

| # | 要求 | 结论 |
|---|---|---|
|27|同步 subscribe、async start/stop|PASS|
|28|注册/反注册一次、stop 幂等|PASS|
|29|start 取消迟到成功补 del|PASS|
|30|注册不确定 ExceptionGroup+FAULTED|PASS|
|31|del 失败 close 后仍 FAULTED 占用|PASS|
|32|driver.close 收敛全部订阅|PASS|
|33|worker 吞取消超预算→FAULTED 后收敛|PASS|
|34|解码字段/顺序|PASS|
|35|UTC/local 时标规则|PASS|
|36|载荷尺寸不符整块 BAD|PASS|
|37|队列饱和丢最旧不阻塞原生线程|PASS（1005 批实测丢 5 保最新）|
|38|回调异常存活/迟到回调忽略/真实投递与停止|PASS（含真实 testserver 2 例）|

### D. Sum Read/Write（39–48）

| # | 要求 | 结论 |
|---|---|---|
|39|空读写 noop|PASS|
|40|读顺序保持|PASS|
|41|逐点设备错 BAD|PASS|
|42|解码失败 BAD|PASS|
|43|短应答 ProtocolError|PASS|
|44|断链读有界显式失败|PASS（F07 真实断链 8 并发）|
|45|逐点写错误 CONFIRMED_FAILURE|PASS|
|46|本地编码失败不联网|PASS|
|47|已下发 UNKNOWN/后续 not sent/不重发|PASS（calls==1 实测）|
|48|NotSent→CONFIRMED_FAILURE|PASS|

### E. 配置/契约/调用方（49–58）

| # | 要求 | 结论 |
|---|---|---|
|49|三超时默认/正整数校验|PASS（8 单测；0/-1/"1000"/1.5/True 全拒）|
|50|parse_domain/snapshot roundtrip/YAML|PASS|
|51|Web Settings 保存身份保留超时|PASS（新增专测）|
|52|options 白名单与类型严格校验|PASS（契约+单测）|
|53|twincat_version→801/851|PASS（源码核查）|
|54|ProtocolPort 同步 subscribe 契约|PASS|
|55|Collector close→stop、start 失败清理|PASS（源码+契约）|
|56|Modbus/IEC104 同步拒订阅、无 await 残留|PASS（grep+契约+两处迁移测试）|
|57|点表热更新规则|PASS（open+symbol 变更拒绝实测）|
|58|Commander 真实装配链路|PASS（4 真实用例）|

### F. 资源/可靠性（59–64）

| # | 要求 | 结论 |
|---|---|---|
|59|50 轮 connect/close 有界无残留|PASS（真实，<30s 预算实测约 0.1s）|
|60|100 轮连接/50 轮订阅/双 close/端口平衡|PASS（替身精确计数）|
|61|10 轮服务端重启重连|PASS（真实）|
|62|洪流 close/start-close 20 轮/connect-close 竞争|PASS（真实 3 例）|
|63|断链期并发读全部有界、无假 GOOD|PASS（F07/F16，8 并发真实断链）|
|64|SIGKILL 残留/句柄跨进程复用/真实 TwinCAT L3|**NOT RUN**（需硬件与子进程 harness）|

合计：**PASS 62，FAIL 1（#18，D1），NOT RUN 1（#64）**。#64 中"真实 PLC 通知周期、TC2/TC3 端口、长稳与 kill -9 句柄回收"在无硬件环境无法验证，合并后硬件门禁须补跑。

## 6. R01–R10 清理门禁

| 门禁 | 结论 | 证据 |
|---|---|---|
|R01 旧 Driver/Session/Subscription 生产代码删除|PASS|ads 包仅 6 个生产文件；旧类/旧机制 grep 无残留，分支净删约 1813 行|
|R02 无兼容层/新旧接口并存|PASS|src 无 `await subscribe`、无 close() 式旧句柄；跨协议契约一致|
|R03 旧测试/fixture 清理且不减测|**PARTIAL**|旧 ADS 测试已由 60+ 新用例替代并增量（1540 全过）；但 5 个孤儿 support 文件 2119 行未删（P2-3）|
|R04 旧配置清理|**FAIL**|push_batch_size/push_batch_wait/max_notifications_per_connection 仍存在（P2-1）|
|R05 死代码清理|**FAIL**|codec 三个零调用 helper（P2-2）、driver.py:157-159 死分支|
|R06 Ruff Lint|**FAIL**|8 个 RELATED 错误（见第 4 节）|
|R07 Ruff Format|**FAIL**|subscription.py 新增漂移；driver.py/parse_domain.py 为 main 既有脏集合（UNRELATED）|
|R08 Mypy|**FAIL**|2 个 RELATED 错误（见第 4 节）|
|R09 Import Linter/循环依赖|PASS|lint-imports 11 kept 0 broken|
|R10 不绕过检查/不新增忽略|PASS|生产侧仅 4 处 `pyads # type: ignore[import-untyped]`（pyads 无 stubs 的既有惯例），无 noqa/规则放宽；验收新增测试侧零 lint 问题|

## 7. 合并结论

**不可合并。** 阻断项：

1. P1 D1（route_auto_add 自愈功能失效，strict xfail 在案）；
2. R06/R08：8 Ruff + 2 Mypy（全部 RELATED，main 同目录零问题）；
3. R07：subscription.py 新增 format 漂移。

建议 ChatGPT 一并清理 P2（R03/R04/R05 与订阅集合累积），修复后交回；验收方将对 D1 去 xfail 转实测、静态门禁与全分层做独立复测。真实 TwinCAT L3（#64）在合并前或合并后硬件门禁必须补跑，当前状态 NOT RUN。

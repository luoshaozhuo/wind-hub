# 业务设计对齐 20261110 — 第四轮遗留系统测试整改与复测（R4）

- 分支：`docs/business-design-alignment-20261110`
- 基线：`ed96b5f3e78df86526d8d6099746b20506239c2f`（R3 验收提交）
- 整改范围：R3 全量清单中的 8 failed + 21 errors，逐条定性（夹具/测试契约/环境/真实生产缺陷）
- 硬约束：只改测试、Fixture、纯格式；生产业务缺陷交 ChatGPT；冻结模块（Contracts/Core/Configuration）、Industrial Gateway 租约契约、Sink Service、`feat/sink-service-v1-rewrite` 零改动；无 skip 滥用、无 xfail/noqa/ignore、无断言弱化
- 日期：2026-11-11

## 0. 结论摘要

| R3 遗留项 | 数量 | R4 定性 | 处置 | 复测 |
| --- | --- | --- | --- | --- |
| Web Fleet 模块 fixture ERROR | 21 | 夹具缺陷（缺 Commander mTLS 客户端三路径） | 测试 PKI + `commander_client_env()` 注入，mTLS 不关闭、无明文回退 | 22/22 PASS |
| REST→Commander 命令 E2E | 4 | 1 处夹具缺口 + 3 处测试契约漂移，**零生产缺陷** | 见 §2 逐条证据 | 4/4 PASS |
| Config Apply 系统测试 | 1 | 测试前提过期（v1/v2 分治拓扑下扁平文件不属 Commander config_sources） | 改写为真实 fail-closed/回滚/零变异验收 | 3/3 PASS |
| CLI TCP 探针 | 1 | 测试自身竞态（复用刚关闭临时端口，WSL2 端口复用） | bind 不 listen 占坑，与环境端口状态解耦 | 5/5 复跑稳定 |
| CLI systemd ×2 | 2 | 环境能力缺失（有 D-Bus socket 但无 dbus-next 绑定） | 能力守卫显式 NOT_RUN + 新增缺绑定 fail-closed 分类用例 | 2 NOT_RUN + 1 新用例 PASS |

**最终裁决：PASS，REL 项全部闭环；本轮整改未发现任何新增生产缺陷（NEW PRODUCTION DEFECT = 0）。** 全部改动限于 6 个测试/夹具文件，生产代码 `src/` 零改动（`git diff --stat` 可核验）。

## 1. S1 静态门禁（最终工作树新鲜复跑）

| 门禁 | 命令 | 退出码 | 结果 |
| --- | --- | --- | --- |
| Ruff Check | `ruff check src tests scripts` | 0 | All checks passed |
| Ruff Format | `ruff format --check src tests scripts` | 0 | 742 files already formatted |
| Mypy | `mypy src` | 0 | Success: no issues found in 333 source files |
| Import Linter | `lint-imports` | 0 | Contracts: 15 kept, 0 broken |
| pb2 架构边界 | `pytest tests/core/architecture/test_rpc_boundary.py -q` | 0 | 4 passed |
| Proto 漂移 | `python scripts/generate_rpc.py --check` | 0 | 无生成物差异 |
| API 漂移 | `python scripts/generate_api_contract.py --check` | 0 | DRIFT CHECK: PASS（44 paths） |

## 2. REST→Commander 四例的逐条定性与证据

### 2.1 夹具缺口：v2 点表缺控制写点（PERMISSION_DENIED 根因）

`write_v2_modbus_tree()` 自生成的点表只有 100/102/104 三个采集点，Commander `ConfigManager.authorized()` 对不存在点按冻结授权链拒绝（`queued_executor.py` 抛 "reference is not authorized"）。在 `tests/support/v2_config.py` 补 `setpoint_power`（holding@200, float32，point_groups 含 control）与 business_points 目录条目；holding 寄存器经冻结的 `shared_point_tables.py` 规则自动派生 READ_WRITE，未改任何生产授权逻辑。

### 2.2 测试契约漂移①：字序（独立回读解出 -1.595e-23）

物理证据：失败时从站 200/201 实际寄存器为 `[0x999a, 0x42b1]`，即 88.8 大端编码 `[0x42b1, 0x999a]` 的**字序互换**——写入真实到达、地址正确，仅字序与测试解码假设不一致。

根因链（全部在测试配置侧）：

- 生产 `ModbusConnectionConfig.word_order` 默认 `little_endian`（`src/core/infrastructure/protocol/modbus/config.py:35`），出厂协议模板 `configs/templates/protocols/modbus_tcp_1.0.0/1.1.0.yaml` 均为 `word_order: little_endian`；
- v1 `functional_config.modbus_device_dict()` 显式覆盖 `big_endian` 以匹配 ModbusMockServer 的大端布局；同仓其余 modbus 夹具（data_generator、gateway_v1、grpc_harness、commander system conftest）全部显式 big_endian；
- 唯独 `write_v2_modbus_tree()` 自建型号只下发 `slave_id`，落到出厂 little_endian 默认，与同一 full_stack 内 Web Server 侧 v1 配置及 Mock 从站布局不一致。

修法（夹具侧）：型号模板 `protocol_options` 显式 `word_order: big_endian`（遵守 Commander b1 契约——字序只配在型号模板，实例 overrides 不放）。生产编解码行为本身自洽，无缺陷。

### 2.3 测试契约漂移②：readback 为 None

`CollectorReadRouter.read_point()` 要求设备在 `TaskPlacementRegistry` 中恰有一个 ASSIGNED Collector owner，否则抛 RuntimeError 并被 `CommandService` 吞入 `readback_error`（readback 保持 None，不推翻写入成功）。full_stack 的 Collector 任务默认 desired_state=STOP，从未 START，无 placement——同仓 `tests/system/command/test_write_idempotency.py::test_successful_write_does_not_depend_on_readback_path` 已锁定该设计。修法：测试内先经真实 REST `POST /api/v1/tasks/modbus-telemetry/start`（断言 200），由生产路径完成 placement 后再发写命令；回读值 66.6、quality=good 与独立从站回读全部通过，无任何断言弱化。

### 2.4 测试契约漂移③：未知设备错误信封

生产 `CommanderGrpcClient.write()` 把 GetDevice 的 RpcError（含 NOT_FOUND）在 Submit 前转译为 `CONFIRMED_FAILURE`（可确证未执行控制，at-most-once 不重试）；Commander 服务端 NOT_FOUND 固定文案 `"device not found"`（`src/commander/infrastructure/grpc/server.py:174`），不含请求方设备 id。旧断言要求错误文本包含 `"ghost-device"` 在生产构造下不可能成立，且无设计文档要求回显设备 id（同层 RPC 测试 `test_unknown_device_write_is_confirmed_failure` 只断言 error 非空）。修法：断言三态结果 `outcome == "confirmed_failure"`（StrEnum 线格式为小写值）、错误含权威细节 `"device not found"`、从站 write_count=0；安全断言一条未减。

### 2.5 测试契约漂移④：Commander 不可达是 200 还是 503

旧测试断言 503 WORKER_UNAVAILABLE。逐层核验生产契约后确认**生产行为正确、测试前提错误**：

- OpenAPI 冻结契约（`src/web_admin/src/api/generated/openapi.json`，漂移检查 PASS）中 `POST /devices/{id}/commands` 只声明 200（DeviceCommandResponse）与 422，**没有 503**；
- 统一错误处理器 `errors.py` 的 gRPC UNAVAILABLE→503 映射只适用于让 RpcError 冒泡的路由（如 tasks start/stop，`tasks.py:90/107`）；命令管道在出站客户端内有意吞掉提交前 RpcError 并转译为三态结果，这是 at-most-once 现场控制语义的核心设计（提交前失败=CONFIRMED_FAILURE，提交后不确定=UNKNOWN，均不得自动重发）；
- 单元层 `tests/web_server/test_config.py::test_legacy_file_outside_commander_sources_fails_closed` 与 RPC 层 `test_server_commander_rpc.py` 已分别锁定转译与 NOT_FOUND 语义。

修法：断言真实 fail-closed 信封——HTTP 200、`success is False`、`outcome == "confirmed_failure"`、error 含 `UNAVAILABLE`、从站零写入。「绝不伪报成功」的安全意图以更强的三态枚举断言保留。**此项不构成生产缺陷，无需移交 ChatGPT。**

## 3. Config Apply：过期前提与真实 fail-closed 边界

失败响应：`success=false, rollback_performed=true`，错误 `"modified file 'cfg/tasks.yaml' is not in Commander config_sources; a legacy Web configuration cannot be pushed as a shared snapshot"`。

拓扑核验：full_stack 中 Web Server 管理 v1 扁平目录（`cfg/tasks.yaml`…），Commander 的 `config_sources` 只含 v2 三路径（`instances/devices.yaml`、`templates/`，见 `tests/support/commander_mtls.py:34-37`）；`ConfigService._changed_files()` 以真实 mTLS disk 快照比对键名，不重叠即在 Push 前拒绝（`src/web_server/application/config/service.py:150-154`），路由层按刻意契约返回 200+success=false+rollback（`v1/config.py:37-46` 注释明示）。旧测试「修改扁平 tasks.yaml 必须 Apply 成功并推动 Commander revision」的前提在 v1/v2 分治下结构性不可满足。

成功路径并未失保：真实 Commander Prepare/Activate 两段事务成功路径由 `tests/system/rpc/test_server_commander_rpc.py::TestConfigSnapshotRoundTrip` 覆盖，REST 侧成功路径由 `tests/web_server/test_config.py::TestReloadSuccess`（假 Commander 同键空间）覆盖。本系统测试改写为真实子进程链路上的 fail-closed 验收：200+success=false+rollback_performed=true、错误指向 config_sources、REST 读模型与磁盘文件节拍保持 0.2、两 Worker active_revision 事务前后完全不变。

## 4. CLI 两族

### 4.1 TCP 探针（`test_tcp_probe_open_closed_timeout`）

旧用例在关闭临时 echo server 后立刻回探同一端口，WSL2 内核端口复用使其非确定性返回 OPEN。改为 bind 一个临时端口但**从不 listen**：端口被测试持有、他进程无法抢占，回环连接必收 RST（`ConnectionRefusedError` → core 探针非 TIMEOUT 失败码 → CLI 映射 CLOSED）。OPEN 分支仍用真实监听 server，两条精确状态断言均保留。连续 5 次复跑 5/5 通过。

### 4.2 systemd（`test_system_real.py`）

本机 `/run/dbus/system_bus_socket` 存在但 wind-hub 环境无 `dbus_next` 模块，生产 `_systemd()` 按 fail-closed 抛 `RuntimeError("systemd requires dbus-next")`（CLI 入口映射 exit 1/FAILURE，与参数错误 exit 2 分离，`main.py:105-107`）。旧守卫只查 socket 不查绑定。整改：

1. 新增 `_systemd_real_dbus_available()` 能力守卫（socket + 绑定同时就位），不满足时两个真实 D-Bus 用例显式 NOT_RUN——这是环境能力判定，不是 Mock；
2. 新增 `test_systemd_without_dbus_binding_fails_closed`：绑定缺失时直接调用真实 `manage()`，断言 RuntimeError 分类（绑定已安装环境自动 NOT_RUN），补上「真实退出码/错误分类」验收中缺失的一环；
3. 全程无 Mock systemd、无 Mock D-Bus；containerd/iSulad 既有 NOT_RUN 模式不变。

## 5. 受影响树与全量对账

### 5.1 定向回归（最终工作树）

| 测试树 | 结果 |
| --- | --- |
| `tests/system/e2e/test_command_via_server.py` | 4 passed |
| `tests/system/reload/test_server_config_apply.py` | 3 passed |
| `tests/system/test_web_fleet_config_e2e.py` | 22 passed |
| `tests/cli/test_local_tools.py` + `test_system_real.py` | 24 passed, 4 skipped（NOT_RUN 原因逐一核验） |
| 字序夹具影响面（collector_rpc、server_startup、web_full_stack、web_fleet、commander_rpc） | 59 passed |

### 5.2 全仓 pytest 与 R3 清单逐条对账

命令：`pytest tests -q`（最终工作树，1587.75s）。统计：**2186 passed, 0 failed, 43 skipped, 0 errors, 10 deselected（2239 收集）**，进程退出码 0。

| R3 清单项（R3 结果） | R4 新鲜运行去向 | 分类 |
| --- | --- | --- |
| `test_web_fleet_config_e2e.py` 21 ERROR（mTLS 三路径缺失） | 同文件 22/22 全部 PASS（原 1 passed + 21 ERROR） | 夹具缺陷，已闭环 |
| `test_command_via_server.py` ×4 FAILED | 4/4 PASS（夹具点表+字序、placement、三态信封契约） | 夹具/测试契约漂移，已闭环 |
| `test_server_config_apply.py` ×1 FAILED | 文件 3/3 PASS（改写为 fail-closed/回滚/零变异验收） | 过期测试前提，已闭环 |
| `test_local_tools.py::test_tcp_probe_open_closed_timeout` FAILED | PASS，连续 5 次复跑稳定 | 测试竞态，已闭环 |
| `test_system_real.py` systemd ×2 FAILED | 2 例显式 NOT_RUN（socket 在但 dbus-next 绑定缺失），另新增 1 例缺绑定 fail-closed 分类用例 PASS | 环境能力缺失，已闭环 |
| R3 其余 2158 passed / 41 skipped | 无回归；skipped 41→43 恰为 systemd 两例迁移，新增用例 +1 收集 | 无回归 |

数量勾稽：R3（2158 passed / 8 failed / 41 skipped / 21 errors）→ R4（2186 / 0 / 43 / 0）。passed +28 = 21 原 ERROR + 6 原 FAILED 转 PASS + 1 新增用例；skipped +2 为 systemd 两例；收集数 +1 即新增用例，无其他测试发现差异。**R3 清单 29 个问题项全部有确定去向，UNKNOWN = 0；本轮无新增失败、无新增错误。**

## 6. 复测方改动清单（仅测试/夹具，无生产改动）

1. `tests/system/test_web_fleet_config_e2e.py`：web_server fixture 注入测试 PKI 与 Commander mTLS 客户端环境变量（+8 行）；
2. `tests/support/v2_config.py`：补 setpoint_power 点表/目录条目；型号模板显式 big_endian 字序；
3. `tests/system/e2e/test_command_via_server.py`：任务 START 建立 placement、三态信封/权威 NOT_FOUND/UNAVAILABLE 契约对齐，模块 docstring 更正；
4. `tests/system/reload/test_server_config_apply.py`：过期成功前提改写为 v1/v2 分治 fail-closed/回滚/零变异验收；
5. `tests/cli/test_local_tools.py`：CLOSED 判定改 bind 不 listen 占坑；
6. `tests/cli/test_system_real.py`：D-Bus 能力守卫 NOT_RUN + 缺绑定 fail-closed 新用例（+1 可执行用例）。

未改架构白名单、未碰冻结模块与 Sink、无 rebase/force push/merge main、无 skip/xfail/noqa/ignore 滥用（NOT_RUN 仅用于真实环境能力缺失，且原因字符串显式可审计）。

## 7. 遗留事项（沿 R3，不阻塞本轮裁决）

| 项 | 性质 | 处置 |
| --- | --- | --- |
| Sink `grpc_sink.py` 无 lease_token 调 UpdateVariables（R1 P1 跨分支阻断） | 跨分支生产缺陷 | 待 Sink 分支改造，本分支无 Sink 改动权 |
| 真实 PLC/ADS/IEC104、持令牌端到端、真实 systemd/containerd/iSulad 守护进程 | NOT_RUN | WSL2 环境不具备，须在合格 CI/真机复核，不得据此宣称完整验收 |
| typings/ai_shared 无作用域 ruff 基线噪声 | UNRELATED | CI 门禁作用域为 `src tests scripts`，不在本轮范围 |

## 8. 最终裁决

**PASS。** R3 遗留的 8 failed + 21 errors 在最终工作树新鲜全量运行中全部闭环：28 项转为确定性 PASS（含 21 个原模块 ERROR），2 项环境能力缺失按显式 NOT_RUN 处理并附真实 fail-closed 分类用例，全仓 0 failed / 0 errors / 2186 passed。

所有整改均限于测试与夹具（6 文件 +128/−51），生产代码零改动，新增生产缺陷数 = 0，无需向 ChatGPT 移交生产缺陷；冻结模块、Sink Service 与架构白名单零触碰，无 skip/xfail/noqa/ignore 滥用，无断言弱化——三态结果信封、从站零写入、独立 Modbus 回读、revision 零变异等安全断言全部保留或加强。限制性仅来自与本分支无关的真实链路/守护进程 NOT_RUN 与 Sink 跨分支 P1 阻断（沿 R3 清单）。

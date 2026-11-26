# DCEMS 控制闭环独立验收报告（2026-11-26）

## 0. 模板与范围声明

- 仓库内未发现《报告模板.md》，本报告未声称符合任何模板。
- 本专项仅验证 wind-hub 的 Modbus → Gateway1 → Commander → Gateway2 → Data Generator 控制执行链路。
- 本专项通过**不等于** DCEMS Server/Web、AGC/AVC/PFR 算法或完整电厂控制闭环生产 READY。

## 1. Git 基线

| 项目 | 值 |
|---|---|
| 分支 | `feat/dcems-gateway-smoke-20261126` |
| 起始 HEAD | `e5d2e7aa435c97044d8c2f7b73b486a221d9d20e` |
| 远端最新 HEAD（fetch 后） | `e5d2e7aa435c97044d8c2f7b73b486a221d9d20e`（与 ChatGPT 交付一致） |
| 测试代码与证据提交 | `cbe86539` |
| 报告提交（最终功能性 HEAD） | `47da7b1f567104023ce43abad8c8e2bbd3a14fb5` |
| HEAD 元数据提交 | 仅更新本表、不改变测试结论 |

未创建新分支、未创建 PR、未合并 main、未 force push / rebase / reset。

## 2. 实际测试环境

| 项目 | 值 |
|---|---|
| 平台 | Linux 6.18.33.2-microsoft-standard-WSL2 |
| Python | 3.14.7（`~/miniconda3/envs/wind-hub`） |
| grpcio | 1.84.0 |
| protobuf | 6.33.6 |
| pymodbus | 3.15.0 |
| nats-py | 2.16.0 |
| NATS Server | 2.10.29（`nats:2.10-alpine`，JetStream，`127.0.0.1:14223`） |
| dc-ems 版本标识 | `release_version=1.1.4-583d5da`（仅记录；本专项未使用 DCEMS Server/Web 镜像） |
| 业务服务 | `industrial_gateway.main`、`commander`、`data_generator.main`，均由正式 `cli.main` 进程管理器启停 |

隔离根：`run/dcems-control/`（独立于 `run/dcems-measurement/`、`run/dcems-smoke/`、生产 Deployment 与既有 Commander 数据库）。
九个 Bearer Token 均为随机生成、仅通过本地临时 Shell 环境文件注入，未提交、未打印、未写入报告；临时文件与 NATS 容器已在验收后删除。

## 3. 静态审查与配置 Loader

### 3.1 审查对象

```text
tests/third_party/dc_ems/
    prepare_control_chain.py
    test_control_chain_config.py
    test_control_chain_live.py
    test_control_chain_timeout_live.py（本轮补充的独立超时轮）
    run_control_chain.sh
    README_control.txt
```

### 3.2 生产 Loader 结果

```text
python -m pytest -q tests/third_party/dc_ems/test_control_chain_config.py
3 passed

ruff check tests/third_party/dc_ems/        All checks passed
ruff format --check tests/third_party/dc_ems/  29 files already formatted
mypy（4 个专项文件）                         Success: no issues found
```

正式 Loader 实际调用：

- `core.configuration.deployment.parse_deployment`
- `core.configuration.local_selection.select_local_service`
- `industrial_gateway.infrastructure.config.resolution.load_gateway`
- `commander.infrastructure.config.service_settings.load_service_settings`
- `data_generator.infrastructure.config.service_settings.load_settings`

### 3.3 配置与授权链结论

| 检查项 | 结果 |
|---|---|
| Deployment 精确版本闭包 | PASS：`dcems-control@1.0.0` 固定 5 个 Service；site/device/scenario 精确版本 |
| Gateway1 / Gateway2 身份与端口隔离 | PASS：Gateway1 gRPC `52174` / Modbus `15431`；Gateway2 gRPC `52173` / 物理 PPC Modbus `15425` |
| `variable_id → target_point_id` | PASS：`agc_enable → agc_enable`，Holding[0]、uint16、count 1 |
| Commander `simulation_targets` | PASS：`PPC_SIM01 → gw-ppc-sim01 @ 127.0.0.1:52173` |
| Gateway2 `control.simulation` | PASS：DG `127.0.0.1:52161`，`scenario_id=e2e-plant-physical` |
| Gateway1 权限 | PASS：仅 `operator`，权限 `read/lifecycle/diagnose`，无 `simulation_execute` |
| Gateway2 执行权限 | PASS：`commander` 身份含 `simulation_execute`；`generator` 身份含 `inject/read` |
| Commander 权限 | PASS：`ems`/`gateway` 含 `control`；`maintenance` 仅 `query/reload/recover` |
| DG 更新权限 | PASS：`gateway` 身份含 `variables_update`；新增 `readonly` 身份仅 `variables_read` |
| 审计依赖 | PASS：Commander 审计流 `COMMAND_AUDIT`、subject `windhub.commander.audit`，实际创建并连接 |
| 事件持久化 | PASS：SQLite 位于隔离根，容量限制 1 GiB；全部事件 `publish_state=PUBLISHED` |
| 配置加载副作用 | PASS：`--prepare` 拒绝覆盖既有根；仅生成本专项目录 |

### 3.4 测试 Fixture 修复（非生产代码）

1. DG `recording.directory` 必须为配置根相对路径；原生成器写入绝对路径导致正式 Loader 拒绝。已改为 `state/generator-records`。
2. 为验证“无更新权限不能调用 DG”，在隔离配置中新增 DG `readonly` 身份（复用 `WIND_HUB_E2E_COMMANDER_MAINTENANCE_TOKEN`，仅 `variables_read`），不新增令牌数量。
3. 修正测试对 CLI 顶层 `status` 的错误假设：Commander `UNKNOWN`/`FAILED` 终态时 CLI 返回码为 3，数据仍完整；测试改为校验 `data.state` 而非仅校验 `status=SUCCESS`。
4. 补充 T03/T04/T05 测试与独立超时轮，未修改 `src/`、正式 Contracts、正式生产配置、`third_party/dc-ems/` 或十机测量链路。

## 4. 真实运行环境准备结果

| 检查项 | 结果 |
|---|---|
| 四个服务 RUNNING | PASS |
| Gateway1 / Gateway2 业务健康 | PASS（HEALTHY） |
| Commander 业务健康 | PASS（HEALTHY，首轮） |
| Data Generator 业务健康 | PASS（HEALTHY；gateway 依赖显示 `no active health probe`） |
| PPC 物理 Scenario 运行 | PASS（`e2e-plant-physical`，generation 已创建） |
| Commander 设备/控制点查询 | PASS：`PPC_SIM01` revision `47a5f3…`；`agc_enable` 可写、Holding[0] |
| Gateway1 Modbus `15431` | PASS（连接并读 Holding[0]） |
| Gateway2 PPC Modbus `15425` | PASS（连接并读 Holding[0]） |
| `COMMAND_AUDIT` 流 | PASS（创建、JetStream 连接、发布成功） |
| 端口/身份/配置激活冲突 | 未发现 |

说明：进程 RUNNING 未被当作业务 READY；上表同时核验业务健康、Modbus 可连接性和 Commander 查询。

## 5. 测试结果总表

| 用例 | 结果 | 关键证据 |
|---|---|---|
| T01 单指令成功 | **PASS** | Modbus ACK + Gateway1 事件 ID + Commander SUCCEEDED/CONFIRMED_SUCCESS + DG APPLIED + Gateway2 回读 1 |
| T02 幂等性 | **PASS** | 同 request_id 同载荷复用同一 command_id；同 request_id 不同值 `ALREADY_EXISTS`；跨 caller 查询 `NOT_FOUND` |
| T03 鉴权 | **PASS** | 错误/缺失 Bearer、无 control、无 simulation_execute、无 variables_update 均被拒绝 |
| T04 失败与 UNKNOWN | **PASS** | Commander/Gateway2/DG 失联与连接超时均不伪报成功、不自动重放；恢复后旧终态保留 |
| T05 恢复与资源清理 | **PASS** | CLI 完整停机/重启；端口和进程释放；SQLite 状态完整；专项资源清理 |

主验收轮：`12 passed`（`tests/third_party/dc_ems/test_control_chain_live.py`）。
独立超时轮：`1 passed`（`tests/third_party/dc_ems/test_control_chain_timeout_live.py`，全新隔离根）。
失败：0。未执行：NATS 审计失联故障注入（见第 10 节）。

## 6. T01 完整因果证据（最终留档轮）

| 环节 | 证据 |
|---|---|
| Modbus FC06 | 地址 0、值 1，响应非错误；Gateway1 Holding[0] 回读 `[1]` |
| Gateway1 写入事件 | `request_id = 3ed0e4fad8624e388607ab7b44bb6c5c`（32 位 hex，由真实 `GetEvents` 读取） |
| Commander request | `request_id` 同上；`state=SUCCEEDED`；`io_settled=true`；`commands` 长度 1 |
| Commander command | `command_id = afaa69b3-377a-4238-8354-c5f065d490c0`；`outcome=CONFIRMED_SUCCESS` |
| DG 执行 | `GetInputExecution`：`request_id=command_id`；`state=APPLIED`；`effective_step=17` |
| Gateway2 独立回读 | `127.0.0.1:15425` Holding[0] = `1` |
| 审计 | `COMMAND_AUDIT` 事件 14/14 `PUBLISHED`，`attempts=0` |

Gateway1 的事件 ID 就是 Commander 的 `request_id`：源码 `industrial_gateway/application/runtime.py` 生成 `event_id` 后写入 observability，并由 `application/forwarding.py` 以 `request_id=event.event_id` 提交；实测一致。

Commander `command_id` 传递到 DG：`commander/infrastructure/adapters/outbound/gateway.py` 以 `request_id=command.command_id` 调用 `ExecuteSimulationCommand`；DG 查询结果与 `command_id` 一致。

## 7. T02 幂等与调用方隔离

- 相同 `request_id`、相同载荷：复用同一 `command_id`，未重复执行。
- 相同 `request_id`、不同值：Commander 返回 `ALREADY_EXISTS`。
- 跨 caller 查询：维护身份（`query` 权限）查询 Gateway1 的 request → `NOT_FOUND`，证明请求结果严格按 `caller_id` 隔离。
- Modbus 写入自动生成的 `event_id` 与 CLI 显式 `--request-id` 属于不同幂等命名空间；测试分别验证。

## 8. T03 鉴权结果

| 通道 | 场景 | 结果 |
|---|---|---|
| Gateway1 gRPC `52174` | 错误 Bearer | UNAUTHENTICATED / PERMISSION_DENIED |
| Gateway1 gRPC `52174` | 缺失 Bearer | UNAUTHENTICATED |
| Commander `52152` | 错误/缺失 Bearer SubmitCommand | UNAUTHENTICATED |
| Commander `52152` | 维护身份（无 `control`）SubmitCommand | PERMISSION_DENIED |
| Commander `52152` | 维护身份 GetDevice（正对照） | SUCCESS |
| Gateway2 `52173` | operator 身份（无 `simulation_execute`）执行/查询 | PERMISSION_DENIED |
| Gateway2 `52173` | 缺失 Bearer | UNAUTHENTICATED |
| DG `52161` | readonly 身份（仅 `variables_read`）UpdateVariables | PERMISSION_DENIED |
| DG `52161` | 缺失/错误 Bearer | UNAUTHENTICATED |

Modbus TCP 写入本身不带 Bearer；T01 在无令牌的 Modbus 会话中完成写入，证明测试未把 Modbus ACK 当作身份认证或执行确认。

## 9. T04 失败、超时与 UNKNOWN

### 9.1 Commander 不可达

- 停 Commander 后通过 Modbus 写入，Gateway1 发出写入事件 `56a21d04b55b4004a696a82cefcce82c`。
- Gateway1 转发事件：`forward REJECTED: 56a21d04b55b4004a696a82cefcce82c`。
- Commander 恢复后按该 `request_id` 查询：`NOT_FOUND`；DG 无 `APPLIED`。
- 结论：不伪报成功，无旁路执行。

### 9.2 Gateway2 不可达

- 停 Gateway2 后通过 Commander 提交：实际 `accepted=true`，最终 `state=UNKNOWN`、`outcome=UNKNOWN`、`error_code=CONNECT_TIMEOUT`，`io_settled=false`。
- DG 查询同一 `command_id`：`UNKNOWN`、`effective_step=0`。
- Gateway2 恢复并等待后：Commander 终态与命令数不变（仅 1 条），DG 仍无 `APPLIED`，证明未自动重放。

### 9.3 Gateway2 连接超时（独立新根）

- Gateway2 停止，`52173` 由故障注入监听器接受连接但不响应。
- Commander 结果：`UNKNOWN`、`CONNECT_TIMEOUT`、`io_settled=true`；DG `UNKNOWN`；无 `APPLIED`。
- 恢复 Gateway2 后旧请求终态不变、命令数仍为 1。

### 9.4 DG 不可达

- 停止 Scenario 与 DG 服务后提交：Commander 最终 `UNKNOWN`、`outcome=UNKNOWN`、`error_code=WRITE_UNKNOWN`。
- DG 恢复后再次查询：仍 `UNKNOWN`、`effective_step=0`、无 `APPLIED`，未自动补执行。

### 9.5 查询失败与恢复

- 停止 Commander 后 CLI 查询失败（`status=UNKNOWN`，`data=null`，连接拒绝）。
- Commander 恢复后再次查询既有成功命令：仍 `SUCCEEDED/CONFIRMED_SUCCESS`，证明持久化证据可追溯。

### 9.6 UNKNOWN 的后续保护

- 重启 Commander 后，因数据库存在 PPC_SIM01 的 UNKNOWN 命令，服务健康为 `DEGRADED`，并对该设备保留恢复阻断；成功命令与 UNKNOWN 命令状态均保持不变。
- 这是设计内的 fail-closed 行为，不是伪报成功；外部恢复需要独立取证，本专项未伪造 recovery evidence。

## 10. T05 恢复与资源清理

| 检查 | 结果 |
|---|---|
| `run_control_chain.sh --down` | PASS：四个服务 `STOPPED` |
| 端口释放 | PASS：`15431/15425/52152/52161/52173/52174` 全部关闭 |
| 僵尸进程 | 未发现 |
| CLI 重启 | PASS：四个服务 RUNNING；Gateway1/Gateway2/DG HEALTHY；Commander DEGRADED（保留 UNKNOWN 恢复阻断） |
| Commander 数据库 | PASS：成功命令仍 `SUCCEEDED/CONFIRMED_SUCCESS`；UNKNOWN/FAILED 终态保留；事件全部 `PUBLISHED` |
| 十机测量链路 | 未触碰 `run/dcems-measurement/` 及其他既有运行目录 |
| 专项资源清理 | PASS：`run/dcems-control/` 删除、NATS 容器删除、临时令牌环境文件删除、`14223` 释放 |

## 11. 特别风险核查

| 风险 | 结论 |
|---|---|
| 控制配置根历史文件歧义 | 生成器删除全部旧 Service/Deployment；Deployment 精确固定版本。复制保留的未引用 Instance（collector_tasks/deliveries/sinks）未被本 Deployment 选择，正式 Loader 未产生歧义。 |
| Commander 查询 caller 隔离 | PASS：仓储查询以 `caller_id` 过滤；跨身份实测 `NOT_FOUND`。 |
| Gateway1 event_id = request_id | PASS：源码与实测均一致。 |
| command_id 传递到 DG | PASS：Gateway2 以 `command_id` 作为 DG `request_id`；DG 查询匹配。 |
| 异步转发竞态 | 未观察到重复提交；事件先记录后入队；Commander 失联时记录 `REJECTED`，不同 request 不共享身份。 |
| Gateway2 Modbus 回读 | 作为辅助证据有效（Holding[0]=1）；未取代 DG `APPLIED`。 |
| DG APPLIED 是否真正改变模型输入 | DG 以 `effective_step>0` 与 `APPLIED` 证明输入已应用到模型；电厂物理响应未验证。 |
| 脚本启动失败/重复运行/日志/释放 | `--prepare` 拒绝覆盖；启动失败有 CLI 报错与日志；停机释放端口；本轮已实际验证。 |
| 测试是否伪造/跳过关键步骤 | 未使用 Mock 替代最终验收；T01 断言跨四服务因果证据；T04 断言非成功终态与无重放。 |
| NATS 审计失联行为 | **NOT_RUN**：本专项仅验证审计流正常连接与 `PUBLISHED`，未注入 NATS 中断。 |
| Gateway2 Modbus 旁路风险 | 执行 Gateway2 的 `control.enabled=true` 按 Schema 必须带 Commander target，因此其 loopback Modbus 端口在拓扑上也是控制入口；本专项未通过该端口发起控制，仅用于回读。该行为属于现有 Schema/配置设计边界，非本轮发现的代码缺陷。 |

## 12. 新增问题与严重程度

本专项未发现需要修改 `src/`、正式 Contracts 或 `third_party/dc-ems/` 的生产缺陷：

- P0：0
- P1：0
- P2：0（生产代码）

非生产问题（已在允许范围内修复）：

| 编号 | 类别 | 描述 | 处理 |
|---|---|---|---|
| F-01 | 测试 Fixture | DG `recording.directory` 写绝对路径，正式 Loader 拒绝 | 改为控制根相对 `state/generator-records` |
| F-02 | 测试设计 | 缺少 DG 只读身份，无法验证 `variables_update` 权限拒绝 | 增加 `readonly` 调用方（仅 `variables_read`） |
| F-03 | 测试断言 | 把 CLI 顶层 `status` 当作命令终态，UNKNOWN 时误判失败 | 改为读取 `data.state` / 接受非 SUCCESS 的已落库终态 |
| F-04 | 测试覆盖 | 缺少 T03/T04/T05 与连接超时工况 | 新增用例与独立超时轮 |

## 13. 修改文件与提交

| 文件 | 说明 |
|---|---|
| `tests/third_party/dc_ems/prepare_control_chain.py` | 修复 DG recording 路径；新增 DG 只读身份；格式化 |
| `tests/third_party/dc_ems/test_control_chain_config.py` | 增加授权链闭合断言 |
| `tests/third_party/dc_ems/test_control_chain_live.py` | 扩展 T02/T03/T04/T05；修复终态判定 |
| `tests/third_party/dc_ems/test_control_chain_timeout_live.py` | 新增独立超时故障注入轮 |
| `tests/third_party/dc_ems/README_control.txt` | 记录隔离轮次约束与只读身份用途 |
| `docs/audit/dcems_control_chain_independent_retest_20261126_evidence/` | 证据目录（无明文令牌） |

提交：

- 起始：`e5d2e7aa`
- 测试代码与证据：`cbe86539`
- 报告：本文件提交

## 14. 尚未验证的边界

1. DCEMS Server/Web 页面与接口。
2. AGC/AVC/PFR 控制算法与完整电厂控制闭环。
3. 电厂真实物理响应（本轮仅证明 DG 模型输入 `APPLIED`）。
4. NATS/JetStream 失联、重连与审计积压行为。
5. Commander 外部隔离/恢复取证流程（本专项未构造合法 recovery attestation）。
6. Gateway2 loopback Modbus 作为控制入口的完整安全边界。
7. 长时间稳定性、吞吐与并发压力。
8. 非 PPC 设备及批量控制。

## 15. 最终裁决

**`READY_FOR_NEXT_STAGE`**

依据：

- 静态配置经正式 Loader 通过；`ruff`/`format`/`mypy` 全绿。
- T01–T05 全部 PASS；主轮 12/12、独立超时轮 1/1。
- 关键控制链路具有真实跨服务执行证据：Gateway1 事件 ID = Commander `request_id`，Commander `command_id` = DG `request_id`，DG `APPLIED`、`effective_step>0`，Gateway2 回读为 1。
- 失败注入未出现伪报成功、自动重放或旁路执行。
- 无开放 P0/P1 生产缺陷。

限制：

- 本裁决只表示 wind-hub 的 Modbus 控制执行链路通过，不表示 DCEMS Server/Web、AGC/AVC/PFR 算法或完整电厂控制闭环生产 READY。
- NATS 审计失联行为未注入验证。

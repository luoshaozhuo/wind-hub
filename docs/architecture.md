# 架构设计

本文档描述 wind-hub 的运行时架构，与当前代码保持一致。配置字段详见
[config.md](config.md)，协议/Sink 扩展点详见 [spi.md](spi.md)。

## 1. 总体结构

### 1.1 术语定义

| 术语 | 定义 |
|---|---|
| Domain | 核心业务模型与业务规则（model / acquisition / routing / command / processing / domain 扩展点端口），不含应用入口与基础设施语义 |
| Use Case | application 层完成完整应用业务流程的编排类（`application/usecase/`），接收 inbound adapter 调用，编排 domain 对象、Runtime 与 outbound port |
| Inbound Adapter | CLI / Web API / IEC104 slave——参数解析、协议转换、调用 Use Case、映射输出 |
| Inbound Port | 不默认存在：inbound adapter 直接依赖具体 Use Case；只有存在多实现或替换边界时才允许保留，且必须命名为 `xxxPort` |
| Outbound Port | application / domain 所需外部能力的接口：`application/port/`（SinkPort、SchedulerPort）与 `domain/port/outbound.py`（ProtocolPort、ProcessorPort——被 domain 服务直接消费，故留在 domain） |
| Outbound Adapter | outbound port 的实现：Modbus / ADS / IEC104 / Kafka / Postgres / FileSink / APScheduler |
| Runtime | 运行期组件生命周期与状态编排核心（`application/runtime/`），不是普通 Use Case |
| Composition Root | `assembly.py`——唯一知道双方具体类型并负责装配的模块 |
| AppContext | 进程级共享 application context / 依赖容器（`application/app_context.py`），CLI 与 Web API 共享同一实例 |

注意：**UseCase 不是 Port 的统一后缀**——`xxxUseCase` 是具体编排类；
"Application Service" 术语不再作为代码主命名体系（不再存在
`xxxService` 类或 `application/*_service.py` 模块）。

### 1.2 入口与装配

入口与装配：

```text
main.py → assembly.py（组合根）→ Runtime
```

`assembly.py` 是唯一组合根：加载配置、创建协议驱动 / Sink / 处理器、
构造 `AcquisitionEngine` 与 `Dispatcher`，最后组装 `Runtime` 并注入全部
回调（指标、超时等）。`Runtime` 是应用层编排核心，向下分三类协作对象：

```text
Runtime
├── SchedulerPort ← APSchedulerAdapter   （什么时候执行——时间调度）
├── AcquisitionEngine                    （执行一次采集）
│     read → Pipeline → Router → DeliveryDispatcher → SinkDispatchPort
└── Dispatcher                           （写命令下发）
```

分层与依赖规则（由 import-linter 强制，见 `pyproject.toml`）：

| 层 | 内容 | 依赖约束 |
|---|---|---|
| `domain` | 模型、扩展点端口（ProtocolPort / ProcessorPort）、AcquisitionEngine、Router、DeliveryDispatcher、Dispatcher、Pipeline | 不得依赖 application / adapter / infra；不得 import APScheduler、FastAPI、pyads、pymodbus、prometheus_client |
| `application` | Use Case（command / config / job / query / route_query）、Runtime、DeviceRuntimeState、AcquisitionRuntimeState、应用级端口（SinkPort / SchedulerPort）、AppContext | 不得依赖 adapter；指标等经回调/端口注入 |
| `adapter` | inbound（webapi/cli）、outbound（protocol/sink/processor） | 可依赖 domain / infra |
| `infra` | metrics、registry、processor_registry | 被各层经组合根接线 |

## 2. Runtime 与三个状态维度

`Runtime`（`application/runtime/runtime.py`）负责组件生命周期与状态编排：
连接设备（best-effort，单设备失败不影响整体启动）、注册调度 Job、
实现引擎的采集前/后钩子、持有 Sink 队列与消费者、执行热重载。

运行期有**三个互相独立的状态维度**，刻意不合并：

1. **调度 Job 状态**（`SchedulerPort`）：Job 的注册/暂停/下次触发时间。
   纯粹的时间调度维度——Job 被 pause 不等于设备故障，设备断线也不删除
   Job。`APSchedulerAdapter` 只翻译调度原语，不持有任何业务状态。
2. **设备连接状态**（`DeviceRuntimeState`，每台设备一份）：`connected`、
   `consecutive_failures`、`next_retry_at`（重连节流点）、`last_error`。
   回答「设备通不通」。
3. **采集执行状态**（`AcquisitionRuntimeState`，每个 `(device, group)`
   一份，即 Job `poll:{device}:{group}`）：`running`、`last_started_at`、
   `last_finished_at`、`last_success_at`、`last_duration`、
   `consecutive_failures`、`last_error`。回答「这个采集 Job 最近跑得怎样」。

引擎通过两个端口上报事件，Runtime 持有状态与指标——与 `DeviceStatePort`
同一 DI 模式：

- `DeviceStatePort`：`ensure_connected` / `report_read_success` /
  `report_read_failure`（读前问一句、读后如实上报）；
- `AcquisitionStatePort`：`report_collect_started` /
  `report_collect_success(partial=...)` / `report_collect_failure(error)`。

一次 `collect` 的生命周期：`begin`（running=True）→ 成功或失败结束
→ `running` 归位并记录 `last_finished_at` / `last_duration`。无论哪条
异常路径（读失败、超时、管线异常），`running` 都必须归位——引擎侧以
try/except 保证，失败只上报一次（不双报）。

所有运行状态的时间戳使用注入时钟（默认 `time.monotonic`），只做内部
时长/节流计算；不混用 `datetime.now` / `time.time`。

## 3. 两层超时模型

超时分两层，职责不同、**同时存在**：

- **驱动内部超时**（下层）：各协议驱动自己的 socket/协议超时
  （如 pyads `set_timeout`、pymodbus `timeout`），由驱动配置管理，
  本层不干预。
- **应用层外层超时**（上层）：`asyncio.wait_for` 兜底一次业务调用允许
  占用的最大时间，配置集中在 `system.yaml` 的 `scheduler` 段：

| 配置 | 作用点 | 语义 |
|---|---|---|
| `connect_timeout`（默认 10s） | Runtime 的 `start` / `ensure_connected` / `add_device` / `rebuild_device` 中的 `connect()` | 单次连接尝试上限 |
| `read_timeout`（默认 5s） | `AcquisitionEngine.collect` 对 `ProtocolPort.read` 的外层 `wait_for` | 一次批量读上限；超时按连接级失败处理（标记断线、走重连节流） |
| `write_timeout`（默认 5s） | `Dispatcher` 对 `ProtocolPort.write` 的外层 `wait_for` | 命令未自带超时时（`Command.timeout <= 0`）的默认值 |

写超时优先级：`Command.timeout > 0` 时用命令自带值，否则用系统
`write_timeout`。不存在硬编码超时。

**错误语义必须定位到阶段**，不允许裸 "timeout"：

- `connect timeout: device=d1 timeout=10.0s — skipped`
- `read timeout: device=d1 group=fast timeout=5.0s`
- `write timeout: device=d1 point=p001 timeout=3.0s`

未配置外层读超时、驱动自身抛 `TimeoutError` 时，沿用驱动消息并标记为
driver-level，同样按连接级失败分类。

## 4. 断线、重连与采集的协同

`DeviceRuntimeState` 实现重连节流（无 jitter）：

```text
T_k = min(30 s, 1 s · 2^k)   ——  1, 2, 4, 8, 16, 30, 30 …
```

协同规则：

- `ensure_connected` 已连接 → 立即放行；断线且未到 `next_retry_at` →
  返回 `False`；断线且窗口已到 → 尝试一次 `connect()`（驱动 connect
  幂等，与其内部重连监控安全共存）。
- `ensure_connected=False` 时本次采集判定 **FAILED**（不是 SKIPPED）：
  不重发 `read`，`last_error` 记 `device disconnected (reconnect
  backoff)`，周期 Job 保留，下周期继续。
- 读失败按故障分类处理：连接级（`TimeoutError` /
  `ConnectionRefusedError` / `OSError`，含 `__cause__` 链）标记断线并
  进入重连路径；协议/编程级只记 `last_error`，连接状态不动。
- 驱动自带的后台重连监控（ADS/Modbus/IEC104 均有）与 Runtime 的
  ensure 路径是两条路径：前者不触发 `device_reconnect_total` 指标，
  后者触发。

一次采集 Job 失败**不会**翻转 `Runtime.running`；设备启动即失败也
不影响整体启动（best-effort）。

## 5. 批量读部分失败语义

`ProtocolPort.read` 返回 `list[PointValue]`，两种失败严格区分：

- **连接/会话级失败**（TCP 断开、请求发不出、响应整体不可解析、会话
  失效）→ 抛 `ProtocolError`，整批失败，走断线/重连路径。
- **单点失败** → 该点返回 `PointValue(value=None, quality=Quality.BAD)`，
  批次数量与顺序不变，不影响其它点。

各协议落地：

- **ADS**：sum 模式（`read_list_by_name`）整体失败抛 `ProtocolError`，
  响应中缺失的符号逐点 BAD；sequential 模式仅 `pyads.ADSError` 且
  `err_code == 1808`（符号不存在）降级为单点 BAD，其余错误无法与连接
  级故障可靠区分，保持上抛——不伪造成功。
- **Modbus**：整组请求失败（异常响应/传输错误）抛 `ProtocolError`；
  组内单点 decode 失败（响应偏短/类型不符）该点 BAD，同组其它点正常。
- **IEC104**：读来自会话点值缓存，未知/未缓存 IOA 逐点 BAD；会话无效
  抛 `ProtocolError`。

管线对 BAD 天然安全（透传语义）：`quality_check` 非数值不校验、
`unit_convert` 不重标、`deadband` 非数值不参与比较也不污染基线。
BAD 批次照常进入管线与派发——数据质量信息应流向 sink。

## 6. 采集结果判定口径

一次 `collect` 的三档判定（`AcquisitionStatePort` 注释同样记录）：

| 判定 | 条件 | 后果 |
|---|---|---|
| SUCCESS | 无异常且全部点有效 | `consecutive_failures` 清零 |
| PARTIAL | GOOD/BAD 混合（至少一个有效） | 计为成功：清零失败计数，`last_success_at` 更新；另计 `acquisition_partial_total` |
| FAILED | 读抛异常 / 读超时 / 断线跳过 / 空批或全 BAD（无任何有效结果） | `consecutive_failures += 1`，记 `last_error` |

## 7. 路由与投递策略

`Router` 按路由表（规则 + 点覆盖）把点值映射到目标 sink 集合；
`RouteTarget` 携带目标级 `DeliveryPolicy`（`always` / `interval` /
`every_n` / `on_change`）。`DeliveryDispatcher` 在 Router 之后按目标
维度过滤：Router 决定「发到哪些 sink」，DeliveryDispatcher 决定
「这批是否投递」。点表快照语义：热重载整体替换 Router/Pipeline/
DeliveryDispatcher 实例，采集循环总是读当前实例。

## 8. 可观测性

指标框架沿用 prometheus_client 单例（`infra/metrics.py`），不引入新
框架。domain/application 不 import 该模块：采集计数经组合根注入的
回调（`on_points_collected` / `on_points_bad`），Runtime 事件经
`RuntimeMetricsPort` 端口（Prometheus 实现 `PrometheusRuntimeMetrics`
为结构化实现，避免 infra→application 依赖）累加；`/metrics` 拉取时
用 Runtime 快照覆盖 gauge。

| 指标 | 类型 | 标签 |
|---|---|---|
| `wind_hub_device_connected` | Gauge | `device_id`, `protocol` |
| `wind_hub_device_connect_failures_total` | Counter | `device_id`, `protocol` |
| `wind_hub_device_reconnect_total` | Counter | `device_id`, `protocol`（仅 Runtime ensure 路径） |
| `wind_hub_acquisition_runs_total` | Counter | `device_id`, `group` |
| `wind_hub_acquisition_failures_total` | Counter | `device_id`, `group` |
| `wind_hub_acquisition_partial_total` | Counter | `device_id`, `group` |
| `wind_hub_acquisition_duration_seconds` | Histogram（默认 bucket） | `device_id`, `group` |
| `wind_hub_sink_queue_depth` | Gauge | `sink_name`（队列归 Runtime，拉取时从 Runtime 读） |
| `wind_hub_sink_write_failures_total` | Counter | `sink_name` |
| `wind_hub_points_bad_total` | Counter | 无（协议采集 BAD 点 ≠ 背压丢弃 `points_dropped`） |

标签基数受控：只用 `device_id` / `protocol` / `group` / `sink_name`
等配置值；禁止 `error_message`、`point_id` 等高基数字段作标签。
热重载删除的设备/sink，其 gauge 标签序列在下一次拉取时移除
（Counter 序列不删除）。

健康与状态查询（`QueryUseCase.status()`）分层返回：
`running` / 设备计数与连通数 / sink 计数与健康数 / 采集计数 /
`acquisitions`（各采集 Job 的 `AcquisitionInfo`：running、
consecutive_failures、last_error、last_duration）。

## 9. 热重载

唯一入口：`ConfigUseCase.reload()` → 加载校验配置并 diff →
`Runtime.reconfigure(new_config, diff)`。状态处理规则：

- **设备删除** → 注销其全部 Job，删除 `DeviceRuntimeState` 与全部
  `AcquisitionRuntimeState`；
- **设备新增** → 连接、注册 Job，同时建立两类状态（Job 注册即建
  采集状态，首次 collect 前 status 即可见）；
- **group 删除** → 注销对应 Job 并删除其采集状态；
- **group 新增** → 注册新 Job 并建立新采集状态；
- **interval 变化** → Job 原地替换（`replace_existing`），采集状态
  **保留**（历史计数不清零）；
- **点表变化** → 重注入映射/重建 Router 与管线，不触碰采集状态；
- **连接参数变化** → 走 `rebuild_device`（关旧连接、工厂建新驱动），
  状态随删除/新建路径重置。

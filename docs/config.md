# 配置文档

wind-hub 的配置由四个 YAML 文件组成（仓库 `configs/` 下有带详细注释的
完整样例，加载与校验见 `config/loader.py` 与 `config/schema.py`）：

| 文件 | 内容 | schema |
|---|---|---|
| `system.yaml` | 引擎调度、管线、Sink、接口 | `SystemConfig` |
| `devices.yaml` | 设备列表与协议参数 | `DevicesConfig` |
| `points.yaml` | 命名点表集合（设备无关） | `PointTablesConfig` |
| `routing.yaml` | 路由规则与投递策略 | `RoutingConfig` |

所有 schema 均 `extra="forbid"`：未声明的字段在加载阶段直接报错。
热重载唯一入口是 `ConfigService.reload()`（重新加载四个文件 → diff →
`Runtime.reconfigure`），状态处理规则见
[architecture.md §9](architecture.md)。

## system.yaml

### scheduler — 调度与超时

```yaml
scheduler:
  default_interval: 1.0        # 设备未配置 polling group 时的默认采集间隔（秒）
  max_concurrent_devices: 32   # 最大并发轮询设备数
  queue_maxsize: 1000          # 每个 Sink 内部队列容量（背压阈值）
  backpressure_policy: drop_old  # drop_old | drop_new | block
  shutdown_timeout: 30.0       # 优雅停机等待在途操作完成的上限（秒）
  connect_timeout: 10.0        # 单次设备连接尝试上限（秒）
  read_timeout: 5.0            # 单次批量读的应用层外层超时（秒）
  write_timeout: 5.0           # 单次写入默认超时（秒）
```

三个超时都是**应用层外层兜底**（`asyncio.wait_for`），与协议驱动内部
的底层 socket/协议超时并存、职责不同（两层模型见
[architecture.md §3](architecture.md)）：

- `connect_timeout` — Runtime 在启动、重连节流窗口、热增/重建设备时
  包裹 `connect()`；
- `read_timeout` — `AcquisitionEngine.collect` 包裹一次
  `ProtocolPort.read`；读超时按连接级失败处理（标记断线、走重连节流）；
- `write_timeout` — `Command.timeout <= 0`（命令未自带超时）时
  Dispatcher 使用的默认写超时；`Command.timeout > 0` 时命令值优先。

背压策略（Sink 队列满时）：`drop_old` 丢最旧数据腾位（默认）、
`drop_new` 丢弃新批次、`block` 阻塞采集直到有空间。

### pipeline — 处理链

```yaml
pipeline:
  processors: [quality_check, unit_convert, deadband]
```

按声明顺序执行（推荐：先校验、再换算、再过滤）。处理器参数
（`min_value` / `max_value` / `scale` / `offset` / `deadband`）在点位
上声明，单位语义见 `configs/points.yaml` 头部注释。管线对
`Quality.BAD` 点透传安全（见 architecture.md §5）。

### sinks — Sink 定义

每个 sink 有 `name`（路由规则按名引用）、`type`（`kafka` / `db` /
`file`）、`enabled`、`params`（类型相关，见样例）。Sink 队列与消费者
归 Runtime 管理；`enabled: false` 的 sink 不创建。

### interfaces — 对外接口

`api`（HTTP/Web API，`host`/`port`）与 `cli` 的开关。

## devices.yaml

每台设备：`device_id`、`protocol`（`modbus` / `ads` / `iec104`）、
`point_table`（绑定 points.yaml 中的表名）、`endpoint`（`host` /
`port` / 协议相关 `extensions`）、`polling`、`enabled`，以及：

- `mode: poll | subscribe` — 周期轮询或订阅推送（ADS 支持订阅）；
- `read_mode: sum | sequential` — ADS 批量读策略：sum 为单条 Sum 命令
  按 Symbol 批量读，sequential 为逐点读（并发受限）。

`polling` 按 group 定义采集周期；点表中每个点的 `group` 必须被
polling 覆盖。一个 `(device, group)` 对应一个调度 Job
（`poll:{device_id}:{group}`）——不存在逐点 Job。

## points.yaml

命名点表集合：表是设备无关的完整点集定义，设备经 `point_table` 绑定，
多台同类型设备共享一份。点位字段：`point_id`（系统内稳定 ID）、
`name`（业务名）、`group`（采集分组）、`address`（协议寻址，各协议
字段见样例头部注释）、`data_type`、处理器参数（`min_value` /
`max_value` / `scale` / `offset` / `deadband`）、`unit` /
`description`，以及可选的点位级 `sinks` 覆盖（优先级高于一切路由
规则，始终按 always 投递）。

## routing.yaml

`unmatched_policy`（`drop` 默认 / `error` 启动校验）+ `rules` 列表。
每条规则：`name`、`match_device` / `match_point_prefix`（null 匹配
全部）、`priority`（高者优先）、`targets`。

每个 target：`sink` + 可选 `delivery` 投递策略（属于 (规则, sink)
二元组，不同 sink 节拍独立）：

| type | 语义 | 参数 |
|---|---|---|
| `always` | 缺省；每批都投递 | — |
| `interval` | 距上次投递不足 N 秒的批次整批抑制 | `interval`（秒） |
| `every_n` | 首批投递，之后每 n 批投递一次 | `n` |
| `on_change` | 按 (sink, 设备, 点) 记忆上次投递值，值变化才投递 | — |

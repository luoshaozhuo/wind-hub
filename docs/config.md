# 配置文档

wind-hub 的配置由四个 YAML 文件组成（仓库 `configs/` 下有带详细注释的
完整样例，加载与校验见 `config/loader.py` 与 `config/schema.py`）：

| 文件 | 内容 | schema |
|---|---|---|
| `system.yaml` | 运行时参数、管线、Sink、接口 | `SystemConfig` |
| `devices.yaml` | 设备列表与协议参数 | `DevicesConfig` |
| `points.yaml` | 命名点表集合（设备无关） | `PointTablesConfig` |
| `tasks.yaml` | 采集任务（周期采集的唯一来源） | `TasksConfig` |

所有 schema 均 `extra="forbid"`：未声明的字段在加载阶段直接报错。
热重载唯一入口是 `ConfigUseCase.reload()`（重新加载四个文件 → diff →
`Runtime.reconfigure`），状态处理规则见
[architecture.md §9](architecture.md)。

## system.yaml

### runtime — 队列、背压与超时

```yaml
runtime:
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
`drop_new` 丢弃新批次、`block` 阻塞采集任务直到有空间。

采集周期**不在** system.yaml 定义——每个 Task 自带必填的 `interval`
（见下文 tasks.yaml），不存在全局默认采集间隔。

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

每个 sink 有 `name`（Task 的 `targets` 按名引用）、`type`（`kafka` /
`db` / `file`）、`enabled`、`params`（类型相关，见样例）。Sink 队列与
消费者归 Runtime 管理；`enabled: false` 的 sink 不创建。

### interfaces — 对外接口

`api`（HTTP/Web API，`host`/`port`）与 `cli` 的开关。

## devices.yaml

每台设备：`device_id`、`protocol`（`modbus` / `ads` / `iec104`）、
`point_table`（绑定 points.yaml 中的表名）、`endpoint`（`host` /
`port` / 协议相关 `extensions`）、`device_group`（业务分组，Task 按它
成组展开）、`enabled`，以及 ADS 的：

- `read_mode: sum | sequential` — ADS 批量读策略：sum 为单条 Sum 命令
  按 Symbol 批量读（参与周期采集）；sequential 为逐点读，只允许
  CLI/WebAPI 单次读取与诊断——任何 Task 引用 sequential 设备都是配置
  错误（加载期报错）。

设备**不再**定义采集周期或采集分组——周期采集完全由 tasks.yaml 的
Task 声明。

## points.yaml

命名点表集合：表是设备无关的完整点集定义，设备经 `point_table` 绑定，
多台同类型设备共享一份。点位字段：`point_id`（系统内稳定 ID）、
`variable_name`（业务名）、`point_groups`（采集分组，**多值**——一个
点可同时属于多个组；Task 按 `task.point_group ∈ point.point_groups`
选点）、`address`（协议寻址，各协议字段见样例头部注释）、`data_type`、
处理器参数（`min_value` / `max_value` / `scale` / `offset` /
`deadband`）、`unit` / `description`。

点位**不再**声明输出 sink——输出去向完全由 Task 的 `targets` 决定。
点表支持单继承（`extends` / `remove_points` / 点位补丁），继承展开后
统一校验（`point_groups` 在合并结果上同样要求非空且不重复）。

## tasks.yaml

`tasks` 列表，每个 Task 在运行时展开为一组 Task Instance
（`{task_id}:{device_id}`）：

```yaml
tasks:
  - task_id: turbine-fast        # 唯一 ID
    device_group: turbine_ads    # device / device_group 二选一（XOR）
    point_group: fast            # 单值；选择 point_groups 含 fast 的点
    interval: 1.0                # 必填，> 0；采集循环 = collect → sleep(interval)
    targets:                     # 输出去向（≥1，引用 system.yaml 的 sink 名）
      - sink: kafka_main
      - sink: file_archive
    enabled: true                # false 则不展开实例、不可启动
```

- `device` — 只采集这一台设备（设备须存在且启用才展开实例）；
- `device_group` — 展开为该分组下全部**启用**设备，每台一个实例；
  设备增删或分组变化在热重载时自动增删实例，不重启 Runtime；
- `targets` 中同一 sink 重复出现是配置错误；
- 加载期跨文件校验：target sink 存在；device 存在；device_group 至少
  匹配一台设备；`point_group` 必须存在于每个匹配到的启用设备的点表
  （报错时列出缺失设备）；引用 ADS `read_mode: sequential` 设备报错。

实例注册为 `STOPPED`，需经 CLI（`wind-hub tasks start`）或 Web API
（`POST /tasks/instances/{id}/start`）显式启动。

## ports.yaml（probe 端口扫描）

`wind-hub probe ports` 的扫描策略（不参与采集链路）：

```yaml
mapping:        # 端口 → 服务名；未指定 --ports 时扫描 mapping 的全部端口
  502: modbus
  2404: iec104
timeout: 1.0    # 单端口默认超时（秒）
concurrency: 128  # 默认并发探测数
```

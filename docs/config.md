# 配置文档

wind-hub 的一个配置目录是**完全独立、自包含**的完整配置集，可直接作为
`--config` 参数加载（仓库 `configs/` 下的 `template` / `example_modbus` /
`example_ads` 都是带详细注释的完整样例；加载与校验见
`config/loader.py` 与 `config/schema.py`）：

| 文件 | 内容 | schema |
|---|---|---|
| `system.yaml` | 运行时参数、Sink、接口、现场身份、ADS 本机配置 | `SystemConfig` |
| `units.yaml` | 单位定义集（点 `unit` 引用的 unit ID） | `UnitsConfig` |
| `device_models.yaml` | 设备类型 + 设备型号（协议、点表绑定、连接默认值） | `DeviceModelsConfig` |
| `points.yaml` | 命名点表集合（设备无关，含 protocol） | `PointTablesConfig` |
| `devices.yaml` | 现场设备实例（引用型号，只写连接差异） | `DeviceInstancesConfig` |
| `tasks.yaml` | 采集任务（周期采集的唯一来源） | `TasksConfig` |
| `reporting.yaml` | IEC104 从站代理（可选） | `ReportingConfig` |

所有 schema 均 `extra="forbid"`：未声明的字段在加载阶段直接报错。
热重载唯一入口是 `ConfigUseCase.reload()`（重新加载同一配置目录 → diff →
`Runtime.reconfigure`），状态处理规则见
[architecture.md §9](architecture.md)。

`reporting.yaml` 缺失、或 `reporting` 为空列表，均表示不启用 IEC104
从站代理（`Config.reporting` 为 `None` 或空映射，Runtime 不启动从站服务）。

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

采集周期**不在** system.yaml 定义——每个 Task 自带 `interval`
（见下文 tasks.yaml），不存在全局默认采集间隔。

### site — 现场身份

`site_id` / `name`：一个 wind-hub 运行实例只对应一个现场。

### ads — 进程级 ADS 本机配置

`local_ams_net_id` / `local_ip` / `route_repair`：本机作为 AMS 路由器的
身份与 route 自动修复参数；不使用 ADS 设备时整段省略即可。

### sinks — Sink 定义

每个 sink 有 `name`（Task 的 `targets` 按名引用）、`type`（`kafka` /
`db` / `file`）、`enabled`、`params`（类型相关，见样例）。Sink 队列与
消费者归 Runtime 管理；`enabled: false` 的 sink 不创建。

### interfaces — 对外接口

`api`（HTTP/Web API，`host`/`port`）与 `cli` 的开关。

## units.yaml

单位定义集——点 `unit` 字段的取值命名空间与展示元数据：

```yaml
units:
  none:                 # 无量纲（point.unit 的默认值）
    symbol: ""
    name: Dimensionless
  kilowatt:
    symbol: kW          # 展示层经 unit ID 取显示符号
    name: Kilowatt
```

`point.unit` 存储 **unit ID**（`units` 的键，如 `kilowatt`），不是显示
符号（`kW`）；引用不存在的 unit ID 在加载期报错（点表继承展开后统一
校验）。`units` 必须包含 `none`。

## device_models.yaml

设备类型 / 设备型号（三层抽象的上两层）：

- `device_types` — 业务分类（`turbine` / `pcs` / `met_mast` …）；
- `device_models` — 型号：`device_type`、`protocol`
  （`modbus` / `ads` / `iec104`）、`point_table`（绑定点表，**型号的
  protocol 必须与点表的 protocol 一致**）、`read_mode`（仅 ADS：
  `sum` 批量周期采集，缺省；`sequential` 逐点读，仅单次读取与诊断，
  任何 Task 引用 sequential 设备都是配置错误）、`properties`、
  `connection_defaults`（`port` 并入实例端口，其余并入 endpoint
  extensions，实例优先）。

## devices.yaml

现场设备实例——只写「身份、型号引用、连接差异」：

```yaml
devices:
  - device_id: wtg-001
    model: modbus_wtg        # 引用 device_models.yaml 的型号
    device_group: turbine    # 采集分组（Task 按它成组展开）
    enabled: true
    endpoint:
      host: "192.168.1.101"  # port 可省略，由型号 connection_defaults 提供
```

协议、点表、读取策略全部继承自型号；加载期合并为 resolved 运行时
`DeviceConfig`，Runtime 不回查型号。设备**不再**定义采集周期——周期
采集完全由 tasks.yaml 的 Task 声明。

## points.yaml

命名点表集合：表是设备无关的完整点集定义，设备型号经 `point_table`
绑定，多台同型号设备共享一份。

```yaml
point_tables:
  modbus_wtg_v1:
    protocol: modbus          # 点表协议：基础表必填
    points:
      - point_id: active_power
        variable_name: active_power
        point_groups: [all]   # 采集分组（多值）
        address:
          type: input         # Modbus：register_type 别名
          address: 178        # 0-based 寄存器偏移
        data_type: int32
        scale: 0.001
        offset: 0.0
        unit: kilowatt        # units.yaml 的 unit ID，缺省 none
        description: "有功功率"
```

- `protocol`（`ads` / `modbus` / `iec104`）决定点 `address` 的形式与
  校验；点表继承时子表缺省继承父表 protocol，显式配置必须与父表一致
  （禁止跨协议继承）。`ResolvedPointTable.protocol` 供运行态与 admin
  直接读取。
- 地址按点表 protocol 在加载期校验：ADS 要求 `symbol` 或
  `index_group` + `index_offset` 成对；Modbus 要求合法 register_type
  （`coil` / `discrete_input` / `holding` / `input` 及别名）与非负
  `address`；IEC104 要求合法 `ioa`。
- `point_groups` 为**多值**——一个点可同时属于多个组；Task 按
  `task.point_group ∈ point.point_groups` 选点。
- `scale` / `offset`：工程值换算，由运行时 Device 在采集出口统一应用
  （轮询与订阅同语义）；点值 quality 只来自协议原生判定，采集链路不做
  值域校验。
- 点表支持单继承（`extends` / `remove_points` / 点位补丁），继承展开后
  统一校验（`point_groups` 非空不重复、`data_type` 白名单、unit ID
  存在、地址合法）。

点位**不再**声明输出 sink——输出去向完全由 Task 的 `targets` 决定。

## tasks.yaml

`tasks` 列表，每个 Task 在运行时展开为一组 Task Instance
（`{task_id}:{device_id}`）：

```yaml
tasks:
  - task_id: turbine-fast        # 唯一 ID
    device_group: turbine_ads    # device / device_group 二选一（XOR）
    point_group: fast            # 单值；选择 point_groups 含 fast 的点
    interval: 1.0                # 主动轮询/ADS 必填，> 0；纯 IEC104 订阅可省略
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

## reporting.yaml（IEC104 从站代理，可选）

把采集到的点经 IEC104 从站代理上报给调度主站：

```yaml
reporting: []             # 空列表 = 禁用（文件缺失同为禁用）
batch_size: 50            # 总召数据每批最多信息对象数
common_address: 1         # IEC104 公共地址（站地址）
host: "127.0.0.1"         # 从站代理监听地址
port: 12404               # 从站代理监听端口
```

启用时 `reporting` 列表把 `(device_id, point_id)` 映射到 `ioa` 与遥测
方向 ASDU `data_type`（白名单见 `schema.py`）。

## probe 端口扫描配置（不属于采集配置体系）

`wind-hub probe ports` 的「端口 → 服务名」映射缺省使用内置工业协议
映射，可经 `--ports-config <file>` 提供自定义 YAML：

```yaml
mapping:        # 端口 → 服务名；未指定 --ports 时扫描 mapping 的全部端口
  502: modbus
  2404: iec104
timeout: 1.0    # 单端口默认超时（秒）
concurrency: 128  # 默认并发探测数
```

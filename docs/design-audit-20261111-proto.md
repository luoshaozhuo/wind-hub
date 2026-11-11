# 设计文档与六份正式 Proto 的机械比对

- 基线：`567e30ffb1aa1c1a519c0156991ca22811f389cf`
- 方法：直接读取六份 `src/contracts/*.proto`，提取 service / rpc / 入参 / 出参及 message 字段，逐篇检索相关 RPC 名称。
- 局限：只验证静态接口存在性与字段数量；字符串匹配通过不代表语义一致。此阶段不包含真实 Descriptor、Servicer 注册、RPC 调用执行或生成代码的编译验证。

## RPC 事实表

| Proto | RPC 数 | RPC 名称 |
|---|---:|---|
| src/contracts/common.proto | 9 | GetServiceInfo, GetServiceStatus, GetCapabilities, GetConfigSnapshot, PushConfigSnapshot, ApplyConfigSnapshot, GetMetrics, GetEvents, Diagnose |
| src/contracts/collector.proto | 11 | ListDevices, GetDevice, ListPoints, GetPoint, ReadPoints, ListTasks, GetTask, StartTask, StopTask, GetPublishingStatus, InterrogateDevice |
| src/contracts/commander.proto | 7 | ListDevices, GetDevice, ListPoints, SubmitCommand, SubmitCommands, GetCommandResult, GetRequestResult |
| src/contracts/sink_service.proto | 4 | ListDeliveries, GetDelivery, StartDelivery, StopDelivery |
| src/contracts/industrial_gateway.proto | 8 | ListGateways, GetGateway, StartGateway, StopGateway, ListVariables, ReadVariables, UpdateVariables, ClaimTargets |
| src/contracts/data_generator.proto | 6 | ListDevices, GetDevice, ListScenarios, GetGeneration, StartGeneration, StopGeneration |

**合计：45 项 RPC。**

## Commander Proto 逐消息字段计数

- 消息数量：15
- 字段总数：58

| Message | 字段数 |
|---|---:|
| ListDevicesRequest | 3 |
| Device | 5 |
| ListDevicesResponse | 2 |
| GetDeviceResponse | 1 |
| ListPointsRequest | 3 |
| ListPointsResponse | 2 |
| SubmitCommandRequest | 6 |
| SubmitCommandResponse | 7 |
| GetCommandResultRequest | 1 |
| GetCommandResultResponse | 9 |
| CommandItem | 5 |
| SubmitCommandsRequest | 2 |
| SubmitCommandsResponse | 6 |
| GetRequestResultRequest | 1 |
| GetRequestResultResponse | 5 |

## 文档覆盖情况

| 设计文档 | 对应 Proto | RPC 数 | 文档未出现的 RPC 名称 |
|---|---|---:|---|
| Contracts_软件设计_v1_19.md | common.proto | 9 | 无 |
| Contracts_软件设计_v1_19.md | collector.proto | 11 | 无 |
| Contracts_软件设计_v1_19.md | commander.proto | 7 | 无 |
| Contracts_软件设计_v1_19.md | sink_service.proto | 4 | 无 |
| Contracts_软件设计_v1_19.md | industrial_gateway.proto | 8 | 无 |
| Contracts_软件设计_v1_19.md | data_generator.proto | 6 | 无 |
| Collector_软件设计_v1_19.md | common.proto | 9 | 无 |
| Collector_软件设计_v1_19.md | collector.proto | 11 | 无 |
| Commander_软件设计_v1_29.md | common.proto | 9 | 无 |
| Commander_软件设计_v1_29.md | commander.proto | 7 | 无 |
| industrial_gateway_软件设计_v1_26.md | common.proto | 9 | 无 |
| industrial_gateway_软件设计_v1_26.md | industrial_gateway.proto | 8 | 无 |
| Data_Generator_软件设计_v1_22.md | common.proto | 9 | 无 |
| Data_Generator_软件设计_v1_22.md | data_generator.proto | 6 | 无 |
| Data_Generator_软件设计_v1_22.md | industrial_gateway.proto | 8 | 无 |
| Sink_Service_软件设计_v1_49.md | common.proto | 9 | 无 |
| Sink_Service_软件设计_v1_49.md | sink_service.proto | 4 | 无 |
| CLI_软件设计_v1_25.md | common.proto | 9 | 无 |
| CLI_软件设计_v1_25.md | collector.proto | 11 | 无 |
| CLI_软件设计_v1_25.md | commander.proto | 7 | 无 |
| CLI_软件设计_v1_25.md | sink_service.proto | 4 | 无 |
| CLI_软件设计_v1_25.md | industrial_gateway.proto | 8 | 无 |
| CLI_软件设计_v1_25.md | data_generator.proto | 6 | 无 |

## 限制与后续核对项

RPC 名称出现不等于文档准确描述字段、身份授权、错误映射或结果状态。须另外核对：逐字段类型、标签、presence；实际 Service Servicer 注册与调用方；Configuration 公开 API；Service YAML 的显式来源；Sink ACK 状态机与可靠消息线协议。

# 测试规则

本文件定义测试层级、真实性和结果语义；何时执行测试由 Gate Skill 决定。

## 1. 目录与层级

正式测试按第一层测试目的组织：

```text
tests/
├── unit/
├── component/
├── contract/
├── integration/
├── system/
├── reliability/
├── performance/
├── fixtures/
└── support/
```

定义：

| 层级 | 目的 |
|---|---|
| `unit` | 单函数、单类、单状态机；无真实外部服务 |
| `component` | 单进程/模块内部多组件协作；允许 fake/mock 外部依赖 |
| `contract` | RPC、API、配置/schema、错误码和稳定语义契约 |
| `integration` | 两个以上真实组件或真实软件服务协作 |
| `system` | 从外部接口观察完整或接近完整系统行为 |
| `reliability` | 重试、故障、恢复、资源释放、重启等可靠性行为 |
| `performance` | 吞吐、延迟、抖动、容量和资源使用 |
| `soak` | 长时间稳定性；作为 reliability 的长期资格属性 |

## 2. Marker

Marker 应正交表达不同维度：

```text
层级:
unit component contract integration system reliability performance soak

真实性:
mock_service real_service hardware

协议/服务:
modbus ads iec104 kafka postgres influxdb file

环境:
docker network root slow fast
```

目录表达主要测试目的，marker 表达真实性、协议、外部依赖和环境要求；不要用一个 marker 混合多个维度。

## 3. 真实性

```text
mock/fake/stub
≠ real_service
≠ hardware
```

1. Integration 若依赖真实软件服务，应明确使用 `real_service` 或相应服务 marker。
2. 环境缺失时不得静默退化到 mock 后仍声称真实集成通过。
3. 真实 PLC、现场设备测试必须标记 `hardware`。
4. health check、脚本存在、端口开放均不是业务测试 PASS。

## 4. 结果状态

Agent 与 Gate 最终只使用：

```text
PASS
FAIL
RUNNING
QUEUED
NOT_RUN
NOT_EXECUTED
```

含义：

- `PASS`：目标 Gate 已执行且全部必需检查成功。
- `FAIL`：目标 Gate 已执行并存在失败。
- `RUNNING` / `QUEUED`：当前目标 SHA 的任务正在执行或等待 runner。
- `NOT_RUN`：没有执行该 Gate 或该检查。
- `NOT_EXECUTED`：因硬件、权限、runner 或外部环境不具备而无法执行资格测试。

pytest 的 `skip/xfail` 是框架原始状态，不直接等价于 Gate PASS。报告必须说明其原因和是否影响 Gate 资格。

## 5. Skip / XFail

允许 skip 的典型原因：

- 硬件不可用；
- Docker/真实外部服务不可用；
- 所需权限或网络条件不可用。

禁止用 skip/xfail 掩盖：

- 本次代码缺陷；
- 陈旧测试；
- 尚未处理的失败；
- 仅为了让 Gate 变绿。

`xfail` 必须有明确已知问题和解除条件。

## 6. 测试设计

1. 行为变化同步更新相应测试。
2. public contract 变化优先补 contract test。
3. 错误、超时、取消、重试、幂等、回滚和清理属于正式行为，应覆盖失败路径。
4. 测试必须隔离并清理进程、socket、asyncio task、临时文件、容器和测试数据。
5. 不为旧测试恢复已废弃架构；先判断生产缺陷还是断言已过时。
6. 性能测试没有正式 SLA 时可建立 baseline，但不得凭空制造生产阈值。

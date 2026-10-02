# 软件交付与 CI 质量门禁规范

> 适用于 Coding Agent、CI 流水线和人工开发。  
> 本文件定义代码从修改到生产发布必须满足的质量门禁。除明确标记为“可选”或“环境受限”的项目外，均为强制要求。

## 1. 基本原则

1. **同一提交逐级晋级**：Gate 2 不替代 Gate 1，Gate 3 不替代 Gate 1～2。
2. **未执行不等于通过**：因硬件、网络、权限、Docker 等原因未执行的测试必须记录为 `SKIPPED` 或 `NOT EXECUTED`，不得计入 `PASS`。
3. **失败必须阻断**：强制门禁失败时，不得继续执行对应的合并、发布或生产投运动作。
4. **禁止伪通过**：不得通过放宽断言、删除有效测试、滥用 `skip/xfail`、关闭静态检查等方式制造绿色结果。
5. **测试必须与当前架构一致**：陈旧测试应修复、重写或删除，不得为旧测试恢复已废弃行为或增加无意义兼容层。
6. **真实缺陷必须修复**：测试暴露业务缺陷时，应修复生产代码并增加回归测试。
7. **门禁应可重复执行**：除真实硬件测试外，CI 中的测试应具备隔离、清理和确定性。

---

## 2. Gate 1 — Commit / Fast Gate

### 2.1 目的

阻止明显代码错误、类型错误、架构违规和基础功能回归进入主干。

### 2.2 强制检查

至少包括：

- Lint / 格式规范检查
- Static typing
- 架构依赖检查
- Unit tests
- Component tests
- Contract tests
- 前端 Unit tests
- 前端类型检查与 Build

当前项目建议映射：

```text
ruff
mypy
import-linter
pytest -m unit
pytest -m component
pytest -m contract
vitest
vue-tsc
vite build
```

### 2.3 门禁规则

任一强制检查失败：

```text
禁止提交候选进入下一阶段
```

修复后必须重新执行受影响检查。

---

## 3. Gate 2 — PR / Integration Gate

### 3.1 目的

验证模块、进程、协议和外部服务之间的真实协作。

### 3.2 强制检查

至少包括：

- Integration tests
- RPC integration
- 配置加载与配置事务集成测试
- 真实 Modbus TCP 测试
- 真实 IEC 104 TCP 测试
- Kafka / PostgreSQL 等可自动部署服务
- System smoke tests
- 前端 E2E smoke tests

外部服务优先由 Docker Compose 或等效自动化方式提供。

### 3.3 禁止事项

- Integration 测试不得全部使用 monkeypatch/fake 后仍标记为真实集成测试。
- 真实服务不可用时，不得静默退化为 mock。
- 不得将硬件未执行测试计入通过率。

### 3.4 门禁规则

Gate 2 失败：

```text
禁止合并
```

---

## 4. Gate 3 — Release Candidate Gate

### 4.1 目的

验证候选版本是否具备生产发布资格。

### 4.2 强制检查

至少包括：

- 完整 System tests
- 关键 E2E 链路
- Recovery tests
- Fault injection tests
- Performance tests
- 全部软件可控协议服务
- 全部软件可控 Sink
- 资源清理与优雅停机
- 配置 reload / rollback / reconciliation
- 重启恢复
- 幂等性与重复请求处理

### 4.3 关键链路

必须至少覆盖：

```text
Device/Protocol
→ Collector
→ Pipeline
→ Sink
```

```text
API/CLI
→ Server
→ Commander
→ Protocol
→ Device
→ Result
```

```text
Config Change
→ Validate
→ Prepare
→ Activate
→ Workers
→ Result / Rollback
```

### 4.4 门禁规则

Gate 3 失败：

```text
禁止生成正式 Release
```

---

## 5. Gate 4 — Production Qualification Gate

### 5.1 目的

验证软件在真实或接近真实生产条件下的长期稳定性与故障恢复能力。

### 5.2 典型项目

根据实际环境执行：

- 真实 ADS / TwinCAT / PLC 测试
- 真实现场协议设备
- 目标规模负载测试
- 长时间 Soak test
- 网络延迟、丢包、断线
- 进程 kill / restart
- Sink 中断与恢复
- 磁盘压力
- 长时间资源泄漏检查

### 5.3 Soak test

长稳测试应关注趋势，而不仅是进程是否存活。

至少监测：

```text
CPU
RSS
FD count
thread count
asyncio task count
queue depth
event-loop lag
data loss
sampling jitter
RPC latency
sink latency
reconnect count
```

### 5.4 门禁规则

生产所需 Qualification 项未执行或失败：

```text
不得声明“已完成生产验证”
```

硬件环境缺失时应记录：

```text
NOT EXECUTED — environment unavailable
```

不得记录为 `PASS`。

---

## 6. Test 分类规则

测试第一层应按测试目的组织，推荐：

```text
tests/
├── unit/
├── component/
├── contract/
├── integration/
├── system/
├── reliability/
├── performance/
├── security/
├── fixtures/
└── support/
```

`reliability/` 可进一步包含：

```text
recovery/
fault_injection/
soak/
```

### 6.1 定义

- `unit`：单函数、单类、单状态机。
- `component`：单进程或单模块内部协作，允许 mock 外部依赖。
- `contract`：RPC、API、配置 Schema、错误码、版本兼容等稳定契约。
- `integration`：两个及以上真实组件或真实服务协作。
- `system`：从外部接口观察完整系统行为。
- `reliability`：故障检测、恢复、重启、降级、长稳。
- `performance`：吞吐、延迟、资源占用、抖动和容量。

不得使用含义不清晰的测试层级代替上述分类。

---

## 7. Marker 规则

推荐使用正交 marker 表达测试属性：

```text
unit
component
contract
integration
system
reliability
performance
soak

mock_service
real_service

modbus
ads
iec104

kafka
postgres
influxdb
file

fast
slow

docker
hardware
network
root
```

测试层级、协议、外部依赖和环境要求必须分别表达，不得混为同一个目录语义。

---

## 8. Skip / XFail 规则

允许 `skip` 的典型原因：

```text
hardware unavailable
docker unavailable
required privilege unavailable
external environment unavailable
```

禁止因为以下原因 `skip`：

```text
测试失败
代码存在缺陷
测试过时但未修复
暂时不想处理
```

`xfail` 必须对应明确、已知、可追踪的问题，并说明解除条件。

---

## 9. 性能与可靠性规则

没有正式 SLA 时：

- 允许建立 baseline；
- 必须记录原始指标；
- 不得凭空创建生产阈值。

建议至少记录：

```text
throughput
P50 / P95 / P99 latency
CPU
RSS
queue depth
missed cycles
data loss
reconnect time
sampling jitter
```

性能测试失败不得通过降低负载、缩短有效测量窗口或删除异常样本来规避。

---

## 10. 测试环境与清理

每个测试必须保证资源隔离和回收。

测试结束后不得遗留：

```text
background process
zombie process
open socket
asyncio task
temporary file
Docker container
Kafka test topic
database test data
```

测试失败路径同样必须执行清理逻辑。

---

## 11. Coding Agent 执行规则

Coding Agent 在完成涉及代码的任务时，应根据变更范围自动选择最低必要 Gate。

### 普通代码修改

至少执行 Gate 1 中与当前模块相关的检查。

### 跨模块 / RPC / 配置 / 协议修改

至少执行：

```text
Gate 1
+ 对应 Integration tests
```

### 核心架构、调度、配置事务、设备控制修改

至少执行：

```text
Gate 1
+ Gate 2 相关项
+ 对应 System / Recovery tests
```

### 发布准备

必须执行 Gate 1～3。

正式现场投运前还必须检查 Gate 4 执行记录。

---

## 12. 提交与报告

每个提交前至少确认：

```text
git status
git diff
相关测试
相关静态检查
```

不得提交已知失败但未说明原因的测试结果。

Release / Production Qualification 报告至少记录：

```text
Git SHA
OS / Runtime
Dependency versions
执行的 Gate
PASS / FAIL / SKIPPED / NOT EXECUTED
性能基线
硬件测试状态
已知限制
```

最终判断必须针对具体 Git SHA，不得只描述“当前代码大致通过”。

---

## 13. 强制决策规则

```text
Gate 1 failed
→ 不得进入下一阶段

Gate 2 failed
→ 不得合并

Gate 3 failed
→ 不得发布正式 Release

Gate 4 未完成或失败
→ 不得声明完成生产验证
```

任何 Coding Agent 都不得绕过上述规则，除非用户明确要求修改本规范本身。

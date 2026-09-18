# 验证路由规则

## 1. 规则定位

本规则用于根据真实变更范围选择验证阶段和验证命令。

核心原则：

1. 每次编码后只执行与本次风险匹配的最小必要验证。
2. 不把高成本验证默认提升为日常编码后的必跑项。
3. 具体命令以仓库工具链、测试索引、任务 handoff 和真实环境为准。

## 2. 执行优先级

| 优先级 | 含义 | 收口要求 |
|---|---|---|
| must-run | 本次变更风险必须覆盖 | FAIL 或 NOT_RUN 时不得收口，除非任务明确调整范围 |
| should-run | 建议执行，可由任务范围裁剪 | 未执行时写入后续建议并说明原因 |
| manual-or-expensive | 需要人工、专门环境或高成本资源 | 不自动执行，需记录触发条件 |
| not-run | 与本次变更无关 | 不执行，必要时说明排除理由 |

## 3. 通用选择原则

1. 默认先跑构建期验证和受影响范围内的低成本测试。
2. 出现 public interface、CLI、schema、adapter、runtime 边界变化时，优先补受影响 unit 和必要 integration。
3. `smoke`、`regression` 是逻辑集合，不自动等同于每次变更后的必跑集合。
4. `performance`、长稳、准生产依赖、完整部署演练默认归 `manual-or-expensive`，除非用户或任务明确要求。
5. 部署相关验证只在部署资产、运行入口、配置装配、发布前检查或用户明确要求时提升优先级。

## 4. 通用路由

| 变更类型 | must-run | should-run | manual-or-expensive |
|---|---|---|---|
| 纯文档、报告、注释文字 | 格式、链接、路径或规则一致性检查 | 相关索引或目录树检查 | 无 |
| 生产源码行为 | 构建期验证、开发期验证（受影响 unit） | 模块集成期验证 | 跨模块联调期验证、回归集合 |
| public interface / API / CLI | 构建期验证、开发期验证、受影响契约测试 | 模块集成期验证、调用方验证、必要 smoke | 跨模块联调期验证、发布前回归 |
| schema / migration / 配置 | 解析和兼容性验证、构建期验证 | 模块集成期验证、部署配置检查 | 数据迁移演练、发布前部署验证 |
| 消息格式 / 协议 / 文件格式 | parser/serializer、兼容性测试、受影响 unit | 跨边界 integration | 准生产依赖验证、专项 regression |
| adapter / repository / external client | 构建期验证、开发期验证、模块集成期验证 | 准生产依赖验证、必要 regression | 故障注入、长稳、真实依赖专项 |
| runtime / scheduler / worker / lease | 构建期验证、开发期验证、模块集成期验证 | 跨模块联调期验证、必要 smoke | failover、并发、长稳、发布前回归 |
| 安全 / 权限 / 审计 / 凭据 | deny/conflict/failure path、审计检查 | 模块集成期验证 | 安全评审、渗透或合规检查 |
| Docker / Compose / deployment / scripts | 脚本语法、配置检查 | 部署前 smoke、最小部署闭环 | 发布演练、回滚演练、准生产依赖验证 |
| 工具/实验模块 | 工具自身构建期和开发期验证 | 工具自身模块集成期验证 | 只有影响生产边界时才扩展到生产链路 |
| performance 相关改动 | 构建期验证、低成本 correctness 验证 | 必要局部基准 | 性能、容量、长稳、压测 |

## 5. smoke / regression / performance / deployment 触发策略

### 5.1 smoke

1. `smoke` 用于快速关键路径最小可用验证。
2. 不默认要求对每次改动运行完整 smoke 集合。
3. 当变更触及运行入口、主装配路径、关键 CLI、部署前最小闭环或历史关键链路时，可提升为 `should-run` 或 `must-run`。

### 5.2 regression

1. `regression` 用于防止历史问题、关键链路或高风险行为回退。
2. 不默认要求对每次改动运行完整 regression 集合。
3. 当变更命中已有回归条目、历史事故边界、关键协议兼容边界或发布前范围时，可提升优先级。

### 5.3 performance

1. `performance` 默认不属于每次编码后的必跑项。
2. 仅在以下情况提升为执行项：
   - 用户明确要求；
   - prompt / handoff 明确要求；
   - 性能问题排查；
   - 发布前性能专项；
   - 性能敏感代码的高风险修改。

### 5.4 deployment

1. `deployment` 验证针对部署入口、配置装配、health/ready、迁移、回滚和最小部署闭环。
2. 仅在部署相关资产、运行入口、发布前检查或用户明确要求时提升优先级。
3. 不应默认因为普通业务逻辑修改而触发完整部署验证。

## 6. 验证计划输出

执行验证前应形成简短计划：

```text
Validation plan:
- must-run:
- should-run:
- manual-or-expensive:
- not-run:
```

计划应说明：

1. 阶段或层次；
2. 验证对象；
3. 命令、marker 表达式或测试索引项；
4. 未执行项的原因码。

## 7. 收口规则

1. `must-run` 中存在 FAIL 时不得收口。
2. `must-run` 中存在 NOT_RUN 时不得收口，除非任务范围明确排除该验证。
3. `should-run` 未执行时，必须记录原因和后续建议。
4. `manual-or-expensive` 未执行时，必须说明触发条件和未执行原因。
5. 只执行局部验证时，反馈和报告必须说明局部范围。
6. 工具、mock、fake、stub、simulator 验证不得写成生产真实闭环。
7. 本次变更新增失败不得进入下一阶段。

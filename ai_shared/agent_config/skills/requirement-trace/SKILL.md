---
name: requirement-trace
description: Use when requirement status or evidence level needs to be updated from real source code, tests, configs, schemas, reports, and validation evidence, without overstating unverified results.
---

# requirement-trace

## 1. 目的

根据当前源码、测试、配置、schema、报告与验证证据，更新需求说明和需求跟踪表。

## 2. 触发条件

必须使用：
- 需求实现状态发生变化。
- 需求证据等级发生变化。
- 发现既有需求状态高估。
- 用户明确要求更新需求跟踪。

禁止使用：
- 只有普通代码格式、注释或局部重构且不影响需求状态。
- 没有验证证据时把状态提升。

## 3. 输入

```text
目标需求文件
真实变更文件
测试命令和结果
Agent result
必要报告或其他稳定证据
```

## 4. 判定规则

1. 需求状态只能基于当前真实证据更新。
2. 不得把 skipped、mock、fake、health check、TCP connect、脚本存在、单文件 passed、环境 pending 写成真实通过。
3. 如果只有 unit/mock/contract/simulator 证据，必须标明证据等级。
4. 如果发现既有状态高估，必须降级或标注 pending。
5. 功能需求和非功能需求必须分开判断。
6. 不把每个函数都抽象成需求；只更新有业务或质量意义的需求条目。

## 5. 输出格式

```text
skill result:
- skill: requirement-trace
- requirement files:
- status changes:
- evidence:
- downgraded items:
- pending items:
- risk:
```

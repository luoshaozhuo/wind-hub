---
name: code-quality-gate
description: Select quality and test commands only inside an explicitly invoked test, validate, or test-all stage, based on the stage's real scope.
---

# code-quality-gate

## 生效条件

仅在用户显式触发 `/test`、`/validate` 或 `/test-all` 时使用。普通编码阶段禁止使用本 skill。

## 输入

```text
validation stage: test | validate | test-all
changed-files-gate 结果
ai_shared/rules/quality-gate.md
相关源码、测试、配置、schema 和仓库工具链
```

## 范围

1. `test`：staged diff 直接修改代码的测试及必要最小语法检查。
2. `validate`：staged 修改及其真实引用/依赖范围的 syntax、lint、type、测试和专项门禁。
3. `test-all`：当前整个工作区的全仓常规工具链和测试；高成本项需要 `include-heavy`。

## 必须判断

需要执行哪些语法/编译、lint、type-check、测试、契约/schema/配置、注释和生产路径边界检查，以及哪些项因范围、环境或成本应标记 `NOT_RUN`。

## 输出

```text
skill result:
- skill: code-quality-gate
- validation stage:
- scope:
- test changes allowed: yes/no
- syntax/compile commands:
- lint commands:
- type-check commands:
- test commands:
- special gates:
- not-run:
- risk:
```

---
name: local-fast-gate
description: Run the local counterpart of GitHub CI Fast after a coding stage is complete. Use by default after code changes before considering the coding stage complete.
---

# Local Fast Gate

## 目的

在本地尽早发现静态检查、unit/component/contract 和前端单元/构建问题。它与 GitHub `ci-fast.yml` 使用同一 Gate 语义。

## 执行

1. 先运行环境 preflight。
2. 执行项目统一 Gate 入口的 `fast` 模式；若统一入口尚不可用，按当前 `.github/workflows/ci-fast.yml` 的实际检查集合执行。
3. 任一必需项失败则结果为 `FAIL`，修复后重新执行。
4. 不自动升级到 integration/system/Playwright/hardware/performance/soak。

## 结束

只报告 `PASS`、`FAIL` 或明确的 `NOT_EXECUTED`；不得用局部通过代替整个 Fast Gate。

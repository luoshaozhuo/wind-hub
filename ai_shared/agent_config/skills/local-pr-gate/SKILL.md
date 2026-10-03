---
name: local-pr-gate
description: Run the local counterpart of GitHub CI PR Gate before creating or updating a pull request for merge into main.
---

# Local PR Gate

## 目的

在 push/PR 前验证 integration、system smoke 和 frontend E2E，减少远端 CI 往返。

## 执行

1. 要求当前任务已通过 `local-fast-gate`。
2. 执行项目统一 Gate 入口的 `pr` 模式；若统一入口尚不可用，按当前 `.github/workflows/ci-pr.yml` 的实际检查集合执行。
3. 真实软件服务不可用时明确 `NOT_EXECUTED`，不得静默换成 mock。
4. 失败不得进入“准备合并”状态。

## 结束

报告当前 branch、HEAD SHA、各检查状态以及未执行原因。

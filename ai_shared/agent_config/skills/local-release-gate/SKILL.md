---
name: local-release-gate
description: Run the local counterpart of the GitHub Release Gate before a release candidate or version tag is prepared.
---

# Local Release Gate

## 目的

执行完整常规软件验证，不包含需要特殊环境的 hardware/performance/soak。

## 执行

1. 执行项目统一 Gate 入口的 `release` 模式；若统一入口尚不可用，按 `.github/workflows/ci-release.yml` 的实际集合执行。
2. Backend：静态检查 + 全部分层常规测试，排除 hardware/performance/soak。
3. Frontend：build + Vitest + Playwright。
4. 失败不得生成 Release 候选结论。

## 结束

结果绑定当前 Git SHA。

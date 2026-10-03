---
name: ci-fix-loop
description: Diagnose and fix GitHub Actions failures bound to one exact branch HEAD SHA, restarting whenever the target branch moves.
---

# CI Fix Loop

## 状态

只使用：

```text
PASS
FAIL
RUNNING
QUEUED
NOT_RUN
NOT_EXECUTED
```

## 流程

1. 获取目标 branch 的远端 HEAD，记录 `TARGET_SHA`。
2. 只分析 `head_sha == TARGET_SHA` 的 workflow run；禁止用旧 SHA 的最近结果代替。
3. queued/running 只报告状态；completed 只读取该 SHA 的失败 job/step/log。
4. 修改前重新确认远端 HEAD。HEAD 已移动则废弃旧诊断并从新 SHA 重启。
5. 只修复当前失败直接暴露的问题，不扩大范围。
6. 修复后执行至少 `local-fast-gate`；必要时执行对应本地 Gate。
7. commit/push 前再次确认远端 HEAD，禁止 force push 和覆盖用户已有修改。
8. push 后以新 SHA 重新跟踪，旧 SHA 结论立即过期。
9. hardware/performance/soak 缺环境时使用 `NOT_EXECUTED`；不要把资格环境问题改成产品代码问题。

## 结束

仅当当前 `TARGET_SHA` 的必需 GitHub Gate 明确 PASS 时，才能声称 CI 通过。

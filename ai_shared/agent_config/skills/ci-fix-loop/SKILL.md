---
name: ci-fix-loop
description: Diagnose and fix only GitHub CI failures that are relevant to the current branch HEAD and current change scope.
---

# CI Fix Loop

## 状态与相关性

状态：

```text
PASS / FAIL / RUNNING / QUEUED / NOT_RUN / NOT_EXECUTED
```

相关性：

```text
RELATED / UNRELATED / UNKNOWN
```

## 流程

1. 获取目标 branch HEAD，记录 `TARGET_SHA`；只分析该 SHA 的 run。
2. 读取本次变更文件，确定产品代码、前端、CI/tooling、治理范围。
3. 对失败先判断相关性：
   - 当前变更直接涉及失败文件、依赖、测试或执行入口 → `RELATED`；
   - 同一失败可在 base/main 证实已存在，或失败子系统与本次变更无依赖 → `UNRELATED`；
   - 证据不足 → `UNKNOWN`。
4. `UNRELATED`：记录失败 job、证据和理由后忽略，不修改代码，不阻断本次任务。
5. `UNKNOWN`：只做足以判断相关性的最小诊断。
6. `RELATED`：只修复当前失败直接暴露的问题，不扩大范围。
7. 修复后只执行与变更范围匹配的本地验证；非产品变更不得为了 CI 红灯跑完整产品 Gate。
8. commit/push 前重新确认远端 HEAD；HEAD 移动则废弃旧诊断。
9. push 后以新 SHA 重启判断，旧 SHA 结论立即过期。
10. hardware/performance/soak 环境不足为 `NOT_EXECUTED`，不改成产品 FAIL。

禁止为了“全绿”修复与当前任务无关的既有问题。

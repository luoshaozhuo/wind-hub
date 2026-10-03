---
name: ci-fix-loop
description: Bind GitHub Actions diagnosis and automated fixes to one exact Git commit SHA, stopping and restarting whenever the target branch HEAD moves.
---

# CI Fix Loop

用于 GitHub Actions 失败诊断与 coding agent 自动修复。核心原则：**CI 结果、失败日志、修复和重新验证必须绑定到同一个明确的 Git commit SHA。**

## 适用场景

- 用户要求检查最新 CI、修复失败 workflow、让 CI 变绿。
- coding agent 在 push 后继续检查 GitHub Actions 并修复。
- PR 或目标分支连续产生多个 commit，需要避免读取过期 run。

## 状态

只使用以下状态：

- `PASS`：目标 SHA 对应的必需检查已完成且成功。
- `FAIL`：目标 SHA 对应的检查已完成且失败。
- `RUNNING`：目标 SHA 对应检查正在执行。
- `QUEUED`：等待 runner。
- `NOT_RUN`：目标 SHA 没有对应 run。
- `NOT_EXECUTED`：资格测试因硬件、权限或环境缺失没有执行。不得视为 PASS。

## 强制流程

1. 获取目标分支（默认用户指定分支；未指定时使用当前工作分支）的远端 HEAD：
   `git fetch origin <branch>`
   `TARGET_SHA=$(git rev-parse origin/<branch>)`
2. 记录 `TARGET_SHA`。后续只分析 `head_sha == TARGET_SHA` 的 workflow run；禁止用“最近一次 run”替代。
3. 查询与该 SHA 对应的 runs/jobs/steps/logs，并按 workflow 的实际触发条件判断：
   - 没有 run：报告 `NOT_RUN`，不要拿旧 SHA 的结果补位。
   - queued/running：报告 `QUEUED/RUNNING`。
   - completed：只读取这个 SHA 的失败 job/step/log。
4. 修改前再次 fetch 目标分支并比较远端 HEAD。若 `origin/<branch> != TARGET_SHA`：
   - 当前修复循环立即失效；
   - 不提交、不 push 当前基于旧日志的修复；
   - 以新 HEAD 重新开始。
5. 只修复当前失败直接暴露的问题。禁止顺手扩大重构范围。
6. 本地执行最小相关测试；必要时执行对应 Gate。
7. commit 前再次确认远端 HEAD 仍等于 `TARGET_SHA`，并检查工作区，禁止覆盖用户已有未提交修改。
8. push 前再次 fetch；若远端已移动，停止并重新基线化。禁止 force push。
9. push 成功后获取新 commit SHA：
   - `TARGET_SHA = NEW_SHA`
   - 旧 SHA 的所有 CI 结论立即视为过期；
   - 只跟踪新 SHA 对应的 workflow run。
10. 重复直到目标 Gate 为 `PASS`，或遇到需要用户/外部环境处理的 `NOT_EXECUTED`。

## 并发与过期 run

- `ci-fast` / `ci-pr` 可配置 `concurrency.cancel-in-progress: true`，让新 push 自动取消同一分支/PR 的旧 run。
- 被取消的旧 run 不代表新 SHA PASS 或 FAIL。
- release、hardware、performance、soak 等长任务不得因为普通新 push 自动取消，除非用户明确要求。

## 资格测试

hardware/performance/soak 需要特殊 runner 或环境时：

- runner 不存在：保持 `QUEUED`，不要修改代码。
- runner 存在但前置环境不足：记录 `NOT_EXECUTED`。
- GitHub UI 若只能以 failure 表示 NOT_EXECUTED，以 Step Summary 的资格状态为语义来源；绝不能把它解释成产品测试 FAIL，也不能解释成 PASS。
- 不自动触发 8h/24h soak、真实 PLC 写入或其他昂贵/有现场风险的资格测试，除非用户明确要求。

## Artifact 与报告

- 资格 artifact/report 名应包含 `github.sha`。
- 每份报告至少记录 SHA、run ID、runner、profile/mode、最终状态。
- 一个 SHA 的 artifact 不能作为另一个 SHA 的发布证据。

## 结束条件

最终反馈至少包含：

- branch；
- TARGET_SHA；
- workflow/run ID；
- 每个必需 Gate 的状态；
- 实际修复的失败；
- 本地验证；
- 新 SHA（若产生提交）；
- 尚未执行的 hardware/performance/soak 及原因。

禁止声称“CI 已通过”，除非对应结论明确属于当前 TARGET_SHA。

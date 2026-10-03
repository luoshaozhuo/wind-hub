---
name: merge-branch
description: Use when a finished task branch must be merged back to main via PR, with CI verification and the final merge handed to the user.
---

# merge-branch

## 目的

将已完成并推送的任务分支经 PR 合并回 main，遵守安全任务分支流程。

## 原则

1. 不直接在 main 上操作；Agent 不执行 `git merge` / `gh pr merge` /
   `git checkout`（git-safety-gate 会拒绝），最终合并与收尾由用户执行。
2. CI 失败按 RELATED / UNRELATED / UNKNOWN 分类，仅 RELATED 必须修复；
   需要修复时衔接 `ci-fix-loop`。
3. 合并方式与仓库历史一致（merge commit），不 squash、不 rebase，
   除非用户明确要求。

## 步骤

1. 确认前提：工作区干净，分支已推送且与远端同步。

   ```bash
   git status --short --branch
   git log origin/main..HEAD --oneline
   ```

2. 创建 PR（已存在则跳过）；title 沿用分支上最近 commit 的 summary。

   ```bash
   gh pr create --base main --title "<summary>" --body "<变更要点>"
   ```

3. 监控 CI 直到全部完成。

   ```bash
   gh pr checks --watch
   ```

   失败时按 `ci-fix-loop` 分类处理；与本变更无关的既有失败标记
   UNRELATED 并在汇报中给出证据。

4. CI 全部通过后，向用户输出合并与收尾命令，由用户执行：

   ```bash
   gh pr merge --merge
   git checkout main
   git pull --ff-only origin main
   git branch -d <task-branch>
   ```

## 输出

- PR 链接与 CI 状态汇总（PASS / FAIL + RELATED 分类）。
- 可合并时给出上述用户命令；不可合并时给出 RELATED 失败清单与修复建议。

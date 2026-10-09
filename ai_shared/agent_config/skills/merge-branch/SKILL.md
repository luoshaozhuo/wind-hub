---
name: merge-branch
description: Use when a finished task branch must be merged back to main via GitHub PR, with CI verification, merge readiness checks, and final merge handed to the user.
---

# merge-branch

## 目的

将已完成的任务分支通过 GitHub PR 合并回 `main`。Agent 负责检查、创建 PR、验证 CI 和报告结果；最终合并与分支清理由用户执行。

## 原则

1. Agent 不直接在 `main` 上开发，不执行 `git merge`、`gh pr merge`、`git checkout`，不绕过 `git-safety-gate`。
2. 禁止 Agent 使用 `--admin` 绕过分支保护。
3. CI 失败按 RELATED / UNRELATED / UNKNOWN 分类：
   - RELATED：必须修复。
   - UNRELATED：提供明确证据，不擅自修改无关代码。
   - UNKNOWN：继续诊断，不能直接认定为可合并。
4. RELATED 失败衔接 `ci-fix-loop`。
5. CI 故障归因与 GitHub 合并许可独立判断。Required checks 未通过时不得宣告可合并。
6. 默认使用 Merge commit，保留任务分支提交历史；除非用户明确要求，不使用 Squash 或 Rebase。
7. 未经用户明确授权，不自动合并、删除分支或改写 Git 历史。

## 步骤

### 1. 检查分支状态

```bash
git fetch origin --prune
git branch --show-current
git status --short --branch
git branch -vv
git log origin/main..HEAD --oneline
git rev-list --left-right --count origin/main...HEAD
```

确认：

- 当前位于任务分支，而不是 `main`。
- 工作区和暂存区干净。
- 任务分支已有提交。
- 分支已推送，且与对应的远程分支同步。
- 已识别与 `origin/main` 的分叉情况。
- 不存在尚未处理的冲突或异常状态。

检查失败时停止，报告原因，不擅自修改历史。

### 2. 检查或创建 PR

```bash
gh pr list --head "<task-branch>" --state all
```

如果已存在对应的开放 PR，则复用。

如果尚不存在，则创建：

```bash
gh pr create \
  --base main \
  --head "<task-branch>" \
  --title "<summary>" \
  --body "<变更要点、测试结果、风险>"
```

PR 标题优先使用任务目标的准确摘要，不机械采用最近一次 commit 标题。

已关闭、已合并或 Draft PR 必须单独判断，不自动重复创建或改变状态。

### 3. 验证 CI

```bash
gh pr checks <PR-number> --watch
```

检查所有适用的 CI 结果。

失败时调用 `ci-fix-loop` 并分类：

- RELATED：修复并重新验证。
- UNRELATED：记录失败检查、日志与无关性证据。
- UNKNOWN：继续调查，不跳过。

区分全部检查、Required checks、可选检查以及尚未执行的检查。

无检查结果不等同于检查通过。

### 4. 检查 PR 合并条件

```bash
gh pr view <PR-number> \
  --json state,isDraft,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup,url
```

确认：

- PR 为 OPEN 且不是 Draft。
- 不存在合并冲突。
- Required checks 满足仓库规则。
- 必要代码审查已通过。
- GitHub 允许当前 PR 合并。
- 仓库允许 Merge commit。

若 GitHub 返回 UNKNOWN 或合并状态尚未确定，不推断为可合并。

### 5. 向用户交付合并命令

只有 PR 满足合并条件时，才提供：

```bash
gh pr merge <PR-number> --merge
```

不得自动执行。

不得默认添加 `--admin`。

如果不满足合并条件，必须说明具体阻塞项，不输出可直接执行的强制合并命令。

### 6. 合并后的用户收尾操作

用户确认 PR 已成功合并后，先检查本地状态：

```bash
git status --short --branch
git switch main
git pull --ff-only origin main
git branch -d <task-branch>
```

说明：

- 切换分支前确保工作区干净。
- 如果 `git pull --ff-only` 失败，停止并排查，不使用 `reset --hard`。
- 如果 `git branch -d` 失败，停止并检查提交是否已被包含，不自动改用 `-D`。
- 远程任务分支是否删除，由用户根据仓库策略决定。
- 不自动清理其他工作区、其他分支或未合并提交。

## 输出

必须包含：

1. 当前任务分支与远程同步状态。
2. PR 编号、链接和状态。
3. CI 检查汇总：PASS / FAIL / PENDING，以及失败归因。
4. GitHub 合并条件检查结果。
5. 最终结论：READY / BLOCKED / UNKNOWN。
6. READY 时给出用户合并命令和合并后收尾命令。
7. BLOCKED 或 UNKNOWN 时给出原因、证据与下一步操作。

禁止将 UNRELATED CI 失败直接视为可合并，也禁止为完成任务而绕过仓库保护规则。
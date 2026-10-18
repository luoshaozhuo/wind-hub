---
name: merge-branch
description: 由 Coding Agent 的 //merge-branch 指令触发；检查当前任务分支及 PR，执行一次本地受影响范围验证，通过后 Squash Merge 到 main 并验证。
---

# merge-branch

## 入口

用户调用 `//merge-branch` 即要求执行当前任务分支的合并，不再询问授权。

在仓库工作区，使用本 Skill 所在目录的脚本：

```bash
python <skill-dir>/scripts/merge.py merge
```

如果尚无 PR，先检查当前任务分支、创建指向 `main` 的 PR，再执行脚本。必须由运行在用户 wind-hub 工作区内的 Coding Agent 调用；远程 GitHub 修改无法代替本地 Conda 验收。
`check` 仅用于排错，不得用 READY 代替实际合并结果。

## 本地合并验收

用户触发合并时，`merge.py` 在确认远端分支与当前 HEAD 一致后，调用 `scripts/ci_gate.py merge`。验证范围由任务分支与 `origin/main` 的完整差异决定；开发、Commit、Push 和创建 PR 不强制运行完整产品测试。任何验证失败都阻止合并，代码或 main 变化后重新验收。GitHub Fast/PR 工作流仅运行轻量治理检查，不重复产品测试。

## 门禁

- 仅允许**当前任务分支 → main**，采用 **Squash Merge**；不得直接在本地 `main` 上操作。
- 工作区干净，分支已推送且与远程同步；PR 不是 Draft、没有冲突。
- CI 判定以仓库级 Actions 开关为准：经 GitHub API 明确 `actions/permissions.enabled=false`，且生效中的分支保护 / Ruleset 没有未满足的外部必需检查时，允许进入本地验收，报告 `CI=NOT_RUN_ACTIONS_DISABLED`，不得伪装成 PASS。
- Actions 启用时，所有适用 Check 必须完成且成功；零 Check、失败、取消、进行中均 **BLOCKED**。
- Actions 开关查询失败或返回未知，或禁用但存在未满足的外部 Required Checks，一律 **BLOCKED**；UNKNOWN 不得视作通过。
- Required Checks 与必需审查必须满足实际生效的 GitHub 分支保护 / Ruleset，不能仅凭 PR Check 数为 0 判定无门禁。轻量 GitHub Check 不能替代合并时本地产品验收。
- Actions 禁用不豁免本地受影响范围验证，Gate 仍必须 PASS。
- 不允许 `--admin`、强制推送或绕过保护；不设置 `--allow-no-ci` 或任何人肉绕过开关。CI 异常按 RELATED / UNRELATED / UNKNOWN 处理。
- 合并后核实 PR 为 MERGED，且 Squash Commit 已进入远程 `main` 历史。
- 若合并响应不确定，先重新查询 PR 状态，不盲目重复执行合并。
- 成功合并并核验远程 main 后，必须自动快进同步本地 main、切换到 main、删除经 PR HEAD 核验的本地任务分支；远程任务分支不自动删除。不得强行覆盖、重置或删除其他 worktree 使用的分支。

## Hook

当前 `git-safety-gate.py` 直接拦截 `gh pr merge`，但不拦截 Python 脚本入口。
合并脚本内部使用受 GitHub 分支保护约束的普通 `gh pr merge --squash`。
**不要求修改现有 Hook**；不关闭、不改写 Hook，不使用 `--admin`。
安全门禁由脚本与 GitHub 双重实施。

## 输出

报告 PR 链接、CI 状态、Squash Commit SHA、远程 `main` 验证结果和 MERGED / BLOCKED / UNKNOWN 状态。

合并成功后由 `merge.py` **自动**执行本地收尾：`fetch origin main`、验证快进条件、`git switch main`、`git merge --ff-only origin/main`、核对本地 main 与 origin/main 一致、删除已验证的本地任务分支。仅在合并事实已核验、工作区干净且无其他 worktree 占用时允许 `git branch -D`。输出 `MERGED_AND_CLEANED`；若远程已合并但本地收尾失败，则输出 `MERGED_LOCAL_CLEANUP_BLOCKED` 和原因，不将远程合并误报为失败。

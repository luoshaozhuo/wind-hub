---
name: merge-branch
description: 由 Coding Agent 的 //merge-branch 指令触发；检查当前任务分支及 PR，满足门禁后立即 Squash Merge 到 main 并验证。
---

# merge-branch

## 入口

用户调用 `//merge-branch` 即要求执行当前任务分支的合并，不再询问授权。

在仓库工作区，使用本 Skill 所在目录的脚本：

```bash
python <skill-dir>/scripts/merge.py merge --pr <PR-number>
```

如果尚无 PR，先检查当前任务分支、创建指向 `main` 的 PR，再执行脚本。
`check` 仅用于排错，不得用 READY 代替实际合并结果。

## 门禁

- 仅允许**当前任务分支 → main**，采用 **Squash Merge**；不得直接在本地 `main` 上操作。
- 工作区干净，分支已推送且与远程同步；PR 不是 Draft、没有冲突。
- CI 必须完成且成功；Required Checks 与必需审查必须满足 GitHub 分支保护规则。
- 没有 CI 检查结果时默认 **BLOCKED**，绝不视作通过。唯一降级路径：调用方显式加 `--allow-no-ci`，且脚本通过 API 确证仓库 GitHub Actions 被整体禁用（`actions/permissions` 的 `enabled=false`）。此时结果必须如实报告 **CI NOT_RUN（Actions 已禁用）**，不得写成 CI PASS；Actions 处于启用状态却无结果（未触发/未完成）、或启用状态无法确认时仍然 BLOCKED。
- 不允许 `--admin`、强制推送或绕过保护。CI 异常按 RELATED / UNRELATED / UNKNOWN 处理，UNKNOWN 不得视作通过。
- 合并后核实 PR 为 MERGED，且 Squash Commit 已进入远程 `main` 历史。
- 若合并响应不确定，先重新查询 PR 状态，不盲目重复执行合并。
- 不自动删除本地分支；远程分支仅根据独立的清理请求使用 `scripts/cleanup.py` 处理。

## Hook

当前 `git-safety-gate.py` 直接拦截 `gh pr merge`，但不拦截 Python 脚本入口。
合并脚本内部使用受 GitHub 分支保护约束的普通 `gh pr merge --squash`。
**不要求修改现有 Hook**；不关闭、不改写 Hook，不使用 `--admin`。
安全门禁由脚本与 GitHub 双重实施。

## 输出

报告 PR 链接、CI 状态、Squash Commit SHA、远程 `main` 验证结果和 MERGED / BLOCKED / UNKNOWN 状态。

合并成功后，只提示用户按需进行本地收尾：

```bash
git switch main
git pull --ff-only origin main
# 确认原开发分支不再需要后，按需执行：
git branch -D <task-branch>
```
